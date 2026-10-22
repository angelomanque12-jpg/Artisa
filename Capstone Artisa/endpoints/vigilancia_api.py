"""Respuestas de /api/rutas-aprendidas, /api/vigilancia y /api/alertas.

- /api/rutas-aprendidas: las rutas de la versión activa del modelo
  (machine_learning/modelo_rutas.py) con su nivel N1–N5, la tabla de niveles y la red para
  dibujar (una sola línea por camino). Cambia con cada versión (una por día).
- /api/vigilancia: cada camión en vivo con el nivel de la ruta que pasa a menos de ~1 km
  (N6 si no hay ninguna) y las alertas abiertas y de hoy.
- /api/alertas?desde=&hasta=: el registro de alertas TERMINADAS por día, para el historial y
  la descarga (las que siguen abiertas están en /api/vigilancia).

Las alertas las detecta y guarda Machine cada minuto en la tabla alertas_ruta (Parte 8 del
modelo) y los cortes de señal en cortes_senal (Parte 9); cada alerta lleva los cortes que hubo
durante ella. Esta API solo los lee. Solo lectura.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os

import pymysql

from common import dias
from common import rutas_mapa as rm
from endpoints import rutas_api

ACTIVA_MIN = 30              # una alerta sin reportes nuevos en 30 min se muestra "sin señal"
MIN_MINUTOS = float(os.environ.get("VIGILANCIA_MIN_MINUTOS", "10"))
MIN_KM = float(os.environ.get("VIGILANCIA_MIN_KM", "2"))
MAX_ALERTAS = 5000           # tope de filas por consulta del historial
MAX_DIAS_HISTORIAL = 92

_CACHE = {"version": None, "modelo": None, "mapa": None}


def leer_modelo(cursor):
    """Versión activa del modelo de rutas, sus rutas y el índice por celdas (se guarda en
    memoria mientras la versión no cambie). None si todavía no hay ninguna."""
    version = rutas_api.active_version(cursor)
    if not version:
        return None, None
    if _CACHE["version"] == (version["id"], version["creado_en"]):
        return _CACHE["modelo"], _CACHE["mapa"]
    rutas = rm.leer_rutas(cursor, version["id"])
    metricas = version["metricas"]
    cortes = (metricas.get("umbrales") or {}).get("cortes") or []
    por_nivel = Counter(r["nivel"] for r in rutas)
    niveles = []
    for nivel, (nombre, color) in rutas_api.NIVELES.items():
        corte = next((c for c in cortes if c["nivel"] == nivel), None)
        if nivel != "N6" and corte is None:
            continue  # pocos datos: el modelo aprendió menos grupos
        niveles.append({"nivel": nivel, "nombre": nombre, "color": color,
                        "desde": corte and corte["desde"], "hasta": corte and corte["hasta"],
                        "rutas": por_nivel.get(nivel, 0),
                        "recorridos": sum(r["recorridos"] for r in rutas if r["nivel"] == nivel),
                        "alerta": rm.numero_nivel(nivel) > rm.HABITUAL})
    parametros = metricas.get("parametros") or {}
    cada = int(os.environ.get("RUTAS_CADA_DIAS", "1"))
    horizonte = int(os.environ.get("RUTAS_HORIZONTE_DIAS", "7"))
    modelo = {
        "version": version["id"], "creado_en": rm.iso(version["creado_en"]),
        "datos_desde": version["desde"].isoformat(), "datos_hasta": version["hasta"].isoformat(),
        "proxima_con_datos_hasta": (version["hasta"] + timedelta(days=cada)).isoformat(),
        "cada_dias": cada,
        # Previsión: desde el día siguiente al último dato aprendido, por `horizonte` días.
        "prevision_desde": (version["hasta"] + timedelta(days=1)).isoformat(),
        "prevision_hasta": (version["hasta"] + timedelta(days=horizonte)).isoformat(),
        "viajes": metricas.get("viajes_clasificados", 0), "camiones": metricas.get("camiones", 0),
        "lugares": metricas.get("lugares", 0), "total_rutas": len(rutas),
        "parametros": parametros, "niveles": niveles,
    }
    mapa = rm.MapaRutas(rutas)
    _CACHE.update(version=(version["id"], version["creado_en"]), modelo=dict(modelo, rutas=rutas, red=mapa.red(rutas, MIN_KM)),
                  mapa=mapa)
    return _CACHE["modelo"], mapa


def rutas_aprendidas(cursor):
    """Las rutas del modelo con su nivel y línea (cambia con cada versión del modelo). None: pendiente."""
    modelo, _ = leer_modelo(cursor)
    return None if modelo is None else dict(modelo, status="ok")


# --- Alertas guardadas ---------------------------------------------------------------------

def _alerta(fila, ahora):
    """Fila de alertas_ruta en el formato de la web. Si Machine dejó de revisar, una alerta
    "activa" sin datos nuevos por ACTIVA_MIN se muestra como "sin señal"."""
    estado = fila["estado"]
    if estado == "activa" and (ahora - fila["fin"]).total_seconds() > ACTIVA_MIN * 60:
        estado = "sin_senal"
    estela = json.loads(fila["estela"])
    if estela and not isinstance(estela[0][0], list):
        estela = [estela]  # alertas guardadas antes de cortar el camino en tramos
    corredor = None
    if fila["corredor_lat"] is not None:
        corredor = {"km": fila["corredor_km"], "punto": [fila["corredor_lat"], fila["corredor_lon"]]}
    return {
        "id": fila["id"], "patente": fila["asset_id"], "nombre": fila["asset_name"],
        "conductor": fila["conductor"], "ubicacion": fila["ubicacion"], "estado": estado,
        "inicio": rm.iso(fila["inicio"]), "fin": rm.iso(fila["fin"]), "minutos": fila["minutos"],
        "reportes": fila["reportes"], "km_recorridos": fila["km_recorridos"],
        "km_al_corredor": fila["km_ruta_habitual"], "nivel": fila["nivel"], "ruta": fila["route_id"],
        "ruta_label": fila["ruta_label"], "posicion": [fila["latitud"], fila["longitud"]],
        "velocidad": fila["velocidad"], "estela": estela, "corredor": corredor,
        "version_modelo": fila["version_modelo"],
    }


def _corte(fila, ahora):
    hasta = fila["hasta"]
    return {
        "desde": rm.iso(fila["desde"]), "hasta": rm.iso(hasta) if hasta else None,
        "minutos": fila["minutos"] if hasta else round((ahora - fila["desde"]).total_seconds() / 60),
        "tipo": fila["tipo"], "antes": [fila["lat_antes"], fila["lon_antes"]],
        "despues": [fila["lat_despues"], fila["lon_despues"]] if fila["lat_despues"] is not None else None,
        "velocidad_antes": fila["velocidad_antes"], "encendido_antes": fila["encendido_antes"],
        "evento_antes": fila["evento_antes"], "km_linea": fila["km_linea"], "kmh_necesaria": fila["kmh_necesaria"],
        "copias": fila["copias"], "ultima_copia": rm.iso(fila["ultima_copia"]) if fila["ultima_copia"] else None,
    }


def _con_cortes(cursor, filas, ahora):
    """Cortes de señal de cada alerta: los que empezaron entre su inicio y su fin (incluidos)."""
    if not filas:
        return {}
    camiones = sorted({f["asset_id"] for f in filas})
    try:
        cursor.execute(
            f"""SELECT * FROM cortes_senal WHERE asset_id IN ({", ".join(["%s"] * len(camiones))})
                AND desde >= %s AND desde <= %s ORDER BY desde""",
            (*camiones, min(f["inicio"] for f in filas), max(f["fin"] for f in filas)))
        cortes = cursor.fetchall()
    except pymysql.err.ProgrammingError:
        return {}  # tabla aún no creada
    return {f["id"]: [_corte(c, ahora) for c in cortes
                      if c["asset_id"] == f["asset_id"] and f["inicio"] <= c["desde"] <= f["fin"]] for f in filas}


def _leer_alertas(cursor, condicion, valores, ahora):
    try:
        cursor.execute(f"SELECT * FROM alertas_ruta WHERE {condicion} ORDER BY fin DESC LIMIT {MAX_ALERTAS}", valores)
    except pymysql.err.ProgrammingError:
        return []  # tabla aún no creada (Machine la crea al arrancar)
    filas = cursor.fetchall()
    cortes = _con_cortes(cursor, filas, ahora)
    lista = [dict(_alerta(fila, ahora), cortes=cortes.get(fila["id"], [])) for fila in filas]
    orden = {"activa": 0, "sin_senal": 1, "terminada": 2}
    return sorted(lista, key=lambda a: orden[a["estado"]])


def _resumen(lista):
    return {"activas": sum(a["estado"] == "activa" for a in lista),
            "sin_senal": sum(a["estado"] == "sin_senal" for a in lista),
            "terminadas": sum(a["estado"] == "terminada" for a in lista),
            "camiones_con_alertas": len({a["patente"] for a in lista})}


def alertas_rango(cursor, desde, hasta, ahora=None):
    """Alertas terminadas que empezaron entre los días locales desde y hasta (ambos incluidos).
    Una alerta entra al historial cuando termina: así sus datos (duración, km, camino) ya no cambian."""
    ahora = ahora or datetime.now(timezone.utc).replace(tzinfo=None)
    inicio, _ = dias.limites_utc(desde)
    _, fin = dias.limites_utc(hasta)
    lista = _leer_alertas(cursor, "estado = 'terminada' AND inicio >= %s AND inicio < %s", (inicio, fin), ahora)
    lista.sort(key=lambda a: a["inicio"], reverse=True)
    return {"status": "ok", "desde": desde.isoformat(), "hasta": hasta.isoformat(), "zona": dias.ZONA,
            "min_minutos": MIN_MINUTOS, "min_km": MIN_KM, "alertas": lista, "resumen": _resumen(lista)}


# --- Respuesta en vivo ------------------------------------------------------------------------

def vigilancia(cursor, ahora=None):
    """Camiones en vivo sobre las rutas aprendidas y alertas abiertas y de hoy. None mientras el
    modelo no haya aprendido ninguna ruta (la API responde "pendiente")."""
    ahora = ahora or datetime.now(timezone.utc).replace(tzinfo=None)
    modelo, mapa = leer_modelo(cursor)
    if modelo is None:
        return None
    etiquetas = {r["id"]: r["label"] for r in modelo["rutas"]}
    vehiculos = rutas_api.latest_positions(cursor, {})
    ahora_por_nivel = Counter()
    for vehiculo in vehiculos:
        vehiculo["nivel"] = vehiculo["ruta"] = vehiculo["ruta_label"] = None
        lat, lon = vehiculo.get("latitude"), vehiculo.get("longitude")
        momento = vehiculo.get("timestamp")
        reciente = momento and (ahora - datetime.fromisoformat(str(momento).rstrip("Z"))).total_seconds() <= ACTIVA_MIN * 60
        if reciente and rm.gps_valido(lat, lon):
            numero, ruta = mapa.en(float(lat), float(lon))
            vehiculo.update(nivel=f"N{numero}", ruta=ruta, ruta_label=etiquetas.get(ruta))
            ahora_por_nivel[f"N{numero}"] += 1
    data = {
        "status": "ok", "generado_en": rm.iso(ahora), "hoy": dias.hoy(ahora).isoformat(),
        "modelo": {k: v for k, v in modelo.items() if k not in ("rutas", "red")},
        "refresh_seconds": rutas_api.REFRESH_SECONDS, "min_minutos": MIN_MINUTOS, "min_km": MIN_KM,
        "vehiculos": vehiculos, "resumen": {"camiones_por_nivel": dict(ahora_por_nivel)},
    }
    return con_alertas(data, cursor, ahora)


def con_alertas(data, cursor, ahora=None):
    """Copia de la respuesta en vivo con las alertas recién leídas de alertas_ruta (abiertas y
    terminadas hoy). Es una consulta liviana: se hace en cada petición, así las alertas y sus
    conteos nunca quedan atrasados respecto del registro aunque las posiciones vengan de la caché."""
    ahora = ahora or datetime.now(timezone.utc).replace(tzinfo=None)
    hoy, _ = dias.limites_utc(dias.hoy(ahora))
    lista = _leer_alertas(cursor, "estado IN ('activa', 'sin_senal') OR fin >= %s", (hoy,), ahora)
    en_alerta = {a["patente"] for a in lista if a["estado"] == "activa"}
    vehiculos = [dict(v, en_alerta=v["vehicle_id"] in en_alerta) for v in data["vehiculos"]]
    resumen = dict(_resumen(lista), camiones_por_nivel=data["resumen"]["camiones_por_nivel"])
    return dict(data, alertas=lista, resumen=resumen, vehiculos=vehiculos)
