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

- **Water** / **green** / **moor** / **grey** / **urban** / **dark** — HSV edges
  come from the active `ColourClimate` (temperate oceanic matches the locked UK
  bands).

Each class maps to a climate `val_*` value; the score is `mean × 100` plus a small
variety bonus. Colour alone cannot separate, say, saturated farmland from
woodland — that is precisely what the land‑cover signal adds.

> Colour is a **global climate-mapped** model. Every `(lat, lng)` resolves to one
> of 12 `ColourClimate` profiles (HSV bands + scenic values + blend weights) via
> a shipped 1° offline grid (`app/data/climate_grid.npz`, ~2 KB compressed) built
> by `scripts/build_climate_grid.py` from `classify_climate` rules. Temperate
> oceanic HSV numbers stay **locked** for UK/near-coast calibration (city ≈
> high‑40s, countryside ≈ 60–85). High elevation upgrades a sample to `alpine`.
> There is no uncalibrated “pretend UK greens” / `generic` fallback. Do **not**
> retune temperate oceanic thresholds in-place for other biomes; edit that
> climate’s params or add a climate instead.

### Terrain (elevation relief) — optional
`app/enrich.py::elevation_batch` fetches elevations and `relief_scores` maps the
*local* elevation range to 0–100 with **soft saturation**
(`1 - exp(-range / RELIEF_FULL_M)`) so gentle hills and big mountains both
differentiate. Along a scored route the window is **kilometre-based**
(`RELIEF_WINDOW_KM` over cumulative sample distances) so long routes that thin
their sample spacing keep the same physical meaning; index ±`RELIEF_WINDOW` is
only a fallback when no along-route distances are available. Elevation is:

- **Cached** in‑process by rounded coordinate, and
- **Fault‑tolerant**: it tries **Open‑Meteo** first, then falls back to
  **Opentopodata** so a single provider's daily quota (HTTP 429) does not zero
  out terrain. **Gap‑fill** interpolates missing samples so one failed point
  does not drop terrain for the whole route. Only an all‑missing batch omits
  the signal (blend renormalises).
- Also feeds the **alpine colour overlay** when a sample is high enough.

Before blending, `soften_terrain` lifts low relief when colour and/or map
context are already strong, and treats **water/coast proximity** as a relief
exception so lakeshores and sea fronts are not scored as dull flats. It also
adds a climate-aware lift for flat but globally scenic non-green contexts
(desert dune systems, glacier coasts, bare alpine rock, etc.). The blend itself
remains a plain weighted `blend_signals` average.

### Map context (OpenStreetMap land cover) — optional but high‑value
`app/enrich.py::fetch_landcover` reads OSM features via Overpass (`out geom`)
and `landcover_details` scores each sample by proximity to **positive**
features (water, coast, beach, cliff, wetland, scrub/heath, grassland, glacier,
sand/dune, bare rock/scree/ridge/volcano, wood/forest, peak, waterfall,
viewpoint, nature reserve, park, national park, protected area, …)
minus **negative** ones (industrial, residential, retail, commercial, quarry,
landfill, construction, …). Baseline is 50; nearby positives push it up,
negatives down. Large ways/relations are **sampled along geometry** (not
centroid‑only) so roads through forests/parks/water register. The UI labels
this signal **map context** (OSM proximity, not satellite land‑cover class).
Tag weights start from a **fixed global pack**, then pass through a light
climate-aware multiplier layer so arid / alpine / polar scenic tags contribute
more where they should, without forking the whole OSM vocabulary per region.

When `prefer_axis` is set, cells are restricted to a **corridor strip** around
A→B and the tile budget scales with corridor length (`LANDCOVER_MAX_TILES` →
`LANDCOVER_MAX_TILES_LONG`). If coverage is **truncated** but ≥15% of samples
still feel influence from fetched tiles, `landcover_is_usable` keeps the signal
(`truncated` / `landcover_incomplete` remain true); near‑empty influence still
**omits** it so a forced neutral 50 does not dilute colour/terrain at weight
0.40. Hotspot pre‑scoring (`_candidate_waypoints`) uses the same usability gate.

For demos, run `python scripts/prewarm_landcover.py` (or `.\scripts\dev.ps1 prewarm`)
to fill `data/landcover_cache/` for every `FEATURED_PRESET_IDS` corridor before
a cold plan — that keeps featured presets under the 30s `PLAN_BUDGET_SEC`.
Optional extras: `--radius-pad`, `--corridors-file docs/demo_corridors.txt`.
Elevation samples persist under `data/elev_cache/` (see
`enrich.elevation_batch`); optional `python scripts/preseed_elev.py` fills
corridor sample points before the first friend plan.

