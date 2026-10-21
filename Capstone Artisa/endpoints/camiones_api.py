"""Camiones: la flota en vivo y la ficha de cada camión, día por día.

Todo sale de datos ya guardados, sin cálculos nuevos de Machine:
- última posición: telemetría en vivo o histórico (rutas_api.latest_positions);
- resumen del día: reporte_diario (cierre diario, 00:00 a 00:00 hora de Chile);
- viajes: viajes_camion. Machine define un viaje como el recorrido entre dos paradas, así
  que las paradas del día son los intervalos entre viajes (no se vuelven a detectar aquí);
- recorrido completo del día: los reportes GPS, sin unir saltos (rutas_api.truck_journey).
"""

from datetime import datetime, timedelta, timezone
import json

import pymysql

from common import dias
from endpoints import rutas_api

RECIENTE_MIN = 15        # un dato más viejo es "sin señal reciente" (igual que la telemetría)
EN_RUTA_KMH = 5          # sobre esto el camión va en ruta; si no, está detenido
SERIE_DIAS = 14          # km por día en la tarjeta de cada camión
HISTORIAL_DIAS = 30      # días que se muestran en la ficha: siempre los 30 que terminan hoy
PARADA_MIN = 10          # igual que machine_learning/modelo_rutas.py (PARADA_MIN_MIN)


def _local(value):
    return rutas_api._local(value) if value else None


def _catalogo(cursor):
    cursor.execute(
        """SELECT v.asset_id FROM vehiculos AS v INNER JOIN tipos_vehiculo AS t ON t.id_tipo = v.id_tipo
           WHERE t.nombre = 'camion' ORDER BY v.asset_id"""
    )
    return [fila["asset_id"] for fila in cursor.fetchall()]


def _rutas_por_camion(cursor):
    """Última ruta clasificada de cada camión (si hay clasificación activa)."""
    try:
        version = rutas_api.active_version(cursor)
        if not version:
            return {}
        cursor.execute(
            """SELECT v.asset_id, r.route_id, r.label, r.usage_level FROM rutas_vehiculo AS v
               INNER JOIN rutas_clasificadas AS r ON r.version_id = v.version_id AND r.route_id = v.route_id
               WHERE v.version_id = %s""",
            (version["id"],),
        )
    except pymysql.err.ProgrammingError:
        return {}
    return {f["asset_id"]: {"route_id": f["route_id"], "label": f["label"], "usage_level": f["usage_level"]}
            for f in cursor.fetchall()}


def estado(edad_minutos, velocidad):
    if edad_minutos is None:
        return "sin_datos"
    if edad_minutos > RECIENTE_MIN:
        return "sin_senal"
    return "en_ruta" if (velocidad or 0) > EN_RUTA_KMH else "detenido"


def fleet(cursor, ahora=None):
    """Todos los camiones del catálogo con su estado, lo de hoy y sus km de los últimos 14 días."""
    ahora = ahora or datetime.now(timezone.utc).replace(tzinfo=None)
    hoy = dias.hoy(ahora)
    camiones = _catalogo(cursor)
    rutas = _rutas_por_camion(cursor)
    posiciones = {v["vehicle_id"]: v for v in rutas_api.latest_positions(cursor, {})}
    desde = hoy - timedelta(days=SERIE_DIAS - 1)
    try:
        cursor.execute(
            """SELECT fecha, asset_id, asset_name, km_observados, viajes, minutos_detenido,
                      primer_reporte, ultimo_reporte
               FROM reporte_diario WHERE fecha >= %s AND fecha <= %s""",
            (desde, hoy),
        )
        diarios = cursor.fetchall()
    except pymysql.err.ProgrammingError:
        diarios = []
    por_camion = {}
    for fila in diarios:
        por_camion.setdefault(fila["asset_id"], {})[fila["fecha"]] = fila
    lista, resumen = [], {"total": 0, "en_ruta": 0, "detenido": 0, "sin_senal": 0, "sin_datos": 0,
                          "km_hoy": 0.0, "viajes_hoy": 0}
    for asset_id in camiones:
        pos = posiciones.get(asset_id)
        dias_camion = por_camion.get(asset_id, {})
        hoy_fila = dias_camion.get(hoy)
        edad = None
        if pos:
            momento = datetime.fromisoformat(pos["timestamp"].rstrip("Z"))
            edad = max(0, round((ahora - momento).total_seconds() / 60, 1))
        est = estado(edad, pos and pos["speed_kmh"])
        nombre = (pos and pos["vehicle_name"]) or next(
            (f["asset_name"] for f in dias_camion.values() if f.get("asset_name")), None) or asset_id
        conductor = " ".join(x for x in ((pos or {}).get("driver_name"), (pos or {}).get("driver_last_name")) if x)
        lista.append({
            "asset_id": asset_id, "nombre": nombre, "estado": est, "edad_minutos": edad,
            "ultimo_dato": _local(momento) if pos else None,
            "lat": pos and pos["latitude"], "lon": pos and pos["longitude"],
            "velocidad": pos and pos["speed_kmh"], "fuente": pos and pos["fuente"],
            "conductor": conductor or None, "ruta": rutas.get(asset_id),
            "hoy": None if not hoy_fila else {
                "km": round(hoy_fila["km_observados"], 1), "viajes": hoy_fila["viajes"],
                "minutos_detenido": round(hoy_fila["minutos_detenido"] or 0),
                "primer": _local(hoy_fila["primer_reporte"]), "ultimo": _local(hoy_fila["ultimo_reporte"])},
            "serie": [{"fecha": (desde + timedelta(days=k)).isoformat(),
                       "km": round(dias_camion[desde + timedelta(days=k)]["km_observados"], 1)
                       if desde + timedelta(days=k) in dias_camion else 0}
                      for k in range(SERIE_DIAS)],
        })
        resumen["total"] += 1
        resumen[est] += 1
        if hoy_fila:
            resumen["km_hoy"] += hoy_fila["km_observados"]
            resumen["viajes_hoy"] += hoy_fila["viajes"]
    resumen["km_hoy"] = round(resumen["km_hoy"], 1)
    return {"status": "ok", "generado_en": rutas_api._iso(ahora), "hoy": hoy.isoformat(),
            "reciente_minutos": RECIENTE_MIN, "en_ruta_kmh": EN_RUTA_KMH,
            "resumen": resumen, "camiones": lista}


