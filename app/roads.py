"""Real-road routing (OSRM) + enriched scenic scoring & scenic route search.

For any A->B we:
  1. Ask OSRM (real OSM road network) for driving routes, and additionally
     build *scenic* candidates by routing through scenic "hotspot" waypoints.
  2. Score every candidate along its length by blending three signals:
        colour density (satellite) + terrain relief + OSM land cover.
  3. Return the candidate that best matches the user's scenic preference.

Works anywhere OSRM/Overpass/Open-Meteo have coverage (all of the UK, most of
the world) and needs no precomputed grid. Every external signal degrades
gracefully; colour always works so a route can always be scored.
"""
from __future__ import annotations

import math
import re
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from . import config, scoring, enrich

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{coords}"

_session = requests.Session()
_session.headers.update({"User-Agent": "ScenicRoutePlanner/1.0"})

# Scenic sampling controls.
SAMPLE_SPACING_KM = 2.0     # score a point roughly every 2 km of road
MAX_SAMPLES = 220           # cap work on very long routes
RENDER_MAX_POINTS = 400     # simplify geometry sent to the browser
SCORE_WORKERS = 16


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
                    waypoints: list[tuple[float, float]] | None = None) -> list[dict]:
    """Return real road routes A->B (optionally via waypoints). Coords (lat,lng).

    `waypoint` routes via a single point; `waypoints` chains several ordered
    intermediate points (used to force a path through multiple scenic zones).
    """
    vias = list(waypoints) if waypoints else ([waypoint] if waypoint else [])
    if not vias:
        coords = f"{a[1]},{a[0]};{b[1]},{b[0]}"  # OSRM wants lng,lat
    else:
        mid = ";".join(f"{w[1]},{w[0]}" for w in vias)
        coords = f"{a[1]},{a[0]};{mid};{b[1]},{b[0]}"
        alternatives = 0  # OSRM disallows alternatives with intermediate waypoints
    url = OSRM_URL.format(coords=coords)
    params = {
        "overview": "full",
        "geometries": "geojson",
        "alternatives": "true" if alternatives else "false",
        "steps": "true",  # turn-by-turn maneuvers for Google-Maps-style directions
    }
    if alternatives:
        params["alternatives"] = str(alternatives)
    r = _session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(data.get("message", "OSRM returned no route."))
    out = []
    for rt in data["routes"]:
        # geometry coords are [lng, lat]
        coords_ll = [(c[1], c[0]) for c in rt["geometry"]["coordinates"]]
        out.append({
            "coords": coords_ll,              # list of (lat, lng)
            "distance_km": rt["distance"] / 1000.0,
            "duration_min": rt["duration"] / 60.0,
            "directions": _directions_from_legs(rt.get("legs", [])),
            "motorway_km": _motorway_km(rt.get("legs", [])),
        })
    return out


# Motorway detection from OSRM step road references. The demo server leaves
# intersection `classes` empty, but `ref` is reliable: UK/IE motorways carry
# refs like M6, M1, M25, A1(M), A38(M). We flag those and sum their distance.
_MOTORWAY_RE = re.compile(r"(?:^|[\s;])M\d|\(M\)", re.I)


def _is_motorway(ref: str | None, name: str | None) -> bool:
    return bool(_MOTORWAY_RE.search(f"{ref or ''} {name or ''}"))


def _motorway_km(legs: list[dict]) -> float:
    """Kilometres of the route driven on motorways."""
    total = 0.0
    for leg in legs:
        for step in leg.get("steps", []):
            if _is_motorway(step.get("ref"), step.get("name")):
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


