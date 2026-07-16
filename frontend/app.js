// Scenic Route Planner — simplified frontend with Google-Maps-style route switching.
const map = L.map("map", { zoomControl: true }).setView([54.5, -3.2], 6);

// Standard road basemap (Google-Maps-like), with satellite as an optional layer.
const roadLayer = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
  { subdomains: "abcd", maxZoom: 20,
    attribution: "© OpenStreetMap contributors © CARTO · Roads © OSRM" }
).addTo(map);
const satLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 18, attribution: "Imagery © Esri · Roads © OSRM/OSM" }
);
L.control.layers({ "Roads": roadLayer, "Satellite": satLayer }, null,
  { position: "topright",
    collapsed: !(window.matchMedia && window.matchMedia("(min-width: 641px) and (pointer: fine)").matches)
  }).addTo(map);

const $ = id => document.getElementById(id);
const HOST_KEY_STORAGE = "scenic_host_key";
let _publicMode = false;

function getHostKey() {
  const el = $("hostKey");
  if (el && el.value) return el.value.trim();
  return (sessionStorage.getItem(HOST_KEY_STORAGE) || "").trim();
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  const key = getHostKey();
  if (key) h["X-API-Key"] = key;
  return h;
}

/** Append api_key for EventSource (cannot set headers). */
function withApiKey(url) {
  const key = getHostKey();
  if (!key) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}api_key=${encodeURIComponent(key)}`;
}

async function apiFetch(url, opts) {
  const o = Object.assign({}, opts || {});
  o.headers = authHeaders(o.headers);
  return fetch(url, o);
}

async function initHostKeyGate() {
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    _publicMode = !!d.public_mode;
  } catch {
    _publicMode = false;
  }
  const row = $("hostKeyRow");
  const input = $("hostKey");
  if (!row || !input) return;
  if (!_publicMode) {
    row.style.display = "none";
    return;
  }
  row.style.display = "block";
  const saved = sessionStorage.getItem(HOST_KEY_STORAGE) || "";
  if (saved) input.value = saved;
  input.addEventListener("change", () => {
    sessionStorage.setItem(HOST_KEY_STORAGE, input.value.trim());
  });
  input.addEventListener("blur", () => {
    sessionStorage.setItem(HOST_KEY_STORAGE, input.value.trim());
  });
}

const routeLayer = L.layerGroup().addTo(map);
const previewLayer = L.layerGroup().addTo(map);  // live candidates during search
const fieldLayer = L.layerGroup().addTo(map);    // scenic grid cells during field build
const drawLayer = L.layerGroup().addTo(map);     // sketch vertices + preview polyline
const attractorLayer = L.layerGroup().addTo(map);  // hotspots / explore attractors

function routingMode() {
  const el = document.querySelector('input[name="routingMode"]:checked');
  return el ? el.value : "road";
}

/** @type {Map<string, L.Rectangle>} */
const _fieldCellLayers = new Map();

function clearFieldLayer() {
  fieldLayer.clearLayers();
  _fieldCellLayers.clear();
}

function fieldCellColor(ev) {
  // Proxy/unknown cells are estimates — do not paint them with the scenic
  // brown→green scale (flat terrain alone used to look like a dull band).
  if (ev && (ev.source === "proxy" || ev.source === "unknown")) {
    return "rgb(110,118,128)";
  }
  return scoreColor(ev && ev.score);
}

function addFieldCell(ev) {
  if (!ev || ev.lat == null || ev.lng == null) return;
  const deg = ev.cell_deg || 0.015;
  const half = deg / 2;
  const bounds = [[ev.lat - half, ev.lng - half], [ev.lat + half, ev.lng + half]];
  const key = `${Number(ev.lat).toFixed(4)},${Number(ev.lng).toFixed(4)}`;
  const prev = _fieldCellLayers.get(key);
  if (prev) fieldLayer.removeLayer(prev);
  const isProxy = ev.source === "proxy" || ev.source === "unknown";
  const rect = L.rectangle(bounds, {
    color: fieldCellColor(ev),
    weight: 0,
    fillColor: fieldCellColor(ev),
    fillOpacity: isProxy ? 0.16 : 0.38,
    interactive: false,
  }).addTo(fieldLayer);
  _fieldCellLayers.set(key, rect);
}

function syncCompareButtons() {
  const field = routingMode() === "field";
  $("compareBtn").textContent = field ? "Compare road vs field" : "Compare fastest vs scenic";
  const hm = $("fieldHeatmapRow");
  if (hm) hm.style.display = field ? "flex" : "none";
}
document.querySelectorAll('input[name="routingMode"]').forEach(el => {
  el.addEventListener("change", syncCompareButtons);
});
syncCompareButtons();

let A = null, B = null, markerA = null, markerB = null;
let vias = [];           // [{latlng, marker}, …] must-pass stops between A and B
let drawMode = false;
let drawVertices = [];   // [{latlng, marker}, …] user sketch for draw route mode
let allRoutes = [];      // route summaries from the API
let selectedIdx = 0;     // which route is currently selected
let suppressMapClick = false;  // set when a route line is clicked, so A/B don't reset
let placeMode = null;    // null = explore freely; 'A' | 'B' | 'via' = next click sets that
const fmt = ll => ll.lat.toFixed(4) + ", " + ll.lng.toFixed(4);
const escapeHtml = s => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const setStats = h => $("stats").innerHTML = h;

// Curated, easy-to-understand scenery styles (map to real backend profiles).
const STYLES = [
  { id: "balanced",                    name: "🌍 A bit of everything" },
  { id: "coastal-moderate-landcover",  name: "🌊 Coast & sea" },
  { id: "mountain-moderate-terrain",   name: "⛰️ Mountains & hills" },
  { id: "waterside-moderate-colour",   name: "🏞️ Lakes & rivers" },
  { id: "woodland-moderate-landcover", name: "🌲 Forests & woodland" },
  { id: "pastoral-moderate-landcover", name: "🌾 Countryside & villages" },
];

function scoreColor(s) {
  // Steeper scenic palette: mid scores stay cool/olive; only true highs go green.
  // Pivot ~68 so towns (~45–62) read browner/greyer than Wye/countryside (~75+).
  const x = Math.max(0, Math.min(100, Number(s) || 0));
  const pivot = 68;
  if (x < pivot) {
    const u = x / pivot; // 0 → brown-grey, 1 → olive
    const r = Math.round(175 - u * 35);   // 175 → 140
    const g = Math.round(95 + u * 55);    // 95 → 150
    const b = Math.round(70 + u * 10);    // 70 → 80
    return `rgb(${r},${g},${b})`;
  }
  const u = (x - pivot) / (100 - pivot); // 0 → olive, 1 → vivid green
  const r = Math.round(140 - u * 100);   // 140 → 40
  const g = Math.round(150 + u * 55);    // 150 → 205
  const b = Math.round(80 - u * 40);     // 80 → 40
  return `rgb(${r},${g},${b})`;
}
const routeColors = ["#4caf50", "#3d8bfd", "#e0894e", "#c05fd6", "#39c0c8", "#d6c24e"];

// ---- Tabs (hidden + focus hygiene) ----
function activateTab(view, { focusTab = false } = {}) {
  document.querySelectorAll(".tab").forEach(t => {
    const on = t.dataset.view === view;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
  });
  document.querySelectorAll(".view").forEach(v => {
    const on = v.id === "view-" + view;
    v.classList.toggle("active", on);
    if (on) v.removeAttribute("hidden");
    else v.setAttribute("hidden", "");
  });
  // Blur focus trapped in a now-hidden panel (e.g. after programmatic tab switch).
  const activeEl = document.activeElement;
  const hiddenView = activeEl && activeEl.closest && activeEl.closest(".view[hidden]");
  if (hiddenView) activeEl.blur();
  if (focusTab) {
    const tab = document.querySelector(`.tab[data-view="${view}"]`);
    if (tab) tab.focus();
  }
  if (view === "saved") { loadSaved(); loadHistory(); }
}
document.querySelectorAll(".tab").forEach(tab => {
  tab.tabIndex = tab.classList.contains("active") ? 0 : -1;
  tab.onclick = () => activateTab(tab.dataset.view, { focusTab: true });
  tab.onkeydown = (e) => {
    const tabs = [...document.querySelectorAll(".tab")];
    const i = tabs.indexOf(tab);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      activateTab(next.dataset.view, { focusTab: true });
    }
  };
});

// ---- Point placement: only when armed, so the map pans/zooms freely ----
function arm(mode) {
  if (mode && drawMode) setDrawMode(false);
  placeMode = mode;
  $("setA").classList.toggle("active", mode === "A");
  $("setB").classList.toggle("active", mode === "B");
  const viaBtn = $("setVia");
  if (viaBtn) viaBtn.classList.toggle("active", mode === "via");
  $("setA").setAttribute("aria-pressed", mode === "A" ? "true" : "false");
  $("setB").setAttribute("aria-pressed", mode === "B" ? "true" : "false");
  if (viaBtn) viaBtn.setAttribute("aria-pressed", mode === "via" ? "true" : "false");
  const peekA = $("peekSetA"), peekB = $("peekSetB");
  if (peekA) {
    peekA.classList.toggle("active", mode === "A");
    peekA.setAttribute("aria-pressed", mode === "A" ? "true" : "false");
  }
  if (peekB) {
    peekB.classList.toggle("active", mode === "B");
    peekB.setAttribute("aria-pressed", mode === "B" ? "true" : "false");
  }
  map.getContainer().style.cursor = mode ? "crosshair" : "";
  if (mode) setMobileSheetMode("peek"); // expose map for the tap
}

function renderViaList() {
  const el = $("viaList");
  if (!el) return;
  if (!vias.length) { el.textContent = ""; return; }
  el.innerHTML = vias.map((v, i) =>
    `Via ${i + 1}: ${fmt(v.latlng)} <button type="button" class="mini secondary" data-vi="${i}">✕</button>`
  ).join(" · ");
  el.querySelectorAll("[data-vi]").forEach(btn => {
    btn.onclick = () => removeVia(+btn.dataset.vi);
  });
}

function addVia(latlng) {
  if (vias.length >= 8) {
    setStats('<span class="err">Maximum 8 via-points.</span>');
    return;
  }
  const m = L.marker(latlng, { draggable: true }).addTo(map)
    .bindTooltip("Via", { permanent: true, direction: "top" });
  const entry = { latlng, marker: m };
  m.on("dragend", () => {
    entry.latlng = m.getLatLng();
    renderViaList();
  });
  vias.push(entry);
  renderViaList();
}

function removeVia(i) {
  const entry = vias[i];
  if (!entry) return;
  map.removeLayer(entry.marker);
  vias.splice(i, 1);
  renderViaList();
}

function clearVias() {
  vias.forEach(v => map.removeLayer(v.marker));
  vias = [];
  renderViaList();
}

function updateDrawButtons() {
  const finish = $("drawFinish");
  const undo = $("drawUndo");
  const finishDock = $("drawFinishDock");
  const undoDock = $("drawUndoDock");
  const disabledFinish = drawVertices.length < 2;
  const disabledUndo = drawVertices.length === 0;
  if (finish) finish.disabled = disabledFinish;
  if (undo) undo.disabled = disabledUndo;
  if (finishDock) finishDock.disabled = disabledFinish;
  if (undoDock) undoDock.disabled = disabledUndo;
}

function clearDrawSketch() {
  drawVertices.forEach(v => { if (v.marker) map.removeLayer(v.marker); });
  drawVertices = [];
  drawLayer.clearLayers();
  updateDrawButtons();
}

function updateDrawPreview() {
  drawLayer.eachLayer(layer => {
    if (layer instanceof L.Polyline) drawLayer.removeLayer(layer);
  });
  if (drawVertices.length >= 2) {
    L.polyline(drawVertices.map(v => v.latlng), {
      color: "#ffd166", weight: 4, opacity: 0.85, dashArray: "8 6",
    }).addTo(drawLayer);
  }
}

function addDrawVertex(latlng) {
  const m = L.circleMarker(latlng, {
    radius: 6, color: "#0b1520", weight: 2,
    fillColor: "#ffd166", fillOpacity: 0.95,
  }).addTo(drawLayer);
  drawVertices.push({ latlng, marker: m });
  updateDrawPreview();
  updateDrawButtons();
}

function setDrawMode(on) {
  drawMode = !!on;
  const toggle = $("drawToggle");
  const controls = $("drawControls");
  const dock = $("drawDock");
  const panel = $("panel");
  if (toggle) {
    toggle.classList.toggle("active", drawMode);
    toggle.setAttribute("aria-pressed", drawMode ? "true" : "false");
  }
  const peekDraw = $("peekDraw");
  if (peekDraw) {
    peekDraw.classList.toggle("active", drawMode);
    peekDraw.setAttribute("aria-pressed", drawMode ? "true" : "false");
  }
  if (controls) controls.style.display = drawMode ? "block" : "none";
  if (dock) dock.hidden = !drawMode;
  if (panel) panel.classList.toggle("drawing", drawMode);
  if (drawMode) {
    arm(null);
    map.getContainer().style.cursor = "crosshair";
    setMobileSheetMode("peek"); // free the map for tapping vertices
  } else if (!placeMode) {
    map.getContainer().style.cursor = "";
  }
  if (!drawMode) clearDrawSketch();
  else updateDrawButtons();
}

function cancelDraw() {
  setDrawMode(false);
  setStats("");
}

async function finishDrawRoute() {
  if (drawVertices.length < 2) return;
  cancelStream();
  const coords = drawVertices.map(v => [v.latlng.lat, v.latlng.lng]);
  clearRoutes();
  busy(true, "Routing your sketch…");
  try {
    const res = await apiFetch("/api/route/draw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        coords,
        profile: $("profile").value,
        snap_to_roads: true,
        time_budget: $("timeBudget").checked,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setStats(`<span class="err">${escapeHtml(err.error || "Draw route failed.")}</span>`);
      busy(false);
      return;
    }
    busy(true, "Scoring…");
    const data = await res.json();
    setDrawMode(false);
    finishDrawn(data);
  } catch {
    setStats('<span class="err">Draw route failed.</span>');
    busy(false);
  }
}

function finishDrawn(data) {
  if (data.from && data.from.length === 2) {
    placeMarker("A", L.latLng(data.from[0], data.from[1]));
  }
  if (data.to && data.to.length === 2) {
    placeMarker("B", L.latLng(data.to[0], data.to[1]));
  }
  finishPlan(data);
}

function viaQuery() {
  return vias.map(v => `&via=${v.latlng.lat.toFixed(5)},${v.latlng.lng.toFixed(5)}`).join("");
}

function placeMarker(which, latlng) {
  const isA = which === "A";
  let m = isA ? markerA : markerB;
  if (m) map.removeLayer(m);
  m = L.marker(latlng, { draggable: true }).addTo(map)
       .bindTooltip(which, { permanent: true, direction: "top" });
  m.on("dragend", () => {
    const p = m.getLatLng();
    if (isA) { A = p; $("ptA").textContent = fmt(p); }
    else { B = p; $("ptB").textContent = fmt(p); }
    syncPeekLabels();
  });
  if (isA) { markerA = m; A = latlng; $("ptA").textContent = fmt(latlng); }
  else { markerB = m; B = latlng; $("ptB").textContent = fmt(latlng); }
  syncPeekLabels();
}

$("setA").onclick = () => arm(placeMode === "A" ? null : "A");
$("setB").onclick = () => arm(placeMode === "B" ? null : "B");
if ($("setVia")) $("setVia").onclick = () => arm(placeMode === "via" ? null : "via");
if ($("clearVias")) $("clearVias").onclick = () => { clearVias(); arm(null); };
if ($("drawToggle")) $("drawToggle").onclick = () => setDrawMode(!drawMode);
if ($("drawUndo")) $("drawUndo").onclick = () => {
  const last = drawVertices.pop();
  if (last && last.marker) map.removeLayer(last.marker);
  updateDrawPreview();
  updateDrawButtons();
};
if ($("drawFinish")) $("drawFinish").onclick = finishDrawRoute;
if ($("drawCancel")) $("drawCancel").onclick = cancelDraw;
if ($("drawUndoDock")) $("drawUndoDock").onclick = () => { if ($("drawUndo")) $("drawUndo").click(); };
if ($("drawFinishDock")) $("drawFinishDock").onclick = () => { if ($("drawFinish")) $("drawFinish").click(); };
if ($("drawCancelDock")) $("drawCancelDock").onclick = () => { if ($("drawCancel")) $("drawCancel").click(); };

// ---- Map click only sets a point when armed; otherwise you explore freely ----
map.on("click", e => {
  if (suppressMapClick) { suppressMapClick = false; return; }
  if (placeMode === "A") {
    clearRoutes();
    placeMarker("A", e.latlng);
    arm(B ? null : "B");          // auto-advance to the end if it isn't set yet
  } else if (placeMode === "B") {
    placeMarker("B", e.latlng);
    arm(null);
  } else if (placeMode === "via") {
    addVia(e.latlng);
    // stay in via mode for multiple stops
  } else if (drawMode) {
    addDrawVertex(e.latlng);
  }
  // else: no armed point -> free pan/zoom/click, nothing is set
});

// ---- Preference slider label ----
const prefLabel = v => v < 0.25 ? "Quickest" : v < 0.5 ? "Fairly direct" : v < 0.75 ? "Balanced" : v < 0.95 ? "Scenic" : "Most scenic";
$("pref").oninput = () => {
  $("prefVal").textContent = prefLabel(+$("pref").value);
  $("pref").setAttribute("aria-valuenow", $("pref").value);
};

// ---- Minimum scenic score slider label ----
$("minScenic").oninput = () => {
  const v = +$("minScenic").value;
  $("minScenicVal").textContent = v === 0 ? "Off" : v + "+";
  $("minScenic").setAttribute("aria-valuenow", String(v));
};

// ---- Draw all routes; selected one is coloured by scenic score, others faint ----
function clearRoutes() {
  routeLayer.clearLayers();
  attractorLayer.clearLayers();
  allRoutes = [];
  $("routeList").innerHTML = "";
  $("directions").innerHTML = "";
  $("actionRow").style.display = "none";
  $("legend").style.display = "none";
}

function drawAttractors(hotspots, attractors) {
  attractorLayer.clearLayers();
  const mk = (pt, color, prefix) => {
    const m = L.circleMarker([pt.lat, pt.lng], {
      radius: 7, color: "#0b1520", weight: 1,
      fillColor: color, fillOpacity: 0.9,
    }).addTo(attractorLayer);
    m.bindTooltip(`${prefix}: ${pt.name || "point"}`, { direction: "top" });
  };
  (hotspots || []).forEach(pt => mk(pt, "#ffd166", "Hotspot"));
  (attractors || []).forEach(pt => mk(pt, "#4caf50", "Attractor"));
}

function drawRoutes(fit) {
  routeLayer.clearLayers();
  // 1) alternatives first (drawn underneath), grey but clickable
  allRoutes.forEach((r, i) => {
    if (i === selectedIdx) return;
    const line = L.polyline(r.render.map(p => [p.lat, p.lng]),
      { color: "#8fa0b3", weight: 5, opacity: .55 }).addTo(routeLayer);
    line.bindTooltip(`Alternative ${i} · scenic ${Math.round(r.avg_scenic_score)} — click to select`, { sticky: true });
    line.on("click", ev => { L.DomEvent.stop(ev); suppressMapClick = true; selectRoute(i, false); });
    line.on("mouseover", () => line.setStyle({ opacity: .85, weight: 6 }));
    line.on("mouseout", () => line.setStyle({ opacity: .55, weight: 5 }));
  });
  // 2) selected route on top: scenic-coloured segments (non-interactive) + a
  //    transparent halo that carries a cursor-following explanation tooltip.
  const sel = allRoutes[selectedIdx];
  const pts = sel.render;
  const halo = L.polyline(pts.map(p => [p.lat, p.lng]),
    { color: "#000", weight: 14, opacity: 0.01 }).addTo(routeLayer);
  halo.bindTooltip("", { sticky: true, direction: "top", className: "why-tip" });
  halo.on("click", ev => { L.DomEvent.stop(ev); suppressMapClick = true; });
  halo.on("mousemove", e => halo.setTooltipContent(pointTooltip(nearestPoint(e.latlng, pts))));
  for (let i = 1; i < pts.length; i++)
    L.polyline([[pts[i-1].lat, pts[i-1].lng], [pts[i].lat, pts[i].lng]],
      { color: scoreColor(pts[i].score), weight: 7, opacity: .95, interactive: false }).addTo(routeLayer);
  if (fit) map.fitBounds(L.latLngBounds(pts.map(p => [p.lat, p.lng])).pad(.15));
}

// ---- Hover explainability: nearest point + why-it-scores tooltip ----
function nearestPoint(latlng, pts) {
  let best = pts[0], bestD = Infinity;
  for (const p of pts) {
    const dLat = p.lat - latlng.lat, dLng = (p.lng - latlng.lng) * Math.cos(latlng.lat * Math.PI/180);
    const d = dLat*dLat + dLng*dLng;
    if (d < bestD) { bestD = d; best = p; }
  }
  return best;
}
function bar(label, v) {
  if (v == null) return "";
  const col = scoreColor(v), w = Math.max(3, Math.round(v));
  return `<div style="display:flex;align-items:center;gap:6px;margin-top:2px">
    <span style="width:52px;color:#c8d3df">${label}</span>
    <span style="flex:1;background:#33414f;border-radius:4px;height:7px;overflow:hidden">
      <span style="display:block;height:7px;width:${w}%;background:${col}"></span></span>
    <span style="width:22px;text-align:right">${Math.round(v)}</span></div>`;
}
function pointTooltip(p) {
  const elev = p.elev_m != null ? ` · ${p.elev_m} m` : "";
  return `<div style="min-width:190px;font-size:12px;line-height:1.35">
    <div style="font-weight:700;margin-bottom:3px">${p.reason || ("Scenic score " + Math.round(p.score))}</div>
    <div style="color:#9fb0c3">Overall <b style="color:#eaeef3">${Math.round(p.score)}/100</b>${elev}</div>
    ${bar("Colour", p.colour)}${bar("Terrain", p.terrain)}${bar("Land cover", p.landcover)}
  </div>`;
}

// ---- Route option cards ----
function routeCardTitle(r, i) {
  if (r._label) return r._label;
  if (_lastPlanner === "compare_field") {
    return i === 0 ? "Road-first recommended" : "Field-first";
  }
  return i === 0
    ? (_lastPlanner === "field" ? "Scenic field route" : "Recommended")
    : "Alternative " + i;
}

function legBudgetExhausted(r) {
  return r._budgetExhausted != null ? r._budgetExhausted : _lastBudgetExhausted;
}

function legBudgetReasons(r) {
  return r._budgetReasons != null ? r._budgetReasons : _lastBudgetReasons;
}

function legSignals(r) {
  return r._signals != null ? r._signals : _lastSignals;
}

function motorwayAvoidReason(r) {
  if (r.motorway_avoid_reason != null) return r.motorway_avoid_reason;
  const sig = legSignals(r) || {};
  return sig.motorway_avoid_reason || null;
}

function legFieldMeta(r, i) {
  if (r._fieldMeta) return r._fieldMeta;
  if (_lastPlanner === "compare_field" && i === 1) return _lastFieldMeta;
  if (_lastPlanner === "field") return _lastFieldMeta;
  return null;
}

function renderRouteList() {
  const banner = (_minScenic > 0 && !_minScenicMet)
    ? `<div class="ropt" style="border-color:#e0a24a;background:#3a2f1a;cursor:default">
         <div class="body"><div class="title" style="color:#f0c27a">Target ${_minScenic}+ not reachable</div>
         <div class="meta">Showing the most scenic route available.</div></div></div>`
    : "";
  $("routeList").innerHTML = banner + allRoutes.map((r, i) => {
    const mins = Math.round(r.duration_min);
    const time = mins >= 60 ? `${Math.floor(mins/60)}h ${mins%60}m` : `${mins} min`;
    const mw = r.motorway_km > 0.1 ? ` · 🛣️ ${r.motorway_km} km motorway` : " · no motorways";
    const mwAvoid = r.avoid_motorways && r.motorway_avoid_met === false
      ? " · <span style=\"color:#ff9a9a\">motorway avoid not met</span>"
      : "";
    const meets = _minScenic > 0 ? (r.meets_min ? "#4caf72" : "#8a94a0") : routeColors[i % routeColors.length];
    const tick = _minScenic > 0 ? (r.meets_min ? " ✓" : "") : "";
    const est = r.colour_scored === false ? " <span style=\"color:var(--dim);font-weight:400\">(estimate)</span>" : "";
    return `<div class="ropt ${i===selectedIdx?"sel":""}" onclick="selectRoute(${i}, false)">
      <div class="swatch" style="background:${routeColors[i % routeColors.length]}"></div>
      <div class="body">
        <div class="title">${routeCardTitle(r, i)}</div>
        <div class="meta">${r.distance_km} km · ${time}${mw}${mwAvoid}</div>
      </div>
      <div class="badge" style="background:${meets}">${Math.round(r.avg_scenic_score)}${tick}${est}</div>
    </div>`;
  }).join("");
}

// ---- Turn-by-turn directions ----
function renderDirections() {
  const r = allRoutes[selectedIdx];
  const steps = r.directions || [];
  if (!steps.length) { $("directions").innerHTML = ""; return; }
  const mins = Math.round(r.duration_min);
  const time = mins >= 60 ? `${Math.floor(mins/60)}h ${mins%60}m` : `${mins} min`;
  $("directions").innerHTML =
    `<div class="dirhead"><span>Directions</span><span style="color:var(--dim)">${r.distance_km} km · ${time}</span></div>` +
    steps.map((s, n) => `
      <div class="dstep" onclick="panTo(${s.lat},${s.lng})">
        <div class="n">${n+1}</div>
        <div class="t">${s.text}</div>
        <div class="d">${s.distance_label}</div>
      </div>`).join("");
}
window.panTo = (lat, lng) => { if (lat != null && lng != null) map.panTo([lat, lng]); };

// ---- Select a route (from list or map) ----
window.selectRoute = (i, fit) => {
  if (i < 0 || i >= allRoutes.length) return;
  selectedIdx = i;
  drawRoutes(fit);
  renderRouteList();
  renderDirections();
  const r = allRoutes[i], cp = r.components || {};
  const sig = legSignals(r) || {};
  let mwLine = r.motorway_km > 0.1
    ? `<br><span style="color:#ff9a9a">${r.motorway_km} km on motorways</span>`
    : `<br><span style="color:var(--accent)">No motorways</span>`;
  if (r.avoid_motorways && r.motorway_avoid_met === false) {
    const why = motorwayAvoidReason(r) === "all_candidates_include_motorways"
      ? "No motorway-free route was found in the candidate pool"
      : "Motorway avoid requested but not met on this leg";
    mwLine += `<br><span style="color:#ff9a9a">${why}</span>`;
  }
  const notes = [];
  if (legBudgetExhausted(r)) {
    const reasons = (legBudgetReasons(r) || []).filter(Boolean);
    // Soft-degrade copy: friends should not read budget stops as "app broken".
    if (reasons.includes("landcover_truncated") || sig.landcover_incomplete) {
      notes.push("Search used partial map context (corridor still warming)");
    } else {
      const why = reasons.length ? ` (${reasons.join(", ")})` : "";
      notes.push(`Search stopped early under the time budget${why}`);
    }
  }
  if (_minScenic > 0 && !_minScenicMet) {
    notes.push(`Scenic target ${_minScenic} not met (best ${Math.round(r.avg_scenic_score)})`);
  }
  if (sig.landcover_incomplete) {
    notes.push("Map context still warming for this corridor (partial coverage — not broken)");
  } else if (sig.landcover === false) {
    notes.push("Map context unavailable (weights renormalised)");
  } else if (sig.terrain === false) {
    notes.push("Terrain unavailable (weights renormalised)");
  }
  if (r.avoid_motorways && r.motorway_avoid_met === false && motorwayAvoidReason(r) === "all_candidates_include_motorways") {
    notes.push("No motorway-free road candidate was found, so this is the least-motorway option available");
  }
  let noteLine = "";
  if (notes.length) {
    noteLine = `<br><span style="color:#f0c27a">${escapeHtml(notes.join(" · "))}</span>`;
  }
  if (sig.climate) {
    const label = sig.climate_name || sig.climate;
    noteLine += `<br><span style="color:#9fb0c3">Colour climate: ${escapeHtml(label)}</span>`;
  }
  if (sig.climates_used && sig.climates_used.length > 1) {
    noteLine += `<br><span style="color:#9fb0c3">Route crosses multiple scenic climates: ${escapeHtml(sig.climates_used.join(", "))}</span>`;
  }
  const divNames = (_lastAttractors || []).map(a => a.name).filter(Boolean);
  if (divNames.length) {
    noteLine += `<br><span style="color:var(--accent)">Attractors: ${escapeHtml(divNames.join(", "))}</span>`;
  } else if ((_lastHotspots || []).length) {
    noteLine += `<br><span style="color:#9fb0c3">${_lastHotspots.length} scenic hotspot diversion${_lastHotspots.length === 1 ? "" : "s"} on map</span>`;
  }
  const estNote = r.colour_scored === false
    ? `<br><span style="color:var(--dim)">Scenic score is a terrain/map estimate (colour pending)</span>`
    : "";
  let fieldNote = "";
  const fm = legFieldMeta(r, i);
  if (fm && (_lastPlanner === "field" || (_lastPlanner === "compare_field" && i === 1))) {
    const nCorr = (fm.green_corridors && fm.green_corridors.length) || 0;
    const nCand = fm.candidates_tried_n ?? (fm.candidates_tried && fm.candidates_tried.length) ?? 0;
    fieldNote = `<br><span style="color:#9fb0c3">Scenic field · lattice avg ${fm.lattice_avg_scenic} → road ${Math.round(r.avg_scenic_score)}`
      + ` · Δ ${fm.snap_delta_scenic ?? "–"} · ${nCorr} corridors · ${nCand} OSRM candidates`
      + ` (${fm.cells_scored} cells)</span>`;
    const fieldNotes = [];
    if (fm.heatmap_proxy_only || (fm.proxy_cells > 0 && fm.colour_cells < fm.cells_scored)) {
      fieldNotes.push("Heatmap used terrain/map estimates (colour budget or cold corridor)");
    }
    const fbr = fm.budget_reasons || [];
    if (fbr.includes("landcover_unavailable")) {
      fieldNotes.push("Map context unavailable — scenic colour only (no woodland/urban map layer)");
    } else if (fbr.includes("landcover_truncated")) {
      const nFeat = fm.landcover_features;
      fieldNotes.push(
        nFeat > 0
          ? `Partial map context (${nFeat} features, ${fm.landcover_tiles_ok ?? "?"}/${fm.landcover_tiles_requested ?? "?"} tiles)`
          : "Partial map context for the field corridor"
      );
    } else if (fm.landcover_usable) {
      fieldNotes.push(`Map context used (${fm.landcover_features ?? 0} features)`);
    }
    if (fbr.includes("colour_budget")) {
      fieldNotes.push("Colour sampling stopped under the time budget");
    } else if (fbr.includes("osrm_budget")) {
      fieldNotes.push("OSRM candidate search stopped under the time budget");
    }
    const cr = fm.chosen_reason || "";
    if (cr.includes("avoid_motorways_unmet")) {
      fieldNotes.push("No motorway-free field candidate — showing least-motorway option");
    } else if (cr.includes("snap_fallback")) {
      fieldNotes.push("Used direct snap fallback when corridor OSRM failed");
    }
    if (fieldNotes.length) {
      fieldNote += `<br><span style="color:#f0c27a">${escapeHtml(fieldNotes.join(" · "))}</span>`;
    }
  }
  setStats(`<b>${routeCardTitle(r, i)}</b><br>
    <span style="color:var(--dim)">Scenic score:</span> <b>${Math.round(r.avg_scenic_score)}/100</b>
    &nbsp;·&nbsp; ${r.distance_km} km &nbsp;·&nbsp; ${Math.round(r.duration_min)} min<br>
    <span style="color:var(--dim)">Landscape blend:</span> colour ${cp.colour ?? "–"} · terrain ${cp.terrain ?? "–"} · land cover ${cp.landcover ?? "–"}${mwLine}${noteLine}${fieldNote}${estNote}`);
};

function busy(on, msg) {
  $("routeBtn").disabled = on;
  if ($("routeBtnSheet")) $("routeBtnSheet").disabled = on;
  $("compareBtn").disabled = on;
  if ($("compareFieldBtn")) $("compareFieldBtn").disabled = on;
  if ($("drawToggle")) $("drawToggle").disabled = on;
  if (on) {
    setStats('<span class="spin">⏳ ' + msg + '</span>');
  }
}

/** Cancel an in-flight SSE plan so compare / reset do not race the stream. */
function cancelStream() {
  if (_es) {
    _es.close();
    _es = null;
  }
  $("searchProgress").style.display = "none";
}


// ---- Plan (live streaming search) ----
let _es = null;                // active EventSource
let _previewLines = [];        // candidate polylines drawn during the search
let _searchStats = { scored: 0, best: 0 };

function clearPreview() {
  previewLayer.clearLayers();
  _previewLines = [];
  _searchStats = { scored: 0, best: 0 };
}

// Draw one candidate route as it is discovered, coloured by its scenic score.
function addPreview(ev) {
  const latlngs = ev.coords.map(c => [c[0], c[1]]);
  const line = L.polyline(latlngs, {
    color: scoreColor(ev.scenic), weight: 3,
    opacity: ev.meets_min ? 0.9 : 0.5,
    dashArray: ev.kind === "expanded" ? "4 5" : null,
  }).addTo(previewLayer);
  // subtle "draw-in" pulse
  line.setStyle({ weight: 5 });
  setTimeout(() => { try { line.setStyle({ weight: 3 }); } catch (e) {} }, 260);
  _previewLines.push(line);
  _searchStats.scored += 1;
  _searchStats.best = Math.max(_searchStats.best, Math.round(ev.scenic));
}

function searchLog(html) { $("searchLog").innerHTML = html; }

function planRoute() {
  if (!A || !B) {
    setMobileSheetMode("half");
    return setStats('<span class="err">Set both a start (A) and end (B) first.</span>');
  }
  cancelStream();
  setDrawMode(false);
  clearRoutes(); clearPreview(); clearFieldLayer();
  setMobileSheetMode("half"); // show live search progress
  busy(true, routingMode() === "field" ? "Building scenic field…" : "Searching…");
  $("searchProgress").style.display = "block";
  searchLog('<div class="sl-phase">Starting search…</div>');

  const field = routingMode() === "field";
  const heatmap = field && $("fieldHeatmap") && $("fieldHeatmap").checked;
  const base = field ? "/api/route/field/stream" : "/api/route/stream";
  const u = withApiKey(
    `${base}?from_lat=${A.lat}&from_lng=${A.lng}&to_lat=${B.lat}&to_lng=${B.lng}`
    + `&preference=${$("pref").value}&profile=${$("profile").value}`
    + `&avoid_motorways=${$("avoidMw").checked}`
    + (field ? "" : `&min_scenic=${$("minScenic").value}`
      + `&explore_all=${$("exploreAll").checked}`)
    + `&time_budget=${$("timeBudget").checked}`
    + (field && heatmap ? "&include_grid=false" : "") + viaQuery()
  );

  const target = +$("minScenic").value;
  const es = new EventSource(u);
  _es = es;

  es.onmessage = (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    switch (ev.type) {
      case "start":
        break;
      case "phase":
        searchLog(
          `<div class="sl-phase">${ev.cold_corridor ? "🗺️" : field ? "🧭" : "🔎"} ${ev.label}</div>` +
          `<div class="sl-stat">${field ? "Scenic field planner" : _searchStats.scored + " routes explored · best so far <b>" + _searchStats.best + "</b>"}</div>`);
        break;
      case "cell":
        if (heatmap) addFieldCell(ev);
        break;
      case "landcover": {
        const warming = ev.cold || (ev.done < ev.total);
        searchLog(
          `<div class="sl-phase">🗺️ ${warming ? "Warming map context for this corridor…" : "Reading land cover…"}</div>` +
          `<div class="sl-stat">${ev.done}/${ev.total} map tiles loaded` +
          (warming ? " — first visit here is slower; later plans reuse the cache" : "") +
          `</div>`);
        break;
      }
      case "candidate":
        addPreview(ev);
        searchLog(
          `<div class="sl-phase">🔎 Exploring routes…</div>` +
          `<div class="sl-stat">${_searchStats.scored} routes explored · best so far <b>${_searchStats.best}</b>` +
          (target ? ` · target <b>${target}</b>` : "") + `</div>`);
        break;
      case "round":
        searchLog(
          `<div class="sl-phase" style="color:#f0c27a">↔️ ${ev.label}</div>` +
          `<div class="sl-stat">${_searchStats.scored} routes explored · best so far <b>${_searchStats.best}</b> · target <b>${ev.target}</b></div>`);
        break;
      case "done":
        es.close(); _es = null;
        finishPlan(ev.result);
        break;
      case "error":
        es.close(); _es = null;
        $("searchProgress").style.display = "none";
        setStats(`<span class="err">${ev.message || "Routing failed."}</span>`);
        busy(false);
        break;
        break;
    }
  };
  es.onerror = () => {
    if (!_es) return;              // already closed cleanly
    es.close(); _es = null;
    $("searchProgress").style.display = "none";
    setStats('<span class="err">Search connection lost — if others are planning routes, wait a moment and try again.</span>');
    busy(false);
  };
}

// Render the final chosen + alternative routes once the search completes.
// Preserve the server ranking order; only promote the chosen route to index 0
// so the UI "Recommended" card matches the planner (do not re-sort by scenic).
function finishPlan(data) {
  clearPreview();
  // Keep scenic heatmap overlay after a field build so green vs dull can be judged
  // against the chosen road. Cleared on the next plan / compare / reset.
  if (routingMode() !== "field" || !($("fieldHeatmap") && $("fieldHeatmap").checked)) {
    clearFieldLayer();
  }
  $("searchProgress").style.display = "none";
  const alts = (data.alternatives || []).slice();
  const ci = alts.findIndex(r => r.chosen);
  if (ci > 0) {
    const [ch] = alts.splice(ci, 1);
    alts.unshift(ch);
  }
  allRoutes = alts;
  selectedIdx = 0;
  _minScenic = data.min_scenic || 0;
  _minScenicMet = data.min_scenic_met !== false;
  _lastSignals = data.signals || null;
  _lastBudgetExhausted = !!data.budget_exhausted;
  _lastBudgetReasons = data.budget_reasons || [];
  _lastFieldMeta = data.field_meta || null;
  _lastPlanner = data.planner || data.source || "road";
  $("actionRow").style.display = "flex";
  $("legend").style.display = "block";
  _lastFrom = data.from; _lastTo = data.to; _lastProfile = data.profile; _lastPref = data.preference;
  _lastHotspots = data.hotspots || [];
  _lastAttractors = data.attractors_used || [];
  drawAttractors(_lastHotspots, _lastAttractors);
  drawRoutes(true); renderRouteList(); selectRoute(0, false);
  busy(false);
  setMobileSheetMode("half"); // show route cards / directions
  if (isMobileSheet()) {
    requestAnimationFrame(() => {
      const list = $("routeList");
      if (list) list.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }
  syncUrlFromState(false);
}
let _lastFrom = null, _lastTo = null, _lastProfile = null, _lastPref = null;
let _minScenic = 0, _minScenicMet = true;
let _lastSignals = null;
let _lastBudgetExhausted = false;
let _lastBudgetReasons = [];
let _lastHotspots = [];
let _lastAttractors = [];
let _lastFieldMeta = null;
let _lastPlanner = "road";

// ---- Place search: multi-hit picker; sets A if unset, else B if unset ----
function hideGeocodeHits() {
  const el = $("geocodeHits");
  if (el) { el.style.display = "none"; el.innerHTML = ""; }
}

function applyGeocodeHit(hit) {
  hideGeocodeHits();
  const latlng = L.latLng(+hit.lat, +hit.lng);
  map.setView(latlng, 12);
  const label = hit.display_name || hit.name || "Place";
  if (!A) {
    clearRoutes();
    placeMarker("A", latlng);
    arm(B ? null : "B");
    setStats(`<b>Start set:</b> ${escapeHtml(label)}`);
  } else if (!B) {
    placeMarker("B", latlng);
    arm(null);
    setStats(`<b>End set:</b> ${escapeHtml(label)}`);
  } else {
    setStats(`<b>Found:</b> ${escapeHtml(label)} — use Set start/end to place.`);
  }
}

async function geocode() {
  const q = $("q").value.trim(); if (!q) return;
  hideGeocodeHits();
  const res = await apiFetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  if (!res.ok) return setStats('<span class="err">Place search failed.</span>');
  const data = await res.json();
  const hits = data.results || [];
  if (!hits.length) return setStats('<span class="err">Place not found.</span>');
  if (hits.length === 1) {
    applyGeocodeHit(hits[0]);
    return;
  }
  setMobileSheetMode("half");
  const box = $("geocodeHits");
  box.style.display = "block";
  box.innerHTML = hits.map((h, i) =>
    `<button type="button" class="ghit" role="option" data-i="${i}">${escapeHtml(h.display_name || h.name)}</button>`
  ).join("");
  box.querySelectorAll(".ghit").forEach(btn => {
    btn.onclick = () => applyGeocodeHit(hits[+btn.dataset.i]);
  });
  setStats(`<b>${hits.length} places found</b> — pick one below.`);
  // Keep search results visible at the top of the sheet.
  const scroll = $("panelScroll");
  if (scroll && isMobileSheet()) scroll.scrollTop = 0;
}

// ---- Save modal ----
let svRating = 0;
$("svStars").querySelectorAll("span").forEach(s => s.onclick = () => {
  svRating = +s.dataset.v;
  $("svStars").querySelectorAll("span").forEach(x => x.classList.toggle("on", +x.dataset.v <= svRating));
});
$("saveBtn").onclick = () => { if (allRoutes.length) $("saveModal").classList.add("open"); };
$("svCancel").onclick = () => $("saveModal").classList.remove("open");
$("svConfirm").onclick = async () => {
  const r = allRoutes[selectedIdx]; if (!r) return;
  const body = {
    name: $("svName").value || "Untitled scenic route",
    notes: $("svNotes").value,
    tags: $("svTags").value.split(",").map(t => t.trim()).filter(Boolean),
    favourite: $("svFav").checked, rating: svRating,
    from_lat: _lastFrom ? _lastFrom[0] : A.lat, from_lng: _lastFrom ? _lastFrom[1] : A.lng,
    to_lat: _lastTo ? _lastTo[0] : B.lat, to_lng: _lastTo ? _lastTo[1] : B.lng,
    preference: _lastPref ?? +$("pref").value, profile: _lastProfile || $("profile").value,
    distance_km: r.distance_km, duration_min: r.duration_min, scenic_score: r.avg_scenic_score,
    geojson: { type: "LineString", coordinates: (r.render || []).map(p => [p.lng, p.lat]) },
  };
  const res = await apiFetch("/api/routes", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) });
  if (res.ok) { $("saveModal").classList.remove("open"); $("svName").value=$("svTags").value=$("svNotes").value=""; svRating=0;
    $("svStars").querySelectorAll("span").forEach(x=>x.classList.remove("on")); setStats("<b>✓ Route saved to My routes.</b>"); }
  else setStats('<span class="err">Save failed.</span>');
};

// ---- Exports ----
async function loadExportFormats() {
  try {
    const r = await apiFetch("/api/export/formats"); const d = await r.json();
    (d.formats || []).forEach(f => { const o=document.createElement("option"); o.value=f.id; o.textContent="Export "+(f.name||f.id).toUpperCase(); $("exportFmt").appendChild(o); });
  } catch {}
}
$("exportFmt").onchange = async () => {
  const fmtId = $("exportFmt").value; $("exportFmt").value = "";
  const r = allRoutes[selectedIdx]; if (!fmtId || !r) return;
  const coordinates = (r.render || []).map(p => [p.lng, p.lat]);
  const res = await apiFetch(`/api/export/${fmtId}`, { method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ name: "scenic-route", coordinates }) });
  if (!res.ok) return setStats('<span class="err">Export failed.</span>');
  const blob = await res.blob(); const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = `scenic-route.${fmtId}`; a.click(); URL.revokeObjectURL(url);
};

// ---- Saved list ----
let favFilter = false;
async function loadSaved() {
  const q = $("savedSearch").value.trim();
  let u = "/api/routes?"; if (favFilter) u += "favourite=true&"; if (q) u += "q=" + encodeURIComponent(q);
  const res = await apiFetch(u); const d = await res.json();
  $("savedList").innerHTML = (d.routes || []).map(r => `
    <div class="card" data-id="${r.id}">
      <h4>${r.favourite ? "★ " : ""}${escapeHtml(r.name)}</h4>
      <div class="meta">${r.distance_km ?? "?"} km · ${r.duration_min ?? "?"} min · scenic ${r.scenic_score ?? "?"} · ${"★".repeat(r.rating||0)}</div>
      ${(r.tags||[]).map(t=>`<span class="chip">${escapeHtml(t)}</span>`).join("")}
      ${r.notes ? `<div class="meta" style="margin-top:4px;">${escapeHtml(r.notes)}</div>` : ""}
      <div class="cardrow">
        <button class="mini secondary" onclick="showSaved(${r.id})">Show on map</button>
        <button class="mini secondary" onclick="toggleFav(${r.id})">★</button>
        <button class="mini secondary" onclick="delSaved(${r.id})">Delete</button>
      </div>
    </div>`).join("") || '<div class="hint">No saved routes yet. Plan one and hit 💾 Save.</div>';
}
window.showSaved = async id => {
  const res = await apiFetch(`/api/routes/${id}`); const r = await res.json();
  clearRoutes();
  if (r.geojson && r.geojson.coordinates) {
    const latlngs = r.geojson.coordinates.map(c => [c[1], c[0]]);
    L.polyline(latlngs, { color:"#4caf50", weight:6, opacity:.95 }).addTo(routeLayer);
    map.fitBounds(L.latLngBounds(latlngs).pad(.15));
    activateTab("plan");
  }
};
window.toggleFav = async id => { await apiFetch(`/api/routes/${id}/favourite`, { method:"POST" }); loadSaved(); };
window.delSaved = async id => { await apiFetch(`/api/routes/${id}`, { method:"DELETE" }); loadSaved(); };
$("reloadSaved").onclick = loadSaved;
$("favOnly").onclick = () => {
  favFilter = !favFilter;
  $("favOnly").classList.toggle("active");
  $("favOnly").setAttribute("aria-pressed", favFilter ? "true" : "false");
  loadSaved();
};
$("savedSearch").oninput = () => loadSaved();

async function loadHistory() {
  try {
    const res = await apiFetch("/api/history?limit=20");
    const d = await res.json();
    const rows = d.history || [];
    $("historyList").innerHTML = rows.map(h => `
      <div class="card">
        <h4>${escapeHtml((h.profile || "route") + "")}</h4>
        <div class="meta">${h.distance_km ?? "?"} km · ${h.duration_min ?? "?"} min · scenic ${h.scenic_score ?? "?"}</div>
        <div class="cardrow">
          <button class="mini secondary history-reuse" type="button"
            data-from-lat="${h.from_lat}" data-from-lng="${h.from_lng}"
            data-to-lat="${h.to_lat}" data-to-lng="${h.to_lng}"
            data-pref="${h.preference ?? 0.6}" data-profile="${escapeHtml(h.profile || "balanced")}">Reuse</button>
        </div>
      </div>`).join("") || '<div class="hint">No recent plans yet.</div>';
    $("historyList").querySelectorAll(".history-reuse").forEach(btn => {
      btn.onclick = () => reuseHistory(
        +btn.dataset.fromLat, +btn.dataset.fromLng,
        +btn.dataset.toLat, +btn.dataset.toLng,
        +btn.dataset.pref, btn.dataset.profile || "balanced",
      );
    });
  } catch {
    $("historyList").innerHTML = '<div class="hint">History unavailable.</div>';
  }
}
function reuseHistory(fromLat, fromLng, toLat, toLng, preference, profile) {
  clearRoutes();
  placeMarker("A", L.latLng(fromLat, fromLng));
  placeMarker("B", L.latLng(toLat, toLng));
  $("pref").value = preference;
  $("pref").oninput();
  if ([...$("profile").options].some(o => o.value === profile)) $("profile").value = profile;
  activateTab("plan", { focusTab: true });
  map.fitBounds(L.latLngBounds([A, B]).pad(0.2));
}
$("reloadHistory").onclick = loadHistory;
$("clearHistory").onclick = async () => {
  await apiFetch("/api/history", { method: "DELETE" });
  loadHistory();
};

// ---- Buttons ----
$("routeBtn").onclick = planRoute;
if ($("routeBtnSheet")) $("routeBtnSheet").onclick = planRoute;
$("searchBtn").onclick = geocode;
$("q").addEventListener("keydown", e => { if (e.key === "Enter") geocode(); });
$("resetBtn").onclick = () => {
  cancelStream();
  busy(false);
  setDrawMode(false);
  A = B = null; clearRoutes(); clearPreview(); clearFieldLayer(); arm(null); _lastSignals = null;
  _lastBudgetExhausted = false; _lastBudgetReasons = [];
  clearVias();
  if (markerA) map.removeLayer(markerA); if (markerB) map.removeLayer(markerB); markerA = markerB = null;
  $("ptA").textContent = "—"; $("ptB").textContent = "—";
  syncPeekLabels();
  setStats("");
};

function compareRoutes(useFieldCompare) {
  if (!A || !B) {
    setMobileSheetMode("half");
    return setStats('<span class="err">Set both a start (A) and end (B) first.</span>');
  }
  const fieldCmp = useFieldCompare || routingMode() === "field";
  cancelStream();
  setDrawMode(false);
  clearRoutes(); clearPreview(); clearFieldLayer();
  setMobileSheetMode("half");
  busy(true, fieldCmp ? "Comparing road vs field…" : "Comparing fastest vs scenic…");
  $("searchProgress").style.display = "block";
  searchLog('<div class="sl-phase">Starting compare…</div>');

  const u = withApiKey(
    `/api/route/compare/stream?from_lat=${A.lat}&from_lng=${A.lng}&to_lat=${B.lat}&to_lng=${B.lng}`
    + `&preference=${$("pref").value}&profile=${encodeURIComponent($("profile").value)}`
    + (fieldCmp ? "&compare_field=1" : "")
    + `&avoid_motorways=${$("avoidMw").checked}`
    + (fieldCmp ? "" : `&min_scenic=${$("minScenic").value}`
      + `&explore_all=${$("exploreAll").checked}`)
    + `&time_budget=${$("timeBudget").checked}` + viaQuery()
  );

  const es = new EventSource(u);
  _es = es;

  es.onmessage = (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    switch (ev.type) {
      case "start":
        break;
      case "phase":
        searchLog(
          `<div class="sl-phase">${ev.cold_corridor ? "🗺️" : "⚖️"} ${ev.label || "Comparing…"}</div>` +
          `<div class="sl-stat">${ev.leg ? "Leg: " + ev.leg : ""}</div>`);
        break;
      case "landcover": {
        const warming = ev.cold || (ev.done < ev.total);
        searchLog(
          `<div class="sl-phase">🗺️ ${warming ? "Warming map context for this corridor…" : "Reading land cover…"}</div>` +
          `<div class="sl-stat">${ev.done}/${ev.total} map tiles` +
          (warming ? " — first visit is slower" : "") +
          (ev.leg ? ` · ${ev.leg}` : "") + `</div>`);
        break;
      }
      case "candidate":
        addPreview(ev);
        searchLog(
          `<div class="sl-phase">🔎 Exploring scenic candidates…</div>` +
          `<div class="sl-stat">${_searchStats.scored} routes · best <b>${_searchStats.best}</b></div>`);
        break;
      case "round":
        searchLog(
          `<div class="sl-phase" style="color:#f0c27a">↔️ ${ev.label}</div>`);
        break;
      case "leg_done":
        searchLog(`<div class="sl-phase">✓ ${ev.leg} leg done</div>`);
        break;
      case "done":
        es.close(); _es = null;
        finishCompare(ev.result);
        break;
      case "error":
        es.close(); _es = null;
        $("searchProgress").style.display = "none";
        setStats(`<span class="err">${ev.message || "Compare failed."}</span>`);
        busy(false);
        break;
    }
  };
  es.onerror = () => {
    if (!_es) return;
    es.close(); _es = null;
    $("searchProgress").style.display = "none";
    setStats('<span class="err">Compare connection lost — if others are planning routes, wait a moment and try again.</span>');
    busy(false);
  };
}

function attachCompareLeg(route, meta, fieldMetaInner) {
  return {
    ...route,
    _budgetExhausted: !!meta.budget_exhausted,
    _budgetReasons: meta.budget_reasons || [],
    _signals: meta.signals || null,
    _fieldMeta: fieldMetaInner || null,
    avoid_motorways: route.avoid_motorways ?? meta.avoid_motorways ?? $("avoidMw").checked,
    motorway_avoid_met: route.motorway_avoid_met ?? meta.motorway_avoid_met,
  };
}

function finishCompare(d) {
  clearPreview();
  clearFieldLayer();
  $("searchProgress").style.display = "none";
  if (d.mode === "compare_field" || d.road) {
    const roadMeta = d.road_meta || {};
    const fieldMetaWrap = d.field_meta || {};
    const road = attachCompareLeg(
      { ...d.road, chosen: true, _label: "Road-first recommended" },
      roadMeta,
      null,
    );
    const field = attachCompareLeg(
      { ...d.field, chosen: false, _label: "Field-first" },
      fieldMetaWrap,
      fieldMetaWrap.field_meta || null,
    );
    allRoutes = [road, field];
    selectedIdx = 0;
    _minScenic = 0;
    _minScenicMet = roadMeta.min_scenic_met !== false;
    _lastSignals = fieldMetaWrap.signals || roadMeta.signals || null;
    _lastBudgetExhausted = !!(roadMeta.budget_exhausted || fieldMetaWrap.budget_exhausted);
    _lastBudgetReasons = [
      ...(roadMeta.budget_reasons || []),
      ...(fieldMetaWrap.budget_reasons || []),
    ];
    _lastFieldMeta = fieldMetaWrap.field_meta || null;
    _lastPlanner = "compare_field";
    _lastFrom = [A.lat, A.lng]; _lastTo = [B.lat, B.lng];
    _lastProfile = $("profile").value; _lastPref = +$("pref").value;
    $("actionRow").style.display = "flex";
    $("legend").style.display = "block";
    drawRoutes(true);
    renderRouteList();
    selectRoute(0, false);
    const lat = _lastFieldMeta ? _lastFieldMeta.lattice_avg_scenic : "–";
    const dKm = field.distance_km - road.distance_km;
    const dMin = field.duration_min - road.duration_min;
    const dScenic = Math.round(field.avg_scenic_score - road.avg_scenic_score);
    let warn = "";
    if (dKm > 30 && dMin > 90 && dScenic <= 2) {
      warn = `<br><span style="color:#f0c27a">Field adds +${Math.round(dKm)} km / +${Math.round(dMin)} min for only +${dScenic} scenic — consider road-first.</span>`;
    }
    setStats(`<b>Road-first vs field-first</b><br>
      Road-first: ${road.distance_km} km · scenic ${Math.round(road.avg_scenic_score)}<br>
      Field-first: ${field.distance_km} km · scenic ${Math.round(field.avg_scenic_score)} · lattice avg ${lat}${warn}`);
    busy(false);
    return;
  }
  const fastest = attachCompareLeg(
    { ...d.fastest, chosen: true, _label: "Fastest" },
    d.fastest_meta || {},
    null,
  );
  const scenic = attachCompareLeg(
    { ...d.scenic, chosen: false, _label: "Most scenic" },
    d.scenic_meta || {},
    null,
  );
  allRoutes = [fastest, scenic];
  selectedIdx = 0;
  const meta = d.scenic_meta || {};
  _minScenic = meta.min_scenic || +$("minScenic").value || 0;
  _minScenicMet = meta.min_scenic_met !== false;
  _lastSignals = meta.signals || null;
  _lastBudgetExhausted = false;
  _lastBudgetReasons = [];
  _lastPlanner = "compare";
  _lastFrom = [A.lat, A.lng]; _lastTo = [B.lat, B.lng];
  _lastProfile = $("profile").value; _lastPref = +$("pref").value;
  $("actionRow").style.display = "flex";
  $("legend").style.display = "block";
  drawRoutes(true);
  renderRouteList();
  selectRoute(0, false);
  setStats(`<b>Fastest vs scenic</b><br>
    Fastest: ${fastest.distance_km} km · ${Math.round(fastest.duration_min)} min · scenic ${Math.round(fastest.avg_scenic_score)}<br>
    Scenic: ${scenic.distance_km} km · ${Math.round(scenic.duration_min)} min · scenic ${Math.round(scenic.avg_scenic_score)}`);
  busy(false);
}
$("compareBtn").onclick = () => compareRoutes(false);
if ($("compareFieldBtn")) $("compareFieldBtn").onclick = () => compareRoutes(true);

// ---- Init: populate scenery dropdown + region jump + presets ----
$("profile").innerHTML = STYLES.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
$("prefVal").textContent = prefLabel(+$("pref").value);
loadExportFormats();

async function loadPresets() {
  try {
    const r = await fetch("/api/presets?featured=true");
    const d = await r.json();
    const sel = $("presetPick");
    (d.presets || []).forEach(p => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.name;
      sel.appendChild(o);
    });
  } catch {}
}
$("presetPick").onchange = async () => {
  const id = $("presetPick").value;
  if (!id) return;
  try {
    const res = await fetch(`/api/presets/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error("preset");
    const p = await res.json();
    clearRoutes();
    placeMarker("A", L.latLng(p.from.lat, p.from.lng));
    placeMarker("B", L.latLng(p.to.lat, p.to.lng));
    arm(null);
    $("pref").value = p.preference ?? 0.7;
    $("pref").oninput();
    if (p.profile && [...$("profile").options].some(o => o.value === p.profile)) {
      $("profile").value = p.profile;
    }
    map.fitBounds(L.latLngBounds([A, B]).pad(0.25));
    setStats(`<b>Preset loaded:</b> ${escapeHtml(p.name)}`);
  } catch {
    setStats('<span class="err">Could not load preset.</span>');
  }
  $("presetPick").value = "";
};
loadPresets();

