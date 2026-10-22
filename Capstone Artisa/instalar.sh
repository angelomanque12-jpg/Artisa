#!/bin/bash
# Instala Artisa con un solo comando en un equipo Linux nuevo (Ubuntu o Debian).
#
#   bash instalar.sh                       (desde la carpeta "Capstone Artisa")
#
# Instala Git, Python y Docker; levanta Artisa; carga los datos (restaura un respaldo, conecta la
# API de la empresa desde el día que indiques o carga el JSON histórico); abre el acceso desde la
# red y deja todo arrancando solo al encender el equipo. Solo pregunta lo necesario.
#
# Para traer los datos de otro equipo, antes de ejecutarlo copia en esta carpeta su .env (mismas
# claves y contraseña de admin) y su respaldo artisa_AAAA-MM-DD.sql.gz en respaldos/.
#
# Se puede volver a ejecutar cuando se quiera: lo que ya está hecho se salta y NUNCA se borran
# datos ni volúmenes. Un respaldo solo se restaura si la base está vacía.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
REGISTRO="$PWD/instalacion.log"
PUERTO=8080

paso() { printf '\n\033[1;36m[%s/6] %s\033[0m\n' "$1" "$2"; }
info() { printf '   %s\n' "$*"; }
preguntar() {  # preguntar "texto" "valor por defecto" -> respuesta en $RESPUESTA
    local sufijo=""
    [ -n "${2:-}" ] && sufijo=" [$2]"
    RESPUESTA=""
    if { : </dev/tty; } 2>/dev/null; then   # sin terminal (por ejemplo, automatizado): valores por defecto
        read -r -p "   $1$sufijo: " RESPUESTA </dev/tty || RESPUESTA=""
    fi
    RESPUESTA="${RESPUESTA:-${2:-}}"
}
env_valor() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n1 || true; }
sudo_() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

if [ ! -f compose.yaml ] || [ ! -f scripts/artisa.py ]; then
    echo "Ejecuta este archivo desde la carpeta 'Capstone Artisa' del proyecto." >&2
    exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
    echo "Este instalador es para Ubuntu o Debian (usa apt)." >&2
    exit 1
fi

exec > >(tee -a "$REGISTRO") 2>&1   # todo queda también en instalacion.log
trap 'printf "\n\033[1;31mLa instalación se detuvo (ver arriba y %s).\033[0m\nCorrige el problema y vuelve a ejecutar: bash instalar.sh (no se borra nada).\n" "$REGISTRO"' ERR

# Una vez instalado Docker, el resto corre con el grupo docker activo (sin cerrar sesión).
if [ "${ARTISA_FASE:-}" != "2" ]; then
    printf '\n=== Instalación de Artisa · %s ===\n' "$(date '+%F %T')"

    # --- 1. Sistema ------------------------------------------------------------------------
    paso 1 "Git, Python y Docker"
    sudo_ env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo_ env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 curl ca-certificates >/dev/null
    if ! command -v docker >/dev/null 2>&1; then
        info "Instalando Docker (unos minutos)..."
        curl -fsSL https://get.docker.com | sudo_ sh >/dev/null
    fi
    # Docker (y con él Artisa) arranca solo al encender el equipo.
    sudo_ systemctl enable --now docker >/dev/null 2>&1 || info "Aviso: no se pudo activar el arranque automático de Docker (systemctl)."
    if [ "$(id -u)" != 0 ] && ! id -nG | grep -qw docker; then
        sudo_ usermod -aG docker "$USER"
        info "Usuario $USER agregado al grupo docker."
    fi
    info "Listo: $(docker --version 2>/dev/null || sudo_ docker --version)"
    if [ "$(id -u)" != 0 ] && ! docker info >/dev/null 2>&1; then
        exec sg docker -c "ARTISA_FASE=2 bash '$PWD/instalar.sh'"
    fi
fi

# --- 2. Datos: qué cargar --------------------------------------------------------------------
paso 2 "Datos"
RESPALDO=""
ultimo=$(ls -1 respaldos/artisa_*.sql.gz 2>/dev/null | sort | tail -n1 || true)
if [ -n "$ultimo" ]; then
    preguntar "Hay un respaldo de la base ($(basename "$ultimo")). ¿Restaurarlo? Es lo más rápido (S/N)" "S"
    [[ "$RESPUESTA" =~ ^[sS] ]] && RESPALDO="$ultimo"
else
    info "(Para traer los datos de otro equipo: su respaldo en respaldos/ y su .env en esta carpeta.)"
