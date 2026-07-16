"""Optional cell-lattice routing for precomputed heatmap grids.

Not used by live FastAPI route endpoints — those call OSRM via `app/roads.py`.
Kept for `scripts/build_grid.py` / `GET /api/cells` tooling only
(see docs/API_SURFACE.md).

Builds an 8-connected lattice over scored cells. Edge cost blends distance
with scenic quality:

    cost = distance_km * (1 + preference * DETOUR_FACTOR * (1 - scenic/100))
"""
from __future__ import annotations

import math
from functools import lru_cache

import networkx as nx

from . import config, store


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_NEIGHBOURS = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


@lru_cache(maxsize=1)
def build_graph() -> nx.Graph:
    """Build the lattice graph from stored cells. Cached until process restart."""
    cells = store.all_cells()
    by_rc = {(c["row"], c["col"]): c for c in cells}
    g = nx.Graph()
    for c in cells:
        g.add_node(c["id"], lat=c["lat"], lng=c["lng"],
                   row=c["row"], col=c["col"], score=c["score"])
    for c in cells:
        for dr, dc in _NEIGHBOURS:
            nb = by_rc.get((c["row"] + dr, c["col"] + dc))
            if nb is None or g.has_edge(c["id"], nb["id"]):
                continue
            dist = haversine_km(c["lat"], c["lng"], nb["lat"], nb["lng"])
            scenic = (c["score"] + nb["score"]) / 2.0
            g.add_edge(c["id"], nb["id"], dist=dist, scenic=scenic)
    return g


def invalidate_graph() -> None:
    build_graph.cache_clear()


def nearest_node(g: nx.Graph, lat: float, lng: float) -> str | None:
    best, best_d = None, float("inf")
    for node, data in g.nodes(data=True):
        d = (data["lat"] - lat) ** 2 + (data["lng"] - lng) ** 2
        if d < best_d:
            best, best_d = node, d
    return best


def _edge_cost(scenic: float, dist: float, preference: float) -> float:
    penalty = config.DETOUR_FACTOR * (1.0 - scenic / 100.0)
    return dist * (1.0 + preference * penalty)


def route(from_lat, from_lng, to_lat, to_lng, preference: float) -> dict:
    """Return a scenic route between two coordinates."""
    preference = max(0.0, min(1.0, float(preference)))
    g = build_graph()
    if g.number_of_nodes() == 0:
        return {"error": "No scored grid available. Run scripts/build_grid.py first."}

    src = nearest_node(g, from_lat, from_lng)
    dst = nearest_node(g, to_lat, to_lng)
    if src is None or dst is None:
        return {"error": "Could not snap endpoints to the grid."}
    if src == dst:
        return {"error": "Start and end snap to the same cell; pick points further apart."}

    def weight(u, v, data):
        return _edge_cost(data["scenic"], data["dist"], preference)

    try:
        path = nx.shortest_path(g, src, dst, weight=weight)
    except nx.NetworkXNoPath:
        return {"error": "No path between the selected points on the scored grid."}

    coords, scenics, total_km = [], [], 0.0
    for i, node in enumerate(path):
        d = g.nodes[node]
        coords.append([d["lng"], d["lat"]])  # GeoJSON order
        if i > 0:
            e = g.edges[path[i - 1], node]
            total_km += e["dist"]
            scenics.append(e["scenic"])

    avg_scenic = sum(scenics) / len(scenics) if scenics else 0.0
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "preference": preference,
            "distance_km": round(total_km, 2),
            "avg_scenic_score": round(avg_scenic, 1),
            "num_segments": len(scenics),
            "from": [from_lat, from_lng],
            "to": [to_lat, to_lng],
        },
    }


def route_variants(from_lat, from_lng, to_lat, to_lng) -> dict:
    """Return fastest vs. most-scenic routes for comparison."""
    fastest = route(from_lat, from_lng, to_lat, to_lng, preference=0.0)
    scenic = route(from_lat, from_lng, to_lat, to_lng, preference=1.0)
    return {"fastest": fastest, "scenic": scenic}
