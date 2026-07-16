# API surface

Contract for the Scenic Route Planner HTTP API. **Supported** endpoints are
what the UI and CONTRIBUTING smoke path rely on. **Experimental** endpoints
exist for tooling or future work — do not wire them into the UI without an
explicit product decision.

The live planner uses **real-road OSRM routing** (`app/roads.py`) as the default.
**Scenic field routing** (`app/field_route.py`) is a supported beta alternative.
The offline cell lattice in `app/graph.py` + `GET /api/cells` remains tooling only.

## Supported (primary product)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/health` | Liveness + local sanity (`cells`, `osrm_configured`, `cache_entries`) |
| `GET /api/geocode?q=` | Nominatim place search proxy (up to 5 hits) |
| `GET /api/route` | Scenic route — road-first (JSON) |
| `GET /api/route/stream` | Same road-first planner as SSE (live search) |
| `GET /api/route/field` | Scenic field planner (JSON, beta) |
| `GET /api/route/field/stream` | Field planner SSE (heatmap / corridors / OSRM / score + optional `cell` overlay) |
| `GET /api/route/compare` | Fastest vs most-scenic pair (JSON; same knobs as plan) |
| `GET /api/route/compare/stream` | Compare SSE (fastest+scenic, or `compare_field=1` → road+field) |
| `POST /api/route/draw` | Score a user-drawn vertex path (OSRM snap + full scenic pipeline) |
| `GET /api/profiles` / `GET /api/profiles/{id}` | Full scenery profile catalogue |
| `GET /api/presets` / `GET /api/presets/{id}` | Drive presets (`?featured=true` → curated list) |
| `GET /api/regions` | Map jump regions |
| Saved routes + history (`/api/routes`, `/api/history`, …) | Persist / reuse plans |
| `GET /api/export/formats` + `POST /api/export/{fmt}` | GPX / GeoJSON / KML / CSV |

### UI contract for profiles & presets

The backend ships a **large** combinatorial profile/preset catalogue for API
consumers. The web UI only exposes:

- A **curated scenery style** dropdown (`STYLES` in `frontend/app.js`) mapping to
  a handful of real profile IDs.
- **Featured presets** via `GET /api/presets?featured=true`, backed by
  `FEATURED_PRESET_IDS` in `app/catalog_data.py`.

## Supported but secondary

| Endpoint | Purpose |
|----------|---------|
| `GET /api/meta` | Grid metadata (useful when a heatmap grid has been built) |
| `GET /api/score?lat=&lng=` | On-demand scenic score for a coordinate |

## Experimental (do not wire UI)

| Surface | Notes |
|---------|--------|
| `GET /api/poi`, `GET /api/poi/categories` | Overpass POI overlay — **experimental**; UI must not depend on it |
| `GET /api/features` (+ `/categories`, `/search`, `/stats`) | Capability catalogue dump — **experimental**; not used by the planner UI |
| `GET /api/cells` | Optional scored-cell heatmap from SQLite |
| `scripts/build_grid.py` + `app/graph.py` | Offline grid build / lattice routing — **not** the live planner |

Mark these experimental in README and leave them unmounted in the shell until a
later wave explicitly adopts them.

## Plan / compare query parameters

Shared by `/api/route`, `/api/route/stream`, `/api/route/compare`, and
`/api/route/compare/stream`:

| Param | Default | Meaning |
|-------|---------|---------|
| `from_lat`, `from_lng`, `to_lat`, `to_lng` | – | start (A) and end (B) |
| `preference` | `0.7` (plan) / `1.0` (compare scenic leg) | 0 = fastest → 1 = most scenic |
| `profile` | `balanced` | scenery profile (weights + detour appetite) |
| `avoid_motorways` | `false` | hard ban on motorway km when any non-motorway candidate exists |
| `min_scenic` | `0` | hard floor (0–100); unmet → most scenic (not fastest) |
| `explore_all` | `false` | disregard time; divert through scenic attractors |
| `time_budget` | `true` | when `false`, skip PLAN_BUDGET_SEC early-exit (cold corridors may take minutes) |
| `via` | – | repeatable `lat,lng` must-pass stops (max 8); OSRM alternatives disabled when present |

Compare always plans a **fastest** leg at `preference=0` (no min_scenic /
explore), then a **scenic** leg with the requested knobs — unless
`compare_field=1`, which plans **road-first** then **field-first** with the
same `preference` / `profile` / `time_budget` / `avoid_motorways`.

