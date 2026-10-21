"""Frontend estático y proxy a endpoints: único puerto público."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


API = os.environ.get("API_URL", "http://endpoints:8000")
ROOT = Path(__file__).with_name("static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/mapa-base.js": ("mapa-base.js", "text/javascript; charset=utf-8"),
         "/mapa-base.css": ("mapa-base.css", "text/css; charset=utf-8"),
         "/exploracion-gps": ("gps-maps.html", "text/html; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/artisa-logo.png": ("artisa-logo.png", "image/png"),
         "/artisa-truck-box-white-green.png": ("artisa-truck-box-white-green.png", "image/png"),
         "/artisa-fleet-cover.jpg": ("artisa-fleet-cover.jpg", "image/jpeg")}


def static_cache(raw_path):
    """Los estáticos con ?v= cambian de URL en cada versión: el navegador los guarda un año.
    Los demás se revalidan cada hora. Las páginas HTML y la API nunca se guardan."""
    if "v=" in urlsplit(raw_path).query:
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600"


# Segundos que el proxy espera a endpoints. Deben ser al menos los del navegador (app.js):
# si el proxy corta antes, una consulta lenta pero válida se ve como "endpoints no disponibles".
# Nginx espera hasta 150 s.
SLOW_PREFIXES = (("/api/gps", 120), ("/api/rutas-aprendidas", 90), ("/api/mapa", 90), ("/api/vigilancia", 90),
                 ("/api/camiones", 90), ("/api/dashboard", 60), ("/api/chat", 140))


def upstream_timeout(path):
    for prefix, seconds in SLOW_PREFIXES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "-"):
            return seconds
    return 20


class Handler(BaseHTTPRequestHandler):
    # Los chequeos de salud de Docker (cada 5 s) no se registran si responden bien;
    # así los registros muestran solo lo útil. Los errores siempre se registran.
    QUIET_PATHS = {"/front-health", "/health"}

    def log_request(self, code="-", size="-"):
        path = getattr(self, "path", "").split("?", 1)[0]
        if path in self.QUIET_PATHS and isinstance(code, int) and 200 <= code < 300:
            return
        super().log_request(code, size)

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def reply(self, status, body, content_type="application/json; charset=utf-8", csp=None,
              cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or "default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' https://unpkg.com 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https://unpkg.com https://tile.openstreetmap.org https://server.arcgisonline.com; frame-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def proxy(self):
        body = None
        try:
            if self.headers.get("Transfer-Encoding"):
                self.reply(400, b'{"error":"Transfer-Encoding no soportado"}')
                return
            if self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 < length <= 4096:
                    self.reply(413 if length > 4096 else 400, b'{"error":"Tamano de cuerpo invalido"}')
                    return
                body = self.rfile.read(length)
            request = Request(API + self.path, data=body, method=self.command)
            if body is not None:
                request.add_header("Content-Type", self.headers.get("Content-Type", "application/octet-stream"))
            try:
                timeout = upstream_timeout(urlsplit(self.path).path)
                upstream = urlopen(request, timeout=timeout)
            except HTTPError as error:
                upstream = error
            with upstream:
                content = upstream.read()
                if urlsplit(self.path).path in ("/api/estado", "/health"):
                    payload = json.loads(content)
                    payload.setdefault("componentes", {})["front"] = "ok"
                    content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.reply(upstream.code, content, upstream.headers.get("Content-Type", "application/json"))
        except (URLError, TimeoutError, ConnectionError):
            self.reply(503, b'{"status":"unavailable","error":"Los endpoints no estan disponibles"}')

    def route(self):
        path = urlsplit(self.path).path
        if self.command == "GET" and path == "/front-health":
            self.reply(200, b'{"status":"ok","servicio":"front"}')
        elif path.startswith("/api/") or path == "/health":
            self.proxy()
        elif self.command == "GET" and path in FILES:
            filename, content_type = FILES[path]
            content = (ROOT / filename).read_bytes()
            if path == "/exploracion-gps":
                # La guía trae un script inline: autorizamos solo su hash exacto.
                inline = re.search(rb"<script>(.*?)</script>", content, re.DOTALL)
                if not inline:
                    self.reply(503, b'{"error":"Explorador GPS no disponible"}')
                    return
                # El parser HTML del navegador normaliza CRLF a LF antes de verificar CSP.
                script = inline.group(1).replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                script_hash = base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
                csp = ("default-src 'self'; script-src 'self' https://unpkg.com "
                       f"'sha256-{script_hash}'; style-src 'self' https://unpkg.com 'unsafe-inline'; "
                       "connect-src 'self'; img-src 'self' data: https://unpkg.com "
                       "https://tile.openstreetmap.org https://server.arcgisonline.com; "
                       "frame-ancestors 'self'")
                self.reply(200, content, content_type, csp=csp)
            elif content_type.startswith("text/html"):
                self.reply(200, content, content_type)
            else:
                self.reply(200, content, content_type, cache=static_cache(self.path))
        else:
            self.reply(404, b'{"error":"Ruta no encontrada"}')

    do_GET = route
    do_POST = route
    do_DELETE = route


if __name__ == "__main__":
    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    with ThreadingHTTPServer(("0.0.0.0", 8000), Handler) as server:
        print("Frontend escuchando en 8000", flush=True)
        server.serve_forever()
