"""Comandos de Artisa. Se ejecutan desde la carpeta "Capstone Artisa":

    python3 scripts/artisa.py iniciar                 levanta todo (o lo actualiza tras un git pull)
    python3 scripts/artisa.py conectar-api URL [--desde "AAAA-MM-DD HH:MM"]
                                                      conecta la telemetría en vivo de la empresa
    python3 scripts/artisa.py estado                  servicios, conexión con la API y clasificación
    python3 scripts/artisa.py cierre                  cierre diario ahora (sin esperar la hora programada)
    python3 scripts/artisa.py rutas                   reentrena el modelo de rutas ahora y muestra su tabla
    python3 scripts/artisa.py respaldo                respaldo de MariaDB ahora (carpeta respaldos/)
    python3 scripts/artisa.py logs [servicio]         registros en vivo (Ctrl+C para salir)
    python3 scripts/artisa.py contrasena              cambia la contraseña de la web
    python3 scripts/artisa.py acceso-red si|no        permite (o no) entrar desde otros equipos de la red
    python3 scripts/artisa.py detener                 detiene los servicios SIN borrar datos

Ningún comando borra datos ni volúmenes.
"""

from pathlib import Path
import argparse
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def run(*cmd, check=True):
    return subprocess.run(list(cmd), cwd=ROOT, check=check).returncode


def compose(*args, check=True):
    return run("docker", "compose", *args, check=check)


def set_env(values):
    """Cambia o agrega variables en .env sin tocar las demás (ni mostrar secretos)."""
    if not ENV.exists():
        run(sys.executable, str(ROOT / "scripts" / "prepare_env.py"))
    lines = ENV.read_text(encoding="utf-8").splitlines()
    pending = dict(values)
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def iniciar(_):
    return run(sys.executable, str(ROOT / "scripts" / "start.py"))


def conectar_api(args):
    url = args.url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/@]+(:\d+)?(/\S*)?", url):
        sys.exit("La URL debe tener la forma http://IP:PUERTO (por ejemplo http://10.0.0.5:3003)")
    if not url.split("://", 1)[1].count("/"):
        url += "/sql"  # la API de la empresa recibe las consultas en /sql
    values = {"COMPANY_API_URL": url}
    if args.desde:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", args.desde):
            sys.exit('--desde debe ser "AAAA-MM-DD" o "AAAA-MM-DD HH:MM" (hora de Chile)')
        values["COMPANY_API_START"] = args.desde if " " in args.desde else args.desde + " 00:00"
    set_env(values)
    print(f"API configurada en .env: {url}" + (f" · desde {values['COMPANY_API_START']}" if args.desde else ""))
    print("Probando la conexión (debes estar en la red de la empresa)...")
    if run(sys.executable, str(ROOT / "scripts" / "check_company_api.py"), check=False) != 0:
        print("No se pudo consultar la API. Revisa la URL y la red; el servicio reintentará solo.")
    print("Iniciando la telemetría en vivo y el cierre diario...")
    compose("up", "--detach", "--build", "--wait", "telemetria", "machine-diario")
    print("Listo. Avance: python3 scripts/artisa.py logs telemetria")
    return 0


def estado(_):
    compose("ps", "--format", "table {{.Service}}\t{{.Status}}", check=False)
    print()
    run(sys.executable, str(ROOT / "scripts" / "check.py"), check=False)
    for servicio in ("telemetria", "machine-diario"):
        print(f"\nÚltimos registros de {servicio}:")
        compose("logs", "--tail", "4", "--no-log-prefix", servicio, check=False)
    return 0


def cierre(_):
    return compose("exec", "machine-diario", "python", "-m", "machine_learning.diario_worker", "--ahora", check=False)


def rutas(_):
    return compose("exec", "machine-diario", "python", "-m", "machine_learning.modelo_rutas", "--ahora", check=False)


def respaldo(_):
    return compose("exec", "respaldo", "bash", "/scripts/respaldo.sh", "--ahora", check=False)


def logs(args):
    return compose("logs", "--follow", "--tail", "100", *([args.servicio] if args.servicio else []), check=False)


def contrasena(_):
    code = run(sys.executable, str(ROOT / "scripts" / "setup_auth.py"), "--rotate", "--manual", check=False)
    if code == 0:
        compose("up", "--detach", "--force-recreate", "--wait", "auth")
    return code


def acceso_red(args):
    bind = "0.0.0.0" if args.opcion == "si" else "127.0.0.1"
    set_env({"ARTISA_BIND": bind})
    compose("up", "--detach", "--force-recreate", "--wait", "nginx")
    print("Acceso desde la red activado." if bind == "0.0.0.0" else "Acceso solo desde este equipo.")
    return 0


def detener(_):
    return compose("stop", check=False)


def main():
    parser = argparse.ArgumentParser(description="Comandos de Artisa", epilog=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("iniciar").set_defaults(fn=iniciar)
    api = sub.add_parser("conectar-api")
    api.add_argument("url")
    api.add_argument("--desde", help='primera conexión: traer reportes desde "AAAA-MM-DD HH:MM" (hora de Chile)')
    api.set_defaults(fn=conectar_api)
    for name, fn in (("estado", estado), ("cierre", cierre), ("rutas", rutas), ("respaldo", respaldo),
                     ("contrasena", contrasena), ("detener", detener)):
        sub.add_parser(name).set_defaults(fn=fn)
    red = sub.add_parser("acceso-red")
    red.add_argument("opcion", choices=["si", "no"])
    red.set_defaults(fn=acceso_red)
    log = sub.add_parser("logs")
    log.add_argument("servicio", nargs="?")
    log.set_defaults(fn=logs)
    args = parser.parse_args()
    try:
        return args.fn(args) or 0
    except subprocess.CalledProcessError as error:
        return error.returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