def _lugar(cursor, asset_id, lat, lon, desde, hasta):
    """Nombre del lugar de una parada: la faena que la contiene o, si no, la dirección
    que informó el GPS mientras estaba ahí. None si no se sabe."""
    if lat is None or lon is None:
        return None
    try:
        cursor.execute("SELECT nombre FROM faenas WHERE ST_Contains(geocerca, ST_GeomFromText(%s, 4326)) LIMIT 1",
                       (f"POINT({lon} {lat})",))
        fila = cursor.fetchone()
        if fila:
            return fila["nombre"]
        cursor.execute(
            """SELECT location FROM telemetria_api WHERE asset_id=%s AND report_date >= %s AND report_date <= %s
               AND location IS NOT NULL AND location <> '' ORDER BY report_date LIMIT 1""",
            (asset_id, desde, hasta),
        )
        fila = cursor.fetchone()
    except pymysql.err.ProgrammingError:
        return None
    return fila["location"] if fila else None


def linea_de_tiempo(viajes, primer, ultimo):
    """Paradas e intervalos sin datos entre viajes, en orden. Fechas UTC naive.

    Entre dos viajes seguidos el camión estuvo detenido en el destino del primero (así
    corta Machine los viajes). Si ese destino se desconoce (corte de señal), el intervalo
    queda como "sin_datos", sin inventar dónde estuvo."""
    eventos = []
    if not viajes:
        return eventos
    limite = timedelta(minutes=PARADA_MIN)
    primero = viajes[0]
    if primero["origen"] and primer and primero["inicio"] - primer >= limite:
        eventos.append({"tipo": "parada", "desde": primer, "hasta": primero["inicio"], "lugar": primero["origen"]})
    for anterior, siguiente in zip(viajes, viajes[1:]):
        if siguiente["inicio"] <= anterior["fin"]:
            continue
        tipo = "parada" if anterior["destino"] else "sin_datos"
        eventos.append({"tipo": tipo, "desde": anterior["fin"], "hasta": siguiente["inicio"], "lugar": anterior["destino"]})
    ultimo_viaje = viajes[-1]
    if ultimo_viaje["destino"] and ultimo and ultimo - ultimo_viaje["fin"] >= limite:
        eventos.append({"tipo": "parada", "desde": ultimo_viaje["fin"], "hasta": ultimo, "lugar": ultimo_viaje["destino"]})
    return eventos


def _punto(lat, lon):
    return None if lat is None or lon is None else (lat, lon)


