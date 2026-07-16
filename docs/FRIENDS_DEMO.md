# Friends demo — private scenic planner on your LAN / VPS

Share a **worldwide** scenic route planner with a few trusted friends: one
machine runs the app, friends open the URL and enter a shared host key.
**Public OSRM is the default** — no Docker, no regional OSM extract.

## Checklist (≈15 minutes)

1. **Clone + install**
   ```powershell
   pip install -r requirements.lock.txt
   ```

2. **Configure** — copy [`.env.example`](../.env.example) to `.env` (or set
   env vars in the shell):
   - `SCENIC_BIND_HOST=0.0.0.0` (or your LAN IP)
   - `SCENIC_PUBLIC=1`
   - `SCENIC_API_KEY=` a long random shared secret (treat like a **password**;
     rotate if it ever appears in a screenshot, ticket, or access log)
   - optionally tune fair-use knobs (see below)

3. **Prewarm land cover (required invite gate)**  
   Featured presets stay under the ~30s plan budget only when corridor tiles
   are warm. **Do this before inviting anyone:**
   ```powershell
   python scripts/prewarm_landcover.py
   # or: .\scripts\dev.ps1 prewarm
   # optional extra corridors + pad:
   #   python scripts/prewarm_landcover.py --corridors-file docs/demo_corridors.txt --radius-pad 0.8
   ```
   Elevation samples persist under `data/elev_cache/` after the first plans
   (and optionally via `python scripts/preseed_elev.py`). Restarts stay warm.

4. **Start the app (single worker only)**
   ```powershell
   .\scripts\dev.ps1 friends-up
   # or:
   #   $env:SCENIC_PUBLIC=1; $env:SCENIC_API_KEY="…"; $env:SCENIC_BIND_HOST="0.0.0.0"
   #   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
   ```
   **Always `--workers 1`.** SQLite plus in-process rate limits and inflight
   caps are process-local — multi-worker silently breaks fairness and can
   corrupt the DB under write load.

5. **Dogfood before invites**
   ```powershell
   .\scripts\dev.ps1 smoke-friends
   # or: python scripts/smoke_friends.py
   python scripts/demo_corridor_qa.py --quick
   ```
   Expects the server on `http://127.0.0.1:8000` (override with
   `SCENIC_SMOKE_BASE`) and the same `SCENIC_API_KEY`. Checks health
   (`public_mode`, `osrm_mode`), rejects unauthenticated calls, and
   handshakes an authorized SSE stream. `demo_corridor_qa.py --quick` runs
   road / field / compare_field / avoid_motorways on 12 dogfood corridors
   (no API key needed on localhost).

6. **Share** the URL **and** the API key.
   - LAN-only: `http://<your-lan-ip>:8000` is OK.
   - Anything off-LAN (VPS, friends over the internet) **must** use **TLS** —
     `api_key` rides on EventSource query strings and must not travel in clear
     HTTP (see TLS runbook below).
   Friends open the UI, paste the key into **Host key**, then plan A→B anywhere.

7. **Sanity check** — `GET /api/health` should show `"public_mode": true`,
   `"osrm_mode": "public_demo"` (or `"byo"`), `"workers": 1`, and cache /
   inflight fields.

## Fair-use playbook

This is for a **handful of friends**, not the open internet.

| Knob | Recommended default | Notes |
|------|---------------------|--------|
| `SCENIC_RATE_LIMIT_PER_MIN` | `30` | Per-IP; raise slightly for a busy LAN party |
| `SCENIC_MAX_INFLIGHT_PLANS` | `2` | Keeps one cold Overpass corridor from melting the host |
| `SCENIC_PLAN_BUDGET_SEC` | `30` | Soft wall-clock; cold corridors may soft-degrade. UI **Advanced → Plan time budget** can turn this off per request (`time_budget=false`) for multi-minute cold searches. |

**SPOFs you inherit** with the friends defaults: public OSRM
(`router.project-osrm.org`) and public Overpass mirrors. Keep traffic modest,

