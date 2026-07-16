"""Central configuration for the Scenic Route Planner."""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "scenic.db"
TILE_CACHE_DIR = DATA_DIR / "tile_cache"
TILE_CACHE_DIR.mkdir(exist_ok=True)
# Persistent land-cover tile cache. Tiles snap to a fixed global grid so every
# route reuses cells fetched by earlier routes; each cell is fetched from Overpass
# at most once ever, then served instantly from disk.
LANDCOVER_CACHE_DIR = DATA_DIR / "landcover_cache"
LANDCOVER_CACHE_DIR.mkdir(exist_ok=True)
# Persistent elevation samples (survives process restart; same spirit as landcover).
ELEV_CACHE_DIR = DATA_DIR / "elev_cache"
ELEV_CACHE_DIR.mkdir(exist_ok=True)
# Corridor field planner: per-cell blend scores (lat/lng + profile id).
FIELD_CELL_CACHE_DIR = DATA_DIR / "field_cell_cache"
FIELD_CELL_CACHE_DIR.mkdir(exist_ok=True)
FRONTEND_DIR = BASE_DIR / "frontend"

# --- Imagery ---------------------------------------------------------------
# Free Esri World Imagery XYZ endpoint (no API key). {z}/{y}/{x}.
ESRI_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
TILE_ZOOM = int(os.environ.get("SCENIC_TILE_ZOOM", "14"))
# Pixel count after downscaling (NxN). Reducing pixels averages out noise.
DOWNSCALE = int(os.environ.get("SCENIC_DOWNSCALE", "24"))
HTTP_TIMEOUT = int(os.environ.get("SCENIC_HTTP_TIMEOUT", "20"))

# --- Upstream services (override for self-hosted / mirrors) ----------------
# OSRM route template must include `{coords}` (lng,lat;…;lng,lat).
OSRM_URL = os.environ.get(
    "SCENIC_OSRM_URL",
    "https://router.project-osrm.org/route/v1/driving/{coords}",
)
# Map-matching endpoint (draw mode). Defaults to OSRM_URL with /match/v1/.
_default_match = OSRM_URL.replace("/route/v1/", "/match/v1/", 1)
OSRM_MATCH_URL = os.environ.get("SCENIC_OSRM_MATCH_URL", _default_match)
# Draw mode: interpolate sketch segments longer than this before map-matching
# (match is a fallback only; primary draw snap is pairwise chord legs).
DRAW_MATCH_SEGMENT_KM = float(os.environ.get("SCENIC_DRAW_MATCH_SEGMENT_KM", "2.0"))
# Per-tracepoint snap radius (metres) for OSRM /match fallback.
DRAW_MATCH_RADIUS_M = float(os.environ.get("SCENIC_DRAW_MATCH_RADIUS_M", "25"))
# Reject match fallback when matched length exceeds sketch haversine by this factor.
DRAW_MATCH_MAX_LENGTH_RATIO = float(
    os.environ.get("SCENIC_DRAW_MATCH_MAX_LENGTH_RATIO", "1.35")
)
# Pairwise draw snap: sample vias along the straight chord between clicks so
# OSRM stays near the dashed preview (spacing, max vias, deviation guard).
DRAW_CHORD_VIA_SPACING_KM = float(
    os.environ.get("SCENIC_DRAW_CHORD_VIA_SPACING_KM", "0.9")
)
DRAW_CHORD_MAX_VIAS = int(os.environ.get("SCENIC_DRAW_CHORD_MAX_VIAS", "16"))
# Reject/retry a leg when any point is farther than this from the click chord.
DRAW_LEG_MAX_DEVIATION_KM = float(
    os.environ.get("SCENIC_DRAW_LEG_MAX_DEVIATION_KM", "1.35")
)
# Reject a leg when routed length exceeds chord length by this factor.
DRAW_LEG_MAX_LENGTH_RATIO = float(
    os.environ.get("SCENIC_DRAW_LEG_MAX_LENGTH_RATIO", "1.75")
)
# Out-and-back spur pruning after draw snap (metres).
DRAW_SPUR_CLOSE_M = float(os.environ.get("SCENIC_DRAW_SPUR_CLOSE_M", "40"))
DRAW_SPUR_MIN_M = float(os.environ.get("SCENIC_DRAW_SPUR_MIN_M", "120"))
DRAW_SPUR_MAX_POINTS = int(os.environ.get("SCENIC_DRAW_SPUR_MAX_POINTS", "80"))
_OVERPASS_DEFAULT = (
    "https://overpass-api.de/api/interpreter,"
    "https://overpass.kumi.systems/api/interpreter"
)
OVERPASS_ENDPOINTS = [
    u.strip() for u in os.environ.get("SCENIC_OVERPASS_URLS", _OVERPASS_DEFAULT).split(",")
    if u.strip()
]
NOMINATIM_URL = os.environ.get(
    "SCENIC_NOMINATIM_URL",
    "https://nominatim.openstreetmap.org/search",
)
# In-process cache bounds (elevation / land-cover cell memo).
CACHE_MAX_ENTRIES = int(os.environ.get("SCENIC_CACHE_MAX_ENTRIES", "4096"))
CACHE_TTL_SEC = float(os.environ.get("SCENIC_CACHE_TTL_SEC", "3600"))

