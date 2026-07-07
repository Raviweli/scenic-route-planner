# Architecture & Technical Notes

This document explains how the Scenic Route Planner scores scenery and builds
routes, and records the design decisions that matter most when extending it.

## 1. Scenic score = a blend of three signals

Every route is sampled along its length (`score_route` in `app/roads.py`). At
each sample point three independent signals are computed and blended into a
single 0–100 score, then distance‑weighted along the route.

### Colour (satellite imagery) — always available
`app/scoring.py` fetches an Esri World Imagery tile for the point, **downscales**
it (averaging out noise), converts to HSV and classifies each pixel:

- **Water** — blue hue with saturation `s >= 0.08`.
- **Green / vegetation** — green hue with `s >= 0.10`; a `gf = (s − 0.10)/0.35`
  factor rewards more vivid greenery.
- **Moorland** — muted green/brown with `s >= 0.15`.
- **Grey / built‑up** — low saturation `s < 0.10` → not scenic.

Each class maps to a `VAL_*` value; the score is `mean × 100` plus a small
variety bonus. Colour alone cannot separate, say, saturated farmland from
woodland — that is precisely what the land‑cover signal adds.

> ⚠️ **The colour thresholds above are deliberately fixed.** They were tuned and
> approved to give a sensible spread (dense city ≈ high‑40s, countryside ≈ 60–85).
> Do **not** change them without an explicit request — earlier "improvements"
> flattened the score and had to be reverted.

### Terrain (elevation relief) — optional
`app/enrich.py::elevation_batch` fetches elevations and `relief_scores` maps the
*local* elevation range (over a small window) to 0–100 — hilly and mountainous
stretches score higher than flat ones. Elevation is:

- **Cached** in‑process by rounded coordinate, and
- **Fault‑tolerant**: it tries **Open‑Meteo** first, then falls back to
  **Opentopodata** so a single provider's daily quota (HTTP 429) does not zero
  out terrain. If both fail, the blend simply renormalises over the remaining
  signals.

### Land cover (OpenStreetMap) — optional but high‑value
`app/enrich.py::fetch_landcover` reads OSM features via Overpass and
`landcover_details` scores each sample point by proximity to **positive**
features (water, coast, wood/forest, peak, nature reserve, park, national park,
protected area) minus **negative** ones (industrial, residential, retail,
commercial). Baseline is 50; nearby positives push it up, negatives down.

## 2. The land‑cover tile cache (a key design point)

A single Overpass query over a multi‑degree corridor **times out and returns
nothing**, which silently collapses land cover to a neutral 50 on every medium/
long route. The fix:

- The world is divided into a **fixed 0.6° grid** (`LANDCOVER_TILE_DEG`). Any
  bbox is resolved as the set of grid cells it touches, snapped to that global
  grid so **cells are shared across every route**.
- Each cell is fetched from Overpass **at most once ever**, then written to disk
  (`data/landcover_cache/lc_<gi>_<gj>.npz`) and mirrored in memory. Cold cells
  are fetched concurrently (a few workers, spread across Overpass mirrors); warm
  cells are effectively instant.
- Overpass queries use `nwr` (node/way/relation in one clause) with `out center`
  to keep each tile light enough to return within the server timeout.
- The `progress` callback streams `done/total` tile counts to the UI so a cold
  area shows a loading bar instead of appearing frozen.

Consequence: the **first** route through a new region is slower while tiles warm;
everything after is fast, and the cache survives restarts.

> Gotcha: `numpy.savez` appends `.npz` unless the filename already ends in it, so
> the atomic temp file is written as `*.tmp.npz` (not `*.npz.tmp`) to avoid a
> doubled extension that silently breaks disk persistence.

## 3. Route generation & ranking

`app/roads.py::plan` (and its streaming twin `plan_events`) drive the search:

1. **Base + hotspot candidates** — OSRM base route and alternatives, plus routes
   forced through scenic hotspots sampled in the corridor.
