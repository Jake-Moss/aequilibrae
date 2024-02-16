# cython: language_level=3str

"""This module aims to implemented the BFS-LE algorithm as described in Rieser-Schüssler, Balmer, and Axhausen, 'Route
Choice Sets for Very High-Resolution Data'.  https://doi.org/10.1080/18128602.2012.671383

A rough overview of the algorithm is as follows.
    1. Prepare the initial graph, this is depth 0 with no links removed.
    2. Find a short path, P. If P is not empty add P to the path set.
    3. For all links p in P, remove p from E, compounding with the previously removed links.
    4. De-duplicate the sub-graphs, we only care about unique sub-graphs.
    5. Go to 2.

Details: The general idea of the algorithm is pretty simple, as is the implementation. The caveats here is that there is
a lot of cpp interop and memory management. A description of the purpose of variables is in order:

route_set: See route_choice.pxd for full type signature. It's an unordered set (hash set) of pointers to vectors of link
IDs. It uses a custom hashing function and comparator. The hashing function is defined in a string that in inlined
directly into the output ccp. This is done allow declaring of the `()` operator, which is required and AFAIK not
possible in Cython. The hash is designed to dereference then hash order dependent vectors. One isn't provided by
stdlib. The comparator simply dereferences the pointer and uses the vector comparator. It's designed to store the
outputted paths. Heap allocated (needs to be returned).

removed_links: See route_choice.pxd for full type signature. It's an unordered set of pointers to unordered sets of link
IDs. Similarly to `route_set` is uses a custom hash function and comparator. This hash function is designed to be order
independent and should only use commutative operations. The comparator is the same. It's designed to store all of the
removed link sets we've seen before. This allows us to detected duplicated graphs.

rng: A custom imported version of std::linear_congruential_engine. libcpp doesn't provide one so we do. It should be
significantly faster than the std::mersenne_twister_engine without sacrificing much. We don't need amazing RNG, just
ok and fast. This is only used to shuffle the queue.

queue, next_queue: These are vectors of pointers to sets of removed links. We never need to push to the front of these so a
vector is best. We maintain two queues, one that we are currently iterating over, and one that we can add to, building
up with all the newly removed link sets. These two are swapped at the end of an iteration, next_queue is then
cleared. These store sets of removed links.

banned, next_banned: `banned` is the iterator variable for `queue`. `banned` is copied into `next_banned` where another
link can be added without mutating `banned`. If we've already seen this set of removed links `next_banned` is immediately
deallocated. Otherwise it's placed into `next_queue`.

vec: `vec` is a scratch variable to store pointers to new vectors, or rather, paths while we are building them. Each time a path
is found a new one is allocated, built, and stored in the route_set.

p, connector: Scratch variables for iteration.

Optimisations: As described in the paper, both optimisations have been implemented. The path finding operates on the
compressed graph and the queue is shuffled if its possible to fill the route set that iteration. The route set may not
be filled due to duplicate paths but we can't know that in advance so we shuffle anyway.

Any further optimisations should focus on the path finding, from benchmarks it dominates the run time (~98%). Since huge
routes aren't required small-ish things like the memcpy and banned link set copy aren't high priority.

"""

from aequilibrae import Graph

from libc.math cimport INFINITY
from libc.string cimport memcpy
from libc.limits cimport UINT_MAX
from libc.stdlib cimport abort
from libcpp.vector cimport vector
from libcpp.unordered_set cimport unordered_set
from libcpp.unordered_map cimport unordered_map
from libcpp.utility cimport pair
from libcpp.algorithm cimport sort
from cython.operator cimport dereference as deref, preincrement as inc
from cython.parallel cimport parallel, prange, threadid

import numpy as np
import pyarrow as pa
from typing import List, Tuple
import itertools
import pathlib
import logging
import warnings

cimport numpy as np  # Numpy *must* be cimport'd BEFORE pyarrow.lib, there's nothing quite like Cython.
cimport pyarrow as pa
cimport pyarrow.lib as libpa
import pyarrow.dataset
import pyarrow.parquet as pq
from libcpp.memory cimport shared_ptr

