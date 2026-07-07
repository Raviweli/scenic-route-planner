"""Ingest + score a grid over a bounding box, then populate the database.

Usage (from project root):
    python -m scripts.build_grid --preset lake-district
    python -m scripts.build_grid --min-lat 54.2 --min-lng -3.4 \
        --max-lat 54.7 --max-lng -2.7 --cell 0.03 --source esri --workers 8

Presets are small enough to score in a minute or two on a normal connection.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Allow running as `python scripts/build_grid.py` too.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app import store, grid, scoring  # noqa: E402

PRESETS = {
    # name: (min_lat, min_lng, max_lat, max_lng, cell_deg)
    "lake-district": (54.35, -3.30, 54.62, -2.85, 0.02),
    "snowdonia": (52.90, -4.15, 53.15, -3.75, 0.02),
    "peak-district": (53.20, -2.00, 53.45, -1.60, 0.02),
    "london": (51.40, -0.30, 51.62, 0.05, 0.02),
    "scotland-highlands": (56.60, -5.30, 57.10, -4.40, 0.03),
}


def parse_args():
    p = argparse.ArgumentParser(description="Build a scenic-scored grid.")
    p.add_argument("--preset", choices=sorted(PRESETS), help="Named region to score.")
    p.add_argument("--min-lat", type=float)
    p.add_argument("--min-lng", type=float)
    p.add_argument("--max-lat", type=float)
    p.add_argument("--max-lng", type=float)
    p.add_argument("--cell", type=float, default=0.02, help="Cell size in degrees.")
    p.add_argument("--source", default="esri", choices=["esri", "synthetic"],
                   help="Imagery source. 'synthetic' runs fully offline.")
    p.add_argument("--workers", type=int, default=8, help="Parallel tile fetches.")
    p.add_argument("--append", action="store_true",
                   help="Add to existing DB instead of describing a fresh region.")
    return p.parse_args()


def resolve_spec(args) -> grid.GridSpec:
    if args.preset:
        mlat, mlng, xlat, xlng, cell = PRESETS[args.preset]
        return grid.GridSpec(mlat, mlng, xlat, xlng, cell)
    required = [args.min_lat, args.min_lng, args.max_lat, args.max_lng]
    if any(v is None for v in required):
        raise SystemExit("Provide --preset OR all of --min-lat/--min-lng/--max-lat/--max-lng.")
    return grid.GridSpec(args.min_lat, args.min_lng, args.max_lat, args.max_lng, args.cell)


def main():
    args = parse_args()
    spec = resolve_spec(args)
    store.init_db()

    cells = grid.build_cells(spec)
    total = len(cells)
    print(f"Region: {spec.min_lat},{spec.min_lng} -> {spec.max_lat},{spec.max_lng}")
    print(f"Grid: {spec.n_rows} rows x {spec.n_cols} cols = {total} cells "
          f"(cell {spec.cell_deg} deg, source={args.source})")
    if total > 2500:
        print("WARNING: large grid; this may take a while and many tile fetches.")

    start = time.time()
    done = 0

    def work(cell):
        s = scoring.score_location(cell.lat, cell.lng, source=args.source)
        return cell, s

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(work, c) for c in cells]
        for fut in as_completed(futures):
            cell, s = fut.result()
            store.upsert_cell(cell, s)
            done += 1
            if done % 25 == 0 or done == total:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed else 0
                print(f"  scored {done}/{total}  ({rate:.1f}/s)")

    store.set_meta("grid", {
        "min_lat": spec.min_lat, "min_lng": spec.min_lng,
        "max_lat": spec.max_lat, "max_lng": spec.max_lng,
        "cell_deg": spec.cell_deg, "n_rows": spec.n_rows, "n_cols": spec.n_cols,
        "source": args.source,
    })
    print(f"Done in {time.time() - start:.1f}s. Total cells in DB: {store.count_cells()}")


if __name__ == "__main__":
    main()
