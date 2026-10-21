// Mapa base común de Artisa: el mismo fondo y el mismo selector de capas en todos los mapas
// (Rutas y alertas, Mapas de telemetría y el día de cada camión).
//
// - Fondos: Calles y lugares (OpenStreetMap) o Satélite (Esri, con nombres encima). Si el
//   proveedor activo falla, se cambia solo al otro.
// - Capas comunes: Faenas (encendida) y Densidad GPS del día anterior (apagada).
// - Los datos de faenas y de densidad se piden una sola vez para toda la página, incluido el
//   explorador histórico que vive en un iframe del mismo origen. Cada mapa dibuja su propia
//   copia, porque una capa de Leaflet solo puede estar en un mapa.
// - Las capas propias de cada mapa (rutas clasificadas, recorrido del camión, predicción…)
//   las agrega cada sección; aquí no se dibuja nada de eso.
(function () {
  const ZONA = "America/Santiago";
  const FONDOS = {
    calles: ["Calles y lugares (OpenStreetMap)", "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
             {maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"}],
    satelite: ["Satélite (Esri)", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
               {maxNativeZoom: 18, maxZoom: 19,
                attribution: "Tiles &copy; Esri &mdash; Esri, Maxar, Earthstar Geographics, GIS User Community"}],
    nombres: ["", "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
              {maxNativeZoom: 18, maxZoom: 19,
               attribution: "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, OpenStreetMap contributors"}],
  };

  // Caché de datos compartida con la página principal cuando el mapa está dentro de su iframe.
  let cache = null;
  try {
    if (window.parent !== window && window.parent.ArtisaMapaBase) cache = window.parent.ArtisaMapaBase.cache;
  } catch (error) { /* otro origen: cada página usa su propia caché */ }
  cache = cache || {faenas: null, densidad: {}};

  // Una sola petición por recurso; si falla se olvida para poder reintentar.
  function una(clave, tabla, crear) {
    if (!tabla[clave]) {
      tabla[clave] = crear();
      tabla[clave].catch(() => { delete tabla[clave]; });
    }
    return tabla[clave];
  }

  function faenas(pedir) {
    return una("faenas", cache, () => pedir("/api/faenas", 30000).then((respuesta) =>
      (respuesta.faenas || []).filter((f) => f.geocerca).map((f) => ({
        nombre: f.nombre, area_m2: f.area_m2,
        geojson: typeof f.geocerca === "string" ? JSON.parse(f.geocerca) : f.geocerca,
      }))));
  }

  function diaAnterior() {
    const hoy = new Intl.DateTimeFormat("en-CA", {timeZone: ZONA}).format(new Date());
    return new Date(Date.parse(hoy) - 86400000).toISOString().slice(0, 10);
  }

  // Celdas de ~100 m (lat/lon x 1e5 -> 1e3), igual que el explorador de telemetría.
  function densidad(pedir, dia) {
    return una(dia, cache.densidad, () => pedir(`/api/gps?desde=${dia}&hasta=${dia}`, 120000).then((gps) => {
      const celdas = new Map();
      for (let i = 0; i < gps.lat.length; i++) {
        const clave = Math.round(gps.lat[i] / 100) + "," + Math.round(gps.lon[i] / 100);
        celdas.set(clave, (celdas.get(clave) || 0) + 1);
      }
      let maximo = 1;
      for (const valor of celdas.values()) maximo = Math.max(maximo, valor);
      const puntos = [];
      for (const [clave, valor] of celdas) {
        const [lat, lon] = clave.split(",").map(Number);
        puntos.push([lat / 1000, lon / 1000, Math.log1p(valor) / Math.log1p(maximo)]);
      }
      return puntos;
    }));
  }

  function texto(valor) {
    const elemento = document.createElement("span");
    elemento.textContent = valor;
    return elemento;
  }

  const fechaCL = (iso) => iso.split("-").reverse().join("-");

  // pedir(ruta, timeoutMs) -> Promise<JSON>, con la sesión de cada página.
  function crear(contenedor, {pedir, centro = [-33.45, -70.66], zoom = 5, preferCanvas = true, aviso} = {}) {
    const mapa = L.map(contenedor, {preferCanvas, zoomControl: true, maxZoom: 19}).setView(centro, zoom);
    const [nombreCalles, urlCalles, opcionesCalles] = FONDOS.calles;
    const [nombreSatelite, urlSatelite, opcionesSatelite] = FONDOS.satelite;
    const calles = L.tileLayer(urlCalles, opcionesCalles).addTo(mapa);
    const satelite = L.tileLayer(urlSatelite, opcionesSatelite);
    // Nombres y límites sobre la foto satelital, por debajo de los trazos.
    mapa.createPane("mapaBaseNombres").style.zIndex = "350";
    mapa.getPane("mapaBaseNombres").style.pointerEvents = "none";
    const nombres = L.tileLayer(FONDOS.nombres[1], {...FONDOS.nombres[2], pane: "mapaBaseNombres"});
    satelite.on("add", () => nombres.addTo(mapa));
    satelite.on("remove", () => nombres.remove());
    respaldo(mapa, [calles, satelite], 6);

    const avisar = aviso || avisoEn(contenedor);
    mapa.createPane("mapaBaseFaenas").style.zIndex = "405";
    const renderFaenas = L.svg({pane: "mapaBaseFaenas", padding: 0.5}).addTo(mapa);
    renderFaenas._container.style.pointerEvents = "none";
    const capaFaenas = L.featureGroup();
    const capaDensidad = L.layerGroup();
    capaFaenas.on("add", () => dibujarFaenas(capaFaenas, pedir, avisar, renderFaenas));
    capaDensidad.on("add", () => dibujarDensidad(mapa, capaDensidad, pedir, avisar));
    capaFaenas.addTo(mapa);

    const fondos = {[nombreCalles]: calles, [nombreSatelite]: satelite};
    const comunes = {"Faenas": capaFaenas, "Densidad GPS (día anterior)": capaDensidad};
    let control = null;
    // Cada sección puede sumar sus propias capas al mismo selector (van antes de las comunes).
    function ponerControl(propias = {}) {
      if (control) control.remove();
      control = L.control.layers(fondos, {...propias, ...comunes}, {collapsed: true, position: "topleft"}).addTo(mapa);
      return control;
    }
    ponerControl();
    return {mapa, ponerControl, faenas: capaFaenas, densidad: capaDensidad};
  }

  function respaldo(mapa, capas, umbral) {
    let cambios = 0;
    capas.forEach((capa, i) => {
      let errores = 0, cargas = 0;
      capa.on("tileload", () => { cargas++; });
      capa.on("tileerror", () => {
        errores++;
        if (errores >= umbral && errores > cargas && mapa.hasLayer(capa) && cambios < capas.length - 1) {
          cambios++;
          mapa.removeLayer(capa);
          mapa.addLayer(capas[(i + 1) % capas.length]);
          console.warn("Mapa base con fallas, cambiando de proveedor");
        }
      });
      capa.on("add", () => { errores = 0; cargas = 0; });
    });
  }

  function avisoEn(contenedor) {
    let nota = null;
    return (mensaje) => {
      if (!nota) {
        nota = document.createElement("div");
        nota.className = "mapa-aviso";
        contenedor.append(nota);
      }
      nota.textContent = mensaje || "";
      nota.hidden = !mensaje;
    };
  }

  async function dibujarFaenas(capa, pedir, avisar, renderer) {
    if (capa.getLayers().length || capa._cargando) return;
    capa._cargando = true;
    try {
      for (const faena of await faenas(pedir)) {
        const area = faena.area_m2 != null
          ? Math.round(faena.area_m2).toLocaleString("es-CL") + " m²" : "sin dato";
        const poligono = L.geoJSON(faena.geojson, {
          pane: "mapaBaseFaenas", renderer,
          style: {color: "#2e7d32", weight: 2, fillOpacity: 0.18}
        }).bindTooltip(texto(faena.nombre), {sticky: true}).addTo(capa);
        L.marker(poligono.getBounds().getCenter(), {
          pane: "mapaBaseFaenas",
          icon: L.divIcon({className: "", html: '<div class="faena-pin"></div>', iconSize: [14, 14]})
        }).bindTooltip(texto(faena.nombre + " · área " + area)).addTo(capa);
      }
    } catch (error) {
      avisar("No se pudieron cargar las faenas: " + error.message);
    } finally {
      capa._cargando = false;
    }
  }

  async function dibujarDensidad(mapa, capa, pedir, avisar) {
    const dia = diaAnterior();
    if (capa._dia === dia) return;
    if (!L.heatLayer) {
      avisar("No se pudo cargar la librería del mapa de calor.");
      return;
    }
    capa.clearLayers();
    capa._dia = dia;
    avisar("Cargando densidad GPS del " + fechaCL(dia) + "…");
    try {
      const puntos = await densidad(pedir, dia);
      if (capa._dia !== dia || !mapa.hasLayer(capa)) {
        if (capa._dia === dia) capa._dia = null;  // se apagó a mitad de camino: rehacer al volver
        avisar("");
        return;
      }
      L.heatLayer(puntos, {radius: 8, blur: 10, minOpacity: 0.3, max: 1, maxZoom: 18}).addTo(capa);
      avisar(puntos.length ? "" : "Sin reportes GPS el " + fechaCL(dia) + ".");
    } catch (error) {
      capa._dia = null;
      avisar("No se pudo cargar la densidad GPS: " + error.message);
    }
  }

  window.ArtisaMapaBase = {crear, cache};
})();