2. **Explore‑everything** (`explore_all`) — seeds waypoints at nearby national
   parks (individually and as ordered multi‑park "grand tours"), reads land cover
   around each park so those off‑corridor segments score correctly, re‑scores
   everything, and **ranks by scenic score only** (motorway mileage is a
   tiebreak; travel time is disregarded). `_nearby_parks` selects parks within a
   perpendicular distance of the A→B line or a bounded detour ratio.
3. **Hard minimum‑scenic target** (`min_scenic`) — if no candidate meets the
   floor, the search **escalates** through `HARD_TARGET_ROUNDS`, each widening
   the corridor, allowing longer detours, sampling a denser grid and chaining
   more hotspots, until a route qualifies (or the terrain genuinely cannot reach
   the target, in which case the most‑scenic route is returned, flagged).
4. **Ranking** — normal mode blends duration with scenic quality and the motorway
   penalty; explore mode ranks purely by scenery.

### Motorway handling
The OSRM demo server rejects native `exclude=motorway`, so motorway mileage is
detected from step `ref`/`name` and penalised heavily per km
(`MOTORWAY_PENALTY_MIN_PER_KM`) in the cost function when `avoid_motorways` is on.

## 4. Live streaming (SSE)

`plan_events` runs `plan` in a daemon thread; its `progress` callback pushes
event dicts onto a `queue.Queue`, which the generator drains and yields as
`data: {json}\n\n`. Event types: `start`, `phase`, `landcover` (tile progress),
`candidate` (a scored route, with a decimated polyline for cheap live drawing),
`round` (target escalation), `done` (full result) and `error`. `EventSource` is
GET‑only, so all parameters go in the query string.

## 5. Configuration map (`app/config.py`)

| Area | Keys |
|------|------|
| Imagery | `TILE_ZOOM`, `DOWNSCALE`, `VAL_*` colour values |
| Blend weights | `BLEND_COLOUR`, `BLEND_TERRAIN`, `BLEND_LANDCOVER` |
| Terrain | `RELIEF_FULL_M`, `RELIEF_WINDOW`, `OPEN_ELEV_FALLBACK` |
| Land cover | `LANDCOVER_RADIUS_KM`, `LANDCOVER_TILE_DEG`, `LANDCOVER_MAX_TILES`, `LANDCOVER_TILE_WORKERS`, `LANDCOVER_CACHE_DIR` |
| Waypoint search | `WAYPOINT_GRID`, `WAYPOINT_CANDIDATES`, `MAX_DETOUR_RATIO`, `CORRIDOR_PAD_DEG` |
| Hard target | `HARD_TARGET_ROUNDS` |
| Explore mode | `EXPLORE_MAX_DETOUR_RATIO`, `EXPLORE_PARK_MAX_KM`, `EXPLORE_MAX_PARKS`, `EXPLORE_MAX_CHAIN`, `EXPLORE_PARK_BBOX_DEG`, `NATIONAL_PARKS` |
| Routing | `MOTORWAY_PENALTY_MIN_PER_KM`, `DETOUR_FACTOR` |

## 6. External services (all free, no key)

| Service | Used for | Notes |
|---------|----------|-------|
| Esri World Imagery | satellite tiles | tile cache in `data/tile_cache/` |
| OSRM demo (`router.project-osrm.org`) | real‑road routing | `alternatives` A→B only; no native motorway exclude |
| Open‑Meteo Elevation | elevation | daily quota → 429; cached + fallback |
| Opentopodata | elevation fallback | stricter rate limits |
| OSM Overpass | land cover | heavy; tiled + disk‑cached as above |

## 7. Known limitations

- Public services are rate‑limited/slow; cold first‑runs in a new area are the
  main cost. Caches and fallbacks mitigate but don't eliminate this.
- National parks are UK‑only; global scenery relies on land cover + relief.
- The synthetic image source keeps the pipeline runnable fully offline for
  development/testing.
