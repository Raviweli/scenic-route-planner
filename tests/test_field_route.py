"""Scenic field routing: square heatmap, corridors, OSRM tournament, API."""
from __future__ import annotations

import json
from unittest.mock import patch

import networkx as nx
import numpy as np
from fastapi.testclient import TestClient

from app import config
from app.field_route import (
    CorridorSpec,
    FieldCorridor,
    HeatmapScores,
    apply_field_urban_clamp,
    build_corridor_cells,
    build_corridor_graph,
    corridor_reject_fraction,
    corridor_spec,
    extract_corridors,
    field_reject_threshold,
    find_field_path,
    green_mask_stats,
    is_reject_score,
    plan_field,
    _adaptive_osrm_reserve_sec,
    _colour_priority_key,
    _inflate_to_square,
    _osrm_tournament,
    _proxy_blend,
    _score_corridor_cells,
)
from app.grid import GridSpec, build_cells
from app.main import app

client = TestClient(app)


def test_field_cell_deg_default_is_fine():
    """Default heatmap cells are substantially finer than the legacy 0.015° grid."""
    assert config.FIELD_CELL_DEG <= 0.0015
    assert config.FIELD_CELL_DEG < 0.008
    assert config.FIELD_CELL_DEG_LONG <= 0.004
    assert config.FIELD_MAX_CELLS >= 20000
    assert config.FIELD_REJECT_SCENIC >= 25.0


def test_corridor_spec_short_uk_hop_keeps_finer_than_legacy():
    """Yate→Portishead-scale square should stay ~2× finer than the old ~0.01° grid."""
    a, b = (51.5405, -2.4184), (51.4840, -2.7690)
    spec = corridor_spec(a, b)
    assert spec.cell_deg <= 0.006
    assert spec.n_cells <= config.FIELD_MAX_CELLS


def test_apply_field_urban_clamp_rejects_grey_and_urban_land():
    """High grey_frac or urban landcover must push the cell under the reject floor."""
    reject = field_reject_threshold()
    grey = apply_field_urban_clamp(70.0, grey_frac=0.45, land_score=60.0)
    assert is_reject_score(grey, reject)
    urban = apply_field_urban_clamp(75.0, grey_frac=0.05, land_score=28.0)
    assert is_reject_score(urban, reject)
    # Modest grey pulls towns below vivid-green even without hard reject.
    soft = apply_field_urban_clamp(80.0, grey_frac=0.14, land_score=None)
    assert soft < 70.0
    # Countryside green stays high.
    rural = apply_field_urban_clamp(72.0, grey_frac=0.02, land_score=70.0)
    assert rural >= 70.0
    assert not is_reject_score(rural, reject)


