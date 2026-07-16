# Contributing

Local MVP development notes. DIY bind is loopback — see
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md). Sharing with friends:
[`docs/FRIENDS_DEMO.md`](docs/FRIENDS_DEMO.md) (public OSRM by default).

## Setup (<5 minutes)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.lock.txt
# or, for a looser install: pip install -r requirements.txt
```

## Day-to-day commands

Prefer the helper script (PowerShell):

```powershell
.\scripts\dev.ps1 test          # pytest
.\scripts\dev.ps1 run           # uvicorn on 127.0.0.1:8077
.\scripts\dev.ps1 friends-up    # LAN/VPS bind + public gate, --workers 1
.\scripts\dev.ps1 smoke-friends # dogfood public gate after friends-up
.\scripts\dev.ps1 smoke-osrm    # ping OSRM (public or SCENIC_OSRM_URL)
.\scripts\dev.ps1 prewarm       # warm landcover_cache for featured presets
.\scripts\dev.ps1 backup-data   # archive data/ db + caches
.\scripts\dev.ps1 osrm-up       # optional BYO: docker compose --profile osrm up
```

Or run directly:

```powershell
python -m pytest -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8077
python scripts/smoke_osrm.py
python scripts/smoke_friends.py
python scripts/prewarm_landcover.py
python scripts/preseed_elev.py
```

Open http://127.0.0.1:8077 after `run`.

**Friends path = single worker** (`--workers 1`). Multi-worker breaks SQLite and
in-process rate/inflight caps — see [`docs/FRIENDS_DEMO.md`](docs/FRIENDS_DEMO.md).

### Reliable demos (public OSRM + warm land cover)

Public OSRM works worldwide with **no Docker**. Before inviting friends:

1. Pre-warm featured-preset corridors:
   `.\scripts\dev.ps1 prewarm` (fills `data/landcover_cache/`; elevation also
   persists under `data/elev_cache/` after first plans, or run
   `python scripts/preseed_elev.py`).
2. Start with the friends gate: set `SCENIC_API_KEY` then
   `.\scripts\dev.ps1 friends-up` — see [`docs/FRIENDS_DEMO.md`](docs/FRIENDS_DEMO.md).
3. Dogfood: `.\scripts\dev.ps1 smoke-friends`.
4. Confirm `/api/health` shows `osrm_mode: "public_demo"` (or `byo` if you set
   `SCENIC_OSRM_URL`).

Optional: bring-your-own OSRM — advanced appendix in
[`docs/SELF_HOSTED_OSRM.md`](docs/SELF_HOSTED_OSRM.md).

## Logging

```powershell
$env:SCENIC_LOG_LEVEL = "DEBUG"   # default INFO
```

Land-cover truncation and OSRM failures log at WARNING. Plan finishes emit a
structured `plan_finished` line (`elapsed_ms`, `budget_exhausted`,
`budget_reasons`, `osrm_mode`). Access logs redact `api_key` query values.
Client responses stay generic (no stack traces).

## Manual UI smoke checklist

With the app running (public OSRM is fine for a short Keswick↔Ambleside check):

1. **Preset** — load a featured scenic drive from the preset dropdown; start/end markers appear.
2. **Plan** — click Plan; watch live search progress, then pick an alternative in the route list.
3. **Compare** — click Compare; confirm fastest vs scenic cards and that a mid-flight Plan stream is cancelled.
4. **Save** — save the active route with a name/tags; find it under **My routes**.
5. **History** — open My routes → History → **Reuse**; confirm A/B and profile restore on the Plan tab.
6. **Export** — choose GPX (or GeoJSON) and download the active route.

Do **not** expect POI overlays or a scenic heatmap in the shell — those APIs are
experimental (see [`docs/API_SURFACE.md`](docs/API_SURFACE.md)).

## Tests & CI

- Offline unit tests: `python -m pytest -q`
- CI runs lockfile install + `compileall` on `app`, `tests`, `scripts`, then
  pytest (`.github/workflows/ci.yml`).
- After dependency bumps, re-run `.\scripts\dev.ps1 smoke-friends` locally once
  against a friends-up host.

## API contract

Supported vs experimental endpoints:
[`docs/API_SURFACE.md`](docs/API_SURFACE.md).
