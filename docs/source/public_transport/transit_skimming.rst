Transit skimming
================

Transit skimming in AequilibraE is incredibly flexible, but it requires a good understanding of the
structure of the :ref:`transit_graph`, so we recommend reading that section first.

As it is the case with traffic graphs, it is possible to perform skimming on any field of the transit
graph, so the effort consists basically of defining consistent fields for skimming.


.. code-block:: python

    # We can add fields to our graph
    >>> transit_graph.graph["boardings"] = transit_graph.graph["link_type"].apply(lambda x: 1 if x == "boarding" else 0)

    >>> transit_graph.graph["in_vehicle_trav_time"] = np.where(
            transit_graph.graph["link_type"].isin(["on-board", "dwell"]), 0, transit_graph.graph["trav_time"]
        )

    >>> transit_graph.graph["egress_trav_time"] = np.where(
            transit_graph.graph["link_type"] != "egress_connector", 0, transit_graph.graph["trav_time"]
        )


    >>> transit_graph.graph["access_trav_time"] = np.where(
            transit_graph.graph["link_type"] != "access_connector", 0, transit_graph.graph["trav_time"]
        )

    >>> skim_cols = ["trav_time", "boardings", "in_vehicle_trav_time", "egress_trav_time", "access_trav_time"]

More sophisticated skimming is also possible, such as skimming related to specific routes and/or modes. In this case,
the logit persists, and it is necessary to define fields that represent the desired skimming metrics.  One example is
skimming travel time in rail only.

.. code-block:: python

    >>> transit_graph.graph["rail_trav_time"] = transit_graph.graph.trav_time
    >>> all_routes = transit.get_table("routes")
    >>> rail_ids = all_routes.query("route_type in [1, 2]").route_id.to_numpy()
    # Assign zero travel time to all non-rail links
    >>> transit_graph.graph.loc[~transit_graph.graph.line_id.isin(rail_ids),"rail_trav_time"] =0
    >>> transit_graph.graph["in_vehicle_trav_time"] = np.where(
            transit_graph.graph["link_type"].isin(["on-board", "dwell"]), 0, transit_graph.graph["trav_time"]
        )