"""Plan wall-clock budget: early exit, landcover deadline, HT gating."""
from __future__ import annotations

import time
from unittest.mock import patch

import numpy as np

from app import config, enrich, roads


def _empty_feats(**extra):
    out = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
        "truncated": False,
        "landcover_incomplete": False,
    }
    out.update(extra)
    return out


def _route(score: float = 40.0, n: int = 6) -> dict:
    coords = [(54.0 + i * 0.01, -3.0 + i * 0.01) for i in range(n)]
    return {
        "coords": coords,
        "distance_km": float(n),
        "duration_min": 40.0,
        "motorway_km": 0.0,
        "directions": [],
        "avg_scenic_score": score,
        "proxy_scenic": score,
        "components": {"colour": None, "terrain": 30.0, "landcover": 40.0},
        "_landcover_usable": False,
        "_colour_scored": False,
        "num_samples": n,
        "render": [
            {"lat": la, "lng": lo, "score": score} for la, lo in coords
        ],
    }


def _ensure_scored(rt, **kwargs):
    rt.setdefault("avg_scenic_score", 45.0)
    rt.setdefault(
        "components",
        {"colour": 50.0, "terrain": 30.0, "landcover": None},
    )
    rt.setdefault("num_samples", len(rt.get("coords") or []))
    rt.setdefault(
        "render",
        [
            {"lat": la, "lng": lo, "score": rt["avg_scenic_score"]}
            for la, lo in (rt.get("coords") or [(54.0, -3.0)])
        ],
    )
    rt["_colour_scored"] = kwargs.get("colour", False)
    return rt


def test_budget_config_defaults():
    assert config.PLAN_BUDGET_SEC == 30
    assert config.PLAN_RESERVE_SEC == 5
    assert config.LANDCOVER_MAX_TILES == 24
    assert config.LANDCOVER_MAX_TILES_LONG == 36
    assert config.LANDCOVER_CELL_TIMEOUT_SEC == 18
    assert config.LANDCOVER_FETCH_RETRIES >= 2
    assert config.LANDCOVER_TAG_BATCH >= 6
    assert config.LANDCOVER_OUT_MODE in ("center", "geom")
    assert config.LANDCOVER_MAX_TILES < 40
    assert config.LANDCOVER_MAX_TILES_LONG < 80


def test_overpass_query_uses_cell_timeout():
    q = enrich._overpass_query((54.0, -3.0, 54.1, -2.9))
    assert f"[timeout:{int(round(config.LANDCOVER_CELL_TIMEOUT_SEC))}]" in q
    assert "out center" in q or "out geom" in q


def test_overpass_query_batches_are_smaller():
    batches = enrich._tag_batches()
    assert len(batches) >= 2
    assert max(len(b) for b in batches) <= config.LANDCOVER_TAG_BATCH
    q0 = enrich._overpass_query((54.0, -3.0, 54.6, -2.4), tags=batches[0])
    q_all = enrich._overpass_query((54.0, -3.0, 54.6, -2.4))
    assert len(q0) < len(q_all)


def test_fetch_landcover_deadline_stops_cold_cells():
    """Slow cold cells must stop under deadline; partial + truncated returned."""
    empty = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
    }
    fetched = []

    def _from_cache(cell):
        return None

    def _bbox(sub, endpoint=None, deadline=None):
        fetched.append(sub)
        time.sleep(0.15)
        return empty

    deadline = time.perf_counter() + 0.25
    with patch.object(enrich, "_cell_from_cache", side_effect=_from_cache), \
         patch.object(enrich, "_cell_to_cache"), \
         patch.object(enrich, "_fetch_landcover_bbox", side_effect=_bbox), \
         patch.object(config, "LANDCOVER_TILE_WORKERS", 2), \
         patch.object(config, "LANDCOVER_MAX_TILES", 20):
        result = enrich.fetch_landcover(
            (54.0, -3.0, 55.2, -1.8), prefer_axis=None, deadline=deadline,
        )

    assert result is not None
    assert result["truncated"] is True
    assert result.get("deadline_stopped") is True
    assert result["landcover_incomplete"] is True
    # Must not have waited out every cold cell at 0.15s each.
    assert len(fetched) < 12