### Colour climates
`app/climates.py` defines twelve climates (`temperate_oceanic`, `arid_hot`,
`tropical_rainforest`, …; `temperate_nw_europe` remains an alias). `score_route`
looks up a climate **per sample** (with alpine elev overlay); blend weights use
the sample's climate. Temperate oceanic HSV stays **locked**. Alpine,
mediterranean, arid_hot, savanna, and temperate_continental are calibrated toward
city-low / countryside-high — including non-green scenic (canyon red, olive
scrub, gold grassland, snow/rock). See `tests/test_climates.py` golden solids.
Result `signals.climate` / `signals.climate_name` / `signals.climates_used`
expose the majority climate along the route to the UI.

### Biome calibration harness
`scripts/biome_calibration.py` is the quick regression harness for worldwide
scenic semantics. It runs deterministic colour and land-cover fixtures for
temperate forest, Mediterranean olive/scrub, arid sand, desert canyon red rock,
tropical canopy, savannah gold, polar snow/glacier and alpine snow/rock cases,
plus urban-vs-scenic deltas in arid/mediterranean climates, so tuning can be
checked without live routing calls.

### Field colour budget (cold-start)
Field mode previously shared one deadline between Overpass landcover and Esri
colour, so cold corridors often exhausted the window before any satellite colour
ran. Planning now (1) stops landcover early enough to leave a colour floor
(half of `FIELD_COLOUR_MIN_SEC` during the landcover phase; full adaptive OSRM
reserve after), (2) adapts `FIELD_OSRM_RESERVE_SEC` down toward
`FIELD_OSRM_RESERVE_MIN_SEC` when landcover was warm/fast, (3) fetches Overpass
on an A→B **corridor strip** (`LANDCOVER_CORRIDOR_HALF_WIDTH_DEG`) rather than
the full square heatmap window so short UK hops need 1–2 tiles instead of six
cold corners, and (4) prioritises A→B spine cells when spending the colour
budget. `field_meta` exposes `landcover_usable`, `landcover_features`, and tile
counts; `heatmap_proxy_only` is true only when **zero** colour/cache cells
landed; partial colour is progress, not failure.

Public Overpass mirrors (`overpass-api.de`, kumi) remain a **SPOF** for cold
map context — 504/timeouts/429s are common on dense UK tiles. Mitigations: disk
tile cache, A→B strip preference, batched `out center` queries, primary-mirror
preference under deadline, per-cell retries with backoff, and partial coverage
over total failure. Self-hosted Overpass is the production ceiling — see
[`PRODUCTION.md`](PRODUCTION.md).

## 2. The land‑cover tile cache (a key design point)

A single Overpass query over a multi‑degree corridor **times out and returns
nothing**, which silently collapses land cover to a neutral 50 on every medium/
long route. The fix:

- The world is divided into a **fixed 0.6° grid** (`LANDCOVER_TILE_DEG`). Any
  bbox is resolved as the set of grid cells it touches, snapped to that global
  grid so **cells are shared across every route**.
- Each cell is fetched from Overpass **at most once ever**, then written to disk
  (`data/landcover_cache/lc_<gi>_<gj>.npz`) and mirrored in memory. Cold cells
  are fetched concurrently (`LANDCOVER_TILE_WORKERS`, default 8, spread across
  Overpass mirrors); warm cells are effectively instant. Pad-widen only queries
  **missing** cells.
- Overpass queries use `nwr` (node/way/relation) with **batched** tag unions and
  `out center` by default (`LANDCOVER_OUT_MODE`; `geom` remains optional). A
  single giant `out geom` query over a dense UK 0.6° tile routinely 504s or
  returns empty — batching prefers partial corridor cover over total failure.
- The `progress` callback streams `done/total` tile counts to the UI so a cold
  area shows a loading bar instead of appearing frozen.

Consequence: the **first** route through a new region is slower while tiles warm;
everything after is fast, and the cache survives restarts.

> Gotcha: `numpy.savez` appends `.npz` unless the filename already ends in it, so
> the atomic temp file is written as `*.tmp.npz` (not `*.npz.tmp`) to avoid a
> doubled extension that silently breaks disk persistence.

## 3. Dual planners: road-first vs field-first (heatmap-first)

Two live planners coexist; **road-first remains the default** (`app/roads.py::plan`).

