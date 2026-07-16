"""Real-road routing (OSRM) + enriched scenic scoring & scenic route search.

For any A->B we:
  1. Ask OSRM (real OSM road network) for driving routes, and additionally
     build *scenic* candidates by routing through scenic "hotspot" waypoints.
  2. Rank candidates cheaply (landcover + terrain), colour a top-K shortlist
     during search, colour every final alternative at rank density, then refine
     the chosen route at full sample density.
  3. Return the candidate that best matches the user's scenic preference.

Works anywhere OSRM/Overpass/Open-Meteo have coverage (all of the UK, most of
the world) and needs no precomputed grid. Every external signal degrades
gracefully; colour always works so a route can always be scored.
"""
from __future__ import annotations

import copy
import logging
import math
import re
import queue
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests

from . import config, scoring, enrich
from .attractors import all_attractors
from .climates import select_climate, climate_display_name

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"User-Agent": "ScenicRoutePlanner/1.0"})

# Scenic sampling controls (full refine density; ranking uses config.*_RANK).
SAMPLE_SPACING_KM = config.SAMPLE_SPACING_KM
MAX_SAMPLES = config.MAX_SAMPLES
RENDER_MAX_POINTS = 400     # simplify geometry sent to the browser
SCORE_WORKERS = 16

# In-process OSRM response cache (TTL + size from config, same pattern as elev).
_OSRM_CACHE: OrderedDict = OrderedDict()
_OSRM_CACHE_LOCK = threading.Lock()


def _osrm_cache_get(key):
    with _OSRM_CACHE_LOCK:
        entry = _OSRM_CACHE.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts >= config.CACHE_TTL_SEC:
            _OSRM_CACHE.pop(key, None)
            return None
        _OSRM_CACHE.move_to_end(key)
        return value


def _osrm_cache_set(key, value) -> None:
    with _OSRM_CACHE_LOCK:
        _OSRM_CACHE[key] = (time.monotonic(), value)
        _OSRM_CACHE.move_to_end(key)
        while len(_OSRM_CACHE) > config.CACHE_MAX_ENTRIES:
            _OSRM_CACHE.popitem(last=False)


def _osrm_cache_key(a, b, vias, alternatives: int, continue_straight=None):
    pts = [(round(a[0], 5), round(a[1], 5))]
    for w in vias:
        pts.append((round(w[0], 5), round(w[1], 5)))
    pts.append((round(b[0], 5), round(b[1], 5)))
    alt = 0 if vias else int(alternatives)
    cs = None
    if continue_straight is not None:
        if isinstance(continue_straight, bool):
            cs = continue_straight
        else:
            cs = tuple(bool(x) for x in continue_straight)
    return (tuple(pts), alt, cs)


def _osrm_match_cache_key(trace_points, radius_m: float):
    pts = tuple((round(p[0], 5), round(p[1], 5)) for p in trace_points)
    return ("match", pts, round(float(radius_m), 1))


def _parse_osrm_route(rt: dict) -> dict:
    """Normalise one OSRM route/matching object into our internal route dict."""
    coords_ll = [(c[1], c[0]) for c in rt["geometry"]["coordinates"]]
    return {
        "coords": coords_ll,
        "distance_km": rt["distance"] / 1000.0,
        "duration_min": rt["duration"] / 60.0,
        "directions": _directions_from_legs(rt.get("legs", [])),
        "motorway_km": _motorway_km(rt.get("legs", [])),
    }


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_osrm_routes(a: tuple[float, float], b: tuple[float, float],
                    alternatives: int = 3,
                    waypoint: tuple[float, float] | None = None,
                    waypoints: list[tuple[float, float]] | None = None,
                    continue_straight: bool | list[bool] | None = None,
                    exclude_motorway: bool = False) -> list[dict]:
    """Return real road routes A->B (optionally via waypoints). Coords (lat,lng).

    `waypoint` routes via a single point; `waypoints` chains several ordered
    intermediate points (used to force a path through multiple scenic zones).

    When ``continue_straight`` is set with via points, OSRM forbids U-turns at
    those intermediates (draw-mode primary snap follows click order this way).
    """
    vias = list(waypoints) if waypoints else ([waypoint] if waypoint else [])
    cache_on = bool(config.OSRM_CACHE)
    cache_key = (
        _osrm_cache_key(a, b, vias, alternatives, continue_straight)
        if cache_on else None
    )
    if cache_key is not None:
        hit = _osrm_cache_get(cache_key)
        if hit is not None:
            return copy.deepcopy(hit)

    req_alternatives = alternatives
    if not vias:
        coords = f"{a[1]},{a[0]};{b[1]},{b[0]}"  # OSRM wants lng,lat
    else:
        mid = ";".join(f"{w[1]},{w[0]}" for w in vias)
        coords = f"{a[1]},{a[0]};{mid};{b[1]},{b[0]}"
        req_alternatives = 0  # OSRM disallows alternatives with intermediate waypoints
    url = config.OSRM_URL.format(coords=coords)
    params = {
        "overview": "full",
        "geometries": "geojson",
        "alternatives": "true" if req_alternatives else "false",
        "steps": "true",  # turn-by-turn maneuvers for Google-Maps-style directions
    }
    if req_alternatives:
        params["alternatives"] = str(req_alternatives)
    if continue_straight is not None and vias:
        if isinstance(continue_straight, bool):
            params["continue_straight"] = "true" if continue_straight else "false"
        else:
            params["continue_straight"] = ";".join(
                "true" if x else "false" for x in continue_straight
            )
    if exclude_motorway and config.OSRM_EXCLUDE_MOTORWAY:
        params["exclude"] = "motorway"
    try:
        r = _session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception:
        if exclude_motorway and config.OSRM_EXCLUDE_MOTORWAY:
            # Public/demo OSRM rejects exclude=motorway — retry without it.
            params.pop("exclude", None)
            r = _session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        else:
            log.warning("osrm_request_failed url=%s", url.split("?")[0], exc_info=True)
            raise
    if data.get("code") != "Ok" or not data.get("routes"):
        msg = data.get("message", "OSRM returned no route.")
        log.warning("osrm_no_route code=%s message=%s", data.get("code"), msg)
        raise RuntimeError(msg)
    out = [_parse_osrm_route(rt) for rt in data["routes"]]
    if cache_key is not None:
        _osrm_cache_set(cache_key, out)
        return copy.deepcopy(out)
    return out


def get_osrm_match_route(
    trace_points: list[tuple[float, float]],
    *,
    radius_m: float | None = None,
) -> dict | None:
    """Snap a GPS-like trace to the road network via OSRM Map Matching.

    Returns a single route dict (same shape as ``get_osrm_routes``) or None when
    matching fails. Trace points are (lat, lng); synthetic timestamps preserve
    click order along the sketch.
    """
    if len(trace_points) < 2:
        return None
    rad = config.DRAW_MATCH_RADIUS_M if radius_m is None else float(radius_m)
    cache_on = bool(config.OSRM_CACHE)
    cache_key = _osrm_match_cache_key(trace_points, rad) if cache_on else None
    if cache_key is not None:
        hit = _osrm_cache_get(cache_key)
        if hit is not None:
            return copy.deepcopy(hit)

    coords = ";".join(f"{lng},{lat}" for lat, lng in trace_points)
    url = config.OSRM_MATCH_URL.format(coords=coords)
    # Monotonic timestamps (1 s per km between points) keep trace order for OSRM.
    ts: list[int] = [0]
    for i in range(1, len(trace_points)):
        seg_km = _haversine_km(*trace_points[i - 1], *trace_points[i])
        ts.append(ts[-1] + max(1, int(round(seg_km * 1000))))
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
        "timestamps": ";".join(str(t) for t in ts),
        "radiuses": ";".join(str(int(round(rad))) for _ in trace_points),
        "gaps": "ignore",
    }
    try:
        r = _session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.warning("osrm_match_request_failed url=%s", url.split("?")[0], exc_info=True)
        return None
    if data.get("code") != "Ok" or not data.get("matchings"):
        log.warning(
            "osrm_match_no_route code=%s message=%s",
            data.get("code"),
            data.get("message"),
        )
        return None
    out = _parse_osrm_route(data["matchings"][0])
    if cache_key is not None:
        _osrm_cache_set(cache_key, out)
        return copy.deepcopy(out)
    return out

# Motorway detection. Prefer OSRM intersection `classes` when present; then
# national ref styles and name keywords. Bare UK A-roads (A6) are NOT motorways —
# only M-roads / A*(M) and clear motorway class/keywords count.
_MOTORWAY_RE = re.compile(r"(?:^|[\s;])M\d|\(M\)", re.I)
_INTERSTATE_RE = re.compile(r"(?:^|[\s;])I[-\s]?\d", re.I)
_MOTORWAY_NAME_RE = re.compile(
    # Allow compound forms (Bundesautobahn, …); keep word bounds for English terms.
    r"(?:autobahn|autoroute|autostrada|autopista|"
    r"\b(?:interstate|motorway|expressway|freeway|parkway|tangenziale)\b)",
    re.I,
)
# Continental A-numbered motorways (DE/FR/IT/…) only with motorway name context —
# never bare "A6" alone (UK A-roads).
_CONTINENTAL_A_RE = re.compile(r"(?:^|[\s;])A\d{1,3}(?:$|[\s;/])", re.I)


def _step_classes(step: dict) -> set[str]:
    classes: set[str] = set()
    for inter in step.get("intersections") or []:
        for c in inter.get("classes") or []:
            classes.add(str(c).lower())
    return classes


def _is_motorway(ref: str | None, name: str | None,
                 classes: set[str] | None = None) -> bool:
    if classes and "motorway" in classes:
        return True
    text = f"{ref or ''} {name or ''}"
    if _MOTORWAY_NAME_RE.search(text):
        return True
    # UK/IE M-roads and A*(M); also AU M\d refs share the M\d pattern.
    if _MOTORWAY_RE.search(text):
        return True
    if _INTERSTATE_RE.search(text):
        return True
    # DE/FR-style A\d only when the name also says motorway/autobahn/autoroute.
    if ref and _CONTINENTAL_A_RE.search(ref) and name and _MOTORWAY_NAME_RE.search(name):
        return True
    return False


def _motorway_km(legs: list[dict]) -> float:
    """Kilometres of the route driven on motorways."""
    total = 0.0
    for leg in legs:
        for step in leg.get("steps", []):
            if _is_motorway(step.get("ref"), step.get("name"), _step_classes(step)):
                total += float(step.get("distance", 0.0)) / 1000.0
    return round(total, 2)


def _fmt_dist(m: float) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{int(round(m))} m"


