"""Terrain relief, elevation gap-fill, soften, and land-cover helpers (no network)."""
from __future__ import annotations

import time

import numpy as np
import requests

from app.enrich import (
    POSITIVE_TAGS,
    NEGATIVE_TAGS,
    _downsample_points,
    _gap_fill_elevations,
    _points_from_element,
    landcover_details,
    landcover_scores,
    is_water_near,
    merge_landcover,
    relief_scores,
    soften_terrain,
)


def test_relief_scores_flat_near_zero():
    scores = relief_scores([100.0] * 10, window=2)
    assert all(s == 0.0 for s in scores)


def test_relief_scores_rugged_higher():
    elevs = [0.0, 50.0, 200.0, 80.0, 10.0]
    scores = relief_scores(elevs, window=2)
    assert max(scores) > min(scores)
    assert all(0.0 <= s <= 100.0 for s in scores)


def test_relief_soft_saturation_differentiates_hills_and_mountains():
    gentle = relief_scores([0.0, 40.0, 80.0, 40.0, 0.0], window=2)
    big = relief_scores([0.0, 250.0, 600.0, 250.0, 0.0], window=2)
    assert max(gentle) < max(big)
    # Soft sat: huge relief approaches 100 without a hard clip at RELIEF_FULL_M.
    assert max(big) > 90.0
    assert max(big) <= 100.0
    # Gentle hills still score meaningfully above flat.
    assert max(gentle) > 20.0


def test_gap_fill_elevations_interpolates_interior():
    filled = _gap_fill_elevations([0.0, None, None, 90.0])
    assert filled is not None
    assert filled[0] == 0.0
    assert filled[3] == 90.0
    assert abs(filled[1] - 30.0) < 1e-6
    assert abs(filled[2] - 60.0) < 1e-6


def test_gap_fill_elevations_all_missing_returns_none():
    assert _gap_fill_elevations([None, None, None]) is None


def test_gap_fill_elevations_edge_fill():
    filled = _gap_fill_elevations([None, 50.0, None])
    assert filled == [50.0, 50.0, 50.0]


def test_soften_terrain_lifts_flat_when_colour_high():
    raw = 10.0
    soft = soften_terrain(raw, colour=90.0, landcover=None)
    assert soft > raw
    assert soft < 90.0  # does not fully replace colour


def test_soften_terrain_water_exception_floors_flat():
    soft = soften_terrain(5.0, colour=40.0, landcover=40.0, near_water=True)
    assert soft >= 55.0


def test_soften_terrain_leaves_high_relief_alone():
    assert soften_terrain(85.0, colour=40.0, landcover=40.0) == 85.0


def test_soften_terrain_lifts_flat_non_green_scenic_climates():
    soft = soften_terrain(
        18.0, colour=42.0, landcover=78.0, near_water=False, climate_id="arid_hot",
    )
    assert soft >= 48.0


def test_is_water_near_from_detail():
    assert is_water_near({"pos_label": "water", "pos_dist": 0.4}) is True
    assert is_water_near({"pos_label": "the coast", "pos_dist": 1.0}) is True
    assert is_water_near({"pos_label": "forest", "pos_dist": 0.2}) is False
    assert is_water_near(None) is False


def test_urban_park_dampened_vs_countryside_forest():
    """A park inside residential fabric must score below open countryside forest."""
    # Park + strong residential at the sample point.
    urban = {
        "pos": np.array([[54.0, -3.0, 0.35]]),  # leisure=park weight
        "neg": np.array([[54.0, -3.0, 0.88]]),  # residential
        "pos_labels": ["a park"],
        "neg_labels": ["housing"],
    }
    rural = {
        "pos": np.array([[54.0, -3.0, 0.8]]),  # forest
        "neg": np.empty((0, 3)),
        "pos_labels": ["forest"],
        "neg_labels": [],
    }
    urban_sc = landcover_scores([(54.0, -3.0)], urban)[0]
    rural_sc = landcover_scores([(54.0, -3.0)], rural)[0]
    assert rural_sc > 70.0
    assert urban_sc < 45.0
    assert urban_sc < rural_sc - 20.0


def test_expanded_positive_and_negative_tags():
    pos_vals = {(k, v) for k, v, _ in POSITIVE_TAGS}
    neg_vals = {(k, v) for k, v, _ in NEGATIVE_TAGS}
    for tag in (
        ("natural", "beach"),
        ("natural", "cliff"),
        ("natural", "wetland"),
        ("natural", "scrub"),
        ("natural", "heath"),
        ("natural", "grassland"),
        ("natural", "glacier"),
        ("natural", "sand"),
        ("natural", "dune"),
        ("natural", "bare_rock"),
        ("natural", "scree"),
        ("natural", "ridge"),
        ("natural", "volcano"),
        ("waterway", "waterfall"),
        ("tourism", "viewpoint"),
    ):
        assert tag in pos_vals
    assert ("landuse", "quarry") in neg_vals
    assert ("landuse", "landfill") in neg_vals


