"use strict";

const $ = (id) => document.getElementById(id);
const landing = document.querySelector(".landing");
const appShell = document.getElementById("app");
const loginForm = document.getElementById("login-form");
const format = (value, decimals = 0) => Number(value).toLocaleString("es-CL", {maximumFractionDigits: decimals});
const formatUtc = (value) => value ? new Intl.DateTimeFormat("es-CL", {
  dateStyle: "short", timeStyle: "medium", timeZone: "UTC"
}).format(new Date(value)) : "Sin fecha";
const HISTORY_ZONE = "America/Santiago";  // días de la empresa: 00:00 a 00:00 hora de Chile
const formatLocal = (value, zone = HISTORY_ZONE) => value ? new Intl.DateTimeFormat("es-CL", {
  dateStyle: "short", timeStyle: "medium", timeZone: zone
}).format(new Date(value)) : "Sin fecha";
const formatDataDay = (value) => value ? new Intl.DateTimeFormat("es-CL", {
  dateStyle: "long", timeZone: "UTC"
}).format(new Date(value + "T12:00:00Z")) : "Sin fecha";
let refreshing = false;
let liveVehiclesRefreshing = false;

function showApp() {
  landing.hidden = true;
  appShell.hidden = false;
  window.scrollTo(0, 0);
}

const TOKEN_KEY = "artisa_token";
const EXPIRED_MESSAGE = "Tu sesión expiró. Ingresa nuevamente.";
let activeToken = null;
let sessionVersion = 0;
let expiryTimer;
const pendingRequests = new Set();

// Esta comprobación es solo para la interfaz; Nginx/Auth verifican la firma.
function tokenExpiration(value) {
  try {
    if (typeof value !== "string" || value.length > 4096) return 0;
    const parts = value.split(".");
    if (parts.length !== 3 || parts.some((part) => !/^[A-Za-z0-9_-]+$/.test(part))) return 0;
    const decode = (part) => {
      const base64 = part.replace(/-/g, "+").replace(/_/g, "/");
      return atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "="));
    };
    const header = JSON.parse(decode(parts[0]));
    const payload = JSON.parse(decode(parts[1]));
    if (header?.alg !== "HS256" || header?.typ !== "JWT" || decode(parts[2]).length !== 32) return 0;
    return Number.isSafeInteger(payload?.exp) && payload.exp > Date.now() / 1000
      ? payload.exp * 1000 : 0;
  } catch { return 0; }
}

function token() {
  if (activeToken && !tokenExpiration(activeToken)) logout(EXPIRED_MESSAGE);
  return activeToken;
}

function clearSessionWork() {
  clearTimeout(expiryTimer);
  sessionVersion++;
  for (const controller of pendingRequests) controller.abort();
  pendingRequests.clear();
  refreshing = false;
  liveVehiclesRefreshing = false;
  if (typeof vg !== "undefined") vg.loading = false;
  if (typeof vgRetry !== "undefined") clearTimeout(vgRetry);
  $("refresh").disabled = false;
  window.clearTimeout(fleetRetry);
}

function clearPassword() {
  $("login-password").value = "";
  $("login-password").type = "password";
  $("toggle-password").textContent = "Mostrar";
  $("toggle-password").setAttribute("aria-pressed", "false");
}

function logout(message = "") {
  clearSessionWork();
  if (typeof historyState !== "undefined") historyState.detail = null;
  for (const wrapper of document.querySelectorAll(".explorer-frame-wrap")) {
    const frame = wrapper.querySelector("iframe");
    if (frame) frame.removeAttribute("src");
  }
  activeToken = null;
  try { localStorage.removeItem(TOKEN_KEY); } catch { /* Almacenamiento no disponible. */ }
  clearAssistantLog();
  clearPassword();
  $("login-user").value = "";
  appShell.hidden = true;
  landing.hidden = false;
  history.replaceState(null, "", location.pathname + location.search);
  $("login-error").textContent = message;
  $("login-error").hidden = !message;
}

function scheduleExpiration() {
  clearTimeout(expiryTimer);
  const remaining = tokenExpiration(activeToken) - Date.now();
  if (remaining <= 0) return logout(EXPIRED_MESSAGE);
  expiryTimer = setTimeout(scheduleExpiration, Math.min(remaining, 2147483647));
}

