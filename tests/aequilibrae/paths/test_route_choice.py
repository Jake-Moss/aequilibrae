import os
import uuid
import zipfile
from os.path import join, dirname
from tempfile import gettempdir
from unittest import TestCase
import pandas as pd
import numpy as np

from aequilibrae import Graph, Project
from aequilibrae.paths.route_choice import RouteChoice

import time

# from ...data import siouxfalls_project


class TestRouteChoice(TestCase):
    def setUp(self) -> None:
        # os.environ["PATH"] = os.path.join(gettempdir(), "temp_data") + ";" + os.environ["PATH"]

        # proj_path = os.path.join(gettempdir(), "test_route_choice" + uuid.uuid4().hex)
        # os.mkdir(proj_path)
        # zipfile.ZipFile(join(dirname(siouxfalls_project), "sioux_falls_single_class.zip")).extractall(proj_path)

        proj_path = "/home/jake/Software/aequilibrae_performance_tests/models/Arkansas/"
        self.project = Project()
        self.project.open(proj_path)
        self.project.network.build_graphs(fields=["distance"], modes=["c"])
        self.graph = self.project.network.graphs["c"]  # type: Graph
        self.graph.set_graph("distance")
        self.graph.set_blocked_centroid_flows(False)

    def tearDown(self) -> None:
        self.project.close()

    # def test_route_choice(self):
    #     rc = RouteChoice(self.graph)

    #     results = rc.run(220591, 352, max_routes=1000, max_depth=0)
    #     # print(*results, sep="\n")
    #     print(len(results), len(set(results)))
    #     self.assertEqual(len(results), len(set(results)))

    #     import shapely

    #     links = self.project.network.links.data.set_index("link_id")
    #     df = []
    #     for route in results:
    #         df.append(
    #             (
    #                 route,
    #                 shapely.MultiLineString(
    #                     links.loc[
    #                         self.graph.graph[self.graph.graph.__compressed_id__.isin(route)].link_id
    #                     ].geometry.to_list()
    #                 ).wkt,
    #             )
    #         )

    #     df = pd.DataFrame(df, columns=["route", "geometry"])
    #     df.to_csv("test1.csv")

    #     # breakpoint()

    def test_route_choice_batched(self):
        rc = RouteChoice(self.graph)

        # breakpoint()
        # results =
        np.random.seed(0)
        n = 1000
        cores = 4

        nodes = [tuple(x) for x in np.random.choice(self.graph.centroids, size=(n, 2), replace=False)]

        t = time.time()
        results = rc.batched(nodes, max_routes=20, max_depth=0, cores=cores)
        end = time.time() - t
        print("Time:", end, "per:", end / n)

        # breakpoint()

        for od, route_set in results.items():
            self.assertEqual(len(route_set), len(set(route_set)))

        # import geopandas as gpd
        # import shapely

        # links = self.project.network.links.data.set_index("link_id")
        # df = []
        # for od, route_set in results.items():
        #     for route in route_set:
        #         df.append(
        #             (
        #                 *od,
        #                 shapely.MultiLineString(
        #                     links.loc[
        #                         self.graph.graph[self.graph.graph.__compressed_id__.isin(route)].link_id
        #                     ].geometry.to_list()
        #                 ),
        #             )
        #         )

        # df = gpd.GeoDataFrame(df, columns=["origin", "destination", "geometry"])
        # df.set_geometry("geometry")
        # df.to_file("test1.gpkg", layer='routes', driver="GPKG")

        # breakpoint()
        assert False

if __name__ == "__main__":
    t = TestRouteChoice()
    t.setUp()
    t.test_route_choice()
