#!/usr/bin/env python3
"""Smoke-test OSRM connectivity (public or self-hosted via SCENIC_OSRM_URL)."""
from __future__ import annotations

import sys

from app import config
from app.roads import get_osrm_routes


def main() -> int:
    a = (54.6009, -3.1371)  # Keswick
    b = (54.4287, -2.9613)  # Ambleside
    print(f"OSRM_URL={config.OSRM_URL}")
    try:
        routes = get_osrm_routes(a, b, alternatives=1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if not routes:
        print("FAIL: no routes", file=sys.stderr)
        return 1
    rt = routes[0]
    print(
        f"OK: {rt['distance_km']:.1f} km, {rt['duration_min']:.1f} min, "
        f"{len(rt['coords'])} geometry points"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
