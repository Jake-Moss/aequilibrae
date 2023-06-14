# cython: language_level=3

import pandas as pd
import numpy as np
cimport numpy as cnp
from cython.parallel import prange, parallel
cimport openmp
from libc.stdlib cimport malloc, free
from libc.string cimport memset


include 'hyperpath.pyx'


class HyperpathGenerating:
    def __init__(self, edges, tail="tail", head="head", trav_time="trav_time", freq="freq", check_edges=False):
        # load the edges
        if check_edges:
            self._check_edges(edges, tail, head, trav_time, freq)
        self._edges = edges[[tail, head, trav_time, freq]].copy(deep=True)
        self.edge_count = len(self._edges)

        # remove inf values if any, and values close to zero
        self._edges[trav_time] = np.where(
            self._edges[trav_time] > DATATYPE_INF_PY, DATATYPE_INF_PY, self._edges[trav_time]
        )
        self._edges[trav_time] = np.where(
            self._edges[trav_time] < A_VERY_SMALL_TIME_INTERVAL_PY,
            A_VERY_SMALL_TIME_INTERVAL_PY,
            self._edges[trav_time],
        )
        self._edges[freq] = np.where(self._edges[freq] > INF_FREQ_PY, INF_FREQ_PY, self._edges[freq])
        self._edges[freq] = np.where(self._edges[freq] < MIN_FREQ_PY, MIN_FREQ_PY, self._edges[freq])

        # create an edge index column
        self._edges = self._edges.reset_index(drop=True)
        data_col = "edge_idx"
        self._edges[data_col] = self._edges.index

        # convert to CSC format
        self.vertex_count = self._edges[[tail, head]].max().max() + 1
        rs_indptr, _, rs_data = convert_graph_to_csc_uint32(self._edges, tail, head, data_col, self.vertex_count)
        self._indptr = rs_indptr.astype(np.uint32)
        self._edge_idx = rs_data.astype(np.uint32)

        # edge attributes
        self._trav_time = self._edges[trav_time].values.astype(DATATYPE_PY)
        self._freq = self._edges[freq].values.astype(DATATYPE_PY)
        self._tail = self._edges[tail].values.astype(np.uint32)
        self._head = self._edges[head].values.astype(np.uint32)

    def run(self, origin, destination, volume, return_inf=False):
        # column storing the resulting edge volumes
        self._edges["volume"] = 0.0
        self.u_i_vec = None

        # allocation of work arrays
        u_i_vec = np.empty(self.vertex_count, dtype=DATATYPE_PY)
        f_i_vec = np.empty(self.vertex_count, dtype=DATATYPE_PY)
        u_j_c_a_vec = np.empty(self.edge_count, dtype=DATATYPE_PY) 
        v_i_vec = np.empty(self.vertex_count, dtype=DATATYPE_PY)
        h_a_vec = np.empty(self.edge_count, dtype=bool)
        edge_indices = np.empty(self.edge_count, dtype=np.uint32)

        # input check
        if type(volume) is not list:
            volume = [volume]
        if type(origin) is not list:
            origin = [origin]
        assert len(origin) == len(volume)
        for i, item in enumerate(origin):
            self._check_vertex_idx(item)
            self._check_volume(volume[i])
        self._check_vertex_idx(destination)
        demand_indices = np.array(origin, dtype=np.uint32)
        assert isinstance(return_inf, bool)

        demand_values = np.array(volume, dtype=DATATYPE_PY)

        # compute_SF_in(
        #     self._indptr,
        #     self._edge_idx,
        #     self._trav_time,
        #     self._freq,
        #     self._tail,
        #     self._head,
        #     demand_indices,
        #     demand_values,
        #     self._edges["volume"].values,
        #     u_i_vec,
        #     f_i_vec,
        #     u_j_c_a_vec,
        #     v_i_vec,
        #     h_a_vec,
        #     edge_indices,
        #     self.vertex_count,
        #     destination,
        # )
        self.u_i_vec = u_i_vec

    def _check_vertex_idx(self, idx):
        assert isinstance(idx, int)
        assert idx >= 0
        assert idx < self.vertex_count

    def _check_volume(self, v):
        assert isinstance(v, float)
        assert v >= 0.0

    def _check_edges(self, edges, tail, head, trav_time, freq):
        if type(edges) != pd.core.frame.DataFrame:
            raise TypeError("edges should be a pandas DataFrame")

        for col in [tail, head, trav_time, freq]:
            if col not in edges:
                raise KeyError(f"edge column '{col}' not found in graph edges dataframe")

        if edges[[tail, head, trav_time, freq]].isna().any().any():
            raise ValueError(
                " ".join(
                    [
                        f"edges[[{tail}, {head}, {trav_time}, {freq}]] ",
                        "should not have any missing value",
                    ]
                )
            )

        for col in [tail, head]:
            if not pd.api.types.is_integer_dtype(edges[col].dtype):
                raise TypeError(f"column '{col}' should be of integer type")

        for col in [trav_time, freq]:
            if not pd.api.types.is_numeric_dtype(edges[col].dtype):
                raise TypeError(f"column '{col}' should be of numeric type")

            if edges[col].min() < 0.0:
                raise ValueError(f"column '{col}' should be nonnegative")

    def assign(
        self,
        demand,
        origin_column="orig_vert_idx",
        destination_column="dest_vert_idx",
        demand_column="demand",
        check_demand=False,
        threads=1
    ):
        # check the input demand paramater
        if check_demand:
            self._check_demand(demand, origin_column, destination_column, demand_column)
        demand = demand[demand[demand_column] > 0]

        # initialize the column storing the resulting edge volumes
        self._edges["volume"] = 0.0

        # travel time is computed but not saved into an array in the following
        self.u_i_vec = None  # TODO: not sure what this supposed to be and if its somethign we want as an out

        o_vert_ids = demand[origin_column].values.astype(np.uint32)
        d_vert_ids = demand[destination_column].values.astype(np.uint32)
        demand_vls = demand[demand_column].values.astype(np.float64)

        # get the list of all destinations
        destination_vertex_indices = np.unique(d_vert_ids)

        edge_volume = np.empty(self._edges["volume"].shape[0], dtype=np.float64)

        cdef: # All will be thread local, allocated in parallel block
            int num_threads = <int> (openmp.omp_get_num_threads() if threads < 1 else threads)
            cnp.uint32_t *thread_demand_origins
            cnp.float64_t *thread_demand_values
            cnp.float64_t *thread_edge_volume
            size_t demand_size

            cnp.float64_t *thread_u_i_vec
            cnp.float64_t *thread_f_i_vec
            cnp.float64_t *thread_u_j_c_a_vec
            cnp.float64_t *thread_v_i_vec
            cnp.uint8_t *thread_h_a_vec
            cnp.uint32_t *thread_edge_indices

            # Views of required READ-ONLY data
            cnp.uint32_t[::1] indptr_view = self._indptr[:]
            cnp.uint32_t[::1] edge_idx_view = self._edge_idx[:]
            cnp.float64_t[::1] trav_time_view = self._trav_time[:]
            cnp.float64_t[::1] freq_view = self._freq[:]
            cnp.uint32_t[::1] tail_view = self._tail[:]
            cnp.uint32_t[::1] head_view = self._head[:]
            cnp.uint32_t[:] d_vert_ids_view = d_vert_ids[:]
            cnp.uint32_t[:] destination_vertex_indices_view = destination_vertex_indices[:]
            cnp.uint32_t[::1] o_vert_ids_view = o_vert_ids[:]
            cnp.float64_t[::1] demand_vls_view = demand_vls[:]

            size_t i, k, destination_vertex_index
            size_t vertex_count = self.vertex_count
            size_t edge_count = self._edges["volume"].shape[0]

        with nogil, parallel(num_threads=num_threads):
            # Allocate thread local scratch space
            thread_demand_origins = <cnp.uint32_t *>  malloc(sizeof(cnp.uint32_t)  * d_vert_ids_view.shape[0])
            thread_demand_values  = <cnp.float64_t *> malloc(sizeof(cnp.float64_t) * d_vert_ids_view.shape[0])
            thread_edge_volume    = <cnp.float64_t *> malloc(sizeof(cnp.float64_t) * edge_count)

            thread_u_i_vec      = <cnp.float64_t *> malloc(sizeof(cnp.float64_t) * vertex_count)
            thread_f_i_vec      = <cnp.float64_t *> malloc(sizeof(cnp.float64_t) * vertex_count)
            thread_u_j_c_a_vec  = <cnp.float64_t *> malloc(sizeof(cnp.float64_t) * edge_count)
            thread_v_i_vec      = <cnp.float64_t *> malloc(sizeof(cnp.float64_t) * vertex_count)
            thread_h_a_vec      = <cnp.uint8_t *>   malloc(sizeof(cnp.uint8_t)   * edge_count)
            thread_edge_indices = <cnp.uint32_t *>  malloc(sizeof(cnp.uint32_t)  * edge_count)

            for i in prange(destination_vertex_indices_view.shape[0]):
                destination_vertex_index = destination_vertex_indices_view[i]

                demand_size = 0
                for k in range(d_vert_ids_view.shape[0]):
                    if d_vert_ids_view[k] == destination_vertex_index:
                        thread_demand_origins[demand_size] = o_vert_ids_view[k]
                        thread_demand_values[demand_size] = demand_vls_view[k]
                        demand_size = demand_size + 1  # demand_size += 1 is not allowed as cython believes this is a reduction

                # S&F
                compute_SF_in(
                    indptr_view,
                    edge_idx_view,
                    trav_time_view,
                    freq_view,
                    tail_view,
                    head_view,
                    thread_demand_origins,
                    thread_demand_values,
                    demand_size,
                    thread_edge_volume,
                    thread_u_i_vec,
                    thread_f_i_vec,
                    thread_u_j_c_a_vec,
                    thread_v_i_vec,
                    thread_h_a_vec,
                    thread_edge_indices,
                    vertex_count,
                    destination_vertex_index
                )

                with gil:
                    self._edges["volume"] += np.asarray(<cnp.float64_t[:edge_count]>thread_edge_volume)

            free(thread_demand_origins)
            free(thread_demand_values)
            free(thread_edge_volume)
            free(thread_u_i_vec)
            free(thread_f_i_vec)
            free(thread_u_j_c_a_vec)
            free(thread_v_i_vec)
            free(thread_h_a_vec)
            free(thread_edge_indices)

    def _check_demand(self, demand, origin_column, destination_column, demand_column):
        if type(demand) != pd.core.frame.DataFrame:
            raise TypeError("demand should be a pandas DataFrame")

        for col in [origin_column, destination_column, demand_column]:
            if col not in demand:
                raise KeyError(f"demand column '{col}' not found in demand dataframe")

        if demand[[origin_column, destination_column, demand_column]].isna().any().any():
            raise ValueError(
                " ".join(
                    [
                        f"demand[[{origin_column}, {destination_column}, {demand_column}]] ",
                        "should not have any missing value",
                    ]
                )
            )

        for col in [origin_column, destination_column]:
            if not pd.api.types.is_integer_dtype(demand[col].dtype):
                raise TypeError(f"column '{col}' should be of integer type")

        col = demand_column

        if not pd.api.types.is_numeric_dtype(demand[col].dtype):
            raise TypeError(f"column '{col}' should be of numeric type")

        if demand[col].min() < 0.0:
            raise ValueError(f"column '{col}' should be nonnegative")