> **Scenic field (beta):** Advanced → **Scenic field (beta)** runs a slower
> heatmap-first planner (`/api/route/field/stream`: wide scenic colour map →
> green corridors → OSRM). Skip it for the first demo unless you are
> experimenting — prewarm landcover helps, but cell colour scoring adds
> **Field mode** paints a wide scenic heatmap before OSRM and has higher
> latency on cold corridors. When **no** satellite colour lands, the UI and
> `field_meta.heatmap_proxy_only` say so (terrain/map estimates, not broken).
> A colour floor + adaptive OSRM reserve reduce true proxy-only outcomes on
> warm or partially-warm corridors.
prewarm featured (and any dogfood) corridors, and prefer TLS + reverse proxy
on a VPS.

When public OSRM flakes, set BYO `SCENIC_OSRM_URL` (next section) rather than
opening the demo to the world.

## Cold corridors (what friends see)

The first plan through a brand-new area fetches land-cover tiles from Overpass
and may stop early under the plan budget. That is **soft degrade**, not a
crash: the UI status line says the corridor is still warming / using partial
map context. A second plan in the same area is usually fast (disk cache).

To force a full search on a cold corridor, open **Advanced** in the plan panel
and uncheck **Plan time budget (~30s)** (or pass `time_budget=false`). Explore
and hard-target will not early-exit for wall-clock; expect multi-minute runs.

## Optional: bring-your-own router

If you already run an OSRM-compatible endpoint, set:

```text
SCENIC_OSRM_URL=http://127.0.0.1:5000/route/v1/driving/{coords}
```

Health then reports `osrm_mode: "byo"` and `osrm_reachable` after a cheap ping.
Building/running a regional extract is **not** required for the friends demo;
see [SELF_HOSTED_OSRM.md](SELF_HOSTED_OSRM.md) only as advanced optional reading.

## Optional: Docker app-only (no OSRM container)

```powershell
docker compose --profile app up scenic-app
```

The compose **app** profile runs uvicorn with **`--workers 1`** on
`0.0.0.0:8000` and does **not** depend on a local OSRM service.

## TLS runbook (WAN) — Caddy

**Rule:** LAN-only HTTP is fine. Anything reachable off-LAN **must** terminate
TLS because SSE plans append `api_key` on the query string (EventSource cannot
set headers). The app redacts `api_key` from access logs, but cleartext HTTP on
the public internet still leaks the key to the network path.

Complete Caddyfile example (HTTPS + SSE-friendly reverse proxy to uvicorn):

```caddy
scenic.example.com {
    encode gzip

    reverse_proxy 127.0.0.1:8000 {
        # SSE: disable response buffering so phase/landcover events flush live
        flush_interval -1
        transport http {
            read_buffer 4096
        }
        header_up Host {host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Point DNS at the VPS, run Caddy (automatic certificates), keep uvicorn on
loopback (`127.0.0.1:8000`) with `--workers 1`, and share
`https://scenic.example.com` plus the host key.

Or nginx:

```nginx
server {
    listen 443 ssl;
    server_name scenic.example.com;
    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;   # important for SSE
        proxy_read_timeout 3600;
    }
}
```

## Backup / restore `data/`

Before a VPS upgrade or wipe:

```powershell
.\scripts\backup_data.ps1
# or: bash scripts/backup_data.sh
```

Archives `scenic.db`, `landcover_cache/`, `elev_cache/`, and `tile_cache/`
(excludes huge OSRM binaries under `data/osrm/`). Restore by stopping the app
and unpacking into `data/`.

## Elevation cache

`app/enrich.py::elevation_batch` writes samples under `data/elev_cache/`. They
survive restarts. Optional offline fill for demo corridors:

```powershell
python scripts/preseed_elev.py
# uses featured presets + docs/demo_corridors.txt sample points
```

## What friends see

With `public_mode` on, the UI shows a **Host key** field (stored in
`sessionStorage`). Every `fetch` sends `X-API-Key`; live plan/compare streams
append `&api_key=` because `EventSource` cannot set headers. Access logs redact
that query value.

## Later / out of scope

Shipped in this harden pass: extended prewarm (`--radius-pad`,
`docs/demo_corridors.txt`), elev preseed, structured `plan_finished` logs,
health `inflight_plans` / cache counts. **Not** in scope: public SaaS,
multi-worker scale-out, private Overpass, or recentering the product on a
regional Lakes extract. When you outgrow single-host SQLite, see the Postgres
stub note in [PRODUCTION.md](PRODUCTION.md).