def _step_instruction(step: dict) -> str:
    """Turn an OSRM maneuver step into a readable instruction."""
    man = step.get("maneuver", {})
    mtype = man.get("type", "")
    mod = man.get("modifier", "")
    road = step.get("name") or step.get("ref") or ""
    onto = f" onto {road}" if road else ""
    on = f" on {road}" if road else ""
    if mtype == "depart":
        return "Head off" + (f" on {road}" if road else "")
    if mtype == "arrive":
        return "Arrive at your destination"
    if mtype in ("roundabout", "rotary"):
        ex = man.get("exit")
        return (f"At the roundabout, take exit {ex}" + onto) if ex else ("Enter the roundabout" + onto)
    if mtype == "roundabout turn":
        return f"At the roundabout, turn {mod}".rstrip() + onto
    if mtype == "turn":
        return f"Turn {mod}".rstrip() + onto
    if mtype == "end of road":
        return f"At the end of the road, turn {mod}".rstrip() + onto
    if mtype == "continue":
        return (f"Continue {mod}".rstrip() + on) or "Continue"
    if mtype == "new name":
        return "Continue" + on
    if mtype == "merge":
        return f"Merge {mod}".rstrip() + onto
    if mtype in ("on ramp", "off ramp"):
        which = "slip road onto" if mtype == "on ramp" else "exit slip road"
        return f"Take the {which} {mod}".rstrip() + (onto if mtype == "on ramp" else on)
    if mtype == "fork":
        return f"Keep {mod}".rstrip() + onto
    label = f"{mtype} {mod}".strip().capitalize()
    return (label + onto).strip() or "Continue"


def _directions_from_legs(legs: list[dict]) -> list[dict]:
    """Flatten OSRM legs into a single readable turn list (A -> ... -> B)."""
    steps = [s for leg in legs for s in leg.get("steps", [])]
    n = len(steps)
    out = []
    for idx, step in enumerate(steps):
        mtype = step.get("maneuver", {}).get("type")
        # Drop intermediate depart/arrive introduced by via-waypoints.
        if mtype == "depart" and idx != 0:
            continue
        if mtype == "arrive" and idx != n - 1:
            continue
        loc = step.get("maneuver", {}).get("location", [None, None])
        dist = float(step.get("distance", 0.0))
        out.append({
            "text": _step_instruction(step),
            "distance_label": _fmt_dist(dist),
            "lat": loc[1] if len(loc) == 2 else None,
            "lng": loc[0] if len(loc) == 2 else None,
        })
    return out


def _cumulative_km(coords: list[tuple[float, float]]) -> list[float]:
    cum = [0.0]
    for i in range(1, len(coords)):
        d = _haversine_km(*coords[i - 1], *coords[i])
        cum.append(cum[-1] + d)
    return cum


def _sample_indices(cum: list[float], spacing_km: float, max_samples: int) -> list[int]:
    total = cum[-1]
    if total <= 0:
        return [0]
    n = min(max_samples, max(2, int(total / spacing_km) + 1))
    targets = [total * i / (n - 1) for i in range(n)]
    idxs, j = [], 0
    for t in targets:
        while j < len(cum) - 1 and cum[j] < t:
            j += 1
        idxs.append(j)
    # de-dup while preserving order
    seen, uniq = set(), []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def _dist_weighted_avg(cum, scored: dict[int, float]) -> float:
    ordered = sorted(scored)
    total_w, acc = 0.0, 0.0
    for k, i in enumerate(ordered):
        seg = 0.0
        if k > 0:
            seg += (cum[i] - cum[ordered[k - 1]]) / 2
        if k < len(ordered) - 1:
            seg += (cum[ordered[k + 1]] - cum[i]) / 2
        seg = max(seg, 1e-6)
        acc += scored[i] * seg
        total_w += seg
    return acc / total_w if total_w else 0.0


def blend_signals(colour: float, terrain: float | None, landcover: float | None,
                  w_colour: float | None = None, w_terrain: float | None = None,
                  w_land: float | None = None) -> float:
    """Blend available scenic signals, renormalising weights over present values."""
    wc = config.BLEND_COLOUR if w_colour is None else w_colour
    wt = config.BLEND_TERRAIN if w_terrain is None else w_terrain
    wl = config.BLEND_LANDCOVER if w_land is None else w_land
    vals = []
    if wc > 0:
        vals.append((colour, wc))
    if terrain is not None and wt > 0:
        vals.append((terrain, wt))
    if landcover is not None and wl > 0:
        vals.append((landcover, wl))
    if not vals:
        # Degenerate (all weights zero / all signals missing): fall back to colour.
        return float(colour)
    wsum = sum(w for _, w in vals) or 1.0
    return sum(v * w for v, w in vals) / wsum


def route_cost(rt: dict, preference: float, detour: float, mw_pen: float = 0.0) -> float:
    """Ranking cost: duration inflated by low scenery, plus optional motorway penalty."""
    preference = max(0.0, min(1.0, float(preference)))
    penalty = detour * (1.0 - rt["avg_scenic_score"] / 100.0)
    return rt["duration_min"] * (1.0 + preference * penalty) + mw_pen * rt.get("motorway_km", 0.0)


def _motorway_eps() -> float:
    return float(getattr(config, "MOTORWAY_EPS_KM", 0.05))


def is_zero_motorway(rt: dict, eps: float | None = None) -> bool:
    """True when motorway mileage is zero (or within eps junction noise)."""
    lim = _motorway_eps() if eps is None else float(eps)
    return float(rt.get("motorway_km", 0.0) or 0.0) <= lim


def filter_zero_motorway(routes: list[dict], eps: float | None = None) -> list[dict]:
    """Keep only routes with essentially zero motorway km."""
    return [r for r in routes if is_zero_motorway(r, eps=eps)]


def select_chosen(routes: list[dict], min_scenic: float = 0.0,
                  avoid_motorways: bool = False) -> tuple[dict, bool]:
    """Pick the recommended route from an already-ranked candidate list.

    Assumes ``routes`` is sorted best-first (cost or explore/scenic order).

    When ``avoid_motorways`` is set and any zero-motorway candidate exists, the
    pool is restricted to those routes — motorway OSRM alts can never win.

    With a min-scenic floor: return the first qualifying route in that pool
    (caller should cost-rank qualifying preference). If none qualify, return
    the **most scenic** route (never the fastest/lowest-cost loser) and
    ``min_scenic_met=False``.
    """
    if not routes:
        raise ValueError("no routes to choose from")
    min_scenic = max(0.0, min(100.0, float(min_scenic)))
    pool = routes
    if avoid_motorways:
        zero = filter_zero_motorway(routes)
        if zero:
            pool = zero
    if min_scenic > 0.0:
        qualifying = [r for r in pool if r["avg_scenic_score"] >= min_scenic]
        if qualifying:
            return qualifying[0], True
        # Last resort: highest scenic among the (possibly zero-mw) pool — not
        # best travel-time / cost.
        return max(
            pool,
            key=lambda r: (
                r["avg_scenic_score"],
                -float(r.get("motorway_km", 0.0) or 0.0),
                -float(r.get("duration_min", 0.0) or 0.0),
            ),
        ), False
    return pool[0], True


def score_route(route: dict, features: dict | None = None, source: str = "esri",
                weights: dict | None = None,
                climate=None,
                sample_spacing_km: float | None = None,
                max_samples: int | None = None,
                colour: bool = True,
                reuse_meta: bool = False) -> dict:
    """Score a route by blending colour density, terrain relief and land cover.

    weights (optional) overrides the blend, e.g. {"colour":.4,"terrain":.3,"landcover":.3}.
    climate (optional ColourClimate or id) forces one climate for all samples;
    otherwise each sample looks up its own climate (with elevation alpine overlay).
    Blend weights use the majority sample climate when no profile override.

    When ``colour`` is False, only landcover + terrain are blended (proxy rank).
    ``sample_spacing_km`` / ``max_samples`` override the default full-density
    sampling (use coarser values for ranking shortlist colour).

    When ``reuse_meta`` is True and ``_score_meta`` is present, elevation /
    climate / colour for sample indices already cached are kept; only missing
    denser samples (and Esri for uncoloured indices) are fetched.
    """
    coords = route["coords"]
    cum = _cumulative_km(coords)
    spacing = SAMPLE_SPACING_KM if sample_spacing_km is None else sample_spacing_km
    cap = MAX_SAMPLES if max_samples is None else max_samples
    sample_idx = _sample_indices(cum, spacing, cap)

    prev = route.get("_score_meta") if reuse_meta else None
    prev_elev = (prev.get("elev_by_idx") or {}) if prev else {}
    prev_colour = (prev.get("colour") or {}) if prev else {}
    prev_climates = (prev.get("climates_by_idx") or {}) if prev else {}

    # Terrain: reuse elev for shared sample indices; fetch only missing points.
    elev_by_idx: dict[int, float] = {}
    need_elev_idx: list[int] = []
    for i in sample_idx:
        if i in prev_elev:
            elev_by_idx[i] = float(prev_elev[i])
        else:
            need_elev_idx.append(i)

    if need_elev_idx:
        elevs = enrich.elevation_batch([coords[i] for i in need_elev_idx])
        if elevs and len(elevs) == len(need_elev_idx):
            for i, v in zip(need_elev_idx, elevs):
                elev_by_idx[i] = float(v)

    terrain: dict[int, float] | None = None
    elev_ok = len(elev_by_idx) == len(sample_idx) and bool(sample_idx)
    if elev_ok:
        elevs_ordered = [elev_by_idx[i] for i in sample_idx]
        sample_km = [cum[i] for i in sample_idx]
        rs = enrich.relief_scores(elevs_ordered, sample_km=sample_km)
        terrain = {sample_idx[k]: rs[k] for k in range(len(sample_idx))}
    else:
        elev_by_idx = None  # type: ignore[assignment]

    # Per-sample colour climate (HSV); forced climate overrides lookup.
    climates_by_idx: dict[int, object] = {}
    for i in sample_idx:
        lat, lng = coords[i]
        elev = elev_by_idx[i] if elev_by_idx is not None else None
        if climate is not None:
            climates_by_idx[i] = (
                climate if not isinstance(climate, str)
                else select_climate(lat, lng, climate_id=climate, elev_m=elev)
            )
        elif i in prev_climates and elev is not None:
            climates_by_idx[i] = select_climate(
                lat, lng, climate_id=prev_climates[i], elev_m=elev,
            )
        elif i in prev_climates and elev is None:
            climates_by_idx[i] = select_climate(
                0.0, 0.0, climate_id=prev_climates[i],
            )
        else:
            climates_by_idx[i] = select_climate(lat, lng, elev_m=elev)

    # Majority climate for stable blend weights on multi-climate routes.
    majority_id = Counter(c.id for c in climates_by_idx.values()).most_common(1)[0][0]
    majority = select_climate(0.0, 0.0, climate_id=majority_id)
    climates_used = sorted({c.id for c in climates_by_idx.values()})

    if weights:
        w_colour = weights["colour"]
        w_terrain = weights["terrain"]
        w_land = weights["landcover"]
    else:
        w_colour = majority.blend_colour
        w_terrain = majority.blend_terrain
        w_land = majority.blend_landcover

    # 1. Colour (satellite) — skipped in proxy phase; reuse cached tiles on refine.
    colour_scores: dict[int, float] = {}
    if colour:
        need_colour = []
        for i in sample_idx:
            if i in prev_colour:
                colour_scores[i] = float(prev_colour[i])
            else:
                need_colour.append(i)

        def _colour(i):
            lat, lng = coords[i]
            return i, scoring.score_location(
                lat, lng, source=source, climate=climates_by_idx[i]
            ).score

        if need_colour:
            with ThreadPoolExecutor(max_workers=SCORE_WORKERS) as ex:
                for i, sc in ex.map(_colour, need_colour):
                    colour_scores[i] = sc

    # Cache raw signals so pad-widen can re-blend landcover without Esri re-fetch.
    route["_score_meta"] = {
        "sample_idx": list(sample_idx),
        "cum": cum,
        "colour": dict(colour_scores),
        "terrain": dict(terrain) if terrain is not None else None,
        "elev_by_idx": dict(elev_by_idx) if elev_by_idx is not None else None,
        "climates_by_idx": {i: c.id for i, c in climates_by_idx.items()},
        "w_colour": w_colour,
        "w_terrain": w_terrain,
        "w_land": w_land,
        "colour_enabled": colour,
        "sample_spacing_km": spacing,
        "max_samples": cap,
        "source": source,
        "majority_id": majority_id,
        "climates_used": climates_used,
    }

    _apply_blend(route, features, colour_scores, terrain, elev_by_idx,
                 climates_by_idx, sample_idx, cum, w_colour, w_terrain, w_land,
                 colour_enabled=colour)
    return route