@patch("app.roads.score_route")
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 40)
@patch("app.roads._candidate_waypoints", return_value=[])
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.fetch_landcover")
def test_plan_returns_under_budget_with_slow_landcover(
    mock_lc, mock_osrm, mock_wps, mock_elev, mock_score,
):
    """Simulated slow landcover + HT must still return a chosen route under budget."""
    base = _route(45.0)
    mock_osrm.return_value = [dict(base)]
    mock_lc.return_value = _empty_feats()
    mock_score.side_effect = _ensure_scored

    # Exhaust work budget almost immediately so explore/HT are gated.
    with patch.object(config, "PLAN_BUDGET_SEC", 0.05), \
         patch.object(config, "PLAN_RESERVE_SEC", 0.0), \
         patch.object(config, "PLAN_EXPLORE_MIN_SEC", 8), \
         patch.object(config, "PLAN_HT_ROUND_MIN_SEC", 6):
        t0 = time.perf_counter()
        result = roads.plan(
            (54.0, -3.0), (54.2, -2.8),
            preference=0.5, min_scenic=90.0, explore_all=True,
            source="synthetic",
        )
        elapsed = time.perf_counter() - t0

    assert "chosen" in result
    assert result["chosen"]["avg_scenic_score"] is not None
    assert result["budget_sec"] == 0.05
    assert "elapsed_ms" in result
    assert result["timings_ms"]["elapsed"] == result["elapsed_ms"]
    # With near-zero budget, explore and/or HT should be skipped.
    assert result["budget_exhausted"] is True
    assert elapsed < 5.0


@patch("app.roads.score_route")
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 40)
@patch("app.roads._fanout_osrm")
@patch("app.roads._candidate_waypoints")
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.fetch_landcover")
def test_hard_target_stops_mid_rounds(
    mock_lc, mock_osrm, mock_wps, mock_fanout, mock_elev, mock_score,
):
    """HT loop must break when remaining time falls below the round floor."""
    mock_osrm.return_value = [_route(30.0)]
    mock_lc.return_value = _empty_feats()
    mock_wps.return_value = [(54.1, -2.9)]

    round_n = {"n": 0}

    def _fanout(jobs, workers=None):
        round_n["n"] += 1
        # Slow enough that a tiny remaining budget is consumed mid-search.
        time.sleep(0.05)
        return [_route(35.0 + round_n["n"])]

    mock_fanout.side_effect = _fanout
    mock_score.side_effect = _ensure_scored

    round_events = []

    def progress(ev):
        if ev.get("type") in ("round", "phase"):
            round_events.append(ev)

    # Work deadline ≈ 0.08s after start; first HT round sleeps 0.05s then
    # subsequent rounds see _time_left() below PLAN_HT_ROUND_MIN_SEC.
    with patch.object(config, "PLAN_BUDGET_SEC", 0.12), \
         patch.object(config, "PLAN_RESERVE_SEC", 0.0), \
         patch.object(config, "PLAN_HT_ROUND_MIN_SEC", 0.05):
        result = roads.plan(
            (54.0, -3.0), (54.2, -2.8),
            preference=0.5, min_scenic=95.0,
            source="synthetic", progress=progress,
        )

    assert result["budget_exhausted"] is True
    assert "hard_target_stopped" in result["budget_reasons"]
    ht_rounds = [e for e in round_events if e.get("type") == "round"]
    assert 1 <= len(ht_rounds) < len(config.HARD_TARGET_ROUNDS)


