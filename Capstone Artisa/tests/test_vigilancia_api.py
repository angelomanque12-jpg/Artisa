"""API de rutas aprendidas: mapa, nivel de cada camión en vivo y lectura del registro de alertas."""

from datetime import date, datetime, timedelta
import json
import unittest
from unittest.mock import patch

from common import rutas_mapa as rm
from endpoints import vigilancia_api as v
from tests.test_rutas_api import FakeCursor

AHORA = datetime(2026, 10, 19, 15, 0)
VERSION = {"id": 9, "desde": date(2026, 7, 12), "hasta": date(2026, 10, 9), "activa": 1,
           "creado_en": datetime(2026, 10, 10, 1, 0),
           "metricas_json": json.dumps({"viajes_clasificados": 300, "camiones": 20, "lugares": 4,
                                        "parametros": {"metodo": "camiones-jenks-v1"},
                                        "umbrales": {"cortes": [{"nivel": "N1", "desde": 15, "hasta": 20},
                                                                {"nivel": "N2", "desde": 3, "hasta": 14},
                                                                {"nivel": "N5", "desde": 1, "hasta": 2}]}}),
           "lugares_json": "{}"}


def linea(lon, lat0=-22.40, lat1=-22.60):
    return json.dumps({"type": "LineString", "coordinates": [[lon, lat0], [lon, lat1]]})


RUTAS = [
    {"route_id": "R-TRONCAL", "label": "Faena Norte ↔ Calama", "usage_level": "N1", "usage_count": 280,
     "unique_vehicle_count": 20, "km_mediana": 22.0, "geometria": linea(-68.90),
     "primer_viaje": datetime(2026, 7, 12), "ultimo_viaje": datetime(2026, 10, 9)},
    {"route_id": "R-MINIMA", "label": "Calama ↔ Punto aislado 3", "usage_level": "N5", "usage_count": 2,
     "unique_vehicle_count": 1, "km_mediana": 22.0, "geometria": linea(-68.70),
     "primer_viaje": datetime(2026, 8, 1), "ultimo_viaje": datetime(2026, 8, 2)},
]


def camino(asset_id, inicio, minutos, lat0=-22.40, lat1=-22.60, lon=-68.90, speed=60.0):
    """Reportes cada minuto en línea recta (norte a sur)."""
    return [{"asset_id": asset_id, "report_date": inicio + timedelta(minutes=k),
             "latitude": lat0 + (lat1 - lat0) * k / max(1, minutos - 1), "longitude": lon, "speed": speed}
            for k in range(minutos)]


def flota(desvio_lon=-68.50, termina=AHORA, minutos=12):
    """20 camiones por la troncal; T19 sale de ella y sigue por desvio_lon hasta `termina`."""
    filas = []
    for k in range(20):
        filas += camino(f"T{k:02d}", AHORA - timedelta(days=2, hours=k), 30)
    return filas + camino("T19", termina - timedelta(minutes=minutos - 1), minutos, lat0=-22.50, lat1=-22.52,
                          lon=desvio_lon)


def cursor_con(alertas=(), version=VERSION, cortes=()):
    return FakeCursor([
        ("FROM rutas_version", lambda _: [dict(version)] if version else []),
        ("FROM rutas_clasificadas", RUTAS),
        ("FROM alertas_ruta", list(alertas)),
        ("FROM cortes_senal", list(cortes)),
    ])


def corte(desde, hasta=None, tipo="sin_datos", asset="T19"):
    """Fila de cortes_senal tal como la deja Machine."""
    return {"asset_id": asset, "desde": desde, "hasta": hasta, "minutos": 17, "tipo": tipo,
            "lat_antes": -22.5, "lon_antes": -68.5, "velocidad_antes": 87.0, "encendido_antes": 1,
            "evento_antes": "Reporte por tiempo", "lat_despues": -22.45 if hasta else None,
            "lon_despues": -68.45 if hasta else None, "km_linea": 10.9 if hasta else None,
            "kmh_necesaria": 38.0 if hasta else None, "copias": 13, "ultima_copia": desde + timedelta(minutes=16)}


def guardada(id_, inicio, fin, estado="activa", asset="T19", nivel="N6"):
    """Fila de alertas_ruta tal como la deja Machine."""
    return {"id": id_, "asset_id": asset, "asset_name": "Lampa-919", "conductor": "JUAN PÉREZ",
            "ubicacion": "Sierra Gorda", "inicio": inicio, "fin": fin, "estado": estado, "nivel": nivel,
            "route_id": None, "ruta_label": None, "minutos": round((fin - inicio).total_seconds() / 60),
            "reportes": 12, "km_recorridos": 9.5, "km_ruta_habitual": 41.0, "velocidad": 60.0,
            "latitud": -22.52, "longitud": -68.5, "corredor_lat": -22.5, "corredor_lon": -68.9, "corredor_km": 41.0,
            "estela": "[[-22.5,-68.5],[-22.52,-68.5]]", "version_modelo": 9}


