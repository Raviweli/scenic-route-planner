"""Mocked OSRM parsing tests (no network)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.roads import _directions_from_legs, _motorway_km, get_osrm_routes


def _osrm_payload():
    return {
        "code": "Ok",
        "routes": [{
            "distance": 12345.0,
            "duration": 900.0,
            "geometry": {
                "coordinates": [
                    [-3.1, 54.4],
                    [-3.05, 54.42],
                    [-3.0, 54.45],
                ]
            },
            "legs": [{
                "steps": [
                    {
                        "distance": 5000,
                        "name": "M6",
                        "ref": "M6",
                        "maneuver": {"type": "depart", "location": [-3.1, 54.4]},
                    },
                    {
                        "distance": 7345,
                        "name": "A591",
                        "ref": "A591",
                        "maneuver": {
                            "type": "turn",
                            "modifier": "left",
                            "location": [-3.05, 54.42],
                        },
                    },
                    {
                        "distance": 0,
                        "name": "",
                        "ref": "",
                        "maneuver": {"type": "arrive", "location": [-3.0, 54.45]},
                    },
                ]
            }],
        }],
    }


def test_directions_from_legs_readable():
    legs = _osrm_payload()["routes"][0]["legs"]
    steps = _directions_from_legs(legs)
    assert steps
    assert "Head" in steps[0]["text"] or "Head off" in steps[0]["text"]
    assert any("Turn left" in s["text"] for s in steps)
    assert steps[-1]["text"].startswith("Arrive")


def test_motorway_km_from_fixture_legs():
    legs = _osrm_payload()["routes"][0]["legs"]
    assert _motorway_km(legs) == 5.0


@patch("app.roads._session")
def test_get_osrm_routes_continue_straight_with_vias(mock_session):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _osrm_payload()
    mock_session.get.return_value = resp

    via = (54.42, -3.05)
    get_osrm_routes((54.4, -3.1), (54.45, -3.0), alternatives=0,
                    waypoints=[via], continue_straight=True)
    params = mock_session.get.call_args.kwargs["params"]
    assert params["continue_straight"] == "true"


@patch("app.roads._session")
def test_get_osrm_routes_parses_geometry(mock_session):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _osrm_payload()
    mock_session.get.return_value = resp

    routes = get_osrm_routes((54.4, -3.1), (54.45, -3.0), alternatives=0)
    assert len(routes) == 1
    rt = routes[0]
    assert rt["coords"][0] == (54.4, -3.1)
    assert abs(rt["distance_km"] - 12.345) < 1e-6
    assert abs(rt["duration_min"] - 15.0) < 1e-6
    assert rt["motorway_km"] == 5.0
    assert rt["directions"]
