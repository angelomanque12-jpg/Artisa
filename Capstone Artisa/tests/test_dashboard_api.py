"""Dashboard de flota: reportes por patente y mes desde el primer cierre."""

from datetime import date
import unittest

from endpoints import dashboard_api
from tests.test_rutas_api import FakeCursor

HOY = date(2026, 10, 15)
ESTADO = [
    {"mes": "2025-10", "dias": 31, "cerrados": 31, "reportes": 4000, "descartados": 20,
     "desde": date(2025, 10, 1), "hasta": date(2025, 10, 31)},
    {"mes": "2026-04", "dias": 20, "cerrados": 20, "reportes": 1000, "descartados": 7,
     "desde": date(2026, 4, 11), "hasta": date(2026, 4, 30)},
    {"mes": "2026-10", "dias": 15, "cerrados": 13, "reportes": 900, "descartados": 3,
     "desde": date(2026, 10, 1), "hasta": date(2026, 10, 15)},
]
CATALOGO = [{"asset_id": "AB1234"}, {"asset_id": "CD5678"}, {"asset_id": "EF9012"}]
MENSUAL = [
    {"mes": "2025-10", "asset_id": "AB1234", "asset_name": "906", "reportes": 2000, "km": 5000.0,
     "dias_activos": 31, "ultimo": date(2025, 10, 31)},
    {"mes": "2026-04", "asset_id": "AB1234", "asset_name": "906", "reportes": 600, "km": 1200.04,
     "dias_activos": 18, "ultimo": date(2026, 4, 30)},
    {"mes": "2026-10", "asset_id": "AB1234", "asset_name": "906", "reportes": 500, "km": 900.0,
     "dias_activos": 15, "ultimo": date(2026, 10, 15)},
    {"mes": "2026-10", "asset_id": "CD5678", "asset_name": "912", "reportes": 400, "km": 300.0,
     "dias_activos": 10, "ultimo": date(2026, 10, 10)},
    {"mes": "2026-10", "asset_id": "ZZ0000", "asset_name": "camioneta", "reportes": 99, "km": 1.0,
     "dias_activos": 1, "ultimo": date(2026, 10, 15)},
]


