#!/usr/bin/env python3
"""Build the shipped 1° world colour-climate grid.

Writes ``app/data/climate_grid.npz`` (~65 KB) from ``classify_climate`` rules
so runtime lookup needs no network. Re-run after changing geographic rules:

    python scripts/build_climate_grid.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.climates import CLIMATE_INDEX, classify_climate  # noqa: E402

OUT = ROOT / "app" / "data" / "climate_grid.npz"
STEP = 1.0
LAT0 = -90.0
LNG0 = -180.0
# Inclusive: -90..90 → 181 rows; -180..179 → 360 cols (180° cell is same as -180).
NLAT = 181
NLNG = 360


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    grid = np.zeros((NLAT, NLNG), dtype=np.uint8)
    for i in range(NLAT):
        lat = LAT0 + i * STEP
        for j in range(NLNG):
            lng = LNG0 + j * STEP
            cid = classify_climate(lat, lng)
            grid[i, j] = CLIMATE_INDEX[cid]
    np.savez_compressed(
        OUT,
        grid=grid,
        lat0=np.float64(LAT0),
        lng0=np.float64(LNG0),
        step=np.float64(STEP),
        ids=np.array(list(CLIMATE_INDEX.keys())),
    )
    size_kb = OUT.stat().st_size / 1024.0
    print(f"Wrote {OUT} ({size_kb:.1f} KB) shape={grid.shape}")


if __name__ == "__main__":
    main()
