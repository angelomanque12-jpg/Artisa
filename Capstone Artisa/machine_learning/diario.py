"""Cierre diario: el mismo trabajo del cuaderno de telemetría del equipo (Colab), día por día.

Fuente: telemetria_api (API de la empresa, en vivo) + raw_gps (histórico), solo
camiones según el catálogo. Mismas reglas que el cuaderno:
- gps_validos: coordenadas en rango y sin el 0,0.
- Se quitan repeticiones (mismo camión y misma hora) y reportes repetidos entre tablas.
- Dos reportes seguidos se unen como recorrido solo si la pausa es <= MAX_GAP_MIN y el
  salto no supera VEL_MAX_KMH (no se inventa recorrido ni se unen saltos de GPS).
- Viajes = recorridos entre paradas (Parte 1 de machine_learning/modelo_rutas.py). Una pausa larga no
  separa viajes: el tiempo detenido (p. ej. cargando) se informa aparte.

Corte: el día va de 00:00 a 00:00 hora Chile (common/dias.py). Un viaje que cruza
la medianoche aparece en ambos días con sus propios reportes; ningún reporte se
pierde ni se cuenta dos veces.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
import math
import time

from common import dias
from machine_learning.modelo_rutas import extraer_viajes, resumen_paradas


VERSION_CALCULO = 3  # 2: viajes entre paradas · 3: paradas sin cortar por pausas, tiempo detenido
MAX_GAP_MIN = 5
VEL_MAX_KMH = 150
GAP_VIAJE_MIN = 30
METROS_POR_GRADO = 111_000


def gps_valido(lat, lon):
    return (lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (lat == 0 and lon == 0))


def distancia_m(lat0, lon0, lat1, lon1):
    """Misma aproximación plana del cuaderno (suficiente entre reportes de 1 minuto)."""
    return math.hypot(lat1 - lat0, (lon1 - lon0) * math.cos(math.radians(lat1))) * METROS_POR_GRADO


@dataclass
class ResumenCamion:
    asset_id: str
    asset_name: str
    reportes: int
    primer_reporte: datetime
    ultimo_reporte: datetime
    km_observados: float
    viajes: int
    tramos_unidos: int
    paradas: int = 0
    minutos_detenido: float = 0.0


def limpiar(filas):
    """Puntos válidos por camión, sin repetidos, ordenados por hora.
    Devuelve ({asset_id: [(fecha, lat, lon, nombre), ...]}, descartados)."""
    vistos_id, vistos_hora = set(), set()
    por_camion = defaultdict(list)
    descartados = 0
    for fila in filas:
        lat, lon = fila.get("latitude"), fila.get("longitude")
        lat = None if lat is None else float(lat)
        lon = None if lon is None else float(lon)
        if not gps_valido(lat, lon) or fila.get("report_date") is None:
            descartados += 1
            continue
        clave_hora = (fila["asset_id"], fila["report_date"])
        if fila.get("report_id") in vistos_id or clave_hora in vistos_hora:
            continue  # el mismo reporte en ambas tablas o repetido por la ETL
        if fila.get("report_id"):
            vistos_id.add(fila["report_id"])
        vistos_hora.add(clave_hora)
        por_camion[fila["asset_id"]].append((fila["report_date"], lat, lon, fila.get("asset_name")))
    for puntos in por_camion.values():
        puntos.sort(key=lambda p: p[0])
    return por_camion, descartados


def resumir(filas, por_camion=None, descartados=0):
    """filas: dicts con asset_id, asset_name, report_id, report_date (UTC), latitude, longitude.
    Devuelve (lista de ResumenCamion, descartados)."""
    if por_camion is None:
        por_camion, descartados = limpiar(filas)
    resumenes = []
    for asset_id, puntos in por_camion.items():
        metros, unidos = 0.0, 0
        for (t0, la0, lo0, _), (t1, la1, lo1, _) in zip(puntos, puntos[1:]):
            horas = (t1 - t0).total_seconds() / 3600
            d = distancia_m(la0, lo0, la1, lo1)
            if 0 < horas <= MAX_GAP_MIN / 60 and d > 0 and d / 1000 <= VEL_MAX_KMH * horas:
                metros += d
                unidos += 1
        nombre = next((p[3] for p in reversed(puntos) if p[3]), None)
        resumenes.append(ResumenCamion(
            asset_id=asset_id, asset_name=nombre, reportes=len(puntos),
            primer_reporte=puntos[0][0], ultimo_reporte=puntos[-1][0],
            km_observados=round(metros / 1000, 2), viajes=0, tramos_unidos=unidos,
        ))
    resumenes.sort(key=lambda r: (-r.reportes, r.asset_id))
    return resumenes, descartados


def viajes_del_dia(por_camion, dia):
    """Viajes entre paradas de cada camión (Parte 1 de machine_learning/modelo_rutas.py)."""
    inicio, fin = dias.limites_utc(dia)
    resultado = []
    for asset_id, puntos in por_camion.items():
        resultado.extend(extraer_viajes(asset_id, [(p[0], p[1], p[2]) for p in puntos], inicio, fin))
    return resultado


# --- Base de datos -----------------------------------------------------------------

def camiones_catalogo(cursor):
    cursor.execute(
        """SELECT v.asset_id FROM vehiculos AS v
           INNER JOIN tipos_vehiculo AS t ON t.id_tipo = v.id_tipo
           WHERE t.nombre = 'camion' ORDER BY v.asset_id"""
    )
    return [fila["asset_id"] for fila in cursor.fetchall()]


def quitar_no_camiones(connection):
    """Regla de reclasificación: si un vehículo deja de ser camión en el catálogo (por
    ejemplo, una patente que era camioneta), se quita de los días ya cerrados y se
    rehacen los totales de esos días. Los días quedan marcados como recalculados, así las
    rutas y la predicción se actualizan solas. Devuelve los días corregidos."""
    with connection.cursor() as cursor:
        camiones = camiones_catalogo(cursor)
        if not camiones:
            return []
        marcas = ",".join(["%s"] * len(camiones))
        cursor.execute(
            f"""SELECT DISTINCT fecha FROM reporte_diario WHERE asset_id NOT IN ({marcas})
                UNION SELECT DISTINCT fecha FROM viajes_camion WHERE asset_id NOT IN ({marcas})""",
            camiones + camiones,
        )
        fechas = sorted(fila["fecha"] for fila in cursor.fetchall())
        for fecha in fechas:
            cursor.execute(f"DELETE FROM reporte_diario WHERE fecha=%s AND asset_id NOT IN ({marcas})",
                           [fecha] + camiones)
            cursor.execute(f"DELETE FROM viajes_camion WHERE fecha=%s AND asset_id NOT IN ({marcas})",
                           [fecha] + camiones)
            cursor.execute(
                """UPDATE reporte_diario_estado AS e
                   LEFT JOIN (SELECT COUNT(*) AS camiones, SUM(reportes) AS reportes,
                                     SUM(km_observados) AS km
                              FROM reporte_diario WHERE fecha=%s) AS r ON 1=1
                   SET e.camiones=COALESCE(r.camiones,0), e.reportes=COALESCE(r.reportes,0),
                       e.km_observados=ROUND(COALESCE(r.km,0),2), e.calculado_en=UTC_TIMESTAMP(6)
                   WHERE e.fecha=%s""",
                (fecha, fecha),
            )
    connection.commit()
    if fechas:
        print(f"Cierre diario: se quitaron vehículos que ya no son camiones de {len(fechas)} días", flush=True)
    return fechas


def leer_filas_dia(cursor, dia):
    """Reportes de camiones del día local, desde ambas tablas.

    Igual que el cuaderno: un SELECT por camión con el índice (asset_id, report_date).
    Una sola consulta por fecha sobre los 23 M de reportes de raw_gps es mucho más lenta.
    """
    inicio, fin = dias.limites_utc(dia)
    filas = []
    for asset_id in camiones_catalogo(cursor):
        for tabla, indice in (("raw_gps", "idx_raw_gps_asset_fecha"),
                              ("telemetria_api", "idx_telemetria_api_asset_fecha")):
            cursor.execute(
                f"""SELECT report_id, asset_id, asset_name, report_date, latitude, longitude
                   FROM {tabla} USE INDEX ({indice})
                   WHERE asset_id = %s AND report_date >= %s AND report_date < %s""",
                (asset_id, inicio, fin),
            )
            filas.extend(cursor.fetchall())
    return filas


def ultimo_recibido(cursor, dia):
    """Último momento en que llegó (desde la API) un reporte de un camión ese día.

    Solo cuentan los camiones del catálogo: los reportes atrasados de otros vehículos
    (camionetas, por ejemplo) no cambian el día y no deben forzar un recálculo.
    """
    inicio, fin = dias.limites_utc(dia)
    cursor.execute(
        """SELECT MAX(a.recibido_en) AS recibido
           FROM telemetria_api AS a
           INNER JOIN vehiculos AS v ON v.asset_id = a.asset_id
           INNER JOIN tipos_vehiculo AS t ON t.id_tipo = v.id_tipo AND t.nombre = 'camion'
           WHERE a.report_date >= %s AND a.report_date < %s""",
        (inicio, fin),
    )
    fila = cursor.fetchone() or {}
    return fila.get("recibido")


def _geometria_json(viaje):
    return json.dumps([[[round(lon, 6), round(lat, 6)] for lat, lon in tramo] for tramo in viaje.tramos],
                      separators=(",", ":"))


def guardar_dia(connection, dia, estado, resumenes, descartados, recibido, recalculo=False, viajes=()):
    """Reemplaza el resumen y los viajes del día en una sola transacción (datos derivados)."""
    inicio, fin = dias.limites_utc(dia)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM reporte_diario WHERE fecha=%s", (dia,))
        cursor.execute("DELETE FROM viajes_camion WHERE fecha=%s", (dia,))
        if viajes:
            cursor.executemany(
                """INSERT IGNORE INTO viajes_camion
                (fecha,asset_id,inicio,fin,origen_lat,origen_lon,destino_lat,destino_lon,puntos,km,
                 con_saltos,abierto_inicio,abierto_fin,geometria)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(dia, v.asset_id, v.inicio, v.fin,
                  v.origen[0] if v.origen else None, v.origen[1] if v.origen else None,
                  v.destino[0] if v.destino else None, v.destino[1] if v.destino else None,
                  v.puntos, v.km, int(v.con_saltos), int(v.abierto_inicio), int(v.abierto_fin),
                  _geometria_json(v)) for v in viajes],
            )
        if resumenes:
            cursor.executemany(
                """INSERT INTO reporte_diario
                (fecha,asset_id,asset_name,reportes,primer_reporte,ultimo_reporte,km_observados,viajes,tramos_unidos,
                 paradas,minutos_detenido)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(dia, r.asset_id, r.asset_name, r.reportes, r.primer_reporte, r.ultimo_reporte,
                  r.km_observados, r.viajes, r.tramos_unidos, r.paradas, r.minutos_detenido) for r in resumenes],
            )
        cursor.execute(
            """INSERT INTO reporte_diario_estado
            (fecha,zona,inicio_utc,fin_utc,estado,reportes,camiones,km_observados,descartados,
             ultimo_recibido,calculado_en,cerrado_en,recalculos,version_calculo)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),
                    CASE WHEN %s='cerrado' THEN UTC_TIMESTAMP(6) ELSE NULL END,0,%s)
            ON DUPLICATE KEY UPDATE zona=VALUES(zona),inicio_utc=VALUES(inicio_utc),fin_utc=VALUES(fin_utc),
            estado=VALUES(estado),reportes=VALUES(reportes),camiones=VALUES(camiones),
            km_observados=VALUES(km_observados),descartados=VALUES(descartados),
            ultimo_recibido=VALUES(ultimo_recibido),calculado_en=VALUES(calculado_en),
            cerrado_en=CASE WHEN VALUES(estado)='cerrado' THEN COALESCE(cerrado_en, UTC_TIMESTAMP(6)) ELSE NULL END,
            recalculos=recalculos+%s,version_calculo=VALUES(version_calculo)""",
            (dia, dias.ZONA, inicio, fin, estado, sum(r.reportes for r in resumenes), len(resumenes),
             round(sum(r.km_observados for r in resumenes), 2), descartados, recibido, estado,
             VERSION_CALCULO, 1 if recalculo else 0),
        )
    connection.commit()


def procesar_dia(connection, dia, estado, recalculo=False):
    comienzo = time.monotonic()
    with connection.cursor() as cursor:
        filas = leer_filas_dia(cursor, dia)
        recibido = ultimo_recibido(cursor, dia)
    por_camion, descartados = limpiar(filas)
    resumenes, descartados = resumir(None, por_camion, descartados)
    viajes = viajes_del_dia(por_camion, dia)
    # Viajes = recorridos entre paradas (no pausas de 30 min); paradas y tiempo detenido
    # sirven para revisar tiempos de carga.
    por_asset = defaultdict(int)
    for v in viajes:
        por_asset[v.asset_id] += 1
    for r in resumenes:
        r.viajes = por_asset.get(r.asset_id, 0)
        r.paradas, r.minutos_detenido = resumen_paradas([(p[0], p[1], p[2]) for p in por_camion[r.asset_id]])
    guardar_dia(connection, dia, estado, resumenes, descartados, recibido, recalculo, viajes)
    reportes = sum(r.reportes for r in resumenes)
    print(f"Cierre diario: {dia} -> {estado} · {len(resumenes)} camiones, {reportes} reportes, "
          f"{len(viajes)} viajes en {time.monotonic() - comienzo:.1f} s", flush=True)
    return len(resumenes), reportes