@patch("app.roads.score_route")
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 20)
@patch("app.roads._nearby_attractors")
@patch("app.roads._fanout_osrm")
@patch("app.roads._candidate_waypoints", return_value=[])
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.fetch_landcover")
def test_explore_skipped_when_budget_low(
    mock_lc, mock_osrm, mock_wps, mock_fanout, mock_attr, mock_elev, mock_score,
):
    mock_osrm.return_value = [_route(50.0)]
    mock_lc.return_value = _empty_feats()
    mock_attr.return_value = ([(54.5, -3.2)], {(54.5, -3.2): "Lake District"})
    mock_fanout.return_value = []
    mock_score.side_effect = _ensure_scored

    with patch.object(config, "PLAN_BUDGET_SEC", 0.01), \
         patch.object(config, "PLAN_RESERVE_SEC", 0.0), \
         patch.object(config, "PLAN_EXPLORE_MIN_SEC", 8):
        result = roads.plan(
            (54.0, -3.0), (54.2, -2.8),
            preference=0.5, explore_all=True, source="synthetic",
        )

    assert result["budget_exhausted"] is True
    assert "explore_skipped" in result["budget_reasons"]
    assert result["signals"]["explore_parks"] is False
    mock_attr.assert_not_called()


@patch("app.roads.score_route")
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 20)
@patch("app.roads._nearby_attractors")
@patch("app.roads._fanout_osrm")
@patch("app.roads._candidate_waypoints", return_value=[])
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.fetch_landcover")
def test_explore_overlaps_park_lc_with_osrm(
    mock_lc, mock_osrm, mock_wps, mock_fanout, mock_attr, mock_elev, mock_score,
):
    """When explore runs, park LC and OSRM should be submitted in parallel."""
    mock_osrm.return_value = [_route(50.0)]
    mock_lc.return_value = _empty_feats()
    park = (54.47, -3.10)
    mock_attr.return_value = ([park], {park: "Lake District"})
    mock_fanout.return_value = [_route(70.0)]
    mock_score.side_effect = _ensure_scored

    # Large budget so explore is not skipped.
    with patch.object(config, "PLAN_BUDGET_SEC", 60), \
         patch.object(config, "PLAN_RESERVE_SEC", 5), \
         patch.object(config, "PLAN_EXPLORE_MIN_SEC", 8), \
         patch("app.roads.ThreadPoolExecutor") as mock_ex_cls:
        # Keep real executor behaviour for nested pools by using the real class
        # as a side effect, but record max_workers=2 explore overlap submit.
        from concurrent.futures import ThreadPoolExecutor as RealPool

        created = []

        def _factory(*args, **kwargs):
            ex = RealPool(*args, **kwargs)
            created.append(kwargs.get("max_workers") or (args[0] if args else None))
            return ex

        mock_ex_cls.side_effect = _factory
        result = roads.plan(
            (54.0, -3.0), (54.5, -2.8),
            preference=0.5, explore_all=True, source="synthetic",
        )

    assert result["signals"]["explore_parks"] is True
    assert result["budget_exhausted"] is False
    # Base LC∥OSRM and explore LC∥OSRM both use max_workers=2.
    assert 2 in created
    assert mock_fanout.called
    # fetch_landcover called with deadline= for explore park boxes too.
    assert any(
        "deadline" in (c.kwargs or {})
        for c in mock_lc.call_args_list
    )


@patch("app.roads.score_route")
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 40)
@patch("app.roads._candidate_waypoints", return_value=[])
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.fetch_landcover")
def test_plan_time_budget_off_skips_time_gates(
    mock_lc, mock_osrm, mock_wps, mock_elev, mock_score,
):
    """time_budget=False must not early-exit explore/HT for wall-clock."""
    base = _route(45.0)
    mock_osrm.return_value = [dict(base)]
    mock_lc.return_value = _empty_feats()
    mock_score.side_effect = _ensure_scored

    # Tiny PLAN_BUDGET would normally exhaust immediately; with time_budget off
    # explore/HT must still be attempted (no explore_skipped / hard_target_stopped).
    with patch.object(config, "PLAN_BUDGET_SEC", 0.01), \
         patch.object(config, "PLAN_RESERVE_SEC", 0.0), \
         patch.object(config, "PLAN_EXPLORE_MIN_SEC", 8), \
         patch.object(config, "PLAN_HT_ROUND_MIN_SEC", 6), \
         patch("app.roads._nearby_attractors", return_value=([], {})):
        result = roads.plan(
            (54.0, -3.0), (54.2, -2.8),
            preference=0.5, min_scenic=90.0, explore_all=True,
            time_budget=False, source="synthetic",
        )

    assert result["time_budget"] is False
    assert result["budget_sec"] is None
    assert "explore_skipped" not in result["budget_reasons"]
    assert "hard_target_stopped" not in result["budget_reasons"]
    # Landcover still receives deadline=None (no wall-clock gate).
    assert any(
        c.kwargs.get("deadline") is None
        for c in mock_lc.call_args_list
        if "deadline" in (c.kwargs or {})
    )


