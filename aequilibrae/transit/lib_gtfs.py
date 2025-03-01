from contextlib import closing
from copy import deepcopy

import geopandas as gpd
import pandas as pd
import pyproj
from pyproj import Transformer
from shapely.geometry import Point, MultiLineString

from aequilibrae.context import get_active_project, get_logger
from aequilibrae.project.database_connection import database_connection
from aequilibrae.transit.constants import Constants, PATTERN_ID_MULTIPLIER
from aequilibrae.transit.functions.get_srid import get_srid
from aequilibrae.transit.transit_elements import Link, Pattern, mode_correspondence
from aequilibrae.utils.aeq_signal import SIGNAL, simple_progress
from aequilibrae.utils.interface.worker_thread import WorkerThread
from .gtfs_loader import GTFSReader
from .map_matching_graph import MMGraph


class GTFSRouteSystemBuilder(WorkerThread):
    signal = SIGNAL(object)

    """Container for GTFS feeds providing data retrieval for the importer"""

    def __init__(
        self, network, agency_identifier, file_path, day="", description="", capacities=None, pces=None
    ):  # noqa: B006
        """Instantiates a transit class for the network

        :Arguments:

            **local network** (:obj:`Network`): Supply model to which this GTFS will be imported

            **agency_identifier** (:obj:`str`): ID for the agency this feed refers to (e.g. 'CTA')

            **file_path** (:obj:`str`): Full path to the GTFS feed (e.g. 'D:/project/my_gtfs_feed.zip')

            **day** (:obj:`str`, *Optional*): Service data contained in this field to be imported (e.g. '2019-10-04')

            **description** (:obj:`str`, *Optional*): Description for this feed (e.g. 'CTA19 fixed by John after coffee')
        """
        WorkerThread.__init__(self, None)

        self.__network = network
        self.project = get_active_project(False)
        self.archive_dir = None  # type: str
        self.day = day
        self.logger = get_logger()
        self.gtfs_data = GTFSReader()

        self.srid = get_srid()
        self.transformer = None
        self.wgs84 = pyproj.Proj("epsg:4326")
        self.trip_by_service = {}
        self.patterns = {}
        self.graphs = {}
        self.transformer = Transformer.from_crs("epsg:4326", f"epsg:{self.srid}", always_xy=False)
        self.sridproj = pyproj.Proj(f"epsg:{self.srid}")
        self.gtfs_data.agency.agency = agency_identifier
        self.gtfs_data.agency.description = description
        self.__default_capacities = {} if capacities is None else capacities
        self.__default_pces = {} if pces is None else pces
        self.__do_execute_map_matching = False
        self.__target_date__ = None
        self.__outside_zones = 0
        self.__has_taz = len(self.project.zoning.all_zones()) > 0

        if file_path is not None:
            self.logger.info(f"Creating GTFS feed object for {file_path}")
            self.gtfs_data.set_feed_path(file_path)
            self.gtfs_data._set_capacities(self.__default_capacities)
            self.gtfs_data._set_pces(self.__default_pces)

        self.select_routes = {}
        self.select_trips = []
        self.select_stops = {}
        self.select_patterns = {}
        self.select_links = {}

        links = self.project.network.links.data
        self.geo_links = gpd.GeoDataFrame(links, geometry=links.geometry, crs="EPSG:4326")

    def set_capacities(self, capacities: dict):
        """Sets default capacities for modes/vehicles.

        :Arguments:
            **capacities** (:obj:`dict`): Dictionary with GTFS types as keys, each with a list
            of 3 items for values for capacities: seated and total i.e. -> "{0: [150, 300],...}"
        """
        self.gtfs_data._set_capacities(capacities)

    def set_pces(self, pces: dict):
        """Sets default passenger car equivalent (PCE) factor for each GTFS mode.

        :Arguments:
            **pces** (:obj:`dict`): Dictionary with GTFS types as keys and the corresponding PCE
            value i.e. -> "{0: 2.0,...}"
        """
        self.gtfs_data._set_pces(pces)

    def set_maximum_speeds(self, max_speeds: pd.DataFrame):
        """Sets the maximum speeds to be enforced at segments.

        :Arguments:
            **max_speeds** (:obj:`pd.DataFrame`): Requires 4 fields: mode, min_distance, max_distance, speed.
            Modes not covered in the data will not be touched and distance brackets not covered will receive
            the maximum speed, with a warning
        """
        dict_speeds = dict(max_speeds.groupby(["mode"]))
        self.gtfs_data._set_maximum_speeds(dict_speeds)

    def dates_available(self) -> list:
        """Returns a list of all dates available for this feed.

        :Returns:
            **feed dates** (:obj:`list`): list of all dates available for this feed
        """
        return deepcopy(self.gtfs_data.feed_dates)

    def set_allow_map_match(self, allow=True):
        """Changes behavior for finding transit-link shapes. Defaults to ``True``.

        :Arguments:
              **allow** (:obj:`bool` *Optional*): If ``True``, allows uses map-matching in search of precise
              transit_link shapes. If ``False``, sets transit_link shapes equal to straight lines between
              stops. In the presence of GTFS raw shapes it has no effect.
        """

        self.__do_execute_map_matching = allow

    def map_match(self, route_types=[3]) -> None:  # noqa: B006
        """Performs map-matching for all routes of one or more types.

        Defaults to map-matching Bus routes (type 3) only.

        For a reference of route types, see the inputs for
        `route_type here <https://gtfs.org/documentation/schedule/reference/#routestxt>`_.

        :Arguments:
            **route_types** (:obj:`List[int]` or :obj:`Tuple[int]`): Default is [3], for bus only
        """
        if not isinstance(route_types, list) and not isinstance(route_types, tuple):
            raise TypeError("Route_types must be list or tuple")

        if any(not isinstance(item, int) for item in route_types):
            raise TypeError("All route types must be integers")

        for pat in simple_progress(self.select_patterns.values(), self.signal, "Map-matching patterns"):
            if pat.route_type in route_types:
                pat.map_match()
                msg = pat.get_error("stop_from_pattern")
                if msg is not None:
                    self.logger.warning(msg)

    def set_agency_identifier(self, agency_id: str) -> None:
        """Adds agency ID to this GTFS for use on import.

        :Arguments:
            **agency_id** (:obj:`str`): ID for the agency this feed refers to (e.g. 'CTA')
        """
        self.gtfs_data.agency.agency = agency_id

    def set_feed(self, feed_path: str) -> None:
        """Sets GTFS feed source to be used.

        :Arguments:
            **file_path** (:obj:`str`): Full path to the GTFS feed (e.g. 'D:/project/my_gtfs_feed.zip')
        """
        self.gtfs_data.set_feed_path(feed_path)
        self.gtfs_data.agency.feed_date = self.gtfs_data.feed_date

    def set_description(self, description: str) -> None:
        """Adds description to be added to the imported layers metadata

        :Arguments:
            **description** (:obj:`str`): Description for this feed (e.g. 'CTA2019 fixed by John Doe after strong coffee')
        """
        self.description = description

    def set_date(self, service_date: str) -> None:
        """Sets the date for import without doing any of data processing, which is left for the importer"""
        self.__target_date__ = service_date

    def load_date(self, service_date: str) -> None:
        """Loads the transit services available for *service_date*

        :Arguments:
            **service_date** (:obj:`str`): Service data contained in this field to be imported (e.g. '2019-10-04')
        """
        if self.srid is None:
            raise ValueError("We cannot load data without an SRID")
        if service_date == self.day:
            return
        if service_date not in self.gtfs_data.feed_dates:
            raise ValueError("The date chosen is not available in this GTFS feed")
        self.day = service_date

        self.gtfs_data.load_data(service_date)

        self.logger.info("  Building data structures")

        self.__build_data()

        self.gtfs_data.agency.service_date = self.day

    def doWork(self):
        """Alias for execute_import"""
        self.execute_import()

    def execute_import(self):
        self.logger.debug("Starting execute_import")

        if self.__target_date__ is not None:
            self.load_date(self.__target_date__)
        if not self.select_routes:
            self.logger.warning(f"Nothing to import for {self.gtfs_data.agency.agency} on {self.day}")
            return

        self.logger.info(f"  Importing feed for agency {self.gtfs_data.agency.agency} on {self.day}")

        self.save_to_disk()

    def save_to_disk(self):
        """Saves all transit elements built in memory to disk"""

        with closing(database_connection("transit")) as conn:
            for pattern in simple_progress(self.select_patterns.values(), self.signal, "Saving patterns (Step: 10/12)"):
                pattern.save_to_database(conn, commit=False)
            conn.commit()

            self.gtfs_data.agency.save_to_database(conn)

            for trip in simple_progress(self.select_trips, self.signal, "Saving trips (Step: 11/12)"):
                trip.save_to_database(conn, commit=False)
            conn.commit()

            for link in simple_progress(self.select_links.values(), self.signal, "Saving links (Step: 11/12)"):
                link.save_to_database(conn, commit=False)
            conn.commit()

            zone_ids1 = [x.origin for x in self.gtfs_data.fare_rules]
            zone_ids2 = [x.destination for x in self.gtfs_data.fare_rules]
            zone_ids = sorted(set(zone_ids1) | set(zone_ids2))

            zones = [[x, self.gtfs_data.agency.agency_id] for x in zone_ids]
            if zones:
                sql = "Insert into fare_zones (transit_fare_zone, agency_id) values(?, ?);"
                conn.executemany(sql, zones)
            conn.commit()

            for fare in self.gtfs_data.fare_attributes.values():
                fare.save_to_database(conn)

            for fare_rule in self.gtfs_data.fare_rules:
                fare_rule.save_to_database(conn)

            for stop in simple_progress(self.select_stops.values(), self.signal, "Saving stops (Step: 12/12)"):
                if self.__has_taz:
                    closest_zone = self.project.zoning.get_closest_zone(stop.geo)
                    if stop.geo.within(self.project.zoning.get(closest_zone).geometry):
                        stop.taz = closest_zone
                stop.save_to_database(conn, commit=False)
            conn.commit()

        self.__outside_zones = None in [x.taz for x in self.select_stops.values()]
        if self.__outside_zones:
            msg = "    Some stops are outside the zoning system. Check the result on a map and see the log for info"
            self.logger.warning(msg)

    def __build_data(self):
        self.logger.debug("Starting __build_data")
        self.__get_routes_by_date()

        self.select_stops.clear()
        self.select_links.clear()
        self.select_patterns.clear()

        if self.__do_execute_map_matching:
            self.builds_link_graphs_with_broken_stops()

        msg = f"Loading data for {self.day} (Step: 9/12) - "
        c = Constants()
        for route_id, route in simple_progress(self.select_routes.items(), self.signal, msg):
            new_trips = self._get_trips_by_date_and_route(route_id, self.day)

            all_pats = [trip.pattern_hash for trip in new_trips]
            cntr = route.route_id + PATTERN_ID_MULTIPLIER
            for pat_hash in sorted(set(all_pats)):
                c.pattern_lookup[pat_hash] = cntr

                cntr += ((all_pats.count(pat_hash) // 100) + 1) * PATTERN_ID_MULTIPLIER

            self.select_trips.extend(new_trips)

            patterns = []
            for trip in new_trips:
                trip.pattern_id = c.pattern_lookup[trip.pattern_hash]
                trip.get_trip_id()
                self.logger.debug(f"Trip ID generated is {trip.trip_id} for pattern {trip.pattern_id}")
                if trip.pattern_id in self.select_patterns:
                    continue

                pat = self.__build_new_pattern(route, route_id, trip)
                patterns.append(pat)

            route.shape = self.__build_route_shape(patterns)
            route.pattern_id = trip.pattern_id

    def __build_new_pattern(self, route, route_id, trip) -> Pattern:
        self.logger.debug(f"New Pattern ID {trip.pattern_id} for route ID {route_id}")
        p = Pattern(route.route_id, self)
        p.pattern_id = trip.pattern_id
        p.route = trip.route
        p.route_type = int(route.route_type)
        p.raw_shape = trip.shape
        p._stop_based_shape = trip._stop_based_shape
        p.agency_id = route.agency_id
        p.shortname = route.route_short_name
        p.longname = route.route_long_name
        p.description = route.route_desc
        p.pce = route.pce
        p.seated_capacity = route.seated_capacity
        p.total_capacity = route.total_capacity
        for stop_id in self.gtfs_data.stop_times[trip.trip].stop_id.values:
            if stop_id not in self.select_stops:
                stp = self.gtfs_data.stops[stop_id]
                stp.route_type = route.route_type
                stp.stop = stp.stop
                self.select_stops[stop_id] = stp
            p.stops.append(self.gtfs_data.stops[stop_id])
        if self.__do_execute_map_matching:
            p.map_match()
        self.select_patterns[trip.pattern_id] = p

        p.links.clear()
        prev_end = self.gtfs_data.stops[trip.stops[0]].geo
        pgeo = p.best_shape()
        for i in range(1, len(trip.stops)):
            link = Link(self.srid)
            link.pattern_id = trip.pattern_id
            link.seq = i - 1
            link.get_link_id()
            fstop = trip.stops[i - 1]
            tstop = trip.stops[i]
            link.from_stop = fstop
            link.to_stop = tstop
            link.build_geo(self.gtfs_data.stops[fstop].geo, self.gtfs_data.stops[tstop].geo, pgeo, prev_end)
            prev_end = Point(list(link.geo.coords)[-1])
            link.type = int(route.route_type)
            p.links.append(link.transit_link)
            self.select_links[link.key] = link

        return p

    def __build_route_shape(self, patterns) -> MultiLineString:
        shapes = [p.best_shape() for p in patterns if p.best_shape() is not None]
        return MultiLineString(shapes)

    def __error_logging(self, titles, values):
        for i, j in zip(titles, values):
            self.logger.error(f"- {i}: {j}")

    def __warning_logging(self, titles, values):
        for i, j in zip(titles, values):
            self.logger.warning(f"- {i}: {j}")

    def __fail(self, msg: str) -> None:
        self.logger.error(msg)
        raise Exception(msg)

    def __get_routes_by_date(self):
        self.logger.debug("Starting __get_routes_by_date")

        routes = {}
        for route_id, route in self.gtfs_data.routes.items():
            if route_id not in self.gtfs_data.trips:
                continue
            for value in self.gtfs_data.trips[route_id].values():
                for trip in value:
                    service = trip.service_id
                    if service not in self.gtfs_data.services.keys():
                        continue
                    if self.day in self.gtfs_data.services[service].dates:
                        routes[route_id] = route
                        break
        if not routes:
            self.logger.warning("NO ROUTES OPERATING FOR THIS DATE")

        for route_id, route in routes.items():
            route.agency = self.gtfs_data.agency.agency

        self.select_routes = routes

    def _get_trips_by_date_and_route(self, route_id: int, service_date: str) -> list:
        trips = [
            trip
            for element in self.gtfs_data.trips[route_id]
            for trip in self.gtfs_data.trips[route_id][element]
            if service_date in self.gtfs_data.services[trip.service_id].dates
        ]
        return sorted(trips)

    def _get_stops_by_date(self, service_date: str) -> dict:
        self.load_date(service_date)
        return self.gtfs_data.stops

    def _get_shapes_by_date(self, service_date: str) -> dict:
        self.load_date(service_date)

        return self.gtfs_data.shapes

    def _get_fare_attributes_by_date(self, service_date: str) -> dict:
        self.load_date(service_date)
        return self.gtfs_data.fare_attributes

    def builds_link_graphs_with_broken_stops(self):
        """Build the graph for links for a certain mode while splitting the closest links at stops' projection

        :Arguments:
            **mode_id** (:obj:`int`): Mode ID for which we will build the graph for
        """

        route_types = list({r.route_type for r in self.select_routes.values()})
        route_types = [
            mode_id
            for mode_id in route_types
            if mode_id in mode_correspondence and mode_correspondence[mode_id] not in self.graphs
        ]
        if not route_types:
            return
        mm = MMGraph(self)
        for mode_id in route_types:
            mode = mode_correspondence[mode_id]
            graph = mm.build_graph_with_broken_stops(mode_id)
            if graph.num_links <= 0:
                continue
            self.graphs[mode] = graph