def _apply_blend(route: dict, features: dict | None,
                 colour_scores: dict[int, float],
                 terrain: dict[int, float] | None,
                 elev_by_idx: dict[int, float] | None,
                 climates_by_idx: dict[int, object],
                 sample_idx: list[int], cum: list[float],
                 w_colour: float, w_terrain: float, w_land: float,
                 colour_enabled: bool) -> None:
    """Write avg_scenic_score / components / render from cached sample signals."""
    coords = route["coords"]
    sample_coords = [coords[i] for i in sample_idx]

    land: dict[int, float] | None = None
    land_detail: dict[int, dict] | None = None
    land_usable = enrich.landcover_is_usable(features, sample_coords)
    if land_usable and features is not None:
        ld = enrich.landcover_details(
            sample_coords,
            features,
            climate_ids=[climates_by_idx[i].id for i in sample_idx],
        )
        land = {sample_idx[k]: ld[k]["score"] for k in range(len(sample_idx))}
        land_detail = {sample_idx[k]: ld[k] for k in range(len(sample_idx))}

    # Proxy phase: omit colour weight so blend renormalises over terrain+land.
    wc = w_colour if colour_enabled else 0.0

    combined: dict[int, float] = {}
    terrain_soft: dict[int, float] | None = None
    if terrain is not None:
        terrain_soft = {}
        for i in sample_idx:
            near = land_detail[i] if land_detail is not None else None
            c_val = colour_scores[i] if colour_enabled and i in colour_scores else 50.0
            terrain_soft[i] = enrich.soften_terrain(
                terrain[i],
                c_val,
                land[i] if land is not None else None,
                near_water=enrich.is_water_near(near),
                climate_id=climates_by_idx[i].id,
            )
    for i in sample_idx:
        c_val = colour_scores[i] if colour_enabled and i in colour_scores else 50.0
        combined[i] = blend_signals(
            c_val,
            terrain_soft[i] if terrain_soft is not None else None,
            land[i] if land is not None else None,
            w_colour=wc, w_terrain=w_terrain, w_land=w_land,
        )

    explain: dict[int, dict] = {}
    for i in sample_idx:
        explain[i] = {
            "colour": round(colour_scores[i]) if colour_enabled and i in colour_scores else None,
            "terrain": round(terrain_soft[i]) if terrain_soft is not None else None,
            "landcover": round(land[i]) if land is not None else None,
            "elev_m": round(elev_by_idx[i]) if elev_by_idx is not None else None,
            "climate": climates_by_idx[i].id,
            "near": land_detail[i] if land_detail is not None else None,
        }

    avg = round(_dist_weighted_avg(cum, combined), 1)
    route["avg_scenic_score"] = avg
    if not colour_enabled:
        route["proxy_scenic"] = avg
    route["components"] = {
        "colour": (
            round(_dist_weighted_avg(cum, colour_scores), 1)
            if colour_enabled and colour_scores else None
        ),
        "terrain": round(_dist_weighted_avg(cum, terrain_soft), 1) if terrain_soft else None,
        "landcover": round(_dist_weighted_avg(cum, land), 1) if land else None,
    }
    meta = route.get("_score_meta") or {}
    route["climate"] = meta.get("majority_id") or (
        Counter(
            (c.id if hasattr(c, "id") else c) for c in climates_by_idx.values()
        ).most_common(1)[0][0]
    )
    route["climates_used"] = meta.get("climates_used") or sorted({
        (c.id if hasattr(c, "id") else c) for c in climates_by_idx.values()
    })
    route["render"] = _render_polyline(coords, cum, combined, explain)
    route["num_samples"] = len(sample_idx)
    route["_landcover_usable"] = land_usable
    route["_colour_scored"] = colour_enabled


def reblend_route_landcover(route: dict, features: dict | None,
                            weights: dict | None = None) -> dict:
    """Recompute landcover (+ soften/blend) from cached samples without Esri fetch.

    Used when the corridor pad widens and landcover features are merged. Colour
    tiles are reused from ``_score_meta``; sample points are unchanged.
    """
    meta = route.get("_score_meta")
    if not meta:
        return route
    sample_idx = meta["sample_idx"]
    cum = meta["cum"]
    colour_scores = meta.get("colour") or {}
    terrain = meta.get("terrain")
    elev_by_idx = meta.get("elev_by_idx")
    climate_ids = meta.get("climates_by_idx") or {}
    climates_by_idx = {
        i: select_climate(0.0, 0.0, climate_id=cid) for i, cid in climate_ids.items()
    }
    if weights:
        w_colour, w_terrain, w_land = (
            weights["colour"], weights["terrain"], weights["landcover"]
        )
    else:
        w_colour = meta["w_colour"]
        w_terrain = meta["w_terrain"]
        w_land = meta["w_land"]
    _apply_blend(
        route, features, colour_scores, terrain, elev_by_idx, climates_by_idx,
        sample_idx, cum, w_colour, w_terrain, w_land,
        colour_enabled=bool(meta.get("colour_enabled")),
    )
    return route


def _fanout_osrm(jobs: list[dict], workers: int | None = None) -> list[dict]:
    """Run get_osrm_routes jobs in parallel; flatten successes (order-independent)."""
    if not jobs:
        return []
    n = workers if workers is not None else config.OSRM_WORKERS
    n = max(1, min(n, len(jobs)))
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(get_osrm_routes, **job) for job in jobs]
        for fut in as_completed(futs):
            try:
                out.extend(fut.result())
            except Exception:
                continue
    return out


def _colour_top_k(pool: list[dict], features: dict | None, source: str,
                  weights: dict | None, top_k: int | None = None,
                  emit=None, kind: str = "colour") -> None:
    """Full colour score (coarse rank density) for top-K by current proxy/avg."""
    k = config.SCENIC_COLOUR_TOP_K if top_k is None else top_k
    need = [rt for rt in pool if not rt.get("_colour_scored")]
    if not need or k <= 0:
        return
    need.sort(key=lambda r: r.get("proxy_scenic", r.get("avg_scenic_score", 0)),
              reverse=True)
    shortlist = need[:k]
    workers = max(1, min(config.SCORE_ROUTE_WORKERS, len(shortlist)))

    def _one(rt):
        score_route(
            rt, features=features, source=source, weights=weights,
            sample_spacing_km=config.SAMPLE_SPACING_KM_RANK,
            max_samples=config.MAX_SAMPLES_RANK,
            colour=True,
            reuse_meta=True,
        )
        return rt

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, rt): rt for rt in shortlist}
        for fut in as_completed(futs):
            rt = fut.result()
            if emit:
                emit({
                    "type": "candidate",
                    "kind": kind,
                    "coords": _decimate(rt["coords"], 80),
                    "scenic": rt["avg_scenic_score"],
                    "distance_km": round(rt["distance_km"], 1),
                    "duration_min": round(rt["duration_min"], 1),
                    "motorway_km": round(rt.get("motorway_km", 0.0), 1),
                    "meets_min": True,  # filled by caller context if needed
                    "total": len(pool),
                    "phase": "colour",
                })


def _nearest_sample_score(pos_km: float, cum: list[float],
                          scored: dict[int, float]) -> float:
    ordered = sorted(scored)
    best_i = min(ordered, key=lambda i: abs(cum[i] - pos_km))
    return scored[best_i]


def _nearest_sample_key(pos_km: float, cum: list[float], keys) -> int:
    return min(keys, key=lambda i: abs(cum[i] - pos_km))


def _grade(score: float) -> str:
    if score >= 75: return "Very scenic"
    if score >= 60: return "Scenic"
    if score >= 45: return "Moderate"
    if score >= 30: return "Low scenery"
    return "Built-up"


def _reason(score: float, comp: dict) -> str:
    """Plain-English explanation of why a point scores the way it does."""
    pos, neg = [], []
    c, t, l = comp.get("colour"), comp.get("terrain"), comp.get("landcover")
    near = comp.get("near") or {}

    if c is not None:
        if c >= 60: pos.append("green, natural colour on satellite")
        elif c <= 40: neg.append("grey, built-up surroundings")
    if t is not None:
        if t >= 60: pos.append("hilly, high relief")
        elif t <= 30 and not enrich.is_water_near(near):
            neg.append("flat ground")
    # Prefer concrete nearby features over the abstract map-context number.
    if near.get("pos_label"):
        d = near.get("pos_dist")
        pos.append(f"near {near['pos_label']}" + (f" ({d} km)" if d is not None else ""))
    elif l is not None and l >= 60:
        pos.append("natural map context")
    if near.get("neg_label"):
        d = near.get("neg_dist")
        neg.append(f"near {near['neg_label']}" + (f" ({d} km)" if d is not None else ""))
    elif l is not None and l <= 40 and not near.get("pos_label"):
        neg.append("developed land")

    text = _grade(score)
    if pos:
        text += " — " + ", ".join(pos)
    if neg:
        text += ("; drawback: " if pos else " — ") + ", ".join(neg)
    return text