@patch("app.roads.score_route")
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 40)
@patch("app.roads._nearby_attractors", return_value=([], {}))
@patch("app.roads._fanout_osrm", return_value=[])
@patch("app.roads._candidate_waypoints", return_value=[])
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.fetch_landcover")
def test_plan_avoid_motorways_filters_chosen(
    mock_lc, mock_osrm, mock_wps, mock_fanout, mock_attr, mock_elev, mock_score,
):
    """Harsh avoid: chosen must be zero-motorway when such a candidate exists."""
    mw = _route(60.0, n=6)
    mw["motorway_km"] = 18.0
    mw["duration_min"] = 25.0
    mw["distance_km"] = 30.0
    plain = _route(55.0, n=10)
    # Distinct geometry so _dedupe does not collapse the pair.
    plain["coords"] = [(54.05 + i * 0.02, -3.05 + i * 0.015) for i in range(10)]
    plain["motorway_km"] = 0.0
    plain["duration_min"] = 45.0
    plain["distance_km"] = 42.0
    mock_osrm.return_value = [dict(mw), dict(plain)]
    mock_lc.return_value = _empty_feats()
    mock_score.side_effect = _ensure_scored

    with patch.object(config, "PLAN_BUDGET_SEC", 60):
        result = roads.plan(
            (54.0, -3.0), (54.2, -2.8),
            preference=0.0, avoid_motorways=True, source="synthetic",
        )

    assert result["avoid_motorways"] is True
    assert result["motorway_avoid_met"] is True
    assert result["chosen"]["motorway_km"] == 0.0
    assert all(a["motorway_km"] == 0.0 for a in result["alternatives"])


@patch("app.roads.score_route")
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 40)
@patch("app.roads._nearby_attractors", return_value=([], {}))
@patch("app.roads._fanout_osrm", return_value=[])
@patch("app.roads._candidate_waypoints", return_value=[])
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.fetch_landcover")
def test_plan_min_scenic_prefers_scenic_not_fastest_when_unmet(
    mock_lc, mock_osrm, mock_wps, mock_fanout, mock_attr, mock_elev, mock_score,
):
    """Unmet min_scenic must choose highest scenic, not the fastest dull route."""
    fast = _route(30.0, n=5)
    fast["duration_min"] = 20.0
    fast["distance_km"] = 20.0
    mid = _route(50.0, n=8)
    mid["coords"] = [(54.02 + i * 0.015, -3.02 + i * 0.012) for i in range(8)]
    mid["duration_min"] = 40.0
    mid["distance_km"] = 35.0
    scenic = _route(65.0, n=12)
    scenic["coords"] = [(54.04 + i * 0.018, -3.04 + i * 0.01) for i in range(12)]
    scenic["duration_min"] = 80.0
    scenic["distance_km"] = 55.0
    mock_osrm.return_value = [dict(fast), dict(mid), dict(scenic)]
    mock_lc.return_value = _empty_feats()

    def _score(rt, **kwargs):
        # Preserve pre-set avg_scenic_score from fixtures.
        return _ensure_scored(rt, **kwargs)

    mock_score.side_effect = _score

    with patch.object(config, "PLAN_BUDGET_SEC", 60), \
         patch.object(config, "HARD_TARGET_ROUNDS", []):
        result = roads.plan(
            (54.0, -3.0), (54.2, -2.8),
            preference=0.0, min_scenic=75.0, source="synthetic",
        )

    assert result["min_scenic_met"] is False
    assert result["chosen"]["avg_scenic_score"] == 65.0
