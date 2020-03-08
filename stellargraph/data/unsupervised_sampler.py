#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2019-2020 Data61, CSIRO
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__all__ = ["UnsupervisedSampler"]


import numpy as np

from stellargraph.core.utils import is_real_iterable
from stellargraph.core.graph import StellarGraph
from stellargraph.data.explorer import UniformRandomWalk
from stellargraph.data.explorer import BiasedRandomWalk
from stellargraph.random import random_state


class UnsupervisedSampler:
    """
        The UnsupervisedSampler is responsible for sampling walks in the given graph
        and returning positive and negative samples w.r.t. those walks, on demand.

        The positive samples are all the (target, context) pairs from the walks and the negative
        samples are contexts generated for each target based on a sampling distribtution.

        Currently uniform random walks and biased random walks are performed, other walk strategies
        will be enabled in the future.

        Args:
            G (StellarGraph): A stellargraph with features.
            nodes (optional, iterable) The root nodes from which individual walks start.
                If not provided, all nodes in the graph are used.
            length (int): An integer giving the length of the walks. Length must be at least 2.
            number_of_walks (int): Number of walks from each root node.
            seed(int): the seed used to generate the initial random state
            walker: the walker used to generate random walks, which can be an instance of UniformRandomWalk or BiasedRandomWalk. If walker
                is None, it will be set to an instance of UniformRandomWalk.
            **kwargs: optional hyperparameters (p, q, weighted) for biased random walkers.
    """

    def __init__(
        self,
        G,
        nodes=None,
        length=2,
        number_of_walks=1,
        seed=None,
        walker=None,
        **kwargs,
    ):
        if not isinstance(G, StellarGraph):
            raise ValueError(
                "({}) Graph must be a StellarGraph or StellarDigraph object.".format(
                    type(self).__name__
                )
            )
        else:
            self.graph = G

        if walker is not None:
            # only work with UniformRandomWalker and BiasedRandomWalker at the moment
            if not isinstance(walker, UniformRandomWalk) and not isinstance(
                walker, BiasedRandomWalk
            ):
                raise TypeError(
                    "({}) Only the UniformRandomWalks and BiasedRandomWalks are possible".format(
                        type(self).__name__
                    )
                )
            else:
                self.walker = walker
        else:
            self.walker = UniformRandomWalk(G, seed=seed)

        if isinstance(self.walker, BiasedRandomWalk):
            self.p = kwargs.get("p", 1.0)
            self.q = kwargs.get("q", 1.0)
            self.weighted = kwargs.get("weighted", False)

        # Define the root nodes for the walks
        # if no root nodes are provided for sampling defaulting to using all nodes as root nodes.
        if nodes is None:
            self.nodes = list(G.nodes())
        elif is_real_iterable(nodes):  # check whether the nodes provided are valid.
            self.nodes = list(nodes)
        else:
            raise ValueError("nodes parameter should be an iterable of node IDs.")

        # Require walks of at lease length two because to create a sample pair we need at least two nodes.
        if length < 2:
            raise ValueError(
                "({}) For generating (target,context) samples, walk length has to be at least 2".format(
                    type(self).__name__
                )
            )
        else:
            self.length = length

        if number_of_walks < 1:
            raise ValueError(
                "({}) At least 1 walk from each head node has to be done".format(
                    type(self).__name__
                )
            )
        else:
            self.number_of_walks = number_of_walks

        # Setup an interal random state with the given seed
        _, self.np_random = random_state(seed)

    def run(self, batch_size):
        """
        This method returns a batch_size number of positive and negative samples from the graph.
        A random walk is generated from each root node, which are transformed into positive context
        pairs, and the same number of negative pairs are generated from a global node sampling
        distribution. The resulting list of context pairs are shuffled and converted to batches of
        size ``batch_size``.

        Currently the global node sampling distribution for the negative pairs is the degree
        distribution to the 3/4 power. This is the same used in node2vec
        (https://snap.stanford.edu/node2vec/).

        Args:
             batch_size (int): The number of samples to generate for each batch.
                This must be an even number.

        Returns:
            List of batches, where each batch is a tuple of (list context pairs, list of labels)
        """
        self._check_parameter_values(batch_size)

        all_nodes = list(self.graph.nodes())
        # Use the sampling distribution as per node2vec
        degrees = self.graph.node_degrees()

        sampling_distribution = np.array([degrees[n] ** 0.75 for n in all_nodes])
        sampling_distribution_norm = sampling_distribution / np.sum(
            sampling_distribution
        )

        if isinstance(self.walker, UniformRandomWalk):  # for uniform random walk
            walks = self.walker.run(
                nodes=self.nodes, length=self.length, n=self.number_of_walks
            )
        else:  # for biased random walk
            walks = self.walker.run(
                nodes=self.nodes,
                length=self.length,
                n=self.number_of_walks,
                p=self.p,
                q=self.q,
                weighted=self.weighted,
            )

        # first item in each walk is the target/head node
        targets = [walk[0] for walk in walks]

        positive_pairs = np.array(
            [
                (target, positive_context)
                for target, walk in zip(targets, walks)
                for positive_context in walk[1:]
            ]
        )

        negative_samples = self.np_random.choice(
            all_nodes, size=len(positive_pairs), p=sampling_distribution_norm
        )
        negative_pairs = np.column_stack((positive_pairs[:, 0], negative_samples))

        pairs = np.concatenate((positive_pairs, negative_pairs), axis=0)
        labels = np.repeat([1, 0], len(positive_pairs))

        # shuffle indices - note this doesn't ensure an equal number of positive/negative examples in
        # each batch, just an equal number overall
        indices = self.np_random.permutation(len(pairs))

        batch_indices = [
            indices[i : i + batch_size] for i in range(0, len(indices), batch_size)
        ]

        return [(pairs[i], labels[i]) for i in batch_indices]

    def _check_parameter_values(self, batch_size):
        """
        Checks that the parameter values are valid or raises ValueError exceptions with a message indicating the
        parameter (the first one encountered in the checks) with invalid value.

        Args:
            batch_size: <int> number of samples to generate in each call of generator

        """

        if (
            batch_size is None
        ):  # must provide a batch size since this is an indicator of how many samples to return
            raise ValueError(
                "({}) The batch_size must be provided to generate samples for each batch in the epoch".format(
                    type(self).__name__
                )
            )

        if type(batch_size) != int:  # must be an integer
            raise TypeError(
                "({}) The batch_size must be positive integer.".format(
                    type(self).__name__
                )
            )

        if batch_size < 1:  # must be greater than 0
            raise ValueError(
                "({}) The batch_size must be positive integer.".format(
                    type(self).__name__
                )
            )

        if (
            batch_size % 2 != 0
        ):  # should be even since we generate 1 negative sample for each positive one.
            raise ValueError(
                "({}) The batch_size must be an even integer since equal number of positive and negative samples are generated in each batch.".format(
                    type(self).__name__
                )
            )
