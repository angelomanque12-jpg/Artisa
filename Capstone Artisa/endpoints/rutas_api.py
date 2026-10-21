"""Respuesta de /api/mapa, y el recorrido GPS de un camión en un día.

Leen la versión activa del modelo de rutas (machine_learning/modelo_rutas.py).
Si todavía no hay una, devuelven None y la API mantiene la respuesta "pendiente".
"""

from datetime import datetime, timedelta, timezone
import json
import math

import pymysql

from common import dias

REFRESH_SECONDS = 60
# Niveles de las rutas aprendidas (machine_learning/modelo_rutas.py), de la más concurrida a la menos.
# El nivel 6 no es una ruta: es un camión que anda por un camino que el modelo no conoce.
NIVELES = {
    "N1": ("Troncal", "#14532d"),
    "N2": ("Muy usada", "#3f9d4a"),
    "N3": ("Uso medio", "#d9a021"),
    "N4": ("Poco usada", "#ef7d22"),
    "N5": ("Mínima: 1 o 2 camiones", "#d7301f"),
    "N6": ("Fuera de ruta", "#7b3fa0"),
}
JOURNEY_LIMIT = 20000
MAX_GAP_MIN = 5
VEL_MAX_KMH = 150


def active_version(cursor):
    try:
        cursor.execute("SELECT * FROM rutas_version WHERE activa=1 ORDER BY id DESC LIMIT 1")
    except pymysql.err.ProgrammingError:
        return None  # tablas aún no creadas
    version = cursor.fetchone()
    if version:
        version["metricas"] = json.loads(version.pop("metricas_json") or "{}")
        version["lugares"] = json.loads(version.pop("lugares_json") or "{}")
    return version


def available_reports(cursor):
    total = 0
    for query in ("SELECT validos AS n FROM raw_gps_estado WHERE id=1",
                  "SELECT validos AS n FROM telemetria_api_estado WHERE id=1"):
        try:
            cursor.execute(query)
            total += int((cursor.fetchone() or {}).get("n") or 0)
        except pymysql.err.ProgrammingError:
            pass
    return total


def _iso(value):
    return value.isoformat() + "Z" if isinstance(value, datetime) else value


DRIVER_LOOKBACK = timedelta(hours=24)


def last_drivers(cursor, positions):
    """Último conductor informado en las 24 h previas, para varios camiones a la vez."""
    missing = {asset_id: row for asset_id, row in positions.items() if not row.get("driver_name")}
    found = {}
    if not missing:
        return found
    conditions = " OR ".join(
        "(source.asset_id=%s AND source.report_date >= %s AND source.report_date <= %s)"
        for _ in missing
    )
    params = []
    for asset_id, row in missing.items():
        params.extend((asset_id, row["report_date"] - DRIVER_LOOKBACK, row["report_date"]))
    for table, index in (("telemetria_api", "idx_telemetria_api_asset_fecha"),
                         ("raw_gps", "idx_raw_gps_asset_fecha")):
        try:
            cursor.execute(
                f"""SELECT asset_id, driver_name, driver_last_name, report_date
                    FROM (
                        SELECT source.asset_id, source.driver_name, source.driver_last_name,
                               source.report_date,
                               ROW_NUMBER() OVER (
                                   PARTITION BY source.asset_id ORDER BY source.report_date DESC
                               ) AS position
                        FROM {table} AS source FORCE INDEX ({index})
                        WHERE source.driver_name IS NOT NULL AND source.driver_name <> ''
                          AND ({conditions})
                    ) AS ranked
                    WHERE position=1""",
                params,
            )
        except pymysql.err.ProgrammingError:
            continue
        for row in cursor.fetchall():
            previous = found.get(row["asset_id"])
            if previous is None or row["report_date"] > previous["report_date"]:
                found[row["asset_id"]] = row
    return found


# Columnas propias de cada fuente: el histórico no trae rumbo ni encendido.
LATEST_SOURCES = (
    ("telemetria_api", "api", "gps.speed, gps.heading, gps.ignition, gps.event_name, "
                              "gps.driver_name, gps.driver_last_name"),
    ("raw_gps", "historico", "gps.speed, NULL AS heading, NULL AS ignition, gps.event_name, "
                             "gps.driver_name, gps.driver_last_name"),
)


def _latest_reports(cursor, table, extra):
    """Un solo reporte, el más reciente, por cada camión del catálogo en la tabla dada."""
    # La subconsulta correlacionada aprovecha el índice (asset_id, report_date) y elige un
    # solo report_id por camión. Agrupar únicamente por MAX(report_date) no basta: la fuente
    # puede entregar miles de reportes con la misma hora máxima para un mismo vehículo.
    try:
        cursor.execute(
            f"""SELECT gps.asset_id, gps.asset_name, gps.report_date, gps.latitude, gps.longitude,
                       {extra}
                FROM {table} AS gps
                INNER JOIN (
                    SELECT vehicle.asset_id,
                           (SELECT source.report_id
                            FROM {table} AS source
                            WHERE source.asset_id = vehicle.asset_id
                            ORDER BY source.report_date DESC
                            LIMIT 1) AS report_id
                    FROM vehiculos AS vehicle
                    INNER JOIN tipos_vehiculo AS vehicle_type
                      ON vehicle_type.id_tipo = vehicle.id_tipo
                    WHERE vehicle_type.nombre = 'camion'
                ) AS latest
                  ON latest.report_id = gps.report_id
                ORDER BY gps.asset_id"""
        )
    except pymysql.err.ProgrammingError:
        return []
    return cursor.fetchall()


