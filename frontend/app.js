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
  { position: "topright", collapsed: false }).addTo(map);

const $ = id => document.getElementById(id);
const routeLayer = L.layerGroup().addTo(map);
const previewLayer = L.layerGroup().addTo(map);  // live candidates during search
let A = null, B = null, markerA = null, markerB = null;
let allRoutes = [];      // route summaries from the API
let selectedIdx = 0;     // which route is currently selected
let suppressMapClick = false;  // set when a route line is clicked, so A/B don't reset
let placeMode = null;    // null = explore freely; 'A' or 'B' = next click sets that point
const fmt = ll => ll.lat.toFixed(4) + ", " + ll.lng.toFixed(4);
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
  const t = Math.max(0, Math.min(100, s)) / 100;
  const r = t < 0.5 ? 200 : Math.round(200 - (t - 0.5) * 2 * 140);
  const g = t < 0.5 ? Math.round(70 + t * 2 * 120) : 175;
  return `rgb(${r},${g},60)`;
}
const routeColors = ["#4caf50", "#3d8bfd", "#e0894e", "#c05fd6", "#39c0c8", "#d6c24e"];

// ---- Tabs ----
document.querySelectorAll(".tab").forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
    tab.classList.add("active");
    $("view-" + tab.dataset.view).classList.add("active");
    if (tab.dataset.view === "saved") loadSaved();
  };
});

// ---- Point placement: only when armed, so the map pans/zooms freely ----
function arm(mode) {
  placeMode = mode;
  $("setA").classList.toggle("active", mode === "A");
  $("setB").classList.toggle("active", mode === "B");
  map.getContainer().style.cursor = mode ? "crosshair" : "";
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
  });
  if (isA) { markerA = m; A = latlng; $("ptA").textContent = fmt(latlng); }
  else { markerB = m; B = latlng; $("ptB").textContent = fmt(latlng); }
}

$("setA").onclick = () => arm(placeMode === "A" ? null : "A");
$("setB").onclick = () => arm(placeMode === "B" ? null : "B");

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
  }
  // else: no armed point -> free pan/zoom/click, nothing is set
});

// ---- Preference slider label ----
const prefLabel = v => v < 0.25 ? "Quickest" : v < 0.5 ? "Fairly direct" : v < 0.75 ? "Balanced" : v < 0.95 ? "Scenic" : "Most scenic";
$("pref").oninput = () => $("prefVal").textContent = prefLabel(+$("pref").value);

// ---- Minimum scenic score slider label ----
$("minScenic").oninput = () => {
  const v = +$("minScenic").value;
  $("minScenicVal").textContent = v === 0 ? "Off" : v + "+";
};

// ---- Draw all routes; selected one is coloured by scenic score, others faint ----
function clearRoutes() {
  routeLayer.clearLayers();
  allRoutes = [];
  $("routeList").innerHTML = "";
  $("directions").innerHTML = "";
  $("actionRow").style.display = "none";
  $("legend").style.display = "none";
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
    ${bar("Colour", p.colour)}${bar("Terrain", p.terrain)}${bar("Land", p.landcover)}
  </div>`;
}

// ---- Route option cards ----
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
    const meets = _minScenic > 0 ? (r.meets_min ? "#4caf72" : "#8a94a0") : routeColors[i % routeColors.length];
    const tick = _minScenic > 0 ? (r.meets_min ? " ✓" : "") : "";
    return `<div class="ropt ${i===selectedIdx?"sel":""}" onclick="selectRoute(${i}, false)">
      <div class="swatch" style="background:${routeColors[i % routeColors.length]}"></div>
      <div class="body">
        <div class="title">${i===0 ? "Recommended" : "Alternative " + i}</div>
        <div class="meta">${r.distance_km} km · ${time}${mw}</div>
      </div>
      <div class="badge" style="background:${meets}">${Math.round(r.avg_scenic_score)}${tick}</div>
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
  const mwLine = r.motorway_km > 0.1
    ? `<br><span style="color:#ff9a9a">🛣️ ${r.motorway_km} km on motorways</span>`
    : `<br><span style="color:var(--accent)">✓ No motorways</span>`;
  setStats(`<b>${i===0 ? "Recommended route" : "Alternative " + i}</b><br>
    <span style="color:var(--dim)">Scenic score:</span> <b>${Math.round(r.avg_scenic_score)}/100</b>
    &nbsp;·&nbsp; ${r.distance_km} km &nbsp;·&nbsp; ${Math.round(r.duration_min)} min<br>
    <span style="color:var(--dim)">Made of:</span> colour ${cp.colour ?? "–"} · terrain ${cp.terrain ?? "–"} · land ${cp.landcover ?? "–"}${mwLine}`);
};

function busy(on, msg) {
  $("routeBtn").disabled = on;
  if (on) setStats('<span class="spin">⏳ ' + msg + '</span>');
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
  if (!A || !B) return setStats('<span class="err">Set both a start (A) and end (B) first.</span>');
  if (_es) { _es.close(); _es = null; }
  clearRoutes(); clearPreview();
  busy(true, "Searching…");
  $("searchProgress").style.display = "block";
  searchLog('<div class="sl-phase">Starting search…</div>');

  const u = `/api/route/stream?from_lat=${A.lat}&from_lng=${A.lng}&to_lat=${B.lat}&to_lng=${B.lng}`
    + `&preference=${$("pref").value}&profile=${$("profile").value}`
    + `&avoid_motorways=${$("avoidMw").checked}&min_scenic=${$("minScenic").value}`
    + `&explore_all=${$("exploreAll").checked}`;

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
          `<div class="sl-phase">🔎 ${ev.label}</div>` +
          `<div class="sl-stat">${_searchStats.scored} routes explored · best so far <b>${_searchStats.best}</b></div>`);
        break;
      case "landcover":
        searchLog(
          `<div class="sl-phase">🗺️ Reading land cover…</div>` +
          `<div class="sl-stat">${ev.done}/${ev.total} map tiles loaded` +
          (ev.done < ev.total ? " (fetching fresh tiles — first run in this area is slower)" : "") +
          `</div>`);
        break;
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
    }
  };
  es.onerror = () => {
    if (!_es) return;              // already closed cleanly
    es.close(); _es = null;
    $("searchProgress").style.display = "none";
    setStats('<span class="err">Search connection lost. Try again.</span>');
    busy(false);
  };
}

