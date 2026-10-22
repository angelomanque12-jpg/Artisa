# Instalación y operación de Artisa

Guía paso a paso para dejar Artisa funcionando en el servidor de la empresa y operarlo.
Todos los comandos se escriben en una terminal Linux (Ubuntu, o Ubuntu/WSL en Windows).
**Ningún comando de esta guía borra datos.**

## Antes de empezar

| Necesitas | Detalle |
|---|---|
| Servidor | Ubuntu 22.04 o 24.04, 4 GB de RAM o más, 20 GB libres en disco |
| Red | El servidor debe estar en la red de la empresa (la API solo responde ahí) |
| Dirección de la API | `http://IP_DE_LA_API:3003` (la entrega el equipo; no se guarda en Git) |
| Clave de Gemini (opcional) | Para el asistente: https://aistudio.google.com/apikey |

---

## 1. Servidor desde cero

### 1.1 Instalar Git, Python y Docker (una sola vez)

```bash
sudo apt update && sudo apt install -y git python3 curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
sudo systemctl enable --now docker
```

Cierra la sesión y vuelve a entrar (así Docker funciona sin `sudo`). Para comprobar:

```bash
docker run --rm hello-world
```

### 1.2 Descargar Artisa

```bash
git clone https://github.com/angelomanque12-jpg/Artisa.git
cd "Artisa/Capstone Artisa"
git checkout main
```

### 1.3 Levantar Artisa

Desde la terminal integrada de Visual Studio Code, abierta en la carpeta `Artisa`, ejecuta:

```powershell
.\iniciar-artisa.cmd
```

Ese es el inicio normal del proyecto. Artisa no se levanta automáticamente al abrir Windows,
Docker o WSL. El comando anterior inicia los servicios en el orden correcto y conserva todos
los datos existentes.

Si la terminal integrada ya está usando WSL y se encuentra dentro de `Capstone Artisa`, el
comando equivalente es:

```bash
python3 scripts/artisa.py iniciar
```

- La primera vez crea el archivo `.env` con claves aleatorias y muestra la **contraseña** del
  usuario `admin`. **Guárdala**: no se vuelve a mostrar.
- Tarda unos minutos mientras descarga y construye todo.
- Al terminar dice `Artisa en funcionamiento: http://localhost:8080`.

### 1.4 Conectar la API de la empresa

```bash
python3 scripts/artisa.py conectar-api http://IP_DE_LA_API:3003 --desde 2026-04-01
```

- `--desde` es desde qué día traer el historial la primera vez (hora de Chile).
- Prueba la conexión y empieza a traer reportes. La primera carga puede tardar horas; la web se
  puede usar mientras tanto.
- Para ver el avance: `python3 scripts/artisa.py logs telemetria` (Ctrl+C para salir; no detiene nada).

### 1.5 Permitir entrar desde otros equipos

```bash
python3 scripts/artisa.py acceso-red si
```

Desde otro equipo de la red se entra con `http://IP_DEL_SERVIDOR:8080`
(la IP se ve con `hostname -I`). Si el servidor tiene firewall: `sudo ufw allow 8080/tcp`.

### 1.6 Activar el asistente (opcional)

Abre `.env` con `nano .env`, completa esta línea con la clave (sin espacios ni comillas) y guarda:

```
GEMINI_API_KEY=tu_clave
```

Después:

```bash
python3 scripts/artisa.py iniciar
```

### 1.7 Comprobar que todo funciona

```bash
python3 scripts/artisa.py estado
```

Todos los servicios deben decir `healthy` y la telemetría no debe mostrar errores. En la web:

| Revisa | Qué deberías ver |
|---|---|
| Camiones en vivo | Los camiones con su último dato de hace pocos minutos |
| Rutas y alertas | El mapa con rutas y camiones (las rutas aparecen cuando se cierra el primer día) |
| Arquitectura | Los 5 servicios operativos |

Para entrenar el modelo de rutas de inmediato, sin esperar a la madrugada:

```bash
python3 scripts/artisa.py rutas
```

---

## 2. Qué corre solo

| Tarea | Cuándo | Qué hace |
|---|---|---|
| Telemetría | Cada 30 s | Trae los reportes nuevos de la API. Si se corta la red, reintenta y recupera lo pendiente. |
| Día en curso | Cada 10 min | Actualiza las cifras de hoy. |
| Alertas | Cada minuto | Revisa si algún camión salió de las rutas habituales y lo guarda en `alertas_ruta`; los cortes de señal fuera de ruta, en `cortes_senal`. |
| Cierre diario | 00:30 | Cierra el día anterior, recalcula días con reportes atrasados y reentrena el modelo de rutas (previsión de los próximos 7 días). |
| Respaldo | 03:00 | Guarda la base en `respaldos/` y conserva los últimos 3 días. |

Si el servidor se reinicia, Docker vuelve a levantar todo solo.

## 3. Actualizar a una versión nueva

```bash
cd "Artisa/Capstone Artisa"
git pull origin main
python3 scripts/artisa.py iniciar
```

Los datos se conservan. La base se actualiza sola si la versión nueva lo necesita.

## 4. Comandos

Se ejecutan dentro de la carpeta `Capstone Artisa`.

