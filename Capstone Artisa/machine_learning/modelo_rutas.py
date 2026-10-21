"""Machine learning de Artisa: el modelo que aprende las rutas de la flota.

Nadie le dice al modelo cuáles son las rutas: las descubre en los viajes reales de los
camiones (aprendizaje no supervisado) y las clasifica según cuántos camiones las usan. Se
reaprende solo cada día con el día recién cerrado, así aparecen las rutas nuevas y bajan de
nivel las que se dejan de usar. Todo el modelo está en este archivo, dividido en partes:

  PARTE 1 · Viajes            Detecta paradas y viajes en los reportes GPS de cada camión.
                              La usa el cierre diario (diario.py) y guarda viajes_camion.
  PARTE 2 · Datos             Lee los viajes de la ventana de entrenamiento, empalma los que
                              cruzaron la medianoche y deja solo los que tienen origen y destino.
  PARTE 3 · Lugares           DBSCAN (distancia haversine): agrupa las paradas en lugares;
                              dentro de una faena, el lugar es la faena.
  PARTE 4 · Rutas             Clustering jerárquico aglomerativo (enlace promedio) sobre la forma
                              de cada viaje; la línea de cada ruta es su medoide (un viaje real).
  PARTE 5 · Niveles           Cortes naturales de Jenks (k-means exacto en 1D) sobre los camiones
                              distintos de cada ruta: N1 troncal … N4 poco usada; N5 mínima.
  PARTE 6 · Entrenamiento     Cuándo reaprender (cada día cerrado, RUTAS_CADA_DIAS), y
                              cómo se guarda y activa cada versión (rutas_version / _clasificadas).
  PARTE 7 · Validación        Revisión del modelo con los datos reales, desde la terminal:
      docker compose exec machine-diario python -m machine_learning.modelo_rutas
      docker compose exec machine-diario python -m machine_learning.modelo_rutas --ahora
      docker compose exec machine-diario python -m machine_learning.modelo_rutas --geojson > rutas.geojson

  PARTE 8 · Alertas           Cada minuto compara los camiones en vivo con las rutas aprendidas y
                              guarda en alertas_ruta a los que andan en una ruta mínima (N5) o
                              fuera de ruta (N6). Al terminar, cada alerta queda fija.

  PARTE 9 · Cortes de señal   Cada minuto registra en cortes_senal cuando un camión fuera de las
                              rutas habituales deja de dar posiciones nuevas: último reporte antes,
                              primero después y si el equipo siguió transmitiendo sin posición.

El mapa de la web (endpoints/vigilancia_api.py) usa la misma versión activa y el mismo índice de
rutas (common/rutas_mapa.py) para dar a cada camión su nivel en vivo, y lee las alertas guardadas.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import sys
import time

import numpy as np

from common import rutas_mapa as rm


# ==============================================================================================
# PARTE 1 · VIAJES: paradas y recorridos de cada camión
# Una parada es un lugar donde el camión permanece al menos PARADA_MIN_MIN minutos dentro de
# un radio de PARADA_RADIO_M metros (carga, descarga, base, espera). Un viaje es lo que ocurre
# entre dos paradas consecutivas. Para no inventar recorrido: solo se unen reportes seguidos con
# pausa <= MAX_GAP_MIN y velocidad implícita <= VEL_MAX_KMH (un salto de GPS no se dibuja y el
# viaje queda con_saltos); una pausa sin reportes > GAP_VIAJE_MIN en movimiento corta el viaje;
# el corte de medianoche marca abierto_fin / abierto_inicio y la Parte 2 los vuelve a empalmar.
# Las líneas son puntos GPS reales simplificados (Douglas-Peucker, SIMPLIFICACION_M).
# ==============================================================================================


PARADA_RADIO_M = 200
PARADA_MIN_MIN = 10
MAX_GAP_MIN = 5
VEL_MAX_KMH = 150
GAP_VIAJE_MIN = 30
VIAJE_MIN_KM = 1.0
VIAJE_MIN_PUNTOS = 5
SIMPLIFICACION_M = 20
METROS_POR_GRADO = 111_000


def distancia_m(lat0, lon0, lat1, lon1):
    return math.hypot(lat1 - lat0, (lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))) * METROS_POR_GRADO


def unible(p0, p1):
    """¿Se puede trazar una línea entre dos reportes seguidos sin inventar recorrido?"""
    horas = (p1[0] - p0[0]).total_seconds() / 3600
    if not 0 < horas <= MAX_GAP_MIN / 60:
        return False
    return distancia_m(p0[1], p0[2], p1[1], p1[2]) / 1000 <= VEL_MAX_KMH * horas


@dataclass
class Viaje:
    asset_id: str
    inicio: datetime
    fin: datetime
    origen: tuple            # (lat, lon) de la parada de origen, o None si se desconoce
    destino: tuple           # (lat, lon) de la parada de destino, o None
    tramos: list             # [[(lat, lon), ...], ...] puntos reales, sin unir saltos
    puntos: int
    km: float
    con_saltos: bool
    abierto_inicio: bool = False
    abierto_fin: bool = False
    extra: dict = field(default_factory=dict)


def detectar_paradas(puntos):
    """Índices [(i, j)] de los tramos donde el camión estuvo detenido (puntos i..j).

    Una parada no se corta por falta de reportes: un camión cargando con el motor apagado
    puede dejar de reportar mucho rato y sigue siendo la misma parada si al volver a
    reportar está en el mismo lugar. El tiempo que pasa detenido no separa viajes."""
    paradas, i, n = [], 0, len(puntos)
    while i < n:
        j = i + 1
        while j < n and distancia_m(puntos[i][1], puntos[i][2], puntos[j][1], puntos[j][2]) <= PARADA_RADIO_M:
            j += 1
        if (puntos[j - 1][0] - puntos[i][0]).total_seconds() >= PARADA_MIN_MIN * 60:
            paradas.append((i, j - 1))
            i = j
        else:
            i += 1
    return paradas


def _centro(puntos, i, j):
    sub = puntos[i:j + 1]
    return (sum(p[1] for p in sub) / len(sub), sum(p[2] for p in sub) / len(sub))


def _simplificar(tramo, tolerancia=SIMPLIFICACION_M):
    """Douglas-Peucker iterativo sobre puntos reales (lat, lon)."""
    if len(tramo) <= 2:
        return list(tramo)
    lat0 = math.radians(sum(p[0] for p in tramo) / len(tramo))
    xy = [(p[1] * METROS_POR_GRADO * math.cos(lat0), p[0] * METROS_POR_GRADO) for p in tramo]
    conservar = [False] * len(tramo)
    conservar[0] = conservar[-1] = True
    pila = [(0, len(tramo) - 1)]
    while pila:
        a, b = pila.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        largo = math.hypot(dx, dy) or 1e-9
        peor, indice = -1.0, None
        for k in range(a + 1, b):
            px, py = xy[k]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / largo
            if d > peor:
                peor, indice = d, k
        if indice is not None and peor > tolerancia:
            conservar[indice] = True
            pila.append((a, indice))
            pila.append((indice, b))
    return [p for p, keep in zip(tramo, conservar) if keep]


def _armar_viaje(asset_id, puntos, i, j, origen, destino, abierto_inicio, abierto_fin):
    tramos, actual, km, saltos = [], [(puntos[i][1], puntos[i][2])], 0.0, False
    for k in range(i + 1, j + 1):
        if unible(puntos[k - 1], puntos[k]):
            km += distancia_m(puntos[k - 1][1], puntos[k - 1][2], puntos[k][1], puntos[k][2]) / 1000
            actual.append((puntos[k][1], puntos[k][2]))
        else:
            saltos = True
            if len(actual) >= 2:
                tramos.append(actual)
            actual = [(puntos[k][1], puntos[k][2])]
    if len(actual) >= 2:
        tramos.append(actual)
    return Viaje(asset_id=asset_id, inicio=puntos[i][0], fin=puntos[j][0], origen=origen, destino=destino,
                 tramos=[_simplificar(t) for t in tramos], puntos=j - i + 1, km=round(km, 3),
                 con_saltos=saltos, abierto_inicio=abierto_inicio, abierto_fin=abierto_fin)


def extraer_viajes(asset_id, puntos, inicio_ventana=None, fin_ventana=None):
    """puntos: [(fecha_utc, lat, lon), ...] ordenados y limpios de un camión.
    inicio_ventana / fin_ventana: límites del día procesado, para marcar viajes cortados."""
    if len(puntos) < 2:
        return []
    paradas = detectar_paradas(puntos)
    # Segmentos de movimiento: entre paradas, y cortados por pausas largas sin reportes.
    limites = []  # (i, j, origen, destino, abierto_inicio, abierto_fin)
    anterior_fin, origen = 0, None
    if paradas and paradas[0][0] == 0:
        origen = _centro(puntos, *paradas[0])
        anterior_fin = paradas[0][1]
        paradas = paradas[1:]
    else:
        origen = None
    marcas = [(i, j) for i, j in paradas] + [(len(puntos), len(puntos))]
    for i_parada, j_parada in marcas:
        destino = _centro(puntos, i_parada, j_parada) if i_parada < len(puntos) else None
        fin_mov = i_parada if i_parada < len(puntos) else len(puntos) - 1
        a = anterior_fin
        # cortar por pausas largas dentro del movimiento
        corte = a
        for k in range(a + 1, fin_mov + 1):
            if (puntos[k][0] - puntos[k - 1][0]).total_seconds() > GAP_VIAJE_MIN * 60:
                limites.append((corte, k - 1, origen, None, origen is None, True))
                corte, origen = k, None
        limites.append((corte, fin_mov, origen, destino, origen is None, destino is None))
        if i_parada < len(puntos):
            origen = destino
            anterior_fin = j_parada
    viajes = []
    for i, j, ori, des, sin_origen, sin_destino in limites:
        if j - i + 1 < 2:
            continue
        cerca_inicio = inicio_ventana is not None and (puntos[i][0] - inicio_ventana) <= timedelta(minutes=GAP_VIAJE_MIN)
        cerca_fin = fin_ventana is not None and (fin_ventana - puntos[j][0]) <= timedelta(minutes=GAP_VIAJE_MIN)
        viaje = _armar_viaje(asset_id, puntos, i, j, ori, des,
                             abierto_inicio=sin_origen and cerca_inicio,
                             abierto_fin=sin_destino and cerca_fin)
        # Los fragmentos en el borde del día se guardan aunque sean cortos: se empalman después.
        if viaje.abierto_inicio or viaje.abierto_fin or (
                viaje.km >= VIAJE_MIN_KM and viaje.puntos >= VIAJE_MIN_PUNTOS):
            viajes.append(viaje)
    return viajes


def empalmar(viajes):
    """Une los fragmentos de un mismo viaje partido por el corte de medianoche.
    `viajes` debe venir ordenado por (asset_id, inicio)."""
    resultado = []
    for viaje in viajes:
        previo = resultado[-1] if resultado else None
        if (previo and previo.asset_id == viaje.asset_id and previo.abierto_fin and viaje.abierto_inicio
                and timedelta(0) <= viaje.inicio - previo.fin <= timedelta(minutes=GAP_VIAJE_MIN)):
            union = (previo.tramos[-1][-1], viaje.tramos[0][0]) if previo.tramos and viaje.tramos else None
            continua = union is not None and unible((previo.fin, *union[0]), (viaje.inicio, *union[1]))
            tramos = list(previo.tramos)
            km_union = distancia_m(*union[0], *union[1]) / 1000 if continua else 0.0
            if continua:
                tramos[-1] = tramos[-1] + viaje.tramos[0]
                tramos += viaje.tramos[1:]
            else:
                tramos += viaje.tramos
            resultado[-1] = Viaje(
                asset_id=viaje.asset_id, inicio=previo.inicio, fin=viaje.fin, origen=previo.origen,
                destino=viaje.destino, tramos=tramos, puntos=previo.puntos + viaje.puntos,
                km=round(previo.km + viaje.km + km_union, 3), con_saltos=previo.con_saltos or viaje.con_saltos or not continua,
                abierto_inicio=previo.abierto_inicio, abierto_fin=viaje.abierto_fin)
        else:
            resultado.append(viaje)
    return resultado


def resumen_paradas(puntos):
    """(cantidad de paradas, minutos detenido) de un camión en el día."""
    paradas = detectar_paradas(puntos)
    minutos = sum((puntos[j][0] - puntos[i][0]).total_seconds() for i, j in paradas) / 60
    return len(paradas), round(minutos, 1)


# ==============================================================================================
# PARTE 2 · DATOS DE ENTRENAMIENTO
# Viajes de los últimos RUTAS_VENTANA_DIAS días cerrados, faenas y nombres de los lugares.
# ==============================================================================================


@dataclass
class ViajeRuta:
    asset_id: str
    inicio: datetime
    fin: datetime
    origen: tuple
    destino: tuple
    km: float
    puntos: int
    con_saltos: bool
    tramos: list            # [[(lat, lon), ...], ...]
    lugar_origen: str = None
    lugar_destino: str = None
    invertido: bool = False
    extra: dict = field(default_factory=dict)


VENTANA_DIAS = int(os.environ.get("RUTAS_VENTANA_DIAS", "90"))
CADA_DIAS = int(os.environ.get("RUTAS_CADA_DIAS", "1"))  # se reaprende con cada día cerrado nuevo
# Lo aprendido es la previsión de por dónde andará la flota los próximos HORIZONTE_DIAS días; como
# se reaprende cada día, esa ventana avanza un día cada madrugada.
HORIZONTE_DIAS = int(os.environ.get("RUTAS_HORIZONTE_DIAS", "7"))
CONSERVAR_VERSIONES = 3


def cargar_faenas(cursor):
    cursor.execute("SELECT id, nombre, ST_AsGeoJSON(geocerca) AS geojson FROM faenas WHERE geocerca IS NOT NULL")
    faenas = []
    for fila in cursor.fetchall():
        geo = fila["geojson"]
        faenas.append({"id": fila["id"], "nombre": fila["nombre"],
                       "geojson": geo if isinstance(geo, dict) else json.loads(geo)})
    return faenas


def ventana(cursor, dias=VENTANA_DIAS):
    """(desde, hasta) entre los días cerrados más recientes."""
    cursor.execute("SELECT MAX(fecha) AS hasta FROM reporte_diario_estado WHERE estado='cerrado'")
    fila = cursor.fetchone() or {}
    hasta = fila.get("hasta")
    if hasta is None:
        return None, None
    return hasta - timedelta(days=dias - 1), hasta


def cargar_viajes(cursor, desde, hasta):
    cursor.execute(
        """SELECT v.* FROM viajes_camion AS v
           INNER JOIN reporte_diario_estado AS e ON e.fecha = v.fecha AND e.estado = 'cerrado'
           WHERE v.fecha >= %s AND v.fecha <= %s ORDER BY v.asset_id, v.inicio""",
        (desde, hasta),
    )
    viajes = []
    for fila in cursor.fetchall():
        tramos = [[(lat, lon) for lon, lat in tramo] for tramo in json.loads(fila["geometria"])]
        origen = (fila["origen_lat"], fila["origen_lon"]) if fila["origen_lat"] is not None else None
        destino = (fila["destino_lat"], fila["destino_lon"]) if fila["destino_lat"] is not None else None
        viajes.append(Viaje(asset_id=fila["asset_id"], inicio=fila["inicio"], fin=fila["fin"], origen=origen,
                            destino=destino, tramos=tramos, puntos=fila["puntos"], km=fila["km"],
                            con_saltos=bool(fila["con_saltos"]), abierto_inicio=bool(fila["abierto_inicio"]),
                            abierto_fin=bool(fila["abierto_fin"])))
    return viajes


def comuna(location):
    """'S/N, Calama, Provincia de El Loa, Región de Antofagasta, Chile' -> 'Calama'."""
    partes = [p.strip() for p in str(location or "").split(",") if p.strip()]
    for i, parte in enumerate(partes):
        if parte.lower().startswith(("provincia", "región", "region")) and i > 0:
            return partes[i - 1]
    utiles = [p for p in partes if p.upper() != "S/N" and not p[:1].isdigit() and p.lower() != "chile"]
    return utiles[1] if len(utiles) > 1 else (utiles[0] if utiles else None)


def nombrador(cursor):
    """Devuelve una función que agrega 'comuna' a cada lugar frecuente, leyendo la dirección
    del GPS en el momento de una de sus paradas (consultas por el índice camión+fecha)."""
    def nombrar(lugares):
        for lugar in lugares.values():
            if lugar["tipo"] != "lugar":
                continue
            for asset_id, momento in lugar.get("muestras", [])[:3]:
                encontrado = None
                for tabla in ("telemetria_api", "raw_gps"):
                    try:
                        cursor.execute(
                            f"""SELECT location FROM {tabla}
                                WHERE asset_id=%s AND report_date BETWEEN %s AND %s
                                  AND location IS NOT NULL AND location <> '' LIMIT 1""",
                            (asset_id, momento - timedelta(minutes=10), momento + timedelta(minutes=10)),
                        )
                    except Exception:
                        continue
                    fila = cursor.fetchone()
                    if fila and comuna(fila["location"]):
                        encontrado = comuna(fila["location"])
                        break
                if encontrado:
                    lugar["comuna"] = encontrado
                    break
    return nombrar


def preparar(viajes):
    """Empalma y separa los viajes clasificables de los que no tienen origen o destino."""
    completos, sin_extremos, cortos = [], 0, 0
    for v in empalmar(viajes):
        if v.origen is None or v.destino is None or not v.tramos:
            sin_extremos += 1
        elif v.km < VIAJE_MIN_KM or v.puntos < VIAJE_MIN_PUNTOS:
            cortos += 1
        else:
            completos.append(ViajeRuta(asset_id=v.asset_id, inicio=v.inicio, fin=v.fin, origen=v.origen,
                                       destino=v.destino, km=v.km, puntos=v.puntos, con_saltos=v.con_saltos,
                                       tramos=v.tramos))
    return completos, {"viajes_sin_origen_o_destino": sin_extremos, "viajes_cortos": cortos}


# ==============================================================================================
# PARTE 3 · LUGARES · DBSCAN
# Cada extremo de viaje va a la faena que lo contiene; los demás se agrupan con DBSCAN
# (radio LUGAR_RADIO_M, mínimo LUGAR_MIN_VIAJES). Los aislados quedan como puntos propios.
# ==============================================================================================


LUGAR_RADIO_M = 500
LUGAR_MIN_VIAJES = 3


def _dentro_anillo(lat, lon, anillo):
    dentro = False
    for (x0, y0), (x1, y1) in zip(anillo, anillo[1:] + anillo[:1]):
        if (y0 > lat) != (y1 > lat) and lon < (x1 - x0) * (lat - y0) / ((y1 - y0) or 1e-12) + x0:
            dentro = not dentro
    return dentro


def dentro_geojson(lat, lon, geometria):
    poligonos = [geometria["coordinates"]] if geometria["type"] == "Polygon" else geometria["coordinates"]
    for poligono in poligonos:
        if _dentro_anillo(lat, lon, poligono[0]) and not any(_dentro_anillo(lat, lon, h) for h in poligono[1:]):
            return True
    return False


def asignar_lugares(viajes, faenas):
    """Asigna lugar_origen/lugar_destino a cada viaje. Devuelve {clave: info del lugar}."""
    from sklearn.cluster import DBSCAN

    extremos = []  # (viaje, "origen"/"destino", lat, lon)
    for v in viajes:
        extremos.append((v, "origen", *v.origen))
        extremos.append((v, "destino", *v.destino))
    lugares, libres = {}, []
    for k, (v, lado, lat, lon) in enumerate(extremos):
        faena = next((f for f in faenas if dentro_geojson(lat, lon, f["geojson"])), None)
        if faena:
            clave = f"F{faena['id']}"
            lugar = lugares.setdefault(clave, {"clave": clave, "nombre": faena["nombre"], "tipo": "faena",
                                               "puntos": [], "muestras": []})
            lugar["puntos"].append((lat, lon))
            if len(lugar["muestras"]) < 5:
                lugar["muestras"].append((v.asset_id, v.inicio if lado == "origen" else v.fin))
            setattr(v, f"lugar_{lado}", clave)
        else:
            libres.append(k)
    if libres:
        coords = np.radians([[extremos[k][2], extremos[k][3]] for k in libres])
        etiquetas = DBSCAN(eps=LUGAR_RADIO_M / 6_371_000, min_samples=LUGAR_MIN_VIAJES,
                           metric="haversine", algorithm="ball_tree").fit_predict(coords)
        for k, etiqueta in zip(libres, etiquetas):
            v, lado, lat, lon = extremos[k]
            clave = f"L{int(etiqueta)}" if etiqueta >= 0 else f"P{lat:.3f},{lon:.3f}"
            tipo = "lugar" if etiqueta >= 0 else "punto"
            lugar = lugares.setdefault(clave, {"clave": clave, "nombre": None, "tipo": tipo,
                                               "puntos": [], "muestras": []})
            lugar["puntos"].append((lat, lon))
            if len(lugar["muestras"]) < 5:
                lugar["muestras"].append((v.asset_id, v.inicio if lado == "origen" else v.fin))
            setattr(v, f"lugar_{lado}", clave)
    numerados = sorted((l for l in lugares.values() if l["tipo"] != "faena"),
                       key=lambda l: (-len(l["puntos"]), l["clave"]))
    for n, lugar in enumerate(numerados, start=1):
        lugar["nombre"] = f"Lugar {n}" if lugar["tipo"] == "lugar" else f"Punto aislado {n}"
    for lugar in lugares.values():
        pts = lugar.pop("puntos")
        lugar["latitud"] = round(sum(p[0] for p in pts) / len(pts), 6)
        lugar["longitud"] = round(sum(p[1] for p in pts) / len(pts), 6)
        lugar["extremos"] = len(pts)
    return lugares


# ==============================================================================================
# PARTE 4 · RUTAS · clustering jerárquico + medoide
# Por par de lugares (sin importar el sentido), cada viaje se resume en MUESTRAS puntos a igual
# distancia; los que siguen el mismo camino (distancia media < RUTA_DIST_M) forman una ruta.
# ==============================================================================================


RUTA_DIST_M = 750
MUESTRAS = 32
MAX_GRUPO = 2500  # pares muy frecuentes: se agrupa una muestra y se asigna el resto


def firma(tramos, lat_ref, n=MUESTRAS):
    """n puntos a igual distancia a lo largo del recorrido, en metros (plano local)."""
    coseno = math.cos(math.radians(lat_ref))
    xy = np.array([(lon * METROS_POR_GRADO * coseno, lat * METROS_POR_GRADO)
                   for tramo in tramos for lat, lon in tramo], dtype=float)
    if len(xy) == 1:
        return np.repeat(xy, n, axis=0)
    pasos = np.hypot(*np.diff(xy, axis=0).T)
    acumulado = np.concatenate([[0.0], np.cumsum(pasos)])
    if acumulado[-1] == 0:
        return np.repeat(xy[:1], n, axis=0)
    objetivo = np.linspace(0, acumulado[-1], n)
    return np.column_stack([np.interp(objetivo, acumulado, xy[:, 0]), np.interp(objetivo, acumulado, xy[:, 1])])


def distancias(firmas, otras=None):
    """Distancia media entre puntos homólogos (metros), por bloques para acotar memoria."""
    otras = firmas if otras is None else otras
    resultado = np.empty((len(firmas), len(otras)))
    for i in range(0, len(firmas), 256):
        bloque = firmas[i:i + 256]
        resultado[i:i + 256] = np.linalg.norm(bloque[:, None] - otras[None], axis=-1).mean(axis=-1)
    return resultado


def _agrupar(firmas, mismo_lugar):
    """Etiquetas de ruta para las firmas de un par origen-destino."""
    from sklearn.cluster import AgglomerativeClustering

    m = len(firmas)
    if m == 1:
        return np.zeros(1, dtype=int), np.zeros((1, 1))
    d = distancias(firmas)
    if mismo_lugar:  # circuitos que salen y vuelven al mismo lugar: se compara en ambos sentidos
        d = np.minimum(d, distancias(firmas, firmas[:, ::-1]))
        d = np.minimum(d, d.T)
    etiquetas = AgglomerativeClustering(n_clusters=None, metric="precomputed", linkage="average",
                                        distance_threshold=RUTA_DIST_M).fit_predict(d)
    return etiquetas, d


def _orientar(v):
    """Orienta el viaje desde el lugar de clave menor, para comparar ida y vuelta juntas."""
    if v.lugar_origen > v.lugar_destino:
        v.invertido = True
        v.tramos = [list(reversed(t)) for t in reversed(v.tramos)]
        v.lugar_origen, v.lugar_destino = v.lugar_destino, v.lugar_origen
    return v


def _route_id(lugar_a, lugar_b, medoide, usados):
    """Identificador estable de la ruta (cabe en route_id VARCHAR(16)). Con miles de rutas un
    código corto puede repetirse: si ya se usó en esta versión, se vuelve a calcular."""
    base, intento = f"{lugar_a}|{lugar_b}|{medoide.asset_id}|{medoide.inicio.isoformat()}", 0
    while True:
        codigo = "R-" + hashlib.sha1(f"{base}|{intento}".encode() if intento else base.encode()).hexdigest()[:12].upper()
        if codigo not in usados:
            usados.add(codigo)
            return codigo
        intento += 1


# ==============================================================================================
# PARTE 5 · NIVELES · cortes naturales de Jenks
# Parámetro: camiones distintos por ruta. N5 hasta MAX_CAMIONES_MINIMA; el resto en hasta
# GRUPOS_APRENDIDOS grupos aprendidos (N1 el de más camiones).
# ==============================================================================================


# Se conservan también los caminos de un solo camión: son justamente las rutas "mínimas".
MIN_RECORRIDOS = int(os.environ.get("RUTAS_MIN_RECORRIDOS", "1"))
MIN_VEHICULOS = int(os.environ.get("RUTAS_MIN_VEHICULOS", "1"))
MAX_CAMIONES_MINIMA = int(os.environ.get("RUTAS_MAX_CAMIONES_MINIMA", "2"))
GRUPOS_APRENDIDOS = 4  # N1..N4
METODO = "camiones-jenks-v1"  # si cambia, se reaprende sin esperar el próximo cierre
NIVELES = {
    "N1": ("Troncal", "#14532d"),
    "N2": ("Muy usada", "#3f9d4a"),
    "N3": ("Uso medio", "#d9a021"),
    "N4": ("Poco usada", "#ef7d22"),
    "N5": ("Mínima: 1 o 2 camiones", "#d7301f"),
}


def cortes_naturales(valores, grupos):
    """Agrupación óptima en 1D (Jenks/Fisher): reparte los valores en `grupos` tramos
    contiguos con la menor varianza dentro de cada uno. Devuelve [(desde, hasta)] de menor a
    mayor. Exacta por programación dinámica sobre los valores distintos (pesados por cuántas
    veces aparecen), así siempre da el mismo resultado."""
    conteo = Counter(valores)
    x = sorted(conteo)
    k = min(grupos, len(x))
    if k == 0:
        return []
    w = [conteo[v] for v in x]
    z = [math.log(v) for v in x]
    sw, sz, szz = [0.0], [0.0], [0.0]
    for wi, zi in zip(w, z):
        sw.append(sw[-1] + wi)
        sz.append(sz[-1] + wi * zi)
        szz.append(szz[-1] + wi * zi * zi)

    def costo(i, j):  # suma de cuadrados de x[i..j]
        peso, suma = sw[j + 1] - sw[i], sz[j + 1] - sz[i]
        return (szz[j + 1] - szz[i]) - suma * suma / peso

    n = len(x)
    inf = float("inf")
    mejor = [[inf] * n for _ in range(k)]
    corte = [[0] * n for _ in range(k)]
    for j in range(n):
        mejor[0][j] = costo(0, j)
    for g in range(1, k):
        for j in range(g, n):
            for i in range(g, j + 1):
                c = mejor[g - 1][i - 1] + costo(i, j)
                if c < mejor[g][j] - 1e-12:
                    mejor[g][j], corte[g][j] = c, i
    tramos, j = [], n - 1
    for g in range(k - 1, -1, -1):
        i = corte[g][j] if g else 0
        tramos.append((x[i], x[j]))
        j = i - 1
    return list(reversed(tramos))


def clasificar_niveles(camiones):
    """Nivel de cada ruta según sus camiones distintos: N5 hasta MAX_CAMIONES_MINIMA; el resto
    en hasta cuatro grupos aprendidos (N1 el de más camiones). Devuelve (niveles, cortes)."""
    frecuentes = [c for c in camiones if c > MAX_CAMIONES_MINIMA]
    tramos = cortes_naturales(frecuentes, GRUPOS_APRENDIDOS)[::-1]  # de mayor a menor
    cortes = [{"nivel": f"N{k}", "desde": int(desde), "hasta": int(hasta)}
              for k, (desde, hasta) in enumerate(tramos, start=1)]
    cortes.append({"nivel": "N5", "desde": 1, "hasta": MAX_CAMIONES_MINIMA})
    niveles = []
    for c in camiones:
        niveles.append(next(t["nivel"] for t in cortes if c >= t["desde"]) if c > MAX_CAMIONES_MINIMA else "N5")
    return niveles, {"parametro": "camiones distintos por ruta", "metodo": "cortes naturales (Jenks) en log",
                     "cortes": cortes}


# ==============================================================================================
# PARTE 6 · ENTRENAMIENTO SEMANAL Y VERSIONES
# construir_rutas junta las Partes 3 a 5. Se reaprende desde cero con cada día cerrado nuevo (CADA_DIAS) o si cambia METODO. Cada
# versión se guarda y activa en una sola transacción; se conservan CONSERVAR_VERSIONES.
# ==============================================================================================


def nombres_unicos(lugares):
    """'Calama', 'Calama 2'... cuando varios lugares caen en la misma comuna."""
    vistos = Counter()
    for lugar in sorted(lugares.values(), key=lambda l: (-l["extremos"], l["clave"])):
        if lugar["tipo"] == "faena" or not lugar.get("comuna"):
            continue
        vistos[lugar["comuna"]] += 1
        n = vistos[lugar["comuna"]]
        lugar["nombre"] = lugar["comuna"] if n == 1 else f"{lugar['comuna']} {n}"


def construir_rutas(viajes, faenas, nombrar=None):
    """Entrenamiento completo: Partes 3, 4 y 5 sobre los viajes de la ventana.

    viajes: ViajeRuta con origen y destino conocidos. nombrar(lugares): opcional, agrega
    lugar['comuna'] a partir de los datos GPS. Devuelve (rutas, lugares, métricas)."""
    if not viajes:
        return [], {}, {"viajes_clasificados": 0, "rutas": 0}
    lugares = asignar_lugares(viajes, faenas)
    if nombrar:
        nombrar(lugares)
        nombres_unicos(lugares)
    lat_ref = float(np.mean([v.origen[0] for v in viajes]))
    grupos = defaultdict(list)
    for v in viajes:
        _orientar(v)
        grupos[(v.lugar_origen, v.lugar_destino)].append(v)

    rutas, usados = [], set()
    rng = np.random.default_rng(42)
    for (a, b), miembros in sorted(grupos.items()):
        firmas_todas = np.stack([firma(v.tramos, lat_ref) for v in miembros])
        indices = np.arange(len(miembros))
        if len(miembros) > MAX_GRUPO:  # pares muy frecuentes: se agrupa una muestra y se asigna el resto
            indices = np.sort(rng.choice(len(miembros), MAX_GRUPO, replace=False))
        etiquetas, d = _agrupar(firmas_todas[indices], a == b)
        clusters = defaultdict(list)
        for pos, etiqueta in enumerate(etiquetas):
            clusters[int(etiqueta)].append(pos)
        medoides = {}
        for etiqueta, posiciones in clusters.items():
            sub = d[np.ix_(posiciones, posiciones)]
            orden = np.argsort(sub.sum(axis=1))
            limpios = [posiciones[k] for k in orden if not miembros[indices[posiciones[k]]].con_saltos]
            medoides[etiqueta] = limpios[0] if limpios else posiciones[orden[0]]
        asignacion = {int(indices[pos]): int(etiquetas[pos]) for pos in range(len(indices))}
        if len(indices) < len(miembros):
            claves = list(medoides)
            centros = firmas_todas[[indices[medoides[c]] for c in claves]]
            resto = [i for i in range(len(miembros)) if i not in asignacion]
            dist = distancias(firmas_todas[resto], centros)
            siguiente = max(claves) + 1
            for fila, i in enumerate(resto):
                mejor = int(np.argmin(dist[fila]))
                if dist[fila, mejor] <= RUTA_DIST_M:
                    asignacion[i] = claves[mejor]
                else:
                    asignacion[i] = siguiente
                    medoides[siguiente] = None
                    siguiente += 1
        por_ruta = defaultdict(list)
        for i, etiqueta in asignacion.items():
            por_ruta[etiqueta].append(miembros[i])
        for etiqueta, lista in por_ruta.items():
            pos = medoides.get(etiqueta)
            medoide = miembros[indices[pos]] if pos is not None else lista[0]
            tramos = medoide.tramos
            fuente = "viaje_real_medoide"
            if len(tramos) > 1:  # medoide con saltos de GPS: se muestra solo su tramo continuo más largo
                tramos = [max(tramos, key=len)]
                fuente = "viaje_real_parcial"
            rutas.append({
                "route_id": _route_id(a, b, medoide, usados),
                "origen": a, "destino": b,
                "label": f"{lugares[a]['nombre']} ↔ {lugares[b]['nombre']}" if a != b
                         else f"Circuito en {lugares[a]['nombre']}",
                "usage_count": len(lista),
                "unique_vehicle_count": len({v.asset_id for v in lista}),
                "observed_points": sum(v.puntos for v in lista),
                "km_mediana": round(float(np.median([v.km for v in lista])), 2),
                "primer_viaje": min(v.inicio for v in lista),
                "ultimo_viaje": max(v.fin for v in lista),
                "vehiculos": sorted(Counter(v.asset_id for v in lista).items(), key=lambda x: -x[1])[:20],
                "geometry": {"type": "LineString",
                             "coordinates": [[round(lon, 6), round(lat, 6)] for lat, lon in tramos[0]]},
                "geometry_source": fuente,
                "miembros": lista,
            })
    # Con mínimos sobre 1 (RUTAS_MIN_*), los caminos más raros quedan fuera del modelo y un
    # camión que pase por ahí se verá "fuera de ruta" en vez de en una ruta mínima.
    esporadicas = [r for r in rutas if r["usage_count"] < MIN_RECORRIDOS or r["unique_vehicle_count"] < MIN_VEHICULOS]
    rutas = [r for r in rutas if r["usage_count"] >= MIN_RECORRIDOS and r["unique_vehicle_count"] >= MIN_VEHICULOS]
    niveles, umbrales = clasificar_niveles([r["unique_vehicle_count"] for r in rutas])
    for ruta, nivel in zip(rutas, niveles):
        ruta["usage_level"] = nivel
        ruta["usage_label"], ruta["color"] = NIVELES[nivel]
    rutas.sort(key=lambda r: (-r["unique_vehicle_count"], -r["usage_count"], r["route_id"]))
    for corte in umbrales["cortes"]:
        del_nivel = [r for r in rutas if r["usage_level"] == corte["nivel"]]
        corte.update(nombre=NIVELES[corte["nivel"]][0], rutas=len(del_nivel),
                     recorridos=sum(r["usage_count"] for r in del_nivel))
    metricas = {
        "viajes_clasificados": sum(r["usage_count"] for r in rutas),
        "viajes_analizados": len(viajes),
        "rutas": len(rutas),
        "pares_origen_destino": len(grupos),
        "lugares": len(lugares),
        "por_nivel": dict(Counter(r["usage_level"] for r in rutas)),
        "caminos_esporadicos": len(esporadicas),
        "viajes_esporadicos": sum(r["usage_count"] for r in esporadicas),
        "umbrales": umbrales,
        "parametros": {"lugar_radio_m": LUGAR_RADIO_M, "lugar_min_viajes": LUGAR_MIN_VIAJES,
                       "min_recorridos": MIN_RECORRIDOS, "min_vehiculos": MIN_VEHICULOS,
                       "max_camiones_minima": MAX_CAMIONES_MINIMA, "metodo": METODO,
                       "ruta_distancia_m": RUTA_DIST_M, "muestras": MUESTRAS},
    }
    return rutas, lugares, metricas


def guardar_version(connection, desde, hasta, rutas, lugares, metricas):
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO rutas_version (creado_en,desde,hasta,activa,metricas_json,lugares_json)
               VALUES (UTC_TIMESTAMP(6),%s,%s,0,%s,%s)""",
            (desde, hasta, json.dumps(metricas, ensure_ascii=False, default=str),
             json.dumps(lugares, ensure_ascii=False)),
        )
        version = cursor.lastrowid
        if rutas:
            cursor.executemany(
                """INSERT INTO rutas_clasificadas
                (version_id,route_id,label,origen,destino,usage_count,unique_vehicle_count,observed_points,
                 km_mediana,usage_level,geometry_source,geometria,primer_viaje,ultimo_viaje,vehiculos_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(version, r["route_id"], r["label"][:255], r["origen"], r["destino"], r["usage_count"],
                  r["unique_vehicle_count"], r["observed_points"], r["km_mediana"], r["usage_level"],
                  r["geometry_source"], json.dumps(r["geometry"], separators=(",", ":")),
                  r["primer_viaje"], r["ultimo_viaje"], json.dumps(r["vehiculos"])) for r in rutas],
            )
        ultimo = {}
        for r in rutas:
            for v in r["miembros"]:
                if v.asset_id not in ultimo or v.fin > ultimo[v.asset_id][1]:
                    ultimo[v.asset_id] = (r["route_id"], v.fin)
        if ultimo:
            cursor.executemany(
                "INSERT INTO rutas_vehiculo (version_id,asset_id,route_id,ultimo_viaje) VALUES (%s,%s,%s,%s)",
                [(version, asset, route, fin) for asset, (route, fin) in ultimo.items()],
            )
        cursor.execute("UPDATE rutas_version SET activa = (id = %s)", (version,))
        cursor.execute("SELECT id FROM rutas_version ORDER BY id DESC LIMIT 18446744073709551615 OFFSET %s",
                       (CONSERVAR_VERSIONES,))
        viejas = [fila["id"] for fila in cursor.fetchall()]
        for vieja in viejas:
            cursor.execute("DELETE FROM rutas_clasificadas WHERE version_id=%s", (vieja,))
            cursor.execute("DELETE FROM rutas_vehiculo WHERE version_id=%s", (vieja,))
            cursor.execute("DELETE FROM rutas_version WHERE id=%s", (vieja,))
    connection.commit()
    return version


def ejecutar(connection, dias=VENTANA_DIAS):
    comienzo = time.monotonic()
    with connection.cursor() as cursor:
        desde, hasta = ventana(cursor, dias)
        if desde is None:
            return None
        viajes = cargar_viajes(cursor, desde, hasta)
        faenas = cargar_faenas(cursor)
        cursor.execute(
            "SELECT COUNT(*) AS cerrados FROM reporte_diario_estado "
            "WHERE estado='cerrado' AND fecha >= %s AND fecha <= %s",
            (desde, hasta),
        )
        cerrados = cursor.fetchone()["cerrados"]
    connection.commit()
    completos, excluidos = preparar(viajes)
    with connection.cursor() as cursor:
        rutas, lugares, metricas = construir_rutas(completos, faenas, nombrar=nombrador(cursor))
    connection.commit()
    for lugar in lugares.values():
        lugar.pop("muestras", None)  # solo se usaban para nombrar
    metricas.update(excluidos)
    metricas["viajes_leidos"] = len(viajes)
    metricas["camiones"] = len({v.asset_id for v in completos})
    dias_ventana = (hasta - desde).days + 1
    metricas["cobertura"] = {"dias_cerrados": cerrados, "dias_ventana": dias_ventana,
                            "dias_pendientes": max(0, dias_ventana - cerrados)}
    metricas["segundos"] = round(time.monotonic() - comienzo, 1)
    version = guardar_version(connection, desde, hasta, rutas, lugares, metricas)
    print(f"Rutas: versión {version} · {desde}..{hasta} · {metricas['viajes_clasificados']} viajes -> "
          f"{metricas['rutas']} rutas {metricas.get('por_nivel', {})} en {metricas['segundos']} s", flush=True)
    return metricas


def necesita_recalculo(cursor, horas_minimas=2):
    """Toca reaprender: no hay versión, cambió el método, o ya se cerraron CADA_DIAS días
    nuevos desde los datos de la versión activa (y pasó el mínimo de horas entre versiones).

    Se cuenta en días de datos y no en días de calendario: durante la recuperación del
    histórico se reaprende a medida que entran días (con un mínimo de horas entre versiones),
    y en la operación normal queda una versión nueva por cada día cerrado."""
    cursor.execute("SELECT MAX(fecha) AS hasta FROM reporte_diario_estado WHERE estado='cerrado'")
    ultimo = (cursor.fetchone() or {}).get("hasta")
    if ultimo is None:
        return False
    cursor.execute("SELECT creado_en, hasta, metricas_json FROM rutas_version WHERE activa=1 ORDER BY id DESC LIMIT 1")
    version = cursor.fetchone()
    if version is None:
        return True
    try:
        metodo = json.loads(version["metricas_json"] or "{}").get("parametros", {}).get("metodo")
    except ValueError:
        metodo = None
    if metodo != METODO:
        return True
    if datetime.now(timezone.utc).replace(tzinfo=None) - version["creado_en"] < timedelta(hours=horas_minimas):
        return False
    return (ultimo - version["hasta"]).days >= CADA_DIAS


# ==============================================================================================
# PARTE 7 · VALIDACIÓN con los datos reales
# --ahora reaprende y publica antes de mostrar; --geojson imprime las rutas activas.
# ==============================================================================================


def main(argv):
    from common.db import connect_db

    with connect_db(autocommit=False, read_timeout=900) as connection:
        if "--ahora" in argv:
            ejecutar(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM rutas_version WHERE activa=1 ORDER BY id DESC LIMIT 1")
            version = cursor.fetchone()
            if not version:
                print("Todavía no hay una clasificación activa. Requiere días cerrados con viajes "
                      "(usa --ahora para intentar calcularla ya).")
                return 1
            cursor.execute("SELECT * FROM rutas_clasificadas WHERE version_id=%s ORDER BY unique_vehicle_count DESC, usage_count DESC, route_id",
                           (version["id"],))
            rutas = cursor.fetchall()
    metricas = json.loads(version["metricas_json"])
    lugares = json.loads(version["lugares_json"])
    if "--geojson" in argv:
        print(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": json.loads(r["geometria"]),
             "properties": {k: r[k] for k in ("route_id", "label", "usage_count", "unique_vehicle_count",
                                              "usage_level", "km_mediana", "geometry_source")}}
            for r in rutas]}, ensure_ascii=False))
        return 0
    print(f"Versión {version['id']} · viajes del {version['desde']} al {version['hasta']} · creada {version['creado_en']} UTC")
    print(f"Viajes leídos: {metricas.get('viajes_leidos')} · clasificados: {metricas.get('viajes_clasificados')} · "
          f"sin origen/destino: {metricas.get('viajes_sin_origen_o_destino')} · cortos: {metricas.get('viajes_cortos')}")
    print(f"Camiones: {metricas.get('camiones')} · lugares: {metricas.get('lugares')} · "
          f"pares origen-destino: {metricas.get('pares_origen_destino')} · rutas: {metricas.get('rutas')}")
    print(f"Por nivel: {metricas.get('por_nivel')} · rutas de un solo viaje: {metricas.get('rutas_de_un_solo_viaje')}")
    print("\nTabla de clasificación (camiones distintos por ruta):")
    for corte in (metricas.get("umbrales") or {}).get("cortes", []):
        print(f"  {corte['nivel']}  {corte.get('nombre', ''):<24} {corte['desde']:>4}–{corte['hasta']:<4} camiones  "
              f"{corte.get('rutas', 0):>6} rutas  {corte.get('recorridos', 0):>7} recorridos")
    print(f"Parámetros: {metricas.get('parametros')} · cálculo: {metricas.get('segundos')} s")
    print("\nRutas con más camiones:")
    for r in rutas[:15]:
        print(f"  {r['route_id']}  {r['usage_level']:<6} {r['usage_count']:>5} recorridos  "
              f"{r['unique_vehicle_count']:>3} camiones  {r['km_mediana']:>7.1f} km  {r['label']}")
    print("\nLugares con más llegadas y salidas:")
    for lugar in sorted(lugares.values(), key=lambda l: -l["extremos"])[:10]:
        print(f"  {lugar['clave']:<10} {lugar['nombre']:<28} {lugar['extremos']:>6}  "
              f"({lugar['latitud']}, {lugar['longitud']})")
    return 0



# ==============================================================================================
# PARTE 8 · ALERTAS EN VIVO
# Cada minuto: los reportes de las últimas ALERTA_VENTANA_HORAS de cada camión del catálogo se
# comparan con las rutas de la versión activa. Una alerta es un camión en movimiento, fuera de
# las faenas, en una ruta mínima (N5) o fuera de ruta (N6) durante al menos ALERTA_REPORTES
# reportes y ALERTA_MIN_MINUTOS minutos, que se aleja ALERTA_MIN_KM km o más de una ruta N1–N4.
# Termina al volver a una ruta N1–N4 o entrar a una faena. Se guarda en alertas_ruta: mientras
# sigue abierta se actualiza; al terminar queda fija (aunque el modelo aprenda después esa ruta).
# ==============================================================================================


ALERTA_REPORTES = int(os.environ.get("VIGILANCIA_REPORTES", "3"))
ALERTA_MIN_MINUTOS = float(os.environ.get("VIGILANCIA_MIN_MINUTOS", "10"))
ALERTA_MIN_KM = float(os.environ.get("VIGILANCIA_MIN_KM", "2"))
ALERTA_ACTIVA_MIN = 30        # sin reportes nuevos en 30 min, la alerta queda "sin señal"
ALERTA_CORTE_HORAS = 6        # más de 6 h sin reportes separa dos episodios
ALERTA_VENTANA_HORAS = 24     # reportes que se revisan en cada pasada
VEL_MOVIMIENTO_KMH = 5
MAX_ESTELA = 160
ALERTA_SALTO_MIN = 5         # minutos sin reportes que cortan el camino dibujado (igual que un recorrido)


def _en_movimiento(punto, anterior):
    if punto[3] is not None:
        return punto[3] >= VEL_MOVIMIENTO_KMH
    if anterior is None:
        return False
    horas = (punto[0] - anterior[0]).total_seconds() / 3600
    return horas > 0 and rm.distancia_km(anterior[1], anterior[2], punto[1], punto[2]) / horas >= VEL_MOVIMIENTO_KMH


def episodios(puntos, mapa, faenas):
    """Tramos del camión fuera de las rutas N1–N4: [(puntos, cerrado, movimientos)]."""
    resultado, actual, movimientos = [], None, 0
    anterior = None
    for punto in puntos:
        if anterior is not None and actual and (punto[0] - anterior[0]).total_seconds() > ALERTA_CORTE_HORAS * 3600:
            resultado.append((actual, True, movimientos))
            actual, movimientos = None, 0
        _, lat, lon, _ = punto
        if rm.en_faena(faenas, lat, lon) or mapa.habitual(lat, lon):
            if actual:
                resultado.append((actual, True, movimientos))
            actual, movimientos = None, 0
        else:
            moviendo = _en_movimiento(punto, anterior)
            if actual is None and moviendo:
                actual, movimientos = [], 0
            if actual is not None:
                actual.append(punto)
                movimientos += moviendo
        anterior = punto
    if actual:
        resultado.append((actual, False, movimientos))
    return [(e, cerrado, n) for e, cerrado, n in resultado if n >= ALERTA_REPORTES]


def _identidad(cursor, asset_id, hasta):
    """Nombre, conductor y ubicación informada en el último reporte hasta ese momento."""
    for tabla in ("telemetria_api", "raw_gps"):
        try:
            cursor.execute(
                f"""SELECT asset_name, driver_name, driver_last_name, location FROM {tabla}
                    WHERE asset_id=%s AND report_date <= %s ORDER BY report_date DESC LIMIT 1""",
                (asset_id, hasta),
            )
        except Exception:  # tabla aún no creada
            continue
        fila = cursor.fetchone()
        if fila:
            conductor = " ".join(filter(None, (fila.get("driver_name"), fila.get("driver_last_name")))).strip()
            return {"nombre": fila.get("asset_name"), "conductor": conductor or None, "ubicacion": fila.get("location")}
    return {"nombre": None, "conductor": None, "ubicacion": None}


def _estela(puntos):
    """Camino seguido durante la alerta, en tramos [[[lat, lon], …], …]. Se corta donde hay un
    hueco de más de ALERTA_SALTO_MIN entre reportes o un salto de GPS, igual que los recorridos:
    así no se dibuja una recta por donde no hay datos."""
    tramos = [[puntos[0]]]
    for a, b in zip(puntos, puntos[1:]):
        horas = (b[0] - a[0]).total_seconds() / 3600
        km = rm.distancia_km(a[1], a[2], b[1], b[2])
        if horas * 60 > ALERTA_SALTO_MIN or (horas > 0 and km / horas > rm.VEL_MAX_KMH):
            tramos.append([])
        tramos[-1].append(b)
    paso = max(1, math.ceil(len(puntos) / MAX_ESTELA))
    salida = []
    for tramo in tramos:
        elegidos = tramo[::paso]
        if elegidos[-1] is not tramo[-1]:
            elegidos.append(tramo[-1])
        salida.append([[round(p[1], 5), round(p[2], 5)] for p in elegidos])
    return salida


def detectar_alertas(cursor, por_camion, mapa, etiquetas, faenas, ahora):
    """Alertas de los reportes dados: [{asset_id, inicio, fin, estado, nivel, …}] (fechas UTC)."""
    lista = []
    for asset_id, puntos in por_camion.items():
        for episodio, cerrado, _ in episodios(puntos, mapa, faenas):
            inicio, fin = episodio[0][0], episodio[-1][0]
            minutos = (fin - inicio).total_seconds() / 60
            if minutos < ALERTA_MIN_MINUTOS:
                continue  # desvío corto: no alcanza a ser alerta
            muestra = episodio[::max(1, len(episodio) // 40)] + [episodio[-1]]
            distancias = [mapa.distancia(p[1], p[2]) for p in muestra]
            lejos = None if any(d is None for d in distancias) else max(distancias)
            if lejos is not None and lejos < ALERTA_MIN_KM:
                continue  # anduvo a menos de ALERTA_MIN_KM de una ruta N1–N4: no es un desvío real
            if cerrado:
                estado = "terminada"
            else:
                estado = "activa" if (ahora - fin).total_seconds() <= ALERTA_ACTIVA_MIN * 60 else "sin_senal"
            ultimo = episodio[-1]
            donde = [mapa.en(p[1], p[2]) for p in episodio]
            minimas = Counter(ruta for numero, ruta in donde if numero == 5)
            ruta = minimas.most_common(1)[0][0] if minimas else None
            cercana = mapa.cercana(ultimo[1], ultimo[2])  # para dibujar la ruta habitual más cercana
            alerta = {
                "asset_id": asset_id, "inicio": inicio, "fin": fin, "estado": estado,
                "nivel": "N6" if any(numero == rm.FUERA for numero, _ in donde) else "N5",
                "route_id": ruta, "ruta_label": etiquetas.get(ruta),
                "minutos": round(minutos), "reportes": len(episodio),
                "km_recorridos": round(sum(rm.distancia_km(a[1], a[2], b[1], b[2])
                                           for a, b in zip(episodio, episodio[1:])), 1),
                "km_ruta_habitual": None if lejos is None else round(lejos, 1),
                "velocidad": ultimo[3], "latitud": round(ultimo[1], 5), "longitud": round(ultimo[2], 5),
                "corredor": cercana, "estela": _estela(episodio),
            }
            alerta.update(_identidad(cursor, asset_id, fin))
            lista.append(alerta)
    return lista


def guardar_alertas(connection, alertas, ahora, version_id):
    """Guarda las alertas detectadas. Una alerta es la misma si es del mismo camión y se cruza en
    el tiempo con una guardada (así no se duplica aunque la ventana corte su inicio). Las
    terminadas no se tocan más. Las abiertas que ya no aparecen se cierran.
    Devuelve {"nuevas", "actualizadas", "cerradas"}."""
    resultado = {"nuevas": 0, "actualizadas": 0, "cerradas": 0}
    desde = ahora - timedelta(hours=ALERTA_VENTANA_HORAS + ALERTA_CORTE_HORAS)
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id, asset_id, inicio, fin, estado FROM alertas_ruta
               WHERE estado IN ('activa', 'sin_senal') OR fin >= %s""", (desde,))
        guardadas = defaultdict(list)
        for fila in cursor.fetchall():
            guardadas[fila["asset_id"]].append(fila)
        vistas = set()
        for a in alertas:
            previa = next((g for g in guardadas[a["asset_id"]]
                           if g["inicio"] <= a["fin"] and g["fin"] >= a["inicio"]), None)
            corredor = a["corredor"]
            valores = (a.get("nombre"), a.get("conductor"),
                       a.get("ubicacion"), a["fin"], a["estado"], a["nivel"], a["route_id"],
                       (a["ruta_label"] or "")[:255] or None, a["minutos"], a["reportes"], a["km_recorridos"],
                       a["km_ruta_habitual"], a["velocidad"], a["latitud"], a["longitud"],
                       corredor[1][0] if corredor else None, corredor[1][1] if corredor else None,
                       round(corredor[0], 1) if corredor else None,
                       json.dumps(a["estela"], separators=(",", ":")), version_id, ahora)
            if previa:
                vistas.add(previa["id"])
                if previa["estado"] == "terminada":
                    continue  # ya cerrada: queda tal como se registró
                cursor.execute(
                    """UPDATE alertas_ruta SET asset_name=%s, conductor=%s, ubicacion=%s, fin=%s, estado=%s,
                       nivel=%s, route_id=%s, ruta_label=%s, minutos=%s, reportes=%s, km_recorridos=%s,
                       km_ruta_habitual=%s, velocidad=%s, latitud=%s, longitud=%s, corredor_lat=%s,
                       corredor_lon=%s, corredor_km=%s, estela=%s, version_modelo=%s, actualizada_en=%s
                       WHERE id=%s""", (*valores, previa["id"]))
                resultado["actualizadas"] += 1
            else:
                cursor.execute(
                    """INSERT IGNORE INTO alertas_ruta (asset_name, conductor, ubicacion, fin, estado, nivel,
                       route_id, ruta_label, minutos, reportes, km_recorridos, km_ruta_habitual, velocidad,
                       latitud, longitud, corredor_lat, corredor_lon, corredor_km, estela, version_modelo,
                       actualizada_en, asset_id, inicio, detectada_en)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (*valores, a["asset_id"], a["inicio"], ahora))
                resultado["nuevas"] += 1
        for filas in guardadas.values():
            for g in filas:
                if g["estado"] != "terminada" and g["id"] not in vistas:
                    cursor.execute("UPDATE alertas_ruta SET estado='terminada', actualizada_en=%s WHERE id=%s",
                                   (ahora, g["id"]))
                    resultado["cerradas"] += 1
    connection.commit()
    return resultado


# ==============================================================================================
# PARTE 9 · CORTES DE SEÑAL
# Un camión en movimiento, fuera de las faenas y de las rutas N1–N4, que pasa más de CORTE_MIN
# minutos sin una posición nueva. Se guarda en cortes_senal con el último reporte antes del
# corte (hora, lugar, velocidad, motor, evento) y el primero después (hora y lugar), la distancia
# en línea recta entre ambos y la velocidad media que habría necesitado.
#
# El tipo sale de las copias del último reporte. La API entrega el mismo reporte (misma hora y
# posición, distinto id) cada vez que el equipo transmite sin una posición GPS nueva:
#   gps_congelado  siguieron llegando copias durante CORTE_MIN minutos o más: el equipo tenía
#                  conexión, pero el GPS no daba posición (antena desconectada o tapada, falla).
#   sin_datos      no llegó nada: sin cobertura, equipo apagado o sin energía.
# Es un indicio para revisar con la empresa, no una prueba. Mientras no reaparece, el corte
# queda abierto (hasta = NULL) y se completa solo cuando llega la posición siguiente.
# ==============================================================================================

CORTE_MIN = ALERTA_SALTO_MIN
CORTE_COPIAS = 3


def detectar_cortes(por_camion, mapa, faenas, ahora):
    """Cortes de señal fuera de las rutas habituales: [{asset_id, desde, hasta, …}] (fechas UTC)."""
    cortes = []
    for asset_id, puntos in por_camion.items():
        anterior = None
        for i, punto in enumerate(puntos):
            siguiente = puntos[i + 1] if i + 1 < len(puntos) else None
            hasta = siguiente[0] if siguiente else ahora
            _, lat, lon, velocidad = punto
            if ((hasta - punto[0]).total_seconds() > CORTE_MIN * 60 and _en_movimiento(punto, anterior)
                    and not rm.en_faena(faenas, lat, lon) and not mapa.habitual(lat, lon)):
                corte = {"asset_id": asset_id, "desde": punto[0], "hasta": siguiente and siguiente[0],
                         "minutos": round((hasta - punto[0]).total_seconds() / 60),
                         "lat_antes": round(lat, 5), "lon_antes": round(lon, 5), "velocidad_antes": velocidad,
                         "lat_despues": None, "lon_despues": None, "km_linea": None, "kmh_necesaria": None}
                if siguiente:
                    km = rm.distancia_km(lat, lon, siguiente[1], siguiente[2])
                    corte.update(lat_despues=round(siguiente[1], 5), lon_despues=round(siguiente[2], 5),
                                 km_linea=round(km, 1), kmh_necesaria=round(km / (corte["minutos"] / 60 or 1), 0))
                cortes.append(corte)
            anterior = punto
    return cortes


def _copias(cursor, corte):
    """Copias recibidas del último reporte antes del corte, su motor y evento, y el tipo de corte."""
    try:
        cursor.execute(
            """SELECT COUNT(*) AS copias, MIN(recibido_en) AS primera, MAX(recibido_en) AS ultima,
                      MAX(ignition) AS encendido, MAX(event_name) AS evento
               FROM telemetria_api WHERE asset_id=%s AND report_date=%s""",
            (corte["asset_id"], corte["desde"]))
        fila = cursor.fetchone() or {}
    except Exception:  # tabla aún no creada o histórico sin copias
        fila = {}
    copias = fila.get("copias") or 0
    transmitiendo = (copias >= CORTE_COPIAS and fila.get("primera") and fila.get("ultima")
                     and (fila["ultima"] - fila["primera"]).total_seconds() >= CORTE_MIN * 60)
    return dict(corte, copias=copias, ultima_copia=fila.get("ultima"), encendido_antes=fila.get("encendido"),
                evento_antes=fila.get("evento"), tipo="gps_congelado" if transmitiendo else "sin_datos")


def _siguiente_posicion(cursor, asset_id, desde):
    """Primera posición nueva después de `desde` (para cerrar cortes que salieron de la ventana)."""
    cursor.execute(
        """SELECT report_date, latitude, longitude FROM telemetria_api
           WHERE asset_id=%s AND report_date > %s ORDER BY report_date LIMIT 1""", (asset_id, desde))
    return cursor.fetchone()


def guardar_cortes(connection, cortes, ahora):
    """Guarda o completa los cortes (uno por camión y hora del último reporte). Los abiertos que
    salieron de la ventana se cierran con la posición siguiente, si ya llegó. Devuelve cuántos."""
    columnas = ("hasta", "minutos", "tipo", "lat_antes", "lon_antes", "velocidad_antes", "encendido_antes",
                "evento_antes", "lat_despues", "lon_despues", "km_linea", "kmh_necesaria", "copias", "ultima_copia")
    with connection.cursor() as cursor:
        vistos = set()
        for corte in cortes:
            corte = _copias(cursor, corte)
            vistos.add((corte["asset_id"], corte["desde"]))
            valores = [corte[c] for c in columnas]
            if valores[7]:
                valores[7] = str(valores[7])[:128]
            cursor.execute(
                f"""INSERT INTO cortes_senal (asset_id, desde, {", ".join(columnas)}, detectado_en, actualizado_en)
                    VALUES (%s, %s, {", ".join(["%s"] * len(columnas))}, %s, %s)
                    ON DUPLICATE KEY UPDATE {", ".join(f"{c}=VALUES({c})" for c in columnas)},
                    actualizado_en=VALUES(actualizado_en)""",
                (corte["asset_id"], corte["desde"], *valores, ahora, ahora))
        cursor.execute("SELECT asset_id, desde, lat_antes, lon_antes FROM cortes_senal WHERE hasta IS NULL")
        for abierto in cursor.fetchall():
            if (abierto["asset_id"], abierto["desde"]) in vistos:
                continue
            fila = _siguiente_posicion(cursor, abierto["asset_id"], abierto["desde"])
            if fila:
                minutos = round((fila["report_date"] - abierto["desde"]).total_seconds() / 60)
                km = rm.distancia_km(abierto["lat_antes"], abierto["lon_antes"], fila["latitude"], fila["longitude"])
                cursor.execute(
                    """UPDATE cortes_senal SET hasta=%s, minutos=%s, lat_despues=%s, lon_despues=%s, km_linea=%s,
                       kmh_necesaria=%s, actualizado_en=%s WHERE asset_id=%s AND desde=%s""",
                    (fila["report_date"], minutos, round(fila["latitude"], 5), round(fila["longitude"], 5),
                     round(km, 1), round(km / (minutos / 60 or 1)), ahora, abierto["asset_id"], abierto["desde"]))
            else:
                cursor.execute("UPDATE cortes_senal SET minutos=%s, actualizado_en=%s WHERE asset_id=%s AND desde=%s",
                               (round((ahora - abierto["desde"]).total_seconds() / 60), ahora,
                                abierto["asset_id"], abierto["desde"]))
    connection.commit()
    return len(cortes)


_VIGILANCIA = {"version": None, "mapa": None, "etiquetas": {}}


def vigilar(connection, ahora=None):
    """Una pasada de alertas y cortes de señal (la llama el servicio cada minuto). None si aún no hay modelo."""
    ahora = ahora or datetime.now(timezone.utc).replace(tzinfo=None)
    with connection.cursor() as cursor:
        version = rm.version_activa(cursor)
        if version is None:
            connection.commit()
            return None
        if _VIGILANCIA["version"] != version:
            rutas = rm.leer_rutas(cursor, version[0])
            _VIGILANCIA.update(version=version, mapa=rm.MapaRutas(rutas),
                               etiquetas={r["id"]: r["label"] for r in rutas})
        por_camion = rm.leer_reportes(cursor, ahora - timedelta(hours=ALERTA_VENTANA_HORAS), ahora,
                                      rm.catalogo(cursor))
        faenas = rm.leer_faenas(cursor)
        alertas = detectar_alertas(cursor, por_camion, _VIGILANCIA["mapa"], _VIGILANCIA["etiquetas"], faenas, ahora)
    connection.commit()
    resultado = guardar_alertas(connection, alertas, ahora, version[0])
    resultado["cortes"] = guardar_cortes(connection, detectar_cortes(por_camion, _VIGILANCIA["mapa"], faenas, ahora), ahora)
    return resultado


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
