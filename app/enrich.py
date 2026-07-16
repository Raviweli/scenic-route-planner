"""Scenic-signal enrichment: terrain ruggedness + OSM map context.

Colour density alone is a weak scenic proxy (a motorway across a green field
still looks green). This module adds the two signals that most improve accuracy:

  * terrain   - elevation relief from Open-Meteo (free, no key). Mountains and
                hilly drives are scenic; flat is softened when other signals
                (or water/coast proximity) already say the place is scenic.
  * landcover - OSM map context from Overpass (free, no key). Proximity to
                forest, water, coastline, peaks, parks, beaches, wetlands and
                similar boosts the score; industrial / quarry / landfill lowers it.

Every network call degrades gracefully: if a source is unavailable the caller
renormalises the blend over whatever signals remain (colour always works).
"""
from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np
import requests

from . import config
from .climates import get_climate

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"User-Agent": "ScenicRoutePlanner/1.0", "Accept": "application/json"})

# In-process caches with TTL + max size (elevation by coord, land-cover by cell).
_LANDCOVER_CACHE: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_ELEV_CACHE: OrderedDict[tuple[float, float], tuple[float, float]] = OrderedDict()

OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"

# Positive scenic map-context tags (OSM key/value, weight 0..1).
# Global fixed pack — no per-climate weight matrix.
POSITIVE_TAGS = [
    ('natural', 'water', 1.0),
    ('natural', 'coastline', 1.0),
    ('natural', 'beach', 0.95),
    ('natural', 'cliff', 0.9),
    ('natural', 'wetland', 0.75),
    ('natural', 'scrub', 0.65),
    ('natural', 'heath', 0.7),
    ('natural', 'grassland', 0.55),
    ('natural', 'glacier', 1.0),
    ('natural', 'wood', 0.8),
    ('natural', 'peak', 1.0),
    ('natural', 'fell', 0.7),
    ('natural', 'moor', 0.65),
    ('natural', 'sand', 0.7),
    ('natural', 'dune', 0.75),
    ('natural', 'bare_rock', 0.8),
    ('natural', 'scree', 0.7),
    ('natural', 'ridge', 0.85),
    ('natural', 'volcano', 0.95),
    ('waterway', 'waterfall', 0.95),
    ('tourism', 'viewpoint', 0.85),
    ('landuse', 'forest', 0.8),
    ('landuse', 'meadow', 0.5),
    ('leisure', 'nature_reserve', 0.9),
    # Town parks are green but must not outrank countryside when urban fabric
    # is nearby — weight is modest; landcover_scores also dampens under neg_eff.
    ('leisure', 'park', 0.35),
    ('leisure', 'beach_resort', 0.7),
    ('boundary', 'national_park', 1.0),
    ('boundary', 'protected_area', 0.7),
]
NEGATIVE_TAGS = [
    ('landuse', 'industrial', 1.0),
    ('landuse', 'residential', 0.88),
    ('landuse', 'retail', 0.92),
    ('landuse', 'commercial', 0.88),
    ('landuse', 'quarry', 1.0),
    ('landuse', 'landfill', 1.0),
    ('landuse', 'construction', 0.85),
    ('landuse', 'railway', 0.7),
    # Dense built-up pockets — helps keep corridors out of town centres.
    ('landuse', 'garages', 0.75),
]

# Human-friendly noun phrases for each land-cover tag, used in explanations.
FRIENDLY = {
    ('natural', 'water'): 'water',
    ('natural', 'coastline'): 'the coast',
    ('natural', 'beach'): 'a beach',
    ('natural', 'cliff'): 'a cliff',
    ('natural', 'wetland'): 'wetland',
    ('natural', 'scrub'): 'scrub',
    ('natural', 'heath'): 'heath',
    ('natural', 'grassland'): 'grassland',
    ('natural', 'glacier'): 'a glacier',
    ('natural', 'wood'): 'woodland',
    ('natural', 'peak'): 'a summit',
    ('natural', 'fell'): 'a fell',
    ('natural', 'moor'): 'moorland',
    ('natural', 'sand'): 'sand',
    ('natural', 'dune'): 'dunes',
    ('natural', 'bare_rock'): 'bare rock',
    ('natural', 'scree'): 'scree',
    ('natural', 'ridge'): 'a ridge',
    ('natural', 'volcano'): 'a volcano',
    ('waterway', 'waterfall'): 'a waterfall',
    ('tourism', 'viewpoint'): 'a viewpoint',
    ('landuse', 'forest'): 'forest',
    ('landuse', 'meadow'): 'meadow',
    ('leisure', 'nature_reserve'): 'a nature reserve',
    ('leisure', 'park'): 'a park',
    ('leisure', 'beach_resort'): 'a beach',
    ('boundary', 'national_park'): 'a national park',
    ('boundary', 'protected_area'): 'protected land',
    ('landuse', 'industrial'): 'industrial land',
    ('landuse', 'residential'): 'housing',
    ('landuse', 'retail'): 'a retail area',
    ('landuse', 'commercial'): 'a commercial area',
    ('landuse', 'quarry'): 'a quarry',
    ('landuse', 'landfill'): 'a landfill',
    ('landuse', 'construction'): 'a building site',
    ('landuse', 'railway'): 'railway land',
    ('landuse', 'garages'): 'garages',
}

# Tags / labels that count as water/coast for the flat-relief exception.
WATER_TAGS = {
    ('natural', 'water'),
    ('natural', 'coastline'),
    ('natural', 'beach'),
    ('natural', 'wetland'),
    ('leisure', 'beach_resort'),
}
WATER_LABELS = {FRIENDLY[t] for t in WATER_TAGS if t in FRIENDLY}

