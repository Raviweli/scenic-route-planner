# Threat model — Scenic Route Planner

## Intended deployment

**Localhost by default.** Bind to `127.0.0.1` (as in the README quick start).
Loopback DIY use has **no** authentication — that is intentional.

**Trusted friends demo:** bind non-loopback with the public gate
(`SCENIC_PUBLIC=1` or `SCENIC_BIND_HOST` non-loopback) **and** a strong
`SCENIC_API_KEY`. Friends enter the key in the UI (or use `api_key` on SSE).
See [FRIENDS_DEMO.md](FRIENDS_DEMO.md). Do **not** expose without the gate.

## Trust boundaries

| Actor | Trust |
|-------|--------|
| Local user on the same machine | Fully trusted |
| Browser origin talking to the local API | Trusted only because the bind is loopback |
| Friend with the shared host key | Semi-trusted; rate-limited + inflight-capped |
| Public OSRM / Overpass / Esri / Open-Meteo / Nominatim | Untrusted third parties; responses are used for routing/scoring |
| Anyone who can reach a non-loopback bind without the key | **Untrusted** — rejected by the gate |

## Assets

- SQLite DB at `data/scenic.db` (saved routes, history, optional heatmap cells)
- Disk caches under `data/tile_cache/`, `data/landcover_cache/`, `data/elev_cache/`
- Outbound quota / goodwill toward free public geo APIs
- Shared `SCENIC_API_KEY` (treat like a password; avoid logging query-string keys)

## Known risks if exposed beyond localhost

1. **Open proxy** — route planning and geocode amplify traffic to OSRM,
   Overpass, Esri, elevation providers, and Nominatim (mitigated by API key +
   per-IP rate limit + `SCENIC_MAX_INFLIGHT_PLANS` when public mode is on).
2. **Key in query string** — SSE uses `api_key=` because EventSource cannot set
   headers; prefer HTTPS so the key is not visible on the wire; avoid putting
   the URL in public logs.
3. **Stored XSS** — mitigated in the UI by HTML-escaping saved names/notes/tags;
   still do not treat the UI as a multi-user product.
4. **Export DoS** — coordinate arrays are capped (50 000 points) but large
   requests remain expensive.
5. **SQLite concurrency** — multiple uvicorn workers against one SQLite file are
   unsafe; stick to a single worker for local / friends use.

## Mitigations already in place

- Prefer `127.0.0.1` in documented DIY startup
- Lat/lng range validation on routing endpoints
- Export coordinate length and range checks
- Nominatim calls go through `/api/geocode` with an identifying User-Agent
- Parameterized SQL; export XML escaping
- UI escapes user-controlled saved-route fields
- **Public gate** (`app/main.py` middleware): when `SCENIC_PUBLIC=1` or
  `SCENIC_BIND_HOST` is non-loopback, mutating/heavy routes require
  `X-API-Key` / `Authorization: Bearer` / `api_key` query matching
  `SCENIC_API_KEY`, plus an in-process per-IP rate limit
  (`SCENIC_RATE_LIMIT_PER_MIN`, default 30). Protected prefixes: `/api/route*`,
  `/api/geocode`, `/api/routes*`, `/api/history*`, `/api/export*`, `/api/score`.
  `/api/health` stays open. Loopback with default config remains ungated.
- **Inflight plan cap** (`SCENIC_MAX_INFLIGHT_PLANS`, default 2).

## Before any public bind (minimum)

1. Set `SCENIC_PUBLIC=1` (or `SCENIC_BIND_HOST=0.0.0.0`) **and** a strong
   `SCENIC_API_KEY`
2. Tune `SCENIC_RATE_LIMIT_PER_MIN` and `SCENIC_MAX_INFLIGHT_PLANS` for your capacity
3. Prefer TLS (Caddy/nginx) so the host key is not sent in cleartext
4. Structured logging and error messages that do not leak internals
5. Keep traffic to a few friends on public OSRM fair-use — or BYO router
   (see [PRODUCTION.md](PRODUCTION.md), [FRIENDS_DEMO.md](FRIENDS_DEMO.md))
6. Pin dependencies from `requirements.lock.txt` and keep CI green