function restoreSession() {
  let saved;
  try { saved = localStorage.getItem(TOKEN_KEY); } catch { saved = null; }
  if (!tokenExpiration(saved)) {
    logout(saved || activeToken ? EXPIRED_MESSAGE : $("login-error").textContent);
    return;
  }
  const changed = activeToken !== saved;
  if (changed) clearSessionWork();
  activeToken = saved;
  $("login-error").textContent = "";
  $("login-error").hidden = true;
  if (!location.hash) history.replaceState(null, "", "#camiones");
  syncDatasetView();
  if (appShell.hidden) showApp();
  scheduleExpiration();
  // Al abrir la página se cargan todas las secciones, no solo la visible: así, al cambiar
  // de pestaña los datos ya están (o vienen en camino).
  if (changed) { refresh(); refreshTelemetry(); loadFleet(); loadVigilance(false); }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = loginForm.querySelector(".landing-button");
  const error = $("login-error");
  const usernameInput = $("login-user");
  const passwordInput = $("login-password");
  button.disabled = true;
  error.hidden = true;
  try {
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      cache: "no-store",
      body: JSON.stringify({username, password})
    });
    if (response.status === 401) throw new Error("Usuario o contraseña incorrectos");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "No se pudo iniciar sesión.");
    if (!tokenExpiration(payload.token)) throw new Error("El servidor devolvió una sesión no válida.");
    try { localStorage.setItem(TOKEN_KEY, payload.token); }
    catch { throw new Error("Permite el almacenamiento local para mantener tu sesión."); }
    clearPassword();
    history.replaceState(null, "", "#camiones");
    restoreSession();
  } catch (loginError) {
    clearPassword();
    error.textContent = loginError.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("toggle-password").addEventListener("click", () => {
  const passwordInput = $("login-password");
  const showing = passwordInput.type === "text";
  passwordInput.type = showing ? "password" : "text";
  $("toggle-password").textContent = showing ? "Mostrar" : "Ocultar";
  $("toggle-password").setAttribute("aria-pressed", String(!showing));
  passwordInput.focus();
});

$("logout").addEventListener("click", () => logout());

async function api(path, options = {}) {
  const currentToken = token();
  if (!currentToken) throw new Error(EXPIRED_MESSAGE);
  const version = sessionVersion;
  const controller = new AbortController();
  pendingRequests.add(controller);
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs || 20000);
  try {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${currentToken}`);
    const {timeoutMs, ...fetchOptions} = options;
    let response;
    try {
      response = await fetch(path, {...fetchOptions, headers, signal: controller.signal});
    } catch (error) {
      if (error.name === "AbortError") throw new Error("El servidor tardó demasiado en responder.");
      throw error;
    }
    if (version !== sessionVersion) throw new Error("Petición de una sesión anterior cancelada.");
    // Nginx puede devolver HTML en un 401: comprobarlo antes de leer JSON.
    if (response.status === 401) {
      logout(EXPIRED_MESSAGE);
      throw new Error(EXPIRED_MESSAGE);
    }
    // El límite de frecuencia de Nginx responde 429 en HTML.
    if (response.status === 429 && !(response.headers.get("Content-Type") || "").includes("json")) {
      throw new Error("Demasiadas consultas seguidas. Espera un momento e intenta de nuevo.");
    }
    const payload = await response.json();
    if (version !== sessionVersion || !token()) throw new Error(EXPIRED_MESSAGE);
    if (!response.ok) throw new Error(payload.error || "El servicio está temporalmente ocupado.");
    return payload;
  } finally {
    clearTimeout(timeout);
    pendingRequests.delete(controller);
  }
}

function syncDatasetView() {
  let view = "trucks";
  if (window.location.hash === "#datos-reales") { history.replaceState(null, "", "#camiones"); }
  else if (window.location.hash === "#mapa-calor") view = "heatmap";
  else if (window.location.hash === "#dashboard") view = "dashboard";
  else if (window.location.hash === "#prediccion") view = "prediction";
  // Arquitectura es una vista aparte; el resto de las vistas parte directo en su contenido.
  const overview = window.location.hash === "#flujo";
  document.querySelector(".heading").hidden = !overview;
  $("flujo").hidden = !overview;
  document.querySelector(".dataset-tabs").hidden = overview;
  for (const space of document.querySelectorAll("[data-dataset-view]")) {
    space.hidden = overview || space.dataset.datasetView !== view;
  }
  for (const tab of document.querySelectorAll("[data-dataset-tab]")) {
    const selected = tab.dataset.datasetTab === view;
    tab.classList.toggle("is-active", selected);
    tab.setAttribute("aria-current", selected ? "page" : "false");
  }
  $("overview-link").classList.toggle("active", overview);
  document.querySelector(".topbar strong").textContent = overview ? "Arquitectura"
    : {trucks: "Camiones en vivo", heatmap: "Datos históricos", prediction: "Rutas y alertas", dashboard: "Dashboard"}[view];
  for (const link of document.querySelectorAll(".sidebar [data-dataset-link]")) {
    link.classList.toggle("active", !overview && link.dataset.datasetLink === view);
  }
  if (overview) return;
  if (view === "trucks") syncTruckView();
  if (view === "heatmap") {
    window.setTimeout(openExplorer, 50);
  }
  if (view === "dashboard" && dbState.data) loadDashPeriod();
  if (view === "prediction") {
    if (!vg.data) loadVigilance(false);
    if (fleetMap) window.setTimeout(() => { fleetMap.invalidateSize(); vgFit(false); }, 0);
  }
  if (view === "dashboard") {
    window.setTimeout(loadFleet, 50);
  }
}

// --- Rutas y alertas: rutas aprendidas por el modelo, camiones en vivo y alertas ---------------------
// El modelo (machine_learning/modelo_rutas.py) aprende las rutas de los viajes reales y las clasifica
// por cuántos camiones distintos las usan: N1 troncal … N5 mínima (1 o 2 camiones). Se reaprende
// cada día y lo aprendido es la previsión de los próximos 7 días. Las líneas llegan de /api/rutas-aprendidas (solo cambian con
// una versión nueva); camiones y alertas de /api/vigilancia cada minuto. Un camión que no está en
// ninguna ruta aprendida queda "fuera de ruta" (N6). Hay alerta en N5 y N6.
const ROUTE_LEVELS = {
  N1: {color: "#14532d", weight: 7, short: "Troncal", label: "Troncal: la usa gran parte de la flota"},
  N2: {color: "#3f9d4a", weight: 5.5, short: "Muy usada", label: "Muy usada"},
  N3: {color: "#d9a021", weight: 4.5, short: "Uso medio", label: "Uso medio"},
  N4: {color: "#ef7d22", weight: 3.5, short: "Poco usada", label: "Poco usada"},
  N5: {color: "#d7301f", weight: 2.5, short: "Mínima", label: "Mínima: la usaron 1 o 2 camiones · genera alerta"},
  N6: {color: "#7b3fa0", weight: 0, short: "Fuera de ruta", label: "Fuera de ruta: camino que el modelo no conoce · genera alerta"}
};
const levelNumber = (level) => Number(String(level || "N6").slice(1));
const VG_STATES = {
  activa: {label: "Activa", className: "active"},
  sin_senal: {label: "Sin señal", className: "nosignal"},
  terminada: {label: "Terminada", className: "done"}
};
const VG_REFRESH_MS = 60000;
let fleetMap = null;
let usageLayer = null;
let usageRenderer = null;
let trailRenderer = null;
let selectionRenderer = null;
let trailLayer = null;
let truckLayer = null;
let selectionLayer = null;
const truckMarkers = new Map();
var vg = {data: null, model: null, modelLoading: false, alerts: new Map(), vehicles: [],
  history: {desde: null, hasta: null, alerts: new Map(), loading: false, error: null},
  tab: "alerts", alertFilter: "activa", zone: "all", layers: {usage: true, vehicles: true, trails: true},
  selected: null, fitted: false, loading: false, openPopup: null};

const vgTime = (iso) => iso ? new Intl.DateTimeFormat("es-CL", {
  weekday: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: HISTORY_ZONE}).format(new Date(iso)) : "—";
const vgLocalDay = (iso) => new Intl.DateTimeFormat("en-CA", {timeZone: HISTORY_ZONE}).format(new Date(iso));
const vgDuration = (minutes) => minutes < 60 ? `${format(minutes)} min`
  : `${format(Math.floor(minutes / 60))} h ${String(Math.round(minutes % 60)).padStart(2, "0")} min`;
const vgClock = (iso) => new Intl.DateTimeFormat("es-CL", {hour: "2-digit", minute: "2-digit", timeZone: HISTORY_ZONE})
  .format(new Date(iso));
// Cortes de señal (Machine, Parte 9): un camión fuera de las rutas habituales sin posición nueva.
const VG_CUTS = {
  gps_congelado: {label: "GPS sin posición nueva", short: "GPS sin posición", color: "#8a2f86",
    note: "el equipo transmitía, pero repitiendo la misma posición"},
  sin_datos: {label: "Sin datos", short: "Sin datos", color: "#5f6f86",
    note: "no llegó ningún reporte (sin cobertura, apagado o sin energía)"}
};
const VG_CUTS_SHOWN = 4;

// Marcador numerado del lugar donde se perdió la posición (el número es el de la lista de la ficha).
const vgCutIcon = (number, cut) => L.divIcon({className: "vg-cut-pin", iconSize: [20, 20], iconAnchor: [10, 10],
  html: `<span style="background:${VG_CUTS[cut.tipo].color}">${number}</span>`});
const vgCutsMinutes = (alert) => (alert.cortes || []).reduce((total, cut) => total + cut.minutos, 0);
function vgCutText(cut) {
  const end = cut.hasta ? `${vgClock(cut.hasta)}` : "sigue";
  const where = (point) => point ? `${point[0].toFixed(5)}, ${point[1].toFixed(5)}` : "—";
  return `${vgClock(cut.desde)} (${where(cut.antes)}) → ${end} (${where(cut.despues)}) · ${vgDuration(cut.minutos)} · `
    + VG_CUTS[cut.tipo].label + (cut.km_linea === null ? "" : ` · ${format(cut.km_linea, 1)} km en línea recta`);
}
// Una alerta por su id, esté en las de hoy o en el historial cargado.
const vgAlertById = (id) => vg.alerts.get(id) || vg.history.alerts.get(id);
const vgAlertKey = (alert) => "a" + alert.id;

function levelBadge(level) {
  const badge = el("b", "lr-badge lr-" + String(level || "N6").toLowerCase(), String(levelNumber(level)));
  badge.title = (ROUTE_LEVELS[level] || ROUTE_LEVELS.N6).label;
  return badge;
}

function levelRange(level) {
  const row = vg.model?.niveles.find((item) => item.nivel === level);
  if (!row || row.desde === null || row.desde === undefined) return "";
  return row.desde === row.hasta ? `${format(row.desde)} ${row.desde === 1 ? "camión" : "camiones"}`
    : `${format(row.desde)}–${format(row.hasta)} camiones`;
}

function openExplorer() {
  if (!token()) return;
  const frame = $("explorer-routes").querySelector("iframe[data-explorer-frame]");
  if (frame && !frame.hasAttribute("src")) frame.src = frame.dataset.explorerFrame;
  frame?.contentWindow?.postMessage("artisa:show-map", location.origin);
}

function setSelectionDetail(kind, title, description, metadata) {
  const detail = $("map-selection-detail");
  const type = document.createElement("span");
  type.textContent = kind;
  const heading = document.createElement("strong");
  heading.textContent = title;
  const copy = document.createElement("p");
  copy.textContent = description;
  detail.replaceChildren(type, heading, copy);
  if (metadata) {
    const extra = document.createElement("small");
    extra.textContent = metadata;
    detail.append(extra);
  }
}

// --- Camiones ---
function vehicleDriver(vehicle) {
  const structured = [vehicle.driver_name, vehicle.driver_last_name]
    .map((part) => String(part || "").trim()).filter(Boolean).join(" ");
  return structured || "No informado";
}

function vehicleStatus(vehicle) {
  const timestamp = vehicle.timestamp ? new Date(vehicle.timestamp).getTime() : NaN;
  if (!Number.isFinite(timestamp)) return {label: "Estado no disponible", className: "off"};
  if (Date.now() - timestamp > 30 * 60 * 1000) return {label: "Última posición conocida", className: "off"};
  if (Number(vehicle.speed_kmh || 0) > 3) return {label: "En movimiento", className: "moving"};
  if (vehicle.ignition !== null && vehicle.ignition !== undefined && Number(vehicle.ignition) === 0) {
    return {label: "Apagado", className: "off"};
  }
  return {label: "Con señal reciente", className: "moving"};
}

const TRUCK_MARKUP = '<img class="truck-glyph" src="/artisa-truck-box-white-green.png?v=20261014" alt="" aria-hidden="true">';

function truckIcon(vehicle) {
  const status = vehicleStatus(vehicle);
  const hasHeading = vehicle.heading !== null && vehicle.heading !== undefined &&
    vehicle.heading !== "" && Number.isFinite(Number(vehicle.heading));
  const heading = hasHeading ? ((Number(vehicle.heading) % 360) + 360) % 360 : null;
  // La imagen base mira hacia la izquierda: se invierte cuando el rumbo tiene componente este.
  const facing = hasHeading && heading <= 180 ? -1 : 1;
  const direction = hasHeading ? "<span class='truck-direction' aria-hidden='true'></span>" : "";
  const orientation = hasHeading ? " style='--truck-heading:" + heading + "deg;--truck-facing:" + facing + "'" : "";
  const alert = vehicle.en_alerta ? " is-alert" : "";
  return L.divIcon({
    className: "truck-div-icon",
    html: "<span class='truck-marker " + status.className + alert + "'" + orientation + ">" + direction + TRUCK_MARKUP + "</span>",
    iconSize: [56, 38], iconAnchor: [28, 19], popupAnchor: [0, -21]
  });
}

function truckAgeText(vehicle) {
  const seconds = (Date.now() - new Date(vehicle.timestamp).getTime()) / 1000;
  return Number.isFinite(seconds) ? formatAge(seconds) : "sin dato";
}

function truckPopup(vehicle) {
  const status = vehicleStatus(vehicle);
  const wrapper = el("div", "truck-popup");
  const state = el("b", status.className, vehicle.en_alerta ? "Fuera de ruta · " + status.label : status.label);
  if (vehicle.en_alerta) state.classList.add("alert");
  const speed = vehicle.speed_kmh === null || vehicle.speed_kmh === undefined ? "Sin velocidad" : format(vehicle.speed_kmh, 0) + " km/h";
  const link = el("a", "truck-popup-link", "Ver su recorrido de hoy");
  link.href = "#camiones/" + encodeURIComponent(vehicle.vehicle_id);
  wrapper.append(
    el("strong", "", vehicle.vehicle_name || vehicle.vehicle_id), state,
    el("p", "", speed + " · " + (vehicle.event || "Sin evento")),
    el("small", "", "Patente: " + vehicle.vehicle_id),
    el("small", "", "Conductor: " + vehicleDriver(vehicle)),
    el("small", "", "Último dato: " + formatLocal(vehicle.timestamp) + " (" + truckAgeText(vehicle) + ")"),
    link);
  return wrapper;
}

// --- Rutas aprendidas en el mapa: una sola línea por camino ---
// Muchas rutas comparten la misma carretera (una por cada par de lugares). El servidor entrega
// la red ya agrupada: cada tramo una vez, con el mejor nivel que pasa por ahí y qué rutas lo usan.
// Con una alerta o un camión elegido, la red queda gris y delgada y se resalta solo lo suyo.
function vgDrawUsage() {
  usageLayer.clearLayers();
  const red = vg.model?.red;
  if (!red) return;
  const focus = Boolean(vg.selected);
  const onlyMinimal = vg.zone === "rare" && !focus;
  const groups = {};
  for (const [a, b, level] of red.tramos) (groups[level] ||= []).push([red.nodos[a], red.nodos[b]]);
  // De la menos usada a la troncal: las troncales quedan encima.
  for (const number of [5, 4, 3, 2, 1]) {
    const segments = groups[number];
    if (!segments) continue;
    const style = ROUTE_LEVELS["N" + number];
    const muted = focus || (onlyMinimal && number !== 5);
    L.polyline(segments, {renderer: usageRenderer, lineCap: "round", lineJoin: "round", interactive: false,
      color: muted ? "#93a29e" : style.color,
      weight: focus ? Math.max(1.5, style.weight / 3) : muted ? 2 : (onlyMinimal ? 4.5 : style.weight),
      opacity: focus ? 0.4 : muted ? 0.45 : 0.9}).addTo(usageLayer);
  }
  if (focus) return;
  // Franja invisible más ancha encima: tocar un camino muestra su nivel y qué rutas pasan.
  const all = red.tramos.map(([a, b]) => [red.nodos[a], red.nodos[b]]);
  L.polyline(all, {renderer: usageRenderer, weight: 14, opacity: 0, lineCap: "round", className: "vg-hit"})
    .on("click", (event) => { L.DomEvent.stopPropagation(event); vgInspect(event.latlng); })
    .addTo(usageLayer);
}

// El tramo más cercano al punto tocado (en píxeles de pantalla).
function vgNearestSegment(latlng) {
  const red = vg.model.red;
  const p = fleetMap.latLngToLayerPoint(latlng);
  const points = new Map();
  const at = (index) => {
    if (!points.has(index)) points.set(index, fleetMap.latLngToLayerPoint(red.nodos[index]));
    return points.get(index);
  };
  let best = null, bestDistance = Infinity;
  for (const tramo of red.tramos) {
    const a = at(tramo[0]), b = at(tramo[1]);
    const distance = L.LineUtil.pointToSegmentDistance(p, a, b);
    if (distance < bestDistance) { best = tramo; bestDistance = distance; }
  }
  return bestDistance <= 12 ? best : null;
}

function vgInspect(latlng) {
  const tramo = vgNearestSegment(latlng);
  if (!tramo) return;
  const [, , number, total, routeIndexes] = tramo;
  const level = "N" + number;
  const style = ROUTE_LEVELS[level];
  const box = el("div", "vg-inspect-box");
  const head = el("span", "vg-inspect-head");
  head.append(levelBadge(level), el("strong", "", `Nivel ${number} · ${style.short}`));
  box.append(head, el("span", "", total === 1 ? "Pasa 1 ruta aprendida por este camino:"
    : `Pasan ${format(total)} rutas aprendidas por este camino:`));
  const list = el("ul", "vg-inspect-routes");
  for (const index of routeIndexes.slice(0, 5)) {
    const route = vg.model.rutas[index];
    const item = el("li");
    item.append(levelBadge(route.nivel), el("span", "", route.label),
      el("small", "", `${format(route.camiones)} ${route.camiones === 1 ? "camión" : "camiones"}`));
    list.append(item);
  }
  box.append(list);
  if (total > Math.min(5, routeIndexes.length)) box.append(el("small", "", `y ${format(total - Math.min(5, routeIndexes.length))} más`));
  box.append(el("small", "", "El color es el de la ruta más concurrida que pasa por aquí."));
  L.popup({className: "vg-inspect", autoPanPaddingTopLeft: [20, 120], autoPanPaddingBottomRight: [20, 90]}).setLatLng(latlng).setContent(box).openOn(fleetMap);
}

// La línea de una ruta aprendida, resaltada sobre la red apagada (con borde blanco).
function vgHighlightRoute(routeId) {
  const route = routeId && vg.model?.rutas.find((item) => item.id === routeId);
  if (!route) return;
  const color = ROUTE_LEVELS[route.nivel].color;
  L.polyline(route.lineas, {renderer: selectionRenderer, color: "#ffffff", weight: 11, opacity: 0.9, lineCap: "round",
    lineJoin: "round", interactive: false}).addTo(selectionLayer);
  L.polyline(route.lineas, {renderer: selectionRenderer, color, weight: 7, opacity: 0.85, lineCap: "round",
    lineJoin: "round", interactive: false}).bindTooltip(`${ROUTE_LEVELS[route.nivel].short} · ${route.label}`,
    {sticky: true, className: "vg-tag"}).addTo(selectionLayer);
}

function initializeFleetMap() {
  const container = $("heatmap-map");
  if (fleetMap || !container) return;
  if (!window.L) {
    container.classList.add("map-unavailable");
    container.textContent = "No fue posible cargar la cartografía. Revisa la conexión a Internet y actualiza la página.";
    return;
  }
  container.replaceChildren();
  fleetMap = ArtisaMapaBase.crear(container, {pedir: mapPedir, centro: [-24.5, -70], zoom: 6}).mapa;
  // Las rutas van sobre las faenas (405): el relleno de una faena taparía los clics de la ruta que la cruza.
  for (const [pane, zIndex] of [["usagePane", 410], ["trailPane", 600], ["selectionPane", 620], ["truckPane", 650]]) {
    fleetMap.createPane(pane).style.zIndex = String(zIndex);
  }
  // SVG y no canvas: un lienzo cubre todo el mapa y se queda con los clics de lo que está
  // debajo; con SVG solo la línea recibe el clic.
  usageRenderer = L.svg({pane: "usagePane", padding: 0.3});
  trailRenderer = L.svg({pane: "trailPane"});
  selectionRenderer = L.svg({pane: "selectionPane"});
  usageLayer = L.layerGroup();
  trailLayer = L.layerGroup();
  truckLayer = L.layerGroup();
  selectionLayer = L.featureGroup().addTo(fleetMap);
  // De lejos los camiones se ven pequeños para no tapar el mapa.
  const compact = () => container.classList.toggle("is-far", fleetMap.getZoom() < 9);
  fleetMap.on("zoomend", compact);
  compact();
  vgApplyLayers();
}

function vgApplyLayers() {
  if (!fleetMap) return;
  const show = (layer, visible) => {
    if (visible && !fleetMap.hasLayer(layer)) fleetMap.addLayer(layer);
    if (!visible && fleetMap.hasLayer(layer)) fleetMap.removeLayer(layer);
  };
  const selected = vg.selected || {};
  show(usageLayer, vg.layers.usage && Boolean(vg.data));
  // Con algo seleccionado se ve solo eso: un camión, o la alerta con su camión.
  show(trailLayer, vg.layers.trails && !vg.selected);
  show(truckLayer, vg.layers.vehicles || Boolean(vg.selected));
  const only = selected.kind === "vehicle" ? selected.id
    : selected.kind === "alert" ? vgAlertById(selected.id)?.patente : null;
  for (const [vehicleId, marker] of truckMarkers) {
    const visible = only ? vehicleId === only : vg.layers.vehicles;
    if (visible && !truckLayer.hasLayer(marker)) truckLayer.addLayer(marker);
    if (!visible && truckLayer.hasLayer(marker)) truckLayer.removeLayer(marker);
  }
  for (const input of document.querySelectorAll("[data-map-layer]")) input.checked = vg.layers[input.dataset.mapLayer];
  for (const button of document.querySelectorAll("[data-zone-filter]")) {
    const active = button.dataset.zoneFilter === vg.zone;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = !vg.layers.usage;
  }
}

// --- Carga ---
// Mientras no haya datos se reintenta cada VG_RETRY_MS (no cada minuto), en cualquier pestaña.
const VG_RETRY_MS = 15000;
let vgRetry = null;

async function loadVigilance(quiet = true) {
  if (!token() || vg.loading) return;
  vg.loading = true;
  clearTimeout(vgRetry);
  initializeFleetMap();  // el mapa base se ve de inmediato, aunque los datos sigan en camino
  const version = sessionVersion;
  try {
    const data = await api("/api/vigilancia", {timeoutMs: 90000});
    if (version !== sessionVersion) return;
    // Las líneas de las rutas solo se piden cuando hay una versión nueva del modelo.
    if (data.status === "ok" && vg.model?.version !== data.modelo.version) await loadLearnedRoutes();
    if (version !== sessionVersion) return;
    renderVigilance(data);
  } catch (error) {
    if (version !== sessionVersion) return;
    $("heatmap-source-status").textContent = "Sin conexión";
    $("heatmap-source-status").className = "status warning";
    if (vg.data) {
      $("map-last-update").textContent = "No se pudo actualizar (" + error.message + "). Se muestran los últimos datos y se reintenta cada minuto.";
    } else {
      // Primera carga: el servidor puede seguir calculando; se reintenta pronto y solo.
      $("heatmap-source-status").textContent = "Cargando…";
      $("heatmap-source-status").className = "status neutral";
      $("map-last-update").textContent = "Preparando las rutas y los camiones… se reintenta solo.";
      $("alert-list").replaceChildren(el("li", "monitor-empty", quiet ? "Preparando los datos… se reintenta solo."
        : "No se pudo cargar (" + error.message + "). Se reintenta solo en unos segundos."));
      vgRetry = setTimeout(() => loadVigilance(), VG_RETRY_MS);
    }
  } finally {
    if (version === sessionVersion) vg.loading = false;
  }
}

async function loadLearnedRoutes() {
  const model = await api("/api/rutas-aprendidas", {timeoutMs: 90000});
  vg.model = model.status === "ok" ? model : null;
  vg.fitted = false;
  if (fleetMap) vgDrawUsage();
}

function renderVigilance(data) {
  initializeFleetMap();
  if (data.status !== "ok" || !vg.model) {
    vg.data = null;
    vg.alerts = new Map();
    vg.vehicles = [];
    const message = data.message || "El modelo todavía no aprende las rutas.";
    $("heatmap-source-status").textContent = "Modelo pendiente";
    $("heatmap-source-status").className = "status neutral";
    for (const id of ["vg-model", "vg-routes", "vg-moving", "vg-last", "vg-alerts"]) $(id).textContent = "—";
    $("map-last-update").textContent = message;
    $("alert-list").replaceChildren(el("li", "monitor-empty", message));
    $("vehicle-live-list").replaceChildren();
    $("lr-table").replaceChildren();
    $("lr-table-foot").textContent = message;
    $("lr-model").replaceChildren(el("li", "chart-empty", message));
    if (fleetMap) { usageLayer.clearLayers(); trailLayer.clearLayers(); truckLayer.clearLayers(); truckMarkers.clear(); vgApplyLayers(); }
    return;
  }
  vg.data = data;
  vg.alerts = new Map(data.alertas.map((alert) => [vgAlertKey(alert), alert]));
  vg.vehicles = data.vehiculos;
  vgRenderScale();
  vgRenderStats(data);
  vgRenderModel();
  vgRenderTable(data);
  if (fleetMap) {
    if (!usageLayer.getLayers().length) vgDrawUsage();
    vgRenderTrails();
    vgRenderTrucks();
  }
  vgRenderLists();
  vgRestoreSelection();
  vgApplyLayers();
  $("heatmap-method").textContent =
    `Un camión en movimiento, fuera de las faenas, anda en una ruta mínima (N5) o fuera de ruta (N6) durante ` +
    `${format(data.min_minutos)} min o más y a ${format(data.min_km)} km o más de una ruta habitual (N1–N4). Termina al ` +
    "volver a una ruta habitual o entrar a una faena; sin reportes por 30 min queda \"sin señal\". Es algo para revisar, " +
    "no una falta comprobada.";
  if (!vg.fitted && $("heatmap-map").offsetParent !== null) requestAnimationFrame(() => vgFit(false));
  // El historial que incluye hoy se mantiene al día con cada actualización (sin parpadear).
  if (vg.alertFilter === "historial" && vg.history.hasta === localToday() && !vg.history.loading) {
    loadAlertHistory(vg.history.kind, true);
  }
}

function vgRenderScale() {
  $("vg-scale").replaceChildren(...vg.model.niveles.map(({nivel}) => {
    const style = ROUTE_LEVELS[nivel];
    const item = el("span", "vg-bin");
    const swatch = el("i");
    swatch.style.background = style.color;
    swatch.style.height = Math.max(3, style.weight) + "px";
    if (nivel === "N6") swatch.classList.add("is-off");
    item.title = style.label;
    item.append(levelBadge(nivel), swatch, el("small", "", nivel === "N6" ? "Fuera de ruta" : style.short));
    return item;
  }));
}

// Tabla de clasificación: los niveles que aprendió el modelo y cuántos camiones hay en cada uno ahora.
function vgRenderTable(data) {
  const now = data.resumen.camiones_por_nivel || {};
  const head = el("thead");
  const headRow = el("tr");
  for (const label of ["Nivel", "Camiones por ruta", "Rutas", "Recorridos", "Ahora", "Alerta"]) {
    headRow.append(el("th", "", label));
  }
  head.append(headRow);
  const body = el("tbody");
  for (const row of vg.model.niveles) {
    const style = ROUTE_LEVELS[row.nivel];
    const tr = el("tr", row.alerta ? "is-alert" : "");
    const name = el("td", "lr-level");
    name.append(levelBadge(row.nivel), el("span", "", style.short));
    tr.append(name,
      el("td", "", row.nivel === "N6" ? "Sin ruta aprendida cerca" : levelRange(row.nivel)),
      el("td", "lr-num", row.nivel === "N6" ? "—" : format(row.rutas)),
      el("td", "lr-num", row.nivel === "N6" ? "—" : format(row.recorridos)),
      el("td", "lr-num", format(now[row.nivel] || 0)),
      el("td", "lr-alert", row.alerta ? "Sí" : "No"));
    tr.title = style.label;
    body.append(tr);
  }
  $("lr-table").replaceChildren(head, body);
  const missing = 4 - vg.model.niveles.filter((row) => levelNumber(row.nivel) <= 4).length;
  $("lr-table-foot").textContent = "N1 a N4 los define el modelo con los datos de cada versión" + (missing > 0
    ? ` (esta vez aprendió ${format(4 - missing)} ${4 - missing === 1 ? "grupo" : "grupos"})` : "") +
    ". «Ahora»: camiones con señal en los últimos 30 min.";
}

// Ficha del modelo: qué algoritmos usa, con qué datos aprendió y cuándo se reentrena.
function vgRenderModel() {
  const model = vg.model;
  const params = model.parametros || {};
  const step = (title, algorithm, text) => {
    const item = el("li");
    item.append(el("strong", "", title), el("span", "lr-algo", algorithm), el("p", "", text));
    return item;
  };
  $("lr-model").replaceChildren(
    step("Datos", "Viajes reales del cierre diario",
      `${format(model.viajes)} viajes de ${format(model.camiones)} camiones, del ${formatDataDay(model.datos_desde)} ` +
      `al ${formatDataDay(model.datos_hasta)}.`),
    step("1. Lugares", "DBSCAN",
      `Junta las paradas cercanas (${format(params.lugar_radio_m || 500)} m) en lugares: ${format(model.lugares)}.`),
    step("2. Rutas", "Clustering jerárquico",
      `Junta los viajes que siguen el mismo camino entre dos lugares: ${format(model.total_rutas)} rutas.`),
    step("3. Línea", "Medoide", "Cada ruta se dibuja con su viaje real más representativo."),
    step("4. Niveles", "Cortes naturales de Jenks",
      `N5 = ${format(params.max_camiones_minima || 2)} camiones o menos. N1 a N4 los aprende el modelo.`),
    step("5. Previsión", `Versión ${format(model.version)} · se reentrena cada día`,
      `Con los últimos 90 días prevé las rutas del ${formatDataDay(model.prevision_desde)} al ` +
      `${formatDataDay(model.prevision_hasta)}.`),
    step("En vivo", "Nivel de cada camión",
      "Toma el nivel de la ruta a menos de ~1 km; sin ninguna cerca, está fuera de ruta (N6).")
  );
}

function vgRenderStats(data) {
  const model = vg.model;
  $("vg-model").textContent = `${dayLabel(model.prevision_desde, "short")} – ${dayLabel(model.prevision_hasta, "short")}`;
  $("vg-model-d").textContent = `Versión ${format(model.version)} · ${model.cada_dias === 1 ? "se actualiza cada día" : `cada ${format(model.cada_dias)} días`}`;
  const alertRoutes = model.niveles.filter((row) => row.nivel === "N5").reduce((total, row) => total + row.rutas, 0);
  $("vg-routes").textContent = format(model.total_rutas);
  $("vg-routes-d").textContent = `${format(model.total_rutas - alertRoutes)} habituales · ${format(alertRoutes)} mínimas`;
  vgRenderLive();
  const summary = data.resumen;
  $("vg-alerts").textContent = format(summary.activas);
  $("vg-alerts-card").classList.toggle("has-alerts", summary.activas > 0);
  $("vg-alerts-d").textContent = `${format(summary.sin_senal)} sin señal · ${format(summary.terminadas)} terminadas hoy`;
  $("heatmap-source-status").textContent = summary.activas
    ? `${format(summary.activas)} ${summary.activas === 1 ? "alerta activa" : "alertas activas"}`
    : "En vivo · sin alertas";
  $("heatmap-source-status").className = "status " + (summary.activas ? "warning" : "info");
}

// Lo que cambia cada minuto con las posiciones: en movimiento, último dato, pie del mapa.
function vgRenderLive() {
  if (!vg.data) return;
  const recent = vg.vehicles.filter((v) => Date.now() - new Date(v.timestamp).getTime() <= 15 * 60 * 1000);
  $("vg-moving").textContent = format(recent.filter((v) => Number(v.speed_kmh || 0) > 3).length);
  $("vg-moving-d").textContent = `de ${format(recent.length)} con señal (15 min)`;
  const last = vg.vehicles.reduce((top, v) => (!top || v.timestamp > top ? v.timestamp : top), null);
  $("vg-last").textContent = last ? formatAge((Date.now() - new Date(last).getTime()) / 1000) : "—";
  $("vg-last-d").textContent = last ? formatLocal(last) : "Sin reportes";
  const stale = last && Date.now() - new Date(last).getTime() > 15 * 60 * 1000;
  $("vg-last").closest(".atlas-stat").classList.toggle("is-stale", Boolean(stale));
  $("map-last-update").textContent = (stale
    ? "Sin datos nuevos: se muestra la última posición"
    : "Toca un camino para ver su detalle") + " · " + new Intl.DateTimeFormat("es-CL", {
    hour: "2-digit", minute: "2-digit", timeZone: HISTORY_ZONE}).format(new Date());
}

// Sin selección se ven los recorridos de las alertas activas y sin señal (las de la semana no).
function vgRenderTrails() {
  trailLayer.clearLayers();
  for (const [id, alert] of vg.alerts) {
    if (alert.estado === "terminada") continue;
    const line = L.polyline(alert.estela, {renderer: trailRenderer, color: alert.estado === "activa" ? "#d7301f" : "#8a6a64",
      weight: 3, opacity: 0.85, dashArray: "6 6", lineCap: "round"});
    line.bindTooltip(el("span", "", `${alert.patente} · ${VG_STATES[alert.estado].label} · desde ${vgTime(alert.inicio)}`), {sticky: true});
    line.on("click", (event) => { L.DomEvent.stopPropagation(event); vgSelect({kind: "alert", id}); });
    line.addTo(trailLayer);
  }
}

function vgRenderTrucks() {
  truckLayer.clearLayers();
  truckMarkers.clear();
  for (const vehicle of vg.vehicles) {
    if (!Number.isFinite(Number(vehicle.latitude)) || !Number.isFinite(Number(vehicle.longitude))) continue;
    const marker = L.marker([vehicle.latitude, vehicle.longitude], {pane: "truckPane", icon: truckIcon(vehicle),
      zIndexOffset: vehicle.en_alerta ? 2000 : 1000}).bindPopup(truckPopup(vehicle));
    marker.on("click", () => vgSelect({kind: "vehicle", id: vehicle.vehicle_id}));
    marker.on("popupopen", () => { vg.openPopup = vehicle.vehicle_id; });
    marker.on("popupclose", () => { if (vg.openPopup === vehicle.vehicle_id) vg.openPopup = null; });
    marker.addTo(truckLayer);
    truckMarkers.set(vehicle.vehicle_id, marker);
  }
  vgApplyLayers();
  vgMarkSelection();
  if (vg.openPopup && truckMarkers.has(vg.openPopup) && truckLayer.hasLayer(truckMarkers.get(vg.openPopup))) {
    truckMarkers.get(vg.openPopup).openPopup();
  }
}

// --- Listas ---
function vgRenderLists() {
  vgRenderAlertList();
  vgRenderVehicleList();
}

function vgMatches(text) {
  const query = $("monitor-search").value.trim().toLocaleLowerCase("es");
  return !query || text.toLocaleLowerCase("es").includes(query);
}

const VG_FILTERS = {activa: "Activas", sin_senal: "Sin señal", historial: "Historial"};

// Resumen de una alerta en una línea: cuándo, cuánto duró y cuánto anduvo así.
function vgAlertWhen(alert, withDay) {
  const start = withDay ? vgTime(alert.inicio) : vgClock(alert.inicio);
  return alert.estado === "terminada" ? `${start} → ${vgClock(alert.fin)} · ${vgDuration(alert.minutos)}`
    : `desde ${start} · ${vgDuration(alert.minutos)}`;
}

function vgAlertFacts(alert) {
  const far = alert.km_al_corredor === null || alert.km_al_corredor === undefined ? "a más de 130 km"
    : `hasta ${format(alert.km_al_corredor, 1)} km`;
  return `${format(alert.km_recorridos, 1)} km recorridos · ${far} de una ruta habitual`;
}

function vgAlertItem(id, alert, withDay) {
  const state = VG_STATES[alert.estado];
  const item = el("li");
  const button = el("button", "vg-alert " + state.className);
  button.type = "button";
  button.dataset.alertSelect = id;
  const head = el("span", "vg-alert-head");
  head.append(el("b", "vg-state " + state.className, state.label), el("small", "", vgAlertWhen(alert, withDay)));
  const who = el("span", "vg-alert-who");
  who.append(el("span", "plate-chip", alert.patente), el("strong", "", alert.nombre || "Sin nombre"));
  button.append(head, who, vgWhere(alert), el("span", "vg-alert-facts", vgAlertFacts(alert)),
    el("span", "vg-alert-driver", "Conductor: " + (alert.conductor || "no informado")));
  const cuts = alert.cortes || [];
  if (cuts.length) {
    const open = cuts.find((cut) => !cut.hasta);
    const frozen = cuts.some((cut) => cut.tipo === "gps_congelado");
    button.append(el("span", "vg-alert-cut" + (frozen ? " is-frozen" : ""), open
      ? `${VG_CUTS[open.tipo].label} desde ${vgClock(open.desde)}`
      : `${format(cuts.length)} ${cuts.length === 1 ? "corte" : "cortes"} de señal · ${vgDuration(vgCutsMinutes(alert))} sin posición`
        + (frozen ? " · GPS sin posición nueva" : "")));
  }
  button.addEventListener("click", () => vgSelect({kind: "alert", id}));
  item.append(button);
  return item;
}

function vgRenderAlertList() {
  const list = $("alert-list");
  const alerts = [...vg.alerts.entries()];
  const count = (state) => alerts.filter(([, a]) => a.estado === state).length;
  $("alert-count").textContent = format(count("activa"));
  for (const button of document.querySelectorAll("[data-alert-filter]")) {
    const filter = button.dataset.alertFilter;
    button.textContent = filter === "historial" ? VG_FILTERS.historial : `${VG_FILTERS[filter]} ${format(count(filter))}`;
    button.classList.toggle("is-active", vg.alertFilter === filter);
  }
  const history = vg.alertFilter === "historial";
  $("vg-history").hidden = !history || vg.tab !== "alerts";
  if (history) {
    vgRenderHistory();
    return;
  }
  const visible = alerts.filter(([, a]) => a.estado === vg.alertFilter &&
    vgMatches(`${a.patente} ${a.nombre || ""} ${a.conductor || ""} ${a.ubicacion || ""}`));
  list.replaceChildren(...visible.map(([id, alert]) => vgAlertItem(id, alert, false)));
  if (!visible.length) {
    const empty = {activa: "Sin alertas en este momento.", sin_senal: "Ningún camión en alerta dejó de reportar."}[vg.alertFilter];
    list.append(el("li", "monitor-empty", alerts.length && !vgMatches("") ? "No hay alertas para esta búsqueda." : empty));
  }
  vgMarkSelection();
}

// --- Historial: el registro de alertas guardado por Machine (/api/alertas) ---
function vgHistoryRange(kind) {
  const end = $("vg-history-day").value || localToday();
  const start = kind === "semana" ? shiftDay(end, -6) : kind === "mes" ? end.slice(0, 8) + "01" : end;
  return [start, end];
}

async function loadAlertHistory(kind = "dia", quiet = false) {
  const day = $("vg-history-day");
  if (!day.value) day.value = localToday();
  day.max = localToday();
  const [desde, hasta] = vgHistoryRange(kind);
  vg.history = {...vg.history, desde, hasta, kind, loading: !quiet, error: null};
  if (!quiet) vgRenderHistory();
  try {
    const data = await api(`/api/alertas?desde=${desde}&hasta=${hasta}`, {timeoutMs: 60000});
    vg.history.alerts = new Map(data.alertas.map((alert) => [vgAlertKey(alert), alert]));
  } catch (error) {
    vg.history.error = error.message;
  }
  vg.history.loading = false;
  if (vg.alertFilter === "historial") vgRenderHistory();
}

function vgRenderHistory() {
  const list = $("alert-list");
  const history = vg.history;
  if (!history.desde) { loadAlertHistory(); return; }
  for (const button of document.querySelectorAll("[data-history-range]")) {
    button.classList.toggle("is-active", button.dataset.historyRange === (history.kind || "dia"));
  }
  const total = history.alerts.size;
  $("vg-history-csv").disabled = !total;
  const range = history.desde === history.hasta ? dayLabel(history.desde, "long")
    : `${dayLabel(history.desde, "short")} – ${dayLabel(history.hasta, "short")}`;
  $("vg-history-note").textContent = history.loading ? "Cargando…" : history.error ? "No se pudo cargar: " + history.error
    : `${format(total)} ${total === 1 ? "alerta terminada" : "alertas terminadas"} · ${range}`;
  if (history.loading || history.error) { list.replaceChildren(); return; }
  const visible = [...history.alerts.entries()].filter(([, a]) =>
    vgMatches(`${a.patente} ${a.nombre || ""} ${a.conductor || ""} ${a.ubicacion || ""}`));
  list.replaceChildren(...visible.map(([id, alert]) => vgAlertItem(id, alert, history.desde !== history.hasta)));
  if (!visible.length) list.append(el("li", "monitor-empty", total ? "No hay alertas para esta búsqueda."
    : "Ninguna alerta terminada en estas fechas. Las que siguen abiertas están en Activas y Sin señal."));
  vgMarkSelection();
}

// Una fila por alerta, para revisarlas en Excel.
function downloadAlertHistory() {
  const history = vg.history;
  const header = ["Patente", "Camión", "Conductor", "Inicio (hora Chile)", "Fin (hora Chile)", "Duración (min)",
    "Nivel", "Ruta", "Km recorridos", "Km máximos a una ruta habitual", "Zona", "Salió de la ruta en (lat, lon)",
    "Terminó en (lat, lon)", "Cortes de señal", "Minutos sin posición", "GPS sin posición nueva",
    "Detalle de los cortes", "Versión del modelo"];
  const number = (value) => value === null || value === undefined ? "" : format(value, 1).replace(/\./g, "");
  const point = (p) => p && Number.isFinite(Number(p[0])) ? `${Number(p[0]).toFixed(5)}, ${Number(p[1]).toFixed(5)}` : "";
  const lines = [header.map(csvValue).join(";")];
  for (const alert of history.alerts.values()) {
    lines.push([alert.patente, alert.nombre, alert.conductor,
      csvLocalDateTime(alert.inicio), csvLocalDateTime(alert.fin), alert.minutos,
      alert.nivel === "N6" ? "N6 · fuera de ruta" : "N5 · ruta mínima", alert.ruta_label, number(alert.km_recorridos),
      number(alert.km_al_corredor), alert.ubicacion, point(alert.estela?.[0]?.[0]), point(alert.posicion),
      (alert.cortes || []).length, vgCutsMinutes(alert), (alert.cortes || []).some((cut) => cut.tipo === "gps_congelado") ? "Sí" : "No",
      (alert.cortes || []).map(vgCutText).join(" | "), alert.version_modelo].map(csvValue).join(";"));
  }
  saveCsv(lines, `artisa-alertas-${history.desde}${history.hasta !== history.desde ? "-a-" + history.hasta : ""}.csv`);
}

function vgRenderVehicleList() {
  const list = $("vehicle-live-list");
  $("vehicle-live-count").textContent = format(vg.vehicles.length);
  const ordered = vg.vehicles
    .filter((v) => vgMatches(`${v.vehicle_id} ${v.vehicle_name || ""} ${vehicleDriver(v)}`))
    .sort((a, b) => (b.en_alerta - a.en_alerta) || Number(b.speed_kmh || 0) - Number(a.speed_kmh || 0));
  list.replaceChildren(...ordered.map((vehicle) => {
    const status = vehicleStatus(vehicle);
    const item = el("li");
    const button = el("button", "vehicle-live-row " + status.className + (vehicle.en_alerta ? " is-alert" : ""));
    button.type = "button";
    button.dataset.vehicleSelect = vehicle.vehicle_id;
    const icon = el("span", "vehicle-list-icon");
    icon.innerHTML = TRUCK_MARKUP;
    const copy = el("span");
    const identity = el("small", "fleet-identity");
    const driver = vehicleDriver(vehicle);
    identity.append(...truckIdentity(vehicle.vehicle_id, driver === "No informado" ? null : driver, null));
    copy.append(el("strong", "", vehicle.vehicle_name || vehicle.vehicle_id), identity);
    const state = el("em", "vehicle-live-state", vehicle.en_alerta ? ROUTE_LEVELS[vehicle.nivel || "N6"].short
      : status.className === "moving" && Number(vehicle.speed_kmh) > 3 ? format(vehicle.speed_kmh, 0) + " km/h" : truckAgeText(vehicle));
    if (vehicle.nivel) state.prepend(levelBadge(vehicle.nivel));
    button.append(icon, copy, state);
    button.addEventListener("click", () => vgSelect({kind: "vehicle", id: vehicle.vehicle_id}));
    item.append(button);
    return item;
  }));
  if (!ordered.length) list.append(el("li", "monitor-empty", "No hay camiones para esta búsqueda."));
  vgMarkSelection();
}

function setMonitorTab(tab) {
  vg.tab = tab === "vehicles" ? "vehicles" : "alerts";
  for (const button of document.querySelectorAll("[data-monitor-tab]")) {
    const selected = button.dataset.monitorTab === vg.tab;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-selected", String(selected));
  }
  for (const list of document.querySelectorAll("[data-monitor-list]")) list.hidden = list.dataset.monitorList !== vg.tab;
  $("vg-alert-filter").hidden = vg.tab !== "alerts";
  $("vg-history").hidden = vg.tab !== "alerts" || vg.alertFilter !== "historial";
  vgRenderLists();
}

// --- Selección: se ve solo lo elegido; "Vista general" vuelve a todo ---
function vgSelect(selection, focus = true) {
  vg.selected = selection;
  selectionLayer.clearLayers();
  if (usageLayer) vgDrawUsage();
  if (fleetMap) fleetMap.closePopup();
  if (selection.kind === "alert") {
    const alert = vgAlertById(selection.id);
    if (!alert) return vgReset(false);
    vgApplyLayers();
    vgShowAlert(alert, focus);
  } else {
    const vehicle = vg.vehicles.find((v) => v.vehicle_id === selection.id);
    if (!vehicle) return vgReset(false);
    vgApplyLayers();
    vgShowVehicle(vehicle, focus);
  }
  vgMarkSelection();
}

function vgFact(value, label) {
  const box = el("div", "vg-fact");
  box.append(el("strong", "", value), el("small", "", label));
  return box;
}

function vgCard(kind, state, title, extra) {
  const detail = $("map-selection-detail");
  detail.classList.add("vg-card");
  const head = el("div", "vg-card-head");
  head.append(el("span", "vg-card-kind", kind));
  if (state) head.append(state);
  detail.replaceChildren(head, title, ...extra);
}

// Dónde anda: en qué ruta mínima o si está fuera de toda ruta aprendida.
function vgWhere(item) {
  const where = el("span", "vg-where");
  const level = item.nivel || "N6";
  where.append(levelBadge(level), el("span", "", level === "N6" ? "Fuera de ruta: camino que el modelo no conoce"
    : `${ROUTE_LEVELS[level].short}${item.ruta_label ? " · " + item.ruta_label : ""}`));
  return where;
}

function vgShowAlert(alert, focus) {
  const state = VG_STATES[alert.estado];
  // La ruta mínima por la que anduvo, resaltada debajo de su recorrido.
  vgHighlightRoute(alert.ruta);
  // Recorrido fuera de ruta (con borde blanco), su inicio y la ruta habitual más cercana.
  // Sin ruta mínima debajo (fuera de ruta) el recorrido lleva borde blanco para verse sobre el mapa;
  // sobre una ruta mínima va sin borde, así se ve la ruta roja a ambos lados.
  if (!alert.ruta) {
    L.polyline(alert.estela, {renderer: selectionRenderer, color: "#ffffff", weight: 8, opacity: 0.95, lineCap: "round", interactive: false}).addTo(selectionLayer);
  }
  L.polyline(alert.estela, {renderer: selectionRenderer, color: "#172d32", weight: alert.ruta ? 3 : 4, opacity: 1, lineCap: "round",
    lineJoin: "round", interactive: false}).bindTooltip(`Recorrido de ${alert.patente} en alerta`,
    {sticky: true, className: "vg-tag"}).addTo(selectionLayer);
  L.circleMarker(alert.estela[0][0], {renderer: selectionRenderer, radius: 6, color: "#d7301f", weight: 3, fillColor: "#fff", fillOpacity: 1})
    .bindTooltip("Salió de las rutas habituales · " + vgTime(alert.inicio), {direction: "top", className: "vg-tag"})
    .addTo(selectionLayer);
  if (alert.corredor) {
    L.polyline([alert.posicion, alert.corredor.punto], {renderer: selectionRenderer, color: "#173f35", weight: 2,
      dashArray: "3 6", opacity: 0.85, interactive: false}).addTo(selectionLayer);
    L.circleMarker(alert.corredor.punto, {renderer: selectionRenderer, radius: 6, color: "#fff", weight: 3, fillColor: "#1f6b50", fillOpacity: 1})
      .bindTooltip(`Ruta habitual más cercana · ${format(alert.corredor.km, 1)} km`, {permanent: true, direction: "auto", className: "vg-tag"})
      .addTo(selectionLayer);
  }
  // Cortes de señal: número donde se perdió la posición, punto hueco donde reapareció y una
  // línea punteada tenue entre ambos (no es un camino recorrido).
  (alert.cortes || []).forEach((cut, k) => {
    const color = VG_CUTS[cut.tipo].color;
    if (cut.despues) {
      L.polyline([cut.antes, cut.despues], {renderer: selectionRenderer, color, weight: 1.5, dashArray: "2 6",
        lineCap: "round", opacity: 0.55, interactive: false}).addTo(selectionLayer);
      L.circleMarker(cut.despues, {renderer: selectionRenderer, radius: 4, color, weight: 2, fillColor: "#fff", fillOpacity: 1})
        .bindTooltip(`Corte ${k + 1} · reapareció ${vgClock(cut.hasta)}`, {direction: "top", className: "vg-tag"}).addTo(selectionLayer);
    }
    L.marker(cut.antes, {icon: vgCutIcon(k + 1, cut), pane: "selectionPane", zIndexOffset: 500})
      .bindTooltip(`Corte ${k + 1} · ${VG_CUTS[cut.tipo].short} desde ${vgClock(cut.desde)}`, {direction: "top", className: "vg-tag"})
      .addTo(selectionLayer);
  });
  if (!truckMarkers.has(alert.patente)) {
    L.circleMarker(alert.posicion, {renderer: selectionRenderer, radius: 8, color: "#fff", weight: 3, fillColor: "#d7301f", fillOpacity: 1})
      .bindTooltip("Último dato · " + vgTime(alert.fin), {direction: "top", className: "vg-tag"}).addTo(selectionLayer);
  }
  const title = el("div", "vg-card-title");
  title.append(el("span", "plate-chip", alert.patente), el("strong", "", alert.nombre || "Sin nombre"));
  const facts = el("div", "vg-facts-grid");
  facts.append(
    vgFact(alert.km_al_corredor === null ? "+130 km" : format(alert.km_al_corredor, 1) + " km", "de una ruta habitual"),
    vgFact(vgDuration(alert.minutos), alert.estado === "terminada" ? "duró" : "hasta ahora"),
    vgFact(format(alert.km_recorridos, 1) + " km", "recorridos así"),
    vgFact(alert.velocidad === null || alert.velocidad === undefined ? "—" : format(alert.velocidad, 0) + " km/h", "último dato " + vgTime(alert.fin).split(", ").pop()));
  const lines = [
    vgWhere(alert),
    el("p", "vg-card-line", "Conductor: " + (alert.conductor || "no informado")),
    el("p", "vg-card-line", vgAlertWhen(alert, true)),
    facts
  ];
  if (alert.cortes?.length) lines.push(vgCutsBlock(alert.cortes));
  else if (alert.estela.length > 1) {
    const gaps = alert.estela.length - 1;
    lines.push(el("p", "vg-card-line", `${format(gaps)} ${gaps === 1 ? "hueco" : "huecos"} de más de 5 min sin reportes (no se dibuja${gaps === 1 ? "" : "n"} como camino)`));
  }
  if (alert.ubicacion) lines.push(el("p", "vg-card-place", alert.ubicacion));
  const link = el("a", "selection-link", "Ver su día completo →");
  link.href = `#camiones/${encodeURIComponent(alert.patente)}/${vgLocalDay(alert.fin)}`;
  lines.push(link);
  vgCard("Alerta", el("b", "vg-state " + state.className, state.label), title, lines);
  if (focus) {
    const bounds = L.latLngBounds(alert.estela.flat());
    for (const cut of alert.cortes || []) { bounds.extend(cut.antes); if (cut.despues) bounds.extend(cut.despues); }
    if (alert.corredor) bounds.extend(alert.corredor.punto);
    fleetMap.fitBounds(bounds, {padding: [60, 60], maxZoom: 13});
  }
}

function vgCutRow(cut, number) {
  const kind = VG_CUTS[cut.tipo];
  const row = el("button", "vg-cut");
  row.type = "button";
  row.style.setProperty("--cut", kind.color);
  const extra = [];
  if (cut.velocidad_antes !== null && cut.velocidad_antes !== undefined) extra.push(`iba a ${format(cut.velocidad_antes, 0)} km/h`);
  if (cut.hasta && cut.km_linea !== null) extra.push(`reapareció a ${format(cut.km_linea, 1)} km`);
  if (!cut.hasta && cut.tipo === "gps_congelado" && cut.ultima_copia) extra.push(`última transmisión ${vgClock(cut.ultima_copia)}`);
  row.title = [`${kind.label}: ${kind.note}.`, cut.evento_antes ? `Último evento: ${cut.evento_antes}.` : "",
    cut.kmh_necesaria ? `Promedio necesario: ${format(cut.kmh_necesaria, 0)} km/h.` : ""].filter(Boolean).join(" ");
  const main = el("span", "vg-cut-main");
  main.append(el("b", "vg-cut-n", String(number)), el("span", "vg-cut-kind", kind.short),
    el("span", "vg-cut-when", `${vgClock(cut.desde)} → ${cut.hasta ? vgClock(cut.hasta) : "sigue"}`),
    el("strong", "vg-cut-min", vgDuration(cut.minutos)));
  row.append(main);
  if (extra.length) row.append(el("span", "vg-cut-extra", extra.join(" · ")));
  row.addEventListener("click", () => {
    const bounds = L.latLngBounds([cut.antes, cut.despues || cut.antes]);
    fleetMap.fitBounds(bounds.pad(0.6), {maxZoom: 13});
  });
  return row;
}

function vgCutsBlock(cuts) {
  const block = el("div", "vg-cuts");
  const total = cuts.reduce((sum, cut) => sum + cut.minutos, 0);
  const head = el("p", "vg-cuts-title");
  head.append(el("span", "", cuts.length === 1 ? "Corte de señal" : `${format(cuts.length)} cortes de señal`),
    el("small", "", `${vgDuration(total)} sin posición`));
  block.append(head);
  const rows = cuts.map((cut, k) => vgCutRow(cut, k + 1));
  block.append(...rows.slice(0, VG_CUTS_SHOWN));
  if (rows.length > VG_CUTS_SHOWN) {
    const more = el("details", "vg-cuts-more");
    more.append(el("summary", "", `Ver ${format(rows.length - VG_CUTS_SHOWN)} más`), ...rows.slice(VG_CUTS_SHOWN));
    block.append(more);
  }
  const kinds = [...new Set(cuts.map((cut) => cut.tipo))];
  block.append(el("p", "vg-cuts-legend", kinds.map((kind) => `${VG_CUTS[kind].short}: ${VG_CUTS[kind].note}`).join(" · ")
    + ". Toca un corte para verlo en el mapa."));
  return block;
}

function vgShowVehicle(vehicle, focus) {
  const status = vehicleStatus(vehicle);
  vgHighlightRoute(vehicle.ruta);
  const title = el("div", "vg-card-title");
  title.append(el("span", "plate-chip", vehicle.vehicle_id), el("strong", "", vehicle.vehicle_name || vehicle.vehicle_id));
  const facts = el("div", "vg-facts-grid");
  facts.append(
    vgFact(vehicle.speed_kmh === null || vehicle.speed_kmh === undefined ? "—" : format(vehicle.speed_kmh, 0) + " km/h", vehicle.event || "velocidad"),
    vgFact(truckAgeText(vehicle).replace(/^hace /, ""), "desde el último dato"));
  const link = el("a", "selection-link", "Ver su recorrido de hoy →");
  link.href = "#camiones/" + encodeURIComponent(vehicle.vehicle_id);
  vgCard("Camión", el("b", "vg-state " + (vehicle.en_alerta ? "active" : status.className === "moving" ? "ok" : "done"),
    vehicle.en_alerta ? "En alerta" : status.label), title, [
    vehicle.nivel ? vgWhere(vehicle) : el("p", "vg-card-line", "Sin posición de los últimos 30 min: no se compara con las rutas"),
    el("p", "vg-card-line", "Conductor: " + vehicleDriver(vehicle)),
    el("p", "vg-card-line", "Último dato: " + formatLocal(vehicle.timestamp)),
    facts, link]);
  const marker = truckMarkers.get(vehicle.vehicle_id);
  if (marker && focus) {
    fleetMap.setView(marker.getLatLng(), Math.max(fleetMap.getZoom(), 11));
    marker.openPopup();
  }
}

function vgMarkSelection() {
  const selected = vg.selected || {};
  for (const button of document.querySelectorAll("[data-alert-select]")) {
    button.classList.toggle("is-active", selected.kind === "alert" && button.dataset.alertSelect === selected.id);
  }
  for (const button of document.querySelectorAll("[data-vehicle-select]")) {
    button.classList.toggle("is-active", selected.kind === "vehicle" && button.dataset.vehicleSelect === selected.id);
  }
}

function vgRestoreSelection() {
  if (!vg.selected) return;
  const exists = vg.selected.kind === "alert" ? Boolean(vgAlertById(vg.selected.id))
    : vg.vehicles.some((v) => v.vehicle_id === vg.selected.id);
  if (exists) vgSelect(vg.selected, false); else vgReset(false);
}

function vgReset(fit = true) {
  vg.selected = null;
  if (selectionLayer) selectionLayer.clearLayers();
  if (usageLayer) vgDrawUsage();
  if (fleetMap) fleetMap.closePopup();
  $("map-selection-detail").classList.remove("vg-card");
  setSelectionDetail("Selección", "Elige una alerta o un camión",
    "El mapa mostrará solo lo suyo.");
  vgApplyLayers();
  vgMarkSelection();
  if (fit) vgFit(true);
}

function vgFit(force) {
  if (!fleetMap || !vg.data || (vg.fitted && !force) || vg.selected) return;
  fleetMap.invalidateSize();
  const bounds = L.latLngBounds([]);
  for (const v of vg.vehicles) if (Number.isFinite(Number(v.latitude))) bounds.extend([v.latitude, v.longitude]);
  for (const route of vg.model?.rutas || []) {
    if (levelNumber(route.nivel) > 4) continue;
    for (const line of route.lineas) { bounds.extend(line[0]); bounds.extend(line[line.length - 1]); }
  }
  if (bounds.isValid()) { fleetMap.fitBounds(bounds, {padding: [28, 28], maxZoom: 11}); vg.fitted = true; }
}

// Cada minuto llegan posiciones nuevas (/api/vehiculos/posiciones): se mueven los camiones; el
// nivel de cada uno y las alertas se recalculan con /api/vigilancia.
function vgMergePositions(incoming) {
  const fresh = new Map(incoming.map((v) => [v.vehicle_id, v]));
  vg.vehicles = vg.vehicles.map((vehicle) => {
    const update = fresh.get(vehicle.vehicle_id);
    if (!update || Date.parse(update.timestamp) <= Date.parse(vehicle.timestamp)) return vehicle;
    return {...vehicle, ...update, en_alerta: vehicle.en_alerta, nivel: vehicle.nivel, ruta: vehicle.ruta,
      ruta_label: vehicle.ruta_label,
      driver_name: update.driver_name || vehicle.driver_name,
      driver_last_name: update.driver_name ? update.driver_last_name : vehicle.driver_last_name};
  });
  if (fleetMap) vgRenderTrucks();
  vgRenderVehicleList();
  vgRenderLive();
}

// Los cuatro mapas comparten fondo, selector de capas, faenas y densidad (mapa-base.js).
const mapPedir = (path, timeoutMs) => api(path, {timeoutMs});

// --- Dashboard: rendimiento por camión ------------------------------------------------------
// Una sola tabla: cada camión con todas sus cifras del período (/api/dashboard/camiones), su
// nivel de reporte A–D y su último día con datos. Al abrir un camión se ve su detalle día por
// día (/api/dashboard/camion). No hay totales de la flota: todo es por camión.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const sum = (values) => values.reduce((total, value) => total + value, 0);  // suma de una lista
const dashMonthLabel = (month, style = "short") => new Intl.DateTimeFormat("es-CL", style === "short"
  ? {month: "short", timeZone: "UTC"} : {month: "long", year: "numeric", timeZone: "UTC"})
  .format(new Date(month + "-15T12:00:00Z")).replace(".", "");
const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);
// "12 de jun" (con el año si no es el actual): fechas cortas que no se confunden entre meses.
const dbDay = (iso) => new Intl.DateTimeFormat("es-CL", {day: "numeric", month: "short", timeZone: "UTC",
  ...(iso.slice(0, 4) !== localToday().slice(0, 4) ? {year: "numeric"} : {})}).format(dayDate(iso)).replace(".", "");

