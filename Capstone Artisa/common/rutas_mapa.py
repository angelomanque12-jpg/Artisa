"""Rutas aprendidas en vivo: lo que comparten Machine (detección de alertas) y la API (mapa).

- MapaRutas: por dónde pasa cada ruta de la versión activa del modelo
  (machine_learning/modelo_rutas.py), en celdas de ~500 m. Da el nivel de cualquier posición
  (N1 troncal … N5 mínima; N6 si no hay ninguna ruta a ~1 km), la distancia a la ruta habitual
  (N1–N4) más cercana y la red para dibujar (una sola línea por camino).
- Lectura de las rutas de una versión, de los reportes GPS (sin repetidos ni saltos) y de las
  faenas.

Solo lee la base.
"""

from collections import defaultdict
from datetime import datetime
import json
import math

import pymysql

ESCALA = 200                 # celda de 0,005° (~500 m): celda = floor(lat * 200)
PASO = 0.5 / ESCALA          # muestreo de las líneas de las rutas: media celda
VEL_MAX_KMH = 150            # más rápido es un salto de GPS
HABITUAL = 4                 # N1–N4: rutas por donde anda la operación
FUERA = 6                    # nivel de un camión que no está en ninguna ruta aprendida
RADIO_BUSQUEDA = 12          # celdas de 0,1° alrededor para la distancia (~130 km)
MAX_RUTAS_TRAMO = 12         # rutas que se nombran al tocar un tramo del mapa


def gps_valido(lat, lon):
    return (lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (lat == 0 and lon == 0))


def distancia_km(lat0, lon0, lat1, lon1):
    dlat = math.radians(lat1 - lat0)
    dlon = math.radians(lon1 - lon0)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat0)) * math.cos(math.radians(lat1)) * math.sin(dlon / 2) ** 2
    return 12742 * math.asin(min(1.0, math.sqrt(a)))


def celda(lat, lon):
    return math.floor(lat * ESCALA), math.floor(lon * ESCALA)


def centro(i):
    return (i + 0.5) / ESCALA


def iso(value):
    return value.isoformat() + "Z" if isinstance(value, datetime) else value


def numero_nivel(nivel):
    return int(str(nivel)[1:]) if str(nivel).startswith("N") else FUERA


# --- Lectura ---------------------------------------------------------------------------------

def catalogo(cursor):
    cursor.execute(
        """SELECT v.asset_id FROM vehiculos AS v INNER JOIN tipos_vehiculo AS t ON t.id_tipo = v.id_tipo
           WHERE t.nombre = 'camion'"""
    )
    return {fila["asset_id"] for fila in cursor.fetchall()}


def leer_reportes(cursor, desde, hasta, camiones):
    """{asset_id: [(report_date, lat, lon, speed), ...]} ordenados, sin repetidos ni saltos."""
    filas = []
    for tabla, indice in (("raw_gps", "idx_raw_gps_fecha"), ("telemetria_api", "idx_telemetria_api_fecha_recibido")):
        try:
            cursor.execute(
                f"""SELECT asset_id, report_date, latitude, longitude, speed FROM {tabla} USE INDEX ({indice})
                    WHERE report_date >= %s AND report_date <= %s""",
                (desde, hasta),
            )
        except pymysql.err.ProgrammingError:
            continue
        filas.extend(cursor.fetchall())
    por_camion = defaultdict(dict)
    for fila in filas:
        if fila["asset_id"] not in camiones:
            continue
        lat = None if fila["latitude"] is None else float(fila["latitude"])
        lon = None if fila["longitude"] is None else float(fila["longitude"])
        if not gps_valido(lat, lon):
            continue
        speed = None if fila["speed"] is None else float(fila["speed"])
        por_camion[fila["asset_id"]][fila["report_date"]] = (fila["report_date"], lat, lon, speed)
    limpios = {}
    for asset_id, por_hora in por_camion.items():
        puntos = []
        for punto in sorted(por_hora.values()):
            if puntos:
                t0, la0, lo0, _ = puntos[-1]
                horas = (punto[0] - t0).total_seconds() / 3600
                if horas <= 0:
                    continue
                if horas < 1 and distancia_km(la0, lo0, punto[1], punto[2]) / horas > VEL_MAX_KMH:
                    continue  # salto de GPS
            puntos.append(punto)
        if puntos:
            limpios[asset_id] = puntos
    return limpios


