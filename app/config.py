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
HTTP_TIMEOUT = 20

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
# this many *virtual minutes* to a route's cost, so scenic non-motorway routes
# win decisively. Deliberately harsh.
MOTORWAY_PENALTY_MIN_PER_KM = float(os.environ.get("SCENIC_MOTORWAY_PENALTY", "20.0"))

# --- Enriched scenic scoring blend -----------------------------------------
# Final scenic score blends three signals. Weights are renormalised if a
# signal is unavailable (e.g. an API is offline).
#   colour     : satellite colour density (green/blue good, grey bad)
#   terrain    : elevation ruggedness / relief (mountains & hills = scenic)
#   landcover  : OSM land use (forest/water/coast/park good, industrial bad)
BLEND_COLOUR = 0.35
BLEND_TERRAIN = 0.25
BLEND_LANDCOVER = 0.40

# Terrain: local relief (metres) mapped 0..RELIEF_FULL -> 0..100 scenic.
RELIEF_FULL_M = 300.0
RELIEF_WINDOW = 2  # +/- samples used for local relief

# Landcover: proximity radius (km) over which features influence a point.
LANDCOVER_RADIUS_KM = 1.5
# A single Overpass query over a large bbox times out and returns nothing, which
# silently collapses land cover to a neutral 50 on every medium/long route. We
# therefore split any bbox larger than LANDCOVER_TILE_DEG into a grid of smaller
# tiles, query each (they each succeed quickly) and merge the results. The tile
# count is capped so we stay polite to the free Overpass endpoints.
LANDCOVER_TILE_DEG = 0.6      # max span (deg) of a single Overpass sub-query
LANDCOVER_MAX_TILES = 40      # hard cap on sub-queries per corridor
LANDCOVER_TILE_WORKERS = 4    # concurrent Overpass tile fetches

# Elevation (Open-Meteo) enforces a daily request quota; once exceeded it returns
# HTTP 429 and terrain silently drops out. A secondary free provider and an
# in-process cache keep terrain alive across the quota window.
OPEN_ELEV_FALLBACK = "https://api.opentopodata.org/v1/aster30m"

# --- Scenic route generation (waypoint injection) --------------------------
# Genuine scenic alternatives are built by routing through scenic "hotspots".
WAYPOINT_GRID = 5          # NxN candidate waypoints sampled in the corridor
WAYPOINT_CANDIDATES = 3    # top scenic hotspots to route through
MAX_DETOUR_RATIO = 1.6     # reject waypoints whose A->W->B exceeds this x A->B
CORRIDOR_PAD_DEG = 0.15    # bbox padding around the A-B line for search/landcover

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
# by scenic score and the planner deliberately detours through national parks and
# far-flung scenic terrain, however long the drive becomes.
EXPLORE_MAX_DETOUR_RATIO = 3.0   # a park route may be up to 3x the direct distance
EXPLORE_PARK_MAX_KM = 80.0       # ...or within 80 km of the straight A->B line
EXPLORE_MAX_PARKS = 6            # cap park waypoints considered (OSRM + tile budget)
EXPLORE_MAX_CHAIN = 4            # longest ordered park "grand tour" chain to try
EXPLORE_PARK_BBOX_DEG = 0.25     # half-size of the land-cover box read around a park

# UK National Parks (approx centroids). Seeded as scenic attractors so a route
# will divert to pass through a park when one lies anywhere near the corridor.
# Outside the UK the planner still finds scenic terrain via land cover + relief
# hotspots; these named parks are a UK-specific booster.
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

