"""Offline scenic colour scoring tests (synthetic tiles, no network)."""
from __future__ import annotations

from PIL import Image

from app.scoring import analyse_image, score_location


def _solid(rgb: tuple[int, int, int], size: int = 64) -> Image.Image:
    return Image.new("RGB", (size, size), rgb)


def test_green_tile_scores_higher_than_grey():
    green = analyse_image(_solid((40, 120, 50)), source="test")
    grey = analyse_image(_solid((130, 130, 130)), source="test")
    assert green.score > grey.score
    assert green.green_frac > grey.green_frac
    assert grey.grey_frac > green.grey_frac


def test_blue_water_registers_as_blue():
    water = analyse_image(_solid((40, 90, 160)), source="test")
    assert water.blue_frac > 0.5
    assert water.score >= 50


def test_score_clamped_0_100():
    rural = score_location(54.5, -3.2, source="synthetic")
    urban = score_location(51.5074, -0.1278, source="synthetic")
    assert 0.0 <= rural.score <= 100.0
    assert 0.0 <= urban.score <= 100.0
    assert rural.source in ("synthetic", "cache", "synthetic-fallback")


def test_synthetic_rural_beats_london():
    # Far from synthetic urban centres should score greener than central London.
    rural = score_location(54.5, -3.2, source="synthetic")
    london = score_location(51.5074, -0.1278, source="synthetic")
    assert rural.score >= london.score