| Planner | Module | Flow | When to use |
|---------|--------|------|-------------|
| **Road-first** | `app/roads.py` | OSRM candidates + scenic hotspot injection → two-phase score | Default; fastest warm-cache behaviour; full motorway / min-scenic / explore knobs |
| **Field-first (beta)** | `app/field_route.py` | Wide square heatmap → green corridors → OSRM via peak vias → pick by `score_route` | A/B testing colour-first routing; live heatmap overlay during build |

### Field planner mental model

```text
wide square → scenic heatmap (proxy then colour)
        → detect green corridor spines (+ dull baseline)
        → OSRM via those corridors (K candidates + direct)
        → pick winner by road score_route
```

Colour answers *where the good land is*. OSRM answers *how you drive there*. The
lattice is a **corridor detector**, not the final drive.

Field mode builds an **ephemeral** heatmap over a near-square window around A→B
(not written to the global SQLite `cells` heatmap). Every cell gets a cheap
**proxy** score (terrain + landcover); Esri **colour** is spent in priority order
(high-proxy and near A→B first). Under the shared ~30s budget, unfinished cells
**keep their proxy** — they are never filled with a fake neutral 50.

From the heatmap the planner extracts a preference-weighted **green primary**
spine, optional **diversions** through separated high-score peaks, and a
low-preference **baseline**. Cells below `FIELD_REJECT_SCENIC` (urban grey /
white-concrete / bad built-up colour, or landcover-clamped town fabric) are
**hard-rejected** from the green lattice — not soft-averaged into an OK path.
Corridors that still cross too many reject cells are discarded
(`field_meta.corridors_rejected`) and another diversion is tried. Peak scenic
vias along each spine steer OSRM; a direct OSRM baseline stays in the tournament
so greener roads only win when they actually beat dull geometry on
`score_route`. `avoid_motorways` uses the same zero-motorway hard filter as
road-first.

Per-cell blend scores persist under `data/field_cell_cache/` (npz keyed by
rounded lat/lng + profile id). Config knobs: `FIELD_CELL_DEG` (default **0.001**,
~2× finer again vs 0.002; long hops start at `FIELD_CELL_DEG_LONG` **0.003**),
`FIELD_CORRIDOR_PAD_DEG`, `FIELD_SQUARE_BBOX`, `FIELD_CORRIDOR_HALF_WIDTH_DEG`
(optional strip; default off), `FIELD_MAX_CELLS` (default **36000** so short UK
hops keep ~0.003–0.005° after square pad), `FIELD_SCORE_WORKERS`,
`FIELD_DETOUR_FACTOR`, `FIELD_GREEN_THRESHOLD`, `FIELD_REJECT_SCENIC`,
`FIELD_REJECT_MAX_FRAC`, `FIELD_GREY_REJECT_FRAC`, `FIELD_URBAN_LAND_CAP`,
`FIELD_MAX_GREEN_CORRIDORS`, `FIELD_MAX_CANDIDATES`, `FIELD_PEAK_VIAS`,
`FIELD_MIN_SCENIC_CONNECT`.

Results expose rich `field_meta` (`bbox`, `cell_deg`, `proxy_cells`, `colour_cells`,
`heatmap_proxy_only`, `reject_cells`, `corridors_rejected`, `osrm_reserve_sec`,
`colour_floor_sec`, `green_corridors`, `candidates_tried`, `lattice_avg_scenic`,
`road_score`, `snap_delta_scenic`, `chosen_reason`, `budget_reasons`,
`motorway_avoid_reason`, …). When `heatmap_proxy_only` is true (zero colour/cache
cells) the UI explains that the lattice used terrain/map estimates only. Partial
colour is reported via `proxy_cells` / `colour_cells` counts.

Expect **higher latency** on cold corridors (parallel cell colour + landcover
warm). Prewarm landcover (`scripts/prewarm_landcover.py`) helps both planners.
The offline `scripts/build_grid.py` + `GET /api/cells` tooling is unchanged.

## 4. Route generation & ranking (road-first)

`app/roads.py::plan` (and its streaming twin `plan_events`) drive the search:

1. **Base + hotspot candidates** — OSRM base route and alternatives (fetched **in
   parallel with** corridor landcover), plus routes forced through scenic
   hotspots. Hotspot / explore / hard-target OSRM calls are **fan-out parallel**
   (`OSRM_WORKERS`, default 6). Parsed OSRM responses are **TTL-cached** in
   process (`SCENIC_OSRM_CACHE`, default on) so warm re-plans skip the public RTT.