def test_reject_cells_excluded_from_corridor_graph():
    """Reject-band cells are omitted from the green lattice (hard nope)."""
    spec = CorridorSpec(min_lat=54.0, min_lng=-3.1, max_lat=54.10, max_lng=-2.98, cell_deg=0.02)
    cells = build_cells(GridSpec(spec.min_lat, spec.min_lng, spec.max_lat, spec.max_lng, spec.cell_deg))
    # Central block of urban reject; scenic ring around so A→B can skirt the town.
    scores = {}
    for c in cells:
        if 1 <= c.col <= 3 and 1 <= c.row <= 3:
            scores[c.id] = 18.0
        else:
            scores[c.id] = 88.0
    a, b = (54.01, -3.09), (54.09, -2.99)
    g = build_corridor_graph(cells, scores, preference=1.0, a=a, b=b)
    reject = field_reject_threshold()
    for c in cells:
        if scores[c.id] < reject and c.id in g:
            near_a = (
                abs(c.lat - a[0]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG
                and abs(c.lng - a[1]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG
            )
            near_b = (
                abs(c.lat - b[0]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG
                and abs(c.lng - b[1]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG
            )
            assert near_a or near_b
    path = find_field_path(g, a, b, preference=1.0)
    assert "error" not in path
    for n in path["path_nodes"]:
        sc = g.nodes[n]["score"]
        if is_reject_score(sc, reject):
            # Only endpoint slack nodes may be reject.
            lat, lng = g.nodes[n]["lat"], g.nodes[n]["lng"]
            near = (
                (abs(lat - a[0]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG
                 and abs(lng - a[1]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG)
                or (abs(lat - b[0]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG
                    and abs(lng - b[1]) <= config.FIELD_ENDPOINT_REJECT_SLACK_DEG)
            )
            assert near


def test_extract_corridors_skips_reject_band_primary():
    """When a soft path would cross reject cells, that spine is discarded."""
    # Build a connected graph that still contains reject nodes (soft graph),
    # then ensure extract_corridors drops spines with high reject_frac.
    spec = CorridorSpec(min_lat=54.0, min_lng=-3.2, max_lat=54.12, max_lng=-2.9, cell_deg=0.02)
    cells = build_cells(GridSpec(spec.min_lat, spec.min_lng, spec.max_lat, spec.max_lng, spec.cell_deg))
    a, b = (54.01, -3.18), (54.11, -2.92)
    scores = {}
    for c in cells:
        if c.col == 4:
            scores[c.id] = 92.0  # clean scenic diversion column
        elif c.col == 1:
            # Primary-ish column interrupted by reject cells along most of the height.
            scores[c.id] = 20.0 if c.row not in (0, 5) else 70.0
        else:
            scores[c.id] = 40.0  # dull but above reject so graph stays connected
    # Soft graph: include all cells, with reject edges heavily penalised.
    from app.field_route import _build_soft_reject_graph
    g = _build_soft_reject_graph(
        cells, scores,
        detour_factor=float(config.FIELD_DETOUR_FACTOR),
        reject_thr=field_reject_threshold(),
    )
    # Force a via through the dirty column as "primary" candidate by temporarily
    # lowering diversion sep / checking reject meta after extract.
    corridors = extract_corridors(g, cells, scores, a, b, preference=1.0)
    green = [c for c in corridors if c.kind.startswith("green")]
    assert green
    for corr in green:
        assert corridor_reject_fraction(corr.path_nodes, scores) <= config.FIELD_REJECT_MAX_FRAC + 1e-9
    # Diversion through col 4 should be preferred over a reject-heavy spine.
    cols = set()
    for corr in green:
        for n in corr.path_nodes:
            cols.add(g.nodes[n]["col"])
    assert 4 in cols


def test_corridor_spec_uses_fine_default_on_short_hop():
    a, b = (51.50, -0.10), (51.55, -0.05)  # ~7 km
    spec = corridor_spec(a, b, pad_deg=0.05, square=True)
    if spec.n_cells <= config.FIELD_MAX_CELLS:
        assert spec.cell_deg <= config.FIELD_CELL_DEG_LONG + 1e-9
        assert abs(spec.cell_deg - config.FIELD_CELL_DEG) < 1e-9


def _make_strip_scores(cells, high_col: int, high_score: float = 90.0, low_score: float = 40.0):
    """Dull cells stay above FIELD_REJECT_SCENIC so the lattice stays connected."""
    scores = {}
    for c in cells:
        scores[c.id] = high_score if c.col == high_col else low_score
    return scores


def test_field_path_prefers_high_scenic_strip():
    """Lattice path should favour a high-scenic column over a dull bypass."""
    spec = CorridorSpec(min_lat=54.0, min_lng=-3.1, max_lat=54.06, max_lng=-3.0, cell_deg=0.02)
    cells = build_cells(GridSpec(spec.min_lat, spec.min_lng, spec.max_lat, spec.max_lng, spec.cell_deg))
    a, b = (54.01, -3.09), (54.05, -3.01)
    scores = _make_strip_scores(cells, high_col=2)
    g = build_corridor_graph(cells, scores, preference=1.0, a=a, b=b)
    path = find_field_path(g, a, b, preference=1.0)
    assert "error" not in path
    node_cols = [g.nodes[n]["col"] for n in path["path_nodes"]]
    assert 2 in node_cols
    assert path["lattice_avg_scenic"] >= 50.0


def test_corridor_spec_inflates_to_square():
    """Padded A→B rectangle should become approximately square."""
    a, b = (51.0, -1.0), (51.05, -0.5)  # wide short strip
    spec = corridor_spec(a, b, pad_deg=0.05, cell_deg=0.02, square=True)
    height = spec.max_lat - spec.min_lat
    width = spec.max_lng - spec.min_lng
    assert abs(height - width) < 1e-9
    # Without square inflation the height would be much smaller than width.
    s, w, n, e = _inflate_to_square(51.0, -1.05, 51.1, -0.45)
    assert abs((n - s) - (e - w)) < 1e-9


def test_corridor_spec_coarsens_when_over_cell_cap(monkeypatch):
    monkeypatch.setattr(config, "FIELD_MAX_CELLS", 10)
    spec = corridor_spec((51.0, -1.0), (52.0, 0.5))
    assert spec.n_cells <= config.FIELD_MAX_CELLS
    assert spec.cell_deg > config.FIELD_CELL_DEG


def test_build_corridor_cells_no_strip_by_default(monkeypatch):
    monkeypatch.setattr(config, "FIELD_CORRIDOR_HALF_WIDTH_DEG", 0.0)
    a, b = (54.0, -3.0), (54.2, -2.8)
    spec = corridor_spec(a, b, pad_deg=0.1, cell_deg=0.05, square=True)
    cells = build_corridor_cells(spec, a, b)
    full = build_cells(GridSpec(spec.min_lat, spec.min_lng, spec.max_lat, spec.max_lng, spec.cell_deg))
    assert len(cells) == len(full)


def test_proxy_blend_omits_colour():
    """Proxy scoring must not invent a fake flat 50 when terrain/land exist."""
    sc = _proxy_blend(80.0, 70.0, {"colour": 0.35, "terrain": 0.25, "landcover": 0.40}, None)
    # Weighted terrain+land only: (80*0.25 + 70*0.40) / 0.65 ≈ 73.8
    assert sc > 60.0
    assert sc != 50.0


def test_proxy_blend_flat_without_landcover_is_neutral():
    """Missing landcover + weak relief must not paint reject-brown (score ≈ 0)."""
    assert _proxy_blend(0.0, None, None, None) == 50.0
    assert _proxy_blend(12.0, None, None, None) == 50.0
    # Strong relief alone remains a real proxy signal.
    assert _proxy_blend(80.0, None, None, None) >= 70.0


def test_score_cell_keeps_colour_when_landcover_missing(monkeypatch):
    """Esri greens must not be diluted to brown by flat terrain when Overpass fails."""
    from app.field_route import score_cell
    from app.scoring import ScenicScore

    monkeypatch.setattr(
        "app.field_route.scoring.score_location",
        lambda *a, **k: ScenicScore(
            score=76.0, green_frac=0.6, blue_frac=0.05, grey_frac=0.0,
            brightness=0.4, source="esri",
        ),
    )
    sc = score_cell(51.80, -2.55, features=None, elev=40.0, terrain=5.0, land_score=None)
    assert sc >= 70.0


def test_score_cell_pulls_down_grey_urban_without_landcover(monkeypatch):
    """Tree-canopy towns with measurable grey roofs must not stay vivid green."""
    from app.field_route import score_cell
    from app.scoring import ScenicScore

    monkeypatch.setattr(
        "app.field_route.scoring.score_location",
        lambda *a, **k: ScenicScore(
            score=72.0, green_frac=0.7, blue_frac=0.02, grey_frac=0.16,
            brightness=0.45, source="esri",
        ),
    )
    sc = score_cell(51.45, -2.59, features=None, elev=20.0, terrain=4.0, land_score=None)
    assert sc < 62.0


def test_score_cell_caps_open_water_green_wash(monkeypatch):
    """High blue_frac cells should not paint as vivid countryside green."""
    from app.field_route import score_cell
    from app.scoring import ScenicScore

    monkeypatch.setattr(
        "app.field_route.scoring.score_location",
        lambda *a, **k: ScenicScore(
            score=90.0, green_frac=0.1, blue_frac=0.55, grey_frac=0.0,
            brightness=0.5, source="esri",
        ),
    )
    sc = score_cell(51.45, -2.70, features=None, elev=0.0, terrain=0.0, land_score=None)
    assert sc <= 65.0

def test_adaptive_osrm_reserve_shrinks_when_warm():
    """Warm/fast landcover frees colour time; cold truncated keeps full reserve."""
    base = config.FIELD_OSRM_RESERVE_SEC
    floor = config.FIELD_OSRM_RESERVE_MIN_SEC
    warm = _adaptive_osrm_reserve_sec(
        landcover_elapsed=2.0,
        landcover_incomplete=False,
        cache_hits=80,
        n_cells=100,
    )
    cold = _adaptive_osrm_reserve_sec(
        landcover_elapsed=18.0,
        landcover_incomplete=True,
        cache_hits=0,
        n_cells=100,
    )
    assert warm <= floor + 0.01
    assert cold == base
    assert warm < cold


def test_colour_priority_prefers_spine_over_remote():
    """Cells on the A→B chord outrank equally-scenic remote cells."""
    a, b = (54.0, -3.0), (54.1, -2.9)
    on_path = type("C", (), {"lat": 54.05, "lng": -2.95})()
    remote = type("C", (), {"lat": 54.05, "lng": -3.20})()
    k_path = _colour_priority_key(on_path, 60.0, a, b)
    k_remote = _colour_priority_key(remote, 60.0, a, b)
    # Lower sort key wins (more negative first component).
    assert k_path < k_remote


@patch("app.field_route._cell_cache_get", return_value=None)
@patch("app.field_route.scoring.score_location")
@patch("app.field_route.enrich.elevation_batch")
@patch("app.field_route.enrich.landcover_is_usable", return_value=False)
def test_score_keeps_proxy_under_deadline(mock_usable, mock_elev, mock_colour, mock_cache):
    """When colour budget is zero, cells keep proxy scores — not filled with 50."""
    mock_elev.return_value = [100.0, 200.0, 150.0, 120.0]
    mock_colour.side_effect = AssertionError("colour should not run past deadline")

    cells = build_cells(GridSpec(54.0, -3.0, 54.04, -2.96, 0.02))
    # Deadline already passed → no colour fetches.
    deadline = 0.0
    heat = _score_corridor_cells(
        cells, features=None,
        a=(54.0, -3.0), b=(54.04, -2.96),
        weights={"colour": 0.35, "terrain": 0.25, "landcover": 0.40},
        profile_id=None,
        source="esri",
        deadline=deadline,
    )
    assert isinstance(heat, HeatmapScores)
    assert len(heat.scores) == len(cells)
    assert heat.proxy_cells == len(cells)
    for c in cells:
        assert heat.sources[c.id] == "proxy"
        assert c.id in heat.scores


def test_extract_corridors_picks_distinct_green_columns():
    """Diversions should force paths through a second green column away from primary."""
    spec = CorridorSpec(min_lat=54.0, min_lng=-3.2, max_lat=54.12, max_lng=-2.9, cell_deg=0.02)
    cells = build_cells(GridSpec(spec.min_lat, spec.min_lng, spec.max_lat, spec.max_lng, spec.cell_deg))
    a, b = (54.01, -3.18), (54.11, -2.92)
    # Two green columns (col 1 and col 4); middle columns dull (above reject floor).
    scores = {}
    for c in cells:
        if c.col in (1, 4):
            scores[c.id] = 90.0
        else:
            scores[c.id] = 40.0
    g = build_corridor_graph(cells, scores, preference=1.0, a=a, b=b)
    corridors = extract_corridors(g, cells, scores, a, b, preference=1.0)
    kinds = [c.kind for c in corridors]
    assert "green_primary" in kinds
    assert "baseline_direct" in kinds
    # At least one diversion if geometry allows.
    green = [c for c in corridors if c.kind.startswith("green")]
    assert len(green) >= 1
    cols_used = set()
    for corr in green:
        for n in corr.path_nodes:
            cols_used.add(g.nodes[n]["col"])
    # Primary should touch at least one green column.
    assert cols_used & {1, 4}

    mask = green_mask_stats(cells, scores, threshold=55.0)
    assert mask["green_cells"] > 0
    assert mask["dull_cells"] > 0


def test_find_field_path_no_path():
    g = nx.Graph()
    g.add_node("a", lat=54.0, lng=-3.0, row=0, col=0, score=80.0)
    g.add_node("b", lat=54.1, lng=-2.0, row=0, col=5, score=80.0)
    out = find_field_path(g, (54.0, -3.0), (54.1, -2.0), 0.7)
    assert "error" in out


def _fake_route(scenic: float, distance_km: float = 12.0, motorway_km: float = 0.0, label="x"):
    return {
        "coords": [(54.0, -3.0), (54.05, -2.95), (54.1, -2.9)],
        "distance_km": distance_km,
        "duration_min": distance_km * 1.5,
        "motorway_km": motorway_km,
        "directions": [],
        "render": [{"lat": 54.0, "lng": -3.0, "score": scenic}],
        "avg_scenic_score": scenic,
        "components": {"colour": scenic, "terrain": scenic, "landcover": scenic},
        "_colour_scored": True,
        "_landcover_usable": True,
        "_field_label": label,
    }


@patch("app.field_route.score_route")
@patch("app.field_route.get_osrm_routes")
def test_osrm_tournament_picks_higher_scenic(mock_osrm, mock_score):
    """Tournament must prefer the higher post-snap scenic road candidate."""
    dull = _fake_route(40.0, distance_km=10.0, label="direct")
    green = _fake_route(75.0, distance_km=14.0, label="via_green")

    def osrm_side_effect(a, b, alternatives=0, waypoints=None, **kwargs):
        if waypoints:
            return [dict(green)]
        return [dict(dull)]

    mock_osrm.side_effect = osrm_side_effect

    def score_side_effect(route, **kwargs):
        # Preserve avg already set; mark scored.
        route["_colour_scored"] = True
        return route

    mock_score.side_effect = score_side_effect

    corr = FieldCorridor(
        kind="green_primary",
        vertices=[(54.0, -3.0), (54.05, -2.95), (54.08, -2.93), (54.1, -2.9)],
        path_nodes=["n0", "n1", "n2", "n3"],
        lattice_km=15.0,
        lattice_avg_scenic=80.0,
    )
    # Fake node scores for peak vias.
    scores = {"n0": 50, "n1": 90, "n2": 85, "n3": 50}
    chosen, tried, reason = _osrm_tournament(
        (54.0, -3.0), (54.1, -2.9), [corr], scores,
        features=None, weights=None, source="esri",
        preference=1.0, avoid_motorways=False, deadline=None,
    )
    assert chosen is not None
    assert chosen["avg_scenic_score"] == 75.0
    assert len(tried) >= 1
    assert "best_road_score" in reason


@patch("app.field_route.score_route")
@patch("app.field_route.get_osrm_routes")
def test_osrm_tournament_avoid_motorways(mock_osrm, mock_score):
    mw = _fake_route(80.0, motorway_km=12.0, label="mw")
    side = _fake_route(70.0, motorway_km=0.0, distance_km=16.0, label="side")

    def osrm_side_effect(a, b, alternatives=0, waypoints=None, **kwargs):
        if waypoints:
            return [dict(side)]
        return [dict(mw), dict(side)]

    mock_osrm.side_effect = osrm_side_effect
    mock_score.side_effect = lambda route, **kw: route

    corr = FieldCorridor(
        kind="green_primary",
        vertices=[(54.0, -3.0), (54.05, -2.95), (54.1, -2.9)],
        path_nodes=["a", "b", "c"],
        lattice_km=12.0,
        lattice_avg_scenic=70.0,
    )
    scores = {"a": 60, "b": 90, "c": 60}
    chosen, tried, reason = _osrm_tournament(
        (54.0, -3.0), (54.1, -2.9), [corr], scores,
        features=None, weights=None, source="esri",
        preference=0.5, avoid_motorways=True, deadline=None,
    )
    assert chosen is not None
    assert chosen.get("motorway_km", 0) <= 0.05
    assert "avoid_motorways" in reason


@patch("app.field_route._osrm_tournament")
@patch("app.field_route._score_corridor_cells")
@patch("app.field_route.enrich.fetch_landcover")
def test_plan_field_returns_field_source(mock_lc, mock_score_cells, mock_tourney):
    mock_lc.return_value = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
        "truncated": False,
    }
    cells = build_corridor_cells(
        corridor_spec((54.0, -3.0), (54.1, -2.9)),
        (54.0, -3.0), (54.1, -2.9),
    )
    heat = HeatmapScores(
        scores={c.id: 70.0 for c in cells},
        sources={c.id: "proxy" for c in cells},
    )
    mock_score_cells.return_value = heat

    route = _fake_route(68.5)
    mock_tourney.return_value = (route, [{"label": "direct"}], "best_road_score among 1")

    result = plan_field(
        (54.0, -3.0), (54.1, -2.9), 0.8,
        profile_id="balanced", time_budget=False,
    )
    assert result.get("error") is None
    assert result["source"] == "field"
    assert result["planner"] == "field"
    assert result["chosen"]["avg_scenic_score"] == 68.5
    meta = result["field_meta"]
    assert "bbox" in meta
    assert "green_corridors" in meta
    assert "proxy_cells" in meta
    assert "chosen_reason" in meta
    assert meta["road_score"] == 68.5
    # Field Overpass uses corridor strip (not full-square half_width=0).
    assert mock_lc.call_args.kwargs.get("corridor_half_width_deg") == config.LANDCOVER_CORRIDOR_HALF_WIDTH_DEG
    assert "landcover_usable" in meta
    assert "landcover_features" in meta


@patch("app.main.field_route.plan_field")
def test_field_route_json_endpoint(mock_plan):
    mock_plan.return_value = {
        "source": "field",
        "planner": "field",
        "from": [54.0, -3.0],
        "to": [54.1, -2.9],
        "chosen": {
            "chosen": True,
            "distance_km": 10.0,
            "duration_min": 15.0,
            "avg_scenic_score": 72.0,
            "meets_min": True,
            "components": {},
            "render": [],
            "directions": [],
            "motorway_km": 0.0,
        },
        "alternatives": [],
        "signals": {},
        "field_meta": {"cells_scored": 10, "cell_deg": 0.015},
    }
    r = client.get(
        "/api/route/field",
        params={
            "from_lat": 54.0, "from_lng": -3.0, "to_lat": 54.1, "to_lng": -2.9,
            "avoid_motorways": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["planner"] == "field"
    assert mock_plan.call_args.kwargs.get("avoid_motorways") is True


def test_field_route_public_gate_401(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    monkeypatch.setattr(config, "API_KEY", "test-secret-key")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 60)
    r = client.get(
        "/api/route/field",
        params={"from_lat": 54.0, "from_lng": -3.0, "to_lat": 54.1, "to_lng": -2.9},
    )
    assert r.status_code == 401


@patch("app.main.field_route.plan_field_events")
@patch("app.main.roads.plan_events")
def test_compare_field_stream_two_legs(mock_road_events, mock_field_events):
    chosen = {
        "chosen": True,
        "distance_km": 10.0,
        "duration_min": 20.0,
        "avg_scenic_score": 55.0,
        "meets_min": True,
        "components": {},
        "render": [],
        "directions": [],
        "motorway_km": 0.0,
    }

    def road_gen(*args, **kwargs):
        yield {"type": "done", "result": {
            "chosen": chosen,
            "budget_exhausted": False,
            "budget_reasons": [],
            "signals": {},
            "min_scenic_met": True,
        }}

    def field_gen(*args, **kwargs):
        yield {"type": "done", "result": {
            "chosen": {**chosen, "avg_scenic_score": 62.0},
            "budget_exhausted": False,
            "budget_reasons": [],
            "signals": {},
            "field_meta": {
                "lattice_avg_scenic": 58.0,
                "cells_scored": 20,
                "cell_deg": 0.015,
                "green_corridors": [{"kind": "green_primary"}],
            },
        }}

    mock_road_events.side_effect = road_gen
    mock_field_events.side_effect = field_gen

    r = client.get(
        "/api/route/compare/stream",
        params={
            "from_lat": 54.0, "from_lng": -3.0,
            "to_lat": 54.1, "to_lng": -2.9,
            "compare_field": True,
        },
    )
    assert r.status_code == 200
    events = []
    for line in r.text.strip().split("\n\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1
    result = done[0]["result"]
    assert result["mode"] == "compare_field"
    assert "road" in result and "field" in result
    assert "avoid_motorways" in result["road"]
    assert "motorway_avoid_met" in result["field"]


@patch("app.field_route._snap_drawn_route_to_roads")
@patch("app.field_route.get_osrm_routes")
@patch("app.field_route.score_route")
def test_osrm_tournament_rejects_snap_when_avoid_mw(mock_score, mock_osrm, mock_snap):
    """snap_fallback with motorway km must not win when avoid_motorways is on."""
    mock_osrm.side_effect = RuntimeError("no osrm")
    mw_snap = _fake_route(60.0, distance_km=400.0, motorway_km=115.0, label="snap_fallback")
    mock_snap.return_value = (dict(mw_snap), "chord")
    mock_score.side_effect = lambda route, **kw: route

    corr = FieldCorridor(
        kind="green_primary",
        vertices=[(51.0, -3.0), (51.2, -3.2), (51.4, -3.5)],
        path_nodes=["a", "b", "c"],
        lattice_km=40.0,
        lattice_avg_scenic=55.0,
    )
    scores = {"a": 50, "b": 80, "c": 50}
    chosen, tried, reason = _osrm_tournament(
        (51.0, -3.0), (51.4, -3.5), [corr], scores,
        features=None, weights=None, source="esri",
        preference=0.8, avoid_motorways=True, deadline=None,
    )
    assert chosen is None
    assert "avoid_motorways" in reason
    assert "snap" in reason or reason == "avoid_motorways_snap_rejected"


@patch("app.field_route.score_route")
@patch("app.field_route.get_osrm_routes")
def test_osrm_tournament_returns_least_motorway_when_no_zero_mw(mock_osrm, mock_score):
    """Avoid-motorways should stay honest and return the least-bad road option."""
    heavy = _fake_route(72.0, motorway_km=18.0, distance_km=40.0, label="direct")
    lighter = _fake_route(68.0, motorway_km=4.0, distance_km=43.0, label="via_green")

    def osrm_side_effect(a, b, alternatives=0, waypoints=None, **kwargs):
        if waypoints:
            return [dict(lighter)]
        return [dict(heavy)]

    mock_osrm.side_effect = osrm_side_effect
    mock_score.side_effect = lambda route, **kw: route

    corr = FieldCorridor(
        kind="green_primary",
        vertices=[(51.0, -3.0), (51.2, -3.2), (51.4, -3.5)],
        path_nodes=["a", "b", "c"],
        lattice_km=40.0,
        lattice_avg_scenic=55.0,
    )
    scores = {"a": 50, "b": 80, "c": 50}
    chosen, tried, reason = _osrm_tournament(
        (51.0, -3.0), (51.4, -3.5), [corr], scores,
        features=None, weights=None, source="esri",
        preference=0.8, avoid_motorways=True, deadline=None,
    )
    assert chosen is not None
    assert chosen.get("motorway_km") == 4.0
    assert "avoid_motorways_unmet" in reason
    assert tried


@patch("app.field_route.score_route")
@patch("app.field_route.get_osrm_routes")
def test_osrm_tournament_emergency_direct_when_budget_starved(mock_osrm, mock_score):
    """When corridor OSRM fails, emergency direct A→B is attempted before snap."""
    plain = _fake_route(62.0, distance_km=80.0, motorway_km=0.0, label="direct_emergency")

    def osrm_side_effect(a, b, alternatives=0, waypoints=None, **kwargs):
        if waypoints:
            raise RuntimeError("via failed")
        if alternatives:
            return [dict(plain)]
        raise RuntimeError("no via")

    mock_osrm.side_effect = osrm_side_effect
    mock_score.side_effect = lambda route, **kw: route

    corr = FieldCorridor(
        kind="green_primary",
        vertices=[(51.0, -3.0), (51.2, -3.2), (51.4, -3.5)],
        path_nodes=["a", "b", "c"],
        lattice_km=40.0,
        lattice_avg_scenic=55.0,
    )
    scores = {"a": 50, "b": 80, "c": 50}
    chosen, tried, reason = _osrm_tournament(
        (51.0, -3.0), (51.4, -3.5), [corr], scores,
        features=None, weights=None, source="esri",
        preference=0.5, avoid_motorways=False, deadline=None,
    )
    assert chosen is not None
    assert chosen.get("_field_label") in ("direct", "direct_emergency")
    assert any(t.get("label") in ("direct", "direct_emergency") for t in tried)