const DB_LEVELS = {
  A: "El 25 % que más reporta", B: "Sobre la mitad de la flota",
  C: "Bajo la mitad de la flota", D: "El 25 % que menos reporta"
};
const detainedPer100 = (truck) => truck.km >= 10 ? truck.detenido / truck.km * 100 : null;
const DB_COLUMNS = [
  {key: "patente", label: "Camión", text: true},
  {key: "nivel", label: "Nivel", text: true, title: "Nivel de reporte del período: A es el 25 % que más reporta y D el 25 % que menos"},
  {key: "reportes", label: "Reportes GPS", show: (t) => format(t.reportes)},
  {key: "km", label: "Km", show: (t) => format(t.km, 1)},
  {key: "dias", label: "Días con datos", show: (t) => format(t.dias)},
  {key: "paradas", label: "Paradas", show: (t) => format(t.paradas), title: "Paradas de 10 minutos o más"},
  {key: "detenido", label: "Tiempo detenido", show: (t) => formatMinutes(t.detenido)},
  {key: "por100", label: "Detenido / 100 km", show: (t) => t.por100 === null ? "—" : `${format(t.por100, 0)} min`,
    title: "Minutos detenido por cada 100 km recorridos (solo con 10 km o más)"},
  {key: "ultimo", label: "Último día con datos", text: true}
];

// Vista de la tabla: cifras del período o reportes (o km) de cada camión mes a mes.
function dbColumns() {
  if (dbState.view !== "meses") return DB_COLUMNS;
  const data = dbState.data;
  const decimals = dbState.monthMetric === "km" ? 1 : 0;
  const years = new Set(data.meses.map((month) => month.slice(0, 4))).size > 1;
  const months = data.meses.map((month) => ({key: "m:" + month, month,
    label: capitalize(dashMonthLabel(month)) + (years ? " " + month.slice(2, 4) : ""),
    title: capitalize(dashMonthLabel(month, "long")) + (month === data.mes_en_curso ? " (en curso)" : ""),
    show: (t) => t["m:" + month] ? format(t["m:" + month], decimals) : "—"}));
  return [DB_COLUMNS[0], DB_COLUMNS[1], ...months,
    {key: "mtotal", label: "Total", show: (t) => format(t.mtotal, decimals)}, DB_COLUMNS[DB_COLUMNS.length - 1]];
}

var dbState = {data: null, period: null, periods: {}, requests: {}, query: "", filter: "todos", sort: "reportes",
  dir: -1, open: null, days: {}, rank: "paradas", minDays: 0, minKm: 0,
  rankOrder: "desc", rankLimit: 8, lowLimit: 8, anomPeriod: null, view: "periodo", monthMetric: "reportes", dayMetric: "km"};
const DB_RANKS = {
  paradas: {title: "Camiones con más paradas", color: "#d97941", value: (t) => t.paradas, show: (t) => `${format(t.paradas)} paradas`,
    detail: (t) => `${formatMinutes(t.detenido)} detenido · ${format(t.km, 1)} km`},
  detenido: {title: "Camiones con más tiempo detenido", color: "#b84d3f", value: (t) => t.detenido, show: (t) => formatMinutes(t.detenido),
    detail: (t) => `${format(t.paradas)} paradas · ${t.paradas ? format(t.detenido / t.paradas, 0) : "—"} min por parada`},
  km: {title: "Camiones con más km", color: "#287a57", value: (t) => t.km, show: (t) => `${format(t.km, 1)} km`,
    detail: (t) => `${format(t.dias)} días con datos · ${format(t.reportes)} reportes`}
};
const DB_EFFICIENCY_KM = 100;  // "Más detención por distancia" solo compara camiones con 100 km o más
const DB_REFRESH_MS = 5 * 60 * 1000;

// Período elegido como rango de días: un mes (hasta hoy si está en curso) o todo el historial.
function dbRange(period = dbState.period) {
  const data = dbState.data;
  if (period === "todos") return {desde: data.desde, hasta: data.hoy};
  const [year, month] = period.split("-").map(Number);
  const last = new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10);
  return {desde: period + "-01" < data.desde ? data.desde : period + "-01", hasta: last > data.hoy ? data.hoy : last};
}

const dbPeriodLabel = (period) => period === "todos" ? "todo el historial" : dashMonthLabel(period, "long");

function renderDashboard(data) {
  if (!data || data.status !== "ok") {
    dbState.data = null;
    $("dashboard-snapshot").textContent = "Pendiente del cierre diario";
    $("db-table").replaceChildren();
    $("db-foot").textContent = "Aún no hay días procesados.";
    return;
  }
  const first = !dbState.data;
  dbState.data = data;
  const valid = (period) => period === "todos" || data.meses.includes(period);
  if (!valid(dbState.period)) dbState.period = data.mes_en_curso;
  if (!valid(dbState.anomPeriod)) dbState.anomPeriod = data.mes_en_curso;
  for (const [id, value] of [["db-period", dbState.period], ["db-anom-period", dbState.anomPeriod]]) {
    $(id).replaceChildren(...[...data.meses].reverse().map((month) => {
      const option = el("option", "", capitalize(dashMonthLabel(month, "long")) + (month === data.mes_en_curso ? " (en curso)" : ""));
      option.value = month;
      return option;
    }), Object.assign(el("option", "", `Todo el historial (desde el ${dbDay(data.desde)})`), {value: "todos"}));
    $(id).value = value;
  }
  if (first || window.location.hash === "#dashboard") { loadDashPeriod(); loadDashPeriod(dbState.anomPeriod); }
}