// Render the final chosen + alternative routes once the search completes.
function finishPlan(data) {
  clearPreview();
  $("searchProgress").style.display = "none";
  allRoutes = data.alternatives.slice().sort((a, b) =>
    (b.chosen === true) - (a.chosen === true) || b.avg_scenic_score - a.avg_scenic_score);
  selectedIdx = 0;
  _minScenic = data.min_scenic || 0;
  _minScenicMet = data.min_scenic_met !== false;
  $("actionRow").style.display = "flex";
  $("legend").style.display = "block";
  _lastFrom = data.from; _lastTo = data.to; _lastProfile = data.profile; _lastPref = data.preference;
  drawRoutes(true); renderRouteList(); selectRoute(0, false);
  busy(false);
}
let _lastFrom = null, _lastTo = null, _lastProfile = null, _lastPref = null;
let _minScenic = 0, _minScenicMet = true;

// ---- Place search ----
async function geocode() {
  const q = $("q").value.trim(); if (!q) return;
  const res = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`,
    { headers: { "Accept-Language": "en" } });
  const arr = await res.json();
  if (!arr.length) return setStats('<span class="err">Place not found.</span>');
  map.setView([+arr[0].lat, +arr[0].lon], 12);
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
  const res = await fetch("/api/routes", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) });
  if (res.ok) { $("saveModal").classList.remove("open"); $("svName").value=$("svTags").value=$("svNotes").value=""; svRating=0;
    $("svStars").querySelectorAll("span").forEach(x=>x.classList.remove("on")); setStats("<b>✓ Route saved to My routes.</b>"); }
  else setStats('<span class="err">Save failed.</span>');
};

// ---- Exports ----
async function loadExportFormats() {
  try {
    const r = await fetch("/api/export/formats"); const d = await r.json();
    (d.formats || []).forEach(f => { const o=document.createElement("option"); o.value=f.id; o.textContent="Export "+(f.name||f.id).toUpperCase(); $("exportFmt").appendChild(o); });
  } catch {}
}
$("exportFmt").onchange = async () => {
  const fmtId = $("exportFmt").value; $("exportFmt").value = "";
  const r = allRoutes[selectedIdx]; if (!fmtId || !r) return;
  const coordinates = (r.render || []).map(p => [p.lng, p.lat]);
  const res = await fetch(`/api/export/${fmtId}`, { method:"POST", headers:{"Content-Type":"application/json"},
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
  const res = await fetch(u); const d = await res.json();
  $("savedList").innerHTML = (d.routes || []).map(r => `
    <div class="card" data-id="${r.id}">
      <h4>${r.favourite ? "★ " : ""}${r.name}</h4>
      <div class="meta">${r.distance_km ?? "?"} km · ${r.duration_min ?? "?"} min · scenic ${r.scenic_score ?? "?"} · ${"★".repeat(r.rating||0)}</div>
      ${(r.tags||[]).map(t=>`<span class="chip">${t}</span>`).join("")}
      ${r.notes ? `<div class="meta" style="margin-top:4px;">${r.notes}</div>` : ""}
      <div class="cardrow">
        <button class="mini secondary" onclick="showSaved(${r.id})">Show on map</button>
        <button class="mini secondary" onclick="toggleFav(${r.id})">★</button>
        <button class="mini secondary" onclick="delSaved(${r.id})">Delete</button>
      </div>
    </div>`).join("") || '<div class="hint">No saved routes yet. Plan one and hit 💾 Save.</div>';
}
window.showSaved = async id => {
  const res = await fetch(`/api/routes/${id}`); const r = await res.json();
  clearRoutes();
  if (r.geojson && r.geojson.coordinates) {
    const latlngs = r.geojson.coordinates.map(c => [c[1], c[0]]);
    L.polyline(latlngs, { color:"#4caf50", weight:6, opacity:.95 }).addTo(routeLayer);
    map.fitBounds(L.latLngBounds(latlngs).pad(.15));
    document.querySelector('.tab[data-view="plan"]').click();
  }
};
window.toggleFav = async id => { await fetch(`/api/routes/${id}/favourite`, { method:"POST" }); loadSaved(); };
window.delSaved = async id => { await fetch(`/api/routes/${id}`, { method:"DELETE" }); loadSaved(); };
$("reloadSaved").onclick = loadSaved;
$("favOnly").onclick = () => { favFilter = !favFilter; $("favOnly").classList.toggle("active"); loadSaved(); };
$("savedSearch").oninput = () => loadSaved();

// ---- Buttons ----
$("routeBtn").onclick = planRoute;
$("searchBtn").onclick = geocode;
$("q").addEventListener("keydown", e => { if (e.key === "Enter") geocode(); });
$("resetBtn").onclick = () => {
  A = B = null; clearRoutes(); arm(null);
  if (markerA) map.removeLayer(markerA); if (markerB) map.removeLayer(markerB); markerA = markerB = null;
  $("ptA").textContent = "—"; $("ptB").textContent = "—"; setStats("");
};

// ---- Init: populate the simple scenery dropdown ----
$("profile").innerHTML = STYLES.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
$("prefVal").textContent = prefLabel(+$("pref").value);
loadExportFormats();
