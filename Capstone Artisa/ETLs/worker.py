"""MongoDB (gps_data.raw_gps) -> ETL -> MariaDB. Sondea cada TELEMETRY_INTERVAL_SECONDS."""

import os
import threading
import time
from urllib.parse import urlsplit

from common.db import connect_db, initialize_db
from common.http import JsonHandler, serve
from common.vehiculos import sincronizar_desde_raw_gps
from ETLs.telemetry import TelemetryConfig, record_error, run_once


INTERVAL = int(os.environ.get("TELEMETRY_INTERVAL_SECONDS", "30"))
STATE = {"last_success": 0.0, "error": None, "estado": "iniciando"}


def sync_catalog():
    """Clasifica una vez los vehiculos ya cargados en raw_gps que faltan en el catalogo.
    Un fallo aqui no detiene el ETL: se reintenta en el proximo arranque."""
    try:
        with connect_db(read_timeout=300) as connection, connection.cursor() as cursor:
            nuevos = sincronizar_desde_raw_gps(cursor)
        if nuevos:
            print(f"ETL: {nuevos} vehiculos agregados al catalogo", flush=True)
    except Exception as error:
        print(f"ETL: no se pudo sincronizar el catalogo ({type(error).__name__}: {error})", flush=True)


def loop():
    initialized = False
    config = None
    while True:
        try:
            if not initialized:
                STATE.update(error=None, estado="iniciando")
                initialize_db()
                config = TelemetryConfig.from_env()
                initialized = True
                # La base histórica tiene millones de reportes. Esta compatibilidad para
                # catálogos antiguos puede tardar varios minutos y no debe bloquear la
                # primera revisión del ETL ni dejar su healthcheck en 503.
                threading.Thread(target=sync_catalog, daemon=True).start()
            STATE.update(error=None, estado="ejecutando")
            result = run_once(config)
            STATE.update(last_success=time.monotonic(), error=None, **result)
            if result["recibidos"]:
                print(
                    f"ETL: {result['validos']} guardados, {result['rechazados']} descartados; "
                    f"total {result['total_validos']} ({result['estado']})",
                    flush=True,
                )
            if result["estado"] != "esperando":
                continue  # aun hay mas datos atrasados en Mongo: sigue drenando sin esperar
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            STATE.update(error=message, estado="error")
            try:
                if initialized:
                    record_error(message)
            except Exception:
                pass
            print(f"ETL: ciclo fallido ({message})", flush=True)
        time.sleep(INTERVAL)


class Handler(JsonHandler):
    def do_GET(self):
        if urlsplit(self.path).path != "/health":
            self.respond(404, {"error": "Ruta no encontrada"})
            return
        # Este endpoint comprueba que el proceso está vivo. En la primera carga el trabajo
        # puede tardar, pero el servicio ya está listo para responder y no debe impedir que
        # `docker compose up --wait` termine.
        ready = STATE["error"] is None
        state = {key: value for key, value in STATE.items() if key != "last_success"}
        self.respond(200 if ready else 503, {"servicio": "etl", "status": "ok" if ready else "unavailable",
                                                "telemetria": state})


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    serve(Handler, "ETL")