def _render_polyline(coords, cum, scored, explain=None) -> list[dict]:
    """Downsample the geometry and attach a scenic score + explanation per point."""
    total = cum[-1]
    n = min(RENDER_MAX_POINTS, len(coords))
    if n < 2:
        n = len(coords)
    step_km = total / (n - 1) if n > 1 else 0
    ex_keys = sorted(explain) if explain else []
    pts, j = [], 0
    for k in range(n):
        target = step_km * k
        while j < len(cum) - 1 and cum[j] < target:
            j += 1
        lat, lng = coords[j]
        score = round(_nearest_sample_score(cum[j], cum, scored), 1)
        pt = {"lat": round(lat, 5), "lng": round(lng, 5), "score": score}
        if ex_keys:
            comp = explain[_nearest_sample_key(cum[j], cum, ex_keys)]
            pt["colour"] = comp.get("colour")
            pt["terrain"] = comp.get("terrain")
            pt["landcover"] = comp.get("landcover")
            pt["elev_m"] = comp.get("elev_m")
            pt["climate"] = comp.get("climate")
            pt["reason"] = _reason(score, comp)
        pts.append(pt)
    return pts


def _corridor_bbox(a, b, pad=None, extras: list[tuple[float, float]] | None = None):
    pad = pad if pad is not None else config.CORRIDOR_PAD_DEG
    pts = [a, b] + list(extras or [])
    return (min(p[0] for p in pts) - pad, min(p[1] for p in pts) - pad,
            max(p[0] for p in pts) + pad, max(p[1] for p in pts) + pad)


def _with_user_vias(user_vias: list[tuple[float, float]],
                    *extra: tuple[float, float]) -> list[tuple[float, float]]:
    """Ordered OSRM intermediates: hard user vias first, then scenic extras."""
    out = list(user_vias)
    out.extend(extra)
    return out


def _candidate_waypoints(a, b, features, pad=None, grid=None,
                         candidates=None, detour_ratio=None):
    """Grid-sample the corridor and return the top scenic hotspots to route via.

    Hotspots are pre-scored cheaply with land cover + elevation only (no tiles),
    and rejected if routing through them would detour too far. The pad / grid /
    candidates / detour_ratio overrides let the planner widen the search when a
    hard scenic target can't be met at the default radius.
    """
    grid = grid if grid is not None else config.WAYPOINT_GRID
    candidates = candidates if candidates is not None else config.WAYPOINT_CANDIDATES
    detour_ratio = detour_ratio if detour_ratio is not None else config.MAX_DETOUR_RATIO
    s, w, n, e = _corridor_bbox(a, b, pad=pad)
    g = grid
    pts = []
    for r in range(g):
        for c in range(g):
            lat = s + (n - s) * (r + 0.5) / g
            lng = w + (e - w) * (c + 0.5) / g
            pts.append((lat, lng))

    direct = _haversine_km(*a, *b)
    # Elevation for all candidates in one batched call; score with *relief*
    # (local ruggedness), matching the terrain signal used in score_route.
    elevs = enrich.elevation_batch(pts) or [0.0] * len(pts)
    relief = enrich.relief_scores(elevs) if elevs else [0.0] * len(pts)

    # Align with score_route: only steer on map context when it is usable.
    land_usable = enrich.landcover_is_usable(features, pts)
    land_scores = (
        enrich.landcover_scores(pts, features)
        if land_usable and features is not None
        else None
    )
    scored = []
    urban_cap = float(config.HOTSPOT_URBAN_LAND_MAX)
    for k, ((lat, lng), rel) in enumerate(zip(pts, relief)):
        detour = _haversine_km(*a, lat, lng) + _haversine_km(lat, lng, *b)
        if direct > 0 and detour > detour_ratio * direct:
            continue
        if land_scores is not None:
            # Skip urban-fabric hotspots (town parks / grey centres) so road-first
            # does not steer through the middle of town for a green pocket.
            if land_scores[k] < urban_cap:
                continue
            est = 0.6 * land_scores[k] + 0.4 * rel
        else:
            est = rel  # omit weak/neutral map context
        scored.append(((lat, lng), est))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in scored[:candidates]]


def _order_along(a, b, pts):
    """Sort points by their projection onto the A->B axis so chained waypoints
    progress toward the destination instead of making OSRM back-track."""
    dlat, dlng = (b[0] - a[0]), (b[1] - a[1])
    denom = dlat * dlat + dlng * dlng or 1.0
    def t(p):
        return ((p[0] - a[0]) * dlat + (p[1] - a[1]) * dlng) / denom
    return sorted(pts, key=t)


def _avoid_motorway_waypoints(
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    offset_km: float | None = None,
) -> list[tuple[float, float]]:
    """Perpendicular offsets from A→B to steer OSRM off motorway corridors."""
    off = config.AVOID_MW_OFFSET_KM if offset_km is None else float(offset_km)
    lat0 = math.radians((a[0] + b[0]) / 2.0)
    cos0 = math.cos(lat0) or 1.0

    def _xy(q):
        return (
            math.radians(q[1]) * cos0 * 6371.0,
            math.radians(q[0]) * 6371.0,
        )

    ax, ay = _xy(a)
    bx, by = _xy(b)
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length

    def _from_xy(x, y):
        lat = math.degrees(y / 6371.0)
        lng = math.degrees(x / (6371.0 * cos0))
        return (lat, lng)

    out: list[tuple[float, float]] = []
    for frac in (0.25, 0.5, 0.75):
        cx = ax + dx * frac
        cy = ay + dy * frac
        for sign in (1.0, -1.0):
            out.append(_from_xy(cx + px * off * sign, cy + py * off * sign))
    # Deduplicate near-identical points.
    kept: list[tuple[float, float]] = []
    for pt in out:
        if any(_haversine_km(pt[0], pt[1], k[0], k[1]) < 2.0 for k in kept):
            continue
        kept.append(pt)
    return kept[:6]


def _point_segment_km(p, a, b):
    """Approx shortest distance (km) from point p to the A->B great-circle chord."""
    # Work in a local equirectangular projection (fine at country scale).
    lat0 = math.radians((a[0] + b[0]) / 2)
    def xy(q):
        return (math.radians(q[1]) * math.cos(lat0) * 6371.0,
                math.radians(q[0]) * 6371.0)
    (px, py), (ax, ay), (bx, by) = xy(p), xy(a), xy(b)
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _nearby_attractors(a, b):
    """Scenic attractors worth diverting through: near the A→B corridor or only a
    modest detour beyond it. Returns [(name, lat, lng)] ordered along the route.
    Travel time is irrelevant here — this is the 'explore everything' booster.

    Uses the pluggable worldwide attractor registry (UK parks + world packs).
    """
    direct = _haversine_km(*a, *b) or 1.0
    hits = []
    for name, lat, lng in all_attractors():
        perp = _point_segment_km((lat, lng), a, b)
        detour = _haversine_km(*a, lat, lng) + _haversine_km(lat, lng, *b)
        if perp <= config.EXPLORE_PARK_MAX_KM or detour <= config.EXPLORE_MAX_DETOUR_RATIO * direct:
            hits.append(((name, lat, lng), perp))
    hits.sort(key=lambda x: x[1])                       # closest to the line first
    parks = [p for p, _ in hits[:config.EXPLORE_MAX_PARKS]]
    return _order_along(a, b, [(la, lo) for _, la, lo in parks]), \
           {(round(la, 3), round(lo, 3)): nm for nm, la, lo in parks}


