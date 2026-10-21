"""Modelo de rutas (machine_learning/modelo_rutas.py), parte por parte:
viajes (Parte 1), lugares y rutas (Partes 3-4), niveles (Parte 5) y reentrenamiento (Parte 6)."""

from datetime import datetime, timedelta
import json
import unittest

try:
    from machine_learning.modelo_rutas import (ViajeRuta, clasificar_niveles, construir_rutas, cortes_naturales,
                                               dentro_geojson, detectar_paradas, empalmar, extraer_viajes,
                                               resumen_paradas)
except ModuleNotFoundError as error:  # numpy/sklearn se validan dentro del contenedor Machine
    if error.name not in {"numpy", "sklearn"}:
        raise
    construir_rutas = None

A = (-33.400, -70.600)
B = (-33.250, -70.600)   # ~16,6 km al norte
C = (-33.400, -70.400)

# --- Parte 1: viajes entre paradas ---

def quieto(t, lugar, minutos):
    return [(t + timedelta(minutes=m), lugar[0] + (m % 3) * 0.0001, lugar[1]) for m in range(minutos)]


def manejar(t, desde, hasta, minutos):
    return [(t + timedelta(minutes=m), desde[0] + (hasta[0] - desde[0]) * m / minutos,
             desde[1] + (hasta[1] - desde[1]) * m / minutos) for m in range(1, minutos)]


def jornada(t0):
    puntos = quieto(t0, A, 30)
    t = puntos[-1][0]
    puntos += manejar(t, A, B, 20)
    t = puntos[-1][0] + timedelta(minutes=1)
    puntos += quieto(t, B, 40)
    t = puntos[-1][0]
    puntos += manejar(t, B, A, 20)
    t = puntos[-1][0] + timedelta(minutes=1)
    puntos += quieto(t, A, 30)
    return puntos


@unittest.skipIf(construir_rutas is None, "numpy/scikit-learn se validan dentro del contenedor Machine")
class TripTests(unittest.TestCase):
    def test_trips_between_stops(self):
        puntos = jornada(datetime(2026, 10, 6, 12))
        self.assertEqual(len(detectar_paradas(puntos)), 3)
        viajes = extraer_viajes("RJXS30", puntos)
        self.assertEqual(len(viajes), 2)
        ida, vuelta = viajes
        self.assertAlmostEqual(ida.origen[0], A[0], places=2)
        self.assertAlmostEqual(ida.destino[0], B[0], places=2)
        self.assertAlmostEqual(vuelta.destino[0], A[0], places=2)
        self.assertAlmostEqual(ida.km, 16.6, delta=0.6)
        self.assertFalse(ida.con_saltos)
        # La geometría es de puntos reales simplificados: una recta queda en pocos puntos.
        self.assertLessEqual(len(ida.tramos[0]), 4)

    def test_gps_jump_is_not_drawn(self):
        puntos = jornada(datetime(2026, 10, 6, 12))
        k = 40
        puntos[k] = (puntos[k][0], -20.0, -70.0)   # salto de GPS a 1.500 km
        ida = extraer_viajes("X", puntos)[0]
        self.assertTrue(ida.con_saltos)
        self.assertGreaterEqual(len(ida.tramos), 2)
        for tramo in ida.tramos:
            self.assertTrue(all(-34 < lat < -33 for lat, _ in tramo) or len(tramo) == 1)
        self.assertLess(ida.km, 20)

    def test_long_gap_cuts_trip_without_destination(self):
        t0 = datetime(2026, 10, 6, 12)
        puntos = quieto(t0, A, 30) + manejar(t0 + timedelta(minutes=29), A, B, 20)
        corte = puntos[-1][0] + timedelta(hours=2)
        puntos += [(corte + timedelta(minutes=m), B[0] + 0.01 * m / 20, B[1]) for m in range(20)]
        puntos += quieto(puntos[-1][0], (B[0] + 0.01, B[1]), 30)
        viajes = extraer_viajes("X", puntos)
        self.assertIsNone(viajes[0].destino)
        self.assertTrue(all(v.destino is None or v.origen is None or v is viajes[-1] for v in viajes))

    def test_long_loading_stop_without_reports_is_one_stop(self):
        # Llega a cargar, apaga el motor 2 horas (sin reportes) y sigue: son 2 viajes y 1 parada
        # intermedia de ~2 h, no un viaje cortado sin destino.
        t0 = datetime(2026, 10, 6, 8)
        puntos = quieto(t0, A, 20) + manejar(t0 + timedelta(minutes=19), A, B, 20)
        llegada = puntos[-1][0] + timedelta(minutes=1)
        puntos += quieto(llegada, B, 5)
        vuelta = puntos[-1][0] + timedelta(hours=2)
        puntos += quieto(vuelta, B, 5) + manejar(vuelta + timedelta(minutes=4), B, A, 20)
        puntos += quieto(puntos[-1][0] + timedelta(minutes=1), A, 20)
        viajes = extraer_viajes("X", puntos)
        self.assertEqual(len(viajes), 2)
        self.assertTrue(all(v.origen and v.destino for v in viajes))
        n, minutos = resumen_paradas(puntos)
        self.assertEqual(n, 3)
        self.assertGreater(minutos, 150)

    def test_midnight_split_is_stitched(self):
        medianoche = datetime(2026, 10, 7, 3)  # 00:00 de Chile en UTC
        puntos = quieto(medianoche - timedelta(minutes=40), A, 30)
        puntos += manejar(puntos[-1][0], A, B, 20)
        puntos += quieto(puntos[-1][0] + timedelta(minutes=1), B, 30)
        antes = [p for p in puntos if p[0] < medianoche]
        despues = [p for p in puntos if p[0] >= medianoche]
        dia1 = extraer_viajes("X", antes, medianoche - timedelta(days=1), medianoche)
        dia2 = extraer_viajes("X", despues, medianoche, medianoche + timedelta(days=1))
        self.assertTrue(dia1[-1].abierto_fin)
        self.assertTrue(dia2[0].abierto_inicio)
        (unido,) = [v for v in empalmar(sorted(dia1 + dia2, key=lambda v: v.inicio)) if v.km > 1]
        self.assertIsNotNone(unido.origen)
        self.assertIsNotNone(unido.destino)
        self.assertAlmostEqual(unido.km, 16.6, delta=0.8)
        self.assertFalse(unido.con_saltos)