def leer_faenas(cursor):
    """[(nombre, (min_lon, min_lat, max_lon, max_lat), [anillos])] con coordenadas [lon, lat]."""
    try:
        cursor.execute("SELECT nombre, ST_AsGeoJSON(geocerca) AS geojson FROM faenas")
    except pymysql.err.ProgrammingError:
        return []
    faenas = []
    for fila in cursor.fetchall():
        if not fila.get("geojson"):
            continue
        geometria = json.loads(fila["geojson"])
        poligonos = geometria["coordinates"] if geometria["type"] == "MultiPolygon" else [geometria["coordinates"]]
        for anillos in poligonos:
            exterior = anillos[0]
            lons = [p[0] for p in exterior]
            lats = [p[1] for p in exterior]
            faenas.append((fila["nombre"], (min(lons), min(lats), max(lons), max(lats)), anillos))
    return faenas


def _dentro(lon, lat, anillo):
    dentro = False
    for (x0, y0), (x1, y1) in zip(anillo, anillo[1:] + anillo[:1]):
        if (y0 > lat) != (y1 > lat) and lon < (x1 - x0) * (lat - y0) / (y1 - y0) + x0:
            dentro = not dentro
    return dentro


def en_faena(faenas, lat, lon):
    for nombre, (x0, y0, x1, y1), anillos in faenas:
        if x0 <= lon <= x1 and y0 <= lat <= y1 and _dentro(lon, lat, anillos[0]) \
                and not any(_dentro(lon, lat, hueco) for hueco in anillos[1:]):
            return nombre
    return None


# --- Rutas aprendidas ---------------------------------------------------------------------

def muestras_tramo(la0, lo0, la1, lo1):
    pasos = max(1, math.ceil(max(abs(la1 - la0), abs(lo1 - lo0)) / PASO))
    return [(la0 + (la1 - la0) * k / pasos, lo0 + (lo1 - lo0) * k / pasos) for k in range(pasos + 1)]


def lineas(geometria):
    coordenadas = geometria["coordinates"]
    if geometria["type"] == "LineString":
        coordenadas = [coordenadas]
    return [[[round(lat, 5), round(lon, 5)] for lon, lat in linea] for linea in coordenadas if len(linea) >= 2]


