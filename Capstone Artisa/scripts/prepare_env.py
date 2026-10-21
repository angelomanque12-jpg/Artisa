"""Genera credenciales locales una sola vez, sin mostrarlas en la terminal."""

import os
from pathlib import Path
import secrets
import subprocess
import sys


env_path = Path(__file__).resolve().parent.parent / ".env"
try:
    descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("Se conserva el archivo .env existente.")
    with env_path.open("r+", encoding="utf-8", newline="") as env_file:
        content = env_file.read()
        has_ttl = any(
            line.partition("=")[0].strip().removeprefix("export ").strip()
            == "JWT_TTL_SECONDS"
            for line in content.splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
        if not has_ttl:
            separator = "\n" if content and not content.endswith("\n") else ""
            env_file.write(separator + "JWT_TTL_SECONDS=43200\n")
            print("Se agregó JWT_TTL_SECONDS=43200 sin cambiar los valores existentes.")
else:
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as env_file:
        env_file.write(
            "MARIADB_DATABASE=artisa\n"
            "MARIADB_USER=artisa_app\n"
            f"MARIADB_PASSWORD={secrets.token_hex(24)}\n"
            f"MARIADB_ROOT_PASSWORD={secrets.token_hex(24)}\n"
            "JWT_TTL_SECONDS=43200\n"
            "MONGO_URI=mongodb://mongodb:27017\n"
            "MONGO_DATABASE=gps_data\n"
            "MONGO_COLLECTION=raw_gps\n"
            "MONGO_TIMEOUT_SECONDS=10\n"
            "TELEMETRY_BATCH_SIZE=2000\n"
            "TELEMETRY_INTERVAL_SECONDS=30\n"
            "# Instalación nueva: solo API de la empresa (sin MongoDB ni JSON histórico).\n"
            "ETL_HISTORICO=0\n"
            "COMPANY_API_URL=\n"
            "COMPANY_API_START=\n"
            "DIARIO_HORA=00:30\n"
            "RESPALDO_HORA=03:00\n"
            "RESPALDO_DIAS=3\n"
            "ARTISA_BIND=127.0.0.1\n"
            "# Asistente de la web: clave gratuita de https://aistudio.google.com/apikey\n"
            "GEMINI_API_KEY=\n"
        )
    print("Archivo .env creado con credenciales locales aleatorias.")

subprocess.run([sys.executable, str(env_path.parent / "scripts" / "setup_auth.py")],
               cwd=env_path.parent, check=True)