# --- Partes 3 a 6: lugares, rutas, niveles y reentrenamiento ---

FAENA_A = {"id": 7, "nombre": "Faena Lampa", "geojson": {"type": "Polygon", "coordinates": [[
    [-70.605, -33.405], [-70.595, -33.405], [-70.595, -33.395], [-70.605, -33.395], [-70.605, -33.405]]]}}


def recorrido(desde, hasta, desvio_lon=0.0, ruido=0.0, n=30):
    puntos = []
    for k in range(n + 1):
        f = k / n
        curva = desvio_lon * (1 - abs(2 * f - 1))  # se aleja y vuelve: otra calle
        puntos.append((desde[0] + (hasta[0] - desde[0]) * f + ruido * ((k % 3) - 1),
                       desde[1] + (hasta[1] - desde[1]) * f + curva))
    return puntos


def viaje(asset, n, desde, hasta, **kw):
    t = datetime(2026, 9, 1, 12) + timedelta(hours=n)
    puntos = recorrido(desde, hasta, **kw)
    return ViajeRuta(asset_id=asset, inicio=t, fin=t + timedelta(minutes=30), origen=puntos[0],
                     destino=puntos[-1], km=17.0, puntos=len(puntos), con_saltos=False, tramos=[puntos])


@unittest.skipIf(construir_rutas is None, "numpy/scikit-learn se validan dentro del contenedor Machine")
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.viajes = []
        n = 0
        for i in range(12):   # camino directo A-B, en ambos sentidos y con ruido GPS
            ida = i % 2 == 0
            self.viajes.append(viaje(f"T{i % 4}", n, A if ida else B, B if ida else A, ruido=0.0003)); n += 1
        for i in range(6):    # desvío real: 4 km al este
            self.viajes.append(viaje(f"T{i % 3}", n, A, B, desvio_lon=0.045)); n += 1
        for i in range(5):    # A-C poco usado
            self.viajes.append(viaje(f"T{8 + i % 2}", n, A, C)); n += 1
        D = (-33.400, -70.200)
        self.viajes.append(viaje("T7", n, A, D)); n += 1   # un solo camión: ruta mínima

    def test_routes_are_separated_by_origin_destination_and_path(self):
        rutas, lugares, metricas = construir_rutas(self.viajes, [FAENA_A])
        self.assertEqual([(r["unique_vehicle_count"], r["usage_count"]) for r in rutas], [(4, 12), (3, 6), (2, 5), (1, 1)])
        directa = rutas[0]
        self.assertIn("Faena Lampa", directa["label"])
        # 1 o 2 camiones: mínima (N5). El resto se reparte en los grupos que aprende el modelo.
        self.assertEqual([r["usage_level"] for r in rutas], ["N1", "N2", "N5", "N5"])
        self.assertEqual(metricas["viajes_clasificados"], 24)
        self.assertEqual(metricas["caminos_esporadicos"], 0)
        self.assertEqual(sum(metricas["por_nivel"].values()), metricas["rutas"])
        cortes = {c["nivel"]: c for c in metricas["umbrales"]["cortes"]}
        self.assertEqual((cortes["N5"]["rutas"], cortes["N5"]["recorridos"]), (2, 6))
        self.assertEqual((cortes["N1"]["desde"], cortes["N1"]["hasta"]), (4, 4))

    def test_places_named_by_commune(self):
        def nombrar(lugares):
            for lugar in lugares.values():
                if lugar["tipo"] != "faena":
                    self.assertTrue(lugar["muestras"])
                    lugar["comuna"] = "Calama"
        rutas, lugares, _ = construir_rutas(self.viajes, [FAENA_A], nombrar=nombrar)
        nombres = sorted(l["nombre"] for l in lugares.values() if l["tipo"] != "faena")
        self.assertEqual(nombres[:2], ["Calama", "Calama 2"])
        self.assertTrue(rutas[0]["label"].startswith("Faena Lampa ↔ Calama"))

    def test_geometry_is_a_real_observed_trip(self):
        rutas, _, _ = construir_rutas(self.viajes, [FAENA_A])
        observados = {tuple((round(lon, 6), round(lat, 6)) for lat, lon in v.tramos[0]) for v in self.viajes}
        observados |= {tuple(reversed(t)) for t in observados}
        for ruta in rutas:
            self.assertIn(tuple(map(tuple, ruta["geometry"]["coordinates"])), observados)
            self.assertEqual(ruta["geometry_source"], "viaje_real_medoide")

    def test_route_ids_are_unique_even_if_the_short_code_repeats(self):
        from machine_learning import modelo_rutas
        medoide = self.viajes[0]
        usados = set()
        primero = modelo_rutas._route_id("L1", "L2", medoide, usados)
        segundo = modelo_rutas._route_id("L1", "L2", medoide, usados)   # mismo origen: choque forzado
        self.assertNotEqual(primero, segundo)
        self.assertLessEqual(len(segundo), 16)                         # route_id VARCHAR(16)
        rutas, _, _ = construir_rutas(self.viajes, [FAENA_A])
        self.assertEqual(len({r["route_id"] for r in rutas}), len(rutas))

    def test_natural_breaks_are_learned_and_contiguous(self):
        valores = [3, 3, 3, 4, 5, 18, 20, 25, 30, 90, 100, 110]
        tramos = cortes_naturales(valores, 4)
        self.assertEqual(len(tramos), 4)
        self.assertEqual((tramos[0][0], tramos[-1][1]), (3, 110))
        self.assertEqual(tramos[-1], (90, 110))          # las troncales quedan juntas
        self.assertIn((18, 30), tramos)
        for (_, hasta), (desde, _) in zip(tramos, tramos[1:]):
            self.assertLess(hasta, desde)
        self.assertEqual(cortes_naturales([7, 7], 4), [(7, 7)])
        self.assertEqual(cortes_naturales([], 4), [])

    def test_levels_from_trucks(self):
        niveles, umbrales = clasificar_niveles([1, 2, 3, 3, 4, 20, 22, 60, 116])
        self.assertEqual(niveles[:2], ["N5", "N5"])
        self.assertEqual(niveles[-1], "N1")
        self.assertEqual(len(niveles), 9)
        self.assertEqual([c["nivel"] for c in umbrales["cortes"]], ["N1", "N2", "N3", "N4", "N5"])
        # Solo rutas mínimas: no hay grupos aprendidos.
        self.assertEqual(clasificar_niveles([1, 2, 1])[0], ["N5"] * 3)

    def test_point_in_faena(self):
        self.assertTrue(dentro_geojson(-33.4, -70.6, FAENA_A["geojson"]))
        self.assertFalse(dentro_geojson(-33.3, -70.6, FAENA_A["geojson"]))


