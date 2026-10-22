"""Corte diario de 00:00 a 00:00 hora Chile: sin pérdidas en el corte ni con datos atrasados."""

from datetime import date, datetime, timedelta
import unittest
from unittest.mock import patch

from common import dias
from machine_learning import diario_worker
from machine_learning.diario import resumir


def punto(asset, t, lat, lon, rid=None, name="Lampa-826"):
    return {"asset_id": asset, "asset_name": name, "report_id": rid or f"{asset}-{t.isoformat()}",
            "report_date": t, "latitude": lat, "longitude": lon}


class DayBoundsTests(unittest.TestCase):
    def test_day_is_midnight_to_midnight_in_chile(self):
        inicio, fin = dias.limites_utc(date(2026, 10, 6))   # Chile en UTC-3 (horario de verano)
        self.assertEqual(inicio, datetime(2026, 10, 6, 3))
        self.assertEqual(fin, datetime(2026, 10, 7, 3))
        inicio, fin = dias.limites_utc(date(2026, 7, 1))    # invierno: UTC-4
        self.assertEqual(inicio, datetime(2026, 7, 1, 4))

    def test_dst_days_are_contiguous(self):
        # Cada día empieza exactamente donde termina el anterior: ningún reporte queda
        # fuera ni en dos días, incluso en los cambios de hora (días de 23 y 25 horas).
        dia = date(2026, 1, 1)
        while dia < date(2027, 1, 1):
            self.assertEqual(dias.limites_utc(dia)[1], dias.limites_utc(dia + timedelta(days=1))[0])
            dia += timedelta(days=1)

    def test_report_belongs_to_local_day(self):
        self.assertEqual(dias.dia_local(datetime(2026, 10, 7, 2, 59, 59)), date(2026, 10, 6))  # 23:59:59 Chile
        self.assertEqual(dias.dia_local(datetime(2026, 10, 7, 3, 0, 0)), date(2026, 10, 7))    # 00:00 Chile


class SummaryTests(unittest.TestCase):
    def test_same_rules_as_notebook(self):
        t0 = datetime(2026, 10, 6, 12)
        filas = []
        # 20 reportes cada minuto avanzando ~500 m (30 km/h): todo se une.
        for i in range(20):
            filas.append(punto("A", t0 + timedelta(minutes=i), -33.4 + i * 0.0045, -70.6))
        # Pausa de 45 min (nuevo viaje, no se une) y otro tramo.
        filas.append(punto("A", t0 + timedelta(minutes=64), -33.2, -70.6))
        filas.append(punto("A", t0 + timedelta(minutes=65), -33.1955, -70.6))
        # Salto imposible de GPS (100 km en 1 min): no suma km.
        filas.append(punto("A", t0 + timedelta(minutes=66), -32.3, -70.6))
        # Basura: 0,0, repetido de la ETL, mismo reporte en las dos tablas.
        filas.append(punto("A", t0 + timedelta(minutes=67), 0, 0))
        filas.append(dict(filas[0]))
        filas.append(dict(filas[1], report_id="otro-id-misma-hora"))
        resumenes, descartados = resumir(filas)
        self.assertEqual(descartados, 1)
        (a,) = resumenes
        self.assertEqual(a.reportes, 23)
        self.assertEqual(a.viajes, 0)  # se completa en procesar_dia con los viajes entre paradas
        self.assertEqual(a.tramos_unidos, 20)
        self.assertAlmostEqual(a.km_observados, 0.5 * 20, delta=0.6)
        self.assertEqual(a.primer_reporte, t0)

    def test_empty_day(self):
        self.assertEqual(resumir([]), ([], 0))


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        if "MAX(a.recibido_en)" in query:
            assert "t.nombre = 'camion'" in query  # solo los reportes de camiones reabren un día
            inicio, fin = params
            self.result = [{"recibido": self.db["recibidos"].get(dias.dia_local(inicio))}]
        elif "FROM vehiculos" in query:
            self.result = []  # sin catálogo: la limpieza de no camiones no hace nada
        elif "MIN(report_date)" in query:
            self.result = [{"primero": self.db["primero"]}]
        elif "FROM telemetria_api_estado" in query:
            self.result = [self.db["conector"]] if self.db["conector"] else []
        elif "SELECT fecha FROM reporte_diario_estado" in query:
            self.result = [{"fecha": k} for k, v in self.db["estados"].items()
                           if k < params[0] and (v["estado"] != "cerrado"
                           or v.get("version_calculo", diario_worker.VERSION_CALCULO) < params[1])]
        elif "FROM reporte_diario_estado" in query:
            self.result = [dict(v, fecha=k) for k, v in self.db["estados"].items()]
        else:
            raise AssertionError(query)

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass


