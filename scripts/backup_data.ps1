#Requires -Version 5.1
<#
.SYNOPSIS
  Archive friends-demo data/ caches (DB + landcover/elev/tile) for backup/restore.

.EXAMPLE
  .\scripts\backup_data.ps1
  .\scripts\backup_data.ps1 -OutDir C:\Backups\scenic
#>
param(
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $Root "data"
if (-not (Test-Path $Data)) {
    Write-Host "No data/ directory at $Data"
    exit 1
}

if (-not $OutDir) {
    $OutDir = Join-Path $Root "backups"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $OutDir "scenic-data-$stamp.zip"
$stage = Join-Path $env:TEMP "scenic-backup-$stamp"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$include = @(
    "scenic.db",
    "landcover_cache",
    "elev_cache",
    "tile_cache"
)
$copied = 0
foreach ($name in $include) {
    $src = Join-Path $Data $name
    if (Test-Path $src) {
        $target = Join-Path $stage $name
        Copy-Item -Path $src -Destination $target -Recurse -Force
        $copied += 1
        Write-Host "  + $name"
    } else {
        Write-Host "  · skip missing $name"
    }
}

if ($copied -eq 0) {
    Write-Host "Nothing to archive under data/ (expected db or caches)."
    Remove-Item -Recurse -Force $stage
    exit 1
}

if (Test-Path $dest) { Remove-Item -Force $dest }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $dest -Force
Remove-Item -Recurse -Force $stage

Write-Host "Backup written: $dest"
Write-Host "Restore: stop the app, then Expand-Archive into the repo data/ folder."
Write-Host "(OSRM binaries under data/osrm are intentionally excluded.)"