| Para | Comando |
|---|---|
| Levantar o actualizar | `python3 scripts/artisa.py iniciar` |
| Ver estado y conexión con la API | `python3 scripts/artisa.py estado` |
| Conectar o cambiar la API | `python3 scripts/artisa.py conectar-api http://IP:3003` |
| Cerrar días ahora | `python3 scripts/artisa.py cierre` |
| Reentrenar el modelo de rutas ahora | `python3 scripts/artisa.py rutas` |
| Respaldar ahora | `python3 scripts/artisa.py respaldo` |
| Ver registros en vivo | `python3 scripts/artisa.py logs [servicio]` |
| Cambiar la contraseña de la web | `python3 scripts/artisa.py contrasena` |
| Permitir o quitar el acceso desde la red | `python3 scripts/artisa.py acceso-red si` / `no` |
| Detener (sin borrar datos) | `python3 scripts/artisa.py detener` |

## 5. Ajustes en `.env`

Se cambian en `.env` y se aplican con `python3 scripts/artisa.py iniciar`.

| Variable | Por defecto | Para qué |
|---|---|---|
| `DIARIO_HORA` | `00:30` | Hora del cierre diario |
| `RESPALDO_HORA` / `RESPALDO_DIAS` | `03:00` / `3` | Hora del respaldo y cuántos días guardar |
| `RUTAS_CADA_DIAS` | `1` | Cada cuántos días se reentrena el modelo |
| `RUTAS_HORIZONTE_DIAS` | `7` | Cuántos días cubre la previsión de rutas |
| `RUTAS_MAX_CAMIONES_MINIMA` | `2` | Hasta cuántos camiones una ruta es "mínima" (N5) |
| `VIGILANCIA_MIN_MINUTOS` / `VIGILANCIA_MIN_KM` | `10` / `2` | Tiempo y distancia para que haya alerta |
| `ARTISA_PORT` | `8080` | Puerto de la web |
| `GEMINI_API_KEY` | vacío | Clave del asistente |

## 6. Registro de alertas para la empresa

Cada alerta queda guardada en la tabla `alertas_ruta` (inicio, fin, duración, nivel, km
recorridos, distancia a la ruta habitual y camino seguido). Una alerta terminada ya no cambia.

- **En la web:** Rutas y alertas → Alertas → **Historial**. Se elige un día y el período
  (Día, 7 días o Mes, hasta 92 días) y **↓ Descargar** genera un CSV que abre en Excel.
- El historial solo trae alertas **terminadas** (sus datos ya no cambian). Las que siguen
  abiertas se ven en **Activas** y **Sin señal** hasta que terminan.
- El CSV trae una fila por alerta: camión, conductor, inicio, fin, duración, nivel, km
  recorridos, distancia a la ruta habitual, zona, dónde salió y dónde terminó (lat, lon) y los
  cortes de señal durante la alerta.
- **Cortes de señal:** se guardan en la tabla `cortes_senal` (camión fuera de las rutas
  habituales que pasa más de 5 min sin posición nueva). En la ficha de la alerta se ve dónde se
  perdió la posición y dónde reapareció. "GPS sin posición nueva" significa que el equipo siguió
  transmitiendo copias del último reporte; "Sin datos", que no llegó nada.
- **Por la API:** `GET /api/alertas?desde=AAAA-MM-DD&hasta=AAAA-MM-DD` (con sesión iniciada; solo terminadas).
- La tabla se crea sola al iniciar; entra en el respaldo diario junto con el resto de la base.

## 7. Respaldos

Cada día a las 03:00 se guarda `respaldos/artisa_AAAA-MM-DD.sql.gz`. Para restaurar uno:

```bash
gunzip -c respaldos/artisa_AAAA-MM-DD.sql.gz | docker compose exec -T mariadb \
  sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" "$MARIADB_DATABASE"'
```

Conviene copiar la carpeta `respaldos/` a otro equipo de vez en cuando.

## 8. Si algo falla

| Síntoma | Qué hacer |
|---|---|
| La web no abre | `python3 scripts/artisa.py estado` y luego `python3 scripts/artisa.py iniciar` |
| "Sin conexión con la API" | Revisa que el servidor esté en la red de la empresa; se recupera solo al volver |
| "La API rechazó las credenciales" (403) | `docker compose restart telemetria`; si sigue, revisa la IP con el equipo y vuelve a `conectar-api` |
| El historial de alertas está vacío | Las alertas se guardan desde que `machine-diario` corre con esta versión; revisa `python3 scripts/artisa.py logs machine-diario` |
| Rutas y alertas dice "Modelo pendiente" | Falta el primer día cerrado; o entrena ya con `python3 scripts/artisa.py rutas` |
| El asistente dice que no está configurado | Falta `GEMINI_API_KEY` en `.env` (ver 1.6) |
| Olvidé la contraseña | `python3 scripts/artisa.py contrasena` |
| Disco lleno | Baja `RESPALDO_DIAS` en `.env` o saca respaldos antiguos de `respaldos/` |

**Nunca** usar `docker compose down -v` ni borrar volúmenes: elimina la base de datos.

---

## 9. Solo desarrollo: cargar el JSON histórico

El servidor de la empresa no lo necesita (trae el historial desde la API con `--desde`). En un
equipo de desarrollo, para cargar el histórico de abril a septiembre:

```bash
# en .env: ETL_HISTORICO=1
python3 scripts/artisa.py iniciar
python3 scripts/recovery.py "/ruta/a/la/carpeta/de/historicos"
python3 scripts/artisa.py conectar-api http://IP_DE_LA_API:3003 --desde "2026-09-08 20:00"
```

`2026-09-08 20:00` empalma con el final del JSON histórico.