_LANDCOVER_CLIMATE_FACTORS: dict[str, dict[tuple[str, str], float]] = {
    "mediterranean": {
        ("natural", "scrub"): 1.25,
        ("natural", "heath"): 1.15,
        ("natural", "sand"): 1.15,
        ("natural", "bare_rock"): 1.2,
        ("natural", "cliff"): 1.2,
        ("natural", "beach"): 1.15,
        ("landuse", "meadow"): 0.9,
        ("leisure", "park"): 0.9,
    },
    "tropical_rainforest": {
        ("natural", "wood"): 1.2,
        ("landuse", "forest"): 1.2,
        ("natural", "wetland"): 1.15,
        ("waterway", "waterfall"): 1.1,
        ("natural", "sand"): 0.85,
        ("natural", "bare_rock"): 0.9,
    },
    "tropical_monsoon_savanna": {
        ("natural", "scrub"): 1.15,
        ("natural", "grassland"): 1.2,
        ("natural", "heath"): 1.1,
        ("natural", "sand"): 1.05,
        ("landuse", "meadow"): 0.9,
        ("natural", "wood"): 0.95,
    },
    "arid_hot": {
        ("natural", "sand"): 1.3,
        ("natural", "dune"): 1.35,
        ("natural", "bare_rock"): 1.3,
        ("natural", "cliff"): 1.25,
        ("natural", "ridge"): 1.25,
        ("natural", "scree"): 1.2,
        ("natural", "scrub"): 1.15,
        ("natural", "grassland"): 0.85,
        ("landuse", "forest"): 0.85,
        ("landuse", "meadow"): 0.8,
        ("leisure", "park"): 0.85,
    },
    "arid_cold": {
        ("natural", "sand"): 1.1,
        ("natural", "dune"): 1.1,
        ("natural", "bare_rock"): 1.25,
        ("natural", "cliff"): 1.2,
        ("natural", "ridge"): 1.2,
        ("natural", "scree"): 1.2,
        ("natural", "scrub"): 1.1,
        ("natural", "grassland"): 1.05,
        ("landuse", "meadow"): 0.85,
    },
    "boreal": {
        ("natural", "wood"): 1.15,
        ("landuse", "forest"): 1.2,
        ("natural", "glacier"): 1.1,
        ("natural", "wetland"): 1.1,
        ("natural", "sand"): 0.85,
        ("leisure", "park"): 0.9,
    },
    "tundra_polar": {
        ("natural", "glacier"): 1.35,
        ("natural", "bare_rock"): 1.25,
        ("natural", "scree"): 1.2,
        ("natural", "ridge"): 1.2,
        ("natural", "sand"): 1.05,
        ("natural", "wood"): 0.7,
        ("landuse", "forest"): 0.7,
        ("landuse", "meadow"): 0.75,
        ("leisure", "park"): 0.85,
    },
    "alpine": {
        ("natural", "glacier"): 1.3,
        ("natural", "bare_rock"): 1.25,
        ("natural", "scree"): 1.2,
        ("natural", "ridge"): 1.2,
        ("natural", "peak"): 1.15,
        ("natural", "cliff"): 1.15,
        ("natural", "heath"): 1.1,
        ("landuse", "meadow"): 0.9,
    },
    "subtropical_humid": {
        ("natural", "wood"): 1.15,
        ("landuse", "forest"): 1.15,
        ("natural", "wetland"): 1.15,
        ("natural", "beach"): 1.1,
    },
    "oceanic_islands": {
        ("natural", "beach"): 1.2,
        ("natural", "cliff"): 1.2,
        ("natural", "volcano"): 1.25,
        ("natural", "bare_rock"): 1.15,
        ("natural", "wood"): 1.1,
    },
}

_FLAT_SCENIC_CLIMATES = {"arid_hot", "arid_cold", "tundra_polar", "alpine"}


# ---------------------------------------------------------------------------
# Terrain (elevation relief)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Bounded in-process caches
# ---------------------------------------------------------------------------
def _cache_get(cache: OrderedDict, key):
    entry = cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts >= config.CACHE_TTL_SEC:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return value


def _cache_set(cache: OrderedDict, key, value) -> None:
    cache[key] = (time.monotonic(), value)
    cache.move_to_end(key)
    while len(cache) > config.CACHE_MAX_ENTRIES:
        cache.popitem(last=False)


def cache_entry_counts() -> dict[str, int]:
    """Cheap in-process cache sizes for /api/health (no upstream I/O)."""
    disk_elev = 0
    try:
        disk_elev = sum(1 for _ in config.ELEV_CACHE_DIR.glob("e_*.npz"))
    except Exception:
        disk_elev = 0
    return {
        "elevation": len(_ELEV_CACHE),
        "elevation_disk": disk_elev,
        "landcover": len(_LANDCOVER_CACHE),
    }


def _elev_disk_path(key: tuple[float, float]):
    lat, lng = key
    # Filename-safe: 4-dp coords match the in-process key rounding.
    return config.ELEV_CACHE_DIR / f"e_{lat:.4f}_{lng:.4f}.npz"


def _elev_from_disk(key: tuple[float, float]) -> float | None:
    path = _elev_disk_path(key)
    if not path.exists():
        return None
    try:
        with np.load(path) as z:
            return float(z["elev"])
    except Exception:
        return None


def _elev_to_disk(key: tuple[float, float], elev: float) -> None:
    try:
        final = _elev_disk_path(key)
        tmp = final.parent / (final.stem + ".tmp.npz")
        np.savez(tmp, elev=float(elev))
        tmp.replace(final)
    except Exception:
        pass


def _elev_cache_get(key: tuple[float, float]) -> float | None:
    hit = _cache_get(_ELEV_CACHE, key)
    if hit is not None:
        return hit
    disk = _elev_from_disk(key)
    if disk is not None:
        _cache_set(_ELEV_CACHE, key, disk)
        return disk
    return None


def _elev_cache_set(key: tuple[float, float], elev: float) -> None:
    _cache_set(_ELEV_CACHE, key, elev)
    _elev_to_disk(key, elev)