class FleetDashboardTests(unittest.TestCase):
    def cursor(self):
        return FakeCursor([
            ("FROM reporte_diario_estado", ESTADO),
            ("FROM vehiculos", CATALOGO),
            ("FROM reporte_diario GROUP BY", MENSUAL),
        ])

    def test_months_from_first_day_to_today(self):
        data = dashboard_api.fleet_dashboard(self.cursor(), HOY)
        self.assertEqual(data["meses"], ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"])
        self.assertEqual((data["desde"], data["mes_en_curso"]), ("2026-04-11", "2026-10"))

    def test_new_current_month_is_added_automatically(self):
        data = dashboard_api.fleet_dashboard(self.cursor(), date(2026, 11, 1))
        self.assertEqual(data["meses"][0], "2026-04")
        self.assertEqual(data["meses"][-1], "2026-11")

    def test_monthly_reports_per_plate_only_catalog_trucks(self):
        data = dashboard_api.fleet_dashboard(self.cursor(), HOY)
        a, c, e = data["camiones"]  # ordenados por total de reportes
        self.assertEqual((a["patente"], a["nombre"], a["total"]), ("AB1234", "906", 1100))
        self.assertEqual(a["reportes"], [600, 0, 0, 0, 0, 0, 500])
        self.assertEqual(a["km"][0], 1200.0)
        self.assertEqual((c["patente"], c["total"]), ("CD5678", 400))
        self.assertEqual((e["patente"], e["total"], e["ultimo_dia"]), ("EF9012", 0, None))
        self.assertNotIn("ZZ0000", [x["patente"] for x in data["camiones"]])  # no es camión
        octubre = data["flota"][-1]
        self.assertEqual((octubre["reportes"], octubre["camiones_activos"], octubre["dias_cerrados"]), (900, 2, 13))
        self.assertEqual(data["totales"]["reportes"], 1500)
        self.assertEqual(data["totales"]["descartados"], 10)

    def test_days_without_reports(self):
        data = dashboard_api.fleet_dashboard(self.cursor(), HOY)
        dias = {c["patente"]: c["dias_sin_reportes"] for c in data["camiones"]}
        self.assertEqual((dias["EF9012"], dias["CD5678"], dias["AB1234"]), (None, 5, 0))
        self.assertNotIn("sin_reportar", data)  # la columna "Última señal" ya lo muestra

    def test_pending_without_closed_days(self):
        self.assertIsNone(dashboard_api.fleet_dashboard(FakeCursor([("FROM reporte_diario_estado", [])]), HOY))

    def test_truck_days(self):
        cursor = FakeCursor([("FROM reporte_diario\n               WHERE asset_id", [
            {"fecha": date(2026, 4, 11), "reportes": 300, "km_observados": 120.04, "paradas": 4,
             "minutos_detenido": 95.4}])])
        self.assertEqual(dashboard_api.truck_days(cursor, "AB1234"),
                         {"patente": "AB1234", "dias": [["2026-04-11", 300, 120.0, 4, 95]]})


ESTADOS_DIA = [{"fecha": date(2026, 4, d), "estado": "cerrado", "reportes": r}
               for d, r in ((13, 1000), (14, 300), (15, 1100), (16, 900))] + [
    {"fecha": date(2026, 4, 17), "estado": "parcial", "reportes": 100}]  # hoy, en curso
CATALOGO_8 = [{"asset_id": f"T{k}"} for k in range(8)]
RANGO = [{"asset_id": f"T{k}", "asset_name": f"Camión {k}", "reportes": r, "km": r / 10, "dias": 3,
          "paradas": k, "detenido": 30.4 * k}
         for k, r in enumerate((800, 600, 400, 400, 200, 100, 50))]  # T7 no reportó
ULTIMOS = [{"asset_id": "T0", "asset_name": "Camión 0", "ultimo": date(2026, 4, 17)},
           {"asset_id": "T7", "asset_name": "Camión 7", "ultimo": date(2026, 4, 2)}]
RANGO.append({"asset_id": "XX", "asset_name": "camioneta", "reportes": 9999})


class TrucksPeriodTests(unittest.TestCase):
    def levels(self, desde=date(2026, 4, 13), hasta=date(2026, 4, 17)):
        cursor = FakeCursor([("FROM reporte_diario_estado", ESTADOS_DIA), ("FROM vehiculos", CATALOGO_8),
                             ("FROM reporte_diario WHERE fecha BETWEEN", RANGO),
                             ("FROM reporte_diario WHERE reportes > 0", ULTIMOS)])
        return dashboard_api.trucks_period(cursor, desde, hasta, date(2026, 4, 17))

    def test_every_figure_of_each_truck_together(self):
        t1 = {c["patente"]: c for c in self.levels()["camiones"]}["T1"]
        self.assertEqual((t1["reportes"], t1["km"], t1["dias"], t1["paradas"], t1["detenido"]), (600, 60.0, 3, 1, 30))
        t7 = {c["patente"]: c for c in self.levels()["camiones"]}["T7"]
        self.assertEqual((t7["nombre"], t7["ultimo_dia"], t7["dias_sin_reportes"]), ("Camión 7", "2026-04-02", 15))

    def test_quartiles_match_the_notebook(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy no está instalado")
        data = self.levels()
        reportes = [c["reportes"] for c in data["camiones"]]
        cortes = np.quantile(reportes, [0.25, 0.5, 0.75])
        esperado = np.array(list("DCBA"))[np.searchsorted(cortes, reportes, side="left")]
        self.assertEqual([c["nivel"] for c in data["camiones"]], list(esperado))
        self.assertEqual(list(data["cortes"].values()), [round(float(x), 2) for x in cortes])
        self.assertEqual(sum(data["conteo"].values()), 8)

    def test_only_catalog_trucks_and_silent_ones_are_d(self):
        data = self.levels()
        patentes = {c["patente"]: c for c in data["camiones"]}
        self.assertNotIn("XX", patentes)
        self.assertEqual((patentes["T7"]["reportes"], patentes["T7"]["nivel"]), (0, "D"))
        self.assertEqual(patentes["T0"]["nivel"], "A")
        self.assertTrue(data["incluye_hoy"])

    def test_failure_days_are_closed_days_below_half_the_median(self):
        self.assertEqual(self.levels()["dias_falla"], [{"fecha": "2026-04-14", "reportes": 300}])
        self.assertEqual(self.levels(date(2026, 4, 15), date(2026, 4, 16))["dias_falla"], [])

    def test_pending_without_closed_days(self):
        cursor = FakeCursor([("FROM reporte_diario_estado", [])])
        self.assertIsNone(dashboard_api.trucks_period(cursor, date(2026, 4, 1), date(2026, 4, 2)))


if __name__ == "__main__":
    unittest.main()