@unittest.skipIf(construir_rutas is None, "numpy/scikit-learn se validan dentro del contenedor Machine")
class RelearnTests(unittest.TestCase):
    def necesita(self, version, ultimo=datetime(2026, 10, 19).date()):
        from machine_learning import modelo_rutas
        from tests.test_rutas_api import FakeCursor
        cursor = FakeCursor([("FROM reporte_diario_estado", [{"hasta": ultimo}]),
                             ("FROM rutas_version", [version] if version else [])])
        return modelo_rutas.necesita_recalculo(cursor, horas_minimas=2)

    def version(self, hasta, metodo=None):
        from machine_learning import modelo_rutas
        return {"creado_en": datetime(2026, 10, 1), "hasta": hasta,
                "metricas_json": json.dumps({"parametros": {"metodo": metodo or modelo_rutas.METODO}})}

    def test_relearns_with_each_new_closed_day(self):
        self.assertTrue(self.necesita(None))
        self.assertFalse(self.necesita(self.version(datetime(2026, 10, 19).date())))   # ya aprendió el último día
        self.assertTrue(self.necesita(self.version(datetime(2026, 10, 18).date())))    # se cerró un día nuevo
        # Cambió el método (por ejemplo, al actualizar Artisa): se reaprende sin esperar.
        self.assertTrue(self.necesita(self.version(datetime(2026, 10, 19).date(), metodo="antiguo")))


if __name__ == "__main__":
    unittest.main()