### Field route (`/api/route/field`, `/api/route/field/stream`)

| Param | Default | Meaning |
|-------|---------|---------|
| `from_lat`, `from_lng`, `to_lat`, `to_lng` | – | start (A) and end (B) |
| `preference` | `0.7` | heatmap corridor cost vs distance (0 = direct, 1 = most scenic) |
| `profile` | `balanced` | blend weights for per-cell scoring |
| `time_budget` | `true` | respect `PLAN_BUDGET_SEC` for landcover + colour + OSRM tournament |
| `include_grid` | `false` | when true, JSON includes `field_cells` heatmap array |
| `avoid_motorways` | `false` | hard filter zero-motorway candidates (same as road-first) |

Field results include `planner: "field"`, `source: "field"`, `motorway_avoid_met`
(when avoid is on), and `field_meta` with:

- `bbox`, `cell_deg`, `cells_scored`
- `proxy_cells`, `colour_cells` (budget honesty)
- `reject_scenic`, `reject_cells`, `reject_fraction`, `corridors_rejected`
  (urban / bad-colour corridor discards)
- `green_mask`, `green_corridors` (spine kinds + lattice metrics + per-spine `reject_frac`)
- `candidates_tried` / `candidates_tried_n`
- `lattice_avg_scenic`, `road_score`, `snap_delta_scenic`
- `chosen_reason`, `budget_reasons`, `road_snap_method`

SSE emits `phase` events (`grid` / `path` / `osrm` / `score`, plus rare `snap`
fallback) and optional `cell` events for the live heatmap overlay (proxy then
colour upgrades).

Soft-degradation fields on plan results: `budget_exhausted`, `budget_reasons`, `min_scenic_met`,
`motorway_avoid_met`, `time_budget`, `signals.landcover_incomplete`. Plan JSON also includes `hotspots` and
`attractors_used` (`[{name, lat, lng}, …]`) for map narrative markers.

## Frontend deep links

The UI encodes the same plan query params in the page URL (`from_lat` /
`from_lng` / `to_lat` / `to_lng` / `preference` / `profile` /
`avoid_motorways` / `min_scenic` / `explore_all` / `time_budget` / repeatable `via`). After a
successful plan, `history.replaceState` updates the URL so Refresh restores the
trip. Optional `autoplan=1` starts a stream once on load. No backend change
required for deep links beyond accepting the shared query shape.

## Draw route (`POST /api/route/draw`)

Click-to-sketch mode: the client sends user vertices; the server routes through
them on real roads (default) and returns plan-shaped JSON for the existing UI.

| Field | Default | Meaning |
|-------|---------|---------|
| `coords` | – | `[[lat,lng], …]` user vertices (2–50) |
| `profile` | `balanced` | scenery profile id |
| `snap_to_roads` | `true` | Pairwise OSRM legs along each click-to-click chord (`continue_straight` + chord vias); densified sketch match is a guarded fallback; `false` densifies straight segments (~100 m) for scoring only |
| `time_budget` | `true` | landcover fetch deadline (same as plan) |

Response includes `source: "drawn"`, `from` / `to` (first/last vertex),
`chosen`, `alternatives` (single entry), `signals`, `draw_vertices`, `road_snap_method`
(`pairwise` or `match`, optionally `*_pruned`), and `snap_to_roads`. Motorway km comes from OSRM legs when snapped; zero when not.
Directions may be empty or a single synthetic step for off-road sketches.

## Public bind gate

When `SCENIC_PUBLIC=1` or `SCENIC_BIND_HOST` is non-loopback, heavy/mutating
endpoints require `X-API-Key`, `Authorization: Bearer`, **or** `api_key` query
param (for `EventSource`, which cannot set headers) matching `SCENIC_API_KEY`,
plus a per-IP rate limit. `/api/health` remains open and reports `public_mode`,
`osrm_mode` (`public_demo` | `byo`), and `max_inflight_plans`.
Default loopback DIY stays ungated. Concurrent plans are capped by
`SCENIC_MAX_INFLIGHT_PLANS` (default 2) with HTTP 503 when saturated.
See [THREAT_MODEL.md](THREAT_MODEL.md) and [FRIENDS_DEMO.md](FRIENDS_DEMO.md).
