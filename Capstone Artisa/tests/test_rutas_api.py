"""Endpoints de rutas: contrato del mapa y recorrido observado de un camión."""

from datetime import date, datetime
import json
import unittest

from endpoints import rutas_api


class FakeCursor:
    def __init__(self, handlers):
        self.handlers = handlers
        self.rows = []

    def execute(self, query, params=None):
        for needle, result in self.handlers:
            if needle in query:
                self.rows = result(params) if callable(result) else result
                return
        raise AssertionError(query)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


VERSION = {"id": 4, "desde": date(2026, 7, 1), "hasta": date(2026, 9, 28), "activa": 1,
           "creado_en": datetime(2026, 10, 6),
           "metricas_json": json.dumps({"viajes_clasificados": 18, "camiones": 5,
                                        "por_nivel": {"N1": 1, "N5": 1},
                                        "umbrales": {"cortes": [{"nivel": "N1", "desde": 3, "hasta": 4},
                                                                {"nivel": "N5", "desde": 1, "hasta": 2}]}}),
           "lugares_json": "{}"}
RUTA = {"route_id": "R-ABC123", "label": "Faena Lampa ↔ Lugar 1", "origen": "F7", "destino": "L0",
        "usage_count": 12, "unique_vehicle_count": 4, "observed_points": 360, "km_mediana": 16.6,
        "usage_level": "N1", "geometry_source": "viaje_real_medoide",
        "geometria": json.dumps({"type": "LineString", "coordinates": [[-70.6, -33.4], [-70.6, -33.25]]}),
        "primer_viaje": datetime(2026, 7, 2), "ultimo_viaje": datetime(2026, 9, 27)}


class MapContractTests(unittest.TestCase):
    def test_pending_without_active_version(self):
        cursor = FakeCursor([("FROM rutas_version", [])])
        self.assertIsNone(rutas_api.classified_map(cursor))

    def test_classified_map_matches_frontend_contract(self):
        cursor = FakeCursor([
            ("FROM rutas_version", lambda p: [dict(VERSION)]),
            ("FROM rutas_clasificadas WHERE", [RUTA]),
            ("FROM rutas_vehiculo", [{"asset_id": "RJXS30", "route_id": "R-ABC123"}]),
            ("SELECT v.asset_id FROM vehiculos", [{"asset_id": "RJXS30"}]),
            ("FROM telemetria_api AS gps", [{"asset_id": "RJXS30", "asset_name": "Lampa-826",
                                             "report_date": datetime(2026, 10, 6, 17), "latitude": -33.3,
                                             "longitude": -70.6, "speed": 40.0, "heading": 5,
                                             "ignition": 1, "event_name": "Reporte",
                                             "driver_name": "JULIO", "driver_last_name": "PÉREZ"}]),
            ("FROM raw_gps AS gps", []),
            ("validos", [{"n": 500}]),
        ])
        payload = rutas_api.classified_map(cursor)
        for key in ("routes", "trips", "vehicles", "readings"):
            self.assertIsInstance(payload[key], list)
        self.assertNotEqual(payload["source_mode"], "PENDING_MACHINE")
        route = payload["routes"][0]
        self.assertEqual(route["geometry"]["type"], "LineString")
        self.assertEqual((route["usage_label"], route["color"]), ("Troncal", "#14532d"))
        self.assertFalse(route["map_matching"])
        vehicle = payload["vehicles"][0]
        self.assertEqual((vehicle["vehicle_id"], vehicle["route_id"], vehicle["fuente"]), ("RJXS30", "R-ABC123", "api"))
        self.assertEqual((vehicle["patente"], vehicle["driver_name"], vehicle["driver_last_name"]),
                         ("RJXS30", "JULIO", "PÉREZ"))
        self.assertEqual(payload["ranking"]["most_used"]["route_id"], "R-ABC123")
        self.assertIn("N1 3–4", payload["metadata"]["classification"])

    def test_latest_position_uses_recent_driver_when_last_report_has_none(self):
        recent_driver = {"asset_id": "TFWK19", "driver_name": "RICHARD", "driver_last_name": "FUENTES",
                         "report_date": datetime(2026, 10, 14, 11, 40)}
        cursor = FakeCursor([
            ("SELECT v.asset_id FROM vehiculos", [{"asset_id": "TFWK19"}]),
            ("FROM telemetria_api AS gps", [{"asset_id": "TFWK19", "asset_name": "Lampa-998",
                                             "report_date": datetime(2026, 10, 14, 11, 41),
                                             "latitude": -29.4, "longitude": -71.1, "speed": 87.0,
                                             "heading": 180, "ignition": 1, "event_name": "Reporte por tiempo",
                                             "driver_name": None, "driver_last_name": None}]),
            ("FROM raw_gps AS gps", []),
            ("ROW_NUMBER() OVER", [recent_driver]),
        ])
        vehicle = rutas_api.latest_positions(cursor, {})[0]
        self.assertEqual((vehicle["driver_name"], vehicle["driver_last_name"]), ("RICHARD", "FUENTES"))

    def test_live_positions_only_read_the_company_api(self):
        cursor = FakeCursor([
            ("FROM telemetria_api AS gps", [{"asset_id": "TFWK19", "asset_name": None,
                                             "report_date": datetime(2026, 10, 14, 11, 41),
                                             "latitude": -29.4, "longitude": -71.1, "speed": 87.0,
                                             "heading": 180, "ignition": 1, "event_name": "Reporte por tiempo",
                                             "driver_name": "RICHARD", "driver_last_name": "FUENTES"}]),
        ])
        vehicle, = rutas_api.live_positions(cursor)
        self.assertEqual((vehicle["vehicle_name"], vehicle["fuente"], vehicle["route_id"]),
                         ("TFWK19", "api", None))  # raw_gps no está en el cursor: fallaría si se consultara