def _vehicle(row, fuente, driver=None, route_id=None):
    driver = driver or row
    return {
        "vehicle_id": row["asset_id"], "vehicle_name": row["asset_name"] or row["asset_id"],
        "patente": row["asset_id"],
        "driver_name": driver.get("driver_name"), "driver_last_name": driver.get("driver_last_name"),
        "latitude": row["latitude"], "longitude": row["longitude"],
        "timestamp": _iso(row["report_date"]), "speed_kmh": row["speed"],
        "heading": row["heading"], "ignition": row["ignition"], "event": row["event_name"],
        "route_id": route_id, "fuente": fuente,
    }


def latest_positions(cursor, route_by_asset):
    """Última posición conocida de cada camión (API en vivo o histórico, la más reciente),
    con su patente (asset_id) y el último conductor informado."""
    cursor.execute(
        """SELECT v.asset_id FROM vehiculos AS v INNER JOIN tipos_vehiculo AS t ON t.id_tipo = v.id_tipo
           WHERE t.nombre = 'camion' ORDER BY v.asset_id"""
    )
    trucks = [row["asset_id"] for row in cursor.fetchall()]
    truck_ids = set(trucks)
    best_by_asset = {}
    for table, fuente, extra in LATEST_SOURCES:
        for row in _latest_reports(cursor, table, extra):
            asset_id = row["asset_id"]
            if asset_id not in truck_ids:
                continue
            best = best_by_asset.get(asset_id)
            if best is None or row["report_date"] > best["report_date"]:
                best_by_asset[asset_id] = dict(row, fuente=fuente)
    fallback_drivers = last_drivers(cursor, best_by_asset)
    vehicles = []
    for asset_id in trucks:
        best = best_by_asset.get(asset_id)
        if best:
            driver = best if best.get("driver_name") else fallback_drivers.get(asset_id, best)
            vehicles.append(_vehicle(best, best["fuente"], driver, route_by_asset.get(asset_id)))
    return vehicles


def live_positions(cursor):
    """Último reporte de cada camión desde la API de la empresa, sin geometrías ni rutas.

    Permite mover los marcadores cada minuto sin esperar la respuesta completa de /api/mapa;
    el navegador conserva la ruta y el conductor que ya conocía.
    """
    table, fuente, extra = LATEST_SOURCES[0]
    return [_vehicle(row, fuente) for row in _latest_reports(cursor, table, extra)]


def _route(row):
    label, color = NIVELES.get(row["usage_level"], (row["usage_level"], "#718785"))
    return {
        "route_id": row["route_id"], "label": row["label"], "usage_count": row["usage_count"],
        "unique_vehicle_count": row["unique_vehicle_count"], "usage_level": row["usage_level"],
        "usage_label": label, "color": color, "geometry_source": row["geometry_source"],
        "observed_points": row["observed_points"], "map_matching": False,
        "km_mediana": row["km_mediana"], "origen": row["origen"], "destino": row["destino"],
        "primer_viaje": _iso(row["primer_viaje"]), "ultimo_viaje": _iso(row["ultimo_viaje"]),
        "geometry": json.loads(row["geometria"]),
    }