class CloseCycleTests(unittest.TestCase):
    def setUp(self):
        self.db = {"primero": datetime(2026, 10, 4, 15), "estados": {}, "recibidos": {},
                   "conector": {"estado": "conectado", "ultimo_exito": None}}
        self.done = []

    def procesar(self, connection, dia, estado, recalculo=False):
        self.done.append((dia, estado, recalculo))
        self.db["estados"][dia] = {"estado": estado, "calculado_en": self.now,
                                   "ultimo_recibido": self.db["recibidos"].get(dia)}

    def run_at(self, now, api_ok_until=None):
        self.now = now
        if self.db["conector"]:
            self.db["conector"]["ultimo_exito"] = api_ok_until or now
        self.done = []
        diario_worker.ciclo(FakeConnection(self.db), now, procesar=self.procesar)
        return [(d.isoformat(), e) for d, e, _ in self.done]

    def test_closes_pending_days_and_keeps_today_open(self):
        # 6 de octubre 12:00 Chile (15:00 UTC); datos desde el 4.
        done = self.run_at(datetime(2026, 10, 6, 15))
        self.assertEqual(done, [("2026-10-05", "cerrado"), ("2026-10-04", "cerrado"), ("2026-10-06", "en_curso")])

    def test_backlog_prioritizes_recent_days(self):
        self.db["primero"] = datetime(2026, 4, 1, 15)
        done = self.run_at(datetime(2026, 10, 6, 15))
        self.assertEqual([day for day, _ in done[:3]], ["2026-10-05", "2026-10-04", "2026-10-03"])

    def test_syncing_backlog_does_not_recalculate_same_days_in_one_run(self):
        self.db["primero"] = datetime(2026, 10, 1, 15)
        self.db["conector"]["estado"] = "sincronizando"
        revisados, done = set(), []
        with patch.object(diario_worker, "HOY_CADA", timedelta(0)):
            for cycle in range(3):
                self.now = datetime(2026, 10, 6, 15) + timedelta(hours=cycle)
                done.extend(diario_worker.ciclo(FakeConnection(self.db), self.now,
                            procesar=self.procesar, revisados=revisados))
        self.assertEqual(len(done), 5)
        self.assertEqual(len({day for day, _ in done}), 5)
        self.assertTrue(all(state == "esperando_datos" for _, state in done))

    def test_closes_provisional_days_when_api_catches_up(self):
        self.now = datetime(2026, 10, 6, 15)
        self.db["estados"][date(2026, 10, 5)] = {"estado": "esperando_datos"}
        self.db["conector"] = {"estado": "sincronizando", "ultimo_exito": self.now}
        connection = FakeConnection(self.db)
        self.assertFalse(diario_worker.cierres_listos(connection, self.now))
        self.db["conector"]["estado"] = "conectado"
        self.assertTrue(diario_worker.cierres_listos(connection, self.now))
        self.db["estados"][date(2026, 10, 5)]["estado"] = "cerrado"
        self.assertFalse(diario_worker.cierres_listos(connection, self.now))

    def test_route_results_are_published_before_entire_backlog_finishes(self):
        with patch.object(diario_worker, "publicar_rutas") as publish:
            def batch(connection, now, **kwargs):
                if batch.first:
                    batch.first = False
                    return [(date(2026, 10, day), "cerrado") for day in (5, 4, 3)]
                self.assertTrue(publish.called)
                return []
            batch.first = True
            with patch.object(diario_worker, "ciclo", side_effect=batch):
                self.assertEqual(diario_worker.ejecucion_completa(FakeConnection(self.db),
                                 datetime(2026, 10, 6, 15)), 3)
            self.assertEqual([call.kwargs["final"] for call in publish.call_args_list], [False, True])

    def test_midnight_waits_grace_and_api_confirmation(self):
        self.run_at(datetime(2026, 10, 6, 15))
        # 00:05 del 7 (03:05 UTC): aún en el margen de gracia -> el 6 no se cierra.
        done = self.run_at(datetime(2026, 10, 7, 3, 5))
        self.assertNotIn(("2026-10-06", "cerrado"), done)
        # 00:20, pero la API está caída desde las 23:50 del 6: queda esperando datos.
        done = self.run_at(datetime(2026, 10, 7, 3, 20), api_ok_until=datetime(2026, 10, 7, 2, 50))
        self.assertIn(("2026-10-06", "esperando_datos"), done)
        # Vuelve la API a las 02:00: recupera los reportes y el día se cierra.
        done = self.run_at(datetime(2026, 10, 7, 5, 0))
        self.assertIn(("2026-10-06", "cerrado"), done)
        self.assertIn(("2026-10-07", "en_curso"), done)

    def test_late_reports_reopen_calculation(self):
        self.db["recibidos"][date(2026, 10, 5)] = datetime(2026, 10, 6, 3, 10)
        self.run_at(datetime(2026, 10, 6, 15))
        self.assertEqual(self.run_at(datetime(2026, 10, 6, 15, 1)), [])
        # Un GPS envía tarde un reporte del día 5: se recalcula ese día, sigue cerrado.
        self.db["recibidos"][date(2026, 10, 5)] = datetime(2026, 10, 6, 15, 30)
        done = self.run_at(datetime(2026, 10, 6, 15, 31))
        self.assertIn(("2026-10-05", "cerrado"), done)
        self.assertTrue(any(r for d, e, r in self.done if d == date(2026, 10, 5)))

    def test_without_company_api_only_waits_for_midnight_grace(self):
        self.db["conector"] = None
        done = self.run_at(datetime(2026, 10, 7, 3, 16))
        self.assertIn(("2026-10-06", "cerrado"), done)

    def test_nothing_to_do_without_data(self):
        self.db["primero"] = None
        self.assertEqual(self.run_at(datetime(2026, 10, 6, 15)), [])


