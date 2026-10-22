"""Servicio de cierre diario (00:00 hora Chile).

Trabajo pesado una vez al día a la hora DIARIO_HORA (Chile, por defecto 00:30): cierra
los días pendientes, recalcula los que recibieron reportes atrasados y reentrena el modelo
de rutas (machine_learning/modelo_rutas.py) con el día recién cerrado. Se puede lanzar a mano con --ahora. Entre medio, cada minuto
solo actualiza el resumen "en curso" de hoy (liviano).

1. Hoy: recalcula el resumen "en curso" cada DIARIO_HOY_CADA_MINUTOS.
2. Días pasados sin cerrar: se cierran cuando pasó la medianoche + DIARIO_GRACIA_MINUTOS
   y la API de la empresa respondió después de ese momento (así se alcanza a recibir el
   último minuto del día). Si la API está desconectada, el día queda "esperando datos"
   con cifras provisorias y se cierra cuando vuelva la conexión.
3. Datos atrasados: si después del cierre llega un reporte de un día ya cerrado
   (por un corte de red, un GPS que envía tarde, etc.), el día se recalcula solo.
   Se revisan los últimos DIARIO_REVISAR_DIAS días.

Si el servicio estuvo apagado varios días, prioriza los recientes y recupera el resto.
Publica clasificaciones intermedias para no bloquear la web durante toda la recuperación.
"""

from datetime import date, datetime, timedelta, timezone
import os
import threading
import time
from urllib.parse import urlsplit

from common import dias
from common.db import connect_db, initialize_db
from common.http import JsonHandler, serve
from machine_learning import modelo_rutas
from machine_learning.diario import VERSION_CALCULO, procesar_dia, quitar_no_camiones, ultimo_recibido


GRACIA = timedelta(minutes=int(os.environ.get("DIARIO_GRACIA_MINUTOS", "15")))
HOY_CADA = timedelta(minutes=int(os.environ.get("DIARIO_HOY_CADA_MINUTOS", "10")))
REVISAR_DIAS = int(os.environ.get("DIARIO_REVISAR_DIAS", "7"))
MAX_DIAS_POR_CICLO = int(os.environ.get("DIARIO_MAX_DIAS_POR_CICLO", "3"))
DESDE = os.environ.get("DIARIO_DESDE", "").strip()
INTERVALO = 60
# Hora (Chile, HH:MM) en que corre el trabajo pesado: cerrar días, recalcular atrasados y
# clasificar rutas. Vacío = continuo, como antes. Fuera de esa hora solo se actualiza "hoy".
HORA_PROGRAMADA = os.environ.get("DIARIO_HORA", "00:30").strip()
LOCK_NAME = "artisa_cierre_diario"
RUTAS_REVISADAS = {"value": float("-inf")}  # revisar a lo más cada 15 min
STATE = {"estado": "iniciando", "error": None, "ultimo_ciclo": None, "dias_procesados": 0}
HEARTBEAT = {"value": time.monotonic()}


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def primer_dia(cursor):
    """Primer día a procesar: DIARIO_DESDE si está definido; si no, el primer día con
    datos, sea del histórico (raw_gps) o de la API de la empresa (telemetria_api)."""
    if DESDE:
        return date.fromisoformat(DESDE)
    primeros = []
    for tabla in ("telemetria_api", "raw_gps"):  # MIN por índice de fecha: inmediato
        try:
            cursor.execute(f"SELECT MIN(report_date) AS primero FROM {tabla}")
        except Exception:
            continue
        fila = cursor.fetchone() or {}
        if fila.get("primero"):
            primeros.append(fila["primero"])
    return dias.dia_local(min(primeros)) if primeros else None


def estado_conector(cursor):
    """('sin_api', None) si la API no está configurada; si no ('api', ultimo_exito)."""
    try:
        cursor.execute("SELECT estado, ultimo_exito FROM telemetria_api_estado WHERE id=1")
    except Exception:
        return "sin_api", None
    fila = cursor.fetchone()
    if not fila or fila["estado"] == "sin_configurar":
        return "sin_api", None
    # Mientras recupera reportes atrasados no se puede asegurar que un día esté completo.
    return "api", fila["ultimo_exito"] if fila["estado"] == "conectado" else None


def dia_listo_para_cerrar(dia, utc_now, modo, ultimo_exito):
    _, fin = dias.limites_utc(dia)
    if utc_now < fin + GRACIA:
        return False
    if modo == "sin_api":
        return True
    return ultimo_exito is not None and ultimo_exito >= fin + GRACIA