def test_global_biome_tag_fixtures():
    """Arid / alpine / tropical map-context tags are in the fixed global pack."""
    pos = {(k, v) for k, v, _ in POSITIVE_TAGS}
    # Arid sand/scrub
    assert ("natural", "sand") in pos
    assert ("natural", "scrub") in pos
    # Alpine bare rock / glacier
    assert ("natural", "bare_rock") in pos
    assert ("natural", "glacier") in pos
    # Tropical wood + water
    assert ("natural", "wood") in pos
    assert ("natural", "water") in pos


def test_relief_km_window_stable_across_spacing():
    """Same physical terrain pattern → similar peak relief whether sampled densely or sparsely."""
    # Dense: 1 km spacing, peak of 200 m over a short crest.
    elev_dense = [0.0, 0.0, 100.0, 200.0, 100.0, 0.0, 0.0]
    km_dense = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    # Sparse: same elevations but 2 km spacing (long-route thinning).
    elev_sparse = [0.0, 100.0, 200.0, 100.0, 0.0]
    km_sparse = [0.0, 2.0, 4.0, 6.0, 8.0]
    dense = relief_scores(elev_dense, sample_km=km_dense, window_km=4.0)
    sparse = relief_scores(elev_sparse, sample_km=km_sparse, window_km=4.0)
    assert abs(max(dense) - max(sparse)) < 15.0
    # Index-only window on sparse samples would see a smaller neighbourhood;
    # km window should still see the full crest.
    index_only = relief_scores(elev_sparse, window=1)
    assert max(sparse) >= max(index_only) - 1e-6


def test_points_from_element_samples_way_geometry():
    el = {
        "type": "way",
        "geometry": [
            {"lat": 54.0, "lon": -3.0},
            {"lat": 54.01, "lon": -3.0},
            {"lat": 54.02, "lon": -3.0},
            {"lat": 54.03, "lon": -3.0},
        ],
        "tags": {"natural": "wood"},
    }
    pts = _points_from_element(el)
    assert len(pts) >= 2
    assert pts[0] == (54.0, -3.0)
    assert pts[-1] == (54.03, -3.0)


def test_points_from_element_falls_back_to_center():
    el = {"type": "relation", "center": {"lat": 54.5, "lon": -3.2}, "tags": {}}
    assert _points_from_element(el) == [(54.5, -3.2)]


def test_downsample_keeps_endpoints():
    pts = [(float(i), 0.0) for i in range(20)]
    out = _downsample_points(pts, max_n=5)
    assert len(out) == 5
    assert out[0] == pts[0]
    assert out[-1] == pts[-1]


def test_merge_landcover_combines_features():
    a = {
        "pos": np.array([[54.0, -3.0, 1.0]]),
        "neg": np.empty((0, 3)),
        "pos_labels": ["forest"],
        "neg_labels": [],
    }
    b = {
        "pos": np.array([[54.5, -3.1, 0.8]]),
        "neg": np.array([[53.0, -2.0, 1.0]]),
        "pos_labels": ["water"],
        "neg_labels": ["industrial"],
    }
    m = merge_landcover(a, b)
    assert m is not None
    assert m["pos"].shape[0] == 2
    assert m["neg"].shape[0] == 1
    assert m["pos_labels"] == ["forest", "water"]
    assert merge_landcover(None, b) is b
    assert merge_landcover(a, None) is a


def test_landcover_scores_are_climate_aware_for_desert_sand():
    features = {
        "pos": np.array([[25.0, 30.0, 0.7]]),
        "neg": np.empty((0, 3)),
        "pos_labels": ["sand"],
        "neg_labels": [],
    }
    coord = [(25.0, 30.0)]
    arid = landcover_scores(coord, features, climate_ids=["arid_hot"])[0]
    temperate = landcover_scores(coord, features, climate_ids=["temperate_oceanic"])[0]
    assert arid > temperate


def test_landcover_details_keep_climate_context():
    features = {
        "pos": np.array([[80.0, 0.0, 1.0]]),
        "neg": np.empty((0, 3)),
        "pos_labels": ["a glacier"],
        "neg_labels": [],
    }
    detail = landcover_details([(80.0, 0.0)], features, climate_ids=["tundra_polar"])[0]
    assert detail["score"] > 50.0
    assert detail["pos_label"] == "a glacier"
    assert detail["climate"] == "tundra_polar"


def test_fetch_landcover_only_queries_missing_cells():
    """Pad-widen must not re-hit Overpass for disk/memory-warm cells."""
    from unittest.mock import patch

    from app import enrich

    empty = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
    }
    calls = {"n": 0}
    fetched = []

    def _cache_side(cell):
        calls["n"] += 1
        # First cell warm; subsequent cells cold.
        if calls["n"] == 1:
            return empty
        return None

    def _bbox(sub, endpoint=None, deadline=None):
        fetched.append(sub)
        return empty

    with patch.object(enrich, "_cell_from_cache", side_effect=_cache_side), \
         patch.object(enrich, "_fetch_landcover_bbox", side_effect=_bbox), \
         patch.object(enrich, "_cell_to_cache"):
        # ~1° span → several 0.6° cells
        result = enrich.fetch_landcover((54.0, -3.0, 55.0, -2.0), prefer_axis=None)
    assert result is not None
    assert calls["n"] >= 2
    # Only cold cells hit the network (total cells minus the one warm hit).
    assert len(fetched) == calls["n"] - 1


