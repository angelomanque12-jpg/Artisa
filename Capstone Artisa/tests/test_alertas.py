"""Parte 8 del modelo de rutas: detectar alertas en vivo y guardarlas en alertas_ruta."""

from datetime import datetime, timedelta
import json
import time
import unittest

try:
    from machine_learning import modelo_rutas as mr
except ModuleNotFoundError as error:  # numpy/sklearn se validan dentro del contenedor Machine
    if error.name not in {"numpy", "sklearn"}:
        raise
    mr = None

from tests.test_rutas_api import FakeCursor
from tests.test_vigilancia_api import AHORA, RUTAS, camino, flota

VERSION = {"id": 9, "creado_en": datetime(2026, 10, 10, 1, 0)}


class FakeConnection:
    """Conexión simulada: responde las lecturas y anota las escrituras en alertas_ruta."""

    def __init__(self, filas, faenas=(), guardadas=(), rutas=None, version=VERSION, copias=None, abiertos=(),
                 siguiente=None):
        self.escrituras, self.cortes = [], []
        copias, siguiente = copias or {}, siguiente or {}
        catalogo = [{"asset_id": f"T{k:02d}"} for k in range(20)]

        def escribir(params):
            self.escrituras.append(params)
            return []

        self.cursor_ = FakeCursor([
            ("FROM rutas_version", [version] if version else []),
            ("FROM rutas_clasificadas", rutas or RUTAS),
            ("WHERE t.nombre = 'camion'", catalogo),
            ("FROM raw_gps USE INDEX", []),
            ("FROM telemetria_api USE INDEX", filas),
            ("FROM faenas", list(faenas)),
            ("FROM telemetria_api\n                    WHERE asset_id=%s", [{
                "asset_name": "Lampa-919", "driver_name": "JUAN", "driver_last_name": "PÉREZ",
                "location": "Sierra Gorda"}]),
            ("SELECT id, asset_id, inicio, fin, estado FROM alertas_ruta", list(guardadas)),
            ("alertas_ruta", escribir),
            ("COUNT(*) AS copias", lambda params: [copias.get(params, {"copias": 1})]),
            ("WHERE hasta IS NULL", list(abiertos)),
            ("report_date > %s ORDER BY", lambda params: [siguiente[params[0]]] if params[0] in siguiente else []),
            ("cortes_senal", lambda params: self.cortes.append(params) or []),
        ])

    def cursor(self):
        connection = self

        class Contexto:
            def __enter__(self):
                return connection.cursor_

            def __exit__(self, *args):
                return False

        return Contexto()

    def commit(self):
        pass


def detectar(filas, faenas=()):
    """Alertas detectadas sin guardar (la lista que recibe guardar_alertas)."""
    mr._VIGILANCIA.update(version=None, mapa=None, etiquetas={})
    capturadas = {}
    original = mr.guardar_alertas
    def capturar(connection, alertas, ahora, version_id):
        capturadas["a"] = alertas
        return {}

    mr.guardar_alertas = capturar
    try:
        mr.vigilar(FakeConnection(filas, faenas), AHORA)
    finally:
        mr.guardar_alertas = original
    return capturadas.get("a", [])