async function loadRegions() {
  try {
    const r = await fetch("/api/regions");
    const d = await r.json();
    const sel = $("regionJump");
    (d.regions || []).forEach(reg => {
      const o = document.createElement("option");
      o.value = reg.id;
      const scope = reg.scope === "world" ? " · World" : "";
      o.textContent = (reg.name || reg.id) + scope;
      o.dataset.lat = reg.center?.lat ?? "";
      o.dataset.lng = reg.center?.lng ?? "";
      o.dataset.zoom = reg.zoom ?? 10;
      sel.appendChild(o);
    });
  } catch {}
}
$("regionJump").onchange = () => {
  const o = $("regionJump").selectedOptions[0];
  if (!o || !o.value) return;
  const lat = +o.dataset.lat, lng = +o.dataset.lng, zoom = +o.dataset.zoom || 10;
  if (Number.isFinite(lat) && Number.isFinite(lng)) map.setView([lat, lng], zoom);
  $("regionJump").value = "";
};
loadRegions();

// Mobile bottom sheet: peek / half / full snaps; map stays primary by default.
function isMobileSheet() {
  return !!(window.matchMedia && window.matchMedia("(max-width: 640px)").matches);
}

function syncPeekLabels() {
  const a = $("peekLabelA"), b = $("peekLabelB");
  if (a) a.textContent = A ? fmt(A) : "—";
  if (b) b.textContent = B ? fmt(B) : "—";
}

