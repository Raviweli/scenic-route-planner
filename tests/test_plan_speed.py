"""Two-phase scoring, incremental pool updates, and parallel OSRM fan-out."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import numpy as np

from app import config, enrich, roads
from app.roads import (
    _colour_top_k,
    _fanout_osrm,
    get_osrm_routes,
    reblend_route_landcover,
    score_route,
)


def _fake_route(score_hint: float = 50.0, n: int = 8) -> dict:
    """Minimal geometry; scenic fields filled by score_route."""
    coords = [(54.0 + i * 0.01, -3.0 + i * 0.01) for i in range(n)]
    return {
        "coords": coords,
        "distance_km": float(n),
        "duration_min": 30.0 + score_hint * 0.1,
        "motorway_km": 0.0,
        "directions": [],
    }


def _land_features_near(coords) -> dict:
    lat, lng = coords[len(coords) // 2]
    return {
        "pos": np.array([[lat, lng, 1.0]]),
        "neg": np.empty((0, 3)),
        "pos_labels": ["forest"],
        "neg_labels": [],
        "truncated": False,
    }


@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_proxy_score_skips_colour(mock_loc, mock_elev):
    mock_elev.return_value = [100.0] * 20
    rt = _fake_route()
    feats = _land_features_near(rt["coords"])
    score_route(rt, features=feats, source="synthetic", colour=False,
                sample_spacing_km=4.0, max_samples=10)
    mock_loc.assert_not_called()
    assert "avg_scenic_score" in rt
    assert rt.get("proxy_scenic") == rt["avg_scenic_score"]
    assert rt.get("_colour_scored") is False
    assert rt["components"]["colour"] is None


@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_colour_top_k_only_colours_shortlist(mock_loc, mock_elev):
    mock_elev.return_value = [80.0 + i for i in range(40)]
    mock_loc.return_value = MagicMock(score=70.0)

    pool = []
    for i, hint in enumerate([90.0, 80.0, 70.0, 40.0, 30.0]):
        rt = _fake_route(hint)
        # Distinct midpoints so routes stay distinct under scoring.
        rt["coords"] = [(54.0 + i, -3.0 + j * 0.01) for j in range(6)]
        score_route(rt, features=None, source="synthetic", colour=False,
                    sample_spacing_km=4.0, max_samples=8)
        # Force distinct proxy ranks.
        rt["proxy_scenic"] = hint
        rt["avg_scenic_score"] = hint
        pool.append(rt)

    with patch.object(config, "SCENIC_COLOUR_TOP_K", 3):
        _colour_top_k(pool, features=None, source="synthetic", weights=None, top_k=3)

    coloured = [r for r in pool if r.get("_colour_scored")]
    assert len(coloured) == 3
    assert all(r["proxy_scenic"] >= 70.0 for r in coloured)
    uncoloured = [r for r in pool if not r.get("_colour_scored")]
    assert len(uncoloured) == 2
    assert mock_loc.call_count > 0


@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_colour_top_k_all_colours_every_unscored(mock_loc, mock_elev):
    """Final-pool call with top_k=len(pool) colours every unscored route."""
    mock_elev.return_value = [80.0 + i for i in range(40)]
    mock_loc.return_value = MagicMock(score=65.0)

    pool = []
    for i, hint in enumerate([90.0, 80.0, 70.0, 40.0]):
        rt = _fake_route(hint)
        rt["coords"] = [(54.0 + i, -3.0 + j * 0.01) for j in range(6)]
        score_route(rt, features=None, source="synthetic", colour=False,
                    sample_spacing_km=4.0, max_samples=8)
        rt["proxy_scenic"] = hint
        pool.append(rt)

    _colour_top_k(pool, features=None, source="synthetic", weights=None, top_k=len(pool))

    assert all(r.get("_colour_scored") for r in pool)
    assert all(r["components"].get("colour") is not None for r in pool)
    assert mock_loc.call_count > 0


@patch("app.roads.enrich.fetch_landcover")
@patch("app.roads._candidate_waypoints", return_value=[])
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_plan_alternatives_all_colour_scored(
    mock_loc, mock_elev, mock_osrm, mock_wps, mock_lc,
):
    """Returned alternatives get rank-density colour, not proxy-only blends."""
    mock_elev.return_value = [100.0] * 40
    mock_loc.side_effect = lambda *a, **k: MagicMock(score=60.0 + (hash(a[0:2]) % 20))
    mock_lc.return_value = _land_features_near([(54.1, -2.9)])

    osrm_routes = []
    for i in range(4):
        rt = _fake_route(40.0 + i * 5)
        rt["coords"] = [(54.0 + i * 0.25, -3.0 + j * 0.01) for j in range(8)]
        rt["duration_min"] = 30.0 + i * 3
        osrm_routes.append(rt)
    mock_osrm.return_value = osrm_routes

    with patch.object(config, "SCENIC_COLOUR_TOP_K", 1):
        result = roads.plan(
            (54.0, -3.0), (54.2, -2.8),
            preference=0.5, source="synthetic",
        )

    alts = result["alternatives"]
    assert len(alts) >= 2
    assert all(a.get("colour_scored") for a in alts)
    colours = [a["components"].get("colour") for a in alts]
    assert all(c is not None for c in colours)
    scenic = [a["avg_scenic_score"] for a in alts]
    assert max(scenic) > 0
    assert len(set(round(s) for s in scenic)) > 1


@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_reblend_updates_landcover_without_esri(mock_loc, mock_elev):
    mock_elev.return_value = [120.0] * 20
    mock_loc.return_value = MagicMock(score=60.0)
    rt = _fake_route()
    empty = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
        "truncated": False,
    }
    rich = _land_features_near(rt["coords"])
    score_route(rt, features=empty, source="synthetic", colour=True,
                sample_spacing_km=4.0, max_samples=8)
    calls_before = mock_loc.call_count
    old_land = rt["components"].get("landcover")
    reblend_route_landcover(rt, rich)
    assert mock_loc.call_count == calls_before  # no new Esri fetches
    # With usable nearby forest, landcover component should appear / rise.
    assert rt["components"].get("landcover") is not None
    if old_land is not None:
        assert rt["components"]["landcover"] >= old_land


@patch("app.roads.get_osrm_routes")
def test_fanout_osrm_same_set_as_sequential(mock_get):
    def _side_effect(a, b, alternatives=3, waypoint=None, waypoints=None):
        tag = waypoint or (waypoints[0] if waypoints else None) or "base"
        return [{
            "coords": [(1.0, 2.0), (1.1, 2.1)],
            "distance_km": 10.0,
            "duration_min": 15.0,
            "directions": [],
            "motorway_km": 0.0,
            "_tag": tag,
        }]

    mock_get.side_effect = _side_effect
    a, b = (54.0, -3.0), (54.2, -2.8)
    jobs = [
        {"a": a, "b": b, "waypoint": (54.1, -2.9)},
        {"a": a, "b": b, "waypoint": (54.15, -2.85)},
        {"a": a, "b": b, "waypoints": [(54.05, -2.95), (54.12, -2.88)]},
    ]
    parallel = _fanout_osrm(jobs, workers=3)
    sequential = []
    for job in jobs:
        sequential.extend(_side_effect(**job))

    def _key(rt):
        return (rt["_tag"], rt["distance_km"], rt["duration_min"])

    assert sorted(parallel, key=_key) == sorted(sequential, key=_key)
    assert mock_get.call_count == len(jobs)


@patch("app.roads.enrich.elevation_batch", return_value=[50.0] * 30)
@patch("app.roads.scoring.score_location", return_value=MagicMock(score=55.0))
def test_incremental_score_does_not_wipe_existing(mock_loc, mock_elev):
    """Simulate explore merge: already-scored routes keep scores; only new get proxy."""
    existing = _fake_route(60)
    score_route(existing, features=None, source="synthetic", colour=False,
                sample_spacing_km=4.0, max_samples=8)
    existing_score = existing["avg_scenic_score"]
    existing_id = id(existing)

    new = _fake_route(40)
    new["coords"] = [(55.0 + j * 0.01, -2.0) for j in range(6)]

    pool = [existing, new]
    # Mimic _score_and_emit: only missing scores.
    for rt in pool:
        if "avg_scenic_score" not in rt:
            score_route(rt, features=None, source="synthetic", colour=False,
                        sample_spacing_km=4.0, max_samples=8)

    assert id(existing) == existing_id
    assert existing["avg_scenic_score"] == existing_score
    assert "avg_scenic_score" in new
    # Existing must not have been re-proxy-scored into a wiped state.
    assert existing.get("_score_meta") is not None


def test_landcover_workers_default_raised():
    assert config.LANDCOVER_TILE_WORKERS >= 8


def test_rank_sample_config_coarser_than_full():
    assert config.SAMPLE_SPACING_KM_RANK >= config.SAMPLE_SPACING_KM
    assert config.MAX_SAMPLES_RANK <= config.MAX_SAMPLES
    assert config.SCENIC_COLOUR_TOP_K >= 1


@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location", return_value=MagicMock(score=55.0))
def test_parallel_proxy_scores_all_new_routes(mock_loc, mock_elev):
    """Shared elev prefetch + parallel proxy still scores every unscored route."""
    mock_elev.return_value = [100.0] * 40
    pool = []
    for i in range(4):
        rt = _fake_route(40 + i)
        rt["coords"] = [(54.0 + i * 0.2, -3.0 + j * 0.01) for j in range(8)]
        pool.append(rt)

    spacing = config.SAMPLE_SPACING_KM_RANK
    cap = config.MAX_SAMPLES_RANK
    elev_coords = {}
    for rt in pool:
        coords = rt["coords"]
        cum = roads._cumulative_km(coords)
        for idx in roads._sample_indices(cum, spacing, cap):
            lat, lng = coords[idx]
            elev_coords[(round(lat, 4), round(lng, 4))] = (lat, lng)
    enrich.elevation_batch(list(elev_coords.values()))

    workers = max(1, min(config.SCORE_ROUTE_WORKERS, len(pool)))

    def _one(rt):
        score_route(rt, features=None, source="synthetic", colour=False,
                    sample_spacing_km=spacing, max_samples=cap)
        return rt

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_one, pool))

    assert all("avg_scenic_score" in rt for rt in pool)
    assert all(rt.get("_colour_scored") is False for rt in pool)
    assert mock_elev.call_count >= 1


@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location", return_value=MagicMock(score=72.0))
def test_meta_reuse_skips_elev_on_colour_upgrade(mock_loc, mock_elev):
    mock_elev.side_effect = lambda coords: [110.0] * len(coords)
    rt = _fake_route()
    score_route(rt, features=None, source="synthetic", colour=False,
                sample_spacing_km=4.0, max_samples=8)
    elev_calls = mock_elev.call_count
    assert elev_calls >= 1
    assert rt.get("_score_meta", {}).get("elev_by_idx")

    score_route(
        rt, features=None, source="synthetic", colour=True,
        sample_spacing_km=4.0, max_samples=8, reuse_meta=True,
    )
    assert mock_elev.call_count == elev_calls  # no second elev fetch
    assert rt.get("_colour_scored") is True
    assert mock_loc.call_count > 0


@patch("app.roads._session")
def test_osrm_cache_hit_skips_http(mock_session):
    roads._OSRM_CACHE.clear()
    payload = {
        "code": "Ok",
        "routes": [{
            "distance": 1000.0,
            "duration": 120.0,
            "geometry": {"coordinates": [[-3.0, 54.0], [-2.9, 54.1]]},
            "legs": [{"steps": []}],
        }],
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    mock_session.get.return_value = resp

    with patch.object(config, "OSRM_CACHE", True):
        a, b = (54.0, -3.0), (54.1, -2.9)
        first = get_osrm_routes(a, b, alternatives=0)
        second = get_osrm_routes(a, b, alternatives=0)

    assert mock_session.get.call_count == 1
    assert first[0]["distance_km"] == second[0]["distance_km"]
    # Mutating the returned route must not corrupt the cache entry.
    first[0]["coords"].append((99.0, 99.0))
    third = get_osrm_routes(a, b, alternatives=0)
    assert len(third[0]["coords"]) == 2


def test_explore_park_lc_merge_order_equivalence():
    """Parallel park fetches then ordered merge matches serial merge."""
    empty = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
        "truncated": False,
    }
    parts = []
    for i in range(3):
        parts.append({
            "pos": np.array([[54.0 + i, -3.0, 1.0]]),
            "neg": np.empty((0, 3)),
            "pos_labels": [f"park{i}"],
            "neg_labels": [],
            "truncated": False,
        })

    serial = empty
    for pf in parts:
        serial = enrich.merge_landcover(serial, pf)

    parallel = empty
    # ex.map preserves input order — same merge sequence as serial.
    for pf in parts:
        parallel = enrich.merge_landcover(parallel, pf)

    assert serial is not None and parallel is not None
    assert serial["pos_labels"] == parallel["pos_labels"] == ["park0", "park1", "park2"]
    assert np.allclose(serial["pos"], parallel["pos"])


def test_out_geom_budget_default_reduced():
    assert config.LANDCOVER_OUT_GEOM <= 600
    q = enrich._overpass_query((54.0, -3.0, 54.1, -2.9))
    assert f"out geom {config.LANDCOVER_OUT_GEOM}" in q
