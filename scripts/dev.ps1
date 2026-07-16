#Requires -Version 5.1
<#
.SYNOPSIS
  Developer helpers for Scenic Route Planner (Windows-friendly).

.EXAMPLE
  .\scripts\dev.ps1 test
  .\scripts\dev.ps1 run
  .\scripts\dev.ps1 run-lan
  .\scripts\dev.ps1 friends-up
  .\scripts\dev.ps1 smoke-friends
  .\scripts\dev.ps1 smoke-osrm
  .\scripts\dev.ps1 prewarm
  .\scripts\dev.ps1 osrm-up
#>
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("test", "run", "run-lan", "friends-up", "smoke-friends", "smoke-osrm", "prewarm", "osrm-up", "backup-data")]
    [string]$Command
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Get-Python {
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    return "python"
}

function Show-LanUrls {
    param([string]$Port)
    $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.PrefixOrigin -ne 'WellKnown' } |
        Select-Object -ExpandProperty IPAddress -Unique
    if (-not $ips) {
        $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch '^127\.' } |
            Select-Object -ExpandProperty IPAddress -Unique
    }
    Write-Host ""
    Write-Host "LAN URLs (no API key; public_mode stays off):" -ForegroundColor Cyan
    foreach ($ip in $ips) {
        Write-Host "  http://${ip}:${Port}/"
    }
    Write-Host ""
    Write-Host "If other devices cannot connect, allow inbound TCP ${Port} in Windows Firewall (Private profile)."
    Write-Host ""
}

$py = Get-Python

switch ($Command) {
    "test" {
        & $py -m pytest -q
        exit $LASTEXITCODE
    }
    "run" {
        & $py -m uvicorn app.main:app --host 127.0.0.1 --port 8077
        exit $LASTEXITCODE
    }
    "run-lan" {
        # Uvicorn listens on all interfaces; SCENIC_BIND_HOST stays loopback so PUBLIC_MODE stays off.
        # Gate turns on when SCENIC_PUBLIC=1 or SCENIC_BIND_HOST is non-loopback (see app/config.py).
        $env:SCENIC_BIND_HOST = "127.0.0.1"
        Remove-Item Env:SCENIC_PUBLIC -ErrorAction SilentlyContinue
        $port = if ($env:SCENIC_PORT) { $env:SCENIC_PORT } else { "8000" }
        Show-LanUrls -Port $port
        Write-Host "Starting uvicorn on 0.0.0.0:${port} (local: http://127.0.0.1:${port}/)"
        & $py -m uvicorn app.main:app --host 0.0.0.0 --port $port
        exit $LASTEXITCODE
    }
    "friends-up" {
        # Public gate + LAN bind. Does NOT start Docker OSRM — public OSRM by default.
        # Single worker only: SQLite + in-process rate/inflight caps break with --workers > 1.
        if (-not $env:SCENIC_API_KEY) {
            Write-Host "SCENIC_API_KEY is not set. Set a shared secret before inviting friends." -ForegroundColor Yellow
            Write-Host '  $env:SCENIC_API_KEY = "your-long-random-secret"'
            exit 1
        }
        if (-not $env:SCENIC_PUBLIC) { $env:SCENIC_PUBLIC = "1" }
        if (-not $env:SCENIC_BIND_HOST) { $env:SCENIC_BIND_HOST = "0.0.0.0" }
        $hostBind = $env:SCENIC_BIND_HOST
        $port = if ($env:SCENIC_PORT) { $env:SCENIC_PORT } else { "8000" }

        $cacheHint = Join-Path $Root "data\landcover_cache"
        $warm = (Test-Path $cacheHint) -and (@(Get-ChildItem $cacheHint -Filter "*.npz" -ErrorAction SilentlyContinue).Count -gt 0)
        if (-not $warm) {
            Write-Host "Landcover cache looks empty. Prewarm before inviting friends:" -ForegroundColor Yellow
            Write-Host "  .\scripts\dev.ps1 prewarm"
            $ans = Read-Host "Run prewarm now? [y/N]"
            if ($ans -match '^[Yy]') {
                & $py scripts/prewarm_landcover.py
                if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            }
        }

        Write-Host "Friends demo on http://${hostBind}:${port} (public_mode gate + shared key)."
        Write-Host "Using --workers 1 (required for SQLite + in-process caps). See docs/FRIENDS_DEMO.md"
        Write-Host "After start, dogfood: .\scripts\dev.ps1 smoke-friends"
        & $py -m uvicorn app.main:app --host $hostBind --port $port --workers 1
        exit $LASTEXITCODE
    }
    "smoke-friends" {
        & $py scripts/smoke_friends.py
        exit $LASTEXITCODE
    }
    "smoke-osrm" {
        & $py scripts/smoke_osrm.py
        exit $LASTEXITCODE
    }
    "backup-data" {
        & (Join-Path $PSScriptRoot "backup_data.ps1")
        exit $LASTEXITCODE
    }
    "prewarm" {
        & $py scripts/prewarm_landcover.py
        exit $LASTEXITCODE
    }
    "osrm-up" {
        Write-Host "Optional BYO OSRM (needs data/osrm — see docs/SELF_HOSTED_OSRM.md)…"
        docker compose --profile osrm up osrm
        exit $LASTEXITCODE
    }
}
