# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""An agent that mixes a list of agents with a constant mixture distribution."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import gin
import tensorflow as tf  # pylint: disable=g-explicit-tensorflow-version-import

from tf_agents.agents import tf_agent
from tf_agents.bandits.policies import mixture_policy
from tf_agents.trajectories import trajectory
from tf_agents.utils import nest_utils


def _dynamic_partition_of_nested_tensors(nested_tensor, partitions,
                                         num_partitions):
  """This function takes a nested structure and partitions every element of it.

  Specifically it outputs a list of nest that all have the same structure as the
  original, and every element of the list is a nest that contains a dynamic
  partition of the corresponding original tensors.

  Note that this function uses tf.dynamic_partition, and thus
  'StaticMixtureAgent' is not compatible with XLA.

  Args:
    nested_tensor: The input nested structure to partition.
    partitions: int32 tensor based on which the partitioning happens.
    num_partitions: The number of expected partitions.

  Returns:
    A list of nested tensors with the same structure as `nested_tensor`.
  """
  flattened_tensors = tf.nest.flatten(nested_tensor)
  partitioned_flat_tensors = [
      tf.dynamic_partition(
          data=t, partitions=partitions, num_partitions=num_partitions)
      for t in flattened_tensors
  ]
  list_of_partitions = list(map(list, zip(*partitioned_flat_tensors)))
  return [
      tf.nest.pack_sequence_as(nested_tensor, i) for i in list_of_partitions
  ]


@gin.configurable
class StaticMixtureAgent(tf_agent.TFAgent):
  """An agent that mixes a set of agents with a given static mixture.

  For every data sample, the agent updates the sub-agent that was used to make
  the action choice in that sample. For this update to happen, the mixture agent
  needs to have the information on which sub-agent is "responsible" for the
  action. This information is in a policy info field `mixture_agent_id`.

  Note that this agent makes use of `tf.dynamic_partition`, and thus it is not
  compatible with XLA.
  """

  def __init__(self, mixture_weights, agents, name=None):
    """Initializes an instance of `StaticMixtureAgent`.

    Args:
      mixture_weights: (list of floats) The (possibly unnormalized) probability
        distribution based on which the agent chooses the sub-agents.
      agents: List of instances of TF-Agents bandit agents. These agents will be
        trained and used to select actions. The length of this list should match
        that of `mixture_weights`.
      name: The name of this instance of `StaticMixtureAgent`.
    """
    tf.Module.__init__(self, name=name)
    time_step_spec = agents[0].time_step_spec
    action_spec = agents[0].action_spec
    self._original_info_spec = agents[0].policy.info_spec
    error_message = None
    for agent in agents[1:]:
      if action_spec != agent.action_spec:
        error_message = 'Inconsistent action specs.'
      if time_step_spec != agent.time_step_spec:
        error_message = 'Inconsistent time step specs.'
      if self._original_info_spec != agent.policy.info_spec:
        error_message = 'Inconsistent info specs.'
    if len(mixture_weights) != len(agents):
      error_message = '`mixture_weights` and `agents` must have equal length.'
    if error_message is not None:
      raise ValueError(error_message)
    self._agents = agents
    self._num_agents = len(agents)
    self._mixture_weights = mixture_weights
    policies = [agent.collect_policy for agent in agents]
    policy = mixture_policy.MixturePolicy(mixture_weights, policies)
    super(StaticMixtureAgent, self).__init__(
        time_step_spec, action_spec, policy, policy, train_sequence_length=None)

  def _train(self, experience, weights=None):
    del weights  # unused

    reward, _ = nest_utils.flatten_multi_batched_nested_tensors(
        experience.reward, self._time_step_spec.reward)
    action, _ = nest_utils.flatten_multi_batched_nested_tensors(
        experience.action, self._action_spec)
    observation, _ = nest_utils.flatten_multi_batched_nested_tensors(
        experience.observation, self._time_step_spec.observation)
    policy_choice, _ = nest_utils.flatten_multi_batched_nested_tensors(
        experience.policy_info[mixture_policy.MIXTURE_AGENT_ID],
        self._time_step_spec.reward)
    original_infos, _ = nest_utils.flatten_multi_batched_nested_tensors(
        experience.policy_info[mixture_policy.SUBPOLICY_INFO],
        self._original_info_spec)

    partitioned_nested_infos = nest_utils.batch_nested_tensors(
        _dynamic_partition_of_nested_tensors(original_infos, policy_choice,
                                             self._num_agents))

    partitioned_nested_rewards = [
        nest_utils.batch_nested_tensors(t)
        for t in _dynamic_partition_of_nested_tensors(reward, policy_choice,
                                                      self._num_agents)
    ]
    partitioned_nested_actions = [
        nest_utils.batch_nested_tensors(t)
        for t in _dynamic_partition_of_nested_tensors(action, policy_choice,
                                                      self._num_agents)
    ]
    partitioned_nested_observations = [
        nest_utils.batch_nested_tensors(t)
        for t in _dynamic_partition_of_nested_tensors(
            observation, policy_choice, self._num_agents)
    ]
    loss = 0
    for k in range(self._num_agents):
      experience = trajectory.single_step(
          observation=partitioned_nested_observations[k],
          action=partitioned_nested_actions[k],
          policy_info=partitioned_nested_infos[k],
          reward=partitioned_nested_rewards[k],
          discount=tf.zeros_like(partitioned_nested_rewards[k]))
      loss_info = self._agents[k].train(experience)
      loss += loss_info.loss
    return tf_agent.LossInfo(loss=(loss), extra=())
