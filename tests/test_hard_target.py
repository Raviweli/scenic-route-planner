"""Hard-target / explore: incremental scoring keeps prior route scores."""
from __future__ import annotations

from app.roads import reblend_route_landcover, select_chosen


def test_select_chosen_still_respects_floor_after_resort():
    # After a hard-target round, list is re-ranked; floor logic unchanged.
    routes = [
        {"avg_scenic_score": 40.0, "duration_min": 50.0},
        {"avg_scenic_score": 72.0, "duration_min": 80.0},
        {"avg_scenic_score": 90.0, "duration_min": 120.0},
    ]
    chosen, met = select_chosen(routes, min_scenic=70)
    assert met is True
    assert chosen["avg_scenic_score"] == 72.0


def test_pool_not_wiped_on_explore_style_merge():
    """Existing scores survive; reblend only adjusts landcover without clearing."""
    rt = {
        "avg_scenic_score": 66.0,
        "proxy_scenic": 66.0,
        "coords": [(54.0, -3.0), (54.1, -2.9)],
        "components": {"colour": None, "terrain": 40.0, "landcover": 50.0},
        "_score_meta": None,  # no meta → reblend is a no-op
    }
    before = rt["avg_scenic_score"]
    # Explore used to pop avg_scenic_score on every route; that must not happen.
    reblend_route_landcover(rt, features=None)
    assert rt["avg_scenic_score"] == before