def ciclo(connection, utc_now, procesar=procesar_dia, completo=True, revisados=None):
    """Un ciclo de trabajo. Devuelve la lista de (día, estado) procesados.
    completo=False solo actualiza el día de hoy (trabajo liviano fuera de la hora programada)."""
    hecho = []
    revisados = revisados if revisados is not None else set()

    def calcular(dia, estado, recalculo=False):
        HEARTBEAT["value"] = time.monotonic()
        STATE.update(estado="procesando", fecha_en_proceso=dia.isoformat())
        procesar(connection, dia, estado, recalculo=recalculo)
        HEARTBEAT["value"] = time.monotonic()
        STATE.update(ultimo_dia_procesado=dia.isoformat(), fecha_en_proceso=None)
        if dia < hoy:
            revisados.add(dia)
    with connection.cursor() as cursor:
        inicio = primer_dia(cursor)
        modo, ultimo_exito = estado_conector(cursor)
        if inicio is None:
            return hecho
        cursor.execute(
            "SELECT fecha, estado, calculado_en, ultimo_recibido, version_calculo "
            "FROM reporte_diario_estado WHERE fecha >= %s",
            (inicio,),
        )
        estados = {fila["fecha"]: fila for fila in cursor.fetchall()}
    connection.commit()
    hoy = dias.hoy(utc_now)

    # 1) Primero los días recientes. Cada día se visita una sola vez por ejecución,
    # incluso si la API sigue sincronizando: no repetir el principio del histórico.
    dia = hoy - timedelta(days=1)
    while completo and dia >= inicio and len(hecho) < MAX_DIAS_POR_CICLO:
        if dia in revisados:
            dia -= timedelta(days=1)
            continue
        fila = estados.get(dia)
        if fila and fila["estado"] == "cerrado" and fila.get("version_calculo", VERSION_CALCULO) < VERSION_CALCULO:
            # Día cerrado con un método anterior: se recalcula y sigue cerrado.
            calcular(dia, "cerrado")
            hecho.append((dia, "actualizado"))
        elif not fila or fila["estado"] != "cerrado":
            if dia_listo_para_cerrar(dia, utc_now, modo, ultimo_exito):
                calcular(dia, "cerrado")
                hecho.append((dia, "cerrado"))
            elif not fila or fila["calculado_en"] is None or fila["calculado_en"] <= utc_now - HOY_CADA:
                calcular(dia, "esperando_datos")
                hecho.append((dia, "esperando_datos"))
        dia -= timedelta(days=1)

    # 2) Hoy, en curso.
    fila = estados.get(hoy)
    if HOY_CADA.total_seconds() > 0 and hoy >= inicio and (not fila or fila["calculado_en"] is None or fila["calculado_en"] <= utc_now - HOY_CADA):
        calcular(hoy, "en_curso")
        hecho.append((hoy, "en_curso"))

    # 3) Reportes que llegaron tarde a días ya cerrados.
    for atras in range(1, (REVISAR_DIAS if completo else 0) + 1):
        dia = hoy - timedelta(days=atras)
        if dia in revisados:
            continue
        fila = estados.get(dia)
        if not fila or fila["estado"] != "cerrado":
            continue
        with connection.cursor() as cursor:
            recibido = ultimo_recibido(cursor, dia)
        connection.commit()
        if recibido and (fila["ultimo_recibido"] is None or recibido > fila["ultimo_recibido"]):
            calcular(dia, "cerrado", recalculo=True)
            hecho.append((dia, "recalculado"))
    return hecho


def _hora_programada():
    if not HORA_PROGRAMADA:
        return None
    try:
        horas, minutos = (int(x) for x in HORA_PROGRAMADA.split(":"))
        if not (0 <= horas < 24 and 0 <= minutos < 60):
            raise ValueError
        return horas, minutos
    except ValueError:
        print(f"Cierre diario: DIARIO_HORA inválida ({HORA_PROGRAMADA!r}); se usa modo continuo", flush=True)
        return None


def toca_programado(utc_now, ultima_fecha):
    """True si ya pasó la hora programada de hoy (hora Chile) y aún no se ejecutó hoy."""
    hora = _hora_programada()
    if hora is None:
        return True
    local = utc_now.replace(tzinfo=timezone.utc).astimezone(dias.zona())
    return (local.hour, local.minute) >= hora and ultima_fecha != local.date()


