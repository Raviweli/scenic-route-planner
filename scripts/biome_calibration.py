#!/usr/bin/env python3
"""Biome calibration harness for worldwide scenic semantics.

Runs a compact, deterministic fixture set across colour climates and map-context
fixtures so "green is not always scenic" can be checked without live routing.

Examples:

    python scripts/biome_calibration.py
    python scripts/biome_calibration.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.climates import (  # noqa: E402
    ALPINE,
    ARID_HOT,
    MEDITERRANEAN,
    TEMPERATE_OCEANIC,
    TROPICAL_MONSOON_SAVANNA,
    TROPICAL_RAINFOREST,
    TUNDRA_POLAR,
)
from app.enrich import landcover_details, soften_terrain  # noqa: E402
from app.scoring import analyse_image  # noqa: E402


def _solid(rgb: tuple[int, int, int], size: int = 72) -> Image.Image:
    return Image.new("RGB", (size, size), rgb)


COLOUR_FIXTURES = [
    {
        "name": "temperate_forest",
        "climate": TEMPERATE_OCEANIC,
        "rgb": (35, 110, 45),
        "expect_min": 60.0,
    },
    {
        "name": "mediterranean_olive_hills",
        "climate": MEDITERRANEAN,
        "rgb": (90, 110, 55),
        "expect_min": 55.0,
    },
    {
        "name": "mediterranean_dry_scrub",
        "climate": MEDITERRANEAN,
        "rgb": (150, 130, 70),
        "expect_min": 55.0,
    },
    {
        "name": "arid_bright_sand",
        "climate": ARID_HOT,
        "rgb": (210, 195, 165),
        "expect_min": 55.0,
    },
    {
        "name": "desert_canyon_red_rock",
        "climate": ARID_HOT,
        "rgb": (180, 90, 55),
        "expect_min": 55.0,
    },
    {
        "name": "desert_canyon_sandstone",
        "climate": ARID_HOT,
        "rgb": (195, 120, 70),
        "expect_min": 55.0,
    },
    {
        "name": "tropical_deep_canopy",
        "climate": TROPICAL_RAINFOREST,
        "rgb": (20, 90, 35),
        "expect_min": 55.0,
    },
    {
        "name": "savannah_gold_grass",
        "climate": TROPICAL_MONSOON_SAVANNA,
        "rgb": (170, 140, 60),
        "expect_min": 55.0,
    },
    {
        "name": "polar_snow_ice",
        "climate": TUNDRA_POLAR,
        "rgb": (235, 235, 240),
        "expect_min": 55.0,
    },
    {
        "name": "alpine_snow_rock",
        "climate": ALPINE,
        "rgb": (228, 230, 233),
        "expect_min": 60.0,
    },
    {
        "name": "alpine_meadow_pasture",
        "climate": ALPINE,
        "rgb": (120, 145, 55),
        "expect_min": 55.0,
    },
    {
        "name": "arid_sage_scrub",
        "climate": ARID_HOT,
        "rgb": (140, 150, 95),
        "expect_min": 50.0,
    },
    {
        "name": "generic_city_grey",
        "climate": TEMPERATE_OCEANIC,
        "rgb": (145, 145, 148),
        "expect_max": 50.0,
    },
    {
        "name": "arid_city_below_sand",
        "climate": ARID_HOT,
        "rgb": (145, 145, 148),
        "expect_max": 50.0,
        "compare_above": {
            "rgb": (210, 195, 165),
            "min_delta": 15.0,
        },
    },
    {
        "name": "mediterranean_city_below_scrub",
        "climate": MEDITERRANEAN,
        "rgb": (145, 145, 148),
        "expect_max": 50.0,
        "compare_above": {
            "rgb": (90, 110, 55),
            "min_delta": 15.0,
        },
    },
]

LANDCOVER_FIXTURES = [
    {
        "name": "desert_sand_context",
        "climate_id": "arid_hot",
        "features": {
            "pos": np.array([[25.0, 30.0, 0.7]]),
            "neg": np.empty((0, 3)),
            "pos_labels": ["sand"],
            "neg_labels": [],
        },
        "coord": (25.0, 30.0),
        "terrain": 18.0,
        "colour": 42.0,
        "expect_soft_min": 48.0,
    },
    {
        "name": "desert_canyon_rock_context",
        "climate_id": "arid_hot",
        "features": {
            "pos": np.array([[37.3, -113.0, 0.9]]),
            "neg": np.empty((0, 3)),
            "pos_labels": ["bare rock"],
            "neg_labels": [],
        },
        "coord": (37.3, -113.0),
        "terrain": 40.0,
        "colour": 50.0,
        "expect_soft_min": 40.0,
        "expect_land_min": 70.0,
    },
    {
        "name": "mediterranean_scrub_context",
        "climate_id": "mediterranean",
        "features": {
            "pos": np.array([[41.9, 12.5, 0.8]]),
            "neg": np.empty((0, 3)),
            "pos_labels": ["scrub"],
            "neg_labels": [],
        },
        "coord": (41.9, 12.5),
        "terrain": 30.0,
        "colour": 55.0,
        "expect_soft_min": 35.0,
        "expect_land_min": 55.0,
    },
    {
        "name": "savannah_grassland_context",
        "climate_id": "tropical_monsoon_savanna",
        "features": {
            "pos": np.array([[-1.3, 36.8, 0.75]]),
            "neg": np.empty((0, 3)),
            "pos_labels": ["grassland"],
            "neg_labels": [],
        },
        "coord": (-1.3, 36.8),
        "terrain": 25.0,
        "colour": 48.0,
        "expect_soft_min": 30.0,
        "expect_land_min": 55.0,
    },
    {
        "name": "polar_glacier_context",
        "climate_id": "tundra_polar",
        "features": {
            "pos": np.array([[80.0, 0.0, 1.0]]),
            "neg": np.empty((0, 3)),
            "pos_labels": ["a glacier"],
            "neg_labels": [],
        },
        "coord": (80.0, 0.0),
        "terrain": 22.0,
        "colour": 45.0,
        "expect_soft_min": 48.0,
    },
    {
        "name": "alpine_scrub_context",
        "climate_id": "alpine",
        "features": {
            "pos": np.array([[46.01, 8.01, 0.8]]),
            "neg": np.empty((0, 3)),
            "pos_labels": ["scrub"],
            "neg_labels": [],
        },
        "coord": (46.0, 8.0),
        "terrain": 35.0,
        "colour": 58.0,
        "expect_soft_min": 35.0,
    },
]


def run_colour_suite() -> list[dict]:
    out = []
    for fx in COLOUR_FIXTURES:
        scored = analyse_image(_solid(fx["rgb"]), climate=fx["climate"])
        passed = True
        if "expect_min" in fx:
            passed = scored.score >= fx["expect_min"]
        if "expect_max" in fx:
            passed = passed and scored.score <= fx["expect_max"]
        delta = None
        if "compare_above" in fx:
            above = analyse_image(_solid(fx["compare_above"]["rgb"]), climate=fx["climate"])
            delta = above.score - scored.score
            passed = passed and delta >= fx["compare_above"]["min_delta"]
        out.append({
            "kind": "colour",
            "name": fx["name"],
            "climate": fx["climate"].id,
            "score": round(scored.score, 1),
            "green_frac": round(scored.green_frac, 3),
            "blue_frac": round(scored.blue_frac, 3),
            "grey_frac": round(scored.grey_frac, 3),
            "scenic_minus_urban_delta": round(delta, 1) if delta is not None else None,
            "passed": passed,
        })
    return out


def run_landcover_suite() -> list[dict]:
    out = []
    for fx in LANDCOVER_FIXTURES:
        detail = landcover_details([fx["coord"]], fx["features"], climate_ids=[fx["climate_id"]])[0]
        soft = soften_terrain(
            fx["terrain"],
            colour=fx["colour"],
            landcover=detail["score"],
            near_water=False,
            climate_id=fx["climate_id"],
        )
        passed = soft >= fx["expect_soft_min"]
        if "expect_land_min" in fx:
            passed = passed and detail["score"] >= fx["expect_land_min"]
        out.append({
            "kind": "landcover",
            "name": fx["name"],
            "climate": fx["climate_id"],
            "landcover_score": round(detail["score"], 1),
            "pos_label": detail["pos_label"],
            "soft_terrain": round(soft, 1),
            "passed": passed,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = ap.parse_args()

    rows = run_colour_suite() + run_landcover_suite()
    passed = sum(1 for row in rows if row["passed"])

    if args.json:
        print(json.dumps({"rows": rows, "passed": passed, "total": len(rows)}, indent=2))
        return 0 if passed == len(rows) else 1

    print("Biome calibration harness")
    print("=========================")
    for row in rows:
        if row["kind"] == "colour":
            extra = ""
            if row.get("scenic_minus_urban_delta") is not None:
                extra = f" (scenic-urban d={row['scenic_minus_urban_delta']})"
            print(
                f"[{ 'OK' if row['passed'] else 'FAIL' }] "
                f"{row['name']} ({row['climate']}) -> {row['score']}{extra}"
            )
        else:
            print(
                f"[{ 'OK' if row['passed'] else 'FAIL' }] "
                f"{row['name']} ({row['climate']}) -> land {row['landcover_score']}, "
                f"soft terrain {row['soft_terrain']}"
            )
    print(f"\nPassed {passed}/{len(rows)} fixtures")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