// Cada período se pide una vez y se renueva cada 5 min (el día en curso sigue sumando datos);
// mientras se renueva se sigue mostrando lo anterior.
// El período de la tabla y el de las anomalías se piden por separado (pueden ser distintos).
async function loadDashPeriod(period = dbState.period) {
  const cached = dbState.periods[period];
  if (period === dbState.period) renderDashTable();
  if (period === dbState.anomPeriod) renderDashAnomalies();
  if (dbState.requests[period] || (cached?.camiones && Date.now() - cached.loadedAt < DB_REFRESH_MS)) return;
  const range = dbRange(period);
  dbState.requests[period] = true;
  try {
    const result = await api(`/api/dashboard/camiones?desde=${range.desde}&hasta=${range.hasta}`, {timeoutMs: 30000});
    dbState.periods[period] = result.status === "ok" ? {...result, loadedAt: Date.now()} : {error: "aún no hay días procesados"};
  } catch (error) {
    if (!cached?.camiones) dbState.periods[period] = {error: error.message};
  } finally {
    delete dbState.requests[period];
  }
  if (dbState.period === period) renderDashTable();
  if (dbState.anomPeriod === period) renderDashAnomalies();
}

function renderDashAnomalies() {
  const info = dbState.data && dbState.periods[dbState.anomPeriod];
  $("db-anom-title").textContent = dbState.anomPeriod ? `Anomalías de ${dbPeriodLabel(dbState.anomPeriod)}` : "Anomalías";
  if (!info || !info.camiones) {
    for (const id of ["db-findings", "db-rank", "db-low"]) $(id).replaceChildren();
    $("db-findings").append(el("p", "fd-loading", info?.error ? "No se pudo cargar: " + info.error : "Cargando las anomalías del período…"));
    return;
  }
  renderDashHighlights(info);
}

function dbSilent(truck, info) {
  return truck.dias_sin_reportes === null || truck.dias_sin_reportes >= info.sin_reportes_dias;
}

// Todos los camiones del período con sus cifras calculadas (las anomalías usan estos, sin filtros).
function dbAll(info) {
  const monthly = new Map((dbState.data.camiones || []).map((truck) => [truck.patente, truck[dbState.monthMetric] || []]));
  return info.camiones.map((truck) => {
    const row = {...truck, por100: detainedPer100(truck), ultimo: truck.ultimo_dia || ""};
    const values = monthly.get(truck.patente) || [];
    dbState.data.meses.forEach((month, k) => { row["m:" + month] = values[k] || 0; });
    row.mtotal = sum(values);
    return row;
  });
}

// Camiones de la tabla: los filtros de nivel o sin reportes, días con datos y recorrido mínimo.
function dbFiltered(info) {
  return dbAll(info)
    .filter((truck) => dbState.filter === "todos" || (dbState.filter === "sin_reportes" ? dbSilent(truck, info) : truck.nivel === dbState.filter))
    .filter((truck) => truck.dias >= dbState.minDays && truck.km >= dbState.minKm);
}

function dbTrucks(info) {
  const query = dbState.query.trim().toLocaleLowerCase("es");
  const trucks = dbFiltered(info)
    .filter((truck) => !query || `${truck.patente} ${truck.nombre || ""}`.toLocaleLowerCase("es").includes(query));
  const columns = dbColumns();
  if (!columns.some((item) => item.key === dbState.sort)) { dbState.sort = dbState.view === "meses" ? "mtotal" : "reportes"; dbState.dir = -1; }
  const key = dbState.sort;
  const column = columns.find((item) => item.key === key);
  return trucks.sort((a, b) => {
    const x = a[key], y = b[key];
    const order = column.text ? String(x || "").localeCompare(String(y || ""), "es") : (x ?? -1) - (y ?? -1);
    return order * dbState.dir || a.patente.localeCompare(b.patente);
  });
}

function renderDashFilters(info) {
  // Cuántos camiones quedan en cada filtro con los mínimos de días y km elegidos.
  const base = info.camiones.filter((truck) => truck.dias >= dbState.minDays && truck.km >= dbState.minKm);
  const silent = base.filter((truck) => dbSilent(truck, info)).length;
  const chips = [["todos", "Todos", base.length, "Todos los camiones del catálogo"],
    ...Object.entries(DB_LEVELS).map(([level, note]) => [level, "Nivel " + level, base.filter((truck) => truck.nivel === level).length, note]),
    ["sin_reportes", "Sin reportes", silent, `Sin reportes desde hace ${info.sin_reportes_dias} días o más (o nunca)`]];
  $("db-filters").replaceChildren(...chips.map(([value, label, count, title]) => {
    const chip = el("button", "db-chip" + (value.length === 1 ? " db-level-" + value.toLowerCase() : value === "sin_reportes" ? " is-warning" : ""));
    chip.type = "button";
    chip.title = title;
    chip.classList.toggle("is-active", dbState.filter === value);
    chip.setAttribute("aria-pressed", String(dbState.filter === value));
    chip.append(el("span", "", label), el("b", "", format(count || 0)));
    chip.addEventListener("click", () => { dbState.filter = dbState.filter === value ? "todos" : value; dbState.open = null; renderDashTable(); });
    return chip;
  }));
}