@unittest.skipIf(mr is None, "numpy/scikit-learn se validan dentro del contenedor Machine")
class DeteccionTests(unittest.TestCase):
    def test_truck_off_every_learned_route_is_an_active_alert(self):
        alerta, = detectar(flota())
        self.assertEqual((alerta["asset_id"], alerta["estado"], alerta["nivel"], alerta["reportes"]),
                         ("T19", "activa", "N6", 12))
        self.assertAlmostEqual(alerta["km_ruta_habitual"], 41, delta=2)
        self.assertAlmostEqual(alerta["corredor"][1][1], -68.9, delta=0.01)  # sobre la troncal
        self.assertEqual((alerta["nombre"], alerta["conductor"], alerta["ubicacion"]),
                         ("Lampa-919", "JUAN PÉREZ", "Sierra Gorda"))
        self.assertEqual(alerta["minutos"], 11)

    def test_minimal_route_alert_names_that_route(self):
        alerta, = detectar(flota(desvio_lon=-68.70))
        self.assertEqual((alerta["nivel"], alerta["route_id"], alerta["ruta_label"]),
                         ("N5", "R-MINIMA", "Calama ↔ Punto aislado 3"))

    def test_without_signal_and_back_on_route(self):
        self.assertEqual(detectar(flota(termina=AHORA - timedelta(hours=2)))[0]["estado"], "sin_senal")
        filas = flota(termina=AHORA - timedelta(minutes=30))
        filas += camino("T19", AHORA - timedelta(minutes=10), 5, lat0=-22.45, lat1=-22.46)
        self.assertEqual(detectar(filas)[0]["estado"], "terminada")

    def test_worksite_short_stopped_or_nearby_detours_are_not_alerts(self):
        faena = {"nombre": "Faena Este", "geojson": json.dumps({"type": "Polygon", "coordinates": [[
            [-68.6, -22.6], [-68.4, -22.6], [-68.4, -22.4], [-68.6, -22.4], [-68.6, -22.6]]]})}
        self.assertEqual(detectar(flota(), [faena]), [])
        self.assertEqual(detectar(flota(minutos=8)), [])                       # 8 min
        self.assertEqual(detectar([dict(f, speed=0.0) if f["longitude"] == -68.50 else f for f in flota()]), [])
        self.assertEqual(detectar(flota(desvio_lon=-68.885)), [])              # a ~1,5 km de la troncal

    def test_the_trail_is_cut_where_reports_stop(self):
        # Como TFWK19: 17 min sin reportes en medio de la alerta. No se une con una recta.
        filas = flota()
        filas += camino("T19", AHORA + timedelta(minutes=17), 6, lat0=-22.53, lat1=-22.56, lon=-68.50)
        alerta, = detectar(filas)
        self.assertEqual(len(alerta["estela"]), 2)
        self.assertEqual(alerta["estela"][1][0], [-22.53, -68.5])

    def test_without_a_learned_model_nothing_is_checked(self):
        self.assertIsNone(mr.vigilar(FakeConnection(flota(), version=None), AHORA))

    def test_a_day_of_the_whole_fleet_is_fast(self):
        rutas = RUTAS + [dict(RUTAS[0], route_id=f"R{k}", usage_level="N2",
                              geometria=json.dumps({"type": "LineString",
                                                    "coordinates": [[-69.0 - k * 0.01, -22.4], [-69.0 - k * 0.01, -22.6]]}))
                         for k in range(300)]
        filas = []
        for k in range(20):
            for tanda in range(6):
                filas += camino(f"T{k:02d}", AHORA - timedelta(hours=4 * tanda + 3), 160,
                                lat0=-22.0, lat1=-23.5, lon=-68.9 - (k % 10) * 0.05)
        mr._VIGILANCIA.update(version=None, mapa=None, etiquetas={})
        comienzo = time.monotonic()
        mr.vigilar(FakeConnection(filas, rutas=rutas), AHORA)
        self.assertLess(time.monotonic() - comienzo, 20)


def alerta(inicio, fin, estado="activa", asset="T19"):
    return {"asset_id": asset, "inicio": inicio, "fin": fin, "estado": estado, "nivel": "N6",
            "route_id": None, "ruta_label": None, "minutos": round((fin - inicio).total_seconds() / 60),
            "reportes": 12, "km_recorridos": 9.5, "km_ruta_habitual": 41.0, "velocidad": 60.0,
            "latitud": -22.52, "longitud": -68.5, "corredor": (41.0, [-22.5, -68.9]),
            "estela": [[-22.5, -68.5], [-22.52, -68.5]], "nombre": "Lampa-919", "conductor": "JUAN PÉREZ",
            "ubicacion": "Sierra Gorda"}


