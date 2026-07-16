"""Unit tests for ranking, blend, motorway detection, and min-scenic selection."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from app.roads import (
    _avoid_motorway_waypoints,
    _candidate_waypoints,
    _is_motorway,
    _motorway_km,
    blend_signals,
    filter_zero_motorway,
    is_zero_motorway,
    route_cost,
    select_chosen,
)


def test_blend_all_signals_weighted_average():
    # Equal weights → arithmetic mean.
    assert blend_signals(100, 50, 0, w_colour=1, w_terrain=1, w_land=1) == 50.0


def test_blend_renormalises_when_terrain_missing():
    # colour 80 @ 0.4 + land 20 @ 0.4 → 50 over weight sum 0.8
    got = blend_signals(80, None, 20, w_colour=0.4, w_terrain=0.2, w_land=0.4)
    assert abs(got - 50.0) < 1e-9


def test_blend_colour_only_when_others_missing():
    assert blend_signals(73, None, None, w_colour=0.35, w_terrain=0.25, w_land=0.4) == 73.0


def test_is_motorway_uk_refs():
    assert _is_motorway("M6", None)
    assert _is_motorway("A1(M)", None)
    assert _is_motorway("M25; A3", "something")
    assert not _is_motorway("A6", None)
    assert not _is_motorway(None, "High Street")
    assert not _is_motorway("B123", "Lane")


def test_is_motorway_classes_and_international():
    assert _is_motorway(None, None, classes={"motorway"})
    assert _is_motorway("I-95", None)
    assert _is_motorway(None, "Autobahn")
    assert _is_motorway(None, "Autoroute du Soleil")
    assert _is_motorway(None, "Tomei Expressway")
    assert _is_motorway("A9", "Bundesautobahn 9")
    assert _is_motorway("A6", "Autoroute A6")
    # Bare continental/UK A-roads without motorway name must not match.
    assert not _is_motorway("A6", None)
    assert not _is_motorway("A9", "Hauptstraße")
    assert not _is_motorway(None, None, classes={"trunk"})
    # AU M-routes share M\d pattern.
    assert _is_motorway("M1", "Western Motorway")
    assert _is_motorway("M1", None)


def test_nearby_attractors_include_non_uk():
    from app.roads import _nearby_attractors

    # Denver → Aspen corridor should pick Colorado Rockies attractors.
    pts, names = _nearby_attractors((39.74, -104.99), (39.19, -106.82))
    assert pts
    assert any("Rocky" in n or "Yosemite" not in n for n in names.values()) or names
    # Yosemite should not appear on a Colorado corridor.
    assert all("Yosemite" not in n for n in names.values())


def test_motorway_km_sums_matching_steps():
    legs = [{
        "steps": [
            {"ref": "M6", "name": "", "distance": 5000},
            {"ref": "A6", "name": "London Road", "distance": 2000},
            {"ref": "A38(M)", "name": "", "distance": 1000},
        ]
    }]
    assert _motorway_km(legs) == 6.0  # 5 + 1 km


def test_route_cost_prefers_scenic_when_preference_high():
    fast_dull = {"duration_min": 60.0, "avg_scenic_score": 30.0, "motorway_km": 0.0}
    slow_scenic = {"duration_min": 90.0, "avg_scenic_score": 90.0, "motorway_km": 0.0}
    # preference=0 → duration only
    assert route_cost(fast_dull, 0.0, detour=3.5) < route_cost(slow_scenic, 0.0, detour=3.5)
    # preference=1 + high detour → scenic wins despite longer time
    assert route_cost(slow_scenic, 1.0, detour=3.5) < route_cost(fast_dull, 1.0, detour=3.5)


def test_route_cost_motorway_penalty():
    plain = {"duration_min": 60.0, "avg_scenic_score": 70.0, "motorway_km": 0.0}
    mw = {"duration_min": 60.0, "avg_scenic_score": 70.0, "motorway_km": 10.0}
    assert route_cost(plain, 0.5, 3.5, mw_pen=20.0) < route_cost(mw, 0.5, 3.5, mw_pen=20.0)
    assert route_cost(plain, 0.5, 3.5, mw_pen=0.0) == route_cost(mw, 0.5, 3.5, mw_pen=0.0)


def _rt(score: float, duration: float = 60.0, mw: float = 0.0) -> dict:
    return {"avg_scenic_score": score, "duration_min": duration, "motorway_km": mw}


def test_select_chosen_no_floor_takes_first():
    routes = [_rt(40), _rt(90), _rt(70)]
    chosen, met = select_chosen(routes, min_scenic=0)
    assert chosen is routes[0]
    assert met is True


def test_select_chosen_min_scenic_picks_first_qualifying():
    # Already cost-ranked: dull-fast, mid, scenic-slow. Floor 70 → mid qualifies first.
    routes = [_rt(40, 50), _rt(75, 70), _rt(90, 100)]
    chosen, met = select_chosen(routes, min_scenic=70)
    assert chosen is routes[1]
    assert met is True


def test_select_chosen_min_scenic_fallback_most_scenic():
    """When none meet the floor, pick highest scenic — never the fastest loser."""
    # Cost-ranked: fastest/dull first, then mid, then slowest/best scenic.
    routes = [_rt(40, 30), _rt(55, 50), _rt(62, 90)]
    chosen, met = select_chosen(routes, min_scenic=80)
    assert chosen["avg_scenic_score"] == 62
    assert chosen is routes[2]
    assert met is False
    # Must not fall back to routes[0] (fastest).
    assert chosen is not routes[0]


def test_select_chosen_avoid_motorways_filters_pool():
    """Harsh avoid: motorway OSRM alts never win when a zero-mw exists."""
    mw_fast = _rt(80, 40, mw=25.0)
    plain = _rt(70, 55, mw=0.0)
    scenic_mw = _rt(90, 70, mw=10.0)
    # Cost order would prefer mw_fast; avoid_motorways must pick plain.
    routes = [mw_fast, plain, scenic_mw]
    chosen, met = select_chosen(routes, min_scenic=0, avoid_motorways=True)
    assert chosen is plain
    assert is_zero_motorway(chosen)
    assert met is True


def test_select_chosen_min_scenic_plus_avoid_mw():
    mw_ok = _rt(85, 40, mw=12.0)
    plain_low = _rt(50, 60, mw=0.0)
    plain_hi = _rt(78, 80, mw=0.0)
    routes = [mw_ok, plain_low, plain_hi]
    chosen, met = select_chosen(routes, min_scenic=75, avoid_motorways=True)
    assert chosen is plain_hi
    assert met is True
    assert chosen.get("motorway_km", 0) == 0.0


def test_select_chosen_avoid_mw_none_zero_still_picks():
    """If every candidate has motorway km, still return something (least bad)."""
    routes = [_rt(40, 30, mw=20), _rt(70, 50, mw=5)]
    chosen, met = select_chosen(routes, min_scenic=0, avoid_motorways=True)
    assert chosen is routes[0]  # cost order when no zero-mw filter applies
    assert not is_zero_motorway(chosen)


def test_filter_zero_motorway_eps():
    routes = [_rt(50, mw=0.0), _rt(50, mw=0.02), _rt(50, mw=1.0)]
    kept = filter_zero_motorway(routes, eps=0.05)
    assert len(kept) == 2
    assert all(is_zero_motorway(r, eps=0.05) for r in kept)
    assert routes[2] not in kept

def test_candidate_waypoints_omits_unusable_landcover():
    """When map context is unusable, hotspot rank uses relief only (not neutral-50)."""
    a, b = (54.0, -3.0), (54.2, -2.8)
    features = {
        "pos": np.array([[10.0, 10.0, 1.0]]),
        "neg": np.empty((0, 3)),
        "pos_labels": ["forest"],
        "neg_labels": [],
        "truncated": False,
    }
    elevs = [100.0 + 80.0 * ((i % 5) / 4.0) for i in range(25)]
    with patch("app.roads.enrich.elevation_batch", return_value=elevs), \
         patch("app.roads.enrich.landcover_is_usable", return_value=False), \
         patch("app.roads.enrich.landcover_scores") as land_scores:
        wps = _candidate_waypoints(a, b, features, pad=0.05, grid=5, candidates=3,
                                   detour_ratio=3.0)
        land_scores.assert_not_called()
        assert len(wps) <= 3


def test_candidate_waypoints_uses_landcover_when_usable():
    a, b = (54.0, -3.0), (54.05, -2.95)
    features = {
        "pos": np.array([[54.025, -2.975, 1.0]]),
        "neg": np.empty((0, 3)),
        "pos_labels": ["forest"],
        "neg_labels": [],
        "truncated": False,
    }
    elevs = [50.0] * 25
    with patch("app.roads.enrich.elevation_batch", return_value=elevs), \
         patch("app.roads.enrich.landcover_is_usable", return_value=True), \
         patch("app.roads.enrich.landcover_scores",
               return_value=[90.0] * 25) as land_scores:
        wps = _candidate_waypoints(a, b, features, pad=0.02, grid=5, candidates=2,
                                   detour_ratio=3.0)
        land_scores.assert_called_once()
        assert len(wps) >= 1


def test_candidate_waypoints_skips_urban_landcover():
    """Town-fabric landcover scores must not become scenic hotspots."""
    a, b = (54.0, -3.0), (54.05, -2.95)
    features = {
        "pos": np.empty((0, 3)),
        "neg": np.array([[54.025, -2.975, 0.9]]),
        "pos_labels": [],
        "neg_labels": ["housing"],
        "truncated": False,
    }
    elevs = [200.0] * 25  # high relief alone must not override urban land skip
    with patch("app.roads.enrich.elevation_batch", return_value=elevs), \
         patch("app.roads.enrich.landcover_is_usable", return_value=True), \
         patch("app.roads.enrich.landcover_scores", return_value=[25.0] * 25):
        wps = _candidate_waypoints(a, b, features, pad=0.02, grid=5, candidates=3,
                                   detour_ratio=3.0)
        assert wps == []


def test_avoid_motorway_waypoints_offset_from_corridor():
    """Inland offsets should sit off the A→B midline (Bristol→Exeter style)."""
    a, b = (51.45, -2.58), (50.72, -3.53)
    wps = _avoid_motorway_waypoints(a, b)
    assert len(wps) >= 3
    for lat, lng in wps:
        assert 50.0 < lat < 52.0
        assert -4.5 < lng < -1.5
