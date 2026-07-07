"""Scenic-signal enrichment: terrain ruggedness + OSM land cover.

Colour density alone is a weak scenic proxy (a motorway across a green field
still looks green). This module adds the two signals that most improve accuracy:

  * terrain   - elevation relief from Open-Meteo (free, no key). Mountains and
                hilly drives are scenic; flat is not.
  * landcover - OSM land use from Overpass (free, no key). Proximity to forest,
                water, coastline, peaks, parks and nature reserves boosts the
                score; industrial / residential / retail land lowers it.

Every network call degrades gracefully: if a source is unavailable the caller
renormalises the blend over whatever signals remain (colour always works).
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from . import config

_session = requests.Session()
_session.headers.update({"User-Agent": "ScenicRoutePlanner/1.0", "Accept": "application/json"})

# In-process caches: land cover keyed by rounded bbox, elevation by rounded coord.
# These cut network load sharply (a single search re-reads the corridor several
# times) and, for elevation, keep terrain alive once a provider's daily quota
# starts returning HTTP 429.
_LANDCOVER_CACHE: dict[tuple, dict] = {}
_ELEV_CACHE: dict[tuple[float, float], float] = {}

OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Positive scenic land cover (OSM key/value, weight 0..1).
POSITIVE_TAGS = [
    ('natural', 'water', 1.0),
    ('natural', 'coastline', 1.0),
    ('natural', 'wood', 0.8),
    ('natural', 'peak', 1.0),
    ('landuse', 'forest', 0.8),
    ('leisure', 'nature_reserve', 0.9),
    ('leisure', 'park', 0.6),
    ('boundary', 'national_park', 1.0),
    ('boundary', 'protected_area', 0.7),
]
NEGATIVE_TAGS = [
    ('landuse', 'industrial', 1.0),
    ('landuse', 'residential', 0.7),
    ('landuse', 'retail', 0.8),
    ('landuse', 'commercial', 0.7),
]

# Human-friendly noun phrases for each land-cover tag, used in explanations.
FRIENDLY = {
    ('natural', 'water'): 'water',
    ('natural', 'coastline'): 'the coast',
    ('natural', 'wood'): 'woodland',
    ('natural', 'peak'): 'a summit',
    ('landuse', 'forest'): 'forest',
    ('leisure', 'nature_reserve'): 'a nature reserve',
    ('leisure', 'park'): 'a park',
    ('boundary', 'national_park'): 'a national park',
    ('boundary', 'protected_area'): 'protected land',
    ('landuse', 'industrial'): 'industrial land',
    ('landuse', 'residential'): 'housing',
    ('landuse', 'retail'): 'a retail area',
    ('landuse', 'commercial'): 'a commercial area',
}


# ---------------------------------------------------------------------------
# Terrain (elevation relief)
# ---------------------------------------------------------------------------
def elevation_batch(coords: list[tuple[float, float]]) -> list[float] | None:
    """Return elevation (m) for each (lat,lng), or None if terrain is unavailable.

    Uses an in-process cache and two free providers (Open-Meteo, then Opentopodata)
    so a single provider's daily quota (HTTP 429) doesn't zero out terrain.
    """
    if not coords:
        return []
    keys = [(round(c[0], 4), round(c[1], 4)) for c in coords]
    missing = [k for k in dict.fromkeys(keys) if k not in _ELEV_CACHE]

    if missing:
        for provider in (_elev_open_meteo, _elev_opentopo):
            still = [k for k in missing if k not in _ELEV_CACHE]
            if not still:
                break
            for i in range(0, len(still), 100):
                chunk = still[i:i + 100]
                vals = provider(chunk)
                if vals is None:
                    break  # provider failed/quota'd — try the next provider
                for k, v in zip(chunk, vals):
                    if v is not None:
                        _ELEV_CACHE[k] = float(v)

    out = [_ELEV_CACHE.get(k) for k in keys]
    if any(v is None for v in out):
        return None
    return out


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


def relief_scores(elevs: list[float], window: int | None = None) -> list[float]:
    """Local relief along an ordered path -> 0..100 (higher = more rugged)."""
    window = window if window is not None else config.RELIEF_WINDOW
    n = len(elevs)
    e = np.asarray(elevs, dtype=float)
    scores = np.zeros(n)
    for i in range(n):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        rng = float(e[lo:hi].max() - e[lo:hi].min())
        scores[i] = min(100.0, rng / config.RELIEF_FULL_M * 100.0)
    return scores.tolist()


# ---------------------------------------------------------------------------
# Land cover (Overpass)
# ---------------------------------------------------------------------------
def _overpass_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox  # min_lat, min_lng, max_lat, max_lng
    b = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
    # One `nwr` statement per tag (node+way+relation in a single clause) keeps the
    # query light enough that larger tiles return within the server timeout, while
    # relations improve coverage of big forests / parks. `out center` gives way and
    # relation centroids.
    parts = [f'nwr["{k}"="{v}"]{b};' for k, v, _ in POSITIVE_TAGS + NEGATIVE_TAGS]
    return f"[out:json][timeout:60];({''.join(parts)});out center 1200;"


def fetch_landcover(bbox: tuple[float, float, float, float],
                    progress=None) -> dict | None:
    """Fetch scenic land-cover features covering a bbox via a cached global grid.

    The world is divided into fixed ``config.LANDCOVER_TILE_DEG`` cells. Each cell
    the bbox touches is fetched from Overpass at most once ever, then persisted to
    disk and served instantly afterwards — so overlapping routes share cells and a
    corridor is only ever slow on its first, cold read. ``progress``, if given, is
    called with (done, total) as cells are gathered.

    Returns {'pos': Nx3 array [lat,lng,weight], 'neg': Mx3 array,
             'pos_labels': [...], 'neg_labels': [...]} or None if no cell resolved.
    """
    tile = config.LANDCOVER_TILE_DEG
    s, w, n, e = bbox
    gi_lo, gi_hi = math.floor(s / tile), math.floor(n / tile)
    gj_lo, gj_hi = math.floor(w / tile), math.floor(e / tile)
    cells = [(gi, gj) for gi in range(gi_lo, gi_hi + 1)
             for gj in range(gj_lo, gj_hi + 1)]
    # Cap the number of live queries; if a corridor spans too many uncached cells
    # we still return whatever cells we can (partial cover beats neutral-50).
    if len(cells) > config.LANDCOVER_MAX_TILES:
        cx = (gi_lo + gi_hi) / 2.0
        cy = (gj_lo + gj_hi) / 2.0
        cells.sort(key=lambda c: (c[0] - cx) ** 2 + (c[1] - cy) ** 2)
        cells = cells[:config.LANDCOVER_MAX_TILES]

    total = len(cells)
    done = 0
    pos, neg, pos_labels, neg_labels = [], [], [], []
    any_ok = False

    # Serve cached cells immediately; only the cold cells hit the network.
    cold = []
    for gi, gj in cells:
        cached = _cell_from_cache((gi, gj))
        if cached is not None:
            any_ok = True
            _accumulate(cached, pos, neg, pos_labels, neg_labels)
            done += 1
        else:
            cold.append((gi, gj))
    if progress:
        progress(done, total)

    if cold:
        def _work(cell):
            gi, gj = cell
            sub = (gi * tile, gj * tile, (gi + 1) * tile, (gj + 1) * tile)
            idx = abs(gi + gj) % len(OVERPASS_ENDPOINTS)
            part = _fetch_landcover_bbox(sub, endpoint=OVERPASS_ENDPOINTS[idx])
            if part is not None:
                _cell_to_cache(cell, part)
            return part
        with ThreadPoolExecutor(max_workers=config.LANDCOVER_TILE_WORKERS) as ex:
            for part in ex.map(_work, cold):
                done += 1
                if part is not None:
                    any_ok = True
                    _accumulate(part, pos, neg, pos_labels, neg_labels)
                if progress:
                    progress(done, total)

    if not any_ok:
        return None
    return {
        "pos": np.vstack(pos) if pos else np.empty((0, 3)),
        "neg": np.vstack(neg) if neg else np.empty((0, 3)),
        "pos_labels": pos_labels,
        "neg_labels": neg_labels,
    }


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
    return {
        "pos": np.vstack([x for x in (a["pos"], b["pos"]) if x.shape[0]])
               if (a["pos"].shape[0] or b["pos"].shape[0]) else np.empty((0, 3)),
        "neg": np.vstack([x for x in (a["neg"], b["neg"]) if x.shape[0]])
               if (a["neg"].shape[0] or b["neg"].shape[0]) else np.empty((0, 3)),
        "pos_labels": list(a["pos_labels"]) + list(b["pos_labels"]),
        "neg_labels": list(a["neg_labels"]) + list(b["neg_labels"]),
    }


def _cell_from_cache(cell: tuple[int, int]) -> dict | None:
    if cell in _LANDCOVER_CACHE:
        return _LANDCOVER_CACHE[cell]
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
        _LANDCOVER_CACHE[cell] = part
        return part
    except Exception:
        return None


def _cell_to_cache(cell: tuple[int, int], part: dict) -> None:
    _LANDCOVER_CACHE[cell] = part
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


def _fetch_landcover_bbox(bbox: tuple[float, float, float, float],
                          endpoint: str | None = None) -> dict | None:
    """Fetch land-cover features for a single (small) bbox from Overpass.

    endpoint, if given, is tried first (spreads tiled load across mirrors); the
    other endpoints are still used as fallbacks.
    """
    query = _overpass_query(bbox)
    pos_map = {(k, v): wt for k, v, wt in POSITIVE_TAGS}
    neg_map = {(k, v): wt for k, v, wt in NEGATIVE_TAGS}
    endpoints = OVERPASS_ENDPOINTS
    if endpoint is not None:
        endpoints = [endpoint] + [e for e in OVERPASS_ENDPOINTS if e != endpoint]
    for ep in endpoints:
        try:
            r = _session.post(ep, data={"data": query}, timeout=45)
            r.raise_for_status()
            elements = r.json().get("elements", [])
            pos, neg = [], []
            pos_labels, neg_labels = [], []
            for el in elements:
                lat = el.get("lat") or (el.get("center") or {}).get("lat")
                lng = el.get("lon") or (el.get("center") or {}).get("lon")
                if lat is None or lng is None:
                    continue
                tags = el.get("tags", {})
                for k, v in list(tags.items()):
                    if (k, v) in pos_map:
                        pos.append((lat, lng, pos_map[(k, v)]))
                        pos_labels.append(FRIENDLY.get((k, v), v))
                        break
                    if (k, v) in neg_map:
                        neg.append((lat, lng, neg_map[(k, v)]))
                        neg_labels.append(FRIENDLY.get((k, v), v))
                        break
            return {
                "pos": np.array(pos, dtype=float) if pos else np.empty((0, 3)),
                "neg": np.array(neg, dtype=float) if neg else np.empty((0, 3)),
                "pos_labels": pos_labels,
                "neg_labels": neg_labels,
            }
        except Exception:
            continue
    return None


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


def landcover_details(coords: list[tuple[float, float]], features: dict,
                      radius_km: float | None = None) -> list[dict]:
    """Per-point landcover score plus the dominant nearby feature (for explanations).

    Returns, per coord: {score, pos_label, pos_dist, neg_label, neg_dist} where
    labels are only reported for features actually within influence range.
    """
    radius = radius_km if radius_km is not None else config.LANDCOVER_RADIUS_KM
    pos, neg = features["pos"], features["neg"]
    pos_labels = features.get("pos_labels", [])
    neg_labels = features.get("neg_labels", [])
    out = []
    for lat, lng in coords:
        dp, wp, jp = _min_weighted_dist_km_idx(lat, lng, pos)
        dn, wn, jn = _min_weighted_dist_km_idx(lat, lng, neg)
        pos_eff = max(0.0, 1.0 - dp / radius) * wp
        neg_eff = max(0.0, 1.0 - dn / radius) * wn
        score = max(0.0, min(100.0, 50.0 + 45.0 * pos_eff - 45.0 * neg_eff))
        out.append({
            "score": score,
            "pos_label": pos_labels[jp] if (jp is not None and pos_eff > 0 and jp < len(pos_labels)) else None,
            "pos_dist": round(dp, 2) if pos_eff > 0 else None,
            "neg_label": neg_labels[jn] if (jn is not None and neg_eff > 0 and jn < len(neg_labels)) else None,
            "neg_dist": round(dn, 2) if neg_eff > 0 else None,
        })
    return out


def landcover_scores(coords: list[tuple[float, float]], features: dict,
                     radius_km: float | None = None) -> list[float]:
    """Per-point landcover score 0..100 from proximity to positive/negative land."""
    radius = radius_km if radius_km is not None else config.LANDCOVER_RADIUS_KM
    pos, neg = features["pos"], features["neg"]
    out = []
    for lat, lng in coords:
        dp, wp = _min_weighted_dist_km(lat, lng, pos)
        dn, wn = _min_weighted_dist_km(lat, lng, neg)
        pos_eff = max(0.0, 1.0 - dp / radius) * wp
        neg_eff = max(0.0, 1.0 - dn / radius) * wn
        out.append(max(0.0, min(100.0, 50.0 + 45.0 * pos_eff - 45.0 * neg_eff)))
    return out