# --- Scoring weights -------------------------------------------------------
# Scenic score = weighted blend of colour composition of the downscaled tile.
W_GREEN = 1.0      # vegetation / farmland / forest
W_BLUE = 1.1       # water / coast (slightly favoured)
W_GREY = 1.0       # built-up / roads / concrete (penalty)
W_BRIGHT_URBAN = 0.3  # bright low-saturation = concrete sprawl (penalty)

# Per-pixel scenic VALUE (0..1) for the colour engine. Each classified pixel is
# assigned one of these, then averaged, so the score spans the full 0-100 range
# and distinguishes land types (water/woodland high, farmland mid, built-up low).
VAL_WATER   = 1.00   # lakes, rivers, sea
VAL_FOREST  = 0.92   # dark, rich green woodland
VAL_MOOR    = 0.68   # heath / moor / bracken / natural bare
VAL_GRASS   = 0.56   # farmland / pasture / bright green
VAL_DEFAULT = 0.45   # unclassified
VAL_DARK    = 0.35   # deep shadow (ambiguous)
VAL_URBAN   = 0.08   # bright concrete / roofs
VAL_GREY    = 0.10   # roads / buildings / grey

# --- Routing ---------------------------------------------------------------
# How strongly a low scenic score inflates travel cost. Higher = bigger
# willingness to detour for scenery when preference is high.
DETOUR_FACTOR = 6.0

# --- Motorway punishment ---------------------------------------------------
# When "avoid motorways" is enabled, every kilometre driven on a motorway adds
# this many *virtual minutes* to a route's cost (ranking tie-break / soft nudge).
# Harsh mode also hard-filters routes with motorway_km above MOTORWAY_EPS_KM.
MOTORWAY_PENALTY_MIN_PER_KM = float(os.environ.get("SCENIC_MOTORWAY_PENALTY", "20.0"))
# Treat tiny junction / rounding noise as zero motorway km.
MOTORWAY_EPS_KM = float(os.environ.get("SCENIC_MOTORWAY_EPS_KM", "0.05"))

# --- Enriched scenic scoring blend -----------------------------------------
# Final scenic score blends three signals. Weights are renormalised if a
# signal is unavailable (e.g. an API is offline).
#   colour     : satellite colour density (green/blue good, grey bad)
#   terrain    : elevation ruggedness / relief (mountains & hills = scenic)
#   landcover  : OSM land use (forest/water/coast/park good, industrial bad)
BLEND_COLOUR = 0.35
BLEND_TERRAIN = 0.25
BLEND_LANDCOVER = 0.40

# Terrain: e-folding scale (m) for soft-saturation relief → 0..100 scenic.
# score ≈ 100 * (1 - exp(-local_range / RELIEF_FULL_M)); not a hard cap.
RELIEF_FULL_M = 180.0
RELIEF_WINDOW = 2  # +/- samples fallback when no km spacing is available
# Physical along-route window for local relief (keeps meaning stable on long routes).
RELIEF_WINDOW_KM = float(os.environ.get("SCENIC_RELIEF_WINDOW_KM", "4.0"))
# Max vertices sampled along an Overpass way/relation geometry (beyond centroid).
LANDCOVER_GEOM_SAMPLES = 10

