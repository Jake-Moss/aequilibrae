from copy import deepcopy

import geopandas as gpd
import shapely.wkb

from aequilibrae.project.basic_table import BasicTable
from aequilibrae.project.data_loader import DataLoader
from aequilibrae.project.network.link import Link
from aequilibrae.project.table_loader import TableLoader
from aequilibrae.utils.db_utils import commit_and_close
from aequilibrae.utils.spatialite_utils import connect_spatialite


class Links(BasicTable):
    """
    Access to the API resources to manipulate the links table in the network

    .. code-block:: python

        >>> project = create_example(project_path)

        >>> all_links = project.network.links

        # We can just get one link in specific
        >>> link = all_links.get(1)

        # We can save changes for all links we have edited so far
        >>> all_links.save()
    """

    __max_id = -1

    #: Query sql for retrieving links
    sql = ""

    def __init__(self, net):
        super().__init__(net.project)
        self.__table_type__ = "links"
        self.__fields = []
        self.__items = {}
        self.__data = None

        if self.sql == "":
            self.refresh_fields()

    def get(self, link_id: int) -> Link:
        """Get a link from the network by its *link_id*

        It raises an error if link_id does not exist

        :Arguments:
            **link_id** (:obj:`int`): Id of a link to retrieve

        :Returns:
            **link** (:obj:`Link`): Link object for requested link_id
        """
        link_id = int(link_id)
        if link_id in self.__items:
            link = self.__items[link_id]
            if not link._exists():
                raise Exception("Link was deleted")
            return link
        data = self.__link_data(link_id)
        if data:
            return self.__create_return_link(data)
        self.__existence_error(link_id)

    def new(self) -> Link:
        """Creates a new link

        :Returns:
            **link** (:obj:`Link`): A new link object populated only with link_id (not saved in the model yet)
        """

        data = {key: None for key in self.__fields}
        data["a_node"] = 0
        data["b_node"] = 0
        data["direction"] = 0
        data["link_type"] = "default"
        data["link_id"] = self.__new_link_id()
        return Link(data, self.project)
        # return self.__create_return_link(data)

    def copy_link(self, link_id: int) -> Link:
        """Creates a copy of a link with a new id

        It raises an error if link_id does not exist

        :Arguments:
            **link_id** (:obj:`int`): Id of the link to copy

        :Returns:
            **link** (:obj:`Link`): Link object for requested link_id
        """

        data = self.__link_data(int(link_id))
        data["link_id"] = self.__new_link_id()

        # The geometry wrangling is just a workaround to signalize that the link is new
        # That allows saving of the link to work properly
        geo = data["geometry"]
        data["geometry"] = None
        link = self.__create_return_link(data)
        link.geometry = shapely.wkb.loads(geo)

        return link

    def delete(self, link_id: int) -> None:
        """Removes the link with link_id from the project

        :Arguments:
            **link_id** (:obj:`int`): Id of a link to delete
        """
        d = 1
        link_id = int(link_id)
        if link_id in self.__items:
            link = self.__items.pop(link_id)  # type: Link
            link.delete()
        else:
            with commit_and_close(connect_spatialite(self.project.path_to_file)) as conn:
                d = conn.execute("Delete from Links where link_id=?", [link_id]).rowcount
        if d:
            self.project.logger.warning(f"Link {link_id} was successfully removed from the project database")
        else:
            self.__existence_error(link_id)

    def refresh_fields(self) -> None:
        """After adding a field one needs to refresh all the fields recognized by the software"""
        tl = TableLoader()
        with commit_and_close(connect_spatialite(self.project.path_to_file)) as conn:
            self.__max_id = conn.execute("select coalesce(max(link_id),0) from Links").fetchone()[0]
            tl.load_structure(conn, "links")
        self.sql = tl.sql
        self.__fields = deepcopy(tl.fields)

    @property
    def data(self) -> gpd.GeoDataFrame:
        """Returns all links data as a Pandas DataFrame

        :Returns:
            **table** (:obj:`GeoDataFrame`): GeoPandas GeoDataFrame with all the nodes
        """
        dl = DataLoader(self.project.path_to_file, "links")
        return dl.load_table()

    def refresh(self):
        """Refreshes all the links in memory"""
        lst = list(self.__items.keys())
        for k in lst:
            del self.__items[k]

    def save(self):
        for item in self.__items.values():
            item.save()

    def __del__(self):
        self.__items.clear()

    def __existence_error(self, link_id):
        raise ValueError(f"Link {link_id} does not exist in the model")

    def __link_data(self, link_id: int) -> dict:
        with commit_and_close(connect_spatialite(self.project.path_to_file)) as conn:
            data = conn.execute(f"{self.sql} where link_id=?", [link_id]).fetchone()
        if data:
            return dict(zip(self.__fields, data))
        raise ValueError("Link_id does not exist on the network")

    def __new_link_id(self):
        self.__max_id += 1
        return self.__max_id

    def __create_return_link(self, data):
        link = Link(data, self.project)
        self.__items[link.link_id] = link
        return link
