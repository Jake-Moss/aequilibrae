from aequilibrae.matrix import AequilibraeMatrix
from aequilibrae.paths.traffic_assignment import TrafficAssignment
import numpy as np


class ODME:
    def __init__(self, obversed_vols, assig: TrafficAssignment):
        """
        Demo.

        .. code-block:: python

            >>> from aequilibrae.paths.odme import ODME
            >>> odme = ODEM(observered, assig)
            >>> odme.execute()
        """
        self.obversed_vols = obversed_vols
        self.assig = assig


    def execute(self):
        pass