# Landcover: proximity radius (km) over which features influence a point.
LANDCOVER_RADIUS_KM = 1.5
# A single Overpass query over a large bbox times out and returns nothing, which
# silently collapses land cover to a neutral 50 on every medium/long route. We
# therefore split any bbox larger than LANDCOVER_TILE_DEG into a grid of smaller
# tiles, query each (they each succeed quickly) and merge the results. The tile
# count is capped so we stay polite to the free Overpass endpoints.
LANDCOVER_TILE_DEG = 0.6      # max span (deg) of a single Overpass sub-query
LANDCOVER_MAX_TILES = int(os.environ.get("SCENIC_LANDCOVER_MAX_TILES", "24"))
# Long corridors may fetch up to this many tiles (scaled by A→B haversine km).
LANDCOVER_MAX_TILES_LONG = int(os.environ.get("SCENIC_LANDCOVER_MAX_TILES_LONG", "36"))
# When prefer_axis is set, only keep cells within this half-width (deg) of A→B.
LANDCOVER_CORRIDOR_HALF_WIDTH_DEG = float(
    os.environ.get("SCENIC_LANDCOVER_CORRIDOR_HALF_DEG", "0.35")
)
LANDCOVER_TILE_WORKERS = int(os.environ.get("SCENIC_LANDCOVER_TILE_WORKERS", "8"))
# Overpass `out geom N` / `out center N` node budget per cell query.
LANDCOVER_OUT_GEOM = int(os.environ.get("SCENIC_LANDCOVER_OUT_GEOM", "400"))
# Prefer centre points (fast, reliable on dense UK tiles). Set to "geom" for
# vertex sampling along large forests/water — heavier and often 504s publicly.
LANDCOVER_OUT_MODE = os.environ.get("SCENIC_LANDCOVER_OUT_MODE", "center").strip().lower()
# Split the big tag union into batches so one Overpass call stays under timeout.
LANDCOVER_TAG_BATCH = int(os.environ.get("SCENIC_LANDCOVER_TAG_BATCH", "8"))
# Bound one stuck Overpass cell so a single mirror cannot burn the plan budget.
# Keep under the field landcover window (~15–18s) so one 504 cannot erase the phase.
LANDCOVER_CELL_TIMEOUT_SEC = float(
    os.environ.get("SCENIC_LANDCOVER_CELL_TIMEOUT_SEC", "18")
)
# Extra endpoint attempts per cold cell after the first pass (504 / timeout).
LANDCOVER_FETCH_RETRIES = int(os.environ.get("SCENIC_LANDCOVER_FETCH_RETRIES", "2"))
LANDCOVER_RETRY_BACKOFF_SEC = float(
    os.environ.get("SCENIC_LANDCOVER_RETRY_BACKOFF_SEC", "0.35")
)
# Pause between tag batches so public Overpass rate-limits (429) are less likely.
LANDCOVER_BATCH_GAP_SEC = float(
    os.environ.get("SCENIC_LANDCOVER_BATCH_GAP_SEC", "0.45")
)

# --- Plan wall-clock budget ------------------------------------------------
# Soft ceiling for plan(): after PLAN_BUDGET_SEC - PLAN_RESERVE_SEC of work,
# skip new explore / hard-target rounds and finish with the best route so far.
PLAN_BUDGET_SEC = float(os.environ.get("SCENIC_PLAN_BUDGET_SEC", "30"))
PLAN_RESERVE_SEC = float(os.environ.get("SCENIC_PLAN_RESERVE_SEC", "5"))
# Minimum remaining work time (sec) to start explore / a hard-target round.
PLAN_EXPLORE_MIN_SEC = float(os.environ.get("SCENIC_PLAN_EXPLORE_MIN_SEC", "8"))
# Lower bar to start avoid-motorways diversions (M5-style corridors burn budget fast).
PLAN_AVOID_MW_MIN_SEC = float(os.environ.get("SCENIC_PLAN_AVOID_MW_MIN_SEC", "4"))
PLAN_HT_ROUND_MIN_SEC = float(os.environ.get("SCENIC_PLAN_HT_ROUND_MIN_SEC", "6"))