def test_fetch_landcover_warm_bbox_zero_overpass():
    """Second fetch of the same bbox must issue zero Overpass HTTP calls."""
    from unittest.mock import patch

    from app import enrich

    empty = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
    }
    fetched = []
    mem: dict = {}

    def _from_cache(cell):
        return mem.get(cell)

    def _to_cache(cell, part):
        mem[cell] = part

    def _bbox(sub, endpoint=None, deadline=None):
        fetched.append(sub)
        return empty

    bbox = (54.0, -3.0, 54.5, -2.5)
    with patch.object(enrich, "_cell_from_cache", side_effect=_from_cache), \
         patch.object(enrich, "_cell_to_cache", side_effect=_to_cache), \
         patch.object(enrich, "_fetch_landcover_bbox", side_effect=_bbox):
        first = enrich.fetch_landcover(bbox, prefer_axis=None)
        n_after_cold = len(fetched)
        second = enrich.fetch_landcover(bbox, prefer_axis=None)
        n_after_warm = len(fetched)

    assert first is not None and second is not None
    assert n_after_cold > 0
    assert n_after_warm == n_after_cold


def test_fetch_landcover_prefers_axis_cold_cells_first():
    """Under a tight deadline, near-axis cold tiles are fetched before corners."""
    from unittest.mock import patch

    from app import config, enrich

    empty = {
        "pos": np.empty((0, 3)),
        "neg": np.empty((0, 3)),
        "pos_labels": [],
        "neg_labels": [],
    }
    order = []

    def _from_cache(cell):
        return None

    def _bbox(sub, endpoint=None, deadline=None):
        # sub is (s, w, n, e); record tile centre for order checks.
        order.append(((sub[0] + sub[2]) / 2.0, (sub[1] + sub[3]) / 2.0))
        time.sleep(0.08)
        return empty

    # Wide bbox → several 0.6° tiles; axis along the bottom edge.
    a, b = (54.05, -3.5), (54.05, -1.5)
    deadline = time.perf_counter() + 0.20
    with patch.object(enrich, "_cell_from_cache", side_effect=_from_cache), \
         patch.object(enrich, "_cell_to_cache"), \
         patch.object(enrich, "_fetch_landcover_bbox", side_effect=_bbox), \
         patch.object(config, "LANDCOVER_TILE_WORKERS", 1), \
         patch.object(config, "LANDCOVER_MAX_TILES", 20):
        result = enrich.fetch_landcover(
            (54.0, -3.6, 55.5, -1.4),
            prefer_axis=(a, b),
            corridor_half_width_deg=0.0,
            deadline=deadline,
        )

    assert result is not None
    assert order, "expected at least one cold fetch"
    # First fetched centre should be closer to the axis than a random corner.
    first_lat, first_lng = order[0]
    assert abs(first_lat - 54.05) < 0.7


def test_fetch_landcover_bbox_retries_on_failure(monkeypatch):
    """504/timeout should rotate mirrors with backoff rather than giving up once."""
    from unittest.mock import MagicMock, patch

    from app import config, enrich

    monkeypatch.setattr(config, "LANDCOVER_FETCH_RETRIES", 2)
    monkeypatch.setattr(config, "LANDCOVER_RETRY_BACKOFF_SEC", 0.01)
    monkeypatch.setattr(config, "LANDCOVER_BATCH_GAP_SEC", 0.0)
    monkeypatch.setattr(config, "LANDCOVER_TAG_BATCH", 40)  # single batch
    monkeypatch.setattr(
        config, "OVERPASS_ENDPOINTS",
        ["https://ep-a.example/api", "https://ep-b.example/api"],
    )

    calls = {"n": 0}

    def _post(url, data=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.Timeout("boom")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "elements": [{
                "type": "node", "lat": 54.1, "lon": -2.9,
                "tags": {"natural": "wood"},
            }],
        }
        return resp

    with patch.object(enrich._session, "post", side_effect=_post):
        part = enrich._fetch_landcover_bbox((54.0, -3.0, 54.6, -2.4))

    assert part is not None
    assert part["pos"].shape[0] >= 1
    assert calls["n"] >= 3
    assert part.get("complete") is True


def test_elev_disk_cache_survives_memory_clear(tmp_path, monkeypatch):
    """Elevation written to disk is reloaded after in-process cache eviction."""
    from unittest.mock import patch

    from app import config, enrich

    monkeypatch.setattr(config, "ELEV_CACHE_DIR", tmp_path)
    enrich._ELEV_CACHE.clear()

    key = (54.6000, -3.1400)
    enrich._elev_cache_set(key, 123.5)
    assert (tmp_path / "e_54.6000_-3.1400.npz").exists()

    enrich._ELEV_CACHE.clear()
    with patch.object(enrich, "_elev_open_meteo") as m1, \
         patch.object(enrich, "_elev_opentopo") as m2:
        vals = enrich.elevation_batch([(54.6, -3.14)])
        m1.assert_not_called()
        m2.assert_not_called()
    assert vals == [123.5]