function syncSheetLayout() {
  const panel = $("panel");
  const open = !!(panel && !panel.classList.contains("collapsed") && isMobileSheet());
  document.body.classList.toggle("sheet-open", open);
  document.body.classList.toggle("sheet-full", !!(open && panel && panel.classList.contains("sheet-full")));
  const sizeRow = $("sheetSizeRow");
  if (sizeRow) sizeRow.hidden = !open;
  const halfBtn = $("sheetHalfBtn"), fullBtn = $("sheetFullBtn");
  if (halfBtn && fullBtn && panel) {
    const full = panel.classList.contains("sheet-full");
    halfBtn.classList.toggle("on", open && !full);
    fullBtn.classList.toggle("on", open && full);
    halfBtn.setAttribute("aria-pressed", open && !full ? "true" : "false");
    fullBtn.setAttribute("aria-pressed", open && full ? "true" : "false");
  }
  const toggle = $("panelToggle");
  if (toggle && panel) {
    const collapsed = panel.classList.contains("collapsed");
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggle.textContent = collapsed ? "Show options" : "Hide options";
  }
  // Measure after class changes so Leaflet attribution clears the sheet.
  requestAnimationFrame(() => {
    const h = panel && isMobileSheet() ? panel.getBoundingClientRect().height : 0;
    document.documentElement.style.setProperty("--sheet-h", `${Math.round(h)}px`);
    setTimeout(() => map.invalidateSize({ pan: false }), 60);
  });
}