# Elevation (Open-Meteo) enforces a daily request quota; once exceeded it returns
# HTTP 429. A secondary free provider, in-process cache, and gap-fill keep terrain
# alive — one missing sample no longer drops the whole route.
OPEN_ELEV_FALLBACK = "https://api.opentopodata.org/v1/aster30m"
# Parallel 100-pt elevation provider chunks within a single elevation_batch call.
ELEV_CHUNK_WORKERS = int(os.environ.get("SCENIC_ELEV_CHUNK_WORKERS", "4"))

# --- Plan scoring speed (two-phase + parallel fan-out) ----------------------
# Phase A ranks every candidate with landcover+terrain only; phase B runs full
# colour scoring on the top-K shortlist. Chosen route is always refined at full
# sample density. Override via env for A/B testing without code changes.
SCENIC_COLOUR_TOP_K = int(os.environ.get("SCENIC_COLOUR_TOP_K", "3"))
SAMPLE_SPACING_KM_RANK = float(os.environ.get("SCENIC_SAMPLE_SPACING_KM_RANK", "4.0"))
MAX_SAMPLES_RANK = int(os.environ.get("SCENIC_MAX_SAMPLES_RANK", "100"))
SAMPLE_SPACING_KM = float(os.environ.get("SCENIC_SAMPLE_SPACING_KM", "2.0"))
MAX_SAMPLES = int(os.environ.get("SCENIC_MAX_SAMPLES", "220"))
OSRM_WORKERS = int(os.environ.get("SCENIC_OSRM_WORKERS", "6"))
SCORE_ROUTE_WORKERS = int(os.environ.get("SCENIC_SCORE_ROUTE_WORKERS", "4"))
# In-process TTL cache for parsed OSRM responses (warm re-plans / UI retries).
OSRM_CACHE = os.environ.get("SCENIC_OSRM_CACHE", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
# Self-hosted OSRM only: native exclude=motorway (public demo rejects it).
OSRM_EXCLUDE_MOTORWAY = os.environ.get("SCENIC_OSRM_EXCLUDE_MOTORWAY", "0").strip().lower() not in (
    "0", "false", "no", "off",
)

# --- Public bind gate (auth + rate limit) ------------------------------------
# Localhost DIY stays open. Non-loopback bind or SCENIC_PUBLIC=1 enables the gate.
_BIND_HOST = os.environ.get("SCENIC_BIND_HOST", "127.0.0.1").strip().lower()
_PUBLIC_FLAG = os.environ.get("SCENIC_PUBLIC", "").strip().lower() in (
    "1", "true", "yes", "on",
)
PUBLIC_MODE = _PUBLIC_FLAG or (
    _BIND_HOST not in ("", "127.0.0.1", "localhost", "::1")
)
API_KEY = os.environ.get("SCENIC_API_KEY", "").strip()
RATE_LIMIT_PER_MIN = int(os.environ.get("SCENIC_RATE_LIMIT_PER_MIN", "30"))
# Cap concurrent plan / stream / compare work (friends demo on one host).
MAX_INFLIGHT_PLANS = max(1, int(os.environ.get("SCENIC_MAX_INFLIGHT_PLANS", "2")))

# --- Scenic route generation (waypoint injection) --------------------------
# Genuine scenic alternatives are built by routing through scenic "hotspots".
WAYPOINT_GRID = 5          # NxN candidate waypoints sampled in the corridor
WAYPOINT_CANDIDATES = 3    # top scenic hotspots to route through
MAX_DETOUR_RATIO = 1.6     # reject waypoints whose A->W->B exceeds this x A->B
CORRIDOR_PAD_DEG = 0.15    # bbox padding around the A-B line for search/landcover

# --- Scenic field routing (heatmap-first corridor planner) -----------------
# Fine colour/heatmap cells (~0.11 km at mid-latitudes). Long hops start coarser
# via FIELD_CELL_DEG_LONG, then FIELD_MAX_CELLS coarsens further if needed.
FIELD_CELL_DEG = float(os.environ.get("SCENIC_FIELD_CELL_DEG", "0.001"))
# Starting cell size when A→B exceeds FIELD_LONG_HOP_KM (still finer than legacy 0.015).
FIELD_CELL_DEG_LONG = float(os.environ.get("SCENIC_FIELD_CELL_DEG_LONG", "0.003"))
FIELD_LONG_HOP_KM = float(os.environ.get("SCENIC_FIELD_LONG_HOP_KM", "100.0"))
FIELD_CORRIDOR_PAD_DEG = float(os.environ.get("SCENIC_FIELD_CORRIDOR_PAD_DEG", "0.25"))
# Optional hard strip filter around A→B (0 = use full square heatmap window).
FIELD_CORRIDOR_HALF_WIDTH_DEG = float(
    os.environ.get("SCENIC_FIELD_CORRIDOR_HALF_WIDTH_DEG", "0")
)
# Raised so short UK hops keep ~0.003–0.005° after square pad (vs ~0.01° at 9k).
FIELD_MAX_CELLS = int(os.environ.get("SCENIC_FIELD_MAX_CELLS", "36000"))
FIELD_SCORE_WORKERS = int(os.environ.get("SCENIC_FIELD_SCORE_WORKERS", "16"))
FIELD_MIN_SCENIC_CONNECT = float(os.environ.get("SCENIC_FIELD_MIN_SCENIC_CONNECT", "0"))
# Hard reject floor: cells below this are dropped from green corridor graphs
# (urban grey/white/red-built-up, not soft-averaged into an "OK" spine).
FIELD_REJECT_SCENIC = float(os.environ.get("SCENIC_FIELD_REJECT_SCENIC", "32.0"))
# Drop a corridor spine when this fraction of its nodes fall below reject.
FIELD_REJECT_MAX_FRAC = float(os.environ.get("SCENIC_FIELD_REJECT_MAX_FRAC", "0.08"))
# Colour grey_frac at/above this forces the cell toward reject (concrete sprawl).
FIELD_GREY_REJECT_FRAC = float(os.environ.get("SCENIC_FIELD_GREY_REJECT_FRAC", "0.28"))
# Soft grey pressure starts here (graduated pull-down before hard reject).
FIELD_GREY_SOFT_FRAC = float(os.environ.get("SCENIC_FIELD_GREY_SOFT_FRAC", "0.06"))
# Landcover score below this (urban fabric) caps green colour so town parks
# cannot pull a corridor through the centre of town.
FIELD_URBAN_LAND_CAP = float(os.environ.get("SCENIC_FIELD_URBAN_LAND_CAP", "40.0"))
# Extra Esri zoom when landcover is missing (roofs/roads resolve better than z14).
FIELD_COLOUR_ZOOM_BONUS = int(os.environ.get("SCENIC_FIELD_COLOUR_ZOOM_BONUS", "1"))
# Allow reject cells within this deg of A/B so paths can leave/enter towns.
FIELD_ENDPOINT_REJECT_SLACK_DEG = float(
    os.environ.get("SCENIC_FIELD_ENDPOINT_REJECT_SLACK_DEG", "0.04")
)
# Inflate the padded A→B bbox on the short axis so the search window is ~square.
FIELD_SQUARE_BBOX = os.environ.get("SCENIC_FIELD_SQUARE_BBOX", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
# Stronger scenic appetite when walking the heatmap lattice (vs road DETOUR_FACTOR).
FIELD_DETOUR_FACTOR = float(os.environ.get("SCENIC_FIELD_DETOUR_FACTOR", "10.0"))
# Cells at/above this score count as "green" for mask / corridor diagnostics.
FIELD_GREEN_THRESHOLD = float(os.environ.get("SCENIC_FIELD_GREEN_THRESHOLD", "55.0"))
# Extra green spines besides the primary preference-weighted path (then + baseline).
FIELD_MAX_GREEN_CORRIDORS = int(os.environ.get("SCENIC_FIELD_MAX_GREEN_CORRIDORS", "3"))
# Cap OSRM tournament size (green vias + direct baseline).
FIELD_MAX_CANDIDATES = int(os.environ.get("SCENIC_FIELD_MAX_CANDIDATES", "4"))
# Peak scenic vias sampled along each corridor spine for OSRM.
FIELD_PEAK_VIAS = int(os.environ.get("SCENIC_FIELD_PEAK_VIAS", "3"))
# Min separation (deg) from the primary spine for diversion peak cells.
FIELD_DIVERSION_MIN_SEP_DEG = float(
    os.environ.get("SCENIC_FIELD_DIVERSION_MIN_SEP_DEG", "0.04")
)
# Road-first: skip hotspot candidates whose landcover estimate is this low (urban).
HOTSPOT_URBAN_LAND_MAX = float(os.environ.get("SCENIC_HOTSPOT_URBAN_LAND_MAX", "42.0"))
# Wall-clock reserved for OSRM tournament after heatmap colour (0 = use PLAN_BUDGET_SEC).
FIELD_BUDGET_SEC = float(os.environ.get("SCENIC_FIELD_BUDGET_SEC", "0"))
# Seconds held back from colour/landcover so field routing always gets real OSRM candidates.
FIELD_OSRM_RESERVE_SEC = float(os.environ.get("SCENIC_FIELD_OSRM_RESERVE_SEC", "10"))
# Floor for adaptive OSRM reserve when landcover was warm / fast (frees colour time).
FIELD_OSRM_RESERVE_MIN_SEC = float(
    os.environ.get("SCENIC_FIELD_OSRM_RESERVE_MIN_SEC", "4")
)
# Guaranteed colour wall-clock slice after landcover (reduces proxy-only forever).
FIELD_COLOUR_MIN_SEC = float(os.environ.get("SCENIC_FIELD_COLOUR_MIN_SEC", "6"))
# Perpendicular offset (km) for avoid-motorways corridor diversions on road-first planner.
AVOID_MW_OFFSET_KM = float(os.environ.get("SCENIC_AVOID_MW_OFFSET_KM", "35.0"))

# Escalating rounds used when a hard minimum-scenic target can't be met at the
# default radius. Each tuple widens the search: (pad_deg, grid, candidates,
# detour_ratio, max_chain). Later rounds allow much longer detours and chain
# more scenic hotspots so the route is forced through scenic terrain.
HARD_TARGET_ROUNDS = [
    (0.30, 6, 6, 2.4, 2),
    (0.50, 7, 8, 3.2, 3),
    (0.80, 8, 10, 4.5, 3),
    (1.20, 9, 12, 6.0, 4),
]

# --- "Explore everything" mode -------------------------------------------------
# When enabled, travel time is completely disregarded: routes are ranked purely
# by scenic score and the planner deliberately detours through scenic attractors
# (national parks and world hotspots) and far-flung scenic terrain.
EXPLORE_MAX_DETOUR_RATIO = 3.0   # an attractor route may be up to 3x the direct distance
EXPLORE_PARK_MAX_KM = 80.0       # ...or within 80 km of the straight A->B line
EXPLORE_MAX_PARKS = int(os.environ.get("SCENIC_EXPLORE_MAX_PARKS", "4"))
EXPLORE_MAX_CHAIN = 4            # longest ordered attractor chain to try
EXPLORE_PARK_BBOX_DEG = 0.25     # half-size of the land-cover box around an attractor

# UK National Parks kept as a named alias; the live registry is app/attractors.py
# (UK pack + compact world packs). Prefer attractors.all_attractors().
NATIONAL_PARKS = [
    ("Lake District",                54.470, -3.100),
    ("Snowdonia (Eryri)",            52.900, -3.900),
    ("Peak District",                53.350, -1.800),
    ("Yorkshire Dales",              54.230, -2.150),
    ("North York Moors",             54.370, -0.900),
    ("Northumberland",               55.300, -2.200),
    ("Brecon Beacons",               51.880, -3.430),
    ("Pembrokeshire Coast",          51.800, -5.100),
    ("Exmoor",                       51.130, -3.650),
    ("Dartmoor",                     50.580, -3.900),
    ("New Forest",                   50.870, -1.600),
    ("South Downs",                  50.920, -0.400),
    ("The Broads",                   52.620,  1.500),
    ("Cairngorms",                   57.080, -3.670),
    ("Loch Lomond & The Trossachs",  56.250, -4.600),
]