def _dedupe(routes, tol_km=1.0):
    """Drop routes that are near-duplicates (similar distance and midpoint)."""
    kept = []
    for rt in routes:
        mid = rt["coords"][len(rt["coords"]) // 2]
        dup = False
        for k in kept:
            kmid = k["coords"][len(k["coords"]) // 2]
            if (abs(rt["distance_km"] - k["distance_km"]) < tol_km
                    and _haversine_km(*mid, *kmid) < tol_km):
                dup = True
                break
        if not dup:
            kept.append(rt)
    return kept


def plan(a: tuple[float, float], b: tuple[float, float],
         preference: float, source: str = "esri",
         weights: dict | None = None, detour_factor: float | None = None,
         profile_id: str | None = None, avoid_motorways: bool = False,
         min_scenic: float = 0.0, explore_all: bool = False, progress=None,
         vias: list[tuple[float, float]] | None = None,
         time_budget: bool = True) -> dict:
    """Scenic road-routing pipeline: real roads + scenic waypoint search.

    weights / detour_factor let a scenic profile reshape scoring and routing.
    avoid_motorways hard-filters motorway routes (motorway_km ≈ 0) when any
    non-motorway candidate exists, and still applies a harsh per-km penalty as
    a ranking nudge. Escalates hotspot/attractor search when only motorway
    geometries are available.
    min_scenic (0-100) is a hard floor: the recommended route is the best-cost
    qualifying candidate; if none qualify, the most scenic route in the
    (motorway-filtered) pool — never the fastest loser. Unmet targets are
    flagged via min_scenic_met=False.
    explore_all disregards travel time entirely: routes are ranked purely by
    scenic score and the planner deliberately diverts through national parks and
    far scenic terrain, however long the drive becomes.
    time_budget (default True) enables the PLAN_BUDGET_SEC wall-clock gate for
    explore / hard-target / landcover deadlines. When False, those stages are
    not skipped solely due to wall-clock (cold corridors may take minutes).
    vias, if given, are hard must-pass intermediates (OSRM waypoints); scenic
    hotspot / attractor injection still runs but always includes these vias.
    progress, if given, is called with small event dicts as the search runs
    (used to stream live search progress to the UI).

    Scoring is two-phase: every candidate gets a cheap landcover+terrain proxy;
    full colour runs on the top-K shortlist during search, then on every route
    in the final pool (after motorway filter) at rank density; the chosen route
    is always refined at full sample density. Explore/hard-target never wipe
    the pool — only new geometries are scored; merged landcover is cheap-reblended.
    """
    preference = max(0.0, min(1.0, float(preference)))
    min_scenic = max(0.0, min(100.0, float(min_scenic)))
    use_time_budget = bool(time_budget)
    user_vias = [(float(v[0]), float(v[1])) for v in (vias or [])]
    detour = detour_factor if detour_factor is not None else config.DETOUR_FACTOR
    mw_pen = config.MOTORWAY_PENALTY_MIN_PER_KM if avoid_motorways else 0.0
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    climate = select_climate(mid[0], mid[1])

    plan_t0 = time.perf_counter()
    work_deadline = (
        plan_t0 + config.PLAN_BUDGET_SEC - config.PLAN_RESERVE_SEC
        if use_time_budget else None
    )
    budget_reasons: list[str] = []

    def _time_left() -> float:
        if work_deadline is None:
            return float("inf")
        return work_deadline - time.perf_counter()

    def _emit(ev):
        if progress:
            try:
                progress(ev)
            except Exception:
                pass

    def _budget_stop(reason: str, label: str | None = None):
        if reason not in budget_reasons:
            budget_reasons.append(reason)
        _emit({
            "type": "phase",
            "label": label or "Time budget reached — returning best route so far",
            "budget_exhausted": True,
            "budget_reason": reason,
        })

    _emit({"type": "start", "from": list(a), "to": list(b),
           "vias": [list(v) for v in user_vias],
           "min_scenic": min_scenic, "avoid_motorways": avoid_motorways,
           "explore_all": explore_all, "climate": climate.id,
           "time_budget": use_time_budget,
           "budget_sec": config.PLAN_BUDGET_SEC if use_time_budget else None})

    timings_ms = {"osrm": 0.0, "landcover": 0.0, "score": 0.0}
    lc_pad = (config.CORRIDOR_PAD_DEG + 0.35) if explore_all else None

    def _lc_progress(done, total):
        cold = total > 0 and done < total
        if cold:
            _emit({
                "type": "phase",
                "label": "Warming map context for this corridor…",
                "cold_corridor": True,
            })
        _emit({
            "type": "landcover",
            "done": done,
            "total": total,
            "cold": cold,
            "label": (
                f"Warming map context… {done}/{total} tiles"
                if cold
                else f"Reading land cover… {done}/{total} tiles"
            ),
        })

    # Overlap corridor landcover with base OSRM so cold Overpass and routing
    # share wall-clock instead of running strictly sequentially.
    _emit({"type": "phase",
           "label": "Finding base routes and reading land cover…"})

    def _fetch_lc():
        t_lc = time.perf_counter()
        feats = enrich.fetch_landcover(
            _corridor_bbox(a, b, pad=lc_pad, extras=user_vias),
            progress=_lc_progress,
            prefer_axis=(a, b),
            deadline=work_deadline,
        )
        return feats, (time.perf_counter() - t_lc) * 1000.0

    def _fetch_base():
        t_os = time.perf_counter()
        excl = bool(avoid_motorways)
        if user_vias:
            rts = get_osrm_routes(
                a, b, alternatives=0, waypoints=user_vias, exclude_motorway=excl,
            )
        else:
            rts = get_osrm_routes(a, b, alternatives=3, exclude_motorway=excl)
        return rts, (time.perf_counter() - t_os) * 1000.0

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_lc = ex.submit(_fetch_lc)
        fut_base = ex.submit(_fetch_base)
        features, lc_ms = fut_lc.result()
        routes, osrm_ms = fut_base.result()
    timings_ms["landcover"] += lc_ms
    timings_ms["osrm"] += osrm_ms
    if features and (
        features.get("deadline_stopped")
        or features.get("truncated")
        or features.get("landcover_incomplete")
    ):
        if "landcover_truncated" not in budget_reasons:
            budget_reasons.append("landcover_truncated")
        _emit({
            "type": "phase",
            "label": "Map context still warming — using partial coverage (not broken)",
            "cold_corridor": True,
            "budget_reason": "landcover_truncated",
        })

    def _refresh_after_landcover_merge(pool):
        """Cheap re-blend landcover/terrain on already-scored routes (no Esri)."""
        for rt in pool:
            if rt.get("_score_meta"):
                reblend_route_landcover(rt, features, weights=weights)

    def _score_and_emit(pool, kind):
        """Proxy-score new geometries; colour only top-K of those new routes."""
        t0 = time.perf_counter()
        new_rts = [rt for rt in pool if "avg_scenic_score" not in rt]
        spacing = config.SAMPLE_SPACING_KM_RANK
        cap = config.MAX_SAMPLES_RANK

        # Union elev samples across new routes → one batch before parallel proxy.
        if new_rts:
            elev_coords: dict[tuple[float, float], tuple[float, float]] = {}
            for rt in new_rts:
                coords = rt["coords"]
                cum = _cumulative_km(coords)
                for i in _sample_indices(cum, spacing, cap):
                    lat, lng = coords[i]
                    elev_coords[(round(lat, 4), round(lng, 4))] = (lat, lng)
            if elev_coords:
                enrich.elevation_batch(list(elev_coords.values()))

            workers = max(1, min(config.SCORE_ROUTE_WORKERS, len(new_rts)))

            def _proxy_one(rt):
                score_route(
                    rt, features=features, source=source, weights=weights,
                    sample_spacing_km=spacing, max_samples=cap, colour=False,
                )
                return rt

            if workers == 1:
                scored_new = [_proxy_one(rt) for rt in new_rts]
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    scored_new = list(ex.map(_proxy_one, new_rts))

            for rt in scored_new:
                _emit({
                    "type": "candidate",
                    "kind": kind,
                    "coords": _decimate(rt["coords"], 80),
                    "scenic": rt["avg_scenic_score"],
                    "proxy": True,
                    "distance_km": round(rt["distance_km"], 1),
                    "duration_min": round(rt["duration_min"], 1),
                    "motorway_km": round(rt.get("motorway_km", 0.0), 1),
                    "meets_min": (min_scenic <= 0) or rt["avg_scenic_score"] >= min_scenic,
                    "total": len(pool),
                    "phase": "proxy",
                })

        def _colour_emit(ev):
            ev["meets_min"] = (
                (min_scenic <= 0) or ev.get("scenic", 0) >= min_scenic
            )
            _emit(ev)

        if new_rts:
            _colour_top_k(
                new_rts, features, source, weights,
                emit=_colour_emit, kind=kind,
            )
        timings_ms["score"] += (time.perf_counter() - t0) * 1000.0

    def _qualifies(pool):
        """Floor / avoid-mw gate for hard-target continuation."""
        cand = filter_zero_motorway(pool) if avoid_motorways else pool
        if avoid_motorways and not cand:
            return False
        if min_scenic > 0:
            return any(rt["avg_scenic_score"] >= min_scenic for rt in cand)
        return True

    # Scenic hotspot detours (parallel OSRM fan-out).
    hotspots_used: list[dict] = []
    attractors_used: list[dict] = []
    if features is not None and _time_left() > 0:
        _emit({"type": "phase", "label": "Searching scenic detours through the corridor…"})
        wps = _candidate_waypoints(a, b, features)
        hotspots_used = [
            {"name": f"Hotspot {i + 1}", "lat": round(lat, 5), "lng": round(lng, 5)}
            for i, (lat, lng) in enumerate(wps)
        ]
        t_os = time.perf_counter()
        routes.extend(_fanout_osrm([
            {"a": a, "b": b, "waypoints": _with_user_vias(user_vias, wp)}
            for wp in wps
        ]))
        timings_ms["osrm"] += (time.perf_counter() - t_os) * 1000.0

    routes = _dedupe(routes)
    _emit({"type": "phase", "label": f"Scoring {len(routes)} candidate routes…"})
    _score_and_emit(routes, "base")

    def _inject_avoid_mw_diversions(label_prefix: str) -> bool:
        """Offset-corridor OSRM fan-out when every route still uses motorways."""
        nonlocal routes
        if not avoid_motorways or any(is_zero_motorway(r) for r in routes):
            return False
        if _time_left() < config.PLAN_AVOID_MW_MIN_SEC:
            return False
        _emit({
            "type": "phase",
            "label": f"{label_prefix}: routing via inland corridor offsets…",
        })
        offset_wps = _avoid_motorway_waypoints(a, b)
        jobs = [
            {"a": a, "b": b, "waypoints": _with_user_vias(user_vias, wp),
             "exclude_motorway": True}
            for wp in offset_wps
        ]
        t_os = time.perf_counter()
        new = _fanout_osrm(jobs)
        timings_ms["osrm"] += (time.perf_counter() - t_os) * 1000.0
        if not new:
            return False
        routes = _dedupe(routes + new)
        _score_and_emit(routes, "avoid_mw")
        return any(is_zero_motorway(r) for r in routes)

    _inject_avoid_mw_diversions("Avoid motorways")

    # --- Explore everything: divert through scenic attractors ----------------
    # Also escalate attractors when avoid_motorways has only motorway geometries.
    parks_used = False

    def _divert_attractors(label_prefix: str) -> bool:
        """Inject nearby attractor waypoints; return True if any parks used."""
        nonlocal features, routes, attractors_used, parks_used
        park_pts, park_names = _nearby_attractors(a, b)
        if not park_pts:
            return False
        parks_used = True
        attractors_used = [
            {
                "name": park_names.get((round(plat, 3), round(plng, 3)), "Attractor"),
                "lat": round(plat, 5),
                "lng": round(plng, 5),
            }
            for (plat, plng) in park_pts
        ]
        names = ", ".join(sorted(set(park_names.values())))
        _emit({"type": "phase",
               "label": f"{label_prefix}: {names}…"})
        d = config.EXPLORE_PARK_BBOX_DEG
        park_boxes = [
            (plat - d, plng - d, plat + d, plng + d)
            for (plat, plng) in park_pts
        ]
        jobs = [
            {"a": a, "b": b, "waypoints": _with_user_vias(user_vias, wp)}
            for wp in park_pts
        ]
        for k in range(2, min(config.EXPLORE_MAX_CHAIN, len(park_pts)) + 1):
            jobs.append({
                "a": a, "b": b,
                "waypoints": _with_user_vias(user_vias, *park_pts[:k]),
            })

        def _fetch_park_lc():
            t_lc = time.perf_counter()

            def _park_lc(box):
                return enrich.fetch_landcover(
                    box, progress=_lc_progress, prefer_axis=(a, b),
                    deadline=work_deadline,
                )

            workers = max(1, min(config.LANDCOVER_TILE_WORKERS, len(park_boxes)))
            if workers == 1:
                park_feats = [_park_lc(box) for box in park_boxes]
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    park_feats = list(ex.map(_park_lc, park_boxes))
            return park_feats, (time.perf_counter() - t_lc) * 1000.0

        def _fetch_park_osrm():
            t_os = time.perf_counter()
            new_local = _fanout_osrm(jobs)
            return new_local, (time.perf_counter() - t_os) * 1000.0

        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_lc = ex.submit(_fetch_park_lc)
            fut_osrm = ex.submit(_fetch_park_osrm)
            park_feats, lc_ms = fut_lc.result()
            new, osrm_ms = fut_osrm.result()
        timings_ms["landcover"] += lc_ms
        timings_ms["osrm"] += osrm_ms
        for pf in park_feats:
            if pf and pf.get("deadline_stopped"):
                if "landcover_truncated" not in budget_reasons:
                    budget_reasons.append("landcover_truncated")
            features = enrich.merge_landcover(features, pf)
        _refresh_after_landcover_merge(routes)
        routes = _dedupe(routes + new)
        _score_and_emit(routes, "park")
        return True

    if explore_all:
        if _time_left() < config.PLAN_EXPLORE_MIN_SEC:
            _budget_stop("explore_skipped",
                         "Time budget reached — skipping explore diversions")
        else:
            if not _divert_attractors("Diverting through scenic attractors"):
                _emit({"type": "phase",
                       "label": "Explore mode: no named attractors near this corridor — "
                                "using scenic hotspots only…"})
    elif avoid_motorways and not any(is_zero_motorway(r) for r in routes):
        # Harsh avoid: force attractor diversions to escape motorway-only pools.
        if _time_left() >= config.PLAN_AVOID_MW_MIN_SEC:
            _emit({"type": "phase",
                   "label": "Avoid motorways: searching non-motorway diversions…"})
            if not _divert_attractors("Avoid motorways via attractors"):
                _emit({"type": "phase",
                       "label": "Avoid motorways: no nearby attractors — "
                                "widening corridor search…"})
            if not any(is_zero_motorway(r) for r in routes):
                _inject_avoid_mw_diversions("Avoid motorways (retry)")

    # --- Hard scenic / avoid-mw target: escalate until floor is met ----------
    need_escalate = (min_scenic > 0 or avoid_motorways) and not _qualifies(routes)
    if need_escalate:
        for ri, (pad, grid, cand, ratio, chain) in enumerate(config.HARD_TARGET_ROUNDS, 1):
            if _time_left() < config.PLAN_HT_ROUND_MIN_SEC:
                _budget_stop(
                    "hard_target_stopped",
                    "Time budget reached — returning best route so far",
                )
                break
            best = max((r["avg_scenic_score"] for r in routes), default=0)
            zero_n = sum(1 for r in routes if is_zero_motorway(r))
            if min_scenic > 0:
                round_label = (
                    f"Target {min_scenic:.0f} not met yet (best {best:.0f}"
                    + (f", {zero_n} non-motorway" if avoid_motorways else "")
                    + f") — widening search, round {ri}/{len(config.HARD_TARGET_ROUNDS)}…"
                )
            else:
                round_label = (
                    f"No motorway-free route yet — widening search, "
                    f"round {ri}/{len(config.HARD_TARGET_ROUNDS)}…"
                )
            _emit({"type": "round", "n": ri, "total": len(config.HARD_TARGET_ROUNDS),
                   "best": round(best, 1), "target": min_scenic,
                   "label": round_label})
            # Waypoints from prior features so widened LC can overlap OSRM wall-clock.
            wps = _candidate_waypoints(a, b, features, pad=pad, grid=grid,
                                       candidates=cand, detour_ratio=ratio)
            jobs = [
                {"a": a, "b": b, "waypoints": _with_user_vias(user_vias, wp)}
                for wp in wps
            ]
            for k in range(2, min(chain, len(wps)) + 1):
                ordered = _order_along(a, b, wps[:k])
                jobs.append({
                    "a": a, "b": b,
                    "waypoints": _with_user_vias(user_vias, *ordered),
                })

            def _fetch_widened_lc():
                t_lc = time.perf_counter()
                feats_local = enrich.fetch_landcover(
                    _corridor_bbox(a, b, pad=pad, extras=user_vias), prefer_axis=(a, b),
                    deadline=work_deadline,
                )
                return feats_local, (time.perf_counter() - t_lc) * 1000.0

            def _fetch_round_osrm():
                t_os = time.perf_counter()
                new_local = _fanout_osrm(jobs)
                return new_local, (time.perf_counter() - t_os) * 1000.0

            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_lc = ex.submit(_fetch_widened_lc)
                fut_osrm = ex.submit(_fetch_round_osrm)
                feats, lc_ms = fut_lc.result()
                new, osrm_ms = fut_osrm.result()
            timings_ms["landcover"] += lc_ms
            timings_ms["osrm"] += osrm_ms
            if feats is not None:
                if feats.get("deadline_stopped"):
                    if "landcover_truncated" not in budget_reasons:
                        budget_reasons.append("landcover_truncated")
                features = enrich.merge_landcover(features, feats)
                _refresh_after_landcover_merge(routes)
            routes = _dedupe(routes + new)
            # Only newly generated geometries enter colour/proxy scoring.
            _score_and_emit(routes, "expanded")
            if _qualifies(routes):
                break
            if _time_left() <= 0:
                _budget_stop(
                    "hard_target_stopped",
                    "Time budget reached — returning best route so far",
                )
                break

    def _rank_routes(pool: list[dict]) -> None:
        """Sort candidate pool best-first (proxy or full blend)."""
        if explore_all:
            pool.sort(key=lambda rt: (-rt["avg_scenic_score"], rt.get("motorway_km", 0.0),
                                      rt["duration_min"]))
        elif min_scenic > 0:
            def _rank_key(rt):
                meets = rt["avg_scenic_score"] >= min_scenic
                mw = float(rt.get("motorway_km", 0.0) or 0.0)
                if avoid_motorways and not is_zero_motorway(rt):
                    return (2, mw, route_cost(rt, preference, detour, mw_pen))
                if meets:
                    return (0, route_cost(rt, preference, detour, mw_pen), mw)
                return (1, -rt["avg_scenic_score"], mw, rt["duration_min"])

            pool.sort(key=_rank_key)
        elif avoid_motorways:
            pool.sort(key=lambda rt: (
                0 if is_zero_motorway(rt) else 1,
                route_cost(rt, preference, detour, mw_pen),
            ))
        else:
            pool.sort(key=lambda rt: route_cost(rt, preference, detour, mw_pen))

    _rank_routes(routes)

    # When harsh avoid has zero-mw options, drop motorway alts from the ranked
    # pool so they cannot be chosen or listed as primary candidates.
    motorway_avoid_met = True
    motorway_free_candidates = sum(1 for r in routes if is_zero_motorway(r))
    motorway_avoid_reason = "not_requested"
    if avoid_motorways:
        zero = filter_zero_motorway(routes)
        if zero:
            routes = zero
            motorway_avoid_met = True
            motorway_avoid_reason = "motorway_free_candidate_found"
        else:
            motorway_avoid_met = False
            motorway_avoid_reason = "all_candidates_include_motorways"
            _emit({"type": "phase",
                   "label": "Avoid motorways: no motorway-free route found — "
                            "showing least-motorway option"})

    # Rank-density colour on every final alternative (after motorway filter).
    if routes:
        _emit({"type": "phase",
               "label": f"Colour-scoring {len(routes)} final route"
               + ("s" if len(routes) != 1 else "") + "…"})
        t_fc = time.perf_counter()
        _colour_top_k(
            routes, features, source, weights,
            top_k=len(routes), kind="final",
        )
        timings_ms["score"] += (time.perf_counter() - t_fc) * 1000.0
        _rank_routes(routes)

    chosen, min_scenic_met = select_chosen(
        routes, min_scenic=min_scenic, avoid_motorways=avoid_motorways,
    )

    # Always refine the chosen route at full sample density with full colour.
    t_refine = time.perf_counter()
    score_route(
        chosen, features=features, source=source, weights=weights,
        sample_spacing_km=SAMPLE_SPACING_KM,
        max_samples=MAX_SAMPLES,
        colour=True,
        reuse_meta=True,
    )
    timings_ms["score"] += (time.perf_counter() - t_refine) * 1000.0
    if min_scenic > 0:
        min_scenic_met = chosen["avg_scenic_score"] >= min_scenic
        # Proxy may have overstated scenery; if refine drops below the floor,
        # re-pick the most scenic remaining candidate (never stick with a
        # now-unmet "fastest" pick that only looked good on proxy).
        if not min_scenic_met:
            alt, alt_met = select_chosen(
                routes, min_scenic=min_scenic, avoid_motorways=avoid_motorways,
            )
            if alt is not chosen and (
                alt_met
                or alt["avg_scenic_score"] > chosen["avg_scenic_score"]
            ):
                chosen = alt
                t_refine2 = time.perf_counter()
                score_route(
                    chosen, features=features, source=source, weights=weights,
                    sample_spacing_km=SAMPLE_SPACING_KM,
                    max_samples=MAX_SAMPLES,
                    colour=True,
                    reuse_meta=True,
                )
                timings_ms["score"] += (time.perf_counter() - t_refine2) * 1000.0
                min_scenic_met = chosen["avg_scenic_score"] >= min_scenic

    if avoid_motorways:
        motorway_avoid_met = is_zero_motorway(chosen)

    timings_ms = {k: round(v, 1) for k, v in timings_ms.items()}
    elapsed_ms = round((time.perf_counter() - plan_t0) * 1000.0, 1)
    timings_ms["elapsed"] = elapsed_ms
    budget_exhausted = bool(budget_reasons)
    _emit({"type": "timings", "timings_ms": timings_ms,
           "budget_exhausted": budget_exhausted,
           "budget_reasons": list(budget_reasons)})

    land_used = any(r.get("_landcover_usable") for r in routes)
    land_incomplete = bool(features and features.get("truncated")) or (
        features is not None and not land_used
    )

    climate_id = chosen.get("climate") or select_climate(mid[0], mid[1]).id
    climates_used = sorted({
        cid
        for r in routes
        for cid in (r.get("climates_used") or ([r["climate"]] if r.get("climate") else []))
    }) or [climate_id]

    result = {
        "preference": preference,
        "profile": profile_id,
        "climate": climate_id,
        "avoid_motorways": avoid_motorways,
        "motorway_avoid_met": motorway_avoid_met if avoid_motorways else True,
        "motorway_avoid_reason": motorway_avoid_reason,
        "min_scenic": min_scenic,
        "min_scenic_met": min_scenic_met,
        "explore_all": explore_all,
        "time_budget": use_time_budget,
        "from": list(a),
        "to": list(b),
        "vias": [[round(v[0], 5), round(v[1], 5)] for v in user_vias],
        "budget_sec": config.PLAN_BUDGET_SEC if use_time_budget else None,
        "elapsed_ms": elapsed_ms,
        "budget_exhausted": budget_exhausted,
        "budget_reasons": list(budget_reasons),
        "timings_ms": timings_ms,
        "signals": {
            "colour": True,
            "terrain": any(
                (r.get("components") or {}).get("terrain") is not None for r in routes
            ),
            "landcover": land_used,
            "landcover_incomplete": land_incomplete,
            "motorway_free_candidates": motorway_free_candidates,
            "motorway_candidates_total": len(routes),
            "explore_parks": parks_used,
            "climate": climate_id,
            "climate_name": climate_display_name(climate_id),
            "climates_used": climates_used,
        },
        "chosen": _route_summary(chosen, chosen=True, min_scenic=min_scenic),
        "alternatives": [
            _route_summary(r, chosen=(r is chosen), min_scenic=min_scenic) for r in routes
        ],
        "hotspots": hotspots_used,
        "attractors_used": attractors_used,
    }
    _emit({"type": "done", "result": result})
    return result


def _decimate(coords: list, target: int) -> list:
    """Down-sample a polyline to ~target points for a lightweight live preview."""
    n = len(coords)
    if n <= target:
        return [[round(la, 5), round(lo, 5)] for la, lo in coords]
    step = n / float(target)
    out = [coords[min(n - 1, int(i * step))] for i in range(target)]
    out[-1] = coords[-1]
    return [[round(la, 5), round(lo, 5)] for la, lo in out]


def plan_events(*args, **kwargs):
    """Run plan() in a worker thread and yield its progress events as they
    happen. The final event is {"type":"done","result":<full plan result>}."""
    q: queue.Queue = queue.Queue()
    _SENTINEL = object()

    def cb(ev):
        q.put(ev)

    def run():
        try:
            plan(*args, progress=cb, **kwargs)
        except Exception:  # noqa: BLE001
            log.warning("plan_events_failed", exc_info=True)
            q.put({"type": "error", "message": "Routing failed."})
        finally:
            q.put(_SENTINEL)

    threading.Thread(target=run, daemon=True).start()
    while True:
        ev = q.get()
        if ev is _SENTINEL:
            break
        yield ev


def _route_summary(rt: dict, chosen: bool = False, min_scenic: float = 0.0) -> dict:
    return {
        "chosen": chosen,
        "distance_km": round(rt["distance_km"], 1),
        "duration_min": round(rt["duration_min"], 1),
        "avg_scenic_score": rt["avg_scenic_score"],
        "meets_min": rt["avg_scenic_score"] >= min_scenic,
        "components": rt.get("components", {}),
        "colour_scored": bool(rt.get("_colour_scored")),
        "num_samples": rt.get("num_samples", 0),
        "render": rt["render"],
        "directions": rt.get("directions", []),
        "motorway_km": round(rt.get("motorway_km", 0.0), 1),
    }


MAX_DRAW_VERTICES = 50
MAX_DRAW_PATH_KM = 600.0
_DRAW_SPEED_KMH = 50.0


def _polyline_length_km(coords: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(coords)):
        total += _haversine_km(*coords[i - 1], *coords[i])
    return total


def _densify_for_match(
    vertices: list[tuple[float, float]],
    max_segment_km: float | None = None,
) -> list[tuple[float, float]]:
    """Insert points along long sketch segments (match fallback only; prefer raw clicks)."""
    if len(vertices) < 2:
        return list(vertices)
    spacing_km = (
        config.DRAW_MATCH_SEGMENT_KM if max_segment_km is None else float(max_segment_km)
    )
    if spacing_km <= 0:
        return list(vertices)
    out: list[tuple[float, float]] = [vertices[0]]
    for i in range(1, len(vertices)):
        a, b = vertices[i - 1], vertices[i]
        seg_km = _haversine_km(a[0], a[1], b[0], b[1])
        if seg_km <= spacing_km:
            out.append(b)
            continue
        n = max(1, int(math.ceil(seg_km / spacing_km)))
        for j in range(1, n + 1):
            t = j / float(n)
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return out


def _prune_out_and_back_spurs(
    coords: list[tuple[float, float]],
    *,
    close_m: float | None = None,
    min_spur_m: float | None = None,
    max_lookback: int | None = None,
) -> list[tuple[float, float]]:
    """Remove thin out-and-back loops where the path revisits a recent point.

    Detects V-shaped village spurs / U-turns: the polyline leaves a point, wanders
    for at least ``min_spur_m``, then returns within ``close_m`` of that point.
    The loop interior is dropped so the route continues past the revisit.
    """
    if len(coords) < 4:
        return list(coords)
    close_km = (
        (config.DRAW_SPUR_CLOSE_M if close_m is None else float(close_m)) / 1000.0
    )
    min_spur_km = (
        (config.DRAW_SPUR_MIN_M if min_spur_m is None else float(min_spur_m)) / 1000.0
    )
    lookback = (
        config.DRAW_SPUR_MAX_POINTS if max_lookback is None else int(max_lookback)
    )
    lookback = max(4, lookback)
    out = list(coords)
    for _ in range(12):
        n = len(out)
        if n < 4:
            break
        removed = False
        for i in range(n - 3):
            j_limit = min(n, i + lookback)
            loop_km = 0.0
            for j in range(i + 1, j_limit):
                loop_km += _haversine_km(*out[j - 1], *out[j])
                if j < i + 3:
                    continue
                if loop_km < min_spur_km:
                    continue
                if _haversine_km(*out[i], *out[j]) > close_km:
                    continue
                # Drop the spur: keep i, skip i+1..j (j ≈ i).
                out = out[: i + 1] + out[j + 1 :]
                removed = True
                break
            if removed:
                break
        if not removed:
            break
    return out


def _apply_spur_prune(route: dict) -> dict:
    """Prune out-and-back spurs and scale distance/duration when geometry shortens."""
    coords = list(route.get("coords") or [])
    if len(coords) < 4:
        return route
    pruned = _prune_out_and_back_spurs(coords)
    if len(pruned) >= len(coords):
        return route
    old_len = _polyline_length_km(coords) or 1.0
    new_len = _polyline_length_km(pruned)
    ratio = max(0.0, min(1.0, new_len / old_len))
    out = dict(route)
    out["coords"] = pruned
    out["distance_km"] = float(route.get("distance_km", old_len)) * ratio
    out["duration_min"] = float(route.get("duration_min", 0.0)) * ratio
    out["motorway_km"] = float(route.get("motorway_km", 0.0)) * ratio
    return out


def _stitch_osrm_leg_routes(legs: list[dict]) -> dict:
    """Concatenate consecutive OSRM leg routes into one polyline + metrics."""
    if not legs:
        raise RuntimeError("No OSRM legs to stitch.")
    if len(legs) == 1:
        return dict(legs[0])
    all_coords: list[tuple[float, float]] = []
    total_dist = 0.0
    total_dur = 0.0
    total_mw = 0.0
    all_dirs: list[dict] = []
    for leg in legs:
        coords = list(leg.get("coords") or [])
        if all_coords and coords:
            if _haversine_km(*coords[0], *all_coords[-1]) < 0.05:
                coords = coords[1:]
        all_coords.extend(coords)
        total_dist += float(leg.get("distance_km", 0.0))
        total_dur += float(leg.get("duration_min", 0.0))
        total_mw += float(leg.get("motorway_km", 0.0))
        all_dirs.extend(leg.get("directions") or [])
    return {
        "coords": all_coords,
        "distance_km": total_dist,
        "duration_min": total_dur,
        "motorway_km": total_mw,
        "directions": all_dirs,
    }


def _point_to_segment_km(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Approximate perpendicular distance (km) from ``p`` to chord ``a``→``b``."""
    lat0 = math.radians((a[0] + b[0]) * 0.5)
    scale = 6371.0

    def to_xy(lat: float, lng: float) -> tuple[float, float]:
        x = math.radians(lng - a[1]) * math.cos(lat0) * scale
        y = math.radians(lat - a[0]) * scale
        return x, y

    ax, ay = 0.0, 0.0
    bx, by = to_xy(b[0], b[1])
    px, py = to_xy(p[0], p[1])
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 < 1e-12:
        return _haversine_km(p[0], p[1], a[0], a[1])
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _max_chord_deviation_km(
    coords: list[tuple[float, float]],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Max distance from any route point to the straight click chord."""
    if not coords:
        return 0.0
    return max(_point_to_segment_km(p, a, b) for p in coords)


def _chord_via_points(
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    spacing_km: float | None = None,
    max_vias: int | None = None,
) -> list[tuple[float, float]]:
    """Sample intermediate points along the straight chord V[i]→V[i+1]."""
    spacing = (
        config.DRAW_CHORD_VIA_SPACING_KM if spacing_km is None else float(spacing_km)
    )
    cap = config.DRAW_CHORD_MAX_VIAS if max_vias is None else int(max_vias)
    cap = max(0, cap)
    if spacing <= 0 or cap <= 0:
        return []
    seg_km = _haversine_km(a[0], a[1], b[0], b[1])
    if seg_km <= spacing:
        return []
    n = int(math.ceil(seg_km / spacing))
    n = max(2, min(n, cap + 1))
    # n equal parts → n-1 intermediate vias (exclude endpoints).
    return [
        (a[0] + (j / float(n)) * (b[0] - a[0]), a[1] + (j / float(n)) * (b[1] - a[1]))
        for j in range(1, n)
    ]


def _leg_max_deviation_km(chord_km: float) -> float:
    """Allowed max point-to-chord distance for a leg of ``chord_km``."""
    max_dev = float(config.DRAW_LEG_MAX_DEVIATION_KM)
    # Short legs: tighten absolute deviation so local hooks still fail.
    if chord_km < 8.0:
        max_dev = min(max_dev, max(0.8, 0.35 * chord_km + 0.35))
    return max_dev


def _leg_length_ratio(
    leg: dict,
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    chord_km = _haversine_km(a[0], a[1], b[0], b[1])
    routed_km = float(leg.get("distance_km", 0.0))
    if chord_km <= 0.05:
        return 1.0
    return routed_km / chord_km


def _leg_corridor_ok(
    leg: dict,
    a: tuple[float, float],
    b: tuple[float, float],
) -> bool:
    """True when the routed leg stays near the drawn chord (length + deviation)."""
    chord_km = _haversine_km(a[0], a[1], b[0], b[1])
    if chord_km > 0.05 and _leg_length_ratio(leg, a, b) > float(
        config.DRAW_LEG_MAX_LENGTH_RATIO
    ):
        return False
    max_dev = _leg_max_deviation_km(chord_km)
    return _max_chord_deviation_km(list(leg.get("coords") or []), a, b) <= max_dev


def _leg_rank(
    leg: dict,
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[int, float, float]:
    """Sort key for draw legs — lower is better (corridor, length ratio, deviation)."""
    return (
        0 if _leg_corridor_ok(leg, a, b) else 1,
        _leg_length_ratio(leg, a, b),
        _max_chord_deviation_km(list(leg.get("coords") or []), a, b),
    )


def _route_one_chord_leg(
    a: tuple[float, float],
    b: tuple[float, float],
) -> dict:
    """OSRM-route one click pair: direct A→B first, then sparse chord vias."""
    candidates: list[dict] = []
    seen: set[tuple] = set()

    def _try(vias: list[tuple[float, float]]) -> dict | None:
        key = tuple((round(v[0], 5), round(v[1], 5)) for v in vias)
        if key in seen:
            return None
        seen.add(key)
        try:
            if vias:
                return get_osrm_routes(
                    a, b, alternatives=0, waypoints=vias, continue_straight=True,
                )[0]
            return get_osrm_routes(a, b, alternatives=0)[0]
        except Exception:
            log.warning(
                "draw_chord_leg_failed vias=%s", len(vias), exc_info=True,
            )
            return None

    # 1. Plain direct A→B — often the cleanest when the sketch is sparse.
    direct = _try([])
    if direct is not None:
        candidates.append(direct)
        if _leg_corridor_ok(direct, a, b):
            return direct

    # 2. Sparse chord vias (coarser spacing first) when direct wanders off-sketch.
    base = float(config.DRAW_CHORD_VIA_SPACING_KM)
    for spacing in (base, base * 1.25):
        leg = _try(_chord_via_points(a, b, spacing_km=spacing))
        if leg is None:
            continue
        candidates.append(leg)
        if _leg_corridor_ok(leg, a, b):
            return leg

    if not candidates:
        raise RuntimeError("OSRM failed for drawn chord leg.")

    best = min(candidates, key=lambda leg: _leg_rank(leg, a, b))
    if not _leg_corridor_ok(best, a, b):
        log.warning(
            "draw_chord_leg_off_corridor chord_km=%.2f routed_km=%.2f "
            "length_ratio=%.2f max_dev_km=%.2f",
            _haversine_km(a[0], a[1], b[0], b[1]),
            best.get("distance_km", 0.0),
            _leg_length_ratio(best, a, b),
            _max_chord_deviation_km(list(best.get("coords") or []), a, b),
        )
    return best


def _route_drawn_pairwise_legs(
    vertices: list[tuple[float, float]],
) -> dict:
    """Route each consecutive click pair along its chord and stitch geometries."""
    legs: list[dict] = []
    for i in range(len(vertices) - 1):
        legs.append(_route_one_chord_leg(vertices[i], vertices[i + 1]))
    return _stitch_osrm_leg_routes(legs)


def _match_fallback_acceptable(
    matched: dict,
    sketch_km: float,
) -> bool:
    """Reject match results that balloon far beyond the drawn sketch length."""
    if sketch_km <= 0:
        return True
    ratio = float(config.DRAW_MATCH_MAX_LENGTH_RATIO)
    return float(matched.get("distance_km", 0.0)) <= sketch_km * ratio


def _snap_drawn_route_to_roads(
    vertices: list[tuple[float, float]],
) -> tuple[dict, str]:
    """Snap a user sketch to drivable roads along drawn click chords.

    Primary: one OSRM request per consecutive pair (V[i]→V[i+1]); try plain
    direct A→B first, then sparse chord vias with ``continue_straight`` if the
    direct leg wanders off the sketch. Never routes first→last through all
    clicks in one call (that wanders off the sketch).

    Fallback: map-match a densified sketch polyline (clicks + chord samples)
    with a length-ratio guard.

    Returns ``(route_dict, method)`` where method is ``"pairwise"`` or ``"match"``
    (optionally ``*_pruned`` after spur cleanup).
    """
    sketch_km = _polyline_length_km(vertices)
    route: dict | None = None
    method = "pairwise"

    try:
        route = _route_drawn_pairwise_legs(vertices)
        method = "pairwise"
    except Exception:
        log.warning("draw_pairwise_route_failed; trying match fallback", exc_info=True)
        route = None

    if route is None:
        # Sample along the same straight-line preview the UI shows.
        trace = _densify_for_match(vertices)
        matched = get_osrm_match_route(trace)
        if matched is not None and _match_fallback_acceptable(matched, sketch_km):
            route = matched
            method = "match"
        elif matched is not None:
            log.warning(
                "draw_match_rejected_length matched_km=%.2f sketch_km=%.2f",
                matched.get("distance_km", 0.0),
                sketch_km,
            )

    if route is None:
        raise RuntimeError("Could not snap drawn route to roads.")

    pruned = _apply_spur_prune(route)
    if len(pruned.get("coords") or []) < len(route.get("coords") or []):
        if method == "pairwise":
            method = "pairwise_pruned"
        elif method == "match":
            method = "match_pruned"
    return pruned, method


def _densify_polyline(vertices: list[tuple[float, float]],
                      spacing_m: float = 100.0) -> list[tuple[float, float]]:
    """Insert points along straight segments ~spacing_m apart (for off-road scoring)."""
    if len(vertices) < 2:
        return list(vertices)
    out: list[tuple[float, float]] = [vertices[0]]
    for i in range(1, len(vertices)):
        a, b = vertices[i - 1], vertices[i]
        seg_km = _haversine_km(a[0], a[1], b[0], b[1])
        if seg_km <= 0:
            out.append(b)
            continue
        n = max(1, int(seg_km * 1000.0 / spacing_m))
        for j in range(1, n + 1):
            t = j / float(n)
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return out


def _parse_draw_vertices(raw: list) -> list[tuple[float, float]]:
    """Validate user sketch vertices: 2–50 points, valid lat/lng."""
    if not isinstance(raw, list):
        raise ValueError("coords must be a list of [lat, lng] pairs.")
    if len(raw) < 2:
        raise ValueError("At least 2 vertices are required.")
    if len(raw) > MAX_DRAW_VERTICES:
        raise ValueError(f"At most {MAX_DRAW_VERTICES} vertices are allowed.")
    out: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Each coord must be [lat, lng].")
        try:
            lat, lng = float(item[0]), float(item[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("Each coord must be numeric [lat, lng].") from exc
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            raise ValueError("Coordinates out of range.")
        out.append((lat, lng))
    sketch_km = _polyline_length_km(out)
    if sketch_km > MAX_DRAW_PATH_KM:
        raise ValueError(f"Sketch exceeds {MAX_DRAW_PATH_KM:.0f} km.")
    return out


def score_drawn_route(
    vertices: list[tuple[float, float]],
    *,
    profile_id: str | None = None,
    weights: dict | None = None,
    snap_to_roads: bool = True,
    time_budget: bool = True,
    source: str = "esri",
) -> dict:
    """Route through user-clicked vertices on real roads, then score fully.

    When ``snap_to_roads`` is True (default), each consecutive click pair is
    OSRM-routed along the straight sketch chord (direct leg first, then sparse
    chord vias; map-match is a guarded fallback). Otherwise the sketch is densified (~100 m)
    and scored as straight segments (no OSRM).
    """
    verts = _parse_draw_vertices(list(vertices))
    a, b = verts[0], verts[-1]
    use_time_budget = bool(time_budget)
    plan_t0 = time.perf_counter()
    work_deadline = (
        plan_t0 + config.PLAN_BUDGET_SEC - config.PLAN_RESERVE_SEC
        if use_time_budget else None
    )
    budget_reasons: list[str] = []

    if snap_to_roads:
        route, road_snap_method = _snap_drawn_route_to_roads(verts)
        if route["distance_km"] > MAX_DRAW_PATH_KM:
            raise ValueError(f"Routed path exceeds {MAX_DRAW_PATH_KM:.0f} km.")
    else:
        road_snap_method = None
        coords = _densify_polyline(verts, spacing_m=100.0)
        dist_km = _polyline_length_km(coords)
        if dist_km > MAX_DRAW_PATH_KM:
            raise ValueError(f"Sketch exceeds {MAX_DRAW_PATH_KM:.0f} km.")
        route = {
            "coords": coords,
            "distance_km": dist_km,
            "duration_min": (dist_km / _DRAW_SPEED_KMH) * 60.0,
            "motorway_km": 0.0,
            "directions": [{
                "text": f"Drawn route · {len(verts)} vertices",
                "distance_label": f"{dist_km:.1f} km",
                "lat": a[0],
                "lng": a[1],
            }],
        }

    def _lc_progress(done, total):
        pass  # draw mode uses POST + spinner; no SSE progress in v1

    features = enrich.fetch_landcover(
        _corridor_bbox(a, b, extras=verts),
        progress=_lc_progress,
        prefer_axis=(a, b),
        deadline=work_deadline,
    )
    if features and (
        features.get("deadline_stopped")
        or features.get("truncated")
        or features.get("landcover_incomplete")
    ):
        budget_reasons.append("landcover_truncated")

    score_route(
        route, features=features, source=source, weights=weights,
        sample_spacing_km=SAMPLE_SPACING_KM,
        max_samples=MAX_SAMPLES,
        colour=True,
    )

    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    climate_id = route.get("climate") or select_climate(mid[0], mid[1]).id
    land_used = bool(route.get("_landcover_usable"))
    land_incomplete = bool(features and features.get("truncated")) or (
        features is not None and not land_used
    )
    elapsed_ms = round((time.perf_counter() - plan_t0) * 1000.0, 1)
    summary = _route_summary(route, chosen=True, min_scenic=0.0)

    log.info(
        "plan_finished source=drawn elapsed_ms=%s budget_exhausted=%s "
        "budget_reasons=%s draw_vertices=%s snap_to_roads=%s",
        elapsed_ms,
        bool(budget_reasons),
        budget_reasons,
        len(verts),
        snap_to_roads,
    )

    return {
        "source": "drawn",
        "from": list(a),
        "to": list(b),
        "profile": profile_id,
        "snap_to_roads": snap_to_roads,
        "road_snap_method": road_snap_method,
        "draw_vertices": len(verts),
        "time_budget": use_time_budget,
        "elapsed_ms": elapsed_ms,
        "budget_exhausted": bool(budget_reasons),
        "budget_reasons": list(budget_reasons),
        "min_scenic": 0.0,
        "min_scenic_met": True,
        "climate": climate_id,
        "chosen": summary,
        "alternatives": [summary],
        "signals": {
            "colour": True,
            "terrain": (route.get("components") or {}).get("terrain") is not None,
            "landcover": land_used,
            "landcover_incomplete": land_incomplete,
            "climate": climate_id,
            "climate_name": climate_display_name(climate_id),
            "climates_used": route.get("climates_used") or [climate_id],
        },
    }
