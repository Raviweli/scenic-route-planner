"""Via-point hard constraints in the planner."""
from __future__ import annotations

from unittest.mock import patch

from app.roads import plan


def _fake_route(a, b, **kwargs):
    vias = kwargs.get("waypoints") or (
        [kwargs["waypoint"]] if kwargs.get("waypoint") else []
    )
    coords = [a, *vias, b]
    return [{
        "coords": coords,
        "distance_km": 20.0 + 5.0 * len(vias),
        "duration_min": 30.0,
        "motorway_km": 0.0,
        "directions": [],
    }]


@patch("app.roads.enrich.fetch_landcover", return_value=None)
@patch("app.roads.get_osrm_routes", side_effect=_fake_route)
def test_plan_with_one_via_passes_waypoints(mock_osrm, _mock_lc):
    a = (54.60, -3.14)
    b = (54.43, -2.96)
    via = (54.52, -3.05)
    result = plan(a, b, preference=0.5, source="synthetic", vias=[via])
    assert result["vias"] == [[54.52, -3.05]]
    # Base call must include the user via as hard OSRM waypoints.
    assert mock_osrm.call_count >= 1
    base = mock_osrm.call_args_list[0]
    assert base.kwargs.get("waypoints") == [via] or (
        base.args and False
    ) or base.kwargs.get("waypoints") == [via]
    # Prefer kwargs form used by plan._fetch_base
    found = False
    for call in mock_osrm.call_args_list:
        wps = call.kwargs.get("waypoints")
        if wps and any(abs(w[0] - via[0]) < 1e-6 and abs(w[1] - via[1]) < 1e-6 for w in wps):
            found = True
            break
    assert found, "user via never passed to get_osrm_routes"
