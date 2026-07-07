"""Scenic scoring engine.

Pipeline (as specified by the product idea):
  1. Take satellite imagery for an area (an XYZ tile).
  2. Reduce the pixel count (downscale) to average out noise.
  3. Analyse the colour composition:
       - greens/blues  -> natural, scenic
       - greys/blacks  -> dense built-up area, not scenic
  4. Produce a 0-100 scenic score plus a component breakdown.

The tile source is pluggable. Primary source is free Esri World Imagery
(no API key). If the network is unavailable, a deterministic synthetic tile
is generated so the pipeline still runs offline and is testable.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import threading
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import requests
from PIL import Image

from . import config


# ---------------------------------------------------------------------------
# Slippy-map tile maths
# ---------------------------------------------------------------------------
def deg2tile(lat: float, lng: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lng to XYZ tile indices."""
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    x = min(max(x, 0), int(n) - 1)
    y = min(max(y, 0), int(n) - 1)
    return x, y


# ---------------------------------------------------------------------------
# Tile fetching (with disk cache + offline synthetic fallback)
# ---------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"User-Agent": "ScenicRoutePlanner/1.0"})


def _cache_path(z: int, x: int, y: int):
    return config.TILE_CACHE_DIR / f"{z}_{x}_{y}.png"


def _synthetic_tile(lat: float, lng: float, size: int = 256) -> Image.Image:
    """Deterministic offline tile.

    Fakes a plausible landscape whose 'greenness' falls near a few synthetic
    urban centres, so the colour pipeline produces meaningful spatial
    variation even without network access.
    """
    urban_centres = [
        (51.5074, -0.1278),   # London
        (53.4808, -2.2426),   # Manchester
        (52.4862, -1.8904),   # Birmingham
        (55.9533, -3.1883),   # Edinburgh
    ]
    d = min(math.hypot(lat - c[0], lng - c[1]) for c in urban_centres)
    urban = max(0.0, 1.0 - d / 0.5)  # 1 near a city, 0 far away

    seed = int(hashlib.md5(f"{lat:.4f},{lng:.4f}".encode()).hexdigest(), 16) % (2**32)
    rng = np.random.default_rng(seed)
    arr = np.zeros((size, size, 3), dtype=np.uint8)

    # Base natural colour: green fields, some brown.
    green = np.array([70, 120, 55])
    grey = np.array([120, 120, 122])
    base = (1 - urban) * green + urban * grey
    noise = rng.normal(0, 12, (size, size, 3))
    field = np.clip(base + noise, 0, 255)

    # Sprinkle a little blue water far from cities.
    if urban < 0.4 and rng.random() < 0.5:
        n = size // 4
        y0, x0 = rng.integers(0, size - n), rng.integers(0, size - n)
        field[y0:y0 + n, x0:x0 + n] = [40, 90, 150]

    arr[:] = field.astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def _save_atomic(img: Image.Image, cache) -> None:
    """Write a tile to cache atomically so concurrent workers can't collide.

    Many threads may score the same tile at once; on Windows a plain save to a
    shared path raises WinError 32. Write to a unique temp file then replace,
    and never let a cache-write failure break scoring.
    """
    try:
        tmp = cache.with_suffix(f".{os.getpid()}_{threading.get_ident()}.tmp")
        img.save(tmp)
        os.replace(tmp, cache)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def fetch_tile(lat: float, lng: float, zoom: int, source: str = "esri") -> tuple[Image.Image, str]:
    """Return (RGB image, actual_source_used) for the tile covering lat/lng."""
    x, y = deg2tile(lat, lng, zoom)
    cache = _cache_path(zoom, x, y)
    if cache.exists():
        try:
            return Image.open(cache).convert("RGB"), "cache"
        except Exception:
            cache.unlink(missing_ok=True)

    if source == "synthetic":
        img = _synthetic_tile(lat, lng)
        _save_atomic(img, cache)
        return img, "synthetic"

    url = config.ESRI_TILE_URL.format(z=zoom, x=x, y=y)
    try:
        r = _session.get(url, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        _save_atomic(img, cache)
        return img, "esri"
    except Exception:
        # Graceful offline fallback.
        img = _synthetic_tile(lat, lng)
        return img, "synthetic-fallback"


# ---------------------------------------------------------------------------
# Colour-composition analysis
# ---------------------------------------------------------------------------
@dataclass
class ScenicScore:
    score: float          # 0-100
    green_frac: float     # vegetation
    blue_frac: float      # water
    grey_frac: float      # built-up / grey / black
    brightness: float     # 0-1 mean value
    source: str

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}


