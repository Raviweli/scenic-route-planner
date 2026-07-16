"""Draw route mode: POST /api/route/draw and score_drawn_route."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.roads import (
    _chord_via_points,
    _densify_for_match,
    _leg_corridor_ok,
    _leg_length_ratio,
    _max_chord_deviation_km,
    _prune_out_and_back_spurs,
    _route_one_chord_leg,
    get_osrm_match_route,
    score_drawn_route,
)

client = TestClient(app)


def _fake_osrm(a, b, **kwargs):
    vias = kwargs.get("waypoints") or (
        [kwargs["waypoint"]] if kwargs.get("waypoint") else []
    )
    coords = [a, *vias, b]
    # Stay near the chord so corridor checks pass in unit tests.
    dist = 0.0
    pts = coords
    for i in range(1, len(pts)):
        dist += abs(pts[i][0] - pts[i - 1][0]) * 111.0 + abs(
            pts[i][1] - pts[i - 1][1]
        ) * 70.0
    return [{
        "coords": coords,
        "distance_km": max(1.0, dist),
        "duration_min": 40.0,
        "motorway_km": 2.5,
        "directions": [{"text": "Head off", "distance_label": "1 km", "lat": a[0], "lng": a[1]}],
    }]


def _fake_match(trace, **kwargs):
    return {
        "coords": list(trace),
        "distance_km": 30.0,
        "duration_min": 45.0,
        "motorway_km": 1.0,
        "directions": [{"text": "Head off", "distance_label": "1 km", "lat": trace[0][0], "lng": trace[0][1]}],
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


@patch("app.roads.enrich.fetch_landcover")
@patch("app.roads.get_osrm_match_route", side_effect=_fake_match)
@patch("app.roads.get_osrm_routes", side_effect=_fake_osrm)
@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_draw_three_vertices_uses_pairwise(mock_loc, mock_elev, mock_osrm, mock_match, mock_lc):
    mock_elev.return_value = [120.0] * 40
    mock_loc.return_value = MagicMock(score=72.0)
    mock_lc.return_value = _land_features_near([(54.1, -2.9)])

    verts = [[54.0, -3.0], [54.1, -2.95], [54.2, -2.9]]
    result = score_drawn_route(verts, profile_id="balanced", source="synthetic")

    assert result["source"] == "drawn"
    assert result["draw_vertices"] == 3
    assert result["snap_to_roads"] is True
    assert result["road_snap_method"] == "pairwise"
    assert result["chosen"]["avg_scenic_score"] > 0
    assert result["chosen"]["colour_scored"] is True
    assert len(result["alternatives"]) == 1
    # One OSRM call per consecutive pair — never a single first→last multi-via.
    assert mock_osrm.call_count == 2
    for call in mock_osrm.call_args_list:
        assert call.kwargs.get("alternatives") == 0
    # Never one global first→last through all clicks.
    assert not any(
        c.args[0] == (54.0, -3.0) and c.args[1] == (54.2, -2.9)
        for c in mock_osrm.call_args_list
    )
    ends = [(c.args[0], c.args[1]) for c in mock_osrm.call_args_list]
    assert ends[0] == ((54.0, -3.0), (54.1, -2.95))
    assert ends[1] == ((54.1, -2.95), (54.2, -2.9))
    mock_match.assert_not_called()


@patch("app.roads.enrich.fetch_landcover")
@patch("app.roads.get_osrm_match_route", side_effect=_fake_match)
@patch("app.roads.get_osrm_routes", side_effect=_fake_osrm)
@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_draw_long_leg_tries_direct_before_chord_vias(
    mock_loc, mock_elev, mock_osrm, mock_match, mock_lc,
):
    mock_elev.return_value = [120.0] * 40
    mock_loc.return_value = MagicMock(score=70.0)
    mock_lc.return_value = None

    # ~22 km east-west chord — direct should pass corridor and win.
    verts = [[51.45, -2.80], [51.45, -2.50]]
    result = score_drawn_route(verts, profile_id="balanced", source="synthetic")

    assert result["road_snap_method"] == "pairwise"
    assert mock_osrm.call_count >= 1
    call = mock_osrm.call_args_list[0]
    assert not call.kwargs.get("waypoints")
    assert call.kwargs.get("alternatives") == 0
    mock_match.assert_not_called()


@patch("app.roads.enrich.fetch_landcover")
@patch("app.roads.get_osrm_match_route", side_effect=_fake_match)
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_draw_long_leg_retries_chord_vias_when_direct_off_corridor(
    mock_loc, mock_elev, mock_osrm, mock_match, mock_lc,
):
    mock_elev.return_value = [120.0] * 40
    mock_loc.return_value = MagicMock(score=70.0)
    mock_lc.return_value = None

    a, b = (51.45, -2.80), (51.45, -2.50)
    hooked = {
        "coords": [(51.45, -2.80), (51.52, -2.65), (51.45, -2.50)],
        "distance_km": 40.0,
        "duration_min": 50.0,
        "motorway_km": 0.0,
        "directions": [],
    }

    def osrm_side_effect(start, end, **kwargs):
        vias = kwargs.get("waypoints") or []
        if not vias:
            return [hooked]
        coords = [start, *vias, end]
        return [{
            "coords": coords,
            "distance_km": 22.0,
            "duration_min": 30.0,
            "motorway_km": 0.0,
            "directions": [],
        }]

    mock_osrm.side_effect = osrm_side_effect
    verts = [[51.45, -2.80], [51.45, -2.50]]
    result = score_drawn_route(verts, profile_id="balanced", source="synthetic")

    assert result["road_snap_method"] == "pairwise"
    assert mock_osrm.call_count >= 2
    assert not mock_osrm.call_args_list[0].kwargs.get("waypoints")
    assert mock_osrm.call_args_list[1].kwargs.get("waypoints")
    assert mock_osrm.call_args_list[1].kwargs.get("continue_straight") is True
    mock_match.assert_not_called()


@patch("app.roads.enrich.fetch_landcover")
@patch("app.roads.get_osrm_match_route", side_effect=_fake_match)
@patch("app.roads.get_osrm_routes", side_effect=RuntimeError("no roads"))
@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_draw_match_fallback_when_routing_fails(
    mock_loc, mock_elev, mock_osrm, mock_match, mock_lc,
):
    mock_elev.return_value = [120.0] * 40
    mock_loc.return_value = MagicMock(score=65.0)
    mock_lc.return_value = None

    verts = [[54.0, -3.0], [54.1, -2.95], [54.2, -2.9]]
    result = score_drawn_route(verts, profile_id="balanced", source="synthetic")

    assert result["road_snap_method"] == "match"
    mock_match.assert_called_once()
    # Match fallback densifies along the sketch polyline (straight chords).
    trace = mock_match.call_args[0][0]
    assert trace[0] == (54.0, -3.0)
    assert trace[-1] == (54.2, -2.9)
    assert len(trace) >= 3


@patch("app.roads.enrich.fetch_landcover")
@patch("app.roads.get_osrm_routes")
@patch("app.roads.enrich.elevation_batch")
@patch("app.roads.scoring.score_location")
def test_snap_false_skips_osrm(mock_loc, mock_elev, mock_osrm, mock_lc):
    mock_elev.return_value = [100.0] * 40
    mock_loc.return_value = MagicMock(score=55.0)
    mock_lc.return_value = None

    verts = [[54.0, -3.0], [54.05, -2.98], [54.1, -2.95]]
    result = score_drawn_route(
        verts, profile_id="balanced", snap_to_roads=False, source="synthetic",
    )

    mock_osrm.assert_not_called()
    assert result["snap_to_roads"] is False
    assert result["road_snap_method"] is None
    assert result["chosen"]["motorway_km"] == 0.0
    assert result["chosen"]["directions"]
    assert "Drawn route" in result["chosen"]["directions"][0]["text"]
    assert len(result["chosen"]["render"]) >= 2


def test_densify_for_match_long_segment():
    verts = [(54.0, -3.0), (54.0, -1.0)]  # ~130 km east at this latitude
    dense = _densify_for_match(verts, max_segment_km=2.0)
    assert dense[0] == verts[0]
    assert dense[-1] == verts[-1]
    assert len(dense) > 50


def test_chord_via_points_samples_between_clicks():
    a, b = (51.45, -2.80), (51.45, -2.50)
    vias = _chord_via_points(a, b, spacing_km=0.9, max_vias=16)
    assert vias
    assert vias[0] != a and vias[-1] != b
    # All vias lie on the straight chord (same lat here).
    for v in vias:
        assert abs(v[0] - 51.45) < 1e-9
        assert -2.80 < v[1] < -2.50
    # Sparse spacing → fewer vias than the old 0.4 km default.
    dense = _chord_via_points(a, b, spacing_km=0.4, max_vias=24)
    assert len(vias) < len(dense)


def test_leg_corridor_ok_rejects_length_and_deviation():
    a, b = (51.40, -2.76), (51.53, -2.33)
    chord_km = 35.0
    on_chord = {
        "coords": [a, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), b],
        "distance_km": chord_km * 1.2,
    }
    southern_arc = {
        "coords": [a, (51.35, -2.55), (51.36, -2.45), b],
        "distance_km": chord_km * 1.9,
    }
    assert _leg_corridor_ok(on_chord, a, b)
    assert not _leg_corridor_ok(southern_arc, a, b)
    assert _leg_length_ratio(southern_arc, a, b) > config.DRAW_LEG_MAX_LENGTH_RATIO


@patch("app.roads.get_osrm_routes")
def test_route_one_chord_leg_prefers_shorter_corridor_ok(mock_osrm):
    a, b = (51.40, -2.76), (51.53, -2.33)
    direct = {
        "coords": [a, b],
        "distance_km": 38.0,
        "duration_min": 40.0,
        "motorway_km": 0.0,
        "directions": [],
    }
    hooked = {
        "coords": [a, (51.35, -2.55), b],
        "distance_km": 55.0,
        "duration_min": 60.0,
        "motorway_km": 0.0,
        "directions": [],
    }

    def side_effect(start, end, **kwargs):
        vias = kwargs.get("waypoints") or []
        return [hooked if vias else direct]

    mock_osrm.side_effect = side_effect
    leg = _route_one_chord_leg(a, b)
    assert leg is direct
    assert mock_osrm.call_count == 1


def test_max_chord_deviation_detects_hook():
    a, b = (51.40, -2.70), (51.40, -2.50)
    on_chord = [(51.40, -2.70), (51.40, -2.60), (51.40, -2.50)]
    hooked = [(51.40, -2.70), (51.48, -2.60), (51.40, -2.50)]  # ~9 km north hook
    assert _max_chord_deviation_km(on_chord, a, b) < 0.2
    assert _max_chord_deviation_km(hooked, a, b) > 5.0


def test_prune_out_and_back_spurs_removes_v_spur():
    # Main corridor westbound with a north spur that returns to the same point.
    coords = [
        (51.45, -2.60),
        (51.45, -2.58),
        (51.45, -2.56),
        (51.48, -2.56),  # spur north
        (51.50, -2.56),
        (51.48, -2.56),
        (51.45, -2.56),  # return
        (51.45, -2.54),
        (51.45, -2.52),
    ]
    pruned = _prune_out_and_back_spurs(
        coords, close_m=50.0, min_spur_m=100.0, max_lookback=20,
    )
    assert len(pruned) < len(coords)
    # Spur tip should be gone; corridor continues west.
    assert (51.50, -2.56) not in pruned
    assert pruned[0] == coords[0]
    assert pruned[-1] == coords[-1]


@patch("app.roads._session")
def test_get_osrm_match_route_parses_geometry(mock_session):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "code": "Ok",
        "matchings": [{
            "distance": 8000.0,
            "duration": 600.0,
            "geometry": {
                "coordinates": [[-3.1, 54.4], [-3.05, 54.42], [-3.0, 54.45]],
            },
            "legs": [{
                "steps": [{
                    "distance": 8000,
                    "name": "A591",
                    "ref": "A591",
                    "maneuver": {"type": "depart", "location": [-3.1, 54.4]},
                }],
            }],
        }],
    }
    mock_session.get.return_value = resp

    trace = [(54.4, -3.1), (54.45, -3.0)]
    route = get_osrm_match_route(trace)
    assert route is not None
    assert route["coords"][0] == (54.4, -3.1)
    assert abs(route["distance_km"] - 8.0) < 1e-6
    params = mock_session.get.call_args.kwargs["params"]
    assert params["geometries"] == "geojson"
    assert params["overview"] == "full"
    assert params["steps"] == "true"
    assert "timestamps" in params
    assert "radiuses" in params
    # Tighter default radius for match fallback.
    assert "25" in params["radiuses"] or params["radiuses"].startswith("25")


@patch("app.roads.enrich.fetch_landcover", return_value=None)
@patch("app.roads.get_osrm_routes", side_effect=_fake_osrm)
@patch("app.roads.enrich.elevation_batch", return_value=[100.0] * 40)
@patch("app.roads.scoring.score_location", return_value=MagicMock(score=60.0))
def test_post_draw_endpoint(_mock_loc, _mock_elev, _mock_osrm, _mock_lc):
    body = {
        "coords": [[54.0, -3.0], [54.1, -2.95], [54.2, -2.9]],
        "profile": "balanced",
    }
    r = client.post("/api/route/draw", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "drawn"
    assert data["road_snap_method"] == "pairwise"
    assert data["chosen"]["colour_scored"] is True


def test_reject_too_few_vertices():
    r = client.post("/api/route/draw", json={"coords": [[54.0, -3.0]]})
    assert r.status_code == 422


def test_reject_too_many_vertices():
    coords = [[54.0 + i * 0.001, -3.0] for i in range(51)]
    r = client.post("/api/route/draw", json={"coords": coords})
    assert r.status_code == 422


@patch("app.roads.score_drawn_route")
def test_public_gate_draw_requires_key(mock_score, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    monkeypatch.setattr(config, "API_KEY", "test-secret-key")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 60)

    r = client.post(
        "/api/route/draw",
        json={"coords": [[54.0, -3.0], [54.2, -2.9]]},
    )
    assert r.status_code == 401
    mock_score.assert_not_called()