class MapaRutas:
    """Dónde pasa cada ruta aprendida: por celda, el mejor nivel (el número más bajo) y su ruta.

    También arma la red para dibujar: muchas rutas comparten la misma carretera (una por cada
    par de lugares), y dibujarlas una encima de otra se ve como un manojo de líneas. La red tiene
    un solo tramo por par de celdas vecinas recorridas, con el mejor nivel que pasa por ahí y
    qué rutas lo usan; el punto de cada celda es el promedio de por dónde pasaron las rutas, así
    queda sobre el camino y no en el centro del cuadro."""

    def __init__(self, rutas):
        self.celdas = {}
        self.sumas = defaultdict(lambda: [0.0, 0.0, 0])
        self.tramos = {}  # (celda, celda) -> [mejor nivel, {índice de ruta}]
        for k, ruta in enumerate(rutas):
            numero = numero_nivel(ruta["nivel"])
            for linea in ruta["lineas"]:
                anterior = None
                for (la0, lo0), (la1, lo1) in zip(linea, linea[1:]):
                    for lat, lon in muestras_tramo(la0, lo0, la1, lo1):
                        c = celda(lat, lon)
                        if c not in self.celdas or numero < self.celdas[c][0]:
                            self.celdas[c] = (numero, ruta["id"])
                        suma = self.sumas[c]
                        suma[0] += lat
                        suma[1] += lon
                        suma[2] += 1
                        if anterior is not None and c != anterior:
                            clave = (anterior, c) if anterior < c else (c, anterior)
                            tramo = self.tramos.setdefault(clave, [numero, set()])
                            tramo[0] = min(tramo[0], numero)
                            tramo[1].add(k)
                        anterior = c
        # Celdas de rutas N1–N4 agrupadas en cuadros de 0,1° para medir distancias rápido.
        # Y en cuadros de 0,02° (~2 km) para saber rápido si algo pasa junto a una ruta habitual.
        self.indice = defaultdict(list)
        self.indice_fino = defaultdict(list)
        for (i, j), (numero, _) in self.celdas.items():
            if numero <= HABITUAL:
                lat, lon = centro(i), centro(j)
                self.indice[(math.floor(lat * 10), math.floor(lon * 10))].append((lat, lon))
                self.indice_fino[(math.floor(lat * 50), math.floor(lon * 50))].append((lat, lon))

    def red(self, rutas, km_aparte=2.0):
        """{"nodos": [[lat, lon], …], "tramos": [[a, b, nivel, total, [rutas…], aparte], …]} con las
        rutas de cada tramo ordenadas de la más a la menos concurrida (a lo más MAX_RUTAS_TRAMO).

        aparte = 1 en un tramo mínimo (N5) que pasa a km_aparte o más de toda ruta habitual: un
        camino que de verdad casi nadie usa. Un tramo N5 más cerca suele ir por la misma carretera
        que una ruta habitual (otro trazo GPS del mismo camino) y no es un desvío; es la misma
        distancia que usan las alertas."""
        indice, nodos = {}, []
        for c, (la, lo, n) in self.sumas.items():
            indice[c] = len(nodos)
            nodos.append([round(la / n, 5), round(lo / n, 5)])
        orden = lambda k: (numero_nivel(rutas[k]["nivel"]), -rutas[k]["camiones"], k)  # noqa: E731
        tramos = []
        for (a, b), (numero, usan) in self.tramos.items():
            ia, ib = indice[a], indice[b]
            aparte = 0
            if numero == 5:
                lat = (nodos[ia][0] + nodos[ib][0]) / 2
                lon = (nodos[ia][1] + nodos[ib][1]) / 2
                aparte = 0 if self.junto_a_habitual(lat, lon, km_aparte) else 1
            tramos.append([ia, ib, numero, len(usan), sorted(usan, key=orden)[:MAX_RUTAS_TRAMO], aparte])
        return {"nodos": nodos, "tramos": tramos}

    def junto_a_habitual(self, lat, lon, km):
        """¿Hay una ruta N1–N4 a menos de km? Primero las celdas vecinas (~1 km); después los
        cuadros de ~2 km (o de ~11 km si km es mayor) alrededor."""
        if self.habitual(lat, lon):
            return True
        escala, indice = (50, self.indice_fino) if km <= 2 else (10, self.indice)
        bi, bj = math.floor(lat * escala), math.floor(lon * escala)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for la, lo in indice.get((bi + di, bj + dj), ()):
                    if distancia_km(lat, lon, la, lo) < km:
                        return True
        return False

    def en(self, lat, lon):
        """(nivel 1..6, route_id) de la mejor ruta en la celda o sus vecinas (~1 km)."""
        i, j = celda(lat, lon)
        mejor = (FUERA, None)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                encontrado = self.celdas.get((i + di, j + dj))
                if encontrado and encontrado[0] < mejor[0]:
                    mejor = encontrado
        return mejor

    def habitual(self, lat, lon):
        return self.en(lat, lon)[0] <= HABITUAL

    def cercana(self, lat, lon):
        """(km, [lat, lon]) de la ruta N1–N4 más cercana; None si no hay a ~130 km."""
        bi, bj = math.floor(lat * 10), math.floor(lon * 10)
        mejor, punto = None, None
        for radio in range(RADIO_BUSQUEDA + 1):
            if mejor is not None and (radio - 1) * 0.1 * 111 > mejor:
                break
            for di in range(-radio, radio + 1):
                for dj in range(-radio, radio + 1):
                    if max(abs(di), abs(dj)) != radio:
                        continue
                    for la, lo in self.indice.get((bi + di, bj + dj), ()):
                        d = distancia_km(lat, lon, la, lo)
                        if mejor is None or d < mejor:
                            mejor, punto = d, [round(la, 5), round(lo, 5)]
        return None if mejor is None else (mejor, punto)

    def distancia(self, lat, lon):
        return (self.cercana(lat, lon) or (None, None))[0]


def leer_rutas(cursor, version_id):
    """Rutas de una versión del modelo, de la más a la menos concurrida, con su línea [[lat, lon]]."""
    cursor.execute(
        """SELECT route_id, label, usage_level, usage_count, unique_vehicle_count, km_mediana, geometria,
                  primer_viaje, ultimo_viaje FROM rutas_clasificadas WHERE version_id=%s
           ORDER BY unique_vehicle_count DESC, usage_count DESC, route_id""",
        (version_id,),
    )
    return [{"id": f["route_id"], "label": f["label"], "nivel": f["usage_level"],
             "camiones": int(f["unique_vehicle_count"]), "recorridos": int(f["usage_count"]),
             "km": f["km_mediana"], "ultimo_viaje": iso(f["ultimo_viaje"]),
             "lineas": lineas(json.loads(f["geometria"]))} for f in cursor.fetchall()]


def version_activa(cursor):
    """(id, creado_en) de la versión activa del modelo de rutas, o None."""
    try:
        cursor.execute("SELECT id, creado_en FROM rutas_version WHERE activa=1 ORDER BY id DESC LIMIT 1")
    except pymysql.err.ProgrammingError:
        return None  # tablas aún no creadas
    fila = cursor.fetchone()
    return (fila["id"], fila["creado_en"]) if fila else None