def classified_map(cursor):
    version = active_version(cursor)
    if not version:
        return None
    cursor.execute("SELECT * FROM rutas_clasificadas WHERE version_id=%s ORDER BY unique_vehicle_count DESC, usage_count DESC, route_id",
                   (version["id"],))
    routes = [_route(row) for row in cursor.fetchall()]
    cursor.execute("SELECT asset_id, route_id FROM rutas_vehiculo WHERE version_id=%s", (version["id"],))
    route_by_asset = {row["asset_id"]: row["route_id"] for row in cursor.fetchall()}
    vehicles = latest_positions(cursor, route_by_asset)
    metrics = version["metricas"]
    available = available_reports(cursor)
    cortes = (metrics.get("umbrales") or {}).get("cortes") or []
    return {
        "schema_version": 1, "status": "ok", "source_mode": "HISTORICAL_GPS_CATALOG",
        "snapshot_date": version["hasta"].isoformat(),
        "routes": routes, "trips": [], "vehicles": vehicles, "readings": [],
        "summary": {
            "trip_count": metrics.get("viajes_clasificados", 0),
            "vehicle_count": metrics.get("camiones", 0),
            "route_count": len(routes),
            "available_report_count": available,
        },
        "ranking": {"most_used": routes[0] if routes else None,
                    "least_used": min(routes, key=lambda r: (r["usage_count"], r["route_id"])) if routes else None},
        "metadata": {
            "status": "ok",
            "version": version["id"],
            "window": {"desde": version["desde"].isoformat(), "hasta": version["hasta"].isoformat()},
            "processed_report_count": sum(r["observed_points"] for r in routes),
            "source_report_count": available,
            "refresh_seconds": REFRESH_SECONDS,
            "classification": (
                f"Rutas completas entre paradas, agrupadas por origen-destino y trayectoria, con los viajes del "
                f"{version['desde']:%d-%m-%Y} al {version['hasta']:%d-%m-%Y}. Nivel por camiones distintos: "
                + ", ".join(f"{c['nivel']} {c['desde']}–{c['hasta']}" for c in cortes) + "."
            ),
            "geometry": "Cada línea es un viaje real observado (el más representativo de su ruta).",
            "road_matching": "Sin ajuste a calles: se dibujan los puntos GPS reportados, sin unir saltos.",
            "data_retention": (f"{metrics.get('viajes_sin_origen_o_destino', 0)} viajes sin origen o destino "
                               f"conocido no se clasifican."),
            "metricas": metrics,
        },
    }


def _distance_m(a, b):
    return math.hypot(b[0] - a[0], (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))) * 111_000


def _local(value):
    return value.replace(tzinfo=timezone.utc).astimezone(dias.zona()).strftime("%Y-%m-%d %H:%M:%S")


def truck_journey(cursor, asset_id, day):
    """Recorrido observado del camión en un día (hora de Chile)."""
    start, end = dias.limites_utc(day)
    rows = []
    for table, extra in (("telemetria_api", "driver_name, driver_last_name"),
                         ("raw_gps", "driver_name, driver_last_name")):
        try:
            cursor.execute(
                f"""SELECT report_id, asset_name, report_date, latitude, longitude, {extra}
                    FROM {table} WHERE asset_id=%s AND report_date >= %s AND report_date < %s
                    ORDER BY report_date DESC LIMIT %s""",
                (asset_id, start, end, JOURNEY_LIMIT + 1),
            )
        except pymysql.err.ProgrammingError:
            continue
        rows.extend(cursor.fetchall())
    seen, points = set(), []
    for row in sorted(rows, key=lambda r: r["report_date"]):
        lat, lon = row["latitude"], row["longitude"]
        key = (row["report_date"], lat, lon)
        if lat is None or lon is None or (lat == 0 and lon == 0) or key in seen or row["report_id"] in seen:
            continue
        seen.update({key, row["report_id"]})
        points.append(row)
    complete = len(points) <= JOURNEY_LIMIT
    points = points[-JOURNEY_LIMIT:]
    paths, current, meters, moving_seconds = [], [], 0.0, 0.0
    gaps, gap_seconds = 0, 0.0  # cortes de señal: más de MAX_GAP_MIN sin reportes
    for prev, row in zip([None] + points[:-1], points):
        coords = [round(row["longitude"], 6), round(row["latitude"], 6)]
        if prev is not None:
            hours = (row["report_date"] - prev["report_date"]).total_seconds() / 3600
            if hours > MAX_GAP_MIN / 60:
                gaps += 1
                gap_seconds += hours * 3600
            d = _distance_m((prev["latitude"], prev["longitude"]), (row["latitude"], row["longitude"]))
            if 0 < hours <= MAX_GAP_MIN / 60 and d / 1000 <= VEL_MAX_KMH * hours:
                meters += d
                if d > 50:
                    moving_seconds += hours * 3600
                current.append(coords)
                continue
            if len(current) >= 2:
                paths.append(current)
        current = [coords]
    if len(current) >= 2:
        paths.append(current)
    driver = next((r for r in reversed(points) if r.get("driver_name")), {})
    return {
        "asset_id": asset_id, "patente": asset_id,
        "asset_name": next((r["asset_name"] for r in reversed(points) if r["asset_name"]), asset_id),
        "fecha": day.isoformat(),
        "driver_name": driver.get("driver_name"), "driver_last_name": driver.get("driver_last_name"),
        "inicio": _local(points[0]["report_date"]) if points else None,
        "termino_observado": _local(points[-1]["report_date"]) if points else None,
        "distancia_km": round(meters / 1000, 2),
        "tiempo_movimiento_minutos": round(moving_seconds / 60, 1),
        "lecturas": len(points), "completo": complete,
        "cortes": gaps, "minutos_sin_senal": round(gap_seconds / 60, 1),
        "nota": (f"Posiciones GPS del {day:%d-%m-%Y} (hora de Chile). Solo se unen reportes seguidos "
                 f"(hasta {MAX_GAP_MIN} min y {VEL_MAX_KMH} km/h); los saltos de GPS no se dibujan."),
        "geometry": {"type": "MultiLineString", "coordinates": paths},
    }