class JourneyTests(unittest.TestCase):
    def test_journey_splits_gps_jumps_and_uses_chile_time(self):
        base = datetime(2026, 10, 6, 15)  # 12:00 hora de Chile
        rows = [{"report_id": f"r{i}", "asset_name": "Lampa-826", "report_date": base.replace(minute=i),
                 "latitude": -33.4 + i * 0.001, "longitude": -70.6, "driver_name": None, "driver_last_name": None}
                for i in range(10)]
        rows[5] = dict(rows[5], latitude=-20.0)  # salto de GPS
        cursor = FakeCursor([
            ("FROM telemetria_api WHERE asset_id=%s AND report_date", rows),
            ("FROM raw_gps WHERE asset_id=%s AND report_date", []),
        ])
        journey = rutas_api.truck_journey(cursor, "RJXS30", date(2026, 10, 6))
        self.assertEqual(journey["fecha"], "2026-10-06")
        self.assertEqual(journey["inicio"], "2026-10-06 12:00:00")
        self.assertEqual(journey["geometry"]["type"], "MultiLineString")
        self.assertEqual(len(journey["geometry"]["coordinates"]), 2)
        self.assertTrue(all(-34 < lat < -33 for path in journey["geometry"]["coordinates"] for _, lat in path))
        self.assertEqual(journey["cortes"], 0)  # un salto de GPS no es un corte de señal

    def test_journey_counts_signal_gaps(self):
        base = datetime(2026, 10, 6, 15)
        minutos = [0, 1, 2, 20, 21, 50]  # dos cortes: 18 y 29 min sin reportes
        rows = [{"report_id": f"r{m}", "asset_name": "Lampa-826", "report_date": base.replace(minute=m),
                 "latitude": -33.4 + m * 0.0001, "longitude": -70.6, "driver_name": None, "driver_last_name": None}
                for m in minutos]
        cursor = FakeCursor([
            ("FROM telemetria_api WHERE asset_id=%s AND report_date", rows),
            ("FROM raw_gps WHERE asset_id=%s AND report_date", []),
        ])
        journey = rutas_api.truck_journey(cursor, "RJXS30", date(2026, 10, 6))
        self.assertEqual(journey["cortes"], 2)
        self.assertEqual(journey["minutos_sin_senal"], 47.0)


if __name__ == "__main__":
    unittest.main()