2. **Two-phase scoring** — every new candidate gets a cheap **proxy** score
   (landcover + terrain only, coarse sample spacing), with **shared elev
   prefetch** then **parallel** proxy workers (`SCORE_ROUTE_WORKERS`). Full
   satellite **colour** runs only on the **top-K** by proxy (`SCENIC_COLOUR_TOP_K`,
   default 3), in parallel, **reusing** elev/climate from `_score_meta`. The
   eventual **chosen** route is always **refined** at full density
   (`SAMPLE_SPACING_KM` / `MAX_SAMPLES`), fetching elev/colour only for sample
   indices denser than the coarse set. Ranking shortlist colour uses coarser
   caps (`SAMPLE_SPACING_KM_RANK` / `MAX_SAMPLES_RANK`). Elevation provider
   chunks also fan out (`ELEV_CHUNK_WORKERS`, default 4).
3. **Incremental scoring** — explore / hard-target **never** wipe
   `avg_scenic_score` on the whole pool. Only routes missing a score are
   proxy/colour-scored. When the corridor pad widens and landcover is merged,
   existing routes **cheap-reblend** landcover/terrain from cached sample
   signals (no Esri re-fetch).
4. **Explore‑everything** (`explore_all`) — seeds waypoints at nearby scenic
   attractors from `app/attractors.py` (UK national parks plus compact world
   packs: US icons, Alps, NZ, Patagonia, Japan Alps, Scandinavian fjords, …),
   individually and as ordered multi‑attractor "grand tours", reads land cover
   around parks **in parallel with** the attractor OSRM fan-out (same overlap
   pattern as hard-target), and **ranks by scenic score only** (motorway
   mileage is a tiebreak; travel time is disregarded). Skipped when remaining
   plan budget is below `PLAN_EXPLORE_MIN_SEC`. `_nearby_attractors`
   selects points within a perpendicular distance of the A→B line or a bounded
   detour ratio — there is no UK‑only hard gate.
5. **Hard minimum‑scenic target** (`min_scenic`) — if no candidate meets the
   floor, the search **escalates** through `HARD_TARGET_ROUNDS`, each widening
   the corridor, allowing longer detours, sampling a denser grid and chaining
   more hotspots. Widened landcover fetches **overlap** OSRM fan-out (waypoints
   picked from prior features); LC is merged before scoring new routes. Continues
   until a route qualifies, the **plan time budget** runs out (see below), or the
   terrain genuinely cannot reach the target (most‑scenic route returned, flagged).
6. **Ranking** — normal mode uses `route_cost` (duration inflated by low scenery
   plus optional motorway penalty); explore mode ranks purely by scenery.
   `select_chosen` then applies the min‑scenic floor. Signal blending is
   centralised in `blend_signals` (weights renormalise when a signal is missing).
   Hard‑target land‑cover fetches **merge** into any explore‑mode park features
   rather than replacing them.
7. **Plan time budget** (~30s wall‑clock) — `PLAN_BUDGET_SEC` (default 30) minus
   `PLAN_RESERVE_SEC` (default 5) is the work deadline for landcover / explore /
   hard‑target. After that, new explore diversions and HT rounds are skipped and
   the best scored route is refined and returned. Cold landcover stops scheduling
   cells at the deadline (`fetch_landcover(deadline=)`), with per‑cell Overpass
   bounded by `LANDCOVER_CELL_TIMEOUT_SEC` (default 18) and tighter tile caps
   (`LANDCOVER_MAX_TILES` 24 → `LANDCOVER_MAX_TILES_LONG` 36). Soft degradation:
   partial map context, fewer HT rounds, `min_scenic_met` may be false. Results
   include `budget_sec`, `elapsed_ms`, `budget_exhausted`, and `budget_reasons`
   (e.g. `landcover_truncated`, `explore_skipped`, `hard_target_stopped`); SSE
   emits a phase when stopping early.

Plan results (and SSE `timings` / `done` events) include lightweight
`timings_ms` with `osrm`, `landcover`, `score`, and total `elapsed` wall-clock
for before/after measurement. **Wave 2** (after two-phase / parallel Wave 1)
targets lower `timings_ms.score` / `.landcover` / warm `.osrm` via parallel
proxy+elev, `_score_meta` reuse into colour/refine, explore/hard-target LC
overlap, OSRM TTL cache, and a smaller cold Overpass `out geom` budget
(`LANDCOVER_OUT_GEOM`, default 600). Self-hosted OSRM/Overpass remain the
production ceiling when public APIs dominate latency — see
[`PRODUCTION.md`](PRODUCTION.md) and [`SELF_HOSTED_OSRM.md`](SELF_HOSTED_OSRM.md).

