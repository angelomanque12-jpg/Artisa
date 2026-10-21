CREATE TABLE IF NOT EXISTS raw_gps (
    mongo_id VARCHAR(24) NOT NULL,
    report_id VARCHAR(36) NOT NULL,
    report_date DATETIME(6) NOT NULL,
    asset_id VARCHAR(16) NOT NULL,
    asset_name VARCHAR(64) NULL,
    event_name VARCHAR(128) NULL,
    latitude DOUBLE NOT NULL,
    longitude DOUBLE NOT NULL,
    location VARCHAR(255) NULL,
    speed DOUBLE NULL,
    cartography_limit_speed DOUBLE NULL,
    driver_name VARCHAR(64) NULL,
    driver_last_name VARCHAR(128) NULL,
    driver_document_number VARCHAR(32) NULL,
    ingestado_en DATETIME(6) NOT NULL,
    PRIMARY KEY (report_id),
    UNIQUE KEY idx_raw_gps_mongo_id (mongo_id),
    INDEX idx_raw_gps_asset_fecha (asset_id, report_date),
    INDEX idx_raw_gps_fecha (report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS raw_gps_estado (
    id TINYINT NOT NULL,
    origen VARCHAR(255) NOT NULL,
    ultimo_mongo_id VARCHAR(24) NULL,
    leidos BIGINT UNSIGNED NOT NULL DEFAULT 0,
    validos BIGINT UNSIGNED NOT NULL DEFAULT 0,
    rechazados BIGINT UNSIGNED NOT NULL DEFAULT 0,
    estado VARCHAR(32) NOT NULL,
    ultimo_error VARCHAR(255) NULL,
    rechazos_json TEXT NOT NULL,
    ultima_carga DATETIME(6) NULL,
    ultima_revision DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS faenas (
    id INT NOT NULL AUTO_INCREMENT,
    external_id VARCHAR(32) NULL,
    nombre VARCHAR(128) NOT NULL,
    grupo VARCHAR(64) NULL,
    area_m2 DOUBLE NULL,
    geocerca POLYGON REF_SYSTEM_ID=4326 NOT NULL,
    creado_en DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY idx_faenas_external_id (external_id),
    SPATIAL INDEX idx_faenas_geocerca (geocerca)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Catálogo de vehículos. raw_gps contiene todos los vehículos que entregó el GPS.
-- las lecturas de camiones filtran con este catálogo (common/vehiculos.py) en vez de
-- borrar filas. DB/vehiculos_data.sql lo puebla y common.vehiculos registra los nuevos.
CREATE TABLE IF NOT EXISTS tipos_vehiculo (
    id_tipo TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
    nombre VARCHAR(50) NOT NULL,
    PRIMARY KEY (id_tipo),
    UNIQUE KEY idx_tipos_vehiculo_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS vehiculos (
    asset_id VARCHAR(16) NOT NULL,
    id_tipo TINYINT UNSIGNED NOT NULL,
    actualizado_en DATETIME(6) NOT NULL,
    PRIMARY KEY (asset_id),
    INDEX idx_vehiculos_id_tipo (id_tipo),
    FOREIGN KEY (id_tipo) REFERENCES tipos_vehiculo(id_tipo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Telemetría actual recibida desde la API de la empresa (servicio telemetria).
-- Separada de raw_gps para no mezclar posiciones recientes con el histórico.
-- report_date se guarda en UTC y report_id evita duplicados al reintentar.
CREATE TABLE IF NOT EXISTS telemetria_api (
    report_id VARCHAR(128) NOT NULL,
    asset_id VARCHAR(16) NOT NULL,
    asset_name VARCHAR(128) NULL,
    report_date DATETIME(6) NOT NULL,
    latitude DOUBLE NOT NULL,
    longitude DOUBLE NOT NULL,
    speed DOUBLE NULL,
    heading DOUBLE NULL,
    ignition TINYINT(1) NULL,
    event_name VARCHAR(128) NULL,
    recibido_en DATETIME(6) NOT NULL,
    PRIMARY KEY (report_id),
    INDEX idx_telemetria_api_fecha_recibido (report_date, recibido_en),
    INDEX idx_telemetria_api_asset_fecha (asset_id, report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Estado y cursor del conector. El cursor (fecha, id) se guarda en la misma
-- transacción que cada página: tras un corte de red o un reinicio se retoma
-- exactamente desde ahí, sin perder ni duplicar reportes. cursor_fecha está en la
-- hora propia del origen (tal como la guarda su base), las demás fechas en UTC.
CREATE TABLE IF NOT EXISTS telemetria_api_estado (
    id TINYINT NOT NULL,
    origen VARCHAR(255) NOT NULL,
    cursor_fecha DATETIME(6) NULL,
    cursor_id VARCHAR(128) NULL,
    estado VARCHAR(32) NOT NULL,
    ultimo_error VARCHAR(255) NULL,
    fallos_consecutivos INT UNSIGNED NOT NULL DEFAULT 0,
    leidos BIGINT UNSIGNED NOT NULL DEFAULT 0,
    validos BIGINT UNSIGNED NOT NULL DEFAULT 0,
    rechazados BIGINT UNSIGNED NOT NULL DEFAULT 0,
    rechazos_json TEXT NOT NULL,
    ultimo_exito DATETIME(6) NULL,
    ultimo_dato DATETIME(6) NULL,
    desconectado_desde DATETIME(6) NULL,
    advertencia VARCHAR(255) NULL,
    ultima_revision DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Cierre diario (machine_learning/diario.py): resumen por camión de cada día de
-- 00:00 a 00:00 hora Chile, con las mismas reglas del cuaderno de telemetría.
-- Son datos derivados: se recalculan desde telemetria_api y raw_gps cuando hace falta.
CREATE TABLE IF NOT EXISTS reporte_diario (
    fecha DATE NOT NULL,
    asset_id VARCHAR(16) NOT NULL,
    asset_name VARCHAR(128) NULL,
    reportes INT UNSIGNED NOT NULL,
    primer_reporte DATETIME(6) NOT NULL,
    ultimo_reporte DATETIME(6) NOT NULL,
    km_observados DOUBLE NOT NULL,
    viajes INT UNSIGNED NOT NULL,
    tramos_unidos INT UNSIGNED NOT NULL,
    PRIMARY KEY (fecha, asset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- estado: en_curso (hoy), esperando_datos (pasó la medianoche pero la API aún no
-- confirma el final del día) o cerrado. ultimo_recibido permite detectar reportes
-- que llegan tarde y recalcular el día.
CREATE TABLE IF NOT EXISTS reporte_diario_estado (
    fecha DATE NOT NULL,
    zona VARCHAR(64) NOT NULL,
    inicio_utc DATETIME(6) NOT NULL,
    fin_utc DATETIME(6) NOT NULL,
    estado VARCHAR(32) NOT NULL,
    reportes INT UNSIGNED NOT NULL,
    camiones INT UNSIGNED NOT NULL,
    km_observados DOUBLE NOT NULL,
    descartados INT UNSIGNED NOT NULL,
    ultimo_recibido DATETIME(6) NULL,
    calculado_en DATETIME(6) NOT NULL,
    cerrado_en DATETIME(6) NULL,
    recalculos INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (fecha)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- (Nombre distinto de la tabla `viajes` del prototipo antiguo, que puede existir.)
-- Viajes entre paradas de cada camión (machine_learning/viajes.py), uno por fila. Se
-- guardan al calcular cada día y alimentan la clasificación de rutas. geometria es JSON
-- con tramos de puntos GPS reales [[[lon,lat],...],...] sin unir saltos de GPS.
CREATE TABLE IF NOT EXISTS viajes_camion (
    fecha DATE NOT NULL,
    asset_id VARCHAR(16) NOT NULL,
    inicio DATETIME(6) NOT NULL,
    fin DATETIME(6) NOT NULL,
    origen_lat DOUBLE NULL,
    origen_lon DOUBLE NULL,
    destino_lat DOUBLE NULL,
    destino_lon DOUBLE NULL,
    puntos INT UNSIGNED NOT NULL,
    km DOUBLE NOT NULL,
    con_saltos TINYINT(1) NOT NULL,
    abierto_inicio TINYINT(1) NOT NULL,
    abierto_fin TINYINT(1) NOT NULL,
    geometria MEDIUMTEXT NOT NULL,
    PRIMARY KEY (fecha, asset_id, inicio),
    INDEX idx_viajes_camion_inicio (inicio)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Versión del cálculo de cada día: si cambia el método (p. ej. al agregar viajes), los
-- días ya cerrados se recalculan solos sin reabrirse. MariaDB: ADD COLUMN IF NOT EXISTS.
ALTER TABLE reporte_diario_estado ADD COLUMN IF NOT EXISTS version_calculo SMALLINT UNSIGNED NOT NULL DEFAULT 1;

-- Clasificación de rutas (machine_learning/rutas.py). Cada ejecución crea una versión
-- y la activa al final en una sola transacción. Son datos derivados de viajes_camion.
CREATE TABLE IF NOT EXISTS rutas_version (
    id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    creado_en DATETIME(6) NOT NULL,
    desde DATE NOT NULL,
    hasta DATE NOT NULL,
    activa TINYINT(1) NOT NULL DEFAULT 0,
    metricas_json MEDIUMTEXT NOT NULL,
    lugares_json MEDIUMTEXT NOT NULL,
    PRIMARY KEY (id),
    INDEX idx_rutas_version_activa (activa)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS rutas_clasificadas (
    version_id INT UNSIGNED NOT NULL,
    route_id VARCHAR(16) NOT NULL,
    label VARCHAR(255) NOT NULL,
    origen VARCHAR(64) NOT NULL,
    destino VARCHAR(64) NOT NULL,
    usage_count INT UNSIGNED NOT NULL,
    unique_vehicle_count INT UNSIGNED NOT NULL,
    observed_points INT UNSIGNED NOT NULL,
    km_mediana DOUBLE NOT NULL,
    usage_level VARCHAR(8) NOT NULL,
    geometry_source VARCHAR(32) NOT NULL,
    geometria MEDIUMTEXT NOT NULL,
    primer_viaje DATETIME(6) NOT NULL,
    ultimo_viaje DATETIME(6) NOT NULL,
    vehiculos_json TEXT NOT NULL,
    PRIMARY KEY (version_id, route_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Ruta del viaje clasificado más reciente de cada camión (para el mapa).
CREATE TABLE IF NOT EXISTS rutas_vehiculo (
    version_id INT UNSIGNED NOT NULL,
    asset_id VARCHAR(16) NOT NULL,
    route_id VARCHAR(16) NOT NULL,
    ultimo_viaje DATETIME(6) NOT NULL,
    PRIMARY KEY (version_id, asset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Paradas del día (10 min dentro de 200 m, sin cortar por pausas de reporte) y tiempo
-- total detenido por camión: base para revisar tiempos de carga.
ALTER TABLE reporte_diario ADD COLUMN IF NOT EXISTS paradas INT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE reporte_diario ADD COLUMN IF NOT EXISTS minutos_detenido DOUBLE NOT NULL DEFAULT 0;

-- Conductor informado por la API de la empresa (el RUT no se guarda).
ALTER TABLE telemetria_api ADD COLUMN IF NOT EXISTS driver_name VARCHAR(64) NULL;
ALTER TABLE telemetria_api ADD COLUMN IF NOT EXISTS driver_last_name VARCHAR(128) NULL;
-- Dirección que entrega el GPS (sirve para nombrar los lugares de las rutas por comuna).
ALTER TABLE telemetria_api ADD COLUMN IF NOT EXISTS location VARCHAR(255) NULL;

-- La antigua predicción por día (LightGBM: tablas actividad_* y prediccion_*) se retiró. En
-- bases existentes esas tablas pueden seguir: no se usan ni se borran.

-- Registro de alertas de ruta (machine_learning/modelo_rutas.py, Parte 8). Machine revisa cada
-- minuto los camiones en vivo contra las rutas aprendidas y guarda cada alerta: un camión en
-- movimiento, fuera de faenas, en una ruta mínima (N5) o fuera de ruta (N6). Mientras sigue
-- abierta se actualiza. Al terminar queda fija, aunque después el modelo aprenda esa ruta.
CREATE TABLE IF NOT EXISTS alertas_ruta (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    asset_id VARCHAR(16) NOT NULL,
    asset_name VARCHAR(128) NULL,
    conductor VARCHAR(255) NULL,
    ubicacion VARCHAR(255) NULL,
    inicio DATETIME(6) NOT NULL,
    fin DATETIME(6) NOT NULL,
    estado VARCHAR(16) NOT NULL,
    nivel VARCHAR(4) NOT NULL,
    route_id VARCHAR(16) NULL,
    ruta_label VARCHAR(255) NULL,
    minutos INT UNSIGNED NOT NULL,
    reportes INT UNSIGNED NOT NULL,
    km_recorridos DOUBLE NOT NULL,
    km_ruta_habitual DOUBLE NULL,
    velocidad DOUBLE NULL,
    latitud DOUBLE NOT NULL,
    longitud DOUBLE NOT NULL,
    corredor_lat DOUBLE NULL,
    corredor_lon DOUBLE NULL,
    corredor_km DOUBLE NULL,
    estela MEDIUMTEXT NOT NULL,
    version_modelo INT UNSIGNED NULL,
    detectada_en DATETIME(6) NOT NULL,
    actualizada_en DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_alertas_ruta_camion_inicio (asset_id, inicio),
    INDEX idx_alertas_ruta_inicio (inicio),
    INDEX idx_alertas_ruta_estado (estado, fin)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Cortes de señal (machine_learning/modelo_rutas.py, Parte 9). Un camión en movimiento fuera de
-- las rutas habituales que pasa más de 5 min sin posición nueva. desde y hasta: último reporte
-- antes del corte y primero después (UTC). hasta NULL: sigue sin posición nueva. tipo
-- gps_congelado: siguieron llegando copias del mismo reporte (el equipo transmitía sin GPS).
-- tipo sin_datos: no llegó nada. km_linea y kmh_necesaria: distancia en línea recta entre ambos
-- puntos y la velocidad media que habría necesitado.
CREATE TABLE IF NOT EXISTS cortes_senal (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    asset_id VARCHAR(16) NOT NULL,
    desde DATETIME(6) NOT NULL,
    hasta DATETIME(6) NULL,
    minutos INT UNSIGNED NOT NULL,
    tipo VARCHAR(16) NOT NULL,
    lat_antes DOUBLE NOT NULL,
    lon_antes DOUBLE NOT NULL,
    velocidad_antes DOUBLE NULL,
    encendido_antes TINYINT(1) NULL,
    evento_antes VARCHAR(128) NULL,
    lat_despues DOUBLE NULL,
    lon_despues DOUBLE NULL,
    km_linea DOUBLE NULL,
    kmh_necesaria DOUBLE NULL,
    copias INT UNSIGNED NOT NULL DEFAULT 0,
    ultima_copia DATETIME(6) NULL,
    detectado_en DATETIME(6) NOT NULL,
    actualizado_en DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_cortes_senal_camion_desde (asset_id, desde),
    INDEX idx_cortes_senal_desde (desde),
    INDEX idx_cortes_senal_abiertos (hasta)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