@unittest.skipIf(mr is None, "numpy/scikit-learn se validan dentro del contenedor Machine")
class CortesTests(unittest.TestCase):
    """Parte 9: como TFWK19, que a 87 km/h fuera de ruta pasó 17 min sin posición y después quedó
    transmitiendo copias del último reporte sin posición nueva."""

    def setUp(self):
        mr._VIGILANCIA.update(version=None, mapa=None, etiquetas={})

    def detectar(self, filas):
        return mr.detectar_cortes(mr.rm.leer_reportes(FakeConnection(filas).cursor_, AHORA - timedelta(hours=24),
                                                      AHORA + timedelta(hours=1), {f"T{k:02d}" for k in range(20)}),
                                  mr.rm.MapaRutas(mr.rm.leer_rutas(FakeConnection([]).cursor_, 9)), [], AHORA + timedelta(minutes=40))

    def test_a_moving_truck_off_route_that_stops_reporting(self):
        filas = flota() + camino("T19", AHORA + timedelta(minutes=17), 6, lat0=-22.53, lat1=-22.56, lon=-68.50)
        hueco, abierto = self.detectar(filas)
        self.assertEqual((hueco["asset_id"], hueco["minutos"], hueco["km_linea"]), ("T19", 17, 1.1))
        self.assertEqual((hueco["lat_antes"], hueco["lat_despues"]), (-22.52, -22.53))
        self.assertIsNone(abierto["hasta"])                      # sigue sin posición nueva

    def test_gaps_on_usual_routes_or_while_stopped_are_not_cuts(self):
        filas = [f for f in flota() if f["asset_id"] != "T19"]  # en la troncal (N1)
        self.assertEqual(self.detectar(filas), [])
        parado = [dict(f, speed=0.0) for f in flota() if f["asset_id"] == "T19"]
        self.assertEqual(self.detectar(parado), [])

    def test_copies_that_keep_arriving_mean_the_gps_was_frozen(self):
        desde = AHORA - timedelta(minutes=50)
        base = {"asset_id": "T19", "desde": desde}
        congelado = mr._copias(FakeConnection([], copias={("T19", desde): {
            "copias": 13, "primera": AHORA - timedelta(minutes=40), "ultima": AHORA - timedelta(minutes=1),
            "encendido": 1, "evento": "Reporte por tiempo"}}).cursor_, base)
        self.assertEqual((congelado["tipo"], congelado["copias"], congelado["encendido_antes"]), ("gps_congelado", 13, 1))
        # Copias que llegaron juntas (el conector se puso al día) no dicen que el equipo transmitía.
        juntas = mr._copias(FakeConnection([], copias={("T19", desde): {
            "copias": 13, "primera": AHORA, "ultima": AHORA + timedelta(seconds=3)}}).cursor_, base)
        self.assertEqual(juntas["tipo"], "sin_datos")

    def test_an_open_cut_out_of_the_window_is_closed_with_the_next_position(self):
        desde = AHORA - timedelta(hours=30)
        abierto = {"asset_id": "T19", "desde": desde, "lat_antes": -22.5, "lon_antes": -68.5}
        connection = FakeConnection([], abiertos=[abierto], siguiente={"T19": {
            "report_date": desde + timedelta(hours=2), "latitude": -22.6, "longitude": -68.5}})
        mr.guardar_cortes(connection, [], AHORA)
        hasta, minutos, *_ = connection.cortes[-1]
        self.assertEqual((hasta, minutos), (desde + timedelta(hours=2), 120))


@unittest.skipIf(mr is None, "numpy/scikit-learn se validan dentro del contenedor Machine")
class GuardadoTests(unittest.TestCase):
    def guardar(self, alertas, guardadas=()):
        connection = FakeConnection([], guardadas=guardadas)
        resultado = mr.guardar_alertas(connection, alertas, AHORA, 9)
        return resultado, connection.escrituras

    def test_a_new_alert_is_inserted(self):
        resultado, escrituras = self.guardar([alerta(AHORA - timedelta(minutes=20), AHORA)])
        self.assertEqual(resultado, {"nuevas": 1, "actualizadas": 0, "cerradas": 0})
        valores = escrituras[0]
        self.assertIn("T19", valores)
        self.assertIn(9, valores)  # versión del modelo con que se detectó

    def test_an_open_alert_is_updated_even_if_the_window_cut_its_start(self):
        guardada = {"id": 7, "asset_id": "T19", "inicio": AHORA - timedelta(hours=30),
                    "fin": AHORA - timedelta(minutes=1), "estado": "activa"}
        resultado, escrituras = self.guardar([alerta(AHORA - timedelta(hours=24), AHORA)], [guardada])
        self.assertEqual(resultado, {"nuevas": 0, "actualizadas": 1, "cerradas": 0})
        self.assertEqual(escrituras[0][-1], 7)

    def test_a_finished_alert_stays_as_it_was(self):
        guardada = {"id": 7, "asset_id": "T19", "inicio": AHORA - timedelta(hours=3),
                    "fin": AHORA - timedelta(hours=2), "estado": "terminada"}
        resultado, escrituras = self.guardar([alerta(AHORA - timedelta(hours=3), AHORA - timedelta(hours=2),
                                                     "terminada")], [guardada])
        self.assertEqual((resultado, escrituras), ({"nuevas": 0, "actualizadas": 0, "cerradas": 0}, []))

    def test_an_open_alert_that_no_longer_appears_is_closed(self):
        guardada = {"id": 7, "asset_id": "T19", "inicio": AHORA - timedelta(hours=3),
                    "fin": AHORA - timedelta(hours=2), "estado": "sin_senal"}
        resultado, escrituras = self.guardar([], [guardada])
        self.assertEqual(resultado["cerradas"], 1)
        self.assertEqual(escrituras, [(AHORA, 7)])


if __name__ == "__main__":
    unittest.main()