def score_route(route: dict, features: dict | None = None, source: str = "esri",
                weights: dict | None = None) -> dict:
    """Score a route by blending colour density, terrain relief and land cover.

    weights (optional) overrides the blend, e.g. {"colour":.4,"terrain":.3,"landcover":.3}.
    """
    w_colour = weights["colour"] if weights else config.BLEND_COLOUR
    w_terrain = weights["terrain"] if weights else config.BLEND_TERRAIN
    w_land = weights["landcover"] if weights else config.BLEND_LANDCOVER
    coords = route["coords"]
    cum = _cumulative_km(coords)
    sample_idx = _sample_indices(cum, SAMPLE_SPACING_KM, MAX_SAMPLES)
    sample_coords = [coords[i] for i in sample_idx]

    # 1. Colour (satellite) -- always available.
    def _colour(i):
        lat, lng = coords[i]
        return i, scoring.score_location(lat, lng, source=source).score

    colour: dict[int, float] = {}
    with ThreadPoolExecutor(max_workers=SCORE_WORKERS) as ex:
        for i, sc in ex.map(_colour, sample_idx):
            colour[i] = sc

    # 2. Terrain (elevation relief) -- optional.
    terrain: dict[int, float] | None = None
    elev_by_idx: dict[int, float] | None = None
    elevs = enrich.elevation_batch(sample_coords)
    if elevs and len(elevs) == len(sample_idx):
        rs = enrich.relief_scores(elevs)
        terrain = {sample_idx[k]: rs[k] for k in range(len(sample_idx))}
        elev_by_idx = {sample_idx[k]: elevs[k] for k in range(len(sample_idx))}

    # 3. Land cover (OSM) -- optional. Details give the dominant nearby feature.
    land: dict[int, float] | None = None
    land_detail: dict[int, dict] | None = None
    if features is not None:
        ld = enrich.landcover_details(sample_coords, features)
        land = {sample_idx[k]: ld[k]["score"] for k in range(len(sample_idx))}
        land_detail = {sample_idx[k]: ld[k] for k in range(len(sample_idx))}

    # Blend per sample, renormalising weights over whatever signals we have.
    combined: dict[int, float] = {}
    for i in sample_idx:
        vals = [(colour[i], w_colour)]
        if terrain is not None:
            vals.append((terrain[i], w_terrain))
        if land is not None:
            vals.append((land[i], w_land))
        wsum = sum(w for _, w in vals) or 1.0
        combined[i] = sum(v * w for v, w in vals) / wsum

    # Per-sample explanation used to make each road point self-explaining.
    explain: dict[int, dict] = {}
    for i in sample_idx:
        explain[i] = {
            "colour": round(colour[i]),
            "terrain": round(terrain[i]) if terrain is not None else None,
            "landcover": round(land[i]) if land is not None else None,
            "elev_m": round(elev_by_idx[i]) if elev_by_idx is not None else None,
            "near": land_detail[i] if land_detail is not None else None,
        }

    route["avg_scenic_score"] = round(_dist_weighted_avg(cum, combined), 1)
    route["components"] = {
        "colour": round(_dist_weighted_avg(cum, colour), 1),
        "terrain": round(_dist_weighted_avg(cum, terrain), 1) if terrain else None,
        "landcover": round(_dist_weighted_avg(cum, land), 1) if land else None,
    }
    route["render"] = _render_polyline(coords, cum, combined, explain)
    route["num_samples"] = len(sample_idx)
    return route


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
        elif t <= 30: neg.append("flat ground")
    # Prefer concrete nearby features over the abstract land-cover number.
    if near.get("pos_label"):
        d = near.get("pos_dist")
        pos.append(f"near {near['pos_label']}" + (f" ({d} km)" if d is not None else ""))
    elif l is not None and l >= 60:
        pos.append("natural land cover")
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
            pt["reason"] = _reason(score, comp)
        pts.append(pt)
    return pts


def _corridor_bbox(a, b, pad=None):
    pad = pad if pad is not None else config.CORRIDOR_PAD_DEG
    return (min(a[0], b[0]) - pad, min(a[1], b[1]) - pad,
            max(a[0], b[0]) + pad, max(a[1], b[1]) + pad)


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
    # Elevation for all candidates in one batched call.
    elevs = enrich.elevation_batch(pts) or [0.0] * len(pts)
    emax = max(elevs) or 1.0

    scored = []
    land_scores = enrich.landcover_scores(pts, features) if features else [50.0] * len(pts)
    for (lat, lng), elev, land in zip(pts, elevs, land_scores):
        detour = _haversine_km(*a, lat, lng) + _haversine_km(lat, lng, *b)
        if direct > 0 and detour > detour_ratio * direct:
            continue
        est = 0.6 * land + 0.4 * (elev / emax * 100.0)  # cheap scenic estimate
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


