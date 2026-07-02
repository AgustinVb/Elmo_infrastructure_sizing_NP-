from src.mine.elhd import ELHD
from src.mine.battery import Battery
from src.mine.layout import Layout
from src.mine.stations import Stations
from src.mine.chargers import Chargers
from src.mine.generators import Generators
from src.mine.storage import StorageSystems
from src.mine.degradation import BatteryDegradation


class Mine(object):
    """
    Clase que centraliza:
      • ELHD: datos de vehículos (hoja “LHD”)
      • Battery: datos de baterías (hoja “Batteries”)
      • Layout: datos de nodos de extracción (hoja “extraction_nodes”)
    """

    def __init__(self, model):
        """
        - model: diccionario de DataFrames, por ejemplo:
               {
                 "LHD": DataFrame(obtenido de Excel.parse("LHD")),
                 "Batteries": DataFrame(obtenido de Excel.parse("Batteries")),
                 "extraction_nodes": DataFrame(obtenido de Excel.parse("extraction_nodes")),
                 ...
               }
        """
        # 1) Vehículos LHD (solo la pestaña “LHD”)
        self.elhd = ELHD(model["LHD"], "name")

        # 2) Baterías (si las tienes en otra hoja llamada “Batteries”)
        #self.battery = Battery(model["Batteries"], "name")

        # 3) Layout de nodos
        self.layout = Layout(model["extraction_nodes"], "node_name")

        #4) Estaciones de carga (si las tienes en otra hoja llamada "Stations")
        self.stations = Stations(model["stations"], "station_name")

        # 5) Chargers: asumimos que la hoja 'chargers' siempre existe
        # y que tiene una columna 'id' que identifica la fila con valores
        self.chargers = Chargers(model['chargers'], 'id')

        # 6) Generators: opcional — solo si la hoja 'Generators' existe en el Excel
        if 'Generators' in model.container:
            self.generators = Generators(model['Generators'], 'name')
        else:
            self.generators = None

        # 7) Storage (BESS): opcional — solo si la hoja 'Storage' existe en el Excel
        if 'Storage' in model.container:
            self.storage = StorageSystems(model['Storage'], 'name')
        else:
            self.storage = None

        # 8) Degradación de batería on-board (fleet-wide): opcional — solo si
        # la hoja 'BatteryDegradation' existe en el Excel
        if 'BatteryDegradation' in model.container:
            self.battery_degradation = BatteryDegradation(model['BatteryDegradation'])
        else:
            self.battery_degradation = None

    # ------------------------------------------------------------------ #
    # Nodo / Layout
    # ------------------------------------------------------------------ #
    def get_system_nodes(self):
        """Devuelve lista de todos los nombres de nodos en el layout."""
        return list(self.layout.get("node_name", None))

    # ------------------------------------------------------------------ #
    # LHD: lista completa o filtrada por 'technology_type'
    # ------------------------------------------------------------------ #
    def get_system_lhds(self):
        """Devuelve TODOS los LHD (sin filtrar por tecnología)."""
        return list(self.elhd.elhds)

    def get_lhds_by_technology(self, tech_type: str):
        """
        Devuelve solo los LHD cuyo 'technology_type' coincide con tech_type.
        Ejemplo:
            get_lhds_by_technology("electric")   → lista de todos los LHD eléctricos
            get_lhds_by_technology("diesel")     → lista de todos los LHD diésel
            get_lhds_by_technology("Battery_swap") → lista de Battery_swap
        """
        df = self.elhd.data
        seleccion = df[df["technology_type"] == tech_type]
        return list(seleccion.index)

    # Métodos de comodidad (wrappers) para tecnologías frecuentes:
    def get_electric_lhds(self):
        return self.get_lhds_by_technology("electric")

    def get_diesel_lhds(self):
        return self.get_lhds_by_technology("diesel")

    def get_swap_lhds(self):
        return self.get_lhds_by_technology("Battery_swap")

    # ------------------------------------------------------------------ #
    # BATERÍAS: si las estás manejando con battery.py (hoja “Batteries”)
    # ------------------------------------------------------------------ #
    def get_system_batteries(self):
        """Devuelve lista de todos los nombres de baterías registradas."""
        return list(self.battery.data.index)

    def get_batteries_by_operation_mode(self, op_mode: str):
        """
        Devuelve baterías cuyo 'operation_mode' coincida con op_mode.
        Ejemplo:
            get_batteries_by_operation_mode("offboard") → baterías offboard
        """
        df = self.battery.data
        seleccion = df[df["operation_mode"] == op_mode]
        return list(seleccion.index)
    
    # ------------------------------------------------------------------ #
    # Estaciones de carga
    # ------------------------------------------------------------------ #
    def get_system_stations(self):
        """Devuelve lista de todas las estaciones de carga."""
        return list(self.stations.get("station_name", None))

    # ------------------------------------------------------------------ #
    # Generadores
    # ------------------------------------------------------------------ #
    def get_system_generators(self) -> list:
        """Devuelve lista de nombres de generadores, o [] si no hay."""
        if self.generators is None:
            return []
        return self.generators.get_names()

    # ------------------------------------------------------------------ #
    # Almacenamiento (BESS)
    # ------------------------------------------------------------------ #
    def get_system_storage(self) -> list:
        """Devuelve lista de nombres de unidades de almacenamiento, o [] si no hay."""
        if self.storage is None:
            return []
        return self.storage.get_names()