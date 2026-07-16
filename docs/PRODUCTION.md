# Production / self-hosted path

The MVP depends on **public demo endpoints**. That is fine for a trusted friends
demo on LAN/VPS ([FRIENDS_DEMO.md](FRIENDS_DEMO.md)); it is not suitable for a
multi-user product.

## Friends demo first

For a handful of friends: public OSRM by default, `SCENIC_PUBLIC=1` +
`SCENIC_API_KEY`, warm land-cover/elev caches, optional TLS reverse proxy.
See [FRIENDS_DEMO.md](FRIENDS_DEMO.md). Do **not** treat a regional OSM extract
as the main path.

## Why self-host (later)

| Service (MVP) | Risk in production |
|---------------|--------------------|
| `router.project-osrm.org` | Rate limits, ToS, no SLA, shared capacity — global SPOF for routing |
| Overpass mirrors | Timeouts, fair-use limits, cold corridors slow — global SPOF for map context |
| Esri World Imagery XYZ | Attribution/ToS; not an owned imagery pipeline |
| Open-Meteo / Opentopodata | Quotas; elevation gaps (mitigated by `data/elev_cache/`) |
| Nominatim (via `/api/geocode`) | Strict usage policy; needs identifying contact |

Public OSRM and Overpass remain shared global single points of failure for any
worldwide demo that is not self-hosted.

## Recommended target architecture

1. **Routing** — Optional BYO: point `SCENIC_OSRM_URL` at any OSRM-compatible
   endpoint you already run. Self-host **OSRM** / Valhalla / GraphHopper only if
   you need reliability beyond public demo fair-use. Keep the same candidate /
   hotspot / ranking logic in `app/roads.py`. Regional extract notes live in
   [SELF_HOSTED_OSRM.md](SELF_HOSTED_OSRM.md) (appendix).
2. **Land cover** — Run a private Overpass instance, or precompute land-cover
   tiles into PostGIS / GeoPackage and replace `enrich.fetch_landcover`.
3. **Elevation** — Local DEM (e.g. Copernicus DEM, OS Terrain) served via a
   small elevation API or sampled offline into the elev tile cache.
4. **Imagery** — Optional: Sentinel-2 / local XYZ for colour scoring; keep the
   synthetic fallback for CI and offline.
5. **Persistence** — Single-host friends demos stay on **SQLite**
   (`data/scenic.db`) with on-disk caches under `data/`. **When you outgrow
   single-host SQLite** (multi-worker, multi-tenant, or write-heavy shared
   state): move metadata/history to **Postgres**, and consider **Redis** (or
   shared disk) for elevation/land-cover memo across processes. No migration
   ships in-tree yet — reopen scope when you leave the friends-demo bar.
6. **Edge** — Auth, rate limits, TLS, and observability in front of FastAPI.
   The app ships a minimal public gate: set `SCENIC_PUBLIC=1` (or
   `SCENIC_BIND_HOST` to a non-loopback host) and `SCENIC_API_KEY`; clients send
   `X-API-Key` (UI) or `api_key` query (SSE). Cap concurrent plans with
   `SCENIC_MAX_INFLIGHT_PLANS` (default 2). Prefer a reverse proxy TLS terminator
   for real deploys; Caddy `flush_interval -1` for SSE. Localhost `127.0.0.1`
   stays open for DIY. Friends path uses **`--workers 1` only**. See
   [THREAT_MODEL.md](THREAT_MODEL.md) and [FRIENDS_DEMO.md](FRIENDS_DEMO.md).

## Migration order

1. Pin deps + CI (done) and keep unit tests green with `source=synthetic`.
2. Ship friends demo on public OSRM ([FRIENDS_DEMO.md](FRIENDS_DEMO.md)).
3. Optionally swap OSRM URL to a BYO router via `SCENIC_OSRM_URL` — appendix
   [SELF_HOSTED_OSRM.md](SELF_HOSTED_OSRM.md).
4. Replace Overpass with cached/self-hosted land cover; warm caches for target regions.
5. Enable the public gate (`SCENIC_PUBLIC=1` + `SCENIC_API_KEY`) before any
   non-loopback bind (see [THREAT_MODEL.md](THREAT_MODEL.md)).
6. Only then consider multi-worker deploy and a public UI.