def _gap_fill_elevations(vals: list[float | None]) -> list[float] | None:
    """Fill missing elevations by edge-fill + linear interpolation along order.

    Returns None only when every sample is missing (terrain truly unavailable).
    """
    n = len(vals)
    if n == 0:
        return []
    if all(v is None for v in vals):
        return None
    out: list[float | None] = list(vals)
    first = next(i for i, v in enumerate(out) if v is not None)
    for i in range(first):
        out[i] = out[first]
    last = next(i for i in range(n - 1, -1, -1) if out[i] is not None)
    for i in range(last + 1, n):
        out[i] = out[last]
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        left = float(out[i - 1])  # type: ignore[arg-type]
        right = float(out[j])  # type: ignore[arg-type]
        span = j - i + 1
        for k in range(i, j):
            t = (k - i + 1) / span
            out[k] = left + t * (right - left)
        i = j
    return [float(v) for v in out]  # type: ignore[misc]


def elevation_batch(coords: list[tuple[float, float]]) -> list[float] | None:
    """Return elevation (m) for each (lat,lng), or None if terrain is unavailable.

    Uses an in-process cache, a disk cache under ``data/elev_cache/``, and two
    free providers (Open-Meteo, then Opentopodata) so a single provider's daily
    quota (HTTP 429) doesn't zero out terrain. Missing points are gap-filled
    from neighbours — one failed sample no longer drops terrain for the whole
    route. Disk entries survive process restarts (friends-demo warm path).
    """
    if not coords:
        return []
    keys = [(round(c[0], 4), round(c[1], 4)) for c in coords]
    missing = [k for k in dict.fromkeys(keys) if _elev_cache_get(k) is None]

    if missing:
        for provider in (_elev_open_meteo, _elev_opentopo):
            still = [k for k in missing if _elev_cache_get(k) is None]
            if not still:
                break
            chunks = [still[i:i + 100] for i in range(0, len(still), 100)]
            workers = max(1, min(config.ELEV_CHUNK_WORKERS, len(chunks)))

            def _fetch_chunk(chunk):
                return chunk, provider(chunk)

            # Fetch chunks in parallel; write cache serially so OrderedDict stays safe.
            results = []
            if workers == 1:
                results = [_fetch_chunk(c) for c in chunks]
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    results = list(ex.map(_fetch_chunk, chunks))
            provider_failed = False
            for chunk, vals in results:
                if vals is None:
                    provider_failed = True
                    break
                for k, v in zip(chunk, vals):
                    if v is not None:
                        _elev_cache_set(k, float(v))
            if provider_failed:
                continue  # try next provider for whatever is still missing

    raw = [_elev_cache_get(k) for k in keys]
    return _gap_fill_elevations(raw)


