#!/usr/bin/env bash
# Archive friends-demo data/ caches (DB + landcover/elev/tile). Excludes data/osrm.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$ROOT/data"
OUT_DIR="${1:-$ROOT/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$OUT_DIR/scenic-data-$STAMP.tar.gz"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/scenic-backup.XXXXXX")"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

if [[ ! -d "$DATA" ]]; then
  echo "No data/ directory at $DATA" >&2
  exit 1
fi

mkdir -p "$OUT_DIR" "$STAGE"
copied=0
for name in scenic.db landcover_cache elev_cache tile_cache; do
  if [[ -e "$DATA/$name" ]]; then
    cp -a "$DATA/$name" "$STAGE/"
    echo "  + $name"
    copied=$((copied + 1))
  else
    echo "  · skip missing $name"
  fi
done

if [[ "$copied" -eq 0 ]]; then
  echo "Nothing to archive under data/" >&2
  exit 1
fi

tar -C "$STAGE" -czf "$DEST" .
echo "Backup written: $DEST"
echo "Restore: stop the app, then tar -xzf into the repo data/ folder."
echo "(OSRM binaries under data/osrm are intentionally excluded.)"
