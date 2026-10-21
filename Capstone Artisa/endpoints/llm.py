"""Proveedor del modelo de lenguaje del asistente. Es el único módulo que habla con él.

Hoy usa la API gratuita de Gemini (Google AI Studio) con urllib, sin dependencias nuevas.
Para cambiar a otro proveedor (por ejemplo Ollama local) basta con reemplazar generate()
manteniendo su firma: recibe las instrucciones del sistema y la pregunta, devuelve el texto
de la respuesta o lanza LLMUnavailable con un mensaje para mostrar en la web.
"""

import json
import os
import threading
from time import sleep as _sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"
# Si el modelo principal está saturado (503), colgado o sin cuota (429), se prueba este,
# más liviano y casi siempre libre (la cuota gratuita es por modelo).
FALLBACK_MODEL = "gemini-flash-lite-latest"
TIMEOUT_SECONDS = 30
# Incluye el razonamiento interno de los modelos que "piensan" antes de responder.
MAX_OUTPUT_TOKENS = 4096
# Razonamiento bajo: para resumir datos ya calculados no hace falta pensar mucho, y con el
# nivel por defecto el modelo principal a veces no alcanzaba a responder en TIMEOUT_SECONDS
# (contestaba el de respaldo, más lento en total y menos preciso). Si un modelo no acepta el
# parámetro (HTTP 400) se repite la consulta sin él.
THINKING = {"thinkingLevel": "low"}
ATTEMPTS = 2
RETRY_SECONDS = 2

_used = threading.local()


class LLMUnavailable(Exception):
    """El modelo no está configurado o no respondió. El mensaje se muestra al usuario."""


class _BadRequest(Exception):
    """HTTP 400: la consulta tiene un parámetro que el modelo no acepta."""


class _Skip(Exception):
    """Este modelo no pudo responder; se prueba otro. `reason` decide el mensaje final y
    `retry` indica si un segundo intento con el mismo modelo tiene sentido."""

    def __init__(self, reason, retry=False):
        super().__init__(reason)
        self.reason, self.retry = reason, retry


MESSAGES = {
    "quota": "Se alcanzó el límite gratuito de Gemini. Intenta de nuevo en unos minutos.",
    "timeout": "Gemini tardó demasiado en responder. Intenta de nuevo.",
    "network": "No se pudo conectar con Gemini. Revisa la conexión a internet.",
    "unavailable": "Gemini no está disponible en este momento. Intenta de nuevo más tarde.",
}


def _api_key():
    return os.getenv("GEMINI_API_KEY", "").strip()


def _model():
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def configured():
    return bool(_api_key())


def model_name():
    """Modelo que respondió la última pregunta de este hilo (o el configurado, si aún no hay)."""
    return getattr(_used, "model", None) or _model()


def _call(model, body, key, opener):
    # La clave va en una cabecera, no en la URL, para que no quede en registros.
    request = Request(GEMINI_URL.format(model=model), data=body, method="POST",
                      headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except HTTPError as error:
        if error.code == 400:
            raise _BadRequest() from None
        if error.code in (401, 403):
            raise LLMUnavailable("Gemini rechazó la consulta: revisa GEMINI_API_KEY y GEMINI_MODEL en .env.") from None
        if error.code == 429:  # la cuota gratuita es por modelo: otro modelo puede responder
            raise _Skip("quota") from None
        raise _Skip("unavailable", retry=error.code >= 500) from None
    except (URLError, ConnectionError):
        raise _Skip("network", retry=True) from None
    except TimeoutError:
        raise _Skip("timeout") from None  # un modelo colgado no mejora reintentando
    except ValueError as error:
        print(f"Asistente: {model} respondió algo ilegible ({error})", flush=True)
        raise _Skip("unavailable") from None


def generate(system, question, opener=urlopen, sleep=_sleep):
    key = _api_key()
    if not key:
        raise LLMUnavailable("El asistente no está configurado: falta GEMINI_API_KEY en .env.")
    def body(thinking):
        config = {"temperature": 0.2, "maxOutputTokens": MAX_OUTPUT_TOKENS}
        if thinking:
            config["thinkingConfig"] = THINKING
        return json.dumps({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "generationConfig": config,
        }).encode("utf-8")

    def call(model):
        try:
            return _call(model, body(True), key, opener)
        except _BadRequest:
            try:
                return _call(model, body(False), key, opener)
            except _BadRequest:
                raise LLMUnavailable("Gemini rechazó la consulta: revisa GEMINI_API_KEY y GEMINI_MODEL en .env.") from None

    payload, reason = None, None
    for model in dict.fromkeys((_model(), FALLBACK_MODEL)):
        for attempt in range(ATTEMPTS):
            try:
                payload = call(model)
            except _Skip as skip:
                reason = skip.reason
                print(f"Asistente: {model} no respondió ({reason})", flush=True)
                if not skip.retry:
                    break
                if attempt < ATTEMPTS - 1:
                    sleep(RETRY_SECONDS)
                continue
            _used.model = model
            break
        if payload is not None:
            break
    if payload is None:
        raise LLMUnavailable(MESSAGES[reason])
    candidates = payload.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    if not text:
        raise LLMUnavailable("Gemini no entregó una respuesta para esta pregunta. Prueba reformulándola.")
    return text
