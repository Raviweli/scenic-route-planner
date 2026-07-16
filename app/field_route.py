"""Heatmap-first scenic field routing.

Wide square → proxy-then-colour scenic heatmap → green corridor spines →
OSRM via peak scenic vias (K candidates) → pick winner by road score_route.
Ephemeral graphs — corridor cells are not written to the global SQLite heatmap.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
import numpy as np

from . import config, scoring, enrich
from .climates import select_climate, climate_display_name
from .graph import haversine_km, _NEIGHBOURS
from .grid import Cell, GridSpec, build_cells
from .roads import (
    _corridor_bbox,
    _route_summary,
    _snap_drawn_route_to_roads,
    blend_signals,
    filter_zero_motorway,
    get_osrm_routes,
    is_zero_motorway,
    route_cost,
    score_route,
    SAMPLE_SPACING_KM,
    MAX_SAMPLES,
)

log = logging.getLogger(__name__)


@dataclass
class CorridorSpec:
    min_lat: float
    min_lng: float
    max_lat: float
    max_lng: float
    cell_deg: float

    @property
    def n_cells(self) -> int:
        gs = GridSpec(self.min_lat, self.min_lng, self.max_lat, self.max_lng, self.cell_deg)
        return gs.n_rows * gs.n_cols

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.min_lat, self.min_lng, self.max_lat, self.max_lng)


@dataclass
class FieldCorridor:
    """A lattice polyline / via sequence extracted from the scenic heatmap."""
    kind: str
    vertices: list[tuple[float, float]]
    path_nodes: list[str]
    lattice_km: float
    lattice_avg_scenic: float
    via_hint: tuple[float, float] | None = None


@dataclass
class HeatmapScores:
    """Per-cell scenic scores with provenance for soft-degrade honesty."""
    scores: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # colour | proxy | cache | unknown

    @property
    def proxy_cells(self) -> int:
        return sum(1 for s in self.sources.values() if s == "proxy")

    @property
    def colour_cells(self) -> int:
        return sum(1 for s in self.sources.values() if s in ("colour", "cache"))


def _inflate_to_square(
    s: float, w: float, n: float, e: float,
) -> tuple[float, float, float, float]:
    """Expand the short axis so the bbox is approximately square."""
    height = max(n - s, 1e-9)
    width = max(e - w, 1e-9)
    if abs(height - width) < 1e-12:
        return s, w, n, e
    if height < width:
        mid = (n + s) / 2.0
        half = width / 2.0
        return mid - half, w, mid + half, e
    mid = (e + w) / 2.0
    half = height / 2.0
    return s, mid - half, n, mid + half


def corridor_spec(
    a: tuple[float, float],
    b: tuple[float, float],
    pad_deg: float | None = None,
    cell_deg: float | None = None,
    *,
    square: bool | None = None,
) -> CorridorSpec:
    """Bbox + cell size for the A→B search window (square by default; auto-coarsens).

    Short/medium hops keep ``FIELD_CELL_DEG`` (fine colour cells). Longer hops
    start at ``FIELD_CELL_DEG_LONG``, then ``FIELD_MAX_CELLS`` coarsens further.
    """
    pad = config.FIELD_CORRIDOR_PAD_DEG if pad_deg is None else float(pad_deg)
    do_square = config.FIELD_SQUARE_BBOX if square is None else bool(square)
    if cell_deg is None:
        hop_km = haversine_km(a[0], a[1], b[0], b[1])
        if hop_km >= float(config.FIELD_LONG_HOP_KM):
            cell = float(config.FIELD_CELL_DEG_LONG)
        else:
            cell = float(config.FIELD_CELL_DEG)
    else:
        cell = float(cell_deg)
    s, w, n, e = _corridor_bbox(a, b, pad=pad)
    if do_square:
        s, w, n, e = _inflate_to_square(s, w, n, e)
    spec = CorridorSpec(min_lat=s, min_lng=w, max_lat=n, max_lng=e, cell_deg=cell)
    while spec.n_cells > config.FIELD_MAX_CELLS and spec.cell_deg < 1.0:
        spec = CorridorSpec(
            min_lat=s, min_lng=w, max_lat=n, max_lng=e,
            cell_deg=spec.cell_deg * 1.5,
        )
    return spec


def _strip_filter_cells(
    cells: list[Cell],
    a: tuple[float, float],
    b: tuple[float, float],
    half_width_deg: float | None = None,
) -> list[Cell]:
    half_w = (
        config.FIELD_CORRIDOR_HALF_WIDTH_DEG
        if half_width_deg is None else float(half_width_deg)
    )
    if half_w <= 0 or len(cells) <= 1:
        return cells
    strip = [
        c for c in cells
        if math.sqrt(enrich._point_to_segment_deg2(c.lat, c.lng, a, b)) <= half_w
    ]
    return strip if strip else cells


def build_corridor_cells(
    spec: CorridorSpec,
    a: tuple[float, float],
    b: tuple[float, float],
) -> list[Cell]:
    """Lattice cells over the (typically square) bbox; strip only if half-width > 0."""
    gs = GridSpec(spec.min_lat, spec.min_lng, spec.max_lat, spec.max_lng, spec.cell_deg)
    return _strip_filter_cells(build_cells(gs), a, b)


def _cell_cache_path(lat: float, lng: float, profile_id: str | None) -> Path:
    pid = (profile_id or "default").replace("/", "_")
    return config.FIELD_CELL_CACHE_DIR / f"fc_{lat:.4f}_{lng:.4f}_{pid}.npz"


def _cell_cache_get(lat: float, lng: float, profile_id: str | None) -> float | None:
    path = _cell_cache_path(lat, lng, profile_id)
    if not path.is_file():
        return None
    try:
        data = np.load(path)
        return float(data["score"])
    except Exception:
        return None


def _cell_cache_set(lat: float, lng: float, profile_id: str | None, score: float) -> None:
    path = _cell_cache_path(lat, lng, profile_id)
    try:
        tmp = path.parent / (path.stem + ".tmp.npz")
        np.savez_compressed(tmp, score=float(score))
        tmp.replace(path)
    except Exception:
        log.debug("field_cell_cache_write_failed", exc_info=True)


def _cell_relief_scores(cells: list[Cell], elevs: list[float]) -> list[float]:
    by_rc = {(c.row, c.col): i for i, c in enumerate(cells)}
    out: list[float] = []
    for i, c in enumerate(cells):
        window = [elevs[i]]
        for dr, dc in _NEIGHBOURS:
            j = by_rc.get((c.row + dr, c.col + dc))
            if j is not None:
                window.append(elevs[j])
        out.append(enrich.relief_scores(window)[0])
    return out


def score_cell(
    lat: float,
    lng: float,
    features: dict | None,
    *,
    elev: float | None = None,
    terrain: float | None = None,
    land_score: float | None = None,
    weights: dict | None = None,
    climate=None,
    source: str = "esri",
) -> float:
    """Full scenic blend at a cell centre (colour + terrain + landcover).

    Applies urban/bad-colour clamps so grey/white sprawl and town-park greens
    cannot masquerade as countryside corridor cells.
    """
    coords = [(lat, lng)]
    if climate is None:
        climate = select_climate(lat, lng, elev_m=elev)
    if land_score is None and features is not None:
        if enrich.landcover_is_usable(features, coords):
            land_score = enrich.landcover_scores(coords, features, climate_ids=[climate.id])[0]
    # Without OSM landcover, bump Esri zoom so roofs/roads register as grey.
    colour_zoom = None
    if land_score is None:
        bonus = max(0, int(config.FIELD_COLOUR_ZOOM_BONUS))
        if bonus:
            colour_zoom = int(config.TILE_ZOOM) + bonus
    colour_sc = scoring.score_location(
        lat, lng, zoom=colour_zoom, source=source, climate=climate,
    )
    colour = float(colour_sc.score)
    # Honest image signals when map context is missing: grey roofs pull down
    # tree-canopy towns; open water should not paint as woodland-green.
    if land_score is None:
        colour = _colour_only_field_adjust(
            colour,
            grey_frac=colour_sc.grey_frac,
            blue_frac=colour_sc.blue_frac,
        )
    terr = terrain
    if terr is None and elev is not None:
        terr = enrich.relief_scores([elev])[0]
    terr_soft = None
    # Without landcover, skip weak relief entirely (including colour-softened lifts)
    # so Esri greens are not diluted into yellow-brown.
    if _weak_terrain_without_landcover(terr, land_score) is not None:
        near = None
        if features is not None and land_score is not None:
            near = enrich.landcover_details(coords, features, climate_ids=[climate.id])[0]
        terr_soft = enrich.soften_terrain(
            terr, colour, land_score,
            near_water=enrich.is_water_near(near),
            climate_id=climate.id,
        )
    wc = weights["colour"] if weights else None
    wt = weights["terrain"] if weights else None
    wl = weights["landcover"] if weights else None
    if weights is None and hasattr(climate, "blend_colour"):
        wc = climate.blend_colour
        wt = climate.blend_terrain
        wl = climate.blend_landcover
    blended = blend_signals(colour, terr_soft, land_score, w_colour=wc, w_terrain=wt, w_land=wl)
    return round(
        apply_field_urban_clamp(blended, grey_frac=colour_sc.grey_frac, land_score=land_score),
        1,
    )


def _colour_only_field_adjust(
    colour: float,
    *,
    grey_frac: float | None,
    blue_frac: float | None,
) -> float:
    """When landcover is missing, trust image grey/water — do not inflate scenic."""
    out = float(colour)
    g = float(grey_frac or 0.0)
    # Soft grey already present in the tile: pull canopy-green towns toward dull.
    if g > 0.0:
        out *= max(0.35, 1.0 - 0.65 * min(1.0, g / 0.22))
    b = float(blue_frac or 0.0)
    if b >= 0.30:
        # Water is scenic but should not wash the heatmap as vivid countryside green.
        water_cap = 52.0 + 18.0 * min(1.0, (b - 0.30) / 0.40)
        out = min(out, water_cap)
    return max(0.0, min(100.0, out))


def apply_field_urban_clamp(
    score: float,
    *,
    grey_frac: float | None = None,
    land_score: float | None = None,
    reject_scenic: float | None = None,
    grey_reject_frac: float | None = None,
    grey_soft_frac: float | None = None,
    urban_land_cap: float | None = None,
) -> float:
    """Force bad urban colour / town-fabric cells toward the hard reject floor.

    - High grey_frac (concrete, white roofs, low-sat built-up) → reject.
    - Modest grey_frac → graduated ceiling (towns stay cooler than countryside).
    - Low landcover (residential/commercial fabric) caps green colour so a
      town park cannot keep a cell in the green corridor band.
    """
    reject = (
        float(config.FIELD_REJECT_SCENIC) if reject_scenic is None else float(reject_scenic)
    )
    grey_thr = (
        float(config.FIELD_GREY_REJECT_FRAC)
        if grey_reject_frac is None else float(grey_reject_frac)
    )
    grey_soft = (
        float(config.FIELD_GREY_SOFT_FRAC)
        if grey_soft_frac is None else float(grey_soft_frac)
    )
    land_cap = (
        float(config.FIELD_URBAN_LAND_CAP) if urban_land_cap is None else float(urban_land_cap)
    )
    out = float(score)
    if grey_frac is not None:
        g = float(grey_frac)
        if g >= grey_thr:
            # White/grey built-up: hard nope — do not average into an OK path.
            out = min(out, reject - 1.0)
        elif g >= grey_soft and grey_thr > grey_soft:
            # Graduated: mild built-up cannot sit in the vivid-green band.
            t = (g - grey_soft) / (grey_thr - grey_soft)
            ceiling = 72.0 - t * (72.0 - (reject + 4.0))
            out = min(out, ceiling)
    if land_score is not None and land_score < land_cap:
        # Urban fabric: blend toward landcover so park-green cannot dominate.
        mixed = 0.35 * out + 0.65 * float(land_score)
        out = min(out, mixed)
        if land_score < reject:
            out = min(out, reject - 1.0)
    return max(0.0, min(100.0, out))


def field_reject_threshold(min_scenic: float | None = None) -> float:
    """Effective scenic floor for green corridor connectivity."""
    base = config.FIELD_MIN_SCENIC_CONNECT if min_scenic is None else float(min_scenic)
    return max(float(base), float(config.FIELD_REJECT_SCENIC))


def is_reject_score(score: float, threshold: float | None = None) -> bool:
    thr = field_reject_threshold() if threshold is None else float(threshold)
    return float(score) < thr


def corridor_reject_fraction(
    path_nodes: list[str],
    scores: dict[str, float],
    threshold: float | None = None,
) -> float:
    """Fraction of spine nodes below the reject scenic floor."""
    if not path_nodes:
        return 0.0
    thr = field_reject_threshold() if threshold is None else float(threshold)
    bad = sum(1 for n in path_nodes if is_reject_score(scores.get(n, 50.0), thr))
    return bad / len(path_nodes)


def _weak_terrain_without_landcover(
    terrain: float | None,
    land_score: float | None,
    *,
    floor: float = 30.0,
) -> float | None:
    """Drop weak relief when landcover is missing so flat cells are not anti-scenic.

    Field heatmaps often lose Overpass landcover (timeout / 504). Relief alone on
    gentle UK countryside is ~0–20, which would paint a fake uniform brown band
    and dilute strong Esri greens. Treat that as unknown instead.
    """
    if land_score is not None:
        return terrain
    if terrain is None:
        return None
    if float(terrain) < float(floor):
        return None
    return terrain


def _proxy_blend(
    terrain: float | None,
    land_score: float | None,
    weights: dict | None,
    climate,
) -> float:
    """Cheap scenic estimate without Esri colour (terrain + landcover only)."""
    wc = 0.0
    wt = weights["terrain"] if weights else None
    wl = weights["landcover"] if weights else None
    if weights is None and climate is not None and hasattr(climate, "blend_terrain"):
        wt = climate.blend_terrain
        wl = climate.blend_landcover
    # Colour weight 0 → blend_signals omits it and renormalises over present signals.
    terrain = _weak_terrain_without_landcover(terrain, land_score)
    if terrain is None and land_score is None:
        # Unknown (no map context, no meaningful relief) — neutral, not reject-brown.
        return 50.0
    return round(
        blend_signals(50.0, terrain, land_score, w_colour=wc, w_terrain=wt, w_land=wl),
        1,
    )


def _colour_priority_key(
    cell: Cell,
    proxy: float,
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, float]:
    """Spend colour on A→B spine first, then high-proxy / uncertain cells.

    Stronger path weight than a flat proxy sort so long world corridors colour
    the navigable chord under a tight budget instead of remote high-relief edges.
    """
    dist2 = enrich._point_to_segment_deg2(cell.lat, cell.lng, a, b)
    dist = math.sqrt(dist2)
    # ~0.07° (~8 km) half-width gets a meaningful boost; spine cells win.
    path_bonus = max(0.0, 28.0 - dist * 400.0)
    # Slight preference for mid-proxy cells (uncertain) over already-extreme ones.
    uncertainty = max(0.0, 1.0 - abs(float(proxy) - 55.0) / 55.0) * 5.0
    return (-(float(proxy) * 0.65 + path_bonus + uncertainty), dist2)


def _adaptive_osrm_reserve_sec(
    *,
    landcover_elapsed: float,
    landcover_incomplete: bool,
    cache_hits: int,
    n_cells: int,
) -> float:
    """Shrink OSRM reserve when colour needs the seconds more than routing does.

    Cold / truncated landcover keeps the full reserve so OSRM still finishes.
    Warm landcover or a high cell-cache hit rate frees wall-clock for Esri colour.
    """
    base = float(config.FIELD_OSRM_RESERVE_SEC)
    floor = float(config.FIELD_OSRM_RESERVE_MIN_SEC)
    floor = max(2.0, min(floor, base))
    if landcover_incomplete:
        return base
    cache_frac = cache_hits / max(1, n_cells)
    if landcover_elapsed < 4.0 or cache_frac >= 0.40:
        return floor
    if landcover_elapsed < 8.0 or cache_frac >= 0.15:
        return (base + floor) / 2.0
    return base


def _score_corridor_cells(
    cells: list[Cell],
    features: dict | None,
    *,
    a: tuple[float, float],
    b: tuple[float, float],
    weights: dict | None,
    profile_id: str | None,
    source: str,
    progress=None,
    deadline: float | None = None,
    cell_deg: float | None = None,
) -> HeatmapScores:
    """Proxy-score every cell, then spend Esri colour in priority order under deadline."""
    coords = [(c.lat, c.lng) for c in cells]
    elevs = enrich.elevation_batch(coords) or [0.0] * len(coords)
    relief = _cell_relief_scores(cells, elevs)
    land_usable = enrich.landcover_is_usable(features, coords)
    climates = [select_climate(c.lat, c.lng, elev_m=elevs[i]) for i, c in enumerate(cells)]
    land_scores = (
        enrich.landcover_scores(coords, features, climate_ids=[c.id for c in climates])
        if land_usable and features is not None else None
    )

    heat = HeatmapScores()
    need_colour: list[tuple[int, Cell, float]] = []  # index, cell, proxy

    for i, c in enumerate(cells):
        cached = _cell_cache_get(c.lat, c.lng, profile_id)
        if cached is not None:
            heat.scores[c.id] = cached
            heat.sources[c.id] = "cache"
            if progress:
                progress({
                    "type": "cell", "lat": c.lat, "lng": c.lng,
                    "score": cached, "cell_deg": cell_deg, "source": "cache",
                })
            continue
        climate = climates[i]
        land = land_scores[i] if land_scores is not None else None
        terr = relief[i]
        terr_soft = enrich.soften_terrain(
            terr, 50.0, land,
            near_water=False,
            climate_id=climate.id,
        )
        proxy = _proxy_blend(terr_soft, land, weights, climate)
        proxy = apply_field_urban_clamp(proxy, grey_frac=None, land_score=land)
        heat.scores[c.id] = proxy
        heat.sources[c.id] = "proxy"
        if progress:
            progress({
                "type": "cell", "lat": c.lat, "lng": c.lng,
                "score": proxy, "cell_deg": cell_deg, "source": "proxy",
            })
        need_colour.append((i, c, proxy))

    need_colour.sort(key=lambda item: _colour_priority_key(item[1], item[2], a, b))

    def _colour_task(item):
        i, c, _proxy = item
        if deadline is not None and time.perf_counter() >= deadline:
            return c.id, None
        climate = climates[i]
        sc = score_cell(
            c.lat, c.lng, features,
            elev=elevs[i],
            terrain=relief[i],
            land_score=land_scores[i] if land_scores is not None else None,
            weights=weights,
            climate=climate,
            source=source,
        )
        return c.id, sc

    workers = max(1, config.FIELD_SCORE_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_colour_task, item): item for item in need_colour}
        for fut in as_completed(futures):
            if deadline is not None and time.perf_counter() >= deadline:
                break
            cid, sc = fut.result()
            if sc is None:
                continue
            heat.scores[cid] = sc
            heat.sources[cid] = "colour"
            cell = next(c for c in cells if c.id == cid)
            _cell_cache_set(cell.lat, cell.lng, profile_id, sc)
            if progress:
                progress({
                    "type": "cell", "lat": cell.lat, "lng": cell.lng,
                    "score": sc, "cell_deg": cell_deg, "source": "colour",
                })

    # Soft-degrade: keep proxy scores. Only truly unknown cells get neutral 50.
    for c in cells:
        if c.id not in heat.scores:
            heat.scores[c.id] = 50.0
            heat.sources[c.id] = "unknown"
    return heat


def _build_soft_reject_graph(
    cells: list[Cell],
    scores: dict[str, float],
    *,
    detour_factor: float,
    reject_thr: float,
) -> nx.Graph:
    """Full lattice with prohibitive scenic on reject edges (connectivity fallback)."""
    g = nx.Graph()
    by_rc = {(c.row, c.col): c for c in cells}
    for c in cells:
        sc = scores.get(c.id, 50.0)
        g.add_node(c.id, lat=c.lat, lng=c.lng, row=c.row, col=c.col, score=sc)
    for c in cells:
        for dr, dc in _NEIGHBOURS:
            nb = by_rc.get((c.row + dr, c.col + dc))
            if nb is None or g.has_edge(c.id, nb.id):
                continue
            dist = haversine_km(c.lat, c.lng, nb.lat, nb.lng)
            scenic = (scores.get(c.id, 50.0) + scores.get(nb.id, 50.0)) / 2.0
            if is_reject_score(scenic, reject_thr):
                scenic = min(scenic, reject_thr - 5.0)
            g.add_edge(c.id, nb.id, dist=dist, scenic=scenic)
    g.graph["detour_factor"] = float(detour_factor)
    g.graph["reject_scenic"] = float(reject_thr)
    return g


def build_corridor_graph(
    cells: list[Cell],
    scores: dict[str, float],
    preference: float,
    *,
    min_scenic: float | None = None,
    detour_factor: float | None = None,
    a: tuple[float, float] | None = None,
    b: tuple[float, float] | None = None,
    endpoint_slack_deg: float | None = None,
) -> nx.Graph:
    """8-connected lattice; edge cost matches graph._edge_cost (optional detour override).

    Cells below the reject scenic floor are omitted so paths cannot soft-average
    through urban grey/white bands. Endpoints keep a small slack so A/B inside
    towns remain reachable.
    """
    preference = max(0.0, min(1.0, float(preference)))
    floor = field_reject_threshold(min_scenic)
    slack = (
        float(config.FIELD_ENDPOINT_REJECT_SLACK_DEG)
        if endpoint_slack_deg is None else float(endpoint_slack_deg)
    )
    by_rc = {(c.row, c.col): c for c in cells}
    g = nx.Graph()
    for c in cells:
        sc = scores.get(c.id, 50.0)
        if sc < floor:
            near_end = False
            if a is not None and abs(c.lat - a[0]) <= slack and abs(c.lng - a[1]) <= slack:
                near_end = True
            if b is not None and abs(c.lat - b[0]) <= slack and abs(c.lng - b[1]) <= slack:
                near_end = True
            if not near_end:
                continue
        g.add_node(c.id, lat=c.lat, lng=c.lng, row=c.row, col=c.col, score=sc)
    for c in cells:
        if c.id not in g:
            continue
        for dr, dc in _NEIGHBOURS:
            nb = by_rc.get((c.row + dr, c.col + dc))
            if nb is None or nb.id not in g:
                continue
            if g.has_edge(c.id, nb.id):
                continue
            dist = haversine_km(c.lat, c.lng, nb.lat, nb.lng)
            scenic = (g.nodes[c.id]["score"] + g.nodes[nb.id]["score"]) / 2.0
            # Prohibitive cost if either endpoint is still a reject (endpoint slack).
            if is_reject_score(g.nodes[c.id]["score"], floor) or is_reject_score(
                g.nodes[nb.id]["score"], floor
            ):
                scenic = min(scenic, floor - 1.0)
            g.add_edge(c.id, nb.id, dist=dist, scenic=scenic)
    if detour_factor is not None:
        g.graph["detour_factor"] = float(detour_factor)
    g.graph["reject_scenic"] = floor
    return g


def _field_edge_cost(scenic: float, dist: float, preference: float, detour: float) -> float:
    penalty = detour * (1.0 - scenic / 100.0)
    return dist * (1.0 + preference * penalty)


def _nearest_cell_node(g: nx.Graph, lat: float, lng: float) -> str | None:
    best, best_d = None, float("inf")
    for node, data in g.nodes(data=True):
        d = (data["lat"] - lat) ** 2 + (data["lng"] - lng) ** 2
        if d < best_d:
            best, best_d = node, d
    return best


def _path_metrics(g: nx.Graph, path: list[str]) -> dict:
    vertices: list[tuple[float, float]] = []
    scenics: list[float] = []
    total_km = 0.0
    for i, node in enumerate(path):
        d = g.nodes[node]
        vertices.append((d["lat"], d["lng"]))
        if i > 0:
            e = g.edges[path[i - 1], node]
            total_km += e["dist"]
            scenics.append(e["scenic"])
    avg_scenic = sum(scenics) / len(scenics) if scenics else g.nodes[path[0]]["score"]
    return {
        "vertices": vertices,
        "lattice_km": round(total_km, 2),
        "lattice_avg_scenic": round(avg_scenic, 1),
        "path_nodes": path,
    }


def find_field_path(
    g: nx.Graph,
    a: tuple[float, float],
    b: tuple[float, float],
    preference: float,
    *,
    via: tuple[float, float] | None = None,
    detour_factor: float | None = None,
) -> dict:
    """Shortest path through scored cells; optional forced via cell centre."""
    preference = max(0.0, min(1.0, float(preference)))
    if g.number_of_nodes() == 0:
        return {"error": "No scenic cells available in the corridor."}

    src = _nearest_cell_node(g, a[0], a[1])
    dst = _nearest_cell_node(g, b[0], b[1])
    if src is None or dst is None:
        return {"error": "Could not snap endpoints to the scenic field."}
    if src == dst and via is None:
        return {"error": "Start and end snap to the same cell; pick points further apart."}

    detour = (
        float(detour_factor) if detour_factor is not None
        else float(g.graph.get("detour_factor", config.DETOUR_FACTOR))
    )

    def weight(u, v, data):
        return _field_edge_cost(data["scenic"], data["dist"], preference, detour)

    try:
        if via is not None:
            mid = _nearest_cell_node(g, via[0], via[1])
            if mid is None:
                return {"error": "Could not snap via to the scenic field."}
            p1 = nx.shortest_path(g, src, mid, weight=weight)
            p2 = nx.shortest_path(g, mid, dst, weight=weight)
            path = p1 + p2[1:]
        else:
            path = nx.shortest_path(g, src, dst, weight=weight)
    except nx.NetworkXNoPath:
        return {"error": "No path between the selected points on the scenic field."}

    return _path_metrics(g, path)


def _point_to_polyline_deg2(
    lat: float, lng: float, vertices: list[tuple[float, float]],
) -> float:
    if len(vertices) < 2:
        if not vertices:
            return float("inf")
        return (lat - vertices[0][0]) ** 2 + (lng - vertices[0][1]) ** 2
    best = float("inf")
    for i in range(len(vertices) - 1):
        d2 = enrich._point_to_segment_deg2(lat, lng, vertices[i], vertices[i + 1])
        if d2 < best:
            best = d2
    return best


def green_mask_stats(
    cells: list[Cell],
    scores: dict[str, float],
    threshold: float | None = None,
) -> dict:
    thr = config.FIELD_GREEN_THRESHOLD if threshold is None else float(threshold)
    green = sum(1 for c in cells if scores.get(c.id, 0.0) >= thr)
    dull = len(cells) - green
    return {
        "threshold": thr,
        "green_cells": green,
        "dull_cells": dull,
        "green_fraction": round(green / len(cells), 3) if cells else 0.0,
    }


def extract_corridors(
    g: nx.Graph,
    cells: list[Cell],
    scores: dict[str, float],
    a: tuple[float, float],
    b: tuple[float, float],
    preference: float,
) -> list[FieldCorridor]:
    """Primary green spine + diversions through separated peaks + dull baseline.

    Spines that cross too many reject (urban/bad-colour) cells are discarded so
    the planner tries another corridor instead of averaging through town.
    """
    field_detour = float(config.FIELD_DETOUR_FACTOR)
    corridors: list[FieldCorridor] = []
    rejected_meta: list[dict] = []
    sep = float(config.FIELD_DIVERSION_MIN_SEP_DEG)
    thr = float(config.FIELD_GREEN_THRESHOLD)
    max_green = max(1, int(config.FIELD_MAX_GREEN_CORRIDORS))
    reject_thr = float(g.graph.get("reject_scenic", field_reject_threshold()))
    max_reject_frac = float(config.FIELD_REJECT_MAX_FRAC)

    def _accept_spine(path: dict, kind: str) -> FieldCorridor | None:
        frac = corridor_reject_fraction(path["path_nodes"], scores, reject_thr)
        if frac > max_reject_frac:
            rejected_meta.append({
                "kind": kind,
                "reason": "urban_bad_colour",
                "reject_frac": round(frac, 3),
            })
            log.info(
                "field_corridor_rejected kind=%s reject_frac=%.3f",
                kind, frac,
            )
            return None
        return FieldCorridor(
            kind=kind,
            vertices=path["vertices"],
            path_nodes=path["path_nodes"],
            lattice_km=path["lattice_km"],
            lattice_avg_scenic=path["lattice_avg_scenic"],
        )

    primary = find_field_path(g, a, b, preference, detour_factor=field_detour)
    primary_corr: FieldCorridor | None = None
    if not primary.get("error"):
        primary_corr = _accept_spine(primary, "green_primary")
        if primary_corr is not None:
            corridors.append(primary_corr)
    if primary_corr is None and not primary.get("error"):
        # Primary crossed reject band — still use its geometry only as a
        # reference for diversion separation, not as an OSRM via spine.
        primary_verts = primary.get("vertices") or []
        used_nodes = set(primary.get("path_nodes") or [])
    elif primary_corr is not None:
        primary_verts = primary_corr.vertices
        used_nodes = set(primary_corr.path_nodes)
    else:
        primary_verts = []
        used_nodes = set()

    # Rank green cells by score, pick peaks far from the primary spine.
    candidates = sorted(
        (c for c in cells if scores.get(c.id, 0.0) >= thr and c.id in g),
        key=lambda c: scores.get(c.id, 0.0),
        reverse=True,
    )
    diversions = 0
    for c in candidates:
        if diversions >= max_green - (1 if primary_corr is not None else 0):
            break
        if c.id in used_nodes:
            continue
        if primary_verts and math.sqrt(
            _point_to_polyline_deg2(c.lat, c.lng, primary_verts)
        ) < sep:
            continue
        path = find_field_path(
            g, a, b, preference, via=(c.lat, c.lng), detour_factor=field_detour,
        )
        if path.get("error"):
            continue
        # Require a meaningfully different path.
        overlap = len(set(path["path_nodes"]) & used_nodes) / max(len(path["path_nodes"]), 1)
        if used_nodes and overlap > 0.85:
            continue
        corr = _accept_spine(path, "green_diversion")
        if corr is None:
            continue
        corr.via_hint = (c.lat, c.lng)
        corridors.append(corr)
        used_nodes.update(path["path_nodes"])
        diversions += 1

    # If primary was rejected but a diversion survived, promote the best
    # diversion so OSRM still has a green_primary label for meta/UI.
    if primary_corr is None:
        for corr in corridors:
            if corr.kind == "green_diversion":
                corr.kind = "green_primary"
                break

    # Near-direct / low-preference spine as the non-green baseline.
    # Baseline uses the same reject graph (no soft path through white town).
    baseline = find_field_path(g, a, b, preference=0.0, detour_factor=field_detour)
    if not baseline.get("error"):
        # Baseline may skim endpoints; still record reject frac for honesty.
        frac = corridor_reject_fraction(baseline["path_nodes"], scores, reject_thr)
        corridors.append(FieldCorridor(
            kind="baseline_direct",
            vertices=baseline["vertices"],
            path_nodes=baseline["path_nodes"],
            lattice_km=baseline["lattice_km"],
            lattice_avg_scenic=baseline["lattice_avg_scenic"],
        ))
        if frac > max_reject_frac:
            rejected_meta.append({
                "kind": "baseline_direct",
                "reason": "urban_bad_colour_noted",
                "reject_frac": round(frac, 3),
            })

    # Stash reject diagnostics on a synthetic attribute for plan_field.
    extract_corridors.last_rejected = rejected_meta  # type: ignore[attr-defined]
    return corridors


# Module-level last-reject list written by extract_corridors (read by plan_field).
extract_corridors.last_rejected = []  # type: ignore[attr-defined]


def _peak_scenic_vias(
    corridor: FieldCorridor,
    scores: dict[str, float],
    max_vias: int | None = None,
) -> list[tuple[float, float]]:
    """Pick peak scenic vias along a spine (not uniform decimation)."""
    cap = config.FIELD_PEAK_VIAS if max_vias is None else int(max_vias)
    nodes = corridor.path_nodes
    verts = corridor.vertices
    if len(verts) <= 2 or cap <= 0:
        return []
    # Interior nodes with their scenic scores.
    interior = []
    for i in range(1, len(nodes) - 1):
        sc = scores.get(nodes[i], 50.0)
        # Prefer local maxima or highest scores.
        prev_sc = scores.get(nodes[i - 1], sc)
        next_sc = scores.get(nodes[i + 1], sc) if i + 1 < len(nodes) else sc
        is_peak = sc >= prev_sc and sc >= next_sc
        interior.append((sc + (5.0 if is_peak else 0.0), i, verts[i]))
    interior.sort(key=lambda x: x[0], reverse=True)

    picked: list[tuple[int, tuple[float, float]]] = []
    min_gap = max(1, len(verts) // (cap + 1))
    for _sc, idx, pt in interior:
        if any(abs(idx - j) < min_gap for j, _ in picked):
            continue
        picked.append((idx, pt))
        if len(picked) >= cap:
            break
    # If via_hint exists and not already near a pick, include it.
    if corridor.via_hint is not None and len(picked) < cap:
        vh = corridor.via_hint
        if not any(abs(vh[0] - p[0]) < 1e-4 and abs(vh[1] - p[1]) < 1e-4 for _, p in picked):
            # Find nearest index on spine for ordering.
            best_i, best_d = 1, float("inf")
            for i, v in enumerate(verts):
                d = (v[0] - vh[0]) ** 2 + (v[1] - vh[1]) ** 2
                if d < best_d:
                    best_i, best_d = i, d
            if 0 < best_i < len(verts) - 1:
                picked.append((best_i, vh))
    picked.sort(key=lambda x: x[0])
    return [p for _, p in picked[:cap]]


def _decimate_vertices(
    vertices: list[tuple[float, float]],
    max_pts: int = 40,
) -> list[tuple[float, float]]:
    if len(vertices) <= max_pts:
        return list(vertices)
    step = (len(vertices) - 1) / float(max_pts - 1)
    out = [vertices[min(len(vertices) - 1, int(round(i * step)))] for i in range(max_pts)]
    out[0] = vertices[0]
    out[-1] = vertices[-1]
    return out


def _rank_key(rt: dict, preference: float) -> float:
    return route_cost(rt, preference, config.DETOUR_FACTOR)


def _emergency_osrm_routes(
    a: tuple[float, float],
    b: tuple[float, float],
    corridors: list[FieldCorridor],
    scores: dict[str, float],
    *,
    add_routes,
    deadline: float | None,
    emit=None,
) -> None:
    """Last-chance real OSRM before lattice snap (direct + lattice peak vias)."""
    def _hit() -> bool:
        return deadline is not None and time.perf_counter() >= deadline

    if _hit():
        return
    if emit:
        emit({"type": "phase", "phase": "osrm", "label": "Routing direct fallback…"})
    try:
        add_routes(get_osrm_routes(a, b, alternatives=2), "direct_emergency", "baseline_osrm")
    except Exception:
        log.warning("field_osrm_emergency_direct_failed", exc_info=True)

    if _hit():
        return
    primary = next(
        (c for c in corridors if c.kind not in ("baseline_direct",)),
        corridors[0] if corridors else None,
    )
    if primary is None:
        return
    vias = _peak_scenic_vias(primary, scores, max_vias=max(2, config.FIELD_PEAK_VIAS))
    if not vias:
        return
    if emit:
        emit({
            "type": "phase", "phase": "osrm",
            "label": "Routing via scenic lattice peaks…",
        })
    try:
        add_routes(
            get_osrm_routes(a, b, waypoints=vias, alternatives=0),
            "via_emergency",
            primary.kind,
        )
    except Exception:
        log.warning("field_osrm_emergency_via_failed", exc_info=True)


def _osrm_tournament(
    a: tuple[float, float],
    b: tuple[float, float],
    corridors: list[FieldCorridor],
    scores: dict[str, float],
    *,
    features: dict | None,
    weights: dict | None,
    source: str,
    preference: float,
    avoid_motorways: bool,
    deadline: float | None,
    progress=None,
) -> tuple[dict | None, list[dict], str]:
    """Route via OSRM through green vias + direct baseline; pick by road score_route."""
    def _emit(ev):
        if progress:
            try:
                progress(ev)
            except Exception:
                pass

    tried: list[dict] = []
    routes: list[dict] = []
    max_cands = max(1, int(config.FIELD_MAX_CANDIDATES))
    snap_rejected_mw = False

    def _budget_hit() -> bool:
        return deadline is not None and time.perf_counter() >= deadline

    def _add_routes(raw: list[dict], label: str, corridor_kind: str | None = None):
        for rt in raw:
            if len(routes) >= max_cands:
                return
            # Dedup by approx geometry length + first/last coords.
            key = (
                round(rt.get("distance_km", 0.0), 2),
                round(rt["coords"][0][0], 4) if rt.get("coords") else 0,
                round(rt["coords"][-1][0], 4) if rt.get("coords") else 0,
                round(rt.get("motorway_km", 0.0), 1),
            )
            if any(t.get("_dedupe") == key for t in tried):
                continue
            rt = dict(rt)
            rt["_dedupe"] = key
            rt["_field_label"] = label
            rt["_corridor_kind"] = corridor_kind
            routes.append(rt)
            tried.append({
                "label": label,
                "corridor_kind": corridor_kind,
                "distance_km": round(rt.get("distance_km", 0.0), 1),
                "motorway_km": round(rt.get("motorway_km", 0.0), 1),
            })

    # Direct / alt OSRM baseline first so greener routes must beat real roads.
    if not _budget_hit():
        try:
            _emit({"type": "phase", "phase": "osrm", "label": "Routing direct baseline…"})
            base = get_osrm_routes(a, b, alternatives=2)
            _add_routes(base, "direct", "baseline_osrm")
        except Exception:
            log.warning("field_osrm_direct_failed", exc_info=True)

    # Green corridor vias (skip lattice-only baseline_direct — road baseline covers it).
    for corr in corridors:
        if len(routes) >= max_cands or _budget_hit():
            break
        if corr.kind == "baseline_direct":
            continue
        vias = _peak_scenic_vias(corr, scores)
        if not vias:
            # Fallback: a couple of midpoints along the spine.
            verts = corr.vertices
            if len(verts) >= 3:
                mid = len(verts) // 2
                vias = [verts[mid]]
            else:
                continue
        try:
            _emit({
                "type": "phase", "phase": "osrm",
                "label": f"Routing via {corr.kind} corridor…",
            })
            via_routes = get_osrm_routes(a, b, waypoints=vias, alternatives=0)
            _add_routes(via_routes, f"via_{corr.kind}", corr.kind)
        except Exception:
            log.warning("field_osrm_via_failed kind=%s", corr.kind, exc_info=True)

    if not routes:
        _emergency_osrm_routes(
            a, b, corridors, scores,
            add_routes=_add_routes,
            deadline=deadline,
            emit=_emit,
        )

    if not routes and corridors and not _budget_hit():
        # Last resort: draw-mode snap of the primary lattice spine.
        if corridors:
            _emit({"type": "phase", "phase": "snap", "label": "Snapping lattice path to roads…"})
            try:
                snap_verts = _decimate_vertices(corridors[0].vertices)
                route, snap_method = _snap_drawn_route_to_roads(snap_verts)
                if avoid_motorways and not is_zero_motorway(route):
                    snap_rejected_mw = True
                    log.warning(
                        "field_snap_rejected_motorway km=%.1f",
                        route.get("motorway_km", 0.0),
                    )
                else:
                    route["_field_label"] = "snap_fallback"
                    route["_corridor_kind"] = corridors[0].kind
                    route["_snap_method"] = snap_method
                    routes.append(route)
                    tried.append({
                        "label": "snap_fallback",
                        "corridor_kind": corridors[0].kind,
                    })
            except Exception:
                log.warning("field_snap_failed", exc_info=True)

    if not routes:
        _emergency_osrm_routes(
            a, b, corridors, scores,
            add_routes=_add_routes,
            deadline=deadline,
            emit=_emit,
        )

    if not routes:
        if snap_rejected_mw and avoid_motorways:
            return None, tried, "avoid_motorways_snap_rejected"
        return None, tried, "no_candidates"

    pool = list(routes)
    if avoid_motorways:
        zero = filter_zero_motorway(pool)
        if zero:
            pool = zero
        elif any(r.get("_field_label") == "snap_fallback" for r in pool):
            # Never return catastrophic snap when avoid_motorways is on.
            return None, tried, "avoid_motorways_snap_rejected"
        else:
            # All OSRM candidates still use motorways — try one more direct pass.
            _emergency_osrm_routes(
                a, b, corridors, scores,
                add_routes=_add_routes,
                deadline=deadline,
                emit=_emit,
            )
            zero = filter_zero_motorway(routes)
            if zero:
                pool = zero
            else:
                pool = [
                    r for r in routes if r.get("_field_label") != "snap_fallback"
                ] or list(routes)

    _emit({"type": "phase", "phase": "score", "label": "Scoring road candidates…"})
    for rt in pool:
        if _budget_hit() and rt.get("avg_scenic_score") is not None:
            continue
        try:
            score_route(
                rt, features=features, source=source, weights=weights,
                sample_spacing_km=SAMPLE_SPACING_KM,
                max_samples=MAX_SAMPLES,
                colour=True,
            )
        except Exception:
            log.warning("field_score_route_failed", exc_info=True)
            rt["avg_scenic_score"] = float(rt.get("avg_scenic_score") or 50.0)

    scored = [r for r in pool if r.get("avg_scenic_score") is not None]
    if not scored:
        return None, tried, "score_failed"

    if avoid_motorways and not any(is_zero_motorway(r) for r in scored):
        scored.sort(
            key=lambda r: (
                float(r.get("motorway_km", 0.0) or 0.0),
                _rank_key(r, preference),
            )
        )
    else:
        scored.sort(key=lambda r: _rank_key(r, preference))
    chosen = scored[0]
    reason = (
        f"best_road_score among {len(scored)} candidates"
        f" ({chosen.get('_field_label', 'route')})"
    )
    if avoid_motorways and is_zero_motorway(chosen):
        reason += "; avoid_motorways"
    elif avoid_motorways:
        reason += "; avoid_motorways_unmet"
    return chosen, tried, reason


def plan_field(
    a: tuple[float, float],
    b: tuple[float, float],
    preference: float,
    *,
    profile_id: str | None = None,
    weights: dict | None = None,
    source: str = "esri",
    time_budget: bool = True,
    include_grid: bool = False,
    avoid_motorways: bool = False,
    progress=None,
) -> dict:
    """Wide square heatmap → green corridors → OSRM tournament → road score pick."""
    preference = max(0.0, min(1.0, float(preference)))
    use_time_budget = bool(time_budget)
    plan_t0 = time.perf_counter()
    budget_sec = (
        config.FIELD_BUDGET_SEC
        if config.FIELD_BUDGET_SEC > 0
        else config.PLAN_BUDGET_SEC
    )
    work_deadline = (
        plan_t0 + budget_sec - config.PLAN_RESERVE_SEC
        if use_time_budget else None
    )
    # Hold a slim OSRM floor out of landcover. Colour still gets FIELD_COLOUR_MIN_SEC
    # via adaptive reserve after landcover returns — cold Overpass needs ~12s+ for
    # even one dense-UK tag batch on the public mirrors.
    osrm_reserve = float(config.FIELD_OSRM_RESERVE_SEC)
    colour_floor = float(config.FIELD_COLOUR_MIN_SEC)
    landcover_deadline = (
        work_deadline - float(config.FIELD_OSRM_RESERVE_MIN_SEC)
        if work_deadline is not None else None
    )
    budget_reasons: list[str] = []
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    climate = select_climate(mid[0], mid[1])

    def _emit(ev):
        if progress:
            try:
                progress(ev)
            except Exception:
                pass

    _emit({
        "type": "start",
        "from": list(a),
        "to": list(b),
        "planner": "field",
        "time_budget": use_time_budget,
        "avoid_motorways": avoid_motorways,
    })
    _emit({"type": "phase", "phase": "grid", "label": "Building scenic heatmap…"})

    spec = corridor_spec(a, b)
    cells = build_corridor_cells(spec, a, b)
    if not cells:
        return {"error": "Corridor grid is empty."}

    def _lc_progress(done, total):
        cold = total > 0 and done < total
        _emit({
            "type": "landcover",
            "done": done,
            "total": total,
            "cold": cold,
        })

    bbox = spec.bbox
    # Corridor strip for Overpass: full-square heatmap still scores colour, but
    # map-context tiles hug A→B so short UK hops hit 1–2 tiles instead of 6+
    # cold corners that 504 under the budget.
    lc_half = float(config.LANDCOVER_CORRIDOR_HALF_WIDTH_DEG)
    lc_t0 = time.perf_counter()
    features = enrich.fetch_landcover(
        bbox,
        progress=_lc_progress,
        prefer_axis=(a, b),
        corridor_half_width_deg=lc_half,
        deadline=landcover_deadline,
    )
    landcover_elapsed = time.perf_counter() - lc_t0
    landcover_incomplete = bool(
        features is None
        or (
            features.get("deadline_stopped")
            or features.get("truncated")
            or features.get("landcover_incomplete")
        )
    )
    if features is None:
        budget_reasons.append("landcover_unavailable")
    elif landcover_incomplete:
        budget_reasons.append("landcover_truncated")

    # Count warm field-cell cache hits so adaptive reserve can free colour time.
    cache_hits = sum(
        1 for c in cells if _cell_cache_get(c.lat, c.lng, profile_id) is not None
    )
    adaptive_reserve = (
        _adaptive_osrm_reserve_sec(
            landcover_elapsed=landcover_elapsed,
            landcover_incomplete=landcover_incomplete,
            cache_hits=cache_hits,
            n_cells=len(cells),
        )
        if use_time_budget else osrm_reserve
    )
    colour_deadline = (
        work_deadline - adaptive_reserve
        if work_deadline is not None else None
    )

    heat = _score_corridor_cells(
        cells, features,
        a=a, b=b,
        weights=weights,
        profile_id=profile_id,
        source=source,
        progress=_emit,
        deadline=colour_deadline,
        cell_deg=spec.cell_deg,
    )
    scores = heat.scores
    if heat.proxy_cells and heat.colour_cells < len(cells):
        if heat.colour_cells == 0:
            if "colour_budget" not in budget_reasons:
                budget_reasons.append("colour_budget")
        elif work_deadline is not None and time.perf_counter() >= work_deadline:
            budget_reasons.append("colour_budget")

    mask = green_mask_stats(cells, scores)
    reject_thr = field_reject_threshold()
    reject_cells = sum(
        1 for c in cells if is_reject_score(scores.get(c.id, 50.0), reject_thr)
    )

    _emit({"type": "phase", "phase": "path", "label": "Detecting green corridors…"})
    g = build_corridor_graph(
        cells, scores, preference,
        detour_factor=float(config.FIELD_DETOUR_FACTOR),
        a=a, b=b,
    )
    # Soft-connect fallback if reject floor disconnected A→B entirely.
    if g.number_of_nodes() == 0 or find_field_path(
        g, a, b, preference, detour_factor=float(config.FIELD_DETOUR_FACTOR),
    ).get("error"):
        g = _build_soft_reject_graph(
            cells, scores,
            detour_factor=float(config.FIELD_DETOUR_FACTOR),
            reject_thr=reject_thr,
        )
        budget_reasons.append("reject_soft_connect")

    extract_corridors.last_rejected = []  # type: ignore[attr-defined]
    corridors = extract_corridors(g, cells, scores, a, b, preference)
    corridors_rejected = list(getattr(extract_corridors, "last_rejected", []) or [])
    if not corridors:
        path_info = find_field_path(
            g, a, b, preference, detour_factor=float(config.FIELD_DETOUR_FACTOR),
        )
        if path_info.get("error"):
            return {"error": path_info["error"]}
        corridors = [FieldCorridor(
            kind="green_primary",
            vertices=path_info["vertices"],
            path_nodes=path_info["path_nodes"],
            lattice_km=path_info["lattice_km"],
            lattice_avg_scenic=path_info["lattice_avg_scenic"],
        )]
        frac = corridor_reject_fraction(path_info["path_nodes"], scores, reject_thr)
        if frac > float(config.FIELD_REJECT_MAX_FRAC):
            corridors_rejected.append({
                "kind": "green_primary_fallback",
                "reason": "urban_bad_colour",
                "reject_frac": round(frac, 3),
            })

    primary = next((c for c in corridors if c.kind == "green_primary"), corridors[0])
    lattice_avg = primary.lattice_avg_scenic

    chosen_route, candidates_tried, chosen_reason = _osrm_tournament(
        a, b, corridors, scores,
        features=features,
        weights=weights,
        source=source,
        preference=preference,
        avoid_motorways=avoid_motorways,
        deadline=work_deadline,
        progress=_emit,
    )
    if chosen_route is None:
        return {"error": "Could not build a driveable field route."}
    if work_deadline is not None and time.perf_counter() >= work_deadline:
        if "osrm_budget" not in budget_reasons:
            budget_reasons.append("osrm_budget")

    road_score = float(chosen_route.get("avg_scenic_score") or 0.0)
    snap_delta = round(road_score - lattice_avg, 1)

    elapsed_ms = round((time.perf_counter() - plan_t0) * 1000.0, 1)
    land_used = bool(chosen_route.get("_landcover_usable"))
    # Also treat heatmap landcover as usable when Overpass returned influence
    # even if the OSRM winner's sample set barely touches it.
    if not land_used and features is not None:
        sample_pts = [(c.lat, c.lng) for c in cells[:: max(1, len(cells) // 40)]]
        land_used = enrich.landcover_is_usable(features, sample_pts or None)
    land_incomplete = features is None or bool(features.get("truncated")) or (
        features is not None and not land_used
    )
    climate_id = chosen_route.get("climate") or climate.id
    summary = _route_summary(chosen_route, chosen=True, min_scenic=0.0)

    motorway_avoid_met = True
    motorway_avoid_reason = "not_requested"
    if avoid_motorways:
        motorway_avoid_met = is_zero_motorway(chosen_route)
        motorway_avoid_reason = (
            "motorway_free_candidate_found"
            if motorway_avoid_met
            else "all_candidates_include_motorways"
        )

    lc_feat_count = 0
    lc_tiles_ok = 0
    lc_tiles_req = 0
    if features is not None:
        lc_feat_count = int(
            features.get("feature_count")
            or (
                int(features["pos"].shape[0]) + int(features["neg"].shape[0])
            )
        )
        lc_tiles_ok = int(features.get("tiles_ok") or 0)
        lc_tiles_req = int(features.get("tiles_requested") or 0)

    field_meta = {
        "bbox": [round(x, 5) for x in bbox],
        "cell_deg": round(spec.cell_deg, 5),
        "cells_scored": len(cells),
        "proxy_cells": heat.proxy_cells,
        "colour_cells": heat.colour_cells,
        # True only when no Esri/cache colour landed — partial colour is not "proxy-only".
        "heatmap_proxy_only": bool(heat.colour_cells == 0 and heat.proxy_cells > 0),
        "osrm_reserve_sec": round(adaptive_reserve, 2),
        "colour_floor_sec": round(colour_floor, 2) if use_time_budget else None,
        "landcover_elapsed_sec": round(landcover_elapsed, 2),
        "landcover_usable": bool(land_used),
        "landcover_features": lc_feat_count,
        "landcover_tiles_ok": lc_tiles_ok,
        "landcover_tiles_requested": lc_tiles_req,
        "green_mask": mask,
        "reject_scenic": reject_thr,
        "reject_cells": reject_cells,
        "reject_fraction": round(reject_cells / len(cells), 3) if cells else 0.0,
        "corridors_rejected": corridors_rejected,
        "green_corridors": [
            {
                "kind": c.kind,
                "lattice_km": c.lattice_km,
                "lattice_avg_scenic": c.lattice_avg_scenic,
                "via_hint": list(c.via_hint) if c.via_hint else None,
                "reject_frac": round(
                    corridor_reject_fraction(c.path_nodes, scores, reject_thr), 3,
                ),
            }
            for c in corridors
        ],
        "candidates_tried": candidates_tried,
        "candidates_tried_n": len(candidates_tried),
        "lattice_km": primary.lattice_km,
        "lattice_avg_scenic": lattice_avg,
        "road_score": road_score,
        "snap_delta_scenic": snap_delta,
        "chosen_reason": chosen_reason,
        "road_snap_method": chosen_route.get("_snap_method") or chosen_route.get(
            "_field_label", "osrm",
        ),
        "budget_reasons": list(budget_reasons),
        "avoid_motorways": avoid_motorways,
        "motorway_avoid_reason": motorway_avoid_reason,
    }
    if corridors_rejected:
        # Honesty: note that urban/bad-colour corridors were discarded.
        if "urban_reject" not in chosen_reason:
            field_meta["chosen_reason"] = (
                f"{chosen_reason}; urban_corridors_rejected={len(corridors_rejected)}"
            )

    result = {
        "source": "field",
        "planner": "field",
        "preference": preference,
        "profile": profile_id,
        "climate": climate_id,
        "min_scenic": 0.0,
        "min_scenic_met": True,
        "avoid_motorways": avoid_motorways,
        "motorway_avoid_met": motorway_avoid_met,
        "motorway_avoid_reason": motorway_avoid_reason,
        "time_budget": use_time_budget,
        "from": list(a),
        "to": list(b),
        "elapsed_ms": elapsed_ms,
        "budget_exhausted": bool(budget_reasons),
        "budget_reasons": list(budget_reasons),
        "signals": {
            "colour": True,
            "terrain": (chosen_route.get("components") or {}).get("terrain") is not None,
            "landcover": land_used,
            "landcover_incomplete": land_incomplete,
            "landcover_features": lc_feat_count,
            "motorway_free_candidates": int(
                sum(1 for r in [chosen_route] if is_zero_motorway(r))
            ) if avoid_motorways else None,
            "motorway_candidates_total": 1 if avoid_motorways else None,
            "climate": climate_id,
            "climate_name": climate_display_name(climate_id),
            "climates_used": chosen_route.get("climates_used") or [climate_id],
        },
        "chosen": summary,
        "alternatives": [summary],
        "field_meta": field_meta,
    }
    if include_grid:
        result["field_cells"] = [
            {
                "lat": c.lat, "lng": c.lng,
                "score": scores.get(c.id, 50.0),
                "source": heat.sources.get(c.id, "unknown"),
            }
            for c in cells
        ]
    _emit({"type": "done", "result": result})
    return result


def plan_field_events(*args, **kwargs):
    """Run plan_field in a worker thread; yield progress events."""
    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def cb(ev):
        q.put(ev)

    def run():
        try:
            plan_field(*args, progress=cb, **kwargs)
        except Exception:
            log.warning("plan_field_events_failed", exc_info=True)
            q.put({"type": "error", "message": "Field routing failed."})
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=run, daemon=True).start()
    while True:
        ev = q.get()
        if ev is _SENTINEL:
            break
        yield ev
