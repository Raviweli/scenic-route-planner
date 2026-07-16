#!/usr/bin/env python3
"""Pre-warm land-cover disk cache for featured presets (+ optional corridors).

Fetches corridor tiles for each id in FEATURED_PRESET_IDS so cold demos
(Denver–Aspen, Interlaken–Zermatt, …) stay under the 30s plan budget.

Usage (from repo root):
  python scripts/prewarm_landcover.py
  python scripts/prewarm_landcover.py --ids denver-to-aspen,interlaken-to-zermatt
  python scripts/prewarm_landcover.py --corridors-file docs/demo_corridors.txt --radius-pad 0.8
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from app import config, enrich
from app.catalog_data import FEATURED_PRESET_IDS, PRESETS


def _bbox_for_axis(
    a: tuple[float, float],
    b: tuple[float, float],
    pad: float | None = None,
) -> tuple[float, float, float, float]:
    pad = pad if pad is not None else config.CORRIDOR_PAD_DEG
    s = min(a[0], b[0]) - pad
    n = max(a[0], b[0]) + pad
    w = min(a[1], b[1]) - pad
    e = max(a[1], b[1]) + pad
    return (s, w, n, e)


def load_corridors_file(path: Path) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    """Parse ``lat1,lng1,lat2,lng2[,label]`` lines into (label, A, B) triples."""
    out: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
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


def _warm_one(
    label: str,
    a: tuple[float, float],
    b: tuple[float, float],
    pad: float | None,
) -> bool:
    box = _bbox_for_axis(a, b, pad=pad)
    dist = enrich._haversine_km(a[0], a[1], b[0], b[1])
    print(f"\n=== {label} ({dist:.0f} km) ===")
    t0 = time.perf_counter()

    def _prog(done: int, total: int) -> None:
        print(f"  tiles {done}/{total}", end="\r", flush=True)

    try:
        feats = enrich.fetch_landcover(box, progress=_prog, prefer_axis=(a, b))
    except Exception as exc:  # noqa: BLE001
        print(f"\n  FAIL: {exc}")
        return False
    elapsed = time.perf_counter() - t0
    if feats is None:
        print(f"\n  WARN: no features ({elapsed:.1f}s)")
        return False
    trunc = feats.get("truncated") or feats.get("landcover_incomplete")
    n_pos = len(feats.get("pos") or [])
    n_neg = len(feats.get("neg") or [])
    flag = " truncated" if trunc else ""
    print(f"\n  OK: +{n_pos} / -{n_neg} pts in {elapsed:.1f}s{flag}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ids",
        default="",
        help="Comma-separated preset ids (default: all FEATURED_PRESET_IDS)",
    )
    ap.add_argument(
        "--radius-pad",
        type=float,
        default=None,
        help="Corridor bbox pad in degrees (default: config.CORRIDOR_PAD_DEG)",
    )
    ap.add_argument(
        "--corridors-file",
        default="",
        help="Optional extra A/B pairs file (see docs/demo_corridors.txt)",
    )
    ap.add_argument(
        "--skip-featured",
        action="store_true",
        help="Only warm corridors-file pairs (skip FEATURED_PRESET_IDS)",
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
        if not cpath.is_file():
            print(f"FAIL: corridors file not found: {cpath}")
            return 1
        jobs.extend(load_corridors_file(cpath))

    if not jobs:
        print("FAIL: nothing to warm")
        return 1

    print(f"Land-cover cache dir: {config.LANDCOVER_CACHE_DIR}")
    if args.radius_pad is not None:
        print(f"Using --radius-pad={args.radius_pad}")
    ok = 0
    fail = 0
    for label, a, b in jobs:
        if _warm_one(label, a, b, args.radius_pad):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} warmed, {fail} failed/skipped of {len(jobs)}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
