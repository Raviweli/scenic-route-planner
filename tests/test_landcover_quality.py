"""Landcover usability / omit-vs-neutral honesty."""
from __future__ import annotations

import numpy as np

from app.enrich import landcover_is_usable, merge_landcover


def _feats(pos=None, neg=None, truncated=False):
    return {
        "pos": pos if pos is not None else np.empty((0, 3)),
        "neg": neg if neg is not None else np.empty((0, 3)),
        "pos_labels": ["forest"] * (0 if pos is None else len(pos)),
        "neg_labels": [],
        "truncated": truncated,
    }


def test_truncated_with_influence_still_usable():
    """Soft truncation: keep map context when ≥15% of samples feel fetched tiles."""
    feats = _feats(pos=np.array([[54.0, -3.0, 1.0]]), truncated=True)
    assert landcover_is_usable(feats, [(54.0, -3.0), (54.001, -3.001)]) is True


def test_truncated_without_influence_not_usable():
    feats = _feats(pos=np.array([[10.0, 10.0, 1.0]]), truncated=True)
    assert landcover_is_usable(feats, [(54.0, -3.0)] * 10) is False


def test_empty_features_not_usable():
    assert landcover_is_usable(_feats(), [(54.0, -3.0)]) is False
    assert landcover_is_usable(None) is False


def test_influential_features_usable():
    feats = _feats(pos=np.array([[54.0, -3.0, 1.0]]), truncated=False)
    assert landcover_is_usable(feats, [(54.0, -3.0), (54.001, -3.001)]) is True


def test_far_away_features_not_usable():
    # Feature hundreds of km away → no sample influenced → omit.
    feats = _feats(pos=np.array([[10.0, 10.0, 1.0]]), truncated=False)
    assert landcover_is_usable(feats, [(54.0, -3.0)] * 10) is False


def test_merge_preserves_truncated_flag():
    a = _feats(pos=np.array([[54.0, -3.0, 1.0]]), truncated=False)
    b = _feats(pos=np.array([[54.5, -3.1, 0.8]]), truncated=True)
    m = merge_landcover(a, b)
    assert m["truncated"] is True
    assert m.get("landcover_incomplete") is True


def test_landcover_max_tiles_scales_with_distance():
    from app.enrich import landcover_max_tiles
    from app import config

    assert landcover_max_tiles(0) == config.LANDCOVER_MAX_TILES
    assert landcover_max_tiles(None) == config.LANDCOVER_MAX_TILES
    mid = landcover_max_tiles(250)
    assert config.LANDCOVER_MAX_TILES < mid < config.LANDCOVER_MAX_TILES_LONG
    assert landcover_max_tiles(500) == config.LANDCOVER_MAX_TILES_LONG
    assert landcover_max_tiles(2000) == config.LANDCOVER_MAX_TILES_LONG
