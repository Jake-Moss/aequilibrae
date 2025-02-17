from aequilibrae.matrix import AequilibraeMatrix
from aequilibrae.project import Project

# from aequilibrae.paths import TrafficClass, TrafficAssignment
from multiprocessing.dummy import Pool as ThreadPool
from aequilibrae.paths.all_or_nothing import allOrNothing
from aequilibrae.paths.results import AssignmentResults
import faulthandler

faulthandler.enable()
pool = ThreadPool(10)
pool.close()
pool.join()

proj = Project()
proj.open("/mnt/d/release/Sample models/sioux_falls_2020_02_15/SiouxFalls.sqlite")
proj.network.build_graphs()

car_graph = proj.network.graphs["c"]
car_graph.set_graph("distance")
car_graph.set_blocked_centroid_flows(False)

mat = AequilibraeMatrix()
mat.load("/mnt/d/release/Sample models/sioux_falls_2020_02_15/demand.omx")
mat.computational_view(["matrix"])

res = AssignmentResults()
res.prepare(car_graph, mat)
print(res.num_skims)
q = allOrNothing("", mat, car_graph, res)
q.execute()
#
# assigclass = TrafficClass(car_graph, mat)
#
# assig = TrafficAssignment()
#
# assig.set_vdf("BPR")
# assig.set_classes(assigclass)
# assig.set_vdf_parameters({"alpha": "b", "beta": "power"})
# assig.set_capacity_field("capacity")
# assig.set_time_field("free_flow_time")
# assig.set_algorithm('bfw')
# assig.max_iter = 10
# #
# assig.execute()
#
# mat.close()
