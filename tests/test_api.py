"""FastAPI TestClient smoke tests (offline; geocode mocked)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "cells" in body
    assert "osrm_configured" in body
    assert isinstance(body["osrm_configured"], bool)
    assert body["osrm_mode"] == "public_demo"
    assert "public_mode" in body
    assert "max_inflight_plans" in body
    assert "cache_entries" in body
    assert "elevation" in body["cache_entries"]
    assert "elevation_disk" in body["cache_entries"]
    assert "landcover" in body["cache_entries"]


def test_route_rejects_out_of_range_lat():
    r = client.get(
        "/api/route",
        params={
            "from_lat": 999,
            "from_lng": -3.0,
            "to_lat": 54.5,
            "to_lng": -3.1,
        },
    )
    assert r.status_code == 422


@patch("app.main.requests.get")
def test_geocode_mocked(mock_get):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = [
        {
            "lat": "54.6009",
            "lon": "-3.1371",
            "display_name": "Keswick, Cumbria",
            "name": "Keswick",
        },
        {
            "lat": "54.61",
            "lon": "-3.14",
            "display_name": "Keswick Museum, Cumbria",
            "name": "Keswick Museum",
        },
    ]
    mock_get.return_value = resp

    r = client.get("/api/geocode", params={"q": "Keswick"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["display_name"] == "Keswick, Cumbria"
    assert body["results"][0]["name"] == "Keswick"
    assert abs(body["results"][0]["lat"] - 54.6009) < 1e-4
    # Nominatim limit raised for multi-hit picker
    assert mock_get.call_args.kwargs["params"]["limit"] == 5


def test_featured_presets_endpoint():
    r = client.get("/api/presets", params={"featured": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert body["count"] <= 20


@patch("app.main.roads.plan")
def test_compare_accepts_planner_knobs(mock_plan):
    """Compare forwards avoid_motorways / min_scenic / explore_all / time_budget."""
    chosen = {
        "chosen": True,
        "distance_km": 10.0,
        "duration_min": 20.0,
        "avg_scenic_score": 55.0,
        "meets_min": True,
        "components": {},
        "num_samples": 0,
        "render": [],
        "directions": [],
        "motorway_km": 0.0,
    }
    mock_plan.return_value = {
        "chosen": chosen,
        "alternatives": [chosen],
        "budget_exhausted": False,
        "budget_reasons": [],
        "signals": {"landcover_incomplete": False},
        "min_scenic_met": True,
        "min_scenic": 40.0,
    }
    r = client.get(
        "/api/route/compare",
        params={
            "from_lat": 54.6,
            "from_lng": -3.1,
            "to_lat": 54.4,
            "to_lng": -2.9,
            "profile": "balanced",
            "preference": 0.9,
            "avoid_motorways": "true",
            "min_scenic": 40,
            "explore_all": "true",
            "time_budget": "false",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "fastest" in body and "scenic" in body
    assert "scenic_meta" in body
    assert mock_plan.call_count == 2
    scenic_kwargs = mock_plan.call_args_list[1].kwargs
    assert scenic_kwargs.get("avoid_motorways") is True
    assert scenic_kwargs.get("min_scenic") == 40.0
    assert scenic_kwargs.get("explore_all") is True
    assert scenic_kwargs.get("preference") == 0.9
    assert scenic_kwargs.get("time_budget") is False


@patch("app.main.roads.plan")
def test_compare_carries_motorway_avoid_reason(mock_plan):
    chosen = {
        "chosen": True,
        "distance_km": 10.0,
        "duration_min": 20.0,
        "avg_scenic_score": 55.0,
        "meets_min": True,
        "components": {},
        "num_samples": 0,
        "render": [],
        "directions": [],
        "motorway_km": 2.0,
    }
    mock_plan.return_value = {
        "chosen": chosen,
        "alternatives": [chosen],
        "budget_exhausted": False,
        "budget_reasons": [],
        "signals": {"landcover_incomplete": False},
        "min_scenic_met": True,
        "min_scenic": 0.0,
        "avoid_motorways": True,
        "motorway_avoid_met": False,
        "motorway_avoid_reason": "all_candidates_include_motorways",
    }
    r = client.get(
        "/api/route/compare",
        params={
            "from_lat": 54.6,
            "from_lng": -3.1,
            "to_lat": 54.4,
            "to_lng": -2.9,
            "avoid_motorways": "true",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fastest"]["motorway_avoid_reason"] == "all_candidates_include_motorways"
    assert body["scenic_meta"]["motorway_avoid_reason"] == "all_candidates_include_motorways"