def publicar_rutas(connection, final=False):
    """Publica lo ya cerrado; durante la recuperación, como máximo cada cinco minutos."""
    with connection.cursor() as cursor:
        recalcular = modelo_rutas.necesita_recalculo(cursor, horas_minimas=0 if final else 5 / 60)
    connection.commit()
    if recalcular:
        STATE["estado"] = "clasificando"
        HEARTBEAT["value"] = time.monotonic()
        STATE["rutas"] = modelo_rutas.ejecutar(connection)
        HEARTBEAT["value"] = time.monotonic()


def cierres_listos(connection, utc_now):
    """Al terminar la carga, cierra los días provisorios sin esperar otra medianoche."""
    with connection.cursor() as cursor:
        modo, ultimo_exito = estado_conector(cursor)
        cursor.execute(
            "SELECT fecha FROM reporte_diario_estado WHERE fecha < %s "
            "AND (estado <> 'cerrado' OR version_calculo < %s)",
            (dias.hoy(utc_now), VERSION_CALCULO),
        )
        pendientes = cursor.fetchall()
    connection.commit()
    return any(dia_listo_para_cerrar(fila["fecha"], utc_now, modo, ultimo_exito) for fila in pendientes)


def ejecucion_completa(connection, utc_now):
    """Recupera días pendientes y publica rutas por lotes. Devuelve días procesados."""
    total = 0
    revisados = set()
    quitar_no_camiones(connection)
    while True:
        HEARTBEAT["value"] = time.monotonic()
        hecho = ciclo(connection, utc_now, completo=True, revisados=revisados)
        total += len(hecho)
        STATE["dias_procesados_ejecucion"] = total
        terminado = len(hecho) < MAX_DIAS_POR_CICLO
        publicar_rutas(connection, final=terminado)
        if terminado:
            break
        utc_now = _utc_now()
    return total


def publicar_alertas(connection, utc_now):
    """Alertas de ruta en vivo (Parte 8 del modelo). Un fallo aquí no detiene el cierre diario."""
    try:
        resultado = modelo_rutas.vigilar(connection, utc_now)
        STATE["alertas"] = resultado or {"estado": "sin modelo de rutas"}
        if resultado and (resultado["nuevas"] or resultado["cerradas"]):
            print(f"Alertas: {resultado['nuevas']} nuevas, {resultado['cerradas']} cerradas"
                  f" · {resultado.get('cortes', 0)} cortes de señal en curso o recientes", flush=True)
    except Exception as error:
        try:
            connection.rollback()
        except Exception:
            pass
        STATE["alertas"] = {"error": f"{type(error).__name__}: {error}"[:255]}
        print(f"Alertas: no se pudieron revisar ({STATE['alertas']['error']})", flush=True)


def ciclo_alertas():
    """Hace una revisión de alertas con una conexión independiente del cierre histórico."""
    try:
        with connect_db(autocommit=False, read_timeout=600) as connection:
            publicar_alertas(connection, _utc_now())
    except Exception as error:
        STATE["alertas"] = {"error": f"{type(error).__name__}: {error}"[:255]}
        print(f"Alertas: ciclo fallido ({STATE['alertas']['error']})", flush=True)


def loop_alertas():
    """Revisa las alertas cada minuto sin esperar al cierre histórico.

    Recuperar muchos días pendientes puede tardar bastante. La vigilancia usa su propia
    conexión para que ese trabajo no deje el panel en vivo sin alertas durante horas.
    """
    while True:
        ciclo_alertas()
        time.sleep(INTERVALO)


def _con_bloqueo(connection, funcion, espera=0):
    """Evita que el servicio y una ejecución manual procesen a la vez.

    La espera se hace en tramos de 30 s: una sola espera larga en MariaDB superaría el
    límite de lectura de la conexión y la cortaría (error 2013) aunque nada haya fallado.
    """
    limite = time.monotonic() + espera
    intentos = 0
    while True:
        tramo = max(0, min(30, int(limite - time.monotonic())))
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s) AS ok", (LOCK_NAME, tramo))
            if (cursor.fetchone() or {}).get("ok"):
                break
        if time.monotonic() >= limite:
            return None
        intentos += 1
        if intentos % 10 == 0:
            print(f"El servicio sigue trabajando; esperando… ({intentos // 2} min)", flush=True)
    try:
        return funcion()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))