function renderDashTable() {
  const info = dbState.data && dbState.periods[dbState.period];
  const table = $("db-table");
  if (!info || info.error) {
    $("dashboard-snapshot").textContent = info?.error ? "No se pudo cargar" : "Cargando…";
    $("dashboard-snapshot").className = "status " + (info?.error ? "warning" : "neutral");
    table.replaceChildren();
    $("db-foot").textContent = info?.error ? "No se pudo cargar el período: " + info.error : "Cargando los camiones del período…";
    return;
  }
  renderDashFilters(info);
  const range = dbRange();
  $("dashboard-snapshot").textContent = `${capitalize(dbPeriodLabel(dbState.period))} · actualizado ${new Intl.DateTimeFormat("es-CL",
    {timeStyle: "short", timeZone: HISTORY_ZONE}).format(new Date(info.loadedAt))}`;
  $("dashboard-snapshot").className = "status info";
  const failures = info.dias_falla || [];
  $("db-note").classList.toggle("is-warning", failures.length > 0);
  $("db-note").textContent = (info.incluye_hoy ? "Hoy sigue sumando datos. " : "") + (failures.length
    ? `Ojo: ${failures.length === 1 ? "el" : "los"} ${failures.map((day) => dbDay(day.fecha)).join(", ")} toda la flota envió menos de la mitad de lo normal (falla de la plataforma GPS), lo que baja las cifras de ese período.`
    : "");
  const trucks = dbTrucks(info);
  const columns = dbColumns();
  for (const button of document.querySelectorAll("[data-db-view]")) {
    const active = button.dataset.dbView === dbState.view;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  for (const button of document.querySelectorAll("[data-db-month-metric]")) {
    const active = button.dataset.dbMonthMetric === dbState.monthMetric;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  $("db-month-metric").hidden = dbState.view !== "meses";
  $("db-csv-note").textContent = dbState.view === "meses"
    ? `Una fila por camión y una columna por mes (${dbState.monthMetric === "km" ? "km" : "reportes"}), como la vista mes a mes`
    : "Una fila por camión: nivel, reportes, km, días con datos, paradas, tiempo detenido y último dato";
  const head = el("tr");
  for (const column of columns) {
    const th = el("th", column.key === "patente" ? "fd-col-truck" : "fd-col-num");
    if (column.month && dbState.period === column.month) th.classList.add("is-period");
    const button = el("button", "", column.label);
    button.type = "button";
    if (column.title) button.title = column.title;
    th.classList.toggle("is-current", dbState.sort === column.key);
    th.setAttribute("aria-sort", dbState.sort !== column.key ? "none" : dbState.dir > 0 ? "ascending" : "descending");
    button.addEventListener("click", () => {
      dbState.dir = dbState.sort === column.key ? -dbState.dir : (column.text ? 1 : -1);
      dbState.sort = column.key;
      renderDashTable();
    });
    th.append(button);
    head.append(th);
  }
  const body = el("tbody");
  for (const truck of trucks) {
    const row = el("tr", "fd-row");
    row.dataset.patente = truck.patente;
    row.tabIndex = 0;
    row.setAttribute("aria-expanded", String(dbState.open === truck.patente));
    const name = el("td", "fd-col-truck");
    name.append(el("span", "fd-plate", truck.patente), el("span", "fd-name", truck.nombre || "Sin nombre"));
    const level = el("td", "fd-col-num");
    level.append(el("b", "db-level db-level-" + truck.nivel.toLowerCase(), truck.nivel));
    level.title = DB_LEVELS[truck.nivel];
    row.append(name, level, ...columns.slice(2, -1).map((column) => {
      const cell = el("td", "fd-col-num" + (column.month && dbState.period === column.month ? " is-period" : ""), column.show(truck));
      if (column.month && !truck[column.key]) cell.classList.add("is-zero");
      return cell;
    }));
    const last = el("td", "fd-col-num");
    if (dbSilent(truck, info)) {
      last.append(el("b", "db-silent", truck.ultimo_dia ? `hace ${format(truck.dias_sin_reportes)} días` : "Nunca reportó"));
      if (truck.ultimo_dia) last.title = dayLabel(truck.ultimo_dia, "long");
    } else {
      last.textContent = truck.ultimo_dia === dbState.data.hoy ? "Hoy" : dbDay(truck.ultimo_dia);
    }
    row.append(last);
    const toggle = () => {
      dbState.open = dbState.open === truck.patente ? null : truck.patente;
      renderDashTable();
      if (dbState.open && !dbState.days[truck.patente]) loadDashTruck(truck.patente);
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });
    body.append(row);
    if (dbState.open === truck.patente) body.append(dashDetailRow(truck, range));
  }
  if (!trucks.length) {
    const row = el("tr");
    const cell = el("td", "empty", "Ningún camión cumple el filtro o la búsqueda.");
    cell.colSpan = dbColumns().length;
    row.append(cell);
    body.append(row);
  }
  const thead = el("thead");
  thead.append(head);
  table.replaceChildren(thead, body);
  dbFitDetail();
  const limits = [dbState.minDays ? `${format(dbState.minDays)} días con datos o más` : "",
    dbState.minKm ? `${format(dbState.minKm)} km o más` : ""].filter(Boolean);
  $("db-foot").textContent = `${format(trucks.length)} de ${format(info.camiones.length)} camiones · del ${dbDay(range.desde)} al ${dbDay(range.hasta)}` +
    (limits.length ? ` · solo con ${limits.join(" y ")}` : "") + ". " +
    "El nivel A–D compara los reportes de cada camión con el resto de la flota en el período; no indica una falla por sí solo.";
  if (dbState.query) {
    const clear = el("button", "db-more", " Ver la lista completa");
    clear.type = "button";
    clear.addEventListener("click", () => { dbState.query = ""; $("db-search").value = ""; renderDashTable(); });
    $("db-foot").append(clear);
  }
}

// El detalle abierto ocupa el ancho visible de la tabla (aunque la tabla tenga scroll lateral).
function dbFitDetail() {
  $("db-table").style.setProperty("--fd-wrap", $("db-table-wrap").clientWidth + "px");
}
window.addEventListener("resize", dbFitDetail);

// Abre un camión en la tabla (desde un destacado o una barra) y la lleva a la vista.
// La lista completa se mantiene: solo se abre ese camión y se lleva a la vista.
// Quita los filtros de la tabla: lo que viene de las anomalías (toda la flota) debe verse en ella.
function dbClearFilters() {
  Object.assign(dbState, {query: "", filter: "todos", minDays: 0, minKm: 0});
  if (dbState.period !== dbState.anomPeriod) {  // la tabla muestra el mismo período que las anomalías
    dbState.period = dbState.anomPeriod;
    $("db-period").value = dbState.period;
    loadDashPeriod();
  }
  $("db-search").value = "";
  $("db-min-days").value = "0";
  $("db-min-km").value = "0";
}

function dbOpenTruck(patente) {
  dbClearFilters();
  dbState.open = patente;
  renderDashTable();
  if (!dbState.days[patente]) loadDashTruck(patente);
  const row = $("db-table").querySelector(`tr[data-patente="${CSS.escape(patente)}"]`);
  (row || $("db-table-title")).scrollIntoView({behavior: "smooth", block: "start"});
}

function dbBars(container, items) {
  container.replaceChildren(...items.map((item) => {
    const row = el("button", "dashboard-bar-row db-bar");
    row.type = "button";
    const top = el("div");
    top.append(el("span", "", item.label), el("strong", "", item.show));
    const track = el("div", "dashboard-bar-track");
    const fill = el("i");
    fill.style.width = item.width + "%";
    fill.style.background = item.color;
    track.append(fill);
    row.append(top, track, el("small", "", item.detail));
    row.addEventListener("click", () => dbOpenTruck(item.patente));
    return row;
  }));
  if (!items.length) container.replaceChildren(el("span", "chart-empty", "Sin camiones con datos para comparar."));
}

// Anomalías del período en toda la flota (sin los filtros de la tabla): destacados y dos comparaciones.
function renderDashHighlights(info) {
  const trucks = dbAll(info).filter((truck) => truck.reportes > 0);
  const withKm = trucks.filter((truck) => truck.km >= DB_EFFICIENCY_KM && truck.por100 !== null);
  const top = (list, key) => [...list].sort((a, b) => b[key] - a[key])[0];
  // Los camiones del catálogo que nunca reportaron se cuentan aparte: no dicen cuándo se cortó la señal.
  const silentAll = dbAll(info).filter((truck) => dbSilent(truck, info));
  const never = silentAll.filter((truck) => !truck.ultimo_dia).length;
  const silent = silentAll.filter((truck) => truck.ultimo_dia).sort((a, b) => b.dias_sin_reportes - a.dias_sin_reportes)[0];
  const range = dbRange(dbState.anomPeriod);
  const periodDays = Math.round((dayDate(range.hasta) - dayDate(range.desde)) / 86400000) + 1;
  const fewest = dbAll(info).filter((truck) => truck.dias > 0)  // reportó en el mes (no cuenta los que aún no llegaban)
    .sort((a, b) => a.dias - b.dias || a.reportes - b.reportes)[0];
  const cards = [
    {truck: top(trucks, "paradas"), label: "Más paradas", value: (t) => `${format(t.paradas)} paradas`,
      note: (t) => `${formatMinutes(t.detenido)} detenido en total`, color: "#d97941"},
    {truck: top(trucks, "detenido"), label: "Más tiempo detenido", value: (t) => formatMinutes(t.detenido),
      note: (t) => `${t.paradas ? format(t.detenido / t.paradas, 0) : "—"} min por parada`, color: "#b84d3f"},
    {truck: top(trucks, "km"), label: "Más km", value: (t) => `${format(t.km, 1)} km`,
      note: (t) => `${format(t.dias)} días con datos`, color: "#287a57"},
    {truck: top(withKm, "por100"), label: "Más detención por distancia", value: (t) => `${format(t.por100, 0)} min / 100 km`,
      note: (t) => `${format(t.km, 1)} km recorridos`, color: "#7b5c91"},
    // En el mes en curso: quién lleva más días sin reportar hoy. En un mes pasado: quién tuvo
    // menos días con datos ese mes (entre los que reportaron al menos un día).
    dbState.anomPeriod === dbState.data.mes_en_curso
      ? {truck: silent, label: "Más días sin reportes", value: (t) => `${format(t.dias_sin_reportes)} días`,
        note: (t) => `último dato el ${dbDay(t.ultimo_dia)}` + (never ? ` · ${format(never)} del catálogo nunca reportaron` : ""), color: "#71817e"}
      : {truck: fewest, label: "Menos días con datos", value: (t) => `${format(t.dias)} de ${format(periodDays)} días`,
        note: (t) => `${format(t.reportes)} reportes en el período`, color: "#71817e"}
  ].filter((card) => card.truck);
  $("db-findings").replaceChildren(...cards.map((card) => {
    const box = el("button", "db-finding");
    box.type = "button";
    box.style.setProperty("--finding", card.color);
    box.append(el("span", "", card.label), el("strong", "", card.value(card.truck)),
      el("b", "", `${card.truck.patente} · ${card.truck.nombre || "Sin nombre"}`), el("small", "", card.note(card.truck)));
    box.addEventListener("click", () => dbOpenTruck(card.truck.patente));
    return box;
  }));
  const rank = DB_RANKS[dbState.rank];
  for (const button of document.querySelectorAll("[data-db-rank]")) {
    const active = button.dataset.dbRank === dbState.rank;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  $("db-rank-title").textContent = rank.title;
  $("db-rank-more").dataset.dbSort = dbState.rank;
  const pick = (list, value, order, limit) => {
    const sorted = [...list].sort((a, b) => order === "asc" ? value(a) - value(b) : value(b) - value(a));
    return limit ? sorted.slice(0, limit) : sorted;
  };
  const ranked = pick(trucks.filter((truck) => rank.value(truck) > 0), rank.value, dbState.rankOrder, dbState.rankLimit);
  const max = Math.max(1, ...ranked.map(rank.value));
  dbBars($("db-rank"), ranked.map((truck) => ({patente: truck.patente, label: `${truck.patente} · ${truck.nombre || "Sin nombre"}`,
    show: rank.show(truck), detail: rank.detail(truck), width: rank.value(truck) / max * 100, color: rank.color})));
  // Menos reportes: incluye a los que no reportaron nada en el período (si alguna vez reportaron);
  // la barra compara con la mediana de la flota (barra completa = lo normal).
  const reporting = dbAll(info).filter((truck) => truck.ultimo_dia);
  const sortedReports = reporting.map((truck) => truck.reportes).sort((a, b) => a - b);
  const median = sortedReports.length ? sortedReports[Math.floor(sortedReports.length / 2)] : 0;
  const low = pick(reporting, (truck) => truck.reportes, "asc", dbState.lowLimit);
  $("db-low-title").textContent = `Camiones que menos reportaron en ${dbPeriodLabel(dbState.anomPeriod)}`;
  $("db-low-note").textContent = `Comparados con la mediana de la flota en el período (${format(median)} reportes): la barra completa es lo normal. En rojo, bajo la mitad.`;
  dbBars($("db-low"), low.map((truck) => {
    const share = median ? truck.reportes / median * 100 : 0;
    return {patente: truck.patente, label: `${truck.patente} · ${truck.nombre || "Sin nombre"}`,
      show: truck.reportes ? `${format(truck.reportes)} reportes · ${format(share, 0)} %` : "Sin reportes",
      width: Math.min(100, share),
      detail: `${format(truck.dias)} días con datos · último dato ${truck.ultimo_dia === dbState.data.hoy ? "hoy" : "el " + dbDay(truck.ultimo_dia)} · nivel ${truck.nivel}`,
      color: share < 50 ? "#b84d3f" : "#d9a441"};
  }));
}

async function loadDashTruck(patente) {
  try {
    const data = await api(`/api/dashboard/camion?patente=${encodeURIComponent(patente)}`, {timeoutMs: 30000});
    dbState.days[patente] = data.dias || [];
  } catch (error) {
    dbState.days[patente] = {error: error.message};
  }
  if (dbState.open === patente) renderDashTable();
}

function dashDetailRow(truck, range) {
  const row = el("tr", "fd-detail");
  const cell = el("td");
  cell.colSpan = dbColumns().length;
  const days = dbState.days[truck.patente];
  if (!days) cell.append(el("p", "fd-loading", "Cargando su detalle…"));
  else if (days.error) cell.append(el("p", "fd-loading", "No se pudo cargar: " + days.error));
  else cell.append(dashCalendar(truck, days, range));
  row.append(cell);
  return row;
}

// Detalle de un camión: cifras del período, un calendario por mes (cada día con su número y,
// en un mes, su valor) y sus reportes y km mes a mes. Lo que se mide en el calendario se elige.
const DB_DAY_METRICS = {
  km: {label: "Km", index: 2, show: (v) => `${format(v, v < 100 ? 1 : 0)} km`, short: (v) => format(v, 0)},
  reportes: {label: "Reportes", index: 1, show: (v) => `${format(v)} reportes`, short: (v) => v >= 1000 ? format(v / 1000, 1) + " mil" : format(v)},
  paradas: {label: "Paradas", index: 3, show: (v) => `${format(v)} paradas`, short: (v) => format(v)},
  detenido: {label: "Tiempo detenido", index: 4, show: (v) => formatMinutes(v) + " detenido", short: (v) => v >= 60 ? format(v / 60, 0) + " h" : format(v) + " m"}
};
const DB_WEEKDAYS = ["L", "M", "M", "J", "V", "S", "D"];

function dashCalendar(truck, days, range) {
  const data = dbState.data;
  const metric = DB_DAY_METRICS[dbState.dayMetric];
  const byDay = new Map(days.map((day) => [day[0], day]));
  const value = (iso) => Number((byDay.get(iso) || [])[metric.index] || 0);
  const inPeriod = days.filter((day) => day[0] >= range.desde && day[0] <= range.hasta);
  const active = inPeriod.filter((day) => day[1] > 0);
  const best = active.reduce((top, day) => !top || day[2] > top[2] ? day : top, null);
  const periodDays = Math.round((dayDate(range.hasta) - dayDate(range.desde)) / 86400000) + 1;
  const months = dbState.period === "todos" ? data.meses : [dbState.period];
  const max = Math.max(1, ...days.filter((day) => day[0].slice(0, 7) >= months[0] && day[0].slice(0, 7) <= months.at(-1))
    .map((day) => Number(day[metric.index] || 0)));
  const detailed = months.length === 1;

  const box = el("div", "db-detail");
  const heading = el("div", "fd-detail-heading");
  heading.append(el("div", "", `${truck.patente} · ${truck.nombre || "Sin nombre"}`),
    el("span", "", capitalize(dbPeriodLabel(dbState.period))));
  const facts = el("div", "db-detail-facts");
  for (const [label, text, note] of [
    ["Días con datos", `${format(active.length)} de ${format(periodDays)}`, periodDays - active.length ? `${format(periodDays - active.length)} sin reportes` : "todos con reportes"],
    ["Promedio por día con datos", active.length ? `${format(truck.km / active.length, 1)} km` : "—", active.length ? `${format(truck.reportes / active.length, 0)} reportes` : ""],
    ["Día con más km", best ? `${format(best[2], 1)} km` : "—", best ? dbDay(best[0]) : ""],
    ["Por parada", truck.paradas ? `${format(truck.detenido / truck.paradas, 0)} min` : "—", `${format(truck.paradas)} paradas en el período`]
  ]) {
    const fact = el("div");
    fact.append(el("span", "", label), el("strong", "", text), el("small", "", note));
    facts.append(fact);
  }

  const toolbar = el("div", "db-cal-tools");
  const toggle = el("div", "db-toggle");
  for (const [key, item] of Object.entries(DB_DAY_METRICS)) {
    const button = el("button", key === dbState.dayMetric ? "is-active" : "", item.label);
    button.type = "button";
    button.addEventListener("click", (event) => { event.stopPropagation(); dbState.dayMetric = key; renderDashTable(); });
    toggle.append(button);
  }
  const legend = el("div", "fd-cal-legend");
  legend.append(el("span", "", "Sin reportes"), el("i", "is-zero"), el("span", "", "· menos"),
    ...[0.25, 0.5, 0.75, 1].map((heat) => { const swatch = el("i"); swatch.style.setProperty("--heat", String(heat)); return swatch; }),
    el("span", "", `más (hasta ${metric.show(max)})`));
  toolbar.append(toggle, legend);

  const calendar = el("div", "db-cal" + (detailed ? " is-detailed" : ""));
  for (const month of months) {
    const [year, number] = month.split("-").map(Number);
    const block = el("div", "db-cal-month");
    const total = sum(days.filter((day) => day[0].startsWith(month)).map((day) => Number(day[metric.index] || 0)));
    const title = el("div", "db-cal-title");
    title.append(el("strong", "", capitalize(dashMonthLabel(month, "long"))), el("span", "", metric.show(total)));
    const grid = el("div", "db-cal-grid");
    grid.append(...DB_WEEKDAYS.map((name) => el("b", "", name)));
    const first = new Date(Date.UTC(year, number - 1, 1));
    for (let k = 0; k < (first.getUTCDay() + 6) % 7; k++) grid.append(el("i"));
    const length = new Date(Date.UTC(year, number, 0)).getUTCDate();
    for (let d = 1; d <= length; d++) {
      const iso = `${month}-${String(d).padStart(2, "0")}`;
      const entry = byDay.get(iso);
      const amount = value(iso);
      const cell = el(iso < data.desde || iso > data.hoy ? "span" : "a", "db-cal-day");
      cell.append(el("small", "", String(d)));
      if (cell.tagName === "SPAN") {
        cell.classList.add("is-out");
      } else {
        const reports = Number((entry || [])[1] || 0);
        cell.classList.toggle("is-zero", !reports);
        cell.classList.toggle("is-today", iso === data.hoy);
        const heat = amount ? 0.18 + 0.82 * Math.sqrt(amount / max) : 0;
        cell.style.setProperty("--heat", heat.toFixed(3));
        cell.classList.toggle("is-dark", heat > 0.4);
        if (detailed) cell.append(el("b", "", reports ? metric.short(amount) : "—"));
        cell.href = `#camiones/${encodeURIComponent(truck.patente)}/${iso}`;
        cell.title = `${dayLabel(iso, "long")}: ` + (reports
          ? `${format(reports)} reportes · ${format(entry[2], 1)} km · ${format(entry[3] || 0)} paradas · ${formatMinutes(entry[4] || 0)} detenido. Toca para ver su recorrido.`
          : "sin reportes");
        cell.setAttribute("aria-label", cell.title);
        cell.addEventListener("click", (event) => event.stopPropagation());
      }
      grid.append(cell);
    }
    block.append(title, grid);
    calendar.append(block);
  }

  // Mes a mes del camión (todo el historial), con lo mismo que se mide en el calendario.
  const perMonth = data.meses.map((month) => {
    const list = days.filter((day) => day[0].startsWith(month));
    return {month, value: sum(list.map((day) => Number(day[metric.index] || 0))), active: list.filter((day) => day[1] > 0).length};
  });
  const maxMonth = Math.max(1, ...perMonth.map((item) => item.value));
  const monthsChart = el("div", "db-month-bars");
  for (const item of perMonth) {
    const bar = el("div", "db-month-bar" + (item.month === dbState.period ? " is-period" : ""));
    const track = el("span");
    const fill = el("i");
    fill.style.height = (item.value / maxMonth * 100).toFixed(1) + "%";
    track.append(fill);
    bar.title = `${capitalize(dashMonthLabel(item.month, "long"))}: ${metric.show(item.value)} · ${format(item.active)} días con datos`;
    bar.append(el("b", "", item.value ? metric.short(item.value) : "0"), track,
      el("small", "", capitalize(dashMonthLabel(item.month))), el("em", "", `${format(item.active)} días`));
    monthsChart.append(bar);
  }
  const monthsBox = el("div", "db-detail-months");
  monthsBox.append(el("p", "db-detail-label", `${metric.label} por mes (todo el historial)`), monthsChart);
  if (truck.ultimo_dia) {
    const link = el("a", "fd-open", "Ver su último día con datos →");
    link.href = `#camiones/${encodeURIComponent(truck.patente)}/${truck.ultimo_dia}`;
    monthsBox.append(link);
  }
  const body = el("div", "db-detail-body");
  const calendarBox = el("div");
  calendarBox.append(el("p", "db-detail-label", "Día por día · toca un día para ver su recorrido"), toolbar, calendar);
  body.append(calendarBox, monthsBox);
  box.append(heading, facts, body);
  return box;
}

const csvNumber = (value, decimals = 1) => value === null || value === undefined ? "" : format(value, decimals).replace(/\./g, "");

// Resumen: la tabla tal como se ve (filtro, búsqueda y orden), una fila por camión.
function downloadDashCsv() {
  const info = dbState.periods[dbState.period];
  if (!info || !info.camiones) return;
  const range = dbRange();
  if (dbState.view === "meses") {
    const unit = dbState.monthMetric === "km" ? "Km" : "Reportes GPS";
    const months = dbState.data.meses;
    const lines = [["Patente", "Camión", `Nivel de reporte (${dbPeriodLabel(dbState.period)})`,
      ...months.map((month) => `${unit} ${dashMonthLabel(month, "long")}`), `${unit} total`, "Último día con datos"].map(csvValue).join(";")];
    for (const truck of dbTrucks(info)) {
      lines.push([truck.patente, truck.nombre, truck.nivel, ...months.map((month) => csvNumber(truck["m:" + month], dbState.monthMetric === "km" ? 1 : 0)),
        csvNumber(truck.mtotal, dbState.monthMetric === "km" ? 1 : 0), truck.ultimo_dia || ""].map(csvValue).join(";"));
    }
    saveCsv(lines, `artisa-camiones-${dbState.monthMetric}-por-mes.csv`);
    return;
  }
  const lines = [["Patente", "Camión", "Nivel de reporte", "Reportes GPS", "Km", "Días con datos", "Paradas",
    "Minutos detenido", "Minutos detenido cada 100 km", "Último día con datos"].map(csvValue).join(";")];
  for (const truck of dbTrucks(info)) {
    lines.push([truck.patente, truck.nombre, truck.nivel, truck.reportes, csvNumber(truck.km), truck.dias, truck.paradas,
      truck.detenido, csvNumber(truck.por100, 0), truck.ultimo_dia || ""].map(csvValue).join(";"));
  }
  saveCsv(lines, `artisa-camiones-${range.desde}-a-${range.hasta}.csv`);
}

// Día a día: una fila por camión y día del período, de los camiones visibles en la tabla.
async function downloadDashDays() {
  const info = dbState.periods[dbState.period];
  if (!info || !info.camiones) return;
  const button = $("db-csv-days");
  const range = dbRange();
  const visible = new Set(dbTrucks(info).map((truck) => truck.patente));
  const months = dbState.period === "todos" ? dbState.data.meses : [dbState.period];
  button.disabled = true;
  button.querySelector("strong").textContent = "Preparando el archivo…";
  try {
    const lines = [["Fecha", "Estado del día", "Patente", "Camión", "Reportes GPS", "Primer reporte (Chile)",
      "Último reporte (Chile)", "Km", "Paradas", "Minutos detenido"].map(csvValue).join(";")];
    for (const month of months) {
      const data = await api(`/api/reportes/mes-completo?mes=${encodeURIComponent(month)}`, {timeoutMs: 60000});
      for (const row of data.filas || []) {
        if (!visible.has(row.asset_id) || row.fecha < range.desde || row.fecha > range.hasta) continue;
        const [stateLabel] = HISTORY_STATES[row.estado] || [row.estado || ""];
        lines.push([row.fecha, stateLabel, row.asset_id, row.asset_name, row.reportes,
          csvLocalDateTime(row.primer_reporte, data.zona), csvLocalDateTime(row.ultimo_reporte, data.zona),
          csvNumber(row.km_observados), row.paradas, Math.round(row.minutos_detenido || 0)].map(csvValue).join(";"));
      }
    }
    saveCsv(lines, `artisa-camiones-dia-a-dia-${range.desde}-a-${range.hasta}.csv`);
  } catch (error) {
    $("db-note").textContent = "No se pudo preparar la descarga: " + error.message;
  } finally {
    button.disabled = false;
    button.querySelector("strong").textContent = "↓ Detalle día por día";
  }
}

$("db-period").addEventListener("change", (event) => { dbState.period = event.target.value; dbState.open = null; loadDashPeriod(); });
$("db-anom-period").addEventListener("change", (event) => { dbState.anomPeriod = event.target.value; loadDashPeriod(dbState.anomPeriod); });
$("db-search").addEventListener("input", (event) => { dbState.query = event.target.value; renderDashTable(); });
$("db-csv").addEventListener("click", downloadDashCsv);
for (const [id, key, number] of [["db-min-days", "minDays", true], ["db-min-km", "minKm", true],
  ["db-rank-order", "rankOrder", false], ["db-rank-limit", "rankLimit", true],
  ["db-low-limit", "lowLimit", true]]) {
  $(id).addEventListener("change", (event) => {
    dbState[key] = number ? Number(event.target.value) : event.target.value;
    renderDashTable();
  });
}
for (const button of document.querySelectorAll("[data-db-view]")) {
  button.addEventListener("click", () => { dbState.view = button.dataset.dbView; renderDashTable(); });
}
for (const button of document.querySelectorAll("[data-db-month-metric]")) {
  button.addEventListener("click", () => { dbState.monthMetric = button.dataset.dbMonthMetric; renderDashTable(); });
}
for (const button of document.querySelectorAll("[data-db-rank]")) {
  button.addEventListener("click", () => { dbState.rank = button.dataset.dbRank; renderDashTable(); });
}
for (const button of document.querySelectorAll("[data-db-sort]")) {
  button.addEventListener("click", () => {
    dbState.view = "periodo";
    dbState.sort = button.dataset.dbSort;
    dbState.dir = Number(button.dataset.dbDir || -1);
    dbClearFilters();
    renderDashTable();
    $("db-table-title").scrollIntoView({behavior: "smooth", block: "start"});
  });
}
$("db-csv-days").addEventListener("click", downloadDashDays);

function formatAge(seconds) {
  const value = Math.max(0, Math.round(seconds));
  if (value < 90) return "hace menos de 2 min";
  if (value < 5400) return `hace ${format(Math.round(value / 60))} min`;
  if (value < 172800) return `hace ${format(Math.round(value / 3600))} h`;
  return `hace ${format(Math.round(value / 86400))} días`;
}

function connectionMessage(connection) {
  const since = connection.desconectado_desde ? ` desde ${formatLocal(connection.desconectado_desde)} (hora de Chile)` : "";
  if (connection.conexion === "reconectando") {
    return `Sin conexión con la API de la empresa${since}. El sistema reintenta solo; cuando vuelva la red recuperará los reportes del período desconectado. Se muestran los últimos datos recibidos.`;
  }
  if (connection.conexion === "error_configuracion") {
    return `La API de la empresa rechazó la consulta${since}. Revisa COMPANY_API_URL y las credenciales en .env. Se muestran los últimos datos recibidos.`;
  }
  if (connection.conexion === "servicio_detenido") {
    return "El servicio de telemetría no informa actividad reciente. Revisa el contenedor artisa-telemetria. Se muestran los últimos datos recibidos.";
  }
  if (connection.conexion === "sincronizando") {
    return "Conectado. Se están recuperando los reportes acumulados desde la última sincronización; los días afectados se cierran cuando termine.";
  }
  if (connection.advertencia) return connection.advertencia;
  return "";
}

function setTelemetryBanner(message, tone = "warning") {
  const banner = $("telemetry-connection");
  banner.hidden = !message;
  banner.textContent = message || "";
  banner.className = "telemetry-connection " + tone;
}

// Bucle propio de Reportes: más frecuente que el refresco general, nunca se
// superpone, conserva los últimos datos si falla y reintenta al volver la red.
const TELEMETRY_REFRESH_MS = 60000;
let telemetryRefreshing = false;
let telemetryLastOk = null;

async function refreshTelemetry() {
  if (!token() || document.hidden || telemetryRefreshing) return;
  const version = sessionVersion;
  telemetryRefreshing = true;
  try {
    const [telemetrySummary, telemetryDataset] = await Promise.all([
      api("/api/telemetria/resumen"),
      api("/api/telemetria?limite=30")
    ]);
    if (version !== sessionVersion || !token()) return;
    renderTelemetry(telemetrySummary, telemetryDataset, {});
    telemetryLastOk = new Date();
  } catch (error) {
    if (version !== sessionVersion || !token()) return;
    if (!$("telemetry-content").hidden) {
      const last = telemetryLastOk
        ? ` Última actualización correcta: ${telemetryLastOk.toLocaleTimeString("es-CL")}.`
        : "";
      setTelemetryBanner(
        (navigator.onLine === false
          ? "Este equipo está sin conexión a la red."
          : "No se pudo contactar al servidor de Artisa.") +
        " Se reintentará automáticamente y se muestran los últimos datos disponibles." + last
      );
    }
  } finally {
    if (version === sessionVersion) telemetryRefreshing = false;
  }
}

function renderTelemetry(summary, dataset, state) {
  const pending = dataset.status === "pending" && dataset.source === "company_api";
  $("telemetry-pending").hidden = !pending;
  $("telemetry-content").hidden = pending;
  const tabNote = document.querySelector('[data-dataset-tab="real"] small');
  if (pending) {
    if (tabNote) tabNote.textContent = "Pendiente de API empresa";
    setTelemetryBanner("");
    $("telemetry-status").textContent = "Pendiente de API empresa";
    $("telemetry-status").className = "status neutral";
    $("telemetry-rows").replaceChildren();
    $("telemetry-count").textContent = "—";
    $("vehicle-count").textContent = "—";
    $("telemetry-range").textContent = "";
    $("telemetry-quality").textContent = "";
    return;
  }
  const reports = dataset.reportes || [];
  const connection = dataset.conexion || summary.conexion || {};
  const recentSeconds = Number(connection.reporte_reciente_segundos || 900);
  $("telemetry-count").textContent = format(summary.reportes || 0);
  $("vehicle-count").textContent = summary.vehiculos === null || summary.vehiculos === undefined
    ? "Sin dato" : format(summary.vehiculos);
  $("vehicle-count-note").textContent = `camiones con reportes en las últimas ${format(summary.vehiculos_ventana_horas || 24)} h`;
  $("telemetry-range").textContent = connection.ultimo_dato
    ? `Último reporte ${formatLocal(connection.ultimo_dato)} · contacto con la API ${connection.ultimo_exito ? formatLocal(connection.ultimo_exito) : "sin contacto aún"}`
    : "La API está configurada, pero todavía no entrega reportes.";
  $("fleet-tab-reportes-note").textContent = connection.ultimo_dato
    ? "Último reporte " + formatAge((Date.now() - new Date(connection.ultimo_dato)) / 1000) : "Lo último que llegó de la API";
  $("telemetry-quality").textContent =
    `${format(connection.guardados || 0)} guardados · ${format(connection.rechazados || 0)} rechazados de ${format(connection.leidos || 0)} leídos`;

  const states = {
    en_vivo: ["En vivo", "status"],
    sin_reportes_recientes: ["Conectado · sin reportes recientes", "status neutral"],
    sincronizando: ["Recuperando reportes atrasados…", "status info"],
    reconectando: ["Reconectando con la API…", "status warning"],
    error_configuracion: ["Revisar configuración de la API", "status warning"],
    servicio_detenido: ["Servicio de telemetría detenido", "status warning"]
  };
  const [label, className] = states[connection.conexion] || ["Estado desconocido", "status neutral"];
  $("telemetry-status").textContent = label;
  $("telemetry-status").className = className;
  if (tabNote) tabNote.textContent = "API empresa · " + label;
  const infoTone = connection.conexion === "sincronizando" || (connection.advertencia && connection.conexion === "en_vivo");
  setTelemetryBanner(connectionMessage(connection), infoTone ? "info" : "warning");

  const body = $("telemetry-rows");
  body.replaceChildren();
  for (const report of reports) {
    const tr = document.createElement("tr");
    // Cada reporte lleva a la ficha del camión ese día, marcando dónde estaba en ese momento.
    if (report.asset_id && report.report_date) {
      tr.className = "report-row";
      tr.tabIndex = 0;
      tr.title = "Ver el recorrido de este camión y dónde estaba en este reporte";
      const open = () => openTruckAtReport(report);
      tr.addEventListener("click", open);
      tr.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
    }
    const vehicle = document.createElement("td");
    vehicle.textContent = report.asset_name || "Vehículo sin nombre";
    const assetId = document.createElement("small");
    const driver = [report.driver_name, report.driver_last_name].filter(hasValue).join(" ");
    assetId.textContent = `Patente ${report.asset_id || "—"} · ${driver ? "Conductor: " + driver : "Conductor no informado"}`;
    vehicle.append(assetId);
    tr.append(vehicle);

    const date = document.createElement("td");
    date.textContent = formatLocal(report.report_date);
    const age = document.createElement("small");
    const ageSeconds = Number(report.edad_segundos);
    if (Number.isFinite(ageSeconds)) {
      age.textContent = ageSeconds <= recentSeconds
        ? "Posición reciente · " + formatAge(ageSeconds)
        : "Último dato conocido · " + formatAge(ageSeconds);
      age.className = ageSeconds <= recentSeconds ? "age-recent" : "age-old";
      date.append(age);
    }
    tr.append(date);

    const event = document.createElement("td");
    event.textContent = report.event_name || "Sin evento";
    tr.append(event);

    const coordinates = document.createElement("td");
    coordinates.textContent = report.latitude === null || report.longitude === null
      ? "Sin coordenadas"
      : `${Number(report.latitude).toFixed(5)}, ${Number(report.longitude).toFixed(5)}`;
    tr.append(coordinates);

    const ignition = document.createElement("td");
    ignition.className = "col-ignition";
    ignition.textContent = hasValue(report.ignition) ? (Number(report.ignition) === 1 ? "Sí" : "No") : "";
    tr.append(ignition);
    body.append(tr);
  }
  // Si la fuente no informa encendido, la columna se oculta en vez de mostrar "Sin dato".
  body.closest("table").classList.toggle("no-ignition", !reports.some((report) => hasValue(report.ignition)));
  if (!reports.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty";
    cell.textContent = "No hay telemetría disponible para mostrar.";
    row.append(cell);
    body.append(row);
  }
}

function updateOverviewTexts(state) {
  const components = state.componentes || {};
  const company = components.telemetria_api === "ok";
  const machine = components.machine === "ok";
  $("overview-summary").textContent = [
    company ? "Telemetría en vivo desde la API de la empresa" : "Telemetría pendiente de la API de la empresa",
    machine ? "rutas aprendidas por Machine" : "Machine preparando el primer modelo de rutas",
    "cierre diario a las 00:00 (hora de Chile)"
  ].join(" · ") + ".";
  $("origin-source").textContent = state.etl_historico === false ? "API de la empresa" : "API de la empresa + histórico MongoDB";
}

function renderState(state) {
  const components = state.componentes || {};
  // Las 5 etapas del diagrama. Una etapa "pending" (p. ej. Machine sin su primera
  // clasificación) no está operativa, pero tampoco es una falla.
  const services = ["etl", "mariadb", "machine", "endpoints", "front"];
  const nodes = [...document.querySelectorAll("[data-service]")];
  const count = services.filter((name) => components[name] === "ok").length;
  const failing = services.some((name) => !["ok", "pending"].includes(components[name]));
  $("system-status").textContent = format(count) + " de " + format(services.length) + " servicios operativos";
  $("system-status").className = "status " + (failing ? "warning" : count === services.length ? "" : "neutral");
  updateOverviewTexts(state);
  for (const node of nodes) {
    const okay = components[node.dataset.service] === "ok";
    const label = node.querySelector(".node-status");
    const pending = components[node.dataset.service] === "pending";
    const pendingText = node.dataset.service === "machine" ? "Esperando el primer modelo de rutas" : "Pendiente de configuración";
    label.textContent = okay ? "En funcionamiento" : pending ? pendingText : "Sin respuesta reciente";
    label.className = "node-status " + (okay ? "ok" : pending ? "neutral" : "bad");
  }
  const etl = state.etl;
  const company = state.telemetria_api;
  if (company && company.ultimo_exito) {
    $("last-sync").textContent = "Último dato de la API: " + formatLocal(company.ultimo_exito) + " (hora de Chile)";
  } else if (etl) {
    $("last-sync").textContent = etl.ultima_carga
      ? "Última carga: " + formatUtc(etl.ultima_carga)
      : "ETL aún sin primera carga";
  }
}

async function refresh() {
  if (!token()) return;
  if (refreshing) return;
  const version = sessionVersion;
  refreshing = true; $("refresh").disabled = true;
  // Cada sección se actualiza por separado: si una consulta falla o tarda, las demás
  // siguen funcionando y esa sección conserva lo último que mostró.
  const [stateResult, summaryResult, datasetResult, dashboardResult] = await Promise.allSettled([
    api("/api/estado"),
    api("/api/telemetria/resumen"),
    api("/api/telemetria?limite=30"),
    api("/api/dashboard", {timeoutMs: 60000})
  ]);
  try {
    if (version !== sessionVersion || !token()) return;
    const problems = [];
    const state = stateResult.status === "fulfilled" ? stateResult.value : null;
    if (state) renderState(state); else problems.push("estado del sistema");
    if (summaryResult.status === "fulfilled" && datasetResult.status === "fulfilled") {
      renderTelemetry(summaryResult.value, datasetResult.value, state || {});
      } else problems.push("reportes");
    if (dashboardResult.status === "fulfilled") renderDashboard(dashboardResult.value);
    else problems.push("dashboard");

    if (!state) {
      $("system-status").textContent = "Sin conexión con el sistema";
      $("system-status").className = "status warning";
      for (const label of document.querySelectorAll(".node-status")) {
        label.textContent = "Estado sin verificar"; label.className = "node-status bad";
      }
    }
    if (problems.length) {
      const reason = [stateResult, summaryResult, datasetResult, dashboardResult]
        .find((result) => result.status === "rejected")?.reason?.message || "";
      $("notice").hidden = false;
      $("notice").textContent = `No se pudo actualizar: ${problems.join(", ")}. Se muestran los últimos datos disponibles y se reintentará automáticamente. ${reason}`;
    } else {
      $("notice").hidden = state.status === "ok";
      $("notice").textContent = "Algún servicio está preparando datos: se muestra la última carga disponible.";
    }
  } finally {
    if (version === sessionVersion) { refreshing = false; $("refresh").disabled = false; }
  }
}

async function refreshMapVehicles() {
  if (!token() || document.hidden || liveVehiclesRefreshing || !vg.data) return;
  const version = sessionVersion;
  liveVehiclesRefreshing = true;
  try {
    const payload = await api("/api/vehiculos/posiciones", {timeoutMs: 20000});
    if (version !== sessionVersion || !token() || !vg.data) return;
    vgMergePositions(payload.vehicles || []);
  } catch {
    // Se conservan las últimas posiciones y se reintenta al minuto siguiente.
  } finally {
    if (version === sessionVersion) liveVehiclesRefreshing = false;
  }
}

// --- Historial diario (00:00 a 00:00 hora de Chile) ---
var historyState = {detail: null};
const isoDayIn = (date, zone = HISTORY_ZONE) =>
  new Intl.DateTimeFormat("en-CA", {timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit"}).format(date);
const localToday = () => isoDayIn(new Date());
const shiftDay = (iso, days) => {
  const value = new Date(iso + "T12:00:00Z");
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
};
const formatLocalTime = (value, zone = HISTORY_ZONE) => value ? new Intl.DateTimeFormat("es-CL", {
  timeStyle: "medium", timeZone: zone
}).format(new Date(value)) : "—";
const HISTORY_STATES = {
  en_curso: ["Hoy · en curso hasta las 00:00", "status info"],
  esperando_datos: ["Cerrando · esperando datos de la API", "status warning"],
  cerrado: ["Día cerrado", "status"]
};

const hasValue = (value) => value !== null && value !== undefined && value !== "";

function formatMinutes(value) {
  const minutes = Math.round(Number(value || 0));
  if (!minutes) return "—";
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours} h ${String(minutes % 60).padStart(2, "0")} min` : `${minutes} min`;
}

function emptyRow(body, columns, message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = columns; cell.className = "empty"; cell.textContent = message;
  row.append(cell); body.append(row);
}

function cell(text, small) {
  const td = document.createElement("td");
  td.textContent = text;
  if (small) { const note = document.createElement("small"); note.textContent = small; td.append(note); }
  return td;
}

async function loadTruckDetail(assetId, assetName, day, after = null) {
  const detail = after ? historyState.detail : {assetId, assetName, day, next: null, hasIgnition: false};
  historyState.detail = detail;
  $("history-detail-title").textContent = `${assetName || assetId} · ${dayLabel(day, "long")} · hora de Chile`;
  const body = $("history-detail-rows");
  if (!after) { body.replaceChildren(); emptyRow(body, 4, "Cargando reportes…"); }
  $("history-detail-more").hidden = true;
  try {
    const query = `/api/reportes/camion?asset_id=${encodeURIComponent(assetId)}&fecha=${day}&limite=500` +
      (after ? `&despues=${encodeURIComponent(after)}` : "");
    const data = await api(query);
    if (historyState.detail !== detail) return;
    if (!after) body.replaceChildren();
    for (const report of data.reportes || []) {
      const tr = document.createElement("tr");
      tr.append(
        cell(formatLocalTime(report.report_date, data.zona)),
        cell(report.event_name || "Sin evento"),
        cell(`${Number(report.latitude).toFixed(5)}, ${Number(report.longitude).toFixed(5)}`)
      );
      const ignition = cell(hasValue(report.ignition) ? (Number(report.ignition) === 1 ? "Sí" : "No") : "");
      ignition.className = "col-ignition";
      tr.append(ignition);
      if (hasValue(report.ignition)) detail.hasIgnition = true;
      body.append(tr);
    }
    body.closest("table").classList.toggle("no-ignition", !detail.hasIgnition);
    if (!body.children.length) emptyRow(body, 4, "Sin reportes para este camión en el día.");
    detail.next = data.siguiente;
    $("history-detail-more").hidden = !data.siguiente;
  } catch (error) {
    if (historyState.detail !== detail) return;
    if (!after) body.replaceChildren();
    emptyRow(body, 4, "No se pudieron cargar los reportes: " + error.message);
  }
}

function csvValue(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}

function csvLocalDateTime(value, zone) {
  if (!value) return "";
  return new Intl.DateTimeFormat("es-CL", {
    dateStyle: "short", timeStyle: "medium", timeZone: zone || HISTORY_ZONE
  }).format(new Date(value));
}

// CSV para Excel en español: separador ";" y BOM para que respete las tildes.
function saveCsv(lines, filename) {
  const blob = new Blob(["\uFEFF", lines.join("\r\n")], {type: "text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("history-detail-more").addEventListener("click", () => {
  const detail = historyState.detail;
  if (detail && detail.next) loadTruckDetail(detail.assetId, detail.assetName, detail.day, detail.next);
});

$("refresh").addEventListener("click", refresh);

// «Actualizar datos»: corta las consultas que quedaron pegadas, libera cada sección y vuelve
// a pedir todo lo que se está mirando, sin recargar la página ni cerrar la sesión.
async function refreshAll() {
  const button = $("refresh-all");
  if (button.disabled) return;
  button.disabled = true;
  button.classList.add("is-busy");
  for (const controller of pendingRequests) controller.abort();
  pendingRequests.clear();
  refreshing = false;
  telemetryRefreshing = false;
  liveVehiclesRefreshing = false;
  vg.loading = false;
  try {
    const onTruck = Boolean(truckHash()) && truckView.id && !$("truck-view").hidden;
    await Promise.allSettled([
      refresh(), refreshTelemetry(), loadFleet(), loadVigilance(false),
      onTruck ? loadTruckDay(truckView.id, truckView.day, true) : null,
      truckView.boardDay ? setBoardDay(truckView.boardDay) : null,
    ]);
    button.title = "Actualizado a las " + new Intl.DateTimeFormat("es-CL", {timeStyle: "short", timeZone: HISTORY_ZONE}).format(new Date());
  } finally {
    button.disabled = false;
    button.classList.remove("is-busy");
  }
}
$("refresh-all").addEventListener("click", refreshAll);
for (const button of document.querySelectorAll("[data-zone-filter]")) {
  button.addEventListener("click", () => {
    vg.zone = button.dataset.zoneFilter;
    vgApplyLayers();
    if (usageLayer) vgDrawUsage();
  });
}
for (const input of document.querySelectorAll("[data-map-layer]")) {
  input.addEventListener("change", () => { vg.layers[input.dataset.mapLayer] = input.checked; vgApplyLayers(); });
}
for (const button of document.querySelectorAll("[data-monitor-tab]")) {
  button.addEventListener("click", () => setMonitorTab(button.dataset.monitorTab));
}
for (const button of document.querySelectorAll("[data-alert-filter]")) {
  button.addEventListener("click", () => { vg.alertFilter = button.dataset.alertFilter; vgRenderAlertList(); });
}
for (const button of document.querySelectorAll("[data-history-range]")) {
  button.addEventListener("click", () => loadAlertHistory(button.dataset.historyRange));
}
$("vg-history-day").addEventListener("change", () => loadAlertHistory(vg.history.kind || "dia"));
$("vg-history-csv").addEventListener("click", downloadAlertHistory);
$("monitor-search").addEventListener("input", vgRenderLists);
$("reset-map-view").addEventListener("click", () => vgReset(true));
$("vg-alerts-card").addEventListener("click", () => {
  vg.alertFilter = "activa";
  setMonitorTab("alerts");
  const first = [...vg.alerts.entries()].find(([, alert]) => alert.estado === "activa");
  if (first) vgSelect({kind: "alert", id: first[0]});
});
// Retirar el almacenamiento de la implementación anterior sin persistir el usuario.
try {
  sessionStorage.removeItem("artisa_token");
  sessionStorage.removeItem("artisa_user");
} catch { /* Puede estar deshabilitado. */ }
window.addEventListener("hashchange", restoreSession);
window.addEventListener("popstate", restoreSession);
window.addEventListener("pageshow", restoreSession);
window.addEventListener("pagehide", () => { appShell.hidden = true; landing.hidden = true; });
window.addEventListener("storage", (event) => {
  if (event.key === TOKEN_KEY || event.key === null) restoreSession();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) restoreSession();
});
// --- Fechas de un día (YYYY-MM-DD) como etiqueta ---
function dayDate(iso) { return new Date(iso + "T12:00:00Z"); }

function dayLabel(iso, style = "short") {
  const options = style === "short" ? {weekday: "short", day: "numeric"}
    : style === "weekday" ? {weekday: "short"}
      : style === "month" ? {month: "short"}
        : {weekday: "long", day: "numeric", month: "long"};
  return new Intl.DateTimeFormat("es-CL", {...options, timeZone: "UTC"}).format(dayDate(iso));
}

const TRUCK_STATES = {
  en_ruta: "En ruta", detenido: "Detenido", sin_senal: "Sin señal reciente", sin_datos: "Sin datos"
};
const TRUCK_REFRESH_MS = 60000;
const TRUCK_COLORS = {base: "#9db5aa", halo: "#ffffff", stop: "#e07a1f", start: "#1f6b4f", end: "#172d32", gap: "#71817e"};
// Un color por viaje (se repiten después del séptimo); el naranja queda para las paradas.
const TRIP_COLORS = ["#1f6b4f", "#2563eb", "#9333ea", "#0e7490", "#db2777", "#4d7c0f", "#475569"];
const tripColor = (number) => TRIP_COLORS[(number - 1) % TRIP_COLORS.length];
const truckView = {fleet: null, filter: "todos", query: "", sort: "nombre", id: null, day: null, detail: null,
  map: null, layer: null, trips: new Map(), base: [], focus: null, play: null, request: 0,
  boardDay: null, dayRows: null, dayState: null, dayLoading: false, tab: "flota", pin: null};

function truckHash() {
  const match = window.location.hash.match(/^#camiones(?:\/([A-Za-z0-9_-]{1,16}))?(?:\/(\d{4}-\d{2}-\d{2}))?$/);
  return match ? {id: match[1] || null, day: match[2] || null} : null;
}

function openTruckAtReport(report) {
  const moment = new Date(report.report_date);
  truckView.pin = {
    id: report.asset_id, day: isoDayIn(moment),
    time: new Intl.DateTimeFormat("en-GB", {hour: "2-digit", minute: "2-digit", hour12: false, timeZone: HISTORY_ZONE}).format(moment),
    label: formatLocal(report.report_date), event: report.event_name || "Reporte",
    point: report.latitude === null || report.longitude === null ? null : [Number(report.latitude), Number(report.longitude)]
  };
  truckView.detail = null;  // aunque sea el mismo camión y día, se vuelve a dibujar con la marca
  openTruck(report.asset_id, truckView.pin.day);
}

function openTruck(id, day) {
  window.location.hash = "#camiones/" + encodeURIComponent(id) + (day ? "/" + day : "");
}

// Hora local "YYYY-MM-DD HH:MM:SS" (la API ya la entrega en hora de Chile).
const clock = (value) => value ? value.slice(11, 16) : "—";
const minuteOfDay = (value) => value ? Number(value.slice(11, 13)) * 60 + Number(value.slice(14, 16)) : 0;

function truckAge(minutes) {
  return minutes === null || minutes === undefined ? "Sin reportes" : formatAge(minutes * 60);
}

function syncTruckView() {
  const route = truckHash();
  if (!route) return;
  $("fleet-view").hidden = Boolean(route.id);
  $("truck-view").hidden = !route.id;
  if (!truckView.fleet) loadFleet();
  if (route.id) {
    const day = route.day || localToday();
    if (route.id !== truckView.id || day !== truckView.day || !truckView.detail) loadTruckDay(route.id, day);
    else if (truckView.map) window.setTimeout(() => truckView.map.invalidateSize(), 50);
  }
}

// Una sola petición de flota a la vez: la carga inicial y la pestaña no la duplican.
let fleetLoading = null;
let fleetRetry = null;
const FLEET_RETRY_MS = 10000;

function loadFleet() {
  if (!fleetLoading) fleetLoading = fetchFleet().finally(() => { fleetLoading = null; });
  return fleetLoading;
}

async function fetchFleet() {
  window.clearTimeout(fleetRetry);
  const version = sessionVersion;
  try {
    const data = await api("/api/camiones", {timeoutMs: 90000});
    truckView.fleet = data;
    renderFleet();
    if (truckView.detail) renderTruckHead();
  } catch (error) {
    if (version !== sessionVersion || !token() || truckView.fleet) return;
    // Mientras endpoints está ocupado (por ejemplo, calculando el mapa) se reintenta solo.
    $("fleet-board").replaceChildren(fleetMessage("No se pudo cargar la flota: " + error.message +
      " Reintentando en " + FLEET_RETRY_MS / 1000 + " s…"));
    fleetRetry = window.setTimeout(loadFleet, FLEET_RETRY_MS);
  }
}

function fleetMessage(text) {
  const note = document.createElement("p");
  note.className = "fleet-empty";
  note.textContent = text;
  return note;
}

function sparkline(points) {
  const width = 140, height = 30, max = Math.max(1, ...points.map((p) => p.km));
  const step = width / Math.max(1, points.length);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "fleet-spark");
  svg.setAttribute("aria-hidden", "true");
  points.forEach((point, index) => {
    const bar = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    const h = point.km ? Math.max(2, (point.km / max) * (height - 2)) : 1;
    bar.setAttribute("x", String(index * step + 1));
    bar.setAttribute("y", String(height - h));
    bar.setAttribute("width", String(Math.max(1, step - 2)));
    bar.setAttribute("height", String(h));
    bar.setAttribute("rx", "1.5");
    if (!point.km) bar.setAttribute("class", "empty");
    if (index === points.length - 1) bar.setAttribute("class", point.km ? "today" : "empty");
    svg.append(bar);
  });
  return svg;
}

// El tablero muestra hoy (estado en vivo) o un día anterior (resumen del cierre diario).
const boardDay = () => truckView.boardDay || localToday();
const boardIsToday = () => boardDay() === localToday();
const shortTime = (value) => value ? new Intl.DateTimeFormat("es-CL", {timeStyle: "short", timeZone: HISTORY_ZONE}).format(new Date(value)) : "—";

// Lo que hizo el camión en el día del tablero: {km, viajes, minutos_detenido, primer, ultimo, reportes} o null.
function truckDay(truck) {
  if (boardIsToday()) return truck.hoy;
  const row = truckView.dayRows && truckView.dayRows.get(truck.asset_id);
  return row ? {km: row.km_observados, viajes: row.viajes, minutos_detenido: row.minutos_detenido,
    primer: shortTime(row.primer_reporte), ultimo: shortTime(row.ultimo_reporte), reportes: row.reportes} : null;
}

function fleetRow(truck) {
  const today = boardIsToday();
  const day = truckDay(truck);
  const row = document.createElement("button");
  row.type = "button";
  row.className = "fleet-row state-" + (today ? truck.estado : day ? "dia" : "sin_datos");
  row.addEventListener("click", () => openTruck(truck.asset_id, today ? null : boardDay()));
  const name = document.createElement("span");
  name.className = "fleet-name";
  const title = document.createElement("strong");
  title.textContent = truck.nombre;
  const sub = document.createElement("small");
  sub.className = "fleet-identity";
  sub.append(...truckIdentity(truck.asset_id, truck.conductor, null));
  name.append(title, sub);
  const state = document.createElement("span");
  state.className = "fleet-state";
  const label = document.createElement("strong");
  const note = document.createElement("small");
  if (today) {
    label.innerHTML = '<i class="state-dot"></i>';
    label.append(TRUCK_STATES[truck.estado] + (truck.estado === "en_ruta" && truck.velocidad ? ` · ${format(truck.velocidad)} km/h` : ""));
    note.textContent = truckAge(truck.edad_minutos);
  } else {
    label.textContent = day ? `${(day.primer || "—").slice(0, 5)} – ${(day.ultimo || "—").slice(0, 5)}` : "Sin reportes";
    note.textContent = day ? `${format(day.reportes)} reportes` : "";
  }
  state.append(label, note);
  const summary = document.createElement("span");
  summary.className = "fleet-today";
  if (day) {
    for (const [value, unit] of [[format(day.km, 1), "km"], [format(day.viajes), day.viajes === 1 ? "viaje" : "viajes"],
      [formatMinutes(day.minutos_detenido), "detenido"]]) {
      const item = document.createElement("span");
      const strong = document.createElement("b");
      strong.textContent = value;
      item.append(strong, " " + unit);
      summary.append(item);
    }
  } else {
    summary.textContent = today ? "Sin actividad hoy" : "Sin actividad ese día";
    summary.classList.add("is-empty");
  }
  row.append(name, state, summary, sparkline(truck.serie || []));
  return row;
}

function renderBoardDay() {
  const today = boardIsToday();
  const day = boardDay();
  $("board-date").value = day;
  $("board-date").max = localToday();
  $("board-next").disabled = today;
  $("fleet-head-state").textContent = today ? "Último dato" : "Primer y último reporte";
  $("fleet-head-day").textContent = today ? "Hoy" : "Ese día";
  // Los estados en vivo solo tienen sentido hoy; en un día anterior se filtra por actividad.
  for (const button of document.querySelectorAll('[data-fleet-filter]:not([data-fleet-filter="todos"])')) button.hidden = !today;
  if (!today && truckView.filter !== "todos") truckView.filter = "todos";
  const totals = $("fleet-day-totals");
  const fleet = truckView.fleet;
  const state = truckView.dayState;
  let note = "";
  if (today) {
    const summary = fleet && fleet.resumen;
    totals.textContent = summary ? `${format(summary.km_hoy, 0)} km · ${format(summary.viajes_hoy)} viajes hoy` : "";
    note = "Hoy, en curso: las cifras se actualizan cada minuto.";
  } else if (truckView.dayLoading) {
    totals.textContent = "";
    note = "Consultando el día…";
  } else {
    totals.textContent = state ? `${format(state.camiones)} camiones con reportes · ${format(state.km_observados, 0)} km · ${format(state.reportes)} reportes` : "";
    const [label] = state ? (HISTORY_STATES[state.estado] || [state.estado]) : ["Sin resumen calculado para este día"];
    note = label + (state && state.estado === "esperando_datos" ? ": cifras provisorias hasta que la API confirme los reportes hasta las 00:00." : "") +
      (state && state.recalculos ? ` · recalculado ${format(state.recalculos)} ${state.recalculos === 1 ? "vez" : "veces"} por reportes atrasados` : "");
  }
  $("board-day-note").textContent = `${dayLabel(day, "long")} · ${note}`;
}

async function setBoardDay(day) {
  const today = localToday();
  if (!day || day >= today) {
    truckView.boardDay = null;
    renderBoardDay();
    renderFleet();
    return;
  }
  truckView.boardDay = day;
  truckView.dayLoading = true;
  truckView.dayRows = null;
  truckView.dayState = null;
  renderBoardDay();
  renderFleet();
  try {
    const data = await api(`/api/reportes/dia?fecha=${day}`);
    if (truckView.boardDay !== day) return;
    truckView.dayRows = new Map((data.camiones || []).map((row) => [row.asset_id, row]));
    truckView.dayState = data.estado;
  } catch (error) {
    if (truckView.boardDay !== day) return;
    $("fleet-board").replaceChildren(fleetMessage("No se pudo consultar el día: " + error.message));
  } finally {
    if (truckView.boardDay === day) {
      truckView.dayLoading = false;
      renderBoardDay();
      renderFleet();
    }
  }
}

function renderFleet() {
  const data = truckView.fleet;
  if (!data || data.status !== "ok") {
    $("fleet-board").replaceChildren(fleetMessage("La flota aparecerá cuando el catálogo de camiones esté cargado."));
    return;
  }
  const summary = data.resumen;
  for (const key of ["todos", "en_ruta", "detenido", "sin_senal"]) {
    $("fleet-count-" + key).textContent = format(key === "todos" ? summary.total : summary[key] + (key === "sin_senal" ? summary.sin_datos : 0));
  }
  for (const button of document.querySelectorAll("[data-fleet-filter]")) {
    button.setAttribute("aria-pressed", String(button.dataset.fleetFilter === truckView.filter));
  }
  if (!boardIsToday() && truckView.dayLoading) {
    $("fleet-board").replaceChildren(fleetMessage("Cargando el día…"));
    return;
  }
  const query = truckView.query.trim().toLocaleLowerCase("es-CL");
  let trucks = data.camiones.filter((truck) => truckView.filter === "todos" || truck.estado === truckView.filter
    || (truckView.filter === "sin_senal" && truck.estado === "sin_datos"));
  if (query) {
    trucks = trucks.filter((truck) => [truck.nombre, truck.asset_id, truck.conductor].some((value) =>
      value && value.toLocaleLowerCase("es-CL").includes(query)));
  }
  const byName = (a, b) => a.nombre.localeCompare(b.nombre, "es-CL", {numeric: true});
  const km = (truck) => (truckDay(truck) || {}).km || 0;
  const sorters = {
    nombre: byName,
    km: (a, b) => km(b) - km(a) || byName(a, b),
    dato: (a, b) => (a.edad_minutos ?? Infinity) - (b.edad_minutos ?? Infinity) || byName(a, b)
  };
  trucks.sort(sorters[truckView.sort] || byName);
  $("fleet-tab-flota-note").textContent = boardIsToday()
    ? `${format(summary.total)} camiones · ${format(summary.en_ruta)} en ruta ahora`
    : `${format(truckView.dayRows ? truckView.dayRows.size : 0)} con actividad el ${dayLabel(boardDay())}`;
  // En un día anterior, primero los que tuvieron actividad.
  if (!boardIsToday()) trucks.sort((a, b) => Number(Boolean(truckDay(b))) - Number(Boolean(truckDay(a))));
  $("fleet-board").replaceChildren(...(trucks.length ? trucks.map(fleetRow)
    : [fleetMessage(query ? "Ningún camión coincide con la búsqueda." : "No hay camiones en este estado.")]));
  renderBoardDay();
}

async function loadTruckDay(id, day, quiet = false) {
  const request = ++truckView.request;
  truckView.id = id;
  truckView.day = day;
  $("truck-date").value = day;
  $("truck-date").max = localToday();
  $("truck-next").disabled = day >= localToday();
  if (!quiet) {
    stopTruckPlay();
    // La lista de reportes GPS es del día anterior: se cierra y se vuelve a pedir al abrirla.
    historyState.detail = null;
    $("history-detail").open = false;
    $("history-detail-rows").replaceChildren();
    $("history-detail-more").hidden = true;
    $("truck-day-note").textContent = "Cargando el día…";
    $("truck-image").disabled = true;
  }
  renderTruckHead();
  try {
    const data = await api(`/api/camiones/${encodeURIComponent(id)}?fecha=${day}`, {timeoutMs: 90000});
    if (request !== truckView.request) return;
    truckView.detail = data;
    renderTruckDay(quiet);
  } catch (error) {
    if (request !== truckView.request) return;
    truckView.detail = null;
    $("truck-day-note").textContent = "No se pudo cargar este día: " + error.message;
  }
}

function fleetTruck(id) {
  return truckView.fleet && truckView.fleet.camiones ? truckView.fleet.camiones.find((truck) => truck.asset_id === id) : null;
}

// Patente con aspecto de placa, conductor y última ruta (con el color de su nivel de uso).
function truckIdentity(plate, driver, route) {
  const nodes = [];
  const plateNode = document.createElement("span");
  plateNode.className = "plate";
  plateNode.textContent = plate;
  plateNode.title = "Patente";
  nodes.push(plateNode);
  const driverNode = document.createElement("span");
  driverNode.className = "driver" + (driver ? "" : " is-empty");
  driverNode.title = "Conductor";
  driverNode.textContent = driver ? driver.toLocaleLowerCase("es-CL").replace(/(^|\s)\S/g, (c) => c.toLocaleUpperCase("es-CL")) : "Sin conductor informado";
  nodes.push(driverNode);
  if (route) {
    const routeNode = document.createElement("span");
    routeNode.className = "last-route";
    routeNode.style.setProperty("--route", (ROUTE_LEVELS[route.usage_level] || {}).color || "#718785");
    routeNode.title = "Última ruta aprendida";
    routeNode.textContent = route.label;
    nodes.push(routeNode);
  }
  return nodes;
}

function renderTruckHead() {
  const live = fleetTruck(truckView.id);
  const detail = truckView.detail && truckView.detail.asset_id === truckView.id ? truckView.detail : null;
  const name = (live && live.nombre) || (detail && detail.nombre) || truckView.id;
  $("truck-name").textContent = "Camión " + name;
  const driver = (detail && detail.conductor) || (live && live.conductor);
  $("truck-meta").replaceChildren(...truckIdentity(truckView.id, driver, live && live.ruta));
  const state = $("truck-state");
  state.className = "truck-state state-" + (live ? live.estado : "sin_datos");
  state.innerHTML = '<i class="state-dot"></i>';
  state.append(live ? `${TRUCK_STATES[live.estado]} · ${truckAge(live.edad_minutos)}` : "Estado en vivo no disponible");
}

function dayStatusNote(data) {
  if (!data.resumen) return "Este camión no tiene reportes este día.";
  if (data.estado_dia === "en_curso") return "Hoy, en curso: las cifras se actualizan cada pocos minutos hasta las 00:00.";
  if (data.estado_dia === "esperando_datos") return "El día terminó, pero faltan reportes de la API: las cifras son provisorias.";
  return "Día cerrado (00:00 a 00:00, hora de Chile).";
}

function renderTruckDay(keepView = false) {
  const data = truckView.detail;
  renderTruckHead();
  const summary = data.resumen;
  const pin = activePin(data);
  $("truck-day-note").textContent = `${dayLabel(data.fecha, "long")} · ${dayStatusNote(data)}` +
    (pin ? ` · El punto rojo marca dónde estaba a las ${pin.time} (${pin.event}).` : "");
  $("truck-km").textContent = summary ? format(summary.km, 1) : "—";
  const average = (data.historial || []).filter((item) => item.fecha !== data.fecha && item.km > 0);
  const mean = average.length ? average.reduce((sum, item) => sum + item.km, 0) / average.length : null;
  $("truck-km-note").textContent = summary && mean
    ? `Su promedio en días con actividad: ${format(mean, 0)} km` : " ";
  $("truck-trips").textContent = summary ? format(summary.viajes) : "—";
  $("truck-stopped").textContent = summary ? formatMinutes(summary.minutos_detenido) : "—";
  $("truck-stopped-note").textContent = summary ? `${format(summary.paradas)} ${summary.paradas === 1 ? "parada" : "paradas"} de 10 min o más` : " ";
  $("truck-span").textContent = summary ? `${clock(summary.primer)} – ${clock(summary.ultimo)}` : "—";
  $("truck-span-note").textContent = summary ? `${formatMinutes(summary.minutos_activo)} entre ambos` : " ";
  const hasRoute = Boolean(data.recorrido && data.recorrido.lineas.length);
  $("truck-image").disabled = !hasRoute;
  $("truck-play").disabled = !data.viajes.length;
  $("truck-signal-note").textContent = data.recorrido && data.recorrido.cortes
    ? `El GPS dejó de reportar ${format(data.recorrido.cortes)} ${data.recorrido.cortes === 1 ? "vez" : "veces"} ` +
      `(${formatMinutes(data.recorrido.minutos_sin_senal)} en total).` : "";
  renderTruckRibbon(data);
  renderTruckEvents(data);
  renderTruckHistory(data);
  if (truckView.play) return;  // no interrumpir la reproducción con la actualización automática
  drawTruckMap(data, keepView);
}

// Orden cronológico de viajes, paradas e intervalos sin datos.
function truckTimeline(data) {
  const items = [
    ...data.viajes.map((trip) => ({kind: "viaje", from: trip.inicio, to: trip.fin, trip})),
    ...data.eventos.map((event) => ({kind: event.tipo, from: event.desde, to: event.hasta, event}))
  ];
  return items.sort((a, b) => a.from.localeCompare(b.from));
}

// --- Geometría auxiliar (mapa e imagen) ---
const distanceM = (a, b) => Math.hypot(b[0] - a[0], (b[1] - a[1]) * Math.cos((a[0] + b[0]) * Math.PI / 360)) * 111000;

// Paradas agrupadas por lugar (a menos de 300 m): una sola marca con todas sus visitas,
// así las paradas repetidas en la base no se tapan entre sí.
function stopPlaces(data) {
  const places = [];
  let number = 0;
  for (const event of data.eventos) {
    if (event.tipo !== "parada" || !event.lugar) continue;
    number += 1;
    let place = places.find((item) => distanceM(item.lugar, event.lugar) < 300);
    if (!place) { place = {lugar: event.lugar, nombre: event.nombre, visitas: []}; places.push(place); }
    if (!place.nombre && event.nombre) place.nombre = event.nombre;
    place.visitas.push({...event, numero: number});
  }
  return places;
}

function stopPlaceLabel(place) {
  const numbers = place.visitas.map((visit) => visit.numero);
  return numbers.length <= 3 ? numbers.join(" ") : `${numbers[0]}…${numbers[numbers.length - 1]}`;
}

// Cortes de señal: línea recta (punteada) entre el último punto antes del corte y el
// primero después. Se dibuja aparte para que se entienda el recorrido completo, pero no es
// el camino exacto.
function signalGaps(lines) {
  const gaps = [];
  for (let k = 1; k < lines.length; k++) {
    const from = lines[k - 1][lines[k - 1].length - 1], to = lines[k][0];
    if (distanceM(from, to) > 40) gaps.push([from, to]);
  }
  return gaps;
}

// Punto a una fracción del largo de un viaje y su rumbo (grados, 0 = este, sentido horario).
function pointAlong(lines, fraction) {
  const segments = [];
  let total = 0;
  for (const line of lines) {
    for (let k = 1; k < line.length; k++) {
      const length = distanceM(line[k - 1], line[k]);
      if (length > 0) { segments.push([line[k - 1], line[k], length]); total += length; }
    }
  }
  if (!segments.length) return null;
  let target = total * fraction;
  for (const [a, b, length] of segments) {
    if (target <= length) {
      const t = target / length;
      const angle = Math.atan2(-(b[0] - a[0]), (b[1] - a[1]) * Math.cos(a[0] * Math.PI / 180)) * 180 / Math.PI;
      return {point: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], angle};
    }
    target -= length;
  }
  const [a, b] = segments[segments.length - 1];
  return {point: b, angle: Math.atan2(-(b[0] - a[0]), (b[1] - a[1]) * Math.cos(a[0] * Math.PI / 180)) * 180 / Math.PI};
}

function tripLength(trip) {
  let total = 0;
  for (const line of trip.lineas) for (let k = 1; k < line.length; k++) total += distanceM(line[k - 1], line[k]);
  return total;
}

// --- Cinta de 24 horas ---
function renderTruckRibbon(data) {
  const ribbon = $("truck-ribbon");
  ribbon.replaceChildren();
  const dayStart = data.fecha + " 00:00:00";
  for (const item of truckTimeline(data)) {
    // Un tramo que empezó el día anterior se recorta a las 00:00.
    const from = item.from < dayStart ? 0 : minuteOfDay(item.from);
    const to = item.to.slice(0, 10) > data.fecha ? 1440 : minuteOfDay(item.to);
    if (to <= from) continue;
    const block = document.createElement("span");
    block.className = "ribbon-block " + (item.kind === "viaje" ? "move" : item.kind === "parada" ? "stop" : "gap");
    block.style.left = (from / 14.4) + "%";
    block.style.width = Math.max(0.3, (to - from) / 14.4) + "%";
    block.title = item.kind === "viaje"
      ? `Viaje ${item.trip.numero}: ${clock(item.from)}–${clock(item.to)} · ${format(item.trip.km, 1)} km`
      : `${item.kind === "parada" ? "Detenido" : "Sin datos"}: ${clock(item.from)}–${clock(item.to)} (${formatMinutes(item.event.minutos)})`;
    if (item.trip) {
      block.style.background = tripColor(item.trip.numero);
      block.addEventListener("click", () => focusTrip(item.trip.numero));
    }
    ribbon.append(block);
  }
  const pin = activePin(data);
  if (pin) {
    const mark = document.createElement("span");
    mark.className = "ribbon-pin";
    mark.style.left = ((Number(pin.time.slice(0, 2)) * 60 + Number(pin.time.slice(3, 5))) / 14.4) + "%";
    mark.title = `Reporte de las ${pin.time}`;
    ribbon.append(mark);
  }
  if (data.fecha === localToday()) {
    const now = document.createElement("span");
    now.className = "ribbon-now";
    const parts = new Intl.DateTimeFormat("en-GB", {hour: "2-digit", minute: "2-digit", hour12: false, timeZone: HISTORY_ZONE}).format(new Date()).split(":");
    now.style.left = ((Number(parts[0]) * 60 + Number(parts[1])) / 14.4) + "%";
    now.title = "Ahora";
    ribbon.append(now);
  }
  if (!ribbon.children.length) ribbon.append(Object.assign(document.createElement("span"), {className: "ribbon-empty", textContent: "Sin viajes ni paradas este día"}));
}

// --- El día, paso a paso ---
function renderTruckEvents(data) {
  const list = $("truck-events");
  list.replaceChildren();
  let stopNumber = 0;
  for (const item of truckTimeline(data)) {
    const li = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = clock(item.from);
    const body = document.createElement("div");
    const title = document.createElement("strong");
    const detail = document.createElement("small");
    const mark = document.createElement("span");
    if (item.kind === "viaje") {
      const trip = item.trip;
      li.className = "event move";
      li.dataset.trip = String(trip.numero);
      li.style.setProperty("--trip", tripColor(trip.numero));
      mark.className = "event-mark trip";
      mark.textContent = "V" + trip.numero;
      title.textContent = `Viaje ${trip.numero} · ${format(trip.km, 1)} km`;
      detail.textContent = `${clock(trip.inicio)} a ${clock(trip.fin)} · ${formatMinutes(trip.minutos)}` +
        (trip.abierto_inicio ? " · venía del día anterior" : "") + (trip.abierto_fin ? " · sigue al día siguiente" : "") +
        (trip.con_saltos ? " · con cortes de señal" : "");
      li.tabIndex = 0;
      li.title = "Ver este viaje en el mapa";
      li.addEventListener("click", () => focusTrip(trip.numero));
      li.addEventListener("keydown", (event) => { if (event.key === "Enter") focusTrip(trip.numero); });
    } else if (item.kind === "parada") {
      stopNumber += 1;
      li.className = "event stop";
      mark.className = "event-mark stop";
      mark.textContent = String(stopNumber);
      title.textContent = `Detenido ${formatMinutes(item.event.minutos)}`;
      detail.textContent = `${clock(item.from)} a ${clock(item.to)} · ${item.event.nombre || "Lugar sin nombre"}`;
    } else {
      li.className = "event gap";
      mark.className = "event-mark gap";
      mark.textContent = "?";
      title.textContent = `Sin datos ${formatMinutes(item.event.minutos)}`;
      detail.textContent = `${clock(item.from)} a ${clock(item.to)} · el GPS no reportó; no se sabe dónde estuvo`;
    }
    body.append(title, detail);
    li.append(time, mark, body);
    list.append(li);
  }
  // Llegando desde un reporte: se marca qué estaba haciendo el camión a esa hora.
  const pin = activePin(data);
  if (pin) {
    const at = `${data.fecha} ${pin.time}:00`;
    const items = truckTimeline(data);
    const index = items.findIndex((item) => item.from <= at && at <= item.to);
    const node = index >= 0 ? list.children[index] : null;
    if (node) {
      node.classList.add("is-report");
      window.setTimeout(() => { list.scrollTop = node.offsetTop - list.offsetTop - list.clientHeight / 3; }, 50);
    }
  }
  if (!list.children.length) {
    const li = document.createElement("li");
    li.className = "event empty";
    li.textContent = data.resumen
      ? "Hubo reportes, pero ningún viaje de más de 1 km ni paradas de 10 min."
      : "Sin reportes este día. Elige otro día en el calendario o en los últimos 30 días.";
    list.append(li);
  }
}

// --- Últimos 30 días: ventana fija que termina hoy ---
function renderTruckHistory(data) {
  const chart = $("truck-history");
  const byDay = new Map((data.historial || []).map((item) => [item.fecha, item]));
  const today = data.hoy || localToday();
  const days = Array.from({length: 30}, (_, k) => shiftDay(today, k - 29));
  const max = Math.max(1, ...days.map((day) => (byDay.get(day) || {}).km || 0));
  chart.replaceChildren(...days.map((day) => {
    const item = byDay.get(day);
    const button = document.createElement("button");
    button.type = "button";
    const weekday = dayDate(day).getUTCDay();
    button.className = "history-bar" + (day === data.fecha ? " is-selected" : "") + (weekday === 0 || weekday === 6 ? " weekend" : "");
    button.title = `${dayLabel(day, "long")}: ` + (item ? `${format(item.km, 1)} km · ${format(item.viajes)} viajes` : "sin reportes");
    button.setAttribute("aria-pressed", String(day === data.fecha));
    const bar = document.createElement("i");
    bar.style.height = item && item.km ? Math.max(3, (item.km / max) * 100) + "%" : "2px";
    const label = document.createElement("span");
    label.textContent = String(Number(day.slice(8)));
    button.append(bar, label);
    button.addEventListener("click", () => openTruck(data.asset_id, day));
    return button;
  }));
}

// --- Mapa del día ---
function ensureTruckMap() {
  if (truckView.map || !window.L) return truckView.map;
  truckView.map = ArtisaMapaBase.crear($("truck-map"), {pedir: mapPedir, centro: [-23.6, -70.4], zoom: 7}).mapa;
  truckView.map.createPane("truckMarks").style.zIndex = 650;
  truckView.map.on("zoomend", applyTripLanes);
  return truckView.map;
}

function pinIcon(text, className, color) {
  const style = color ? ` style="--pin:${color}"` : "";
  return L.divIcon({className: "truck-pin-icon", html: `<span class="truck-pin ${className}"${style}>${text}</span>`,
    iconSize: [0, 0], iconAnchor: [0, 0]});
}

function arrowIcon(angle, color) {
  return L.divIcon({className: "truck-pin-icon",
    html: `<span class="truck-arrow" style="--pin:${color};transform:rotate(${angle.toFixed(0)}deg)"></span>`,
    iconSize: [14, 14], iconAnchor: [7, 7]});
}

function playbackTruckIcon(facing = 1) {
  return L.divIcon({
    className: "playback-truck-icon",
    html: `<span class="playback-truck-marker" style="--truck-facing:${facing}">${TRUCK_MARKUP}</span>`,
    iconSize: [56, 38],
    iconAnchor: [28, 19]
  });
}

function playbackTruckFacing(map, points, index, fallback = 1) {
  if (!points || points.length < 2) return fallback;
  const before = points[Math.max(0, index - 1)];
  const after = points[Math.min(points.length - 1, index + 1)];
  const first = map.latLngToLayerPoint(before);
  const second = map.latLngToLayerPoint(after);
  const dx = second.x - first.x;
  return Math.abs(dx) < 0.25 ? fallback : dx > 0 ? -1 : 1;
}

// Los viajes se corren unos píxeles a la derecha de su sentido de marcha, como pistas de una
// carretera: la ida y la vuelta por el mismo camino quedan lado a lado en vez de taparse.
const TRIP_LANE_PX = 4;

function offsetPath(points, px) {
  if (points.length < 2) return points.map((p) => [p[0], p[1]]);
  return points.map((p, k) => {
    const a = points[Math.max(0, k - 1)], b = points[Math.min(points.length - 1, k + 1)];
    const dx = b[0] - a[0], dy = b[1] - a[1], length = Math.hypot(dx, dy) || 1;
    return [p[0] - dy / length * px, p[1] + dx / length * px];
  });
}

function laneLines(map, lines) {
  return lines.map((line) => offsetPath(line.map((p) => {
    const point = map.latLngToLayerPoint(p);
    return [point.x, point.y];
  }), TRIP_LANE_PX).map(([x, y]) => {
    const latlng = map.layerPointToLatLng([x, y]);
    return [latlng.lat, latlng.lng];
  }));
}

function applyTripLanes() {
  const map = truckView.map;
  if (!map) return;
  for (const entry of truckView.trips.values()) {
    const lanes = laneLines(map, entry.trip.lineas);
    entry.lanes = lanes;
    for (const part of entry.parts) part.setLatLngs(lanes);
    for (const mark of entry.marks) {
      const at = pointAlong(lanes, mark.options.fraction);
      if (at) mark.setLatLng(at.point);
    }
  }
}

// Marcas fijas del mapa: paradas agrupadas por lugar, y A (primer punto) y B (último). Si A o
// B caen donde hay paradas, van en la misma marca para que no se tapen.
function placeMarks(data) {
  const lines = data.recorrido ? data.recorrido.lineas : [];
  const marks = stopPlaces(data).map((place) => ({lugar: place.lugar, place, start: false, end: false}));
  if (lines.length) {
    const first = lines[0][0], lastLine = lines[lines.length - 1], last = lastLine[lastLine.length - 1];
    for (const [point, key] of [[first, "start"], [last, "end"]]) {
      let mark = marks.find((item) => distanceM(item.lugar, point) < 600);
      if (!mark) { mark = {lugar: point, place: null, start: false, end: false}; marks.push(mark); }
      mark[key] = true;
    }
  }
  return marks.map((mark) => ({...mark, parts: [
    ...(mark.start ? [["start", "A"]] : []),
    ...(mark.place ? [["stop", stopPlaceLabel(mark.place)]] : []),
    ...(mark.end ? [["end", "B"]] : [])
  ]}));
}

function placeMarkTooltip(mark, data) {
  const lines = [];
  if (mark.start) lines.push(`<strong>A</strong> · primer punto del día · ${clock(data.recorrido.inicio)}`);
  if (mark.place) {
    lines.push(`<strong>${mark.place.visitas.length > 1 ? `${mark.place.visitas.length} paradas en ` : "Parada en "}` +
      `${escapeText(mark.place.nombre || "lugar sin nombre")}</strong>`);
    for (const visit of mark.place.visitas) {
      lines.push(`${visit.numero}. ${clock(visit.desde)}–${clock(visit.hasta)} (${formatMinutes(visit.minutos)})`);
    }
  }
  if (mark.end) lines.push(`<strong>B</strong> · último punto · ${clock(data.recorrido.fin)}`);
  return lines.join("<br>");
}

function drawTruckMap(data, keepView = false) {
  const map = ensureTruckMap();
  if (!map) return;
  map.invalidateSize();
  if (truckView.layer) map.removeLayer(truckView.layer);
  truckView.trips.clear();
  truckView.base = [];
  truckView.focus = null;
  $("truck-show-all").hidden = true;
  const layer = L.featureGroup();
  const lines = data.recorrido ? data.recorrido.lineas : [];
  // 1. Todo lo que registró el GPS (también movimientos cortos que no son viajes), de fondo.
  if (lines.length) {
    truckView.base.push(
      L.polyline(lines, {color: TRUCK_COLORS.halo, weight: 6, opacity: 0.85, interactive: false}).addTo(layer),
      L.polyline(lines, {color: TRUCK_COLORS.base, weight: 3, opacity: 0.9, interactive: false}).addTo(layer));
    for (const gap of signalGaps(lines)) {
      truckView.base.push(L.polyline(gap, {color: TRUCK_COLORS.gap, weight: 2, opacity: 0.8, dashArray: "2 7"})
        .bindTooltip("Sin señal: línea recta entre dos reportes, no es el camino exacto", {sticky: true}).addTo(layer));
    }
  }
  // 2. Cada viaje con su color, flechas de sentido y su número (V1, V2…).
  for (const trip of data.viajes) {
    const color = tripColor(trip.numero);
    const parts = [
      L.polyline(trip.lineas, {color: TRUCK_COLORS.halo, weight: 8, opacity: 0.95, lineJoin: "round", lineCap: "round"}),
      L.polyline(trip.lineas, {color, weight: 4, opacity: 1, lineJoin: "round", lineCap: "round"})
    ];
    const tip = `Viaje ${trip.numero}: ${clock(trip.inicio)}–${clock(trip.fin)} · ${format(trip.km, 1)} km`;
    for (const part of parts) part.bindTooltip(tip, {sticky: true}).on("click", () => focusTrip(trip.numero));
    const marks = [];
    if (tripLength(trip) > 400) {
      for (const fraction of [0.2, 0.8]) {
        const at = pointAlong(trip.lineas, fraction);
        if (at) marks.push(L.marker(at.point, {icon: arrowIcon(at.angle, color), interactive: false, pane: "truckMarks", fraction}));
      }
      const fraction = 0.35;
      const middle = pointAlong(trip.lineas, fraction);
      if (middle) marks.push(L.marker(middle.point, {icon: pinIcon("V" + trip.numero, "trip", color), pane: "truckMarks", fraction})
        .bindTooltip(tip).on("click", () => focusTrip(trip.numero)));
    }
    for (const item of [...parts, ...marks]) item.addTo(layer);
    truckView.trips.set(trip.numero, {trip, parts, marks, lanes: trip.lineas});
  }
  // 3. Paradas (agrupadas por lugar) y los puntos A y B.
  for (const mark of placeMarks(data)) {
    const html = `<span class="truck-pin group">${mark.parts.map(([kind, text]) => `<b class="${kind}">${text}</b>`).join("")}</span>`;
    L.marker(mark.lugar, {icon: L.divIcon({className: "truck-pin-icon", html, iconSize: [0, 0], iconAnchor: [0, 0]}),
      zIndexOffset: mark.start || mark.end ? 600 : 400, pane: "truckMarks"})
      .bindTooltip(placeMarkTooltip(mark, data)).addTo(layer);
  }
  layer.addTo(map);
  truckView.layer = layer;
  // Si se llegó desde un reporte, se marca dónde estaba el camión en ese momento.
  const pin = activePin(data);
  if (pin && pin.point) {
    L.marker(pin.point, {icon: L.divIcon({className: "truck-pin-icon", iconSize: [0, 0], iconAnchor: [0, 0],
      html: `<span class="report-pin"><b></b><em>${escapeText(pin.time)}</em></span>`}), zIndexOffset: 1000, pane: "truckMarks"})
      .bindTooltip(`Reporte del ${escapeText(pin.label)} · ${escapeText(pin.event)}`).addTo(layer);
  }
  if (!keepView && layer.getLayers().length) map.fitBounds(layer.getBounds(), {padding: [30, 30], maxZoom: 14, animate: false});
  applyTripLanes();
}

function activePin(data) {
  const pin = truckView.pin;
  return pin && data && pin.id === data.asset_id && pin.day === data.fecha ? pin : null;
}

function escapeText(value) {
  const node = document.createElement("span");
  node.textContent = value;
  return node.innerHTML;
}

// Ver un viaje: él al frente, el resto atenuado.
function focusTrip(number, fit = true) {
  const entry = truckView.trips.get(number);
  if (!entry || !truckView.map) return;
  truckView.focus = number;
  for (const [key, item] of truckView.trips) {
    const active = key === number;
    item.parts[0].setStyle({opacity: active ? 0.95 : 0});
    item.parts[1].setStyle({opacity: active ? 1 : 0.18, weight: active ? 6 : 3.5});
    for (const mark of item.marks) mark.setOpacity(active ? 1 : 0);
    if (active) { item.parts[0].bringToFront(); item.parts[1].bringToFront(); }
  }
  for (const base of truckView.base) base.setStyle({opacity: 0.35});
  for (const node of document.querySelectorAll(".truck-events .event.move")) {
    node.classList.toggle("is-selected", node.dataset.trip === String(number));
  }
  $("truck-show-all").hidden = false;
  if (fit) truckView.map.fitBounds(L.featureGroup(entry.parts).getBounds(), {padding: [50, 50], maxZoom: 15});
}

function showWholeDay() {
  stopTruckPlay();
  if (truckView.detail) drawTruckMap(truckView.detail);
  for (const node of document.querySelectorAll(".truck-events .event.is-selected")) node.classList.remove("is-selected");
}

// --- Reproducir el día: el camión recorre sus viajes en orden y se detiene en cada parada ---
function playbackSteps(data) {
  const steps = [];
  let stopNumber = 0;
  for (const item of truckTimeline(data)) {
    if (item.kind === "viaje") {
      if (item.trip.lineas.flat().length > 1) steps.push({kind: "viaje", trip: item.trip});
    } else if (item.kind === "parada") {
      stopNumber += 1;
      if (item.event.lugar) steps.push({kind: "parada", event: item.event, numero: stopNumber});
    } else {
      steps.push({kind: "sin_datos", event: item.event});
    }
  }
  return steps;
}

function setPlayStatus(text) {
  $("truck-play-status").textContent = text;
}

function stopTruckPlay() {
  const play = truckView.play;
  if (!play) return;
  cancelAnimationFrame(play.frame);
  clearTimeout(play.timer);
  if (play.layer && truckView.map) truckView.map.removeLayer(play.layer);
  truckView.play = null;
  $("truck-play").textContent = "▶ Reproducir el día";
  $("truck-play").setAttribute("aria-pressed", "false");
  setPlayStatus("");
  for (const node of document.querySelectorAll(".truck-events .event.is-playing")) node.classList.remove("is-playing");
}

function toggleTruckPlay() {
  if (truckView.play) { showWholeDay(); return; }
  const data = truckView.detail;
  const map = truckView.map;
  if (!data || !map || !data.viajes.length) return;
  drawTruckMap(data);
  for (const item of truckView.trips.values()) {
    item.parts[0].setStyle({opacity: 0});
    item.parts[1].setStyle({opacity: 0.15});
    for (const mark of item.marks) mark.setOpacity(0);
  }
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const play = {steps: playbackSteps(data), index: 0, frame: 0, timer: 0, layer: L.layerGroup().addTo(map)};
  const firstPoints = data.viajes[0].lineas.flat();
  play.facing = playbackTruckFacing(map, firstPoints, 0);
  play.truck = L.marker(firstPoints[0],
    {icon: playbackTruckIcon(play.facing),
      zIndexOffset: 900, pane: "truckMarks"}).addTo(play.layer);
  truckView.play = play;
  $("truck-play").textContent = "■ Detener";
  $("truck-play").setAttribute("aria-pressed", "true");
  const markItem = (selector) => {
    for (const node of document.querySelectorAll(".truck-events .event.is-playing")) node.classList.remove("is-playing");
    const node = selector && document.querySelector(selector);
    if (node) {
      node.classList.add("is-playing");
      const list = $("truck-events");
      list.scrollTop = node.offsetTop - list.offsetTop - list.clientHeight / 3;
    }
  };
  const next = () => {
    if (truckView.play !== play) return;
    if (play.index >= play.steps.length) {
      setPlayStatus("Fin del día · " + clock(data.recorrido.fin));
      play.timer = setTimeout(() => { if (truckView.play === play) showWholeDay(); }, 2500);
      return;
    }
    const step = play.steps[play.index++];
    if (step.kind === "parada") {
      play.truck.setLatLng(step.event.lugar);
      setPlayStatus(`Parada ${step.numero} · detenido ${formatMinutes(step.event.minutos)} · ${clock(step.event.desde)}–${clock(step.event.hasta)}` +
        (step.event.nombre ? ` · ${step.event.nombre}` : ""));
      markItem(null);
      play.timer = setTimeout(next, reduceMotion ? 300 : 900);
      return;
    }
    if (step.kind === "sin_datos") {
      setPlayStatus(`Sin datos · ${formatMinutes(step.event.minutos)} sin reportes`);
      play.timer = setTimeout(next, reduceMotion ? 200 : 600);
      return;
    }
    const trip = step.trip, color = tripColor(trip.numero);
    // Si el viaje no cabe en la vista, el mapa lo sigue (sin animar, para que las pistas se recalculen).
    const tripBounds = L.latLngBounds(trip.lineas.flat());
    if (!map.getBounds().contains(tripBounds)) map.fitBounds(tripBounds, {padding: [40, 40], maxZoom: 15, animate: false});
    const entryLanes = truckView.trips.get(trip.numero);
    step.points = (entryLanes ? entryLanes.lanes : trip.lineas).flat();
    setPlayStatus(`Viaje ${trip.numero} · ${clock(trip.inicio)} → ${clock(trip.fin)} · ${format(trip.km, 1)} km`);
    markItem(`.truck-events .event.move[data-trip="${trip.numero}"]`);
    const entry = truckView.trips.get(trip.numero);
    if (entry) for (const mark of entry.marks) mark.setOpacity(1);
    L.polyline(step.points, {color: TRUCK_COLORS.halo, weight: 9, opacity: 0.9, interactive: false}).addTo(play.layer);
    const drawn = L.polyline([step.points[0]], {color, weight: 5, opacity: 1, interactive: false}).addTo(play.layer);
    const duration = reduceMotion ? 1 : Math.min(5000, 1200 + trip.km * 25);
    const started = performance.now();
    const animate = (now) => {
      if (truckView.play !== play) return;
      const t = Math.min(1, (now - started) / duration);
      const count = Math.max(1, Math.round(t * (step.points.length - 1)));
      drawn.setLatLngs(step.points.slice(0, count + 1));
      play.truck.setLatLng(step.points[count]);
      const facing = playbackTruckFacing(map, step.points, count, play.facing);
      if (facing !== play.facing) {
        play.facing = facing;
        const marker = play.truck.getElement() && play.truck.getElement().querySelector(".playback-truck-marker");
        if (marker) marker.style.setProperty("--truck-facing", String(facing));
      }
      if (t < 1) play.frame = requestAnimationFrame(animate);
      else play.timer = setTimeout(next, 250);
    };
    play.frame = requestAnimationFrame(animate);
  };
  next();
}

// --- Imagen del recorrido ---
// Lienzo con el mapa base de OpenStreetMap si el navegador lo permite (si no, fondo liso),
// los viajes con su color, sentido y número, las paradas, y a la derecha el día paso a paso.
const TILE = 256;
const mercator = (lat, lon, zoom) => {
  const scale = TILE * 2 ** zoom, sin = Math.sin(lat * Math.PI / 180);
  return [(lon + 180) / 360 * scale, (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale];
};

function loadTile(url) {
  return new Promise((resolve) => {
    const image = new Image();
    image.crossOrigin = "anonymous";
    const timer = setTimeout(() => resolve(null), 8000);
    image.onload = () => { clearTimeout(timer); resolve(image); };
    image.onerror = () => { clearTimeout(timer); resolve(null); };
    image.src = url;
  });
}

function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

async function drawTruckImage(data, withTiles) {
  const width = 1600, height = 1100, header = 150, footer = 64, side = 450, pad = 40;
  const mapW = width - side;
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#eef2ee";
  ctx.fillRect(0, 0, width, height);
  const lines = data.recorrido.lineas;
  const points = lines.flat();
  const lats = points.map((p) => p[0]), lons = points.map((p) => p[1]);
  const top = header, bottom = height - footer;
  let zoom = 17;
  for (; zoom > 3; zoom--) {
    const [x0, y0] = mercator(Math.max(...lats), Math.min(...lons), zoom);
    const [x1, y1] = mercator(Math.min(...lats), Math.max(...lons), zoom);
    if (x1 - x0 <= mapW - pad * 2 && y1 - y0 <= bottom - top - pad * 2) break;
  }
  const [cx0, cy0] = mercator(Math.max(...lats), Math.min(...lons), zoom);
  const [cx1, cy1] = mercator(Math.min(...lats), Math.max(...lons), zoom);
  const origin = [(cx0 + cx1) / 2 - mapW / 2, (cy0 + cy1) / 2 - (top + bottom) / 2];
  const project = ([lat, lon]) => { const [x, y] = mercator(lat, lon, zoom); return [x - origin[0], y - origin[1]]; };
  ctx.save();
  ctx.beginPath(); ctx.rect(0, top, mapW, bottom - top); ctx.clip();
  let tilesDrawn = 0;
  if (withTiles) {
    const tiles = [];
    const maxTile = 2 ** zoom;
    for (let tx = Math.floor(origin[0] / TILE); tx <= Math.floor((origin[0] + mapW) / TILE); tx++) {
      for (let ty = Math.floor((origin[1] + top) / TILE); ty <= Math.floor((origin[1] + bottom) / TILE); ty++) {
        if (ty < 0 || ty >= maxTile) continue;
        const wrapped = ((tx % maxTile) + maxTile) % maxTile;
        tiles.push(loadTile(`https://tile.openstreetmap.org/${zoom}/${wrapped}/${ty}.png`).then((image) => ({image, tx, ty})));
      }
    }
    for (const {image, tx, ty} of await Promise.all(tiles)) {
      if (image) { ctx.drawImage(image, tx * TILE - origin[0], ty * TILE - origin[1]); tilesDrawn += 1; }
    }
    if (tilesDrawn) { ctx.fillStyle = "rgba(244,246,243,.35)"; ctx.fillRect(0, top, mapW, bottom - top); }
  }
  const strokePixels = (paths, color, lineWidth, dash = []) => {
    ctx.strokeStyle = color; ctx.lineWidth = lineWidth; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.setLineDash(dash);
    for (const line of paths) {
      ctx.beginPath();
      line.forEach(([x, y], index) => { if (index) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      ctx.stroke();
    }
    ctx.setLineDash([]);
  };
  const strokeLines = (paths, color, lineWidth, dash) => strokePixels(paths.map((line) => line.map(project)), color, lineWidth, dash);
  // Viajes en su "pista" (a la derecha del sentido de marcha), igual que en el mapa de la web.
  const lanes = new Map(data.viajes.map((trip) => [trip.numero, trip.lineas.map((line) => offsetPath(line.map(project), 5))]));
  const alongPixels = (paths, fraction) => {
    const segments = [];
    let total = 0;
    for (const line of paths) for (let k = 1; k < line.length; k++) {
      const length = Math.hypot(line[k][0] - line[k - 1][0], line[k][1] - line[k - 1][1]);
      if (length) { segments.push([line[k - 1], line[k], length]); total += length; }
    }
    let target = total * fraction;
    for (const [a, b, length] of segments) {
      if (target <= length) {
        const t = target / length;
        return {x: a[0] + (b[0] - a[0]) * t, y: a[1] + (b[1] - a[1]) * t, angle: Math.atan2(b[1] - a[1], b[0] - a[0])};
      }
      target -= length;
    }
    return null;
  };
  strokeLines(lines, "#ffffff", 7);
  strokeLines(lines, TRUCK_COLORS.base, 3.5);
  strokeLines(signalGaps(lines), TRUCK_COLORS.gap, 2.5, [3, 9]);
  for (const trip of data.viajes) {
    strokePixels(lanes.get(trip.numero), "#ffffff", 9);
    strokePixels(lanes.get(trip.numero), tripColor(trip.numero), 4.5);
  }
  const font = (weight, size) => `${weight} ${size}px Inter, "Segoe UI", Arial, sans-serif`;
  for (const trip of data.viajes) {
    if (tripLength(trip) <= 400) continue;
    const color = tripColor(trip.numero);
    for (const fraction of [0.2, 0.8]) {
      const at = alongPixels(lanes.get(trip.numero), fraction);
      if (!at) continue;
      ctx.save(); ctx.translate(at.x, at.y); ctx.rotate(at.angle);
      ctx.beginPath(); ctx.moveTo(9, 0); ctx.lineTo(-7, -7); ctx.lineTo(-3, 0); ctx.lineTo(-7, 7); ctx.closePath();
      ctx.fillStyle = color; ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2.5; ctx.stroke(); ctx.fill();
      ctx.restore();
    }
    const middle = alongPixels(lanes.get(trip.numero), 0.35);
    if (middle) {
      const {x, y} = middle;
      ctx.font = font(700, 15);
      const label = "V" + trip.numero, w = ctx.measureText(label).width + 16;
      roundedRect(ctx, x - w / 2, y - 13, w, 26, 6);
      ctx.fillStyle = color; ctx.fill(); ctx.lineWidth = 2.5; ctx.strokeStyle = "#ffffff"; ctx.stroke();
      ctx.fillStyle = "#ffffff"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(label, x, y + 1);
    }
  }
  // Paradas agrupadas y A/B: una sola marca por lugar, con un tramo de color por cada parte.
  const fills = {start: TRUCK_COLORS.start, stop: TRUCK_COLORS.stop, end: TRUCK_COLORS.end};
  for (const mark of placeMarks(data)) {
    const [x, y] = project(mark.lugar);
    ctx.font = font(700, 15);
    const widths = mark.parts.map(([, text]) => Math.max(32, ctx.measureText(text).width + 18));
    const total = widths.reduce((sum, w) => sum + w, 0);
    let left = x - total / 2;
    roundedRect(ctx, left - 3, y - 19, total + 6, 38, 19);
    ctx.fillStyle = "#ffffff"; ctx.fill();
    ctx.save();
    roundedRect(ctx, left, y - 16, total, 32, 16); ctx.clip();
    mark.parts.forEach(([kind, text], index) => {
      ctx.fillStyle = fills[kind]; ctx.fillRect(left, y - 16, widths[index], 32);
      if (index) { ctx.fillStyle = "#ffffff"; ctx.fillRect(left, y - 16, 2, 32); }
      ctx.fillStyle = "#ffffff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(text, left + widths[index] / 2, y + 1);
      left += widths[index];
    });
    ctx.restore();
  }
  ctx.restore();
  // Encabezado
  ctx.fillStyle = "#172d32";
  ctx.fillRect(0, 0, width, header);
  ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  ctx.fillStyle = "#d5ee9c"; ctx.font = font(600, 19);
  ctx.fillText("Artisa · recorrido del día", pad, 44);
  ctx.fillStyle = "#ffffff"; ctx.font = font(700, 40);
  const label = dayLabel(data.fecha, "long");
  ctx.fillText(`Camión ${data.nombre}  ·  ${label.charAt(0).toUpperCase() + label.slice(1)}`, pad, 94);
  const summary = data.resumen;
  ctx.fillStyle = "#b8c9c4"; ctx.font = font(400, 20);
  ctx.fillText([
    summary ? `${format(summary.km, 1)} km` : null,
    summary ? `${format(summary.viajes)} ${summary.viajes === 1 ? "viaje" : "viajes"}` : null,
    summary ? `${formatMinutes(summary.minutos_detenido)} detenido` : null,
    summary ? `reportes de ${clock(summary.primer)} a ${clock(summary.ultimo)}` : null,
    data.conductor ? `conductor ${data.conductor}` : null
  ].filter(Boolean).join("   ·   "), pad, 128);
  // Columna derecha: el día paso a paso
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(mapW, top, side, bottom - top);
  ctx.fillStyle = "#dce4e2"; ctx.fillRect(mapW, top, 1, bottom - top);
  ctx.fillStyle = "#172d32"; ctx.font = font(700, 20); ctx.textAlign = "left";
  ctx.fillText("El día, paso a paso", mapW + 28, top + 40);
  const rows = truckTimeline(data);
  const rowHeight = 36, maxRows = Math.floor((bottom - top - 80) / rowHeight);
  let y = top + 78, stopNumber = 0;
  rows.slice(0, rows.length > maxRows ? maxRows - 1 : maxRows).forEach((item) => {
    ctx.font = font(400, 15); ctx.fillStyle = "#687b7c"; ctx.textAlign = "left";
    ctx.fillText(clock(item.from), mapW + 28, y);
    let mark, color, text;
    if (item.kind === "viaje") {
      mark = "V" + item.trip.numero; color = tripColor(item.trip.numero);
      text = `Viaje · ${format(item.trip.km, 1)} km · ${formatMinutes(item.trip.minutos)}`;
    } else if (item.kind === "parada") {
      mark = String(++stopNumber); color = TRUCK_COLORS.stop;
      text = `Detenido ${formatMinutes(item.event.minutos)}` + (item.event.nombre ? ` · ${item.event.nombre}` : "");
    } else {
      mark = "?"; color = "#9aa8a6"; text = `Sin datos ${formatMinutes(item.event.minutos)}`;
    }
    ctx.font = font(700, 13);
    const w = Math.max(30, ctx.measureText(mark).width + 14);
    roundedRect(ctx, mapW + 86, y - 17, w, 24, 6);
    ctx.fillStyle = color; ctx.fill();
    ctx.fillStyle = "#ffffff"; ctx.textAlign = "center"; ctx.fillText(mark, mapW + 86 + w / 2, y);
    ctx.textAlign = "left"; ctx.fillStyle = "#172d32"; ctx.font = font(500, 15);
    let shown = text;
    while (ctx.measureText(shown).width > side - 160 && shown.length > 4) shown = shown.slice(0, -2);
    ctx.fillText(shown === text ? text : shown.trimEnd() + "…", mapW + 130, y);
    y += rowHeight;
  });
  if (rows.length > maxRows) {
    ctx.font = font(400, 14); ctx.fillStyle = "#687b7c";
    ctx.fillText(`… y ${rows.length - maxRows + 1} más (ver en la web)`, mapW + 28, y);
  }
  // Pie
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, bottom, width, footer);
  ctx.fillStyle = "#dce4e2"; ctx.fillRect(0, bottom, width, 1);
  ctx.font = font(400, 16); ctx.textAlign = "left"; ctx.textBaseline = "middle";
  const legend = [[TRUCK_COLORS.start, "A primer punto"], [TRIP_COLORS[1], "V1, V2… viajes (flecha = sentido)"],
    [TRUCK_COLORS.stop, "1, 2… paradas"], [TRUCK_COLORS.gap, "punteado: sin señal"], [TRUCK_COLORS.end, "B último punto"]];
  let x = pad;
  for (const [color, text] of legend) {
    ctx.beginPath(); ctx.arc(x + 8, bottom + footer / 2, 8, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
    ctx.fillStyle = "#4d6264"; ctx.fillText(text, x + 22, bottom + footer / 2 + 1);
    x += ctx.measureText(text).width + 52;
  }
  ctx.textAlign = "right";
  ctx.fillText(`GPS real · hora de Chile${tilesDrawn ? " · © OpenStreetMap" : ""}`, width - pad, bottom + footer / 2 + 1);
  return canvas;
}
async function downloadTruckImage() {
  const data = truckView.detail;
  if (!data || !data.recorrido || !data.recorrido.lineas.length) return;
  const button = $("truck-image");
  button.disabled = true;
  button.textContent = "Generando imagen…";
  try {
    let canvas = await drawTruckImage(data, true);
    let blob;
    try {
      blob = await new Promise((resolve, reject) => {
        try { canvas.toBlob((value) => value ? resolve(value) : reject(new Error("vacío")), "image/png"); }
        catch (error) { reject(error); }
      });
    } catch {
      // El mapa base no permitió exportarse: se genera igual, sin fondo de mapa.
      canvas = await drawTruckImage(data, false);
      blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    }
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `recorrido-${data.nombre}-${data.fecha}.png`.replace(/[^\w.-]+/g, "_");
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 5000);
  } finally {
    button.disabled = false;
    button.textContent = "↓ Imagen del recorrido";
  }
}

for (const button of document.querySelectorAll("[data-fleet-filter]")) {
  button.addEventListener("click", () => { truckView.filter = button.dataset.fleetFilter; renderFleet(); });
}
$("fleet-search").addEventListener("input", () => { truckView.query = $("fleet-search").value; renderFleet(); });
$("fleet-sort").addEventListener("change", () => { truckView.sort = $("fleet-sort").value; renderFleet(); });
$("truck-back").addEventListener("click", () => { truckView.pin = null; window.location.hash = "#camiones"; });
$("truck-date").addEventListener("change", () => { if ($("truck-date").value) openTruck(truckView.id, $("truck-date").value); });
$("truck-prev").addEventListener("click", () => openTruck(truckView.id, shiftDay(truckView.day || localToday(), -1)));
$("truck-next").addEventListener("click", () => {
  const next = shiftDay(truckView.day || localToday(), 1);
  if (next <= localToday()) openTruck(truckView.id, next);
});
$("truck-today").addEventListener("click", () => openTruck(truckView.id, localToday()));
$("truck-yesterday").addEventListener("click", () => openTruck(truckView.id, shiftDay(localToday(), -1)));
$("truck-image").addEventListener("click", downloadTruckImage);
$("truck-play").addEventListener("click", toggleTruckPlay);
$("history-detail").addEventListener("toggle", () => {
  const detail = truckView.detail;
  if ($("history-detail").open && detail && !historyState.detail) loadTruckDetail(detail.asset_id, detail.nombre, detail.fecha);
});
$("board-date").addEventListener("change", () => setBoardDay($("board-date").value));
$("board-prev").addEventListener("click", () => setBoardDay(shiftDay(boardDay(), -1)));
$("board-next").addEventListener("click", () => setBoardDay(shiftDay(boardDay(), 1)));
$("board-today").addEventListener("click", () => setBoardDay(null));
$("board-yesterday").addEventListener("click", () => setBoardDay(shiftDay(localToday(), -1)));
// Camiones en vivo conserva solo las tareas operativas: la flota y los reportes recientes.
function setFleetTab(tab) {
  if (tab === "flota") truckView.pin = null;
  truckView.tab = tab;
  for (const button of document.querySelectorAll("[data-fleet-tab]")) {
    button.setAttribute("aria-selected", String(button.dataset.fleetTab === tab));
  }
  $("fleet-panel-flota").hidden = tab !== "flota";
  $("live-reports").hidden = tab !== "reportes";
}
for (const button of document.querySelectorAll("[data-fleet-tab]")) {
  button.addEventListener("click", () => setFleetTab(button.dataset.fleetTab));
}
$("truck-show-all").addEventListener("click", showWholeDay);
window.addEventListener("hashchange", () => { if (!truckHash()) stopTruckPlay(); });
setInterval(() => {
  if (!token()) return;
  if (!truckHash()) return;
  loadFleet();
  // Hoy se actualiza solo, sin mover el mapa ni cortar la reproducción.
  if (truckView.id && truckView.day === localToday() && !$("truck-view").hidden && !truckView.play) {
    loadTruckDay(truckView.id, truckView.day, true);
  }
}, TRUCK_REFRESH_MS);

// --- Asistente (/api/chat) ---
// Ventana flotante en todas las vistas. El historial vive solo en esta página: se borra al
// cerrar sesión o recargar.
const ASSISTANT_MAX = 500;
let assistantBusy = false;

// Solo usa el DOM: logout() puede llamarla antes de que se ejecute esta parte del archivo.
function clearAssistantLog() {
  for (const message of document.querySelectorAll("#assistant-log .assistant-message")) message.remove();
  document.querySelector("#assistant-log .assistant-empty").hidden = false;
  document.getElementById("assistant-panel").hidden = true;
  document.getElementById("assistant-toggle").setAttribute("aria-expanded", "false");
  document.getElementById("assistant-toggle").classList.remove("is-open");
  document.getElementById("assistant-unread").hidden = true;
}

function setAssistantOpen(open) {
  $("assistant-panel").hidden = !open;
  $("assistant-toggle").setAttribute("aria-expanded", String(open));
  $("assistant-toggle").classList.toggle("is-open", open);
  if (open) {
    $("assistant-unread").hidden = true;
    $("assistant-log").scrollTop = $("assistant-log").scrollHeight;
    $("assistant-input").focus();
  }
}

// Texto del modelo sin HTML: párrafos, listas con "-" o "*" y **negritas**.
function assistantText(text) {
  const container = document.createElement("div");
  container.className = "assistant-text";
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    const item = /^[-*•]\s+(.*)$/.exec(line);
    const block = document.createElement(item ? "li" : "p");
    (item ? item[1] : line).split(/\*\*(.+?)\*\*/).forEach((part, index) => {
      if (!part) return;
      if (index % 2) { const strong = document.createElement("strong"); strong.textContent = part; block.append(strong); }
      else block.append(part.replace(/^#+\s*/, ""));
    });
    if (item) {
      if (!list) { list = document.createElement("ul"); container.append(list); }
      list.append(block);
    } else {
      list = null;
      container.append(block);
    }
  }
  return container;
}

function addAssistantMessage(kind, text, meta = "") {
  const log = $("assistant-log");
  log.querySelector(".assistant-empty").hidden = true;
  const message = document.createElement("article");
  message.className = `assistant-message ${kind}`;
  const label = document.createElement("span");
  label.className = "assistant-who";
  label.textContent = kind === "user" ? "Tú" : "Asistente";
  message.append(label);
  if (kind === "bot") message.append(assistantText(text));
  else { const body = document.createElement("p"); body.textContent = text; message.append(body); }
  if (meta) { const small = document.createElement("small"); small.textContent = meta; message.append(small); }
  log.append(message);
  log.scrollTop = log.scrollHeight;
  return message;
}

function setAssistantStatus(text, tone) {
  const status = $("assistant-status");
  status.textContent = text;
  status.className = `assistant-state ${tone}`;
}

async function askAssistant(rawQuestion) {
  const question = rawQuestion.replace(/\s+/g, " ").trim();
  if (assistantBusy || !question) return;
  if (question.length > ASSISTANT_MAX) {
    setAssistantStatus(`Máximo ${ASSISTANT_MAX} caracteres`, "warning");
    return;
  }
  assistantBusy = true;
  $("assistant-send").disabled = true;
  $("assistant-input").value = "";
  updateAssistantCount();
  setAssistantStatus("Pensando…", "info");
  addAssistantMessage("user", question);
  const waiting = addAssistantMessage("pending", "Revisando los datos de la flota…");
  try {
    const payload = await api("/api/chat", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({pregunta: question}), timeoutMs: 145000,
    });
    waiting.remove();
    addAssistantMessage("bot", payload.respuesta, `Datos al ${payload.datos_al} · ${payload.modelo}`);
    setAssistantStatus("Listo", "neutral");
    if ($("assistant-panel").hidden) $("assistant-unread").hidden = false;  // llegó con la ventana minimizada
  } catch (error) {
    waiting.remove();
    if (!activeToken) return;  // la sesión se cerró: logout() ya limpió la conversación
    addAssistantMessage("error", error.message);
    setAssistantStatus("No disponible", "warning");
  } finally {
    assistantBusy = false;
    $("assistant-send").disabled = false;
  }
}

function updateAssistantCount() {
  const input = $("assistant-input");
  $("assistant-count").textContent = `${input.value.length} / ${ASSISTANT_MAX}`;
  // El cuadro crece con el texto hasta un tope (después aparece la barra de desplazamiento).
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
}

$("assistant-toggle").addEventListener("click", () => setAssistantOpen($("assistant-panel").hidden));
$("assistant-close").addEventListener("click", () => { setAssistantOpen(false); $("assistant-toggle").focus(); });
$("assistant-panel").addEventListener("keydown", (event) => {
  if (event.key === "Escape") { setAssistantOpen(false); $("assistant-toggle").focus(); }
});

$("assistant-form").addEventListener("submit", (event) => {
  event.preventDefault();
  askAssistant($("assistant-input").value);
});
$("assistant-input").addEventListener("input", updateAssistantCount);
$("assistant-input").addEventListener("keydown", (event) => {
  // Enter envía; Shift+Enter agrega una línea.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("assistant-form").requestSubmit();
  }
});
$("assistant-clear").addEventListener("click", () => {
  clearAssistantLog();
  setAssistantStatus("Listo", "neutral");
  setAssistantOpen(true);
});
for (const button of document.querySelectorAll("[data-assistant-question]")) {
  button.addEventListener("click", () => askAssistant(button.dataset.assistantQuestion));
}

restoreSession();
setInterval(refresh, 300000);
setInterval(refreshMapVehicles, 60000);
// Los últimos 7 días avanzan con la hora real: se recalculan cada minuto aunque no lleguen datos.
setInterval(() => { if (!document.hidden) loadVigilance(); }, VG_REFRESH_MS);
setInterval(refreshTelemetry, TELEMETRY_REFRESH_MS);
// Al recuperar la red o volver a la pestaña, actualizar de inmediato sin esperar el minuto.
window.addEventListener("online", () => { refreshTelemetry(); refreshMapVehicles(); loadVigilance(); refresh(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshTelemetry();
    refreshMapVehicles();
    loadVigilance();
    refresh();
  }
});