def _nearby_parks(a, b):
    """National parks worth diverting through: near the A->B corridor or only a
    modest detour beyond it. Returns [(name, lat, lng)] ordered along the route.
    Travel time is irrelevant here — this is the 'explore everything' booster."""
    direct = _haversine_km(*a, *b) or 1.0
    hits = []
    for name, lat, lng in config.NATIONAL_PARKS:
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
         min_scenic: float = 0.0, explore_all: bool = False, progress=None) -> dict:
    """Scenic road-routing pipeline: real roads + scenic waypoint search.

    weights / detour_factor let a scenic profile reshape scoring and routing.
    avoid_motorways applies a harsh per-km penalty to any motorway mileage.
    min_scenic (0-100) is a target: the recommended route is the best-cost one
    whose average scenic score meets it, falling back to the most scenic route
    available if none qualify.
    explore_all disregards travel time entirely: routes are ranked purely by
    scenic score and the planner deliberately diverts through national parks and
    far scenic terrain, however long the drive becomes.
    progress, if given, is called with small event dicts as the search runs
    (used to stream live search progress to the UI).
    """
    preference = max(0.0, min(1.0, float(preference)))
    min_scenic = max(0.0, min(100.0, float(min_scenic)))
    detour = detour_factor if detour_factor is not None else config.DETOUR_FACTOR
    mw_pen = config.MOTORWAY_PENALTY_MIN_PER_KM if avoid_motorways else 0.0

    def _emit(ev):
        if progress:
            try:
                progress(ev)
            except Exception:
                pass

    _emit({"type": "start", "from": list(a), "to": list(b),
           "min_scenic": min_scenic, "avoid_motorways": avoid_motorways,
           "explore_all": explore_all})

    # Land cover for the whole corridor, fetched once and reused everywhere. In
    # explore mode we read a much wider corridor so distant parks score properly.
    # Cells stream in from a persistent cache; cold cells emit progress so the
    # live search shows tile-by-tile reading instead of appearing frozen.
    _emit({"type": "phase", "label": "Reading land cover across the corridor…"})
    lc_pad = (config.CORRIDOR_PAD_DEG + 0.35) if explore_all else None

    def _lc_progress(done, total):
        _emit({"type": "landcover", "done": done, "total": total,
               "label": f"Reading land cover… {done}/{total} tiles"})

    features = enrich.fetch_landcover(_corridor_bbox(a, b, pad=lc_pad),
                                      progress=_lc_progress)

    def _score_and_emit(pool, kind):
        """Score any unscored routes one at a time, emitting each as it lands."""
        for rt in pool:
            if "avg_scenic_score" not in rt:
                score_route(rt, features=features, source=source, weights=weights)
                _emit({
                    "type": "candidate",
                    "kind": kind,
                    "coords": _decimate(rt["coords"], 80),
                    "scenic": rt["avg_scenic_score"],
                    "distance_km": round(rt["distance_km"], 1),
                    "duration_min": round(rt["duration_min"], 1),
                    "motorway_km": round(rt.get("motorway_km", 0.0), 1),
                    "meets_min": (min_scenic <= 0) or rt["avg_scenic_score"] >= min_scenic,
                    "total": len(pool),
                })

    def _qualifies(pool):
        return any(rt["avg_scenic_score"] >= min_scenic for rt in pool) if min_scenic > 0 else True

    # Base (fastest-ish) routes plus scenic candidates via hotspot waypoints.
    _emit({"type": "phase", "label": "Finding base routes on real roads…"})
    routes = get_osrm_routes(a, b, alternatives=3)
    if features is not None:
        _emit({"type": "phase", "label": "Searching scenic detours through the corridor…"})
        for wp in _candidate_waypoints(a, b, features):
            try:
                routes.extend(get_osrm_routes(a, b, waypoint=wp))
            except Exception:
                continue

    routes = _dedupe(routes)
    _emit({"type": "phase", "label": f"Scoring {len(routes)} candidate routes…"})
    _score_and_emit(routes, "base")

    # --- Explore everything: divert through national parks -------------------
    # Travel time is disregarded, so we deliberately route via every national
    # park near the corridor (individually) and stitch an ordered "grand tour"
    # through several of them. This is what pulls routes into the Lake District,
    # Snowdonia, the Peak District, etc. even when they are well off the direct
    # line.
    if explore_all:
        park_pts, park_names = _nearby_parks(a, b)
        if park_pts:
            names = ", ".join(sorted(set(park_names.values())))
            _emit({"type": "phase",
                   "label": f"Diverting through national parks: {names}…"})
            # Make sure land cover actually covers the parks we're about to divert
            # through, otherwise those off-corridor segments score a neutral 50.
            # Fetch a small box per park so already-cached parks resolve instantly
            # and only genuinely new areas hit the network.
            d = config.EXPLORE_PARK_BBOX_DEG
            for (plat, plng) in park_pts:
                pf = enrich.fetch_landcover((plat - d, plng - d, plat + d, plng + d),
                                            progress=_lc_progress)
                features = enrich.merge_landcover(features, pf)
            new = []
            for wp in park_pts:                              # via each park alone
                try:
                    new.extend(get_osrm_routes(a, b, waypoint=wp))
                except Exception:
                    continue
            # Ordered multi-park "grand tours" (2..N parks in sequence).
            for k in range(2, min(config.EXPLORE_MAX_CHAIN, len(park_pts)) + 1):
                try:
                    new.extend(get_osrm_routes(a, b, waypoints=park_pts[:k]))
                except Exception:
                    continue
            routes = _dedupe(routes + new)
            # Re-score everything against the park-inclusive land cover so the
            # scenic-first ranking reflects the true scenery of each route.
            for rt in routes:
                rt.pop("avg_scenic_score", None)
            _score_and_emit(routes, "park")

    # --- Hard scenic target: keep expanding the search until it is met --------
    # Each round widens the corridor, allows longer detours, samples a denser
    # grid, and chains several scenic hotspots so the road is forced through
    # more scenic terrain. Escalates until a route meets the floor (or the
    # search budget is exhausted, which only happens if the terrain genuinely
    # cannot reach the target anywhere in range).
    if min_scenic > 0 and not _qualifies(routes):
        for ri, (pad, grid, cand, ratio, chain) in enumerate(config.HARD_TARGET_ROUNDS, 1):
            best = max((r["avg_scenic_score"] for r in routes), default=0)
            _emit({"type": "round", "n": ri, "total": len(config.HARD_TARGET_ROUNDS),
                   "best": round(best, 1), "target": min_scenic,
                   "label": f"Target {min_scenic:.0f} not met yet (best {best:.0f}) — "
                            f"widening search, round {ri}/{len(config.HARD_TARGET_ROUNDS)}…"})
            feats = enrich.fetch_landcover(_corridor_bbox(a, b, pad=pad))
            if feats is not None:
                features = feats  # wider land cover strictly improves scoring
            wps = _candidate_waypoints(a, b, features, pad=pad, grid=grid,
                                       candidates=cand, detour_ratio=ratio)
            new = []
            for wp in wps:                                   # single-hotspot routes
                try:
                    new.extend(get_osrm_routes(a, b, waypoint=wp))
                except Exception:
                    continue
            # Chained routes forced through the best hotspots, ordered along A->B.
            for k in range(2, min(chain, len(wps)) + 1):
                chain_wps = _order_along(a, b, wps[:k])
                try:
                    new.extend(get_osrm_routes(a, b, waypoints=chain_wps))
                except Exception:
                    continue
            routes = _dedupe(routes + new)
            _score_and_emit(routes, "expanded")
            if _qualifies(routes):
                break

    # Ranking. In explore mode, travel time is disregarded entirely: routes are
    # ranked purely by scenic score (motorway mileage still breaks ties, since
    # motorways aren't scenic). Otherwise blend travel time with scenic quality.
    def cost(rt):
        penalty = detour * (1.0 - rt["avg_scenic_score"] / 100.0)
        return rt["duration_min"] * (1.0 + preference * penalty) + mw_pen * rt.get("motorway_km", 0.0)

    if explore_all:
        routes.sort(key=lambda rt: (-rt["avg_scenic_score"], rt.get("motorway_km", 0.0),
                                    rt["duration_min"]))
    else:
        routes.sort(key=cost)

    # Apply the minimum-scenic target as a hard floor: the recommended route is
    # the best-cost route that meets it. Only if the escalating search still
    # could not reach the target anywhere in range do we fall back (flagged).
    min_scenic_met = True
    if min_scenic > 0.0:
        qualifying = [r for r in routes if r["avg_scenic_score"] >= min_scenic]
        if qualifying:
            chosen = qualifying[0]
        else:
            chosen = max(routes, key=lambda r: r["avg_scenic_score"])
            min_scenic_met = False
    else:
        chosen = routes[0]

    result = {
        "preference": preference,
        "profile": profile_id,
        "avoid_motorways": avoid_motorways,
        "min_scenic": min_scenic,
        "min_scenic_met": min_scenic_met,
        "explore_all": explore_all,
        "from": list(a),
        "to": list(b),
        "signals": {
            "colour": True,
            "terrain": any(r["components"]["terrain"] is not None for r in routes),
            "landcover": features is not None,
        },
        "chosen": _route_summary(chosen, chosen=True, min_scenic=min_scenic),
        "alternatives": [_route_summary(r, chosen=(r is chosen), min_scenic=min_scenic) for r in routes],
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
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "error", "message": str(exc)})
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
        "num_samples": rt.get("num_samples", 0),
        "render": rt["render"],
        "directions": rt.get("directions", []),
        "motorway_km": round(rt.get("motorway_km", 0.0), 1),
    }
