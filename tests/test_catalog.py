"""Catalogue integrity checks."""
from __future__ import annotations

from app.catalog_data import (
    FEATURED_PRESET_IDS,
    PRESETS,
    PROFILES,
    WORLD_REGION_DEFINITIONS,
    build_regions,
    featured_presets,
    get_profile,
)


def test_balanced_profile_exists():
    p = get_profile("balanced")
    assert p is not None
    assert set(p["weights"]) == {"colour", "terrain", "landcover"}


def test_ui_style_profile_ids_resolve():
    for pid in (
        "balanced",
        "coastal-moderate-landcover",
        "mountain-moderate-terrain",
        "waterside-moderate-colour",
        "woodland-moderate-landcover",
        "pastoral-moderate-landcover",
    ):
        assert get_profile(pid) is not None, pid


def test_profile_names_use_middle_dot_not_question_mark():
    broken = [p for p in PROFILES if " ? " in p["name"]]
    assert broken == [], f"corrupted names: {[p['id'] for p in broken[:5]]}"
    # Generated variants should use · separators.
    sample = get_profile("mountain-moderate-colour")
    assert sample is not None
    assert "·" in sample["name"]
    assert "?" not in sample["name"]


def test_world_regions_and_featured_presets():
    uk = build_regions("uk")
    world = build_regions("world")
    all_regs = build_regions("all")
    assert len(uk) >= 5
    assert len(world) == len(WORLD_REGION_DEFINITIONS)
    assert len(all_regs) == len(uk) + len(world)
    assert all(r.get("scope") == "world" for r in world)
    assert any(r["id"] == "colorado-rockies" for r in world)
    featured = featured_presets()
    assert any(p["id"] == "denver-to-aspen" for p in featured)
    assert any(p["id"] == "highland-to-skye" for p in featured)
    for fid in FEATURED_PRESET_IDS:
        assert any(p["id"] == fid for p in PRESETS), fid


def test_attractor_registry_has_world_packs():
    from app.attractors import ATTRACTOR_PACKS, all_attractors

    assert "uk_national_parks" in ATTRACTOR_PACKS
    assert "us_iconic" in ATTRACTOR_PACKS
    assert "alps_dolomites" in ATTRACTOR_PACKS
    attractors = all_attractors()
    assert 150 <= len(attractors) <= 250
    names = {n for n, _, _ in attractors}
    assert "Yosemite" in names
    assert "Lake District" in names
    assert "Banff" in names
    assert "Torres del Paine" in names