class ScheduleTests(unittest.TestCase):
    def test_runs_once_after_scheduled_hour_chile(self):
        from unittest.mock import patch
        with patch.object(diario_worker, "HORA_PROGRAMADA", "00:30"):
            # 00:20 Chile (03:20 UTC): todavía no
            self.assertFalse(diario_worker.toca_programado(datetime(2026, 10, 7, 3, 20), None))
            # 00:31 Chile: sí
            self.assertTrue(diario_worker.toca_programado(datetime(2026, 10, 7, 3, 31), None))
            # ya corrió hoy: no se repite
            self.assertFalse(diario_worker.toca_programado(datetime(2026, 10, 7, 15, 0), date(2026, 10, 7)))
            # al día siguiente vuelve a tocar
            self.assertTrue(diario_worker.toca_programado(datetime(2026, 10, 8, 3, 31), date(2026, 10, 7)))
        with patch.object(diario_worker, "HORA_PROGRAMADA", ""):
            self.assertTrue(diario_worker.toca_programado(datetime(2026, 10, 7, 15, 0), date(2026, 10, 7)))

    def test_light_cycle_only_updates_today(self):
        db = {"primero": datetime(2026, 10, 4, 15), "estados": {}, "recibidos": {},
              "conector": {"estado": "conectado", "ultimo_exito": datetime(2026, 10, 6, 15)}}
        hecho = []
        diario_worker.ciclo(FakeConnection(db), datetime(2026, 10, 6, 15),
                            procesar=lambda c, d, e, recalculo=False: hecho.append((d, e)), completo=False)
        self.assertEqual(hecho, [(date(2026, 10, 6), "en_curso")])


class AlertCycleTests(unittest.TestCase):
    def test_live_alerts_use_a_connection_independent_from_daily_close(self):
        class Context:
            def __enter__(self): return "conexion-alertas"
            def __exit__(self, *exc): return False

        with patch.object(diario_worker, "connect_db", return_value=Context()) as connect, \
             patch.object(diario_worker, "publicar_alertas") as publish:
            diario_worker.ciclo_alertas()

        connect.assert_called_once_with(autocommit=False, read_timeout=600)
        self.assertEqual(publish.call_args.args[0], "conexion-alertas")


class ReclassificationTests(unittest.TestCase):
    def test_vehicle_no_longer_a_truck_is_removed_from_closed_days(self):
        from machine_learning.diario import quitar_no_camiones
        ejecutadas = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def execute(self, query, params=None):
                ejecutadas.append((" ".join(query.split()), params))
                if "FROM vehiculos" in query:
                    self.result = [{"asset_id": "RJXS30"}, {"asset_id": "RRSP47"}]
                elif "SELECT DISTINCT fecha" in query:
                    self.result = [{"fecha": date(2026, 10, 2)}, {"fecha": date(2026, 10, 1)}]
                else:
                    self.result = []
            def fetchall(self): return self.result

        class Conn:
            def cursor(self): return Cursor()
            def commit(self): pass

        fechas = quitar_no_camiones(Conn())
        self.assertEqual(fechas, [date(2026, 10, 1), date(2026, 10, 2)])
        borrados = [p for q, p in ejecutadas if q.startswith("DELETE FROM reporte_diario")]
        self.assertEqual(borrados[0], [date(2026, 10, 1), "RJXS30", "RRSP47"])  # se conserva solo a camiones
        self.assertEqual(sum(q.startswith("DELETE FROM viajes_camion") for q, _ in ejecutadas), 2)
        totales = [q for q, _ in ejecutadas if q.startswith("UPDATE reporte_diario_estado")]
        self.assertEqual(len(totales), 2)
        self.assertIn("calculado_en=UTC_TIMESTAMP(6)", totales[0])  # rutas y predicción se recalculan


class LockWaitTests(unittest.TestCase):
    def test_waits_for_the_lock_in_short_steps(self):
        esperas = []

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, args):
                if "GET_LOCK" in sql:
                    esperas.append(args[1])
            def fetchone(self):
                return {"ok": 1 if len(esperas) >= 3 else 0}

        class Conexion:
            def cursor(self): return Cursor()

        resultado = diario_worker._con_bloqueo(Conexion(), lambda: "hecho", espera=3600)
        self.assertEqual(resultado, "hecho")
        self.assertEqual(len(esperas), 3)
        self.assertTrue(all(e <= 30 for e in esperas))  # nunca supera el límite de lectura


if __name__ == "__main__":
    unittest.main()