fi
URL_ACTUAL=$(env_valor COMPANY_API_URL)
preguntar "URL de la API de la empresa, ej. http://10.0.0.5:3003 (Enter = omitir)" "$URL_ACTUAL"
URL="$RESPUESTA"
DESDE=""
if [ -n "$URL" ] && [ "$URL" != "$URL_ACTUAL" ] && [ -z "$RESPALDO" ]; then
    preguntar "Desde qué día traer el historial de la API (AAAA-MM-DD)" "2026-04-01"
    DESDE="$RESPUESTA"
fi
preguntar "Carpeta con el JSON histórico, solo desarrollo (Enter = omitir)" ""
HISTORICO="$RESPUESTA"
preguntar "¿Permitir entrar a la web desde otros equipos de la red? (S/N)" "S"
RED="$RESPUESTA"

# --- 3. Base de datos ------------------------------------------------------------------------
paso 3 "Base de datos"
NUEVA=0
[ -f .env ] || NUEVA=1
CLAVE=$(python3 scripts/prepare_env.py | grep -i "contras" || true)
if [ -n "$RESPALDO" ]; then
    docker compose up --detach --wait mariadb
    tablas=$(docker compose exec -T mariadb sh -c \
        'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"$MARIADB_DATABASE\" AND table_name=\"reporte_diario\""' \
        2>/dev/null | tr -d '[:space:]')
    if [ "${tablas:-0}" = "0" ]; then
        info "Restaurando $(basename "$RESPALDO") (puede tardar varios minutos)..."
        gunzip -c "$RESPALDO" | docker compose exec -T mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD"'
        info "Respaldo restaurado."
    else
        info "La base ya tiene datos: no se restaura el respaldo (no se pisa nada)."
    fi
else
    info "Base nueva o existente: se conserva tal cual."
fi

# --- 4. Levantar y cargar ----------------------------------------------------------------------
paso 4 "Levantando Artisa (la primera vez tarda varios minutos)"
python3 scripts/artisa.py iniciar
if [ -n "$URL" ] && { [ "$URL" != "$URL_ACTUAL" ] || [ -n "$DESDE" ]; }; then
    if [ -n "$DESDE" ]; then
        python3 scripts/artisa.py conectar-api "$URL" --desde "$DESDE"
    else
        python3 scripts/artisa.py conectar-api "$URL"
    fi
fi
if [ -n "$HISTORICO" ]; then
    if grep -q '^ETL_HISTORICO=' .env; then
        sed -i 's/^ETL_HISTORICO=.*/ETL_HISTORICO=1/' .env
    else
        echo "ETL_HISTORICO=1" >> .env
    fi
    python3 scripts/artisa.py iniciar
    python3 scripts/recovery.py "$HISTORICO"
fi

# --- 5. Acceso desde la red -----------------------------------------------------------------
paso 5 "Acceso desde la red"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [[ "$RED" =~ ^[sS] ]]; then
    python3 scripts/artisa.py acceso-red si
    if command -v ufw >/dev/null 2>&1 && sudo_ ufw status | grep -q "Status: active"; then
        sudo_ ufw allow "$PUERTO/tcp" >/dev/null && info "Firewall: puerto $PUERTO abierto."
    fi
else
    info "Solo desde este equipo (se cambia con: python3 scripts/artisa.py acceso-red si)."
fi

# --- 6. Listo --------------------------------------------------------------------------------
paso 6 "Listo"
python3 scripts/artisa.py estado || true
printf '\n\033[1;32m=== Artisa instalado ===\033[0m\n'
echo "Web en este equipo: http://localhost:$PUERTO   (usuario: admin)"
[[ "$RED" =~ ^[sS] ]] && [ -n "$IP" ] && echo "Web desde la red:   http://$IP:$PUERTO"
if [ "$NUEVA" = 1 ] && [ -n "$CLAVE" ]; then
    printf '\033[1;33m>>> %s\033[0m\n' "$CLAVE"
    echo ">>> Guárdala ahora: no se vuelve a mostrar (se cambia con: python3 scripts/artisa.py contrasena)."
elif [ "$NUEVA" = 0 ]; then
    echo "Se usó el .env existente: la contraseña de admin es la misma de antes."
fi
echo "Los días se procesan solos por detrás; con muchos meses de datos puede tardar unas horas."
echo "Al reiniciar el equipo, Artisa vuelve a arrancar solo. Registro: $REGISTRO"
