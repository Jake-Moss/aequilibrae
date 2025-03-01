import os
import uuid
import zipfile
from os.path import join, dirname
from pathlib import Path
from tempfile import gettempdir
from unittest import TestCase
import pandas as pd
import numpy as np
import sqlite3

from aequilibrae import TrafficAssignment, TrafficClass, Graph, Project, PathResults
from aequilibrae.matrix import AequilibraeMatrix
from ...data import siouxfalls_project


class TestSelectLink(TestCase):
    def setUp(self) -> None:
        os.environ["PATH"] = os.path.join(gettempdir(), "temp_data") + ";" + os.environ["PATH"]

        proj_path = os.path.join(gettempdir(), "test_traffic_assignment_path_files" + uuid.uuid4().hex)
        os.mkdir(proj_path)
        zipfile.ZipFile(join(dirname(siouxfalls_project), "sioux_falls_single_class.zip")).extractall(proj_path)
        self.project = Project()
        self.project.open(proj_path)
        self.project.network.build_graphs()
        self.car_graph = self.project.network.graphs["c"]  # type: Graph
        self.car_graph.set_graph("free_flow_time")
        self.car_graph.set_blocked_centroid_flows(False)
        self.matrix = self.project.matrices.get_matrix("demand_omx")
        self.matrix.computational_view()

        self.assignment = TrafficAssignment()
        self.assignclass = TrafficClass("car", self.car_graph, self.matrix)
        self.assignment.set_classes([self.assignclass])
        self.assignment.set_vdf("BPR")
        self.assignment.set_vdf_parameters({"alpha": 0.15, "beta": 4.0})
        self.assignment.set_vdf_parameters({"alpha": "b", "beta": "power"})
        self.assignment.set_capacity_field("capacity")
        self.assignment.set_time_field("free_flow_time")
        self.assignment.max_iter = 1
        self.assignment.set_algorithm("msa")

    def tearDown(self) -> None:
        self.matrix.close()
        self.project.close()

    def test_multiple_link_sets(self):
        """
        Tests whether the Select Link feature works as wanted.
        Uses two examples: 2 links in one select link, and a single Selected Link
        Checks both the OD Matrix and Link Loading
        """
        self.assignclass.set_select_links({"sl_9_or_6": [(9, 1), (6, 1)], "just_3": [(3, 1)], "sl_5_for_fun": [(5, 1)]})
        self.assignment.execute()
        for key in self.assignclass._selected_links.keys():
            od_mask, link_loading = create_od_mask(
                self.assignclass.matrix.matrix_view, self.assignclass.graph, self.assignclass._selected_links[key]
            )
            np.testing.assert_allclose(
                self.assignclass.results.select_link_od.matrix[key][:, :, 0],
                od_mask,
                err_msg="OD SL matrix for: " + str(key) + " does not match",
            )
            np.testing.assert_allclose(
                self.assignclass.results.select_link_loading[key],
                link_loading,
                err_msg="Link loading SL matrix for: " + str(key) + " does not match",
            )

        # Test if files are saved in the right place
        self.assignment.save_select_link_results("select_link_analysis")

        matrices = self.project.matrices
        matrices.update_database()
        assert "select_link_analysis.omx" in matrices.list()["file_name"].tolist()

        # Test if matrices are with the correct shape and are not empty
        sla = matrices.get_matrix("select_link_analysis_omx")
        num_zones = self.assignment.classes[0].graph.num_zones
        for mat in sla.names:
            m = sla.get_matrix(mat)
            assert m.sum() > 0 and m.shape == (num_zones, num_zones)

        pth = Path(self.project.project_base_path)
        conn = sqlite3.connect(pth / "results_database.sqlite")
        results = [x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type ='table'").fetchall()]
        assert "select_link_analysis" in results

    def test_equals_demand_one_origin(self):
        """
        Test to ensure the Select Link functionality behaves as required.
        Tests to make sure the OD matrix works when all links surrounding one origin are selected
        Confirms the Link Loading is done correctly in this case
        """
        self.assignclass.set_select_links({"sl_1_4_3_and_2": [(1, 1), (4, 1), (3, 1), (2, 1)]})

        self.assignment.execute()

        for key in self.assignclass._selected_links.keys():
            od_mask, link_loading = create_od_mask(
                self.assignclass.matrix.matrix_view, self.assignclass.graph, self.assignclass._selected_links[key]
            )
            np.testing.assert_allclose(
                self.assignclass.results.select_link_od.matrix[key][:, :, 0],
                od_mask,
                err_msg="OD SL matrix for: " + str(key) + " does not match",
            )
            np.testing.assert_allclose(
                self.assignclass.results.select_link_loading[key],
                link_loading,
                err_msg="Link loading SL matrix for: " + str(key) + " does not match",
            )

    def test_single_demand(self):
        """
        Tests the functionality of Select Link when given a custom demand matrix, where only 1 OD pair has demand on it
        Confirms the OD matrix behaves, and the Link Loading is just on the path of this OD pair
        """
        custom_demand = np.zeros((24, 24, 1)).astype(float)
        custom_demand[0, 23, 0] = 1000
        self.matrix.matrix_view = custom_demand
        self.assignclass.matrix = self.matrix

        self.assignclass.set_select_links({"sl_39_66_or_73": [(39, 1), (66, 1), (73, 1)]})

        self.assignment.execute()
        for key in self.assignclass._selected_links.keys():
            od_mask, link_loading = create_od_mask(
                self.assignclass.matrix.matrix_view, self.assignclass.graph, self.assignclass._selected_links[key]
            )
            np.testing.assert_allclose(
                self.assignclass.results.select_link_od.matrix[key][:, :, 0],
                od_mask,
                err_msg="OD SL matrix for: " + str(key) + " does not match",
            )
            np.testing.assert_allclose(
                self.assignclass.results.select_link_loading[key],
                link_loading,
                err_msg="Link loading SL matrix for: " + str(key) + " does not match",
            )

    def test_select_link_network_loading(self):
        """
        Test to ensure the SL_network_loading method correctly does the network loading
        """
        self.assignment.execute()
        non_sl_loads = self.assignclass.results.get_load_results()
        self.setUp()
        self.assignclass.set_select_links({"sl_39_66_or_73": [(39, 1), (66, 1), (73, 1)]})
        self.assignment.execute()
        sl_loads = self.assignclass.results.get_load_results()
        np.testing.assert_allclose(non_sl_loads.matrix_tot, sl_loads.matrix_tot)

    def test_duplicate_links(self):
        """
        Tests to make sure the user api correctly filters out duplicate links in the compressed graph
        """
        self.assignment = TrafficAssignment()
        self.assignclass = TrafficClass("car", self.car_graph, self.matrix)
        with self.assertWarns(Warning):
            self.assignclass.set_select_links({"test": [(1, 1), (1, 1)]})
        self.assertEqual(len(self.assignclass._selected_links["test"]), 1, "Did not correctly remove duplicate link")

    def test_link_out_of_bounds(self):
        """
        Test to confirm the user api correctly identifies when an input node is invalid for the current graph
        """
        self.assignment = TrafficAssignment()
        self.assignclass = TrafficClass("car", self.car_graph, self.matrix)
        self.assertRaises(ValueError, self.assignclass.set_select_links, {"test": [(78, 1), (1, 1)]})

    def test_kaitang(self):
        proj_path = os.path.join(gettempdir(), "test_traffic_assignment_path_files" + uuid.uuid4().hex)
        os.mkdir(proj_path)
        zipfile.ZipFile(join(dirname(siouxfalls_project), "KaiTang.zip")).extractall(proj_path)

        link_df = pd.read_csv(os.path.join(proj_path, "link.csv"))
        centroids_array = np.array([7, 8, 11])

        net = link_df.copy()

        g = Graph()
        g.network = net
        g.network_ok = True
        g.status = "OK"
        g.mode = "a"
        g.prepare_graph(centroids_array)
        g.set_blocked_centroid_flows(False)
        g.set_graph("fft")

        aem_mat = AequilibraeMatrix()
        aem_mat.load(os.path.join(proj_path, "demand_a.aem"))
        aem_mat.computational_view(["a"])

        assign_class = TrafficClass("class_a", g, aem_mat)
        assign_class.set_fixed_cost("a_toll")
        assign_class.set_vot(1.1)
        assign_class.set_select_links(links={"trace": [(7, 0), (13, 0)]})

        assign = TrafficAssignment()
        assign.set_classes([assign_class])
        assign.set_vdf("BPR")
        assign.set_vdf_parameters({"alpha": "alpha", "beta": "beta"})
        assign.set_capacity_field("capacity")
        assign.set_time_field("fft")
        assign.set_algorithm("bfw")
        assign.max_iter = 100
        assign.rgap_target = 0.0001

        # 4.execute
        assign.execute()

        # 5.receive results
        assign_flow_res_df = assign.results().sort_index().reset_index(drop=False).astype(float).fillna(0.0)
        select_link_flow_df = assign.select_link_flows().sort_index().reset_index(drop=False).astype(float).fillna(0.0)

        pd.testing.assert_frame_equal(
            assign_flow_res_df[["link_id", "a_ab", "a_ba", "a_tot"]],
            select_link_flow_df.rename(
                columns={"class_a_trace_a_ab": "a_ab", "class_a_trace_a_ba": "a_ba", "class_a_trace_a_tot": "a_tot"}
            ),
        )

    def test_multi_iteration(self):
        for algorithm in ["all-or-nothing", "msa", "fw", "cfw", "bfw"]:
            with self.subTest(algorithm=algorithm):
                assignment = TrafficAssignment()
                assignclass = TrafficClass("car", self.car_graph, self.matrix)
                assignment.set_classes([assignclass])
                assignment.set_vdf("BPR")
                assignment.set_vdf_parameters({"alpha": 0.15, "beta": 4.0})
                assignment.set_vdf_parameters({"alpha": "b", "beta": "power"})
                assignment.set_capacity_field("capacity")
                assignment.set_time_field("free_flow_time")
                assignment.max_iter = 10
                assignment.set_algorithm(algorithm)

                assignclass.set_select_links({"sl_1_1": [(1, 1)], "sl_5_1": [(5, 1)]})
                assignment.execute()

                assignment_results = assignclass.results.get_load_results()
                sl_results = assignclass.results.get_sl_results()

                self.assertAlmostEqual(
                    assignment_results["matrix_ab"].loc[1],
                    sl_results["sl_1_1_matrix_ab"].loc[1],
                    msg=f"Select link results differ to that of the assignment ({algorithm})",
                    delta=1e-6,
                )
                self.assertAlmostEqual(
                    assignment_results["matrix_ab"].loc[5],
                    sl_results["sl_5_1_matrix_ab"].loc[5],
                    msg=f"Select link results differ to that of the assignment ({algorithm})",
                    delta=1e-6,
                )


def create_od_mask(demand: np.array, graph: Graph, sl):
    res = PathResults()
    # This uses the UNCOMPRESSED graph, since we don't know which nodes the user may ask for
    graph.set_graph("free_flow_time")
    res.prepare(graph)

    def g(o, d):
        res.compute_path(o, d)
        return list(res.path_nodes) if (res.path_nodes is not None and o != d) else []

    a = [[g(o, d) for d in range(1, 25)] for o in range(1, 25)]
    sl_links = []
    for i in range(len(sl)):
        node_pair = graph.graph.iloc[sl[i]]["a_node"] + 1, graph.graph.iloc[sl[i]]["b_node"] + 1
        sl_links.append(node_pair)
    mask = {}
    for origin, val in enumerate(a):
        for dest, path in enumerate(val):
            for k in range(1, len(path)):
                if origin == dest:
                    pass
                elif (path[k - 1], path[k]) in sl_links:
                    mask[(origin, dest)] = True
    sl_od = np.zeros((24, 24))
    for origin in range(24):
        for dest in range(24):
            if mask.get((origin, dest)):
                sl_od[origin, dest] = demand[origin, dest][0]

    # make link loading
    loading = np.zeros((76, 1))
    for orig, dest in mask.keys():
        path = a[orig][dest]
        for i in range(len(path) - 1):
            link = (
                graph.graph[(graph.graph["a_node"] == path[i] - 1) & (graph.graph["b_node"] == path[i + 1] - 1)][
                    "link_id"
                ].values[0]
                - 1
            )
            loading[link] += demand[orig, dest]
    return sl_od, loading
