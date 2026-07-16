"""Regional colour climates for satellite scenic scoring.

Colour HSV rules are regional: temperate oceanic greens are not a global scenic
model. Every (lat, lng) maps to one of 12 fully-parameterised climates via an
offline 1° world grid (with the same rules as a fallback) plus an elevation
overlay for alpine. UK temperate oceanic numbers stay locked.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

# Shipped with the package (~2 KB compressed). Runtime caches live under repo-root data/.
_GRID_PATH = Path(__file__).resolve().parent / "data" / "climate_grid.npz"


@dataclass(frozen=True)
class ColourClimate:
    id: str
    name: str
    # Per-pixel scenic values 0..1
    val_water: float
    val_forest: float
    val_moor: float
    val_grass: float
    val_default: float
    val_dark: float
    val_urban: float
    val_grey: float
    # Default colour blend weight when no profile override is supplied.
    blend_colour: float
    blend_terrain: float
    blend_landcover: float
    # HSV band edges (hue in degrees 0..360; s/v in 0..1)
    h_water_min: float
    h_water_max: float
    s_water_min: float
    v_water_min: float
    h_green_min: float
    h_green_max: float
    s_green_min: float
    v_green_min: float
    h_moor_min: float
    h_moor_max: float
    s_moor_min: float
    v_moor_min: float
    v_moor_max: float
    s_grey_max: float
    v_urban_min: float
    v_grey_min: float
    v_grey_max: float
    v_dark_max: float
    # Continuous green quality: forest vs grass grading
    gf_s_floor: float
    gf_s_span: float
    gf_v_peak: float
    gf_v_span: float
    # When True, bright low-saturation pixels (sand/snow) are not treated as urban.
    protect_bright_natural: bool = False
    # Scenic value used for protected bright natural pixels when enabled.
    val_bright_natural: float = 0.55


def _uk_hsv() -> dict:
    """Locked temperate oceanic / UK HSV bands — do not retune for other biomes."""
    return dict(
        h_water_min=172.0,
        h_water_max=285.0,
        s_water_min=0.08,
        v_water_min=0.06,
        h_green_min=60.0,
        h_green_max=172.0,
        s_green_min=0.10,
        v_green_min=0.10,
        h_moor_min=20.0,
        h_moor_max=60.0,
        s_moor_min=0.15,
        v_moor_min=0.18,
        v_moor_max=0.80,
        s_grey_max=0.10,
        v_urban_min=0.60,
        v_grey_min=0.18,
        v_grey_max=0.60,
        v_dark_max=0.18,
        gf_s_floor=0.10,
        gf_s_span=0.35,
        gf_v_peak=0.62,
        gf_v_span=0.45,
    )


# ---------------------------------------------------------------------------
# 12 climates (full params). temperate_oceanic holds the locked UK numbers.
# ---------------------------------------------------------------------------

# Temperate oceanic coasts: NW Europe, NZ west, Pacific NW.
# Scenic intent: lush greens, grey skies, moor/heath; UK lock unchanged.
TEMPERATE_OCEANIC = ColourClimate(
    id="temperate_oceanic",
    name="Temperate oceanic",
    val_water=1.00,
    val_forest=0.92,
    val_moor=0.68,
    val_grass=0.56,
    val_default=0.45,
    val_dark=0.35,
    val_urban=0.08,
    val_grey=0.10,
    blend_colour=0.35,
    blend_terrain=0.25,
    blend_landcover=0.40,
    protect_bright_natural=False,
    **_uk_hsv(),
)

# Backward-compatible alias id used by older tests/API callers.
TEMPERATE_NW_EUROPE = ColourClimate(
    **{
        **asdict(TEMPERATE_OCEANIC),
        "id": "temperate_nw_europe",
        "name": "Temperate NW Europe",
    }
)

# Inland Europe, NE US/Canada, NE China — stronger seasons, more farmland grey.
# Calibrated: city grey/urban low-40s; forest/countryside greens ~60–85.
TEMPERATE_CONTINENTAL = ColourClimate(
    id="temperate_continental",
    name="Temperate continental",
    val_water=1.00,
    val_forest=0.93,
    val_moor=0.64,
    val_grass=0.55,
    val_default=0.40,
    val_dark=0.30,
    val_urban=0.06,
    val_grey=0.08,
    blend_colour=0.30,
    blend_terrain=0.28,
    blend_landcover=0.42,
    protect_bright_natural=False,
    h_water_min=170.0,
    h_water_max=280.0,
    s_water_min=0.08,
    v_water_min=0.06,
    h_green_min=55.0,
    h_green_max=170.0,
    s_green_min=0.11,
    v_green_min=0.10,
    h_moor_min=15.0,
    h_moor_max=55.0,
    s_moor_min=0.14,
    v_moor_min=0.18,
    v_moor_max=0.82,
    s_grey_max=0.10,
    v_urban_min=0.56,
    v_grey_min=0.18,
    v_grey_max=0.56,
    v_dark_max=0.18,
    gf_s_floor=0.11,
    gf_s_span=0.34,
    gf_v_peak=0.58,
    gf_v_span=0.44,
)

# Med basin, California, Chile central, Cape, SW Australia — olive/dry scrub.
# Calibrated: urban low; olive scrub / pine / bright dry hills high.
# Moor (ochre/olive) is first-class scenic here — lush green is not required.
MEDITERRANEAN = ColourClimate(
    id="mediterranean",
    name="Mediterranean",
    val_water=1.00,
    val_forest=0.90,
    val_moor=0.78,
    val_grass=0.52,
    val_default=0.44,
    val_dark=0.32,
    val_urban=0.06,
    val_grey=0.10,
    blend_colour=0.30,
    blend_terrain=0.30,
    blend_landcover=0.40,
    protect_bright_natural=True,
    val_bright_natural=0.64,
    h_water_min=175.0,
    h_water_max=280.0,
    s_water_min=0.08,
    v_water_min=0.08,
    h_green_min=48.0,
    h_green_max=165.0,
    s_green_min=0.11,
    v_green_min=0.12,
    h_moor_min=8.0,
    h_moor_max=55.0,
    s_moor_min=0.11,
    v_moor_min=0.20,
    v_moor_max=0.90,
    s_grey_max=0.11,
    v_urban_min=0.60,
    v_grey_min=0.20,
    v_grey_max=0.60,
    v_dark_max=0.16,
    gf_s_floor=0.11,
    gf_s_span=0.36,
    gf_v_peak=0.56,
    gf_v_span=0.42,
)

# Amazon, Congo, SE Asia wet — deep saturated canopy.
TROPICAL_RAINFOREST = ColourClimate(
    id="tropical_rainforest",
    name="Tropical rainforest",
    val_water=1.00,
    val_forest=0.96,
    val_moor=0.55,
    val_grass=0.62,
    val_default=0.48,
    val_dark=0.38,
    val_urban=0.08,
    val_grey=0.10,
    blend_colour=0.34,
    blend_terrain=0.22,
    blend_landcover=0.44,
    protect_bright_natural=False,
    h_water_min=170.0,
    h_water_max=290.0,
    s_water_min=0.10,
    v_water_min=0.06,
    h_green_min=70.0,
    h_green_max=175.0,
    s_green_min=0.14,
    v_green_min=0.08,
    h_moor_min=25.0,
    h_moor_max=70.0,
    s_moor_min=0.12,
    v_moor_min=0.15,
    v_moor_max=0.75,
    s_grey_max=0.09,
    v_urban_min=0.58,
    v_grey_min=0.16,
    v_grey_max=0.58,
    v_dark_max=0.14,
    gf_s_floor=0.14,
    gf_s_span=0.40,
    gf_v_peak=0.55,
    gf_v_span=0.40,
)

# Monsoon India/SE Asia, African savanna, N Australia — mixed green + dry grass.
# Gold/ochre grassland is scenic; do not require deep rainforest green.
TROPICAL_MONSOON_SAVANNA = ColourClimate(
    id="tropical_monsoon_savanna",
    name="Tropical monsoon / savanna",
    val_water=1.00,
    val_forest=0.93,
    val_moor=0.74,
    val_grass=0.60,
    val_default=0.46,
    val_dark=0.34,
    val_urban=0.08,
    val_grey=0.11,
    blend_colour=0.28,
    blend_terrain=0.27,
    blend_landcover=0.45,
    protect_bright_natural=True,
    val_bright_natural=0.58,
    h_water_min=170.0,
    h_water_max=285.0,
    s_water_min=0.09,
    v_water_min=0.06,
    h_green_min=55.0,
    h_green_max=170.0,
    s_green_min=0.11,
    v_green_min=0.10,
    h_moor_min=15.0,
    h_moor_max=60.0,
    s_moor_min=0.14,
    v_moor_min=0.20,
    v_moor_max=0.88,
    s_grey_max=0.10,
    v_urban_min=0.60,
    v_grey_min=0.18,
    v_grey_max=0.60,
    v_dark_max=0.16,
    gf_s_floor=0.11,
    gf_s_span=0.36,
    gf_v_peak=0.60,
    gf_v_span=0.44,
)

# Sahara, Arabia, Sonoran, Australian desert — sand protected, scrub/rock scenic.
# Calibrated: bright sand/rock + canyon reds high; concrete urban low.
# Terrain/landcover lean: colour alone is a weak desert scenic proxy.
ARID_HOT = ColourClimate(
    id="arid_hot",
    name="Arid hot desert",
    val_water=1.00,
    val_forest=0.86,
    val_moor=0.80,
    val_grass=0.52,
    val_default=0.48,
    val_dark=0.36,
    val_urban=0.05,
    val_grey=0.10,
    blend_colour=0.22,
    blend_terrain=0.38,
    blend_landcover=0.40,
    protect_bright_natural=True,
    val_bright_natural=0.68,
    h_water_min=175.0,
    h_water_max=275.0,
    s_water_min=0.10,
    v_water_min=0.08,
    h_green_min=55.0,
    h_green_max=165.0,
    s_green_min=0.12,
    v_green_min=0.12,
    h_moor_min=5.0,
    h_moor_max=55.0,
    s_moor_min=0.10,
    v_moor_min=0.22,
    v_moor_max=0.92,
    s_grey_max=0.12,
    v_urban_min=0.62,
    v_grey_min=0.22,
    v_grey_max=0.62,
    v_dark_max=0.16,
    gf_s_floor=0.12,
    gf_s_span=0.35,
    gf_v_peak=0.55,
    gf_v_span=0.40,
)

# Gobi, Patagonia steppe, Great Basin — cold desert / steppe.
ARID_COLD = ColourClimate(
    id="arid_cold",
    name="Arid cold / steppe",
    val_water=1.00,
    val_forest=0.82,
    val_moor=0.68,
    val_grass=0.48,
    val_default=0.44,
    val_dark=0.32,
    val_urban=0.08,
    val_grey=0.12,
    blend_colour=0.22,
    blend_terrain=0.40,
    blend_landcover=0.38,
    protect_bright_natural=True,
    val_bright_natural=0.60,
    h_water_min=170.0,
    h_water_max=275.0,
    s_water_min=0.08,
    v_water_min=0.08,
    h_green_min=50.0,
    h_green_max=160.0,
    s_green_min=0.10,
    v_green_min=0.10,
    h_moor_min=5.0,
    h_moor_max=55.0,
    s_moor_min=0.10,
    v_moor_min=0.18,
    v_moor_max=0.90,
    s_grey_max=0.11,
    v_urban_min=0.62,
    v_grey_min=0.18,
    v_grey_max=0.62,
    v_dark_max=0.16,
    gf_s_floor=0.10,
    gf_s_span=0.32,
    gf_v_peak=0.58,
    gf_v_span=0.42,
)

# SE US, SE China, southern Brazil — humid subtropical greens.
SUBTROPICAL_HUMID = ColourClimate(
    id="subtropical_humid",
    name="Subtropical humid",
    val_water=1.00,
    val_forest=0.94,
    val_moor=0.60,
    val_grass=0.58,
    val_default=0.46,
    val_dark=0.34,
    val_urban=0.08,
    val_grey=0.10,
    blend_colour=0.32,
    blend_terrain=0.24,
    blend_landcover=0.44,
    protect_bright_natural=False,
    h_water_min=172.0,
    h_water_max=285.0,
    s_water_min=0.09,
    v_water_min=0.06,
    h_green_min=65.0,
    h_green_max=172.0,
    s_green_min=0.11,
    v_green_min=0.10,
    h_moor_min=20.0,
    h_moor_max=65.0,
    s_moor_min=0.14,
    v_moor_min=0.18,
    v_moor_max=0.80,
    s_grey_max=0.10,
    v_urban_min=0.58,
    v_grey_min=0.18,
    v_grey_max=0.58,
    v_dark_max=0.16,
    gf_s_floor=0.11,
    gf_s_span=0.36,
    gf_v_peak=0.58,
    gf_v_span=0.42,
)

# Scandinavia inland, Canada taiga, Siberia — conifer dark greens, snow.
BOREAL = ColourClimate(
    id="boreal",
    name="Boreal / taiga",
    val_water=1.00,
    val_forest=0.93,
    val_moor=0.65,
    val_grass=0.50,
    val_default=0.42,
    val_dark=0.36,
    val_urban=0.08,
    val_grey=0.10,
    blend_colour=0.28,
    blend_terrain=0.32,
    blend_landcover=0.40,
    protect_bright_natural=True,
    val_bright_natural=0.68,
    h_water_min=170.0,
    h_water_max=280.0,
    s_water_min=0.08,
    v_water_min=0.06,
    h_green_min=70.0,
    h_green_max=165.0,
    s_green_min=0.10,
    v_green_min=0.08,
    h_moor_min=15.0,
    h_moor_max=70.0,
    s_moor_min=0.12,
    v_moor_min=0.15,
    v_moor_max=0.78,
    s_grey_max=0.10,
    v_urban_min=0.58,
    v_grey_min=0.16,
    v_grey_max=0.58,
    v_dark_max=0.14,
    gf_s_floor=0.10,
    gf_s_span=0.38,
    gf_v_peak=0.50,
    gf_v_span=0.40,
)

# Arctic / Antarctic / high Arctic coasts — snow/ice protected, sparse veg.
TUNDRA_POLAR = ColourClimate(
    id="tundra_polar",
    name="Tundra / polar",
    val_water=1.00,
    val_forest=0.70,
    val_moor=0.58,
    val_grass=0.45,
    val_default=0.42,
    val_dark=0.30,
    val_urban=0.08,
    val_grey=0.12,
    blend_colour=0.20,
    blend_terrain=0.45,
    blend_landcover=0.35,
    protect_bright_natural=True,
    val_bright_natural=0.72,
    h_water_min=175.0,
    h_water_max=270.0,
    s_water_min=0.08,
    v_water_min=0.08,
    h_green_min=60.0,
    h_green_max=160.0,
    s_green_min=0.08,
    v_green_min=0.10,
    h_moor_min=10.0,
    h_moor_max=60.0,
    s_moor_min=0.08,
    v_moor_min=0.15,
    v_moor_max=0.85,
    s_grey_max=0.12,
    v_urban_min=0.70,
    v_grey_min=0.20,
    v_grey_max=0.70,
    v_dark_max=0.14,
    gf_s_floor=0.08,
    gf_s_span=0.30,
    gf_v_peak=0.55,
    gf_v_span=0.40,
)

# High mountains globally (elevation overlay) — snow/rock, terrain-leaning.
# Calibrated: snow/rock high; urban/valley grey low.
ALPINE = ColourClimate(
    id="alpine",
    name="Alpine / high mountain",
    val_water=1.00,
    val_forest=0.92,
    val_moor=0.78,
    val_grass=0.58,
    val_default=0.44,
    val_dark=0.34,
    val_urban=0.05,
    val_grey=0.08,
    blend_colour=0.25,
    blend_terrain=0.40,
    blend_landcover=0.35,
    protect_bright_natural=True,
    val_bright_natural=0.76,
    h_water_min=172.0,
    h_water_max=280.0,
    s_water_min=0.08,
    v_water_min=0.06,
    h_green_min=60.0,
    h_green_max=170.0,
    s_green_min=0.10,
    v_green_min=0.08,
    h_moor_min=10.0,
    h_moor_max=60.0,
    s_moor_min=0.12,
    v_moor_min=0.18,
    v_moor_max=0.85,
    s_grey_max=0.11,
    v_urban_min=0.62,
    v_grey_min=0.18,
    v_grey_max=0.62,
    v_dark_max=0.16,
    gf_s_floor=0.10,
    gf_s_span=0.35,
    gf_v_peak=0.55,
    gf_v_span=0.42,
)

# Humid subtropical / oceanic islands fallback (Hawaii, Caribbean, etc.).
OCEANIC_ISLANDS = ColourClimate(
    id="oceanic_islands",
    name="Oceanic islands",
    val_water=1.00,
    val_forest=0.94,
    val_moor=0.62,
    val_grass=0.58,
    val_default=0.48,
    val_dark=0.35,
    val_urban=0.08,
    val_grey=0.10,
    blend_colour=0.33,
    blend_terrain=0.27,
    blend_landcover=0.40,
    protect_bright_natural=True,
    val_bright_natural=0.60,
    h_water_min=172.0,
    h_water_max=290.0,
    s_water_min=0.10,
    v_water_min=0.08,
    h_green_min=65.0,
    h_green_max=175.0,
    s_green_min=0.12,
    v_green_min=0.10,
    h_moor_min=20.0,
    h_moor_max=65.0,
    s_moor_min=0.12,
    v_moor_min=0.18,
    v_moor_max=0.85,
    s_grey_max=0.10,
    v_urban_min=0.60,
    v_grey_min=0.18,
    v_grey_max=0.60,
    v_dark_max=0.16,
    gf_s_floor=0.12,
    gf_s_span=0.36,
    gf_v_peak=0.58,
    gf_v_span=0.42,
)

# Canonical catalogue (no generic path).
CLIMATES: dict[str, ColourClimate] = {
    c.id: c
    for c in (
        TEMPERATE_OCEANIC,
        TEMPERATE_NW_EUROPE,
        TEMPERATE_CONTINENTAL,
        MEDITERRANEAN,
        TROPICAL_RAINFOREST,
        TROPICAL_MONSOON_SAVANNA,
        ARID_HOT,
        ARID_COLD,
        SUBTROPICAL_HUMID,
        BOREAL,
        TUNDRA_POLAR,
        ALPINE,
        OCEANIC_ISLANDS,
    )
}

# Grid index → climate id (uint8). alpine rarely baked in; elev overlay upgrades.
CLIMATE_IDS: tuple[str, ...] = (
    "temperate_oceanic",
    "temperate_continental",
    "mediterranean",
    "tropical_rainforest",
    "tropical_monsoon_savanna",
    "arid_hot",
    "arid_cold",
    "subtropical_humid",
    "boreal",
    "tundra_polar",
    "alpine",
    "oceanic_islands",
)

CLIMATE_INDEX: dict[str, int] = {cid: i for i, cid in enumerate(CLIMATE_IDS)}

# Legacy stub names → modern ids (tests / old callers).
_LEGACY_ALIASES: dict[str, str] = {
    "arid": "arid_hot",
    "alpine_snow": "alpine",
    "tropical_green": "tropical_rainforest",
    "generic": "oceanic_islands",
}


def _box(lat: float, lng: float, s: float, w: float, n: float, e: float) -> bool:
    return s <= lat <= n and w <= lng <= e


def _lng_in(lng: float, w: float, e: float) -> bool:
    """Inclusive longitude range; supports wrap across ±180 when w > e."""
    if w <= e:
        return w <= lng <= e
    return lng >= w or lng <= e


def alpine_elev_threshold_m(lat: float) -> float:
    """Elevation (m) above which a sample upgrades to alpine."""
    alat = abs(lat)
    if alat < 20:
        return 2500.0
    if alat < 40:
        return 2200.0
    if alat < 55:
        return 2000.0
    return 1800.0


def _open_ocean(lat: float, lng: float) -> bool:
    """Coarse open-ocean masks (no pretend land climate).

    Keep coastal land boxes (California, Chile, NZ, …) out of these masks —
    east/west edges are set seaward of major scenic coasts.
    """
    if _box(lat, lng, -45, -55, 55, -12):  # mid-Atlantic
        return True
    # E Pacific open water only — leave CA/OR/WA and Chile coasts on land.
    if _box(lat, lng, -40, -160, 40, -126):
        return True
    if _lng_in(lng, 160, -160) and abs(lat) < 45:  # central Pacific
        return True
    if _box(lat, lng, -50, 55, -15, 100):  # S Indian Ocean
        return True
    if _box(lat, lng, 5, -180, 40, -150) or _box(lat, lng, 5, 150, 40, 180):
        return True
    return False


def classify_climate(lat: float, lng: float) -> str:
    """Deterministic offline (lat, lng) → climate id. Covers the whole globe.

    Simplified Köppen / biome rules used both to build the shipped 1° grid and
    as a runtime fallback if the grid file is missing.
    """
    lat = float(max(-90.0, min(90.0, lat)))
    lng = float(((lng + 180.0) % 360.0) - 180.0)

    # --- Polar / tundra ------------------------------------------------------
    if lat >= 66.5 or lat <= -60.0:
        return "tundra_polar"
    if lat >= 70.0:
        return "tundra_polar"

    # --- Open ocean (before land-like mid-latitude defaults) ------------------
    if _open_ocean(lat, lng):
        return "oceanic_islands"

    # --- Mediterranean (before SW-USA arid so California coast stays Med) ----
    if (
        _box(lat, lng, 36, -10, 46, 28)  # N Med / S Europe
        or _box(lat, lng, 31, -10, 38, 12)  # Maghreb coast
        or _box(lat, lng, 32, -125, 42, -114)  # California
        or _box(lat, lng, -38, -75, -30, -70)  # Central Chile
        or _box(lat, lng, -35, 17, -32, 23)  # Cape
        or _box(lat, lng, -36, 114, -30, 122)  # SW Australia
        or _box(lat, lng, 30, 25, 40, 40)  # E Med / Levant coast band
    ):
        return "mediterranean"

    # --- Major arid hot deserts ----------------------------------------------
    if (
        _box(lat, lng, 15, -18, 36, 55)  # Sahara / Arabia
        or _box(lat, lng, 22, -115, 36, -104)  # Sonoran / Mojave inland (not CA coast)
        or _box(lat, lng, 35.5, -114.5, 39.5, -109.0)  # Colorado Plateau / Utah canyon
        or _box(lat, lng, -32, 114, -18, 148)  # Australian interior
        or _box(lat, lng, 5, 42, 18, 52)  # Horn / Red Sea arid
        or _box(lat, lng, -28, 12, -18, 25)  # Namib / Kalahari north
        or _box(lat, lng, 20, 68, 30, 75)  # Thar (west India / Pakistan)
    ):
        return "arid_hot"

    # --- Arid cold / steppe --------------------------------------------------
    if (
        _box(lat, lng, 36, 75, 50, 120)  # Gobi / Central Asia
        or _box(lat, lng, -52, -75, -38, -63)  # Patagonia
        # Great Basin / Rockies foothills; Colorado Plateau arid_hot wins first.
        or _box(lat, lng, 36, -120, 45, -104)
        or _box(lat, lng, 45, 50, 55, 80)  # Kazakh steppe
    ):
        return "arid_cold"

    # --- Tropical rainforest -------------------------------------------------
    if abs(lat) <= 12 and (
        _box(lat, lng, -12, -80, 8, -45)  # Amazon
        or _box(lat, lng, -8, 8, 8, 32)  # Congo
        or _box(lat, lng, -10, 95, 10, 150)  # SE Asia / New Guinea
        or _box(lat, lng, -5, -92, 5, -75)  # NW Amazon / Andes foothills wet
    ):
        return "tropical_rainforest"

    # --- Tropical monsoon / savanna ------------------------------------------
    if abs(lat) <= 25 and (
        _box(lat, lng, 5, 68, 28, 98)  # India monsoon
        or _box(lat, lng, -20, 95, 20, 140)  # SE Asia / N Australia band
        or _box(lat, lng, -20, -20, 18, 45)  # African savanna belt
        or _box(lat, lng, -25, -65, 10, -35)  # Brazilian cerrado / dry tropics
        or _box(lat, lng, 7, -95, 22, -82)  # Central America dry tropics
        or _box(lat, lng, -20, 110, -10, 150)  # N Australia
    ):
        return "tropical_monsoon_savanna"

    if abs(lat) <= 12:
        return "tropical_monsoon_savanna"

    # --- Subtropical humid ---------------------------------------------------
    if (
        _box(lat, lng, 24, -100, 38, -74)  # SE US
        or _box(lat, lng, 20, 105, 35, 123)  # SE China
        or _box(lat, lng, -35, -60, -22, -40)  # southern Brazil / Uruguay
        or _box(lat, lng, -35, 135, -25, 155)  # E Australia humid
        or _box(lat, lng, 30, 125, 40, 145)  # Japan / Korea humid
    ):
        return "subtropical_humid"

    # --- Temperate oceanic (UK lock zone + analogues) ------------------------
    if (
        _box(lat, lng, 48.5, -12.0, 61.5, 8.0)  # UK / Ireland / near-coast NW Europe
        or _box(lat, lng, 43, -130, 58, -122)  # Pacific NW
        or _box(lat, lng, -47, 165, -40, 175)  # NZ west / south
        or _box(lat, lng, 42, -10, 52, 2)  # Brittany / Bay of Biscay coast
        or _box(lat, lng, -45, -75, -40, -72)  # Chilean wet south (coastal)
    ):
        return "temperate_oceanic"

    # Western Europe Atlantic fringe
    if _box(lat, lng, 45, -10, 62, 10) and lng <= 8:
        return "temperate_oceanic"

    # --- Boreal / taiga ------------------------------------------------------
    if lat >= 50 and lat < 70 and (
        _box(lat, lng, 50, -140, 70, -50)  # Canada
        or _box(lat, lng, 55, 5, 70, 60)  # Scandinavia / NW Russia
        or _box(lat, lng, 50, 60, 70, 180)  # Siberia
        or (_lng_in(lng, 160, -140) and 50 <= lat < 70)  # Far East / Alaska
    ):
        return "boreal"

    if lat >= 55 and lat < 66.5:
        return "boreal"

    # --- Temperate continental -----------------------------------------------
    if (
        _box(lat, lng, 40, -100, 55, -65)  # NE US / SE Canada inland
        or _box(lat, lng, 42, 5, 56, 40)  # inland Europe
        or _box(lat, lng, 35, 100, 50, 135)  # NE China / Korea inland
        or _box(lat, lng, 35, -110, 50, -80)  # interior N America
        or _box(lat, lng, -45, -70, -35, -55)  # southern cone inland
    ):
        return "temperate_continental"

    if 30 <= abs(lat) <= 55:
        return "temperate_continental"

    # --- Oceanic islands / residual ------------------------------------------
    if abs(lat) < 50:
        return "oceanic_islands"
    return "boreal" if abs(lat) < 66.5 else "tundra_polar"


@lru_cache(maxsize=1)
def _load_grid() -> tuple[np.ndarray, float, float, float] | None:
    """Return (grid[lat_i, lng_i], lat0, lng0, step_deg) or None."""
    if not _GRID_PATH.is_file():
        return None
    try:
        data = np.load(_GRID_PATH)
        grid = np.asarray(data["grid"], dtype=np.uint8)
        lat0 = float(data["lat0"])
        lng0 = float(data["lng0"])
        step = float(data["step"])
        return grid, lat0, lng0, step
    except Exception:
        return None


def climate_id_from_grid(lat: float, lng: float) -> str | None:
    packed = _load_grid()
    if packed is None:
        return None
    grid, lat0, lng0, step = packed
    lat = float(max(-90.0, min(90.0, lat)))
    lng = float(((lng + 180.0) % 360.0) - 180.0)
    li = int(round((lat - lat0) / step))
    ji = int(round((lng - lng0) / step))
    li = max(0, min(grid.shape[0] - 1, li))
    ji = max(0, min(grid.shape[1] - 1, ji))
    idx = int(grid[li, ji])
    if 0 <= idx < len(CLIMATE_IDS):
        return CLIMATE_IDS[idx]
    return None


def get_climate(climate_id: str | None) -> ColourClimate:
    if climate_id:
        if climate_id in CLIMATES:
            return CLIMATES[climate_id]
        mapped = _LEGACY_ALIASES.get(climate_id)
        if mapped and mapped in CLIMATES:
            return CLIMATES[mapped]
    return TEMPERATE_OCEANIC


def select_climate(
    lat: float,
    lng: float,
    climate_id: str | None = None,
    elev_m: float | None = None,
) -> ColourClimate:
    """Pick a colour climate. Explicit id wins; else grid/rules + alpine elev."""
    if climate_id:
        return get_climate(climate_id)

    cid = climate_id_from_grid(lat, lng) or classify_climate(lat, lng)
    climate = get_climate(cid)

    if elev_m is not None and elev_m >= alpine_elev_threshold_m(lat):
        return ALPINE
    return climate


def climate_display_name(climate_id: str | None) -> str:
    c = get_climate(climate_id) if climate_id else None
    if c is None:
        return "Unknown"
    return c.name


def in_uk_explore_zone(lat: float, lng: float) -> bool:
    """National-park explore list is UK-oriented."""
    return 49.5 <= lat <= 61.0 and -8.5 <= lng <= 2.0
