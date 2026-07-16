#!/usr/bin/env python3
"""Pre-seed elevation disk cache for demo corridors.

Samples points along featured presets (+ optional docs/demo_corridors.txt) and
calls ``enrich.elevation_batch`` so the first friend plan skips a cold elev fill.

Usage (from repo root):
  python scripts/preseed_elev.py
  python scripts/preseed_elev.py --corridors-file docs/demo_corridors.txt --samples 12
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from app import config, enrich
from app.catalog_data import FEATURED_PRESET_IDS, PRESETS


def _load_corridors_file(path: Path) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    out: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) < 4:
            print(f"SKIP corridors line {i}: need lat1,lng1,lat2,lng2[,label]")
            continue
        try:
            a = (float(parts[0]), float(parts[1]))
            b = (float(parts[2]), float(parts[3]))
        except ValueError:
            print(f"SKIP corridors line {i}: bad floats")
            continue
        label = parts[4] if len(parts) >= 5 else f"corridor-{i}"
        out.append((label, a, b))
    return out


def _lerp_samples(
    a: tuple[float, float],
    b: tuple[float, float],
    n: int,
) -> list[tuple[float, float]]:
    if n <= 1:
        return [a]
    out = []
    for i in range(n):
        t = i / (n - 1)
        out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ids",
        default="",
        help="Comma-separated preset ids (default: all FEATURED_PRESET_IDS)",
    )
    ap.add_argument(
        "--corridors-file",
        default="docs/demo_corridors.txt",
        help="Extra A/B pairs (empty string to skip)",
    )
    ap.add_argument(
        "--samples",
        type=int,
        default=10,
        help="Sample points per corridor (default 10)",
    )
    ap.add_argument(
        "--skip-featured",
        action="store_true",
        help="Only seed corridors-file pairs",
    )
    args = ap.parse_args()

    by_id = {p["id"]: p for p in PRESETS}
    jobs: list[tuple[str, tuple[float, float], tuple[float, float]]] = []

    if not args.skip_featured:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()] or list(FEATURED_PRESET_IDS)
        for pid in ids:
            preset = by_id.get(pid)
            if not preset:
                print(f"SKIP {pid}: not in catalogue")
                continue
            a = (float(preset["from"]["lat"]), float(preset["from"]["lng"]))
            b = (float(preset["to"]["lat"]), float(preset["to"]["lng"]))
            jobs.append((pid, a, b))

    if args.corridors_file:
        cpath = Path(args.corridors_file)
        if cpath.is_file():
            jobs.extend(_load_corridors_file(cpath))
        else:
            print(f"WARN: corridors file not found: {cpath}")

    if not jobs:
        print("FAIL: nothing to seed")
        return 1

    n = max(2, int(args.samples))
    print(f"Elev cache dir: {config.ELEV_CACHE_DIR}")
    ok = 0
    fail = 0
    for label, a, b in jobs:
        pts = _lerp_samples(a, b, n)
        print(f"\n=== {label} ({len(pts)} pts) ===")
        t0 = time.perf_counter()
        try:
            vals = enrich.elevation_batch(pts)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {exc}")
            fail += 1
            continue
        elapsed = time.perf_counter() - t0
        if vals is None:
            print(f"  WARN: terrain unavailable ({elapsed:.1f}s)")
            fail += 1
            continue
        print(f"  OK: {len(vals)} elevs in {elapsed:.1f}s")
        ok += 1

    print(f"\nDone: {ok} seeded, {fail} failed of {len(jobs)}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