def truck_detail(cursor, asset_id, dia):
    """Ficha del camión para un día: resumen, viajes, paradas, recorrido y últimos 30 días."""
    if asset_id not in _catalogo(cursor):
        return None
    try:
        cursor.execute("SELECT estado FROM reporte_diario_estado WHERE fecha=%s", (dia,))
        estado_dia = (cursor.fetchone() or {}).get("estado")
        # La ventana de 30 días no se mueve al elegir otro día: termina siempre hoy.
        hoy = dias.hoy()
        cursor.execute(
            """SELECT fecha, asset_name, reportes, primer_reporte, ultimo_reporte, km_observados, viajes,
                      paradas, minutos_detenido
               FROM reporte_diario WHERE fecha >= %s AND fecha <= %s AND asset_id = %s ORDER BY fecha""",
            (hoy - timedelta(days=HISTORIAL_DIAS - 1), hoy, asset_id),
        )
        historial = cursor.fetchall()
        del_dia = next((f for f in historial if f["fecha"] == dia), None)
        if del_dia is None and dia < hoy - timedelta(days=HISTORIAL_DIAS - 1):
            cursor.execute(
                """SELECT fecha, asset_name, reportes, primer_reporte, ultimo_reporte, km_observados, viajes,
                          paradas, minutos_detenido
                   FROM reporte_diario WHERE fecha = %s AND asset_id = %s""",
                (dia, asset_id),
            )
            del_dia = cursor.fetchone()
        cursor.execute(
            """SELECT inicio, fin, origen_lat, origen_lon, destino_lat, destino_lon, puntos, km, con_saltos,
                      abierto_inicio, abierto_fin, geometria
               FROM viajes_camion WHERE fecha=%s AND asset_id=%s ORDER BY inicio""",
            (dia, asset_id),
        )
        filas_viajes = cursor.fetchall()
    except pymysql.err.ProgrammingError:
        estado_dia, historial, filas_viajes, del_dia = None, [], [], None
    viajes = [{"inicio": f["inicio"], "fin": f["fin"], "origen": _punto(f["origen_lat"], f["origen_lon"]),
               "destino": _punto(f["destino_lat"], f["destino_lon"]), "fila": f} for f in filas_viajes]
    eventos = linea_de_tiempo(viajes, del_dia and del_dia["primer_reporte"], del_dia and del_dia["ultimo_reporte"])
    for evento in eventos:
        lugar = evento["lugar"]
        evento["nombre"] = lugar and _lugar(cursor, asset_id, lugar[0], lugar[1], evento["desde"], evento["hasta"])
    recorrido = rutas_api.truck_journey(cursor, asset_id, dia)
    salida_viajes = []
    for numero, v in enumerate(viajes, start=1):
        f = v["fila"]
        salida_viajes.append({
            "numero": numero, "inicio": _local(f["inicio"]), "fin": _local(f["fin"]),
            "minutos": round((f["fin"] - f["inicio"]).total_seconds() / 60),
            "km": round(f["km"], 1), "puntos": f["puntos"], "con_saltos": bool(f["con_saltos"]),
            "abierto_inicio": bool(f["abierto_inicio"]), "abierto_fin": bool(f["abierto_fin"]),
            "origen": v["origen"], "destino": v["destino"],
            "lineas": [[[lat, lon] for lon, lat in tramo] for tramo in json.loads(f["geometria"] or "[]")],
        })
    return {
        "asset_id": asset_id, "fecha": dia.isoformat(), "hoy": dias.hoy().isoformat(), "zona": dias.ZONA,
        "estado_dia": estado_dia,
        "nombre": (del_dia and del_dia["asset_name"]) or recorrido.get("asset_name") or asset_id,
        "conductor": " ".join(x for x in (recorrido.get("driver_name"), recorrido.get("driver_last_name")) if x) or None,
        "resumen": None if not del_dia else {
            "reportes": del_dia["reportes"], "km": round(del_dia["km_observados"], 1), "viajes": del_dia["viajes"],
            "paradas": del_dia["paradas"], "minutos_detenido": round(del_dia["minutos_detenido"] or 0),
            "primer": _local(del_dia["primer_reporte"]), "ultimo": _local(del_dia["ultimo_reporte"]),
            "minutos_activo": round((del_dia["ultimo_reporte"] - del_dia["primer_reporte"]).total_seconds() / 60),
        },
        "viajes": salida_viajes,
        "eventos": [{"tipo": e["tipo"], "desde": _local(e["desde"]), "hasta": _local(e["hasta"]),
                     "minutos": round((e["hasta"] - e["desde"]).total_seconds() / 60),
                     "lugar": e["lugar"], "nombre": e["nombre"]} for e in eventos],
        "recorrido": {
            "lineas": [[[lat, lon] for lon, lat in linea] for linea in recorrido["geometry"]["coordinates"]],
            "km": recorrido["distancia_km"], "lecturas": recorrido["lecturas"], "completo": recorrido["completo"],
            "cortes": recorrido["cortes"], "minutos_sin_senal": recorrido["minutos_sin_senal"],
            "inicio": recorrido["inicio"], "fin": recorrido["termino_observado"],
        },
        "historial": [{"fecha": f["fecha"].isoformat(), "km": round(f["km_observados"], 1), "viajes": f["viajes"],
                       "minutos_detenido": round(f["minutos_detenido"] or 0)} for f in historial],
    }