class ModeloTests(unittest.TestCase):
    def setUp(self):
        v._CACHE.update(version=None, modelo=None, mapa=None)

    def test_learned_routes_with_levels_and_table(self):
        data = v.rutas_aprendidas(cursor_con())
        self.assertEqual([r["id"] for r in data["rutas"]], ["R-TRONCAL", "R-MINIMA"])
        self.assertEqual(data["total_rutas"], 2)
        self.assertEqual(data["rutas"][0]["lineas"], [[[-22.4, -68.9], [-22.6, -68.9]]])
        tabla = {n["nivel"]: n for n in data["niveles"]}
        self.assertEqual(list(tabla), ["N1", "N2", "N5", "N6"])   # N3/N4: el modelo aprendió 2 grupos
        self.assertEqual((tabla["N1"]["desde"], tabla["N1"]["hasta"], tabla["N1"]["rutas"]), (15, 20, 1))
        self.assertEqual([n["nivel"] for n in data["niveles"] if n["alerta"]], ["N5", "N6"])
        self.assertEqual((data["datos_hasta"], data["proxima_con_datos_hasta"]), ("2026-10-09", "2026-10-10"))
        self.assertEqual((data["prevision_desde"], data["prevision_hasta"]), ("2026-10-10", "2026-10-16"))

    def test_shared_roads_are_drawn_once_with_the_best_level(self):
        # Una ruta mínima que va por la misma carretera que la troncal no agrega otra línea: cada
        # tramo aparece una vez, con el mejor nivel, y dice qué rutas lo usan (la troncal primero).
        global RUTAS
        original = RUTAS
        RUTAS = original + [dict(original[1], route_id="R-MINIMA-2", geometria=linea(-68.90))]
        try:
            data = v.rutas_aprendidas(cursor_con())
        finally:
            RUTAS = original
        red = data["red"]
        compartidos = [t for t in red["tramos"] if t[3] == 2]
        self.assertTrue(compartidos)
        self.assertTrue(all(t[2] == 1 and t[4] == [0, 2] for t in compartidos))
        pares = {(min(a, b), max(a, b)) for a, b, *_ in red["tramos"]}
        self.assertEqual(len(pares), len(red["tramos"]))   # sin tramos repetidos
        self.assertTrue(all(abs(lon + 68.90) < 1e-6 or abs(lon + 68.70) < 1e-6 for _, lon in red["nodos"]))

    def test_position_level_uses_the_cell_and_its_neighbors(self):
        _, mapa = v.leer_modelo(cursor_con())
        self.assertEqual(mapa.en(-22.5, -68.9), (1, "R-TRONCAL"))
        self.assertEqual(mapa.en(-22.5, -68.9 + 1 / rm.ESCALA)[0], 1)     # celda vecina
        self.assertEqual(mapa.en(-22.5, -68.70), (5, "R-MINIMA"))
        self.assertEqual(mapa.en(-22.5, -68.50), (6, None))               # fuera de ruta
        self.assertAlmostEqual(mapa.distancia(-22.5, -68.5), 41.1, delta=1.5)

    def test_pending_without_a_learned_model(self):
        self.assertIsNone(v.rutas_aprendidas(cursor_con(version=None)))
        self.assertIsNone(v.vigilancia(cursor_con(version=None), AHORA))