/** @param {"peek"|"half"|"full"} mode */
function setMobileSheetMode(mode) {
  const panel = $("panel");
  if (!panel || !isMobileSheet()) return;
  if (mode === "peek") {
    panel.classList.add("collapsed");
    panel.classList.remove("sheet-full");
  } else if (mode === "full") {
    panel.classList.remove("collapsed");
    panel.classList.add("sheet-full");
  } else {
    panel.classList.remove("collapsed");
    panel.classList.remove("sheet-full");
  }
  syncSheetLayout();
}

function setMobilePanelCollapsed(collapsed) {
  setMobileSheetMode(collapsed ? "peek" : "half");
}

function syncDisclosureForViewport() {
  const more = $("moreOpts");
  const extra = $("extraActions");
  if (!isMobileSheet()) {
    if (more) more.open = true;
    if (extra) extra.open = true;
  } else {
    if (more) more.open = false;
    if (extra) extra.open = false;
  }
}

function syncVisualViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  document.documentElement.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
  if (isMobileSheet()) syncSheetLayout();
}

(() => {
  const panel = $("panel");
  if (!panel) return;

  const expand = () => setMobileSheetMode("half");
  const collapse = () => setMobileSheetMode("peek");

  if ($("peekExpand")) $("peekExpand").onclick = expand;
  if ($("sheetCollapseBtn")) $("sheetCollapseBtn").onclick = collapse;
  if ($("sheetHideBtn")) $("sheetHideBtn").onclick = collapse;
  if ($("sheetHalfBtn")) $("sheetHalfBtn").onclick = () => setMobileSheetMode("half");
  if ($("sheetFullBtn")) $("sheetFullBtn").onclick = () => setMobileSheetMode("full");

  if ($("peekSetA")) $("peekSetA").onclick = () => arm(placeMode === "A" ? null : "A");
  if ($("peekSetB")) $("peekSetB").onclick = () => arm(placeMode === "B" ? null : "B");
  if ($("peekDraw")) $("peekDraw").onclick = () => setDrawMode(!drawMode);

  const btn = $("panelToggle");
  if (btn) {
    btn.onclick = () => {
      if (!isMobileSheet()) return;
      if (panel.classList.contains("collapsed")) setMobileSheetMode("half");
      else setMobileSheetMode("peek");
    };
  }

  const handle = $("sheetHandle") || panel.querySelector(".sheet-handle");
  if (handle) {
    // Light drag: pull up → full/half, pull down → half/peek.
    let dragY0 = null;
    let sheetDragged = false;
    handle.addEventListener("touchstart", (e) => {
      if (!isMobileSheet() || !e.touches[0]) return;
      dragY0 = e.touches[0].clientY;
      sheetDragged = false;
    }, { passive: true });
    handle.addEventListener("touchend", (e) => {
      if (dragY0 == null || !e.changedTouches[0]) return;
      const dy = e.changedTouches[0].clientY - dragY0;
      dragY0 = null;
      if (Math.abs(dy) < 28) return; // treat as tap (click handler)
      sheetDragged = true;
      if (dy < 0) {
        if (panel.classList.contains("collapsed")) setMobileSheetMode("half");
        else setMobileSheetMode("full");
      } else {
        if (panel.classList.contains("sheet-full")) setMobileSheetMode("half");
        else setMobileSheetMode("peek");
      }
    }, { passive: true });
    const cycleSheet = () => {
      if (!isMobileSheet()) return;
      if (sheetDragged) { sheetDragged = false; return; }
      if (panel.classList.contains("collapsed")) setMobileSheetMode("half");
      else if (panel.classList.contains("sheet-full")) setMobileSheetMode("half");
      else setMobileSheetMode("full");
    };
    handle.addEventListener("click", cycleSheet);
    handle.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); cycleSheet(); }
    });
  }

  syncPeekLabels();
  syncDisclosureForViewport();

  // Default: start collapsed on phones so the map is usable immediately.
  if (isMobileSheet()) setMobileSheetMode("peek");
  else {
    panel.classList.remove("collapsed", "sheet-full");
    document.body.classList.remove("sheet-open", "sheet-full");
    document.documentElement.style.setProperty("--sheet-h", "0px");
  }

  window.addEventListener("resize", () => {
    syncDisclosureForViewport();
    if (!isMobileSheet()) {
      panel.classList.remove("collapsed", "sheet-full");
      document.body.classList.remove("sheet-open", "sheet-full");
      document.documentElement.style.setProperty("--sheet-h", "0px");
      const toggle = $("panelToggle");
      if (toggle) toggle.setAttribute("aria-expanded", "true");
      setTimeout(() => map.invalidateSize({ pan: false }), 80);
    } else {
      syncSheetLayout();
    }
  });

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", syncVisualViewport);
    window.visualViewport.addEventListener("scroll", syncVisualViewport);
    syncVisualViewport();
  } else {
    syncSheetLayout();
  }
})();

