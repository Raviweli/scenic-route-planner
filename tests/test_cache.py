"""In-process cache TTL / size helpers."""
from __future__ import annotations

from collections import OrderedDict
from unittest.mock import patch

from app import enrich


def test_cache_evicts_when_over_max():
    cache: OrderedDict = OrderedDict()
    with patch.object(enrich.config, "CACHE_MAX_ENTRIES", 2), \
         patch.object(enrich.config, "CACHE_TTL_SEC", 3600.0):
        enrich._cache_set(cache, "a", 1)
        enrich._cache_set(cache, "b", 2)
        enrich._cache_set(cache, "c", 3)
        assert "a" not in cache
        assert enrich._cache_get(cache, "b") == 2
        assert enrich._cache_get(cache, "c") == 3


def test_cache_expires_after_ttl():
    cache: OrderedDict = OrderedDict()
    with patch.object(enrich.config, "CACHE_MAX_ENTRIES", 10), \
         patch.object(enrich.config, "CACHE_TTL_SEC", 0.0):
        enrich._cache_set(cache, "a", 99)
        # TTL 0 => immediately stale on next get
        assert enrich._cache_get(cache, "a") is None