class EnVivoTests(unittest.TestCase):
    def setUp(self):
        v._CACHE.update(version=None, modelo=None, mapa=None)
        posiciones = [{"vehicle_id": "T19", "latitude": -22.52, "longitude": -68.50,
                       "timestamp": (AHORA - timedelta(minutes=1)).isoformat() + "Z"},
                      {"vehicle_id": "T00", "latitude": -22.5, "longitude": -68.9,
                       "timestamp": (AHORA - timedelta(minutes=2)).isoformat() + "Z"},
                      {"vehicle_id": "T01", "latitude": -22.5, "longitude": -68.9,
                       "timestamp": (AHORA - timedelta(days=1)).isoformat() + "Z"}]
        self.vehiculos = patch.object(v.rutas_api, "latest_positions", return_value=posiciones)
        self.vehiculos.start()

    def tearDown(self):
        self.vehiculos.stop()

    def test_stored_alerts_and_the_level_of_each_truck(self):
        data = v.vigilancia(cursor_con([guardada(1, AHORA - timedelta(minutes=20), AHORA - timedelta(minutes=1))]), AHORA)
        alerta, = data["alertas"]
        self.assertEqual((alerta["patente"], alerta["estado"], alerta["nivel"], alerta["minutos"]), ("T19", "activa", "N6", 19))
        self.assertEqual(alerta["corredor"], {"km": 41.0, "punto": [-22.5, -68.9]})
        self.assertEqual(alerta["estela"], [[[-22.5, -68.5], [-22.52, -68.5]]])  # guardada sin tramos: uno solo
        self.assertEqual((alerta["nombre"], alerta["conductor"]), ("Lampa-919", "JUAN PÉREZ"))
        self.assertEqual(data["resumen"]["activas"], 1)
        self.assertEqual(data["modelo"]["version"], 9)
        self.assertNotIn("rutas", data["modelo"])  # las líneas van en /api/rutas-aprendidas
        t19, t00, t01 = data["vehiculos"]
        self.assertEqual((t19["en_alerta"], t19["nivel"]), (True, "N6"))
        self.assertEqual((t00["en_alerta"], t00["nivel"], t00["ruta_label"]), (False, "N1", "Faena Norte ↔ Calama"))
        self.assertIsNone(t01["nivel"])  # posición de ayer: no se clasifica
        self.assertEqual(data["resumen"]["camiones_por_nivel"], {"N6": 1, "N1": 1})

    def test_each_alert_carries_the_signal_cuts_during_it(self):
        inicio, fin = AHORA - timedelta(minutes=50), AHORA - timedelta(minutes=40)
        cortes = [corte(inicio, inicio + timedelta(minutes=17)),                     # durante la alerta
                  corte(fin, None, "gps_congelado"),                                  # sigue sin posición
                  corte(inicio, inicio + timedelta(minutes=17), asset="T05")]         # otro camión
        data = v.vigilancia(cursor_con([guardada(1, inicio, fin)], cortes=cortes), AHORA)
        cerrado, abierto = data["alertas"][0]["cortes"]
        self.assertEqual((cerrado["minutos"], cerrado["km_linea"], cerrado["despues"]), (17, 10.9, [-22.45, -68.45]))
        self.assertEqual((abierto["hasta"], abierto["tipo"], abierto["minutos"]), (None, "gps_congelado", 40))

    def test_an_active_alert_without_new_data_shows_without_signal(self):
        # Si Machine dejó de revisar, la web no la sigue mostrando como activa.
        data = v.vigilancia(cursor_con([guardada(1, AHORA - timedelta(hours=3), AHORA - timedelta(hours=2))]), AHORA)
        self.assertEqual(data["alertas"][0]["estado"], "sin_senal")
        self.assertFalse(data["vehiculos"][0]["en_alerta"])

    def test_history_has_only_finished_alerts_newest_first(self):
        cursor = cursor_con([guardada(2, AHORA - timedelta(hours=2), AHORA - timedelta(hours=1), "terminada", asset="T05"),
                             guardada(1, AHORA - timedelta(hours=5), AHORA - timedelta(hours=4), "terminada")])
        consultas = []
        execute = cursor.execute
        cursor.execute = lambda query, params=None: (consultas.append(query), execute(query, params))
        data = v.alertas_rango(cursor, date(2026, 10, 19), date(2026, 10, 19), AHORA)
        self.assertIn("estado = 'terminada'", consultas[0])  # las abiertas no entran al historial
        self.assertEqual([a["id"] for a in data["alertas"]], [2, 1])
        self.assertEqual(data["resumen"]["terminadas"], 2)
        self.assertEqual(data["desde"], "2026-10-19")

    def test_cached_positions_get_the_latest_alerts(self):
        # La respuesta guardada en caché no tenía alertas; Machine guardó una después.
        vieja = v.vigilancia(cursor_con(), AHORA)
        self.assertEqual(vieja["resumen"]["activas"], 0)
        nueva = v.con_alertas(vieja, cursor_con([guardada(1, AHORA - timedelta(minutes=20), AHORA - timedelta(minutes=1))]), AHORA)
        self.assertEqual((nueva["resumen"]["activas"], nueva["vehiculos"][0]["en_alerta"]), (1, True))
        self.assertEqual(nueva["resumen"]["camiones_por_nivel"], vieja["resumen"]["camiones_por_nivel"])
        self.assertEqual(vieja["resumen"]["activas"], 0)  # la caché no se modifica

    def test_without_the_table_yet_there_are_no_alerts(self):
        def sin_tabla(_):
            raise v.pymysql.err.ProgrammingError(1146, "Table 'alertas_ruta' doesn't exist")
        cursor = FakeCursor([("FROM rutas_version", lambda _: [dict(VERSION)]), ("FROM rutas_clasificadas", RUTAS),
                             ("FROM alertas_ruta", sin_tabla)])
        self.assertEqual(v.vigilancia(cursor, AHORA)["alertas"], [])


if __name__ == "__main__":
    unittest.main()