from libc.stdio cimport fprintf, printf, stderr

# It would really be nice if these were modules. The 'include' syntax is long deprecated and adds a lot to compilation times
include 'basic_path_finding.pyx'

@cython.embedsignature(True)
cdef class RouteChoiceSet:
    """
    Route choice implemented via breadth first search with link removal (BFS-LE) as described in Rieser-Schüssler,
    Balmer, and Axhausen, 'Route Choice Sets for Very High-Resolution Data'
    """

    route_set_dtype = pa.list_(pa.uint32())

    schema = pa.schema([
        pa.field("origin id", pa.uint32(), nullable=False),
        pa.field("destination id", pa.uint32(), nullable=False),
        pa.field("route set", route_set_dtype, nullable=False),
    ])

    def __cinit__(self):
        """C level init. For C memory allocation and initialisation. Called exactly once per object."""
        pass

    def __init__(self, graph: Graph):
        """Python level init, may be called multiple times, for things that can't be done in __cinit__."""
        # self.heuristic = HEURISTIC_MAP[self.res._heuristic]
        self.cost_view = graph.compact_cost
        self.graph_fs_view = graph.compact_fs
        self.b_nodes_view = graph.compact_graph.b_node.values
        self.nodes_to_indices_view = graph.compact_nodes_to_indices

        # tmp = graph.lonlat_index.loc[graph.compact_all_nodes]
        # self.lat_view = tmp.lat.values
        # self.lon_view = tmp.lon.values
        self.a_star = False

        self.ids_graph_view = graph.compact_graph.id.values
        self.num_nodes = graph.compact_num_nodes
        self.zones = graph.num_zones
        self.block_flows_through_centroids = graph.block_centroid_flows


    def __dealloc__(self):
        """
        C level deallocation. For freeing memory allocated by this object. *Must* have GIL, `self` may be in a
        partially deallocated state already.
        """
        pass

    @cython.embedsignature(True)
    def run(self, origin: int, destination: int, *args, **kwargs):
        """
        Compute the a route set for a single OD pair.

        Often the returned list's length is ``max_routes``, however, it may be limited by ``max_depth`` or if all
        unique possible paths have been found then a smaller set will be returned.

        Thin wrapper around ``RouteChoiceSet.batched``. Additional arguments are forwarded to ``RouteChoiceSet.batched``.

        :Arguments:
            **origin** (:obj:`int`): Origin node ID. Must be present within compact graph. Recommended to choose a centroid.
            **destination** (:obj:`int`): Destination node ID. Must be present within compact graph. Recommended to choose a centroid.

        :Returns:
            **route set** (:obj:`list[tuple[int, ...]]): Returns a list of unique variable length tuples of compact link IDs.
                                                         Represents paths from ``origin`` to ``destination``.
        """
        return [tuple(x) for x in self.batched([(origin, destination)], *args, **kwargs).column("route set").to_pylist()]

    # Bounds checking doesn't really need to be disabled here but the warning is annoying
    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.embedsignature(True)
    @cython.initializedcheck(False)
    def batched(
            self,
            ods: List[Tuple[int, int]],
            max_routes: int = 0,
            max_depth: int = 0,
            seed: int = 0,
            cores: int = 1,
            a_star: bool = True,
            bfsle: bool = True,
            penalty: float = 0.0,
            where: Optional[str] = None,
            freq_as_well = False,
            cost_as_well = False,
    ):
        """
        Compute the a route set for a list of OD pairs.

        Often the returned list for each OD pair's length is ``max_routes``, however, it may be limited by ``max_depth`` or if all
        unique possible paths have been found then a smaller set will be returned.

        :Arguments:
            **ods** (:obj:`list[tuple[int, int]]`): List of OD pairs ``(origin, destination)``. Origin and destination node ID must be
                                                    present within compact graph. Recommended to choose a centroids.
            **max_routes** (:obj:`int`): Maximum size of the generated route set. Must be non-negative. Default of ``0`` for unlimited.
            **max_depth** (:obj:`int`): Maximum depth BFSLE can explore, or maximum number of iterations for link penalisation.
                                        Must be non-negative. Default of ``0`` for unlimited.
            **seed** (:obj:`int`): Seed used for rng. Must be non-negative. Default of ``0``.
            **cores** (:obj:`int`): Number of cores to use when parallelising over OD pairs. Must be non-negative. Default of ``1``.
            **bfsle** (:obj:`bool`): Whether to use Breadth First Search with Link Removal (BFSLE) over link penalisation. Default ``True``.
            **penalty** (:obj:`float`): Penalty to use for Link Penalisation. Must be ``> 1.0``. Not compatible with ``bfsle=True``.
            **where** (:obj:`str`): Optional file path to save results to immediately. Will return None.

        :Returns:
            **route sets** (:obj:`dict[tuple[int, int], list[tuple[int, ...]]]`): Returns a list of unique tuples of compact link IDs for
                each OD pair provided (as keys). Represents paths from ``origin`` to ``destination``. None if ``where`` was not None.
        """
        cdef:
            long long o, d

        if max_routes == 0 and max_depth == 0:
            raise ValueError("Either `max_routes` or `max_depth` must be > 0")

        if max_routes < 0 or max_depth < 0 or cores < 0:
            raise ValueError("`max_routes`, `max_depth`, and `cores` must be non-negative")

        if penalty != 0.0 and bfsle:
            raise ValueError("Link penalisation (`penatly` > 1.0) and `bfsle` cannot be enabled at once")

        if not bfsle and penalty <= 1.0:
            raise ValueError("`penalty` must be > 1.0. `penalty=1.1` is recommended")

        for o, d in ods:
            if self.nodes_to_indices_view[o] == -1:
                raise ValueError(f"Origin {o} is not present within the compact graph")
            if self.nodes_to_indices_view[d] == -1:
                raise ValueError(f"Destination {d} is not present within the compact graph")

        cdef:
            long long origin_index, dest_index, i
            unsigned int c_max_routes = max_routes
            unsigned int c_max_depth = max_depth
            unsigned int c_seed = seed
            unsigned int c_cores = cores

            vector[pair[long long, long long]] c_ods

            # A* (and Dijkstra's) require memory views, so we must allocate here and take slices. Python can handle this memory
            double [:, :] cost_matrix = np.empty((cores, self.cost_view.shape[0]), dtype=float)
            long long [:, :] predecessors_matrix = np.empty((cores, self.num_nodes + 1), dtype=np.int64)
            long long [:, :] conn_matrix = np.empty((cores, self.num_nodes + 1), dtype=np.int64)
            long long [:, :] b_nodes_matrix = np.broadcast_to(self.b_nodes_view, (cores, self.b_nodes_view.shape[0])).copy()

            # This matrix is never read from, it exists to allow using the Dijkstra's method without changing the
            # interface.
            long long [:, :] _reached_first_matrix

            vector[RouteSet_t *] *results

        # self.a_star = a_star

        if self.a_star:
            _reached_first_matrix = np.zeros((cores, 1), dtype=np.int64)  # Dummy array to allow slicing
        else:
            _reached_first_matrix = np.zeros((cores, self.num_nodes + 1), dtype=np.int64)

        set_ods = set(ods)
        if len(set_ods) != len(ods):
            warnings.warn(f"Duplicate OD pairs found, dropping {len(ods) - len(set_ods)} OD pairs")

        if where is not None:
            checkpoint = Checkpoint(where, self.schema, partition_cols=["origin id"])
            batches = list(Checkpoint.batches(list(set_ods)))
            results = new vector[RouteSet_t *](<size_t>max(len(batch) for batch in batches))
        else:
            batches = [list(set_ods)]
            results = new vector[RouteSet_t *](len(set_ods))

        for batch in batches:
            results.resize(len(batch))  # We know we've allocated enough size to store all max length batch but we resize to a smaller size when not needed
            c_ods = batch  # Convert the batch to a cpp vector, this isn't strictly efficient but is nicer
            with nogil, parallel(num_threads=c_cores):
                for i in prange(c_ods.size()):
                    origin_index = self.nodes_to_indices_view[c_ods[i].first]
                    dest_index = self.nodes_to_indices_view[c_ods[i].second]

                    if self.block_flows_through_centroids:
                        blocking_centroid_flows(
                            0,  # Always blocking
                            origin_index,
                            self.zones,
                            self.graph_fs_view,
                            b_nodes_matrix[threadid()],
                            self.b_nodes_view,
                        )

                    if bfsle:
                        deref(results)[i] = RouteChoiceSet.bfsle(
                            self,
                            origin_index,
                            dest_index,
                            c_max_routes,
                            c_max_depth,
                            cost_matrix[threadid()],
                            predecessors_matrix[threadid()],
                            conn_matrix[threadid()],
                            b_nodes_matrix[threadid()],
                            _reached_first_matrix[threadid()],
                            c_seed,
                        )
                    else:
                        deref(results)[i] = RouteChoiceSet.link_penalisation(
                            self,
                            origin_index,
                            dest_index,
                            c_max_routes,
                            c_max_depth,
                            cost_matrix[threadid()],
                            predecessors_matrix[threadid()],
                            conn_matrix[threadid()],
                            b_nodes_matrix[threadid()],
                            _reached_first_matrix[threadid()],
                            penalty,
                            c_seed,
                        )

                    if self.block_flows_through_centroids:
                        blocking_centroid_flows(
                            1,  # Always unblocking
                            origin_index,
                            self.zones,
                            self.graph_fs_view,
                            b_nodes_matrix[threadid()],
                            self.b_nodes_view,
                        )

            table = libpa.pyarrow_wrap_table(RouteChoiceSet.make_table_from_results(c_ods, deref(results)))


            if freq_as_well:
                freqs = []
                for freq in deref(RouteChoiceSet.frequency(deref(results), c_cores)):
                    freqs.append(
                        (
                            list(deref(freq.first)),
                            list(deref(freq.second)),
                        )
                    )

            if cost_as_well:
                costs = []
                for cost_vec in deref(RouteChoiceSet.compute_cost(deref(results), self.cost_view, c_cores)):
                    costs.append(list(deref(cost_vec)))

            # Once we've made the table all results have been copied into some pyarrow structure, we can free our internal structures
            for result in deref(results):
                for route in deref(result):
                    del route

            if where is None:  # There was only one batch anyway
                if cost_as_well:
                    return table, costs
                elif freq_as_well:
                    return table, freqs
                else:
                    return table

            checkpoint.write(table)

            del table

        return

    @cython.initializedcheck(False)
    cdef void path_find(
        RouteChoiceSet self,
        long origin_index,
        long dest_index,
        double [:] thread_cost,
        long long [:] thread_predecessors,
        long long [:] thread_conn,
        long long [:] thread_b_nodes,
        long long [:] _thread_reached_first
    ) noexcept nogil:
        """Small wrapper around path finding, thread locals should be passes as arguments."""
        if self.a_star:
            path_finding_a_star(
                origin_index,
                dest_index,
                thread_cost,
                thread_b_nodes,
                self.graph_fs_view,
                self.nodes_to_indices_view,
                self.lat_view,
                self.lon_view,
                thread_predecessors,
                self.ids_graph_view,
                thread_conn,
                EQUIRECTANGULAR  # FIXME: enum import failing due to redefinition
            )
        else:
            path_finding(
                origin_index,
                dest_index,
                thread_cost,
                thread_b_nodes,
                self.graph_fs_view,
                thread_predecessors,
                self.ids_graph_view,
                thread_conn,
                _thread_reached_first
            )

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.embedsignature(True)
    @cython.initializedcheck(False)
    cdef RouteSet_t *bfsle(
        RouteChoiceSet self,
        long origin_index,
        long dest_index,
        unsigned int max_routes,
        unsigned int max_depth,
        double [:] thread_cost,
        long long [:] thread_predecessors,
        long long [:] thread_conn,
        long long [:] thread_b_nodes,
        long long [:] _thread_reached_first,
        unsigned int seed
    ) noexcept nogil:
        """Main method for route set generation. See top of file for commentary."""
        cdef:
            RouteSet_t *route_set
            LinkSet_t removed_links
            minstd_rand rng

            # Scratch objects
            vector[unordered_set[long long] *] queue
            vector[unordered_set[long long] *] next_queue
            unordered_set[long long] *banned
            unordered_set[long long] *new_banned
            vector[long long] *vec
            long long p, connector

        max_routes = max_routes if max_routes != 0 else UINT_MAX
        max_depth = max_depth if max_depth != 0 else UINT_MAX

        queue.push_back(new unordered_set[long long]()) # Start with no edges banned
        route_set = new RouteSet_t()
        rng.seed(seed)

        # We'll go at most `max_depth` iterations down, at each depth we maintain a queue of the next set of banned edges to consider
        for depth in range(max_depth):
            if route_set.size() >= max_routes or queue.size() == 0:
                break

            # If we could potentially fill the route_set after this depth, shuffle the queue
            if queue.size() + route_set.size() >= max_routes:
                shuffle(queue.begin(), queue.end(), rng)

            for banned in queue:
                # Copying the costs back into the scratch costs buffer. We could keep track of the modifications and reverse them as well
                memcpy(&thread_cost[0], &self.cost_view[0], self.cost_view.shape[0] * sizeof(double))

                for connector in deref(banned):
                    thread_cost[connector] = INFINITY

                RouteChoiceSet.path_find(self, origin_index, dest_index, thread_cost, thread_predecessors, thread_conn, thread_b_nodes, _thread_reached_first)

                # Mark this set of banned links as seen
                removed_links.insert(banned)

                # If the destination is reachable we must build the path and readd
                if thread_predecessors[dest_index] >= 0:
                    vec = new vector[long long]()
                    # Walk the predecessors tree to find our path, we build it up in a cpp vector because we can't know how long it'll be
                    p = dest_index
                    while p != origin_index:
                        connector = thread_conn[p]
                        p = thread_predecessors[p]
                        vec.push_back(connector)

                    for connector in deref(vec):
                        # This is one area for potential improvement. Here we construct a new set from the old one, copying all the elements
                        # then add a single element. An incremental set hash function could be of use. However, the since of this set is
                        # directly dependent on the current depth and as the route set size grows so incredibly fast the depth will rarely get
                        # high enough for this to matter.
                        # Copy the previously banned links, then for each vector in the path we add one and push it onto our queue
                        new_banned = new unordered_set[long long](deref(banned))
                        new_banned.insert(connector)
                        # If we've already seen this set of removed links before we already know what the path is and its in our route set
                        if removed_links.find(new_banned) != removed_links.end():
                            del new_banned
                        else:
                            next_queue.push_back(new_banned)

                    # The deduplication of routes occurs here
                    route_set.insert(vec)
                    if route_set.size() >= max_routes:
                        break

            queue.swap(next_queue)
            next_queue.clear()

        # We may have added more banned link sets to the queue then found out we hit the max depth, we should free those
        for banned in queue:
            del banned

        # We should also free all the sets in removed_links, we don't be needing them
        for banned in removed_links:
            del banned

        return route_set

    @cython.wraparound(False)
    @cython.embedsignature(True)
    @cython.boundscheck(False)
    @cython.initializedcheck(False)
    cdef RouteSet_t *link_penalisation(
        RouteChoiceSet self,
        long origin_index,
        long dest_index,
        unsigned int max_routes,
        unsigned int max_depth,
        double [:] thread_cost,
        long long [:] thread_predecessors,
        long long [:] thread_conn,
        long long [:] thread_b_nodes,
        long long [:] _thread_reached_first,
        double penatly,
        unsigned int seed
    ) noexcept nogil:
        cdef:
            RouteSet_t *route_set

            # Scratch objects
            vector[long long] *vec
            long long p, connector

        max_routes = max_routes if max_routes != 0 else UINT_MAX
        max_depth = max_depth if max_depth != 0 else UINT_MAX
        route_set = new RouteSet_t()
        memcpy(&thread_cost[0], &self.cost_view[0], self.cost_view.shape[0] * sizeof(double))

        for depth in range(max_depth):
            if route_set.size() >= max_routes:
                break

            RouteChoiceSet.path_find(self, origin_index, dest_index, thread_cost, thread_predecessors, thread_conn, thread_b_nodes, _thread_reached_first)

            if thread_predecessors[dest_index] >= 0:
                vec = new vector[long long]()
                # Walk the predecessors tree to find our path, we build it up in a cpp vector because we can't know how long it'll be
                p = dest_index
                while p != origin_index:
                    connector = thread_conn[p]
                    p = thread_predecessors[p]
                    vec.push_back(connector)

                for connector in deref(vec):
                    thread_cost[connector] *= penatly

                route_set.insert(vec)
            else:
                break

        return route_set

    @cython.wraparound(False)
    @cython.embedsignature(True)
    @cython.boundscheck(False)
    @cython.initializedcheck(False)
    @staticmethod
    cdef vector[pair[vector[long long] *, vector[long long] *]] *frequency(vector[RouteSet_t *] &route_sets, unsigned int cores) noexcept nogil:
        cdef:
            vector[pair[vector[long long] *, vector[long long] *]] *freq_set = new vector[pair[vector[long long] *, vector[long long] *]]()
            vector[long long] *keys
            vector[long long] *counts

            # Scratch objects
            vector[long long] *link_union
            size_t length, count
            long long link, i

        freq_set.reserve(route_sets.size())

        with parallel(num_threads=cores):
            link_union = new vector[long long]()
            for i in prange(route_sets.size()):
                route_set = route_sets[i]

                link_union.clear()

                keys = new vector[long long]()
                counts = new vector[long long]()

                length = 0
                for route in deref(route_set):
                    length = length + route.size()
                link_union.reserve(length)

                for route in deref(route_set):
                    link_union.insert(link_union.end(), route.begin(), route.end())

                sort(link_union.begin(), link_union.end())

                union_iter = link_union.begin()
                while union_iter != link_union.end():
                    count = 0
                    link = deref(union_iter)
                    while link == deref(union_iter):
                        count = count + 1
                        inc(union_iter)

                    keys.push_back(link)
                    counts.push_back(count)

                freq_set.emplace(freq_set.cbegin() + i, keys, counts)

            del link_union

        return freq_set

    @cython.wraparound(False)
    @cython.embedsignature(True)
    @cython.boundscheck(False)
    @cython.initializedcheck(False)
    @staticmethod
    cdef vector[vector[double] *] *compute_cost(vector[RouteSet_t *] &route_sets, double[:] cost_view, unsigned int cores) noexcept nogil:
        cdef:
            vector[vector[double] *] *cost_set = new vector[vector[double] *](route_sets.size())
            vector[double] *cost_vec

            # Scratch objects
            vector[long long] *link_union
            size_t length,
            double cost
            long long link, i

        with parallel(num_threads=cores):
            for i in prange(route_sets.size()):
                route_set = route_sets[i]
                cost_vec = new vector[double]()
                cost_vec.reserve(route_set.size())

                for route in deref(route_set):
                    cost = 0.0
                    for link in deref(route):
                        cost = cost + cost_view[link]

                    cost_vec.push_back(cost)

                deref(cost_set)[i] = cost_vec

        return cost_set

    @cython.wraparound(False)
    @cython.embedsignature(True)
    @cython.boundscheck(False)
    @cython.initializedcheck(False)
    @staticmethod
    cdef shared_ptr[libpa.CTable] make_table_from_results(vector[pair[long long, long long]] &ods, vector[RouteSet_t *] &route_sets):
        cdef:
            shared_ptr[libpa.CArray] paths
            shared_ptr[libpa.CArray] offsets
            libpa.CMemoryPool *pool = libpa.c_get_memory_pool()
            libpa.CUInt32Builder *path_builder = new libpa.CUInt32Builder(pool)
            libpa.CInt32Builder *offset_builder = new libpa.CInt32Builder(pool)  # Must be Int32 *not* UInt32
            libpa.CUInt32Builder *o_col = new libpa.CUInt32Builder(pool)
            libpa.CUInt32Builder *d_col = new libpa.CUInt32Builder(pool)
            vector[shared_ptr[libpa.CArray]] columns
            shared_ptr[libpa.CDataType] route_set_dtype = libpa.pyarrow_unwrap_data_type(RouteChoiceSet.route_set_dtype)

            libpa.CResult[shared_ptr[libpa.CArray]] route_set_results

            vector[long long].reverse_iterator riter

            int offset = 0

        columns.emplace_back(shared_ptr[libpa.CArray]())  # Origins
        columns.emplace_back(shared_ptr[libpa.CArray]())  # Destination
        columns.emplace_back(shared_ptr[libpa.CArray]())  # Route set

        for i in range(ods.size()):
            route_set = route_sets[i]

            # Instead of construction a "list of lists" style object for storing the route sets we instead will construct one big array of link ids
            # with a corresponding offsets array that indicates where each new row (path) starts.
            for route in deref(route_set):
                o_col.Append(ods[i].first)
                d_col.Append(ods[i].second)
                offset_builder.Append(offset)
                # TODO: Pyarrows Cython API is incredibly lacking, it's just functional and doesn't include all the nice things the C++ API has
                # One such thing its missing the AppendValues, which can add whole iterators at once in a much smarter fashion.
                # We'll have to reimport/extern the classes we use if we want to avoid the below
                riter = route.rbegin()
                while riter != route.rend():
                    path_builder.Append(deref(riter))
                    inc(riter)

                offset += route.size()

        path_builder.Finish(&paths)
        offset_builder.Append(offset)  # Mark the end of the array in offsets
        offset_builder.Finish(&offsets)

        route_set_results = libpa.CListArray.FromArraysAndType(route_set_dtype, deref(offsets.get()), deref(paths.get()), pool, shared_ptr[libpa.CBuffer]())

        o_col.Finish(&columns[0])
        d_col.Finish(&columns[1])
        columns[2] = deref(route_set_results)

        cdef shared_ptr[libpa.CTable] table = libpa.CTable.MakeFromArrays(libpa.pyarrow_unwrap_schema(RouteChoiceSet.schema), columns)

        del path_builder
        del offset_builder
        del o_col
        del d_col
        return table


@cython.embedsignature(True)
cdef class Checkpoint:
    """
    A small wrapper class to write a dataset partition by partition
    """

    def __init__(self, where, schema, partition_cols = None):
        """Python level init, may be called multiple times, for things that can't be done in __cinit__."""
        self.where = pathlib.Path(where)
        self.schema = schema
        self.partition_cols = partition_cols

    def write(self, table):
        logger = logging.getLogger("aequilibrae")
        pq.write_to_dataset(
            table,
            self.where,
            partitioning=self.partition_cols,
            partitioning_flavor="hive",
            schema=self.schema,
            use_threads=True,
            existing_data_behavior="overwrite_or_ignore",
            file_visitor=lambda written_file: logger.info(f"Wrote partition dataset at {written_file.path}")
        )

    def read_dataset(self):
        return pa.dataset.dataset(self.where, format="parquet", partitioning=pa.dataset.HivePartitioning(self.schema))

    @staticmethod
    def batches(ods: List[Tuple[int, int]]):
        return (list(g) for k, g in itertools.groupby(sorted(ods), key=lambda x: x[0]))