### Motorway handling
The OSRM demo server rejects native `exclude=motorway`, so motorway mileage is
detected from step `classes` (preferred), then ref/name patterns (UK/IE `M` /
`A*(M)`, US Interstates, name keywords such as autobahn/autoroute/expressway;
continental `A\d` only with motorway name context so UK A‑roads are not flagged).
When `avoid_motorways` is on, routes with `motorway_km` above `MOTORWAY_EPS_KM`
are **hard-filtered** whenever any zero-motorway candidate exists; the planner
also escalates attractor / hard-target search to find non-motorway geometries.
A per‑km penalty (`MOTORWAY_PENALTY_MIN_PER_KM`) remains as a ranking nudge.
`motorway_avoid_met` is false only when no motorway-free route could be found.

## 4. Live streaming (SSE)

`plan_events` runs `plan` in a daemon thread; its `progress` callback pushes
event dicts onto a `queue.Queue`, which the generator drains and yields as
`data: {json}\n\n`. Event types: `start`, `phase`, `landcover` (tile progress),
`candidate` (a scored route, with a decimated polyline for cheap live drawing;
may include `phase: proxy|colour`), `timings` (`timings_ms`), `round` (target
escalation), `done` (full result, including `timings_ms`) and `error`.
`EventSource` is GET‑only, so all parameters go in the query string.

## 5. Configuration map (`app/config.py`)

| Area | Keys |
|------|------|
| Imagery | `TILE_ZOOM`, `DOWNSCALE`, `VAL_*` colour values |
| Blend weights | `BLEND_COLOUR`, `BLEND_TERRAIN`, `BLEND_LANDCOVER` |
| Terrain | `RELIEF_FULL_M` (e‑folding scale), `RELIEF_WINDOW_KM`, `RELIEF_WINDOW` (index fallback), `OPEN_ELEV_FALLBACK` |
| Map context | `LANDCOVER_RADIUS_KM`, `LANDCOVER_TILE_DEG`, `LANDCOVER_MAX_TILES` (24), `LANDCOVER_MAX_TILES_LONG` (36), `LANDCOVER_CORRIDOR_HALF_WIDTH_DEG`, `LANDCOVER_TILE_WORKERS` (default 8), `LANDCOVER_OUT_GEOM` (default 400), `LANDCOVER_OUT_MODE` (`center`), `LANDCOVER_TAG_BATCH` (8), `LANDCOVER_CELL_TIMEOUT_SEC` (18), `LANDCOVER_FETCH_RETRIES` (2), `LANDCOVER_CACHE_DIR`, `LANDCOVER_GEOM_SAMPLES` |
| Plan speed | `SCENIC_COLOUR_TOP_K`, `SAMPLE_SPACING_KM` / `MAX_SAMPLES` (full refine), `SAMPLE_SPACING_KM_RANK` / `MAX_SAMPLES_RANK` (proxy + shortlist colour), `OSRM_WORKERS`, `SCORE_ROUTE_WORKERS`, `ELEV_CHUNK_WORKERS`, `SCENIC_OSRM_CACHE` |
| Plan budget | `PLAN_BUDGET_SEC` (30), `PLAN_RESERVE_SEC` (5), `PLAN_EXPLORE_MIN_SEC` (8), `PLAN_HT_ROUND_MIN_SEC` (6) |
| Waypoint search | `WAYPOINT_GRID`, `WAYPOINT_CANDIDATES`, `MAX_DETOUR_RATIO`, `CORRIDOR_PAD_DEG` |
| Hard target | `HARD_TARGET_ROUNDS` |
| Explore mode | `EXPLORE_MAX_DETOUR_RATIO`, `EXPLORE_PARK_MAX_KM`, `EXPLORE_MAX_PARKS`, `EXPLORE_MAX_CHAIN`, `EXPLORE_PARK_BBOX_DEG`, `NATIONAL_PARKS` (UK pack alias), `app/attractors.py` packs |
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
- Explore mode uses pluggable worldwide attractor packs (`app/attractors.py`,
  ~150–250 curated centroids). Plan results expose `hotspots` and
  `attractors_used` for map markers.
- The synthetic image source keeps the pipeline runnable fully offline for
  development/testing.
- `app/graph.py` is optional heatmap/lattice tooling and is **not** imported by
  live route endpoints (see [API_SURFACE.md](API_SURFACE.md)).
- Friends demo (public OSRM default): [FRIENDS_DEMO.md](FRIENDS_DEMO.md).
- Optional BYO router appendix: [SELF_HOSTED_OSRM.md](SELF_HOSTED_OSRM.md)
  (`SCENIC_OSRM_URL` + optional compose profile `osrm`).