// Keyboard shortcuts (documented in /api/features)
document.addEventListener("keydown", e => {
  const tag = (e.target && e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select";
  if (e.key === "Escape") {
    $("saveModal").classList.remove("open");
    if (drawMode) cancelDraw();
    else if (placeMode) arm(null);
    else if (isMobileSheet()) {
      const panel = $("panel");
      if (panel && !panel.classList.contains("collapsed")) setMobileSheetMode("peek");
    }
    return;
  }
  if (typing) return;
  if (e.key === "/" ) { e.preventDefault(); $("q").focus(); }
  else if (e.key === "p" || e.key === "P") planRoute();
  else if (e.key === "s" || e.key === "S") {
    if (allRoutes.length) $("saveModal").classList.add("open");
  }
});

// ---- Shareable deep links (same query shape as the plan API) ----
function syncUrlFromState(autoplan) {
  if (!A || !B) return;
  const p = new URLSearchParams();
  p.set("from_lat", A.lat.toFixed(5));
  p.set("from_lng", A.lng.toFixed(5));
  p.set("to_lat", B.lat.toFixed(5));
  p.set("to_lng", B.lng.toFixed(5));
  p.set("preference", $("pref").value);
  p.set("profile", $("profile").value);
  if ($("avoidMw").checked) p.set("avoid_motorways", "true");
  const ms = +$("minScenic").value;
  if (ms > 0) p.set("min_scenic", String(ms));
  if ($("exploreAll").checked) p.set("explore_all", "true");
  // Default is budget ON; only encode when turned off so shared links stay short.
  if (!$("timeBudget").checked) p.set("time_budget", "false");
  vias.forEach(v => p.append("via", `${v.latlng.lat.toFixed(5)},${v.latlng.lng.toFixed(5)}`));
  if (autoplan) p.set("autoplan", "1");
  const url = `${location.pathname}?${p.toString()}`;
  history.replaceState(null, "", url);
}

function applyDeepLink() {
  const p = new URLSearchParams(location.search);
  const fla = parseFloat(p.get("from_lat"));
  const flo = parseFloat(p.get("from_lng"));
  const tla = parseFloat(p.get("to_lat"));
  const tlo = parseFloat(p.get("to_lng"));
  if (Number.isFinite(fla) && Number.isFinite(flo)) {
    placeMarker("A", L.latLng(fla, flo));
  }
  if (Number.isFinite(tla) && Number.isFinite(tlo)) {
    placeMarker("B", L.latLng(tla, tlo));
  }
  if (A && B) {
    map.fitBounds(L.latLngBounds([A, B]).pad(0.2));
  } else if (A) {
    map.setView(A, 10);
  }
  const pref = p.get("preference");
  if (pref != null && pref !== "") {
    $("pref").value = pref;
    $("pref").oninput();
  }
  const profile = p.get("profile");
  if (profile) {
    const opt = [...$("profile").options].find(o => o.value === profile);
    if (opt) $("profile").value = profile;
  }
  $("avoidMw").checked = ["1", "true", "yes"].includes(String(p.get("avoid_motorways") || "").toLowerCase());
  $("exploreAll").checked = ["1", "true", "yes"].includes(String(p.get("explore_all") || "").toLowerCase());
  // time_budget defaults ON; only false/0/no turns it off.
  const tb = p.get("time_budget");
  $("timeBudget").checked = tb == null || tb === ""
    ? true
    : !["0", "false", "no", "off"].includes(String(tb).toLowerCase());
  if (!$("timeBudget").checked) {
    const adv = $("advancedOpts");
    if (adv) adv.open = true;
  }
  const ms = p.get("min_scenic");
  if (ms != null && ms !== "") {
    $("minScenic").value = ms;
    $("minScenic").oninput();
  }
  clearVias();
  p.getAll("via").forEach(raw => {
    const parts = String(raw).split(",");
    if (parts.length !== 2) return;
    const la = parseFloat(parts[0]), lo = parseFloat(parts[1]);
    if (Number.isFinite(la) && Number.isFinite(lo)) addVia(L.latLng(la, lo));
  });
  if (["1", "true", "yes"].includes(String(p.get("autoplan") || "").toLowerCase()) && A && B) {
    setTimeout(() => planRoute(), 100);
  }
}

// Host-key gate + deep link after curated styles are in the DOM.
initHostKeyGate().then(() => applyDeepLink());