def _elev_open_meteo(chunk: list[tuple[float, float]]) -> list[float] | None:
    lats = ",".join(f"{c[0]:.5f}" for c in chunk)
    lngs = ",".join(f"{c[1]:.5f}" for c in chunk)
    try:
        r = _session.get(OPEN_METEO_ELEV,
                         params={"latitude": lats, "longitude": lngs},
                         timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()["elevation"]
    except Exception:
        return None


def _elev_opentopo(chunk: list[tuple[float, float]]) -> list[float] | None:
    locs = "|".join(f"{c[0]:.5f},{c[1]:.5f}" for c in chunk)
    try:
        r = _session.get(config.OPEN_ELEV_FALLBACK,
                         params={"locations": locs}, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return [res.get("elevation") for res in r.json().get("results", [])]
    except Exception:
        return None


def relief_scores(elevs: list[float], window: int | None = None,
                  sample_km: list[float] | None = None,
                  window_km: float | None = None) -> list[float]:
    """Local relief along an ordered path -> 0..100 (higher = more rugged).

    Uses soft saturation so gentle hills and big mountains both differentiate
    instead of clipping everything above RELIEF_FULL_M to 100.

    Prefer ``sample_km`` (cumulative km at each elevation sample) with
    ``window_km`` (default ``RELIEF_WINDOW_KM``) so the physical window stays
    stable when long routes thin their sample spacing. Falls back to ±index
    ``window`` when no along-route distances are provided.
    """
    n = len(elevs)
    e = np.asarray(elevs, dtype=float)
    scale = float(config.RELIEF_FULL_M) or 1.0
    scores = np.zeros(n)
    use_km = (
        sample_km is not None
        and len(sample_km) == n
        and n > 0
    )
    half_km = float(window_km if window_km is not None else config.RELIEF_WINDOW_KM)
    idx_window = window if window is not None else config.RELIEF_WINDOW
    for i in range(n):
        if use_km:
            centre = float(sample_km[i])  # type: ignore[index]
            lo = i
            while lo > 0 and centre - float(sample_km[lo - 1]) <= half_km:  # type: ignore[index]
                lo -= 1
            hi = i
            while hi < n - 1 and float(sample_km[hi + 1]) - centre <= half_km:  # type: ignore[index]
                hi += 1
            hi += 1  # exclusive
        else:
            lo, hi = max(0, i - idx_window), min(n, i + idx_window + 1)
        rng = float(e[lo:hi].max() - e[lo:hi].min())
        scores[i] = 100.0 * (1.0 - math.exp(-rng / scale))
    return scores.tolist()


def soften_terrain(terrain: float, colour: float,
                   landcover: float | None = None,
                   near_water: bool = False,
                   climate_id: str | None = None) -> float:
    """Lift low relief when colour/map-context are strong, or near water/coast.

    Keeps ``blend_signals`` as a plain weighted average — we adjust the terrain
    *input* so flat coasts and colourful flats are not fully punished.
    """
    t = float(terrain)
    if near_water and t < 55.0:
        t = 55.0
    other = colour if landcover is None else max(colour, landcover)
    if other >= 55.0 and t < other:
        strength = min(1.0, (other - 50.0) / 50.0)
        t = t + strength * 0.55 * (other - t)
    if climate_id in _FLAT_SCENIC_CLIMATES and landcover is not None and landcover >= 65.0 and t < 48.0:
        lift = min(12.0, (landcover - 60.0) * 0.35)
        t = max(t, 42.0 + lift)
    return max(0.0, min(100.0, t))


def is_water_near(detail: dict | None) -> bool:
    """True when landcover detail shows water/coast/beach influence."""
    if not detail:
        return False
    label = detail.get("pos_label")
    return bool(label) and label in WATER_LABELS and detail.get("pos_dist") is not None


def _feature_multiplier(tag: tuple[str, str], climate_id: str | None) -> float:
    if not climate_id:
        return 1.0
    return _LANDCOVER_CLIMATE_FACTORS.get(climate_id, {}).get(tag, 1.0)


def _feature_score(
    lat: float,
    lng: float,
    feats: np.ndarray,
    labels: list[str],
    radius_km: float,
    climate_id: str | None,
) -> tuple[float, str | None, float | None]:
    d, w, j = _min_weighted_dist_km_idx(lat, lng, feats)
    if j is None:
        return 0.0, None, None
    tag = None
    if j < len(labels):
        for k, v, _wt in POSITIVE_TAGS + NEGATIVE_TAGS:
            if FRIENDLY.get((k, v), v) == labels[j]:
                tag = (k, v)
                break
    weight = w * _feature_multiplier(tag, climate_id) if tag is not None else w
    eff = max(0.0, 1.0 - d / radius_km) * weight
    if eff <= 0:
        return 0.0, None, None
    return eff, labels[j] if j < len(labels) else None, round(d, 2)


# ---------------------------------------------------------------------------
# Land cover / map context (Overpass)
# ---------------------------------------------------------------------------
def _overpass_query(bbox: tuple[float, float, float, float],
                    tags: list[tuple[str, str, float]] | None = None,
                    *,
                    out_mode: str | None = None,
                    out_n: int | None = None) -> str:
    s, w, n, e = bbox  # min_lat, min_lng, max_lat, max_lng
    b = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
    tag_list = tags if tags is not None else (POSITIVE_TAGS + NEGATIVE_TAGS)
    # `out center` is the default: dense UK tiles routinely 504 on a single
    # giant `out geom` union. Geometry sampling remains available via config.
    parts = [f'nwr["{k}"="{v}"]{b};' for k, v, _ in tag_list]
    geom_n = max(100, int(out_n if out_n is not None else config.LANDCOVER_OUT_GEOM))
    timeout_sec = max(1, int(round(config.LANDCOVER_CELL_TIMEOUT_SEC)))
    mode = (out_mode or config.LANDCOVER_OUT_MODE or "center").strip().lower()
    if mode == "geom":
        out_clause = f"out geom {geom_n}"
    else:
        out_clause = f"out center {geom_n}"
    return f"[out:json][timeout:{timeout_sec}];({''.join(parts)});{out_clause};"


def _tag_batches() -> list[list[tuple[str, str, float]]]:
    """Split POSITIVE+NEGATIVE tags into Overpass-sized batches.

    High-signal scenic / urban tags come first so a deadline still yields a
    usable partial map-context set.
    """
    priority = {
        ("natural", "water"), ("natural", "coastline"), ("natural", "beach"),
        ("natural", "wood"), ("landuse", "forest"), ("leisure", "park"),
        ("leisure", "nature_reserve"), ("boundary", "national_park"),
        ("landuse", "residential"), ("landuse", "industrial"),
        ("landuse", "commercial"), ("landuse", "retail"),
        ("landuse", "quarry"), ("landuse", "landfill"),
    }
    all_tags = list(POSITIVE_TAGS) + list(NEGATIVE_TAGS)
    head = [t for t in all_tags if (t[0], t[1]) in priority]
    tail = [t for t in all_tags if (t[0], t[1]) not in priority]
    ordered = head + tail
    batch_n = max(3, int(config.LANDCOVER_TAG_BATCH))
    return [ordered[i:i + batch_n] for i in range(0, len(ordered), batch_n)]


def _downsample_points(pts: list[tuple[float, float]],
                       max_n: int | None = None) -> list[tuple[float, float]]:
    max_n = max_n if max_n is not None else config.LANDCOVER_GEOM_SAMPLES
    if len(pts) <= max_n:
        return pts
    if max_n < 2:
        return [pts[len(pts) // 2]]
    out = [pts[0]]
    for i in range(1, max_n - 1):
        idx = int(round(i * (len(pts) - 1) / (max_n - 1)))
        out.append(pts[idx])
    out.append(pts[-1])
    return out


def _points_from_element(el: dict) -> list[tuple[float, float]]:
    """Prefer sampled way/relation geometry; fall back to node/centroid."""
    geom = el.get("geometry")
    if geom:
        pts = [(float(g["lat"]), float(g["lon"])) for g in geom
               if g.get("lat") is not None and g.get("lon") is not None]
        if pts:
            return _downsample_points(pts)
    members = el.get("members")
    if members:
        pts = []
        for m in members:
            g = m.get("geometry")
            if not g:
                continue
            pts.extend(
                (float(p["lat"]), float(p["lon"])) for p in g
                if p.get("lat") is not None and p.get("lon") is not None
            )
        if pts:
            return _downsample_points(pts)
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lng = el.get("lon") or (el.get("center") or {}).get("lon")
    if lat is not None and lng is not None:
        return [(float(lat), float(lng))]
    return []


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def landcover_max_tiles(corridor_km: float | None = None) -> int:
    """Tile budget: base cap, scaling up toward LANDCOVER_MAX_TILES_LONG with distance."""
    base = int(config.LANDCOVER_MAX_TILES)
    long_cap = int(config.LANDCOVER_MAX_TILES_LONG)
    if corridor_km is None or corridor_km <= 0 or long_cap <= base:
        return base
    # Linear scale: ~0 km → base, ≥500 km → long_cap.
    t = min(1.0, float(corridor_km) / 500.0)
    return int(round(base + t * (long_cap - base)))


def fetch_landcover(bbox: tuple[float, float, float, float],
                    progress=None,
                    prefer_axis: tuple[tuple[float, float], tuple[float, float]] | None = None,
                    corridor_half_width_deg: float | None = None,
                    deadline: float | None = None,
                    ) -> dict | None:
    """Fetch scenic land-cover features covering a bbox via a cached global grid.

    The world is divided into fixed ``config.LANDCOVER_TILE_DEG`` cells. Each cell
    the bbox touches is fetched from Overpass at most once ever, then persisted to
    disk and served instantly afterwards — so overlapping routes share cells and a
    corridor is only ever slow on its first, cold read. ``progress``, if given, is
    called with (done, total) as cells are gathered.

    ``prefer_axis``, if given as ((lat,lng), (lat,lng)), biases the tile budget
    toward cells near the A→B corridor midline and optionally restricts the cell
    set to a strip of ``corridor_half_width_deg`` around that axis (so long routes
    are a band, not a fat rectangle).

    ``deadline``, if given, is an absolute ``time.perf_counter()`` value: after
    serving warm cells, cold Overpass work stops when the deadline is reached
    (partial coverage is returned with ``truncated`` / ``landcover_incomplete``).

    Returns {'pos': Nx3 array [lat,lng,weight], 'neg': Mx3 array,
             'pos_labels': [...], 'neg_labels': [...], 'truncated': bool,
             'landcover_incomplete': bool}
    or None if no cell resolved.
    """
    tile = config.LANDCOVER_TILE_DEG
    s, w, n, e = bbox
    gi_lo, gi_hi = math.floor(s / tile), math.floor(n / tile)
    gj_lo, gj_hi = math.floor(w / tile), math.floor(e / tile)
    cells = [(gi, gj) for gi in range(gi_lo, gi_hi + 1)
             for gj in range(gj_lo, gj_hi + 1)]

    corridor_km = None
    half_w = (
        corridor_half_width_deg
        if corridor_half_width_deg is not None
        else config.LANDCOVER_CORRIDOR_HALF_WIDTH_DEG
    )
    if prefer_axis is not None:
        a, b = prefer_axis
        corridor_km = _haversine_km(a[0], a[1], b[0], b[1])
        # Corridor strip: drop cells far from the A→B midline (long fat bboxes).
        if half_w > 0 and len(cells) > 1:
            strip = []
            for gi, gj in cells:
                clat = (gi + 0.5) * tile
                clng = (gj + 0.5) * tile
                if math.sqrt(_point_to_segment_deg2(clat, clng, a, b)) <= half_w:
                    strip.append((gi, gj))
            if strip:
                cells = strip

    max_tiles = landcover_max_tiles(corridor_km)
    # Cap the number of live queries; if a corridor spans too many uncached cells
    # we still return whatever cells we can (partial cover beats neutral-50).
    uncapped = len(cells)
    truncated = uncapped > max_tiles
    if truncated:
        log.warning(
            "landcover_truncated requested_tiles=%s max_tiles=%s bbox=%s",
            uncapped, max_tiles, bbox,
        )
        if prefer_axis is not None:
            a, b = prefer_axis

            def _cell_key(cell):
                gi, gj = cell
                clat = (gi + 0.5) * tile
                clng = (gj + 0.5) * tile
                return _point_to_segment_deg2(clat, clng, a, b)
        else:
            cx = (gi_lo + gi_hi) / 2.0
            cy = (gj_lo + gj_hi) / 2.0

            def _cell_key(cell):
                return (cell[0] - cx) ** 2 + (cell[1] - cy) ** 2

        cells.sort(key=_cell_key)
        cells = cells[:max_tiles]

    total = len(cells)
    done = 0
    pos, neg, pos_labels, neg_labels = [], [], [], []
    any_ok = False
    deadline_stopped = False
    tiles_ok = 0
    tiles_cold = 0

    # Serve cached cells immediately; only the cold (missing) cells hit the
    # network. Pad-widen / explore merges therefore never re-query Overpass for
    # disk-warm cells — only newly covered grid indices are fetched.
    cold = []
    for gi, gj in cells:
        cached = _cell_from_cache((gi, gj))
        if cached is not None:
            any_ok = True
            tiles_ok += 1
            _accumulate(cached, pos, neg, pos_labels, neg_labels)
            done += 1
        else:
            cold.append((gi, gj))
    # Prefer near-axis cold tiles first so a deadline still yields corridor cover
    # instead of burning the budget on far-corner square cells.
    if prefer_axis is not None and len(cold) > 1:
        a, b = prefer_axis

        def _cold_key(cell):
            gi, gj = cell
            clat = (gi + 0.5) * tile
            clng = (gj + 0.5) * tile
            return _point_to_segment_deg2(clat, clng, a, b)

        cold.sort(key=_cold_key)
    tiles_cold = len(cold)
    if progress:
        progress(done, total)

    endpoints = config.OVERPASS_ENDPOINTS
    if cold:
        def _work(cell):
            gi, gj = cell
            sub = (gi * tile, gj * tile, (gi + 1) * tile, (gj + 1) * tile)
            # Prefer primary Overpass mirror; hash only diversifies when no deadline.
            idx = 0 if deadline is not None else (abs(gi + gj) % len(endpoints))
            part = _fetch_landcover_bbox(
                sub, endpoint=endpoints[idx], deadline=deadline,
            )
            # Only persist complete cells so a deadline-truncated tag batch
            # does not permanently freeze a half-empty tile on disk.
            if part is not None and part.get("complete", True):
                _cell_to_cache(cell, part)
            return part

        # Cap concurrency under a tight deadline so we finish near-axis tiles
        # instead of starting every cold cell and cancelling them all at once.
        workers = max(1, min(config.LANDCOVER_TILE_WORKERS, len(cold)))
        if deadline is not None:
            left = deadline - time.perf_counter()
            if left < float(config.LANDCOVER_CELL_TIMEOUT_SEC) * 1.5:
                workers = min(workers, 2)
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            pending: set = set()
            cold_iter = iter(cold)

            def _submit_next() -> bool:
                nonlocal truncated, deadline_stopped
                if deadline is not None and time.perf_counter() >= deadline:
                    truncated = True
                    deadline_stopped = True
                    return False
                try:
                    cell = next(cold_iter)
                except StopIteration:
                    return False
                pending.add(ex.submit(_work, cell))
                return True

            for _ in range(workers):
                if not _submit_next():
                    break

            while pending:
                timeout = None
                if deadline is not None:
                    left = deadline - time.perf_counter()
                    if left <= 0:
                        truncated = True
                        deadline_stopped = True
                        for fut in pending:
                            fut.cancel()
                        finished = {
                            f for f in pending if f.done() and not f.cancelled()
                        }
                        pending = finished
                        if not pending:
                            break
                        timeout = 0
                    else:
                        timeout = left

                finished, pending = wait(
                    pending, timeout=timeout, return_when=FIRST_COMPLETED,
                )
                if not finished:
                    truncated = True
                    deadline_stopped = True
                    for fut in pending:
                        fut.cancel()
                    break

                for fut in finished:
                    try:
                        part = fut.result()
                    except Exception:
                        part = None
                    done += 1
                    if part is not None:
                        any_ok = True
                        tiles_ok += 1
                        _accumulate(part, pos, neg, pos_labels, neg_labels)
                    if progress:
                        progress(done, total)
                    _submit_next()

            # Any cold cells never submitted count as truncated coverage.
            try:
                next(cold_iter)
                truncated = True
                deadline_stopped = True
            except StopIteration:
                pass
        finally:
            # Do not block past the plan deadline on cancelled/in-flight cells.
            ex.shutdown(wait=False, cancel_futures=True)

    if not any_ok:
        return None
    pos_arr = np.vstack(pos) if pos else np.empty((0, 3))
    neg_arr = np.vstack(neg) if neg else np.empty((0, 3))
    out = {
        "pos": pos_arr,
        "neg": neg_arr,
        "pos_labels": pos_labels,
        "neg_labels": neg_labels,
        "truncated": truncated,
        "landcover_incomplete": truncated,
        "tiles_ok": tiles_ok,
        "tiles_requested": total,
        "tiles_cold": tiles_cold,
        "feature_count": int(pos_arr.shape[0] + neg_arr.shape[0]),
    }
    if deadline_stopped:
        out["deadline_stopped"] = True
    return out


def _point_to_segment_deg2(lat: float, lng: float,
                           a: tuple[float, float], b: tuple[float, float]) -> float:
    """Squared distance in degrees from a point to segment a→b (for tile ranking)."""
    ax, ay = a[1], a[0]
    bx, by = b[1], b[0]
    px, py = lng, lat
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-18:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2


def landcover_is_usable(features: dict | None,
                        sample_coords: list[tuple[float, float]] | None = None,
                        ) -> bool:
    """False when landcover would silently dilute the blend toward neutral 50.

    Near-empty influence on the sample set means the signal should be omitted
    (renormalise) rather than forced in at weight 0.40.

    Truncated / incomplete tile coverage is soft: if ≥15% of samples still feel
    influence from the fetched tiles, keep map context (``truncated`` /
    ``landcover_incomplete`` remain true for callers that want to surface it).
    """
    if features is None:
        return False
    pos = features.get("pos")
    neg = features.get("neg")
    if pos is None or neg is None:
        return False
    if pos.shape[0] == 0 and neg.shape[0] == 0:
        return False
    if not sample_coords:
        # No samples to check: truncated-only coverage is still usable as a
        # partial signal; empty was already rejected above.
        return True
    # Require at least ~15% of samples to feel any nearby feature influence.
    radius = config.LANDCOVER_RADIUS_KM
    influenced = 0
    for lat, lng in sample_coords:
        dp, wp = _min_weighted_dist_km(lat, lng, pos)
        dn, wn = _min_weighted_dist_km(lat, lng, neg)
        if (dp < radius and wp > 0) or (dn < radius and wn > 0):
            influenced += 1
    return influenced / max(len(sample_coords), 1) >= 0.15


def _accumulate(part, pos, neg, pos_labels, neg_labels):
    if part["pos"].shape[0]:
        pos.append(part["pos"])
    if part["neg"].shape[0]:
        neg.append(part["neg"])
    pos_labels.extend(part["pos_labels"])
    neg_labels.extend(part["neg_labels"])


def _cell_path(cell: tuple[int, int]):
    return config.LANDCOVER_CACHE_DIR / f"lc_{cell[0]}_{cell[1]}.npz"


def merge_landcover(a: dict | None, b: dict | None) -> dict | None:
    """Merge two land-cover feature sets (either may be None)."""
    if a is None:
        return b
    if b is None:
        return a
    truncated = bool(a.get("truncated") or b.get("truncated"))
    return {
        "pos": np.vstack([x for x in (a["pos"], b["pos"]) if x.shape[0]])
               if (a["pos"].shape[0] or b["pos"].shape[0]) else np.empty((0, 3)),
        "neg": np.vstack([x for x in (a["neg"], b["neg"]) if x.shape[0]])
               if (a["neg"].shape[0] or b["neg"].shape[0]) else np.empty((0, 3)),
        "pos_labels": list(a["pos_labels"]) + list(b["pos_labels"]),
        "neg_labels": list(a["neg_labels"]) + list(b["neg_labels"]),
        "truncated": truncated,
        "landcover_incomplete": truncated or bool(
            a.get("landcover_incomplete") or b.get("landcover_incomplete")
        ),
    }


def _cell_from_cache(cell: tuple[int, int]) -> dict | None:
    hit = _cache_get(_LANDCOVER_CACHE, cell)
    if hit is not None:
        return hit
    path = _cell_path(cell)
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=True) as z:
            part = {
                "pos": z["pos"], "neg": z["neg"],
                "pos_labels": list(z["pos_labels"]),
                "neg_labels": list(z["neg_labels"]),
            }
        _cache_set(_LANDCOVER_CACHE, cell, part)
        return part
    except Exception:
        return None


def _cell_to_cache(cell: tuple[int, int], part: dict) -> None:
    _cache_set(_LANDCOVER_CACHE, cell, part)
    try:
        final = _cell_path(cell)
        # numpy appends ".npz" unless the name already ends in it, so the temp
        # file must itself end in ".npz" to avoid a doubled extension.
        tmp = final.parent / (final.stem + ".tmp.npz")
        np.savez(tmp, pos=part["pos"], neg=part["neg"],
                 pos_labels=np.array(part["pos_labels"], dtype=object),
                 neg_labels=np.array(part["neg_labels"], dtype=object))
        tmp.replace(final)
    except Exception:
        pass


def _parse_landcover_elements(elements: list) -> dict:
    """Turn Overpass elements into pos/neg feature arrays."""
    pos_map = {(k, v): wt for k, v, wt in POSITIVE_TAGS}
    neg_map = {(k, v): wt for k, v, wt in NEGATIVE_TAGS}
    pos, neg = [], []
    pos_labels, neg_labels = [], []
    for el in elements:
        tags = el.get("tags", {})
        matched = None
        for k, v in list(tags.items()):
            if (k, v) in pos_map:
                matched = ("pos", pos_map[(k, v)], FRIENDLY.get((k, v), v))
                break
            if (k, v) in neg_map:
                matched = ("neg", neg_map[(k, v)], FRIENDLY.get((k, v), v))
                break
        if matched is None:
            continue
        kind, weight, label = matched
        for lat, lng in _points_from_element(el):
            if kind == "pos":
                pos.append((lat, lng, weight))
                pos_labels.append(label)
            else:
                neg.append((lat, lng, weight))
                neg_labels.append(label)
    return {
        "pos": np.array(pos, dtype=float) if pos else np.empty((0, 3)),
        "neg": np.array(neg, dtype=float) if neg else np.empty((0, 3)),
        "pos_labels": pos_labels,
        "neg_labels": neg_labels,
    }


def _merge_landcover_parts(parts: list[dict]) -> dict | None:
    """Combine batch results; None if every batch failed."""
    if not parts:
        return None
    pos, neg, pos_labels, neg_labels = [], [], [], []
    for part in parts:
        _accumulate(part, pos, neg, pos_labels, neg_labels)
    return {
        "pos": np.vstack(pos) if pos else np.empty((0, 3)),
        "neg": np.vstack(neg) if neg else np.empty((0, 3)),
        "pos_labels": pos_labels,
        "neg_labels": neg_labels,
    }


def _post_overpass_elements(query: str, endpoint: str, http_timeout: float) -> list | None:
    """POST one Overpass query; return elements or None on hard failure.

    Empty elements with a runtime timeout remark are treated as failure so we
    do not cache a false "warm empty" tile. HTTP 429 returns None after a short
    polite pause (caller retries).
    """
    try:
        r = _session.post(endpoint, data={"data": query}, timeout=http_timeout)
        if r.status_code == 429:
            time.sleep(min(1.5, max(0.4, http_timeout * 0.1)))
            return None
        r.raise_for_status()
        payload = r.json()
        remark = (payload.get("remark") or "").lower()
        elements = payload.get("elements", [])
        if elements:
            return elements
        if "timed out" in remark or "runtime error" in remark:
            return None
        # Genuine empty tile (ocean / desert) — still a successful read.
        return []
    except Exception as exc:
        log.debug(
            "overpass_post_failed endpoint=%s err=%s",
            endpoint, type(exc).__name__,
        )
        return None


def _fetch_landcover_bbox(bbox: tuple[float, float, float, float],
                          endpoint: str | None = None,
                          deadline: float | None = None) -> dict | None:
    """Fetch land-cover features for a single (small) bbox from Overpass.

    Tags are queried in small batches with ``out center`` (default) so dense UK
    corridors return partial map context instead of a single 504 on a giant
    ``out geom`` union. endpoint, if given, is tried first; other mirrors are
    fallbacks. HTTP timeout shrinks to remaining ``deadline``. Under a tight
    deadline, returns after the first useful batch rather than chasing every tag.
    """
    endpoints = list(config.OVERPASS_ENDPOINTS)
    if endpoint is not None:
        endpoints = [endpoint] + [e for e in endpoints if e != endpoint]
    # Always prefer the primary mirror first — cell-hash rotation onto a dead
    # secondary (timeout/403) was burning the whole landcover window.
    primary = config.OVERPASS_ENDPOINTS[0] if config.OVERPASS_ENDPOINTS else None
    if primary and primary in endpoints:
        endpoints = [primary] + [e for e in endpoints if e != primary]
    base_timeout = max(1.0, float(config.LANDCOVER_CELL_TIMEOUT_SEC))
    retries = max(1, int(config.LANDCOVER_FETCH_RETRIES))
    # Under a plan deadline, do not burn the window on secondary-mirror retries
    # after the primary already had a fair shot at the first tag batch.
    if deadline is not None:
        retries = min(retries, 1)
    backoff = max(0.0, float(config.LANDCOVER_RETRY_BACKOFF_SEC))
    batch_gap = max(0.0, float(config.LANDCOVER_BATCH_GAP_SEC))
    batches = _tag_batches()
    collected: list[dict] = []
    batches_ok = 0

    def _pack(complete: bool) -> dict | None:
        out = _merge_landcover_parts(collected)
        if out is None:
            return None
        out["batches_ok"] = batches_ok
        out["batches_total"] = len(batches)
        out["complete"] = complete and batches_ok >= len(batches)
        return out

    for bi, tag_batch in enumerate(batches):
        if deadline is not None and time.perf_counter() >= deadline:
            break
        query = _overpass_query(bbox, tags=tag_batch)
        batch_ok = False
        for attempt in range(retries):
            if deadline is not None and time.perf_counter() >= deadline:
                break
            # Under a tight deadline, only try the primary + one backup.
            if deadline is not None and (deadline - time.perf_counter()) < base_timeout * 1.25:
                order = endpoints[:2]
            else:
                order = (
                    endpoints[attempt % len(endpoints):]
                    + endpoints[: attempt % len(endpoints)]
                )
            for ep in order:
                if deadline is not None:
                    left = deadline - time.perf_counter()
                    if left <= 0.4:
                        break
                    http_timeout = max(1.0, min(base_timeout, left))
                else:
                    http_timeout = base_timeout
                elements = _post_overpass_elements(query, ep, http_timeout)
                if elements is None:
                    continue
                collected.append(_parse_landcover_elements(elements))
                batches_ok += 1
                batch_ok = True
                break
            if batch_ok:
                break
            if attempt + 1 < retries and backoff > 0:
                sleep_for = backoff * (attempt + 1)
                if deadline is not None:
                    sleep_for = min(
                        sleep_for, max(0.0, deadline - time.perf_counter() - 0.3),
                    )
                if sleep_for > 0:
                    time.sleep(sleep_for)

        # Early exit: one good batch with features beats chasing all tags into a 429.
        if batch_ok and deadline is not None and collected:
            n_feat = sum(
                int(p["pos"].shape[0]) + int(p["neg"].shape[0]) for p in collected
            )
            left = deadline - time.perf_counter()
            if n_feat >= 60 and left < base_timeout + batch_gap:
                return _pack(complete=False)

        if bi + 1 < len(batches) and batch_gap > 0:
            if deadline is not None:
                gap = min(batch_gap, max(0.0, deadline - time.perf_counter() - 0.3))
            else:
                gap = batch_gap
            if gap > 0:
                time.sleep(gap)

    if batches_ok == 0:
        return None
    return _pack(complete=True)


def _min_weighted_dist_km(lat, lng, feats: np.ndarray) -> tuple[float, float]:
    """Nearest feature distance (km) and its weight. inf if none."""
    d, w, _ = _min_weighted_dist_km_idx(lat, lng, feats)
    return d, w


def _min_weighted_dist_km_idx(lat, lng, feats: np.ndarray) -> tuple[float, float, int | None]:
    """Nearest feature distance (km), weight and row index. (inf, 0, None) if none."""
    if feats.shape[0] == 0:
        return math.inf, 0.0, None
    dlat = (feats[:, 0] - lat) * 111.0
    dlng = (feats[:, 1] - lng) * 111.0 * math.cos(math.radians(lat))
    d = np.sqrt(dlat * dlat + dlng * dlng)
    j = int(np.argmin(d))
    return float(d[j]), float(feats[j, 2]), j


def _urban_damped_land_score(
    pos_eff: float,
    neg_eff: float,
) -> float:
    """Blend pos/neg into 0..100, damping park-like positives inside urban fabric.

    Town centres often contain green parks that would otherwise score like
    countryside. When negative (residential/commercial/industrial) influence is
    strong, shrink the positive contribution so urban green cannot dominate.
    """
    # Soften positives under urban pressure; keep nature reserves viable when
    # negatives are weak (countryside villages still get park credit).
    urban_dampen = min(1.0, max(0.0, neg_eff) / 0.50)
    pos_adj = pos_eff * (1.0 - 0.90 * urban_dampen)
    # Slightly stronger urban penalty than the historic 45× so grey towns fall
    # below the field reject floor more reliably.
    return max(0.0, min(100.0, 50.0 + 45.0 * pos_adj - 52.0 * neg_eff))


def landcover_details(coords: list[tuple[float, float]], features: dict,
                      radius_km: float | None = None,
                      climate_ids: list[str | None] | None = None) -> list[dict]:
    """Per-point landcover score plus the dominant nearby feature (for explanations).

    Returns, per coord: {score, pos_label, pos_dist, neg_label, neg_dist} where
    labels are only reported for features actually within influence range.
    """
    radius = radius_km if radius_km is not None else config.LANDCOVER_RADIUS_KM
    pos, neg = features["pos"], features["neg"]
    pos_labels = features.get("pos_labels", [])
    neg_labels = features.get("neg_labels", [])
    out = []
    ids = climate_ids or [None] * len(coords)
    for (lat, lng), climate_id in zip(coords, ids):
        pos_eff, pos_label, pos_dist = _feature_score(
            lat, lng, pos, pos_labels, radius, climate_id,
        )
        neg_eff, neg_label, neg_dist = _feature_score(
            lat, lng, neg, neg_labels, radius, climate_id,
        )
        score = _urban_damped_land_score(pos_eff, neg_eff)
        out.append({
            "score": score,
            "pos_label": pos_label,
            "pos_dist": pos_dist,
            "neg_label": neg_label,
            "neg_dist": neg_dist,
            "climate": climate_id,
            "urban_neg_eff": round(neg_eff, 3),
        })
    return out


def landcover_scores(coords: list[tuple[float, float]], features: dict,
                     radius_km: float | None = None,
                     climate_ids: list[str | None] | None = None) -> list[float]:
    """Per-point landcover score 0..100 from proximity to positive/negative land."""
    radius = radius_km if radius_km is not None else config.LANDCOVER_RADIUS_KM
    pos, neg = features["pos"], features["neg"]
    pos_labels = features.get("pos_labels", [])
    neg_labels = features.get("neg_labels", [])
    ids = climate_ids or [None] * len(coords)
    out = []
    for (lat, lng), climate_id in zip(coords, ids):
        pos_eff, _pl, _pd = _feature_score(
            lat, lng, pos, pos_labels, radius, climate_id,
        )
        neg_eff, _nl, _nd = _feature_score(
            lat, lng, neg, neg_labels, radius, climate_id,
        )
        out.append(_urban_damped_land_score(pos_eff, neg_eff))
    return out
