import numpy as np
import pandas as pd
from aequilibrae.paths.public_transport import HyperpathGenerating
from numba import jit

RS = 124  # random seed


def create_vertices(n):
    x = np.linspace(0, 1, n)
    y = np.linspace(0, 1, n)
    xv, yv = np.meshgrid(x, y, indexing="xy")
    vertices = pd.DataFrame()
    vertices["x"] = xv.ravel()
    vertices["y"] = yv.ravel()
    return vertices


n = 10
vertices = create_vertices(n)


@jit
def create_edges_numba(n):
    m = 2 * n * (n - 1)
    tail = np.zeros(m, dtype=np.uint32)
    head = np.zeros(m, dtype=np.uint32)
    k = 0
    for i in range(n - 1):
        for j in range(n):
            tail[k] = i + j * n
            head[k] = i + 1 + j * n
            k += 1
            tail[k] = j + i * n
            head[k] = j + (i + 1) * n
            k += 1
    return tail, head


def create_edges(n, seed=124):
    tail, head = create_edges_numba(n)
    edges = pd.DataFrame()
    edges["tail"] = tail
    edges["head"] = head
    m = len(edges)
    rng = np.random.default_rng(seed=seed)
    edges["trav_time"] = rng.uniform(0.0, 1.0, m)
    edges["delay_base"] = rng.uniform(0.0, 1.0, m)
    edges['var_1_c'] = rng.uniform(0.0, 1.0, m)
    return edges


edges = create_edges(n, seed=RS)

alpha = 10.0

delay_base = edges.delay_base.values
indices = np.where(delay_base == 0.0)
delay_base[indices] = 1.0  # use this to prevent an error?
freq_base = 1.0 / delay_base
freq_base[indices] = np.inf
edges["freq_base"] = freq_base

if alpha == 0.0:
    edges["freq"] = np.inf
else:
    edges["freq"] = edges.freq_base / alpha

sf = HyperpathGenerating(
    edges, tail="tail", head="head", trav_time="trav_time", freq="freq"
)

dest = n * n - 1
sf.run(origin=0, destination=dest, volume=1.0)

edges_df = sf._edges

for i in range(sf._indptr[dest], sf._indptr[dest + 1]):
    print(i)
    edge_idx = sf._edge_idx[i]
    print(edge_idx)
    print(sf._edges.trav_time[edge_idx])
edges_df[edges_df['head'] == 99]