def analyse_image(img: Image.Image, source: str = "") -> ScenicScore:
    """Downscale, classify each pixel by colour, and average a scenic value.

    Instead of a single green-vs-grey ratio (which saturated to 100 for any
    green-ish tile), every pixel is assigned a scenic *value* in [0,1] by land
    type, then averaged. This gives a genuine 0-100 spread and distinguishes
    rich woodland/water from plain farmland from built-up areas.
    """
    small = img.resize((config.DOWNSCALE, config.DOWNSCALE), Image.BILINEAR)
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32)
    h = hsv[..., 0] / 255.0 * 360.0   # hue in degrees (0=red, 120=green, 240=blue)
    s = hsv[..., 1] / 255.0           # saturation 0-1
    v = hsv[..., 2] / 255.0           # value 0-1
    total = h.size

    # --- classify pixels -----------------------------------------------------
    # Water incl. dark, low-saturation lakes (broad blue hue, allow low V).
    water  = (h >= 172) & (h <= 285) & (s >= 0.08) & (v >= 0.06)
    green  = (h >= 60) & (h < 172) & (s >= 0.10) & (v >= 0.10)
    moor   = (h >= 20) & (h < 60) & (s >= 0.15) & (v >= 0.18) & (v <= 0.80)  # heath/bracken/soil
    bright_urban = (s < 0.10) & (v > 0.60)             # concrete / bright roofs
    grey   = (s < 0.10) & (v >= 0.18) & (v <= 0.60)    # roads / buildings / grey
    dark   = (v < 0.18)                                # deep shadow (ambiguous)

    # --- assign a scenic value to every pixel (higher priority assigned last) -
    value = np.full(h.shape, config.VAL_DEFAULT, dtype=np.float32)
    value[dark]         = config.VAL_DARK
    value[grey]         = config.VAL_GREY
    value[bright_urban] = config.VAL_URBAN
    value[moor]         = config.VAL_MOOR

    # Green quality is graded continuously: dark, saturated green = woodland
    # (high value); bright, pale green = farmland/pasture (mid value). This is
    # what separates a forest drive from a field, which a hard split could not.
    gf = (np.clip((s - 0.10) / 0.35, 0.0, 1.0) * 0.5
          + np.clip((0.62 - v) / 0.45, 0.0, 1.0) * 0.5)
    green_val = config.VAL_GRASS + (config.VAL_FOREST - config.VAL_GRASS) * gf
    value[green] = green_val[green]
    value[water] = config.VAL_WATER

    base = float(value.mean()) * 100.0

    # --- landscape variety bonus --------------------------------------------
    # A mix of natural colours (green + water + moor) is more interesting than a
    # uniform field. Circular spread of hue over natural pixels -> up to +8.
    natural = water | green | moor
    if int(natural.sum()) >= 4:
        ang = np.radians(h[natural])
        r = math.hypot(float(np.cos(ang).mean()), float(np.sin(ang).mean()))
        variety = max(0.0, 1.0 - r)
    else:
        variety = 0.0
    score = max(0.0, min(100.0, base + variety * 8.0))

    green_frac = float(green.sum()) / total
    blue_frac = float(water.sum()) / total
    grey_frac = float((grey | bright_urban).sum()) / total
    brightness = float(v.mean())

    return ScenicScore(
        score=score,
        green_frac=green_frac,
        blue_frac=blue_frac,
        grey_frac=grey_frac,
        brightness=brightness,
        source=source,
    )


def score_location(lat: float, lng: float, zoom: Optional[int] = None,
                   source: str = "esri") -> ScenicScore:
    """Full pipeline for a single coordinate."""
    zoom = zoom if zoom is not None else config.TILE_ZOOM
    img, used = fetch_tile(lat, lng, zoom, source=source)
    return analyse_image(img, source=used)
