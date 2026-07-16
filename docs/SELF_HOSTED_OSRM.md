# Optional appendix: self-hosted / BYO OSRM

> **Not required for the friends demo.** Worldwide planning uses **public OSRM**
> by default. Share the app with a host key via [FRIENDS_DEMO.md](FRIENDS_DEMO.md).
> Use this page only if you already want a private router URL
> (`SCENIC_OSRM_URL`).

# Example path: Cumbria / Lake District extract

Use this when you want routing that does not depend on
`router.project-osrm.org`. Land cover and elevation still use public APIs
(see [PRODUCTION.md](PRODUCTION.md)).

## One path (recommended)

```powershell
# 1. Download + build the extract once (sections below)
# 2. Start OSRM (compose profile — not the default friends path)
docker compose --profile osrm up osrm
#    or: .\scripts\dev.ps1 osrm-up

# 3. Point the app at it (PowerShell)
$env:SCENIC_OSRM_URL = "http://127.0.0.1:5000/route/v1/driving/{coords}"

# 4. Smoke-check, then run the app
.\scripts\dev.ps1 smoke-osrm
.\scripts\dev.ps1 run
```

`GET /api/health` reports `osrm_mode: "byo"` and `osrm_reachable` when
`SCENIC_OSRM_URL` is set. Leave it unset for `osrm_mode: "public_demo"`
(friends default — no Docker).

## Requirements

- Docker Desktop (or Docker Engine + Compose)
- ~2–4 GB free disk for a county extract; more RAM if you later switch to a full GB extract

## Folder layout

After the steps below you should have:

```
data/osrm/
  cumbria-latest.osm.pbf          # downloaded extract (~40–50 MB)
  lake-district.osm.pbf           # copy/rename used by extract (or symlink)
  lake-district.osrm              # produced by osrm-extract
  lake-district.osrm.*            # partition / customize artefacts
```

`data/` is gitignored — do not commit PBF or `.osrm*` files.

## Download a concrete small PBF

Full Great Britain is ~2 GB — too heavy for a laptop demo. Use Geofabrik’s
**Cumbria** extract (~43 MB), which covers the Lake District smoke route
(Keswick → Ambleside):

```powershell
New-Item -ItemType Directory -Force -Path data\osrm | Out-Null

# ~43 MB — https://download.geofabrik.de/europe/united-kingdom/england/cumbria.html
Invoke-WebRequest `
  -Uri "https://download.geofabrik.de/europe/united-kingdom/england/cumbria-latest.osm.pbf" `
  -OutFile "data\osrm\cumbria-latest.osm.pbf"

Copy-Item data\osrm\cumbria-latest.osm.pbf data\osrm\lake-district.osm.pbf
```

(Alternative: clip a custom bbox with [BBBike](https://extract.bbbike.org/) and
save the `.osm.pbf` as `data/osrm/lake-district.osm.pbf`.)

## Build the extract once

From the repo root (PowerShell):

```powershell
docker run --rm -v ${PWD}/data/osrm:/data ghcr.io/project-osrm/osrm-backend:latest `
  osrm-extract -p /opt/car.lua /data/lake-district.osm.pbf

docker run --rm -v ${PWD}/data/osrm:/data ghcr.io/project-osrm/osrm-backend:latest `
  osrm-partition /data/lake-district.osrm

docker run --rm -v ${PWD}/data/osrm:/data ghcr.io/project-osrm/osrm-backend:latest `
  osrm-customize /data/lake-district.osrm
```

You should end up with `data/osrm/lake-district.osrm*` files.

## Run OSRM

```powershell
docker compose --profile osrm up osrm
# or: .\scripts\dev.ps1 osrm-up
```

OSRM listens on `http://127.0.0.1:5000`.

## Point the app at it

```powershell
$env:SCENIC_OSRM_URL = "http://127.0.0.1:5000/route/v1/driving/{coords}"
.\scripts\dev.ps1 run
```

Plan routes inside the extract coverage (Cumbria / Lake District). Outside that
bbox OSRM will fail or return poor results — fall back to the public demo URL
for wider UK trips.

## Smoke check

```powershell
$env:SCENIC_OSRM_URL = "http://127.0.0.1:5000/route/v1/driving/{coords}"
.\scripts\dev.ps1 smoke-osrm
# or: python scripts/smoke_osrm.py
```

The script plans Keswick → Ambleside and prints distance/duration.