def loop():
    hora = _hora_programada()
    print(f"Cierre diario: zona {dias.ZONA}, desde {DESDE or 'el primer dato de la API de la empresa'}, "
          f"gracia {GRACIA}, " + (f"trabajo completo diario a las {hora[0]:02d}:{hora[1]:02d} (Chile)"
                                  if hora else "modo continuo"), flush=True)
    initialized = False
    ultima_programada = None
    while True:
        HEARTBEAT["value"] = time.monotonic()
        try:
            if not initialized:
                initialize_db()  # idempotente: crea las tablas del cierre si faltan
                initialized = True
            ahora = _utc_now()
            # Leer un día completo del histórico puede tomar más que el límite normal de 15 s.
            with connect_db(autocommit=False, read_timeout=600) as connection:
                if toca_programado(ahora, ultima_programada) or cierres_listos(connection, ahora):
                    print("Cierre diario: comienza el trabajo programado", flush=True)
                    total = _con_bloqueo(connection, lambda: ejecucion_completa(connection, ahora))
                    if total is not None:
                        ultima_programada = ahora.replace(tzinfo=timezone.utc).astimezone(dias.zona()).date()
                        STATE["dias_procesados"] += total
                        STATE["ultima_ejecucion_programada"] = ahora.isoformat() + "Z"
                        print(f"Cierre diario: trabajo programado terminado ({total} días)", flush=True)
                else:
                    hecho = _con_bloqueo(connection, lambda: ciclo(connection, ahora, completo=False)) or []
                    STATE["dias_procesados"] += len(hecho)
                    # Primer modelo de rutas, día cerrado nuevo o método nuevo, sin esperar la
                    # hora programada (necesita_recalculo es una consulta liviana).
                    if not hecho and time.monotonic() - RUTAS_REVISADAS["value"] >= 900:
                        RUTAS_REVISADAS["value"] = time.monotonic()
                        _con_bloqueo(connection, lambda: publicar_rutas(connection, final=True))
            STATE.update(estado="ok", error=None, ultimo_ciclo=_utc_now().isoformat() + "Z")
        except Exception as error:  # MariaDB caída u otro fallo: se reintenta en el próximo ciclo
            STATE.update(estado="error", error=f"{type(error).__name__}: {error}"[:255])
            print(f"Cierre diario: ciclo fallido ({STATE['error']})", flush=True)
        time.sleep(INTERVALO)


def ejecutar_ahora():
    """Ejecución manual del trabajo completo, sin esperar la hora programada:
        docker compose exec machine-diario python -m machine_learning.diario_worker --ahora
    """
    initialize_db()
    with connect_db(autocommit=False, read_timeout=900) as connection:
        print("Esperando a que termine el trabajo en curso del servicio (si hay)…", flush=True)
        total = _con_bloqueo(connection, lambda: ejecucion_completa(connection, _utc_now()), espera=3600)
    if total is None:
        print("El servicio sigue ocupado; inténtalo más tarde.")
        return 1
    print(f"Listo: {total} días procesados y rutas actualizadas si correspondía.")
    return 0


class Handler(JsonHandler):
    def do_GET(self):
        if urlsplit(self.path).path != "/health":
            self.respond(404, {"error": "Ruta no encontrada"})
            return
        alive = time.monotonic() - HEARTBEAT["value"] < 900
        self.respond(200 if alive else 503, {"servicio": "machine-diario",
                                             "status": "ok" if alive else "unavailable",
                                             "zona": dias.ZONA, "cierre": dict(STATE)})


def watchdog(worker):
    while True:
        time.sleep(30)
        if not worker.is_alive() or time.monotonic() - HEARTBEAT["value"] > 3600:
            print("Cierre diario: el ciclo se detuvo; reiniciando el contenedor", flush=True)
            os._exit(1)


if __name__ == "__main__":
    import sys
    if "--ahora" in sys.argv:
        raise SystemExit(ejecutar_ahora())
    # La vigilancia va en un hilo independiente: el cierre puede tardar horas al recuperar
    # un histórico grande, pero las alertas deben seguir actualizándose cada minuto.
    alert_worker = threading.Thread(target=loop_alertas, daemon=True, name="alertas-en-vivo")
    alert_worker.start()
    worker = threading.Thread(target=loop, daemon=True, name="cierre-diario")
    worker.start()
    threading.Thread(target=watchdog, args=(worker,), daemon=True).start()
    serve(Handler, "Cierre diario")
