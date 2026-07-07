"""FastAPI backend + static frontend for the Scenic Route Planner."""
from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import json

from . import config, store, graph, scoring, roads
from . import saved, catalog_data, exports, features

app = FastAPI(title="Scenic Route Planner", version="2.0")

store.init_db()

# Feature routers (built as standalone modules).
app.include_router(saved.router)
app.include_router(catalog_data.router)
app.include_router(exports.router)
app.include_router(features.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "cells": store.count_cells()}


@app.get("/api/meta")
def meta():
    return {
        "grid": store.get_meta("grid"),
        "cell_count": store.count_cells(),
        "tile_zoom": config.TILE_ZOOM,
        "downscale": config.DOWNSCALE,
    }


@app.get("/api/cells")
def cells(
    min_lat: float | None = None,
    min_lng: float | None = None,
    max_lat: float | None = None,
    max_lng: float | None = None,
):
    """Scenic scores for the heatmap. Optionally filtered by bbox."""
    if None not in (min_lat, min_lng, max_lat, max_lng):
        rows = store.cells_in_bbox(min_lat, min_lng, max_lat, max_lng)
    else:
        rows = store.all_cells()
    return {
        "count": len(rows),
        "cells": [
            {
                "id": r["id"],
                "lat": r["lat"], "lng": r["lng"],
                "min_lat": r["min_lat"], "min_lng": r["min_lng"],
                "max_lat": r["max_lat"], "max_lng": r["max_lng"],
                "score": round(r["score"], 1),
                "green": round(r["green"], 3),
                "blue": round(r["blue"], 3),
                "grey": round(r["grey"], 3),
            }
            for r in rows
        ],
    }


@app.get("/api/score")
def score_point(lat: float, lng: float, source: str = "esri"):
    """Score an arbitrary coordinate on demand (live pipeline)."""
    s = scoring.score_location(lat, lng, source=source)
    return {"lat": lat, "lng": lng, **s.to_dict()}


@app.get("/api/route")
def route(
    from_lat: float = Query(...),
    from_lng: float = Query(...),
    to_lat: float = Query(...),
    to_lng: float = Query(...),
    preference: float = Query(0.7, ge=0.0, le=1.0),
    profile: str = Query("balanced"),
    avoid_motorways: bool = Query(False),
    min_scenic: float = Query(0.0, ge=0.0, le=100.0),
    explore_all: bool = Query(False),
):
    """Scenic route on real roads (OSRM) with enriched, profile-driven scoring."""
    prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
    weights = prof["weights"] if prof else None
    detour = prof["detour_factor"] if prof else None
    try:
        result = roads.plan(
            (from_lat, from_lng), (to_lat, to_lng), preference,
            weights=weights, detour_factor=detour,
            profile_id=(prof["id"] if prof else None),
            avoid_motorways=avoid_motorways,
            min_scenic=min_scenic,
            explore_all=explore_all,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Routing failed: {exc}"}, status_code=502)

    # Log to history (best-effort).
    try:
        c = result["chosen"]
        store.log_history(from_lat, from_lng, to_lat, to_lng, preference,
                          result.get("profile"), c["distance_km"],
                          c["duration_min"], c["avg_scenic_score"])
    except Exception:
        pass
    return result


@app.get("/api/route/stream")
def route_stream(
    from_lat: float = Query(...),
    from_lng: float = Query(...),
    to_lat: float = Query(...),
    to_lng: float = Query(...),
    preference: float = Query(0.7, ge=0.0, le=1.0),
    profile: str = Query("balanced"),
    avoid_motorways: bool = Query(False),
    min_scenic: float = Query(0.0, ge=0.0, le=100.0),
    explore_all: bool = Query(False),
):
    """Same planner as /api/route, but streams live search progress as Server-
    Sent Events: each candidate route is emitted as it is found and scored, plus
    phase/round updates, ending with a `done` event carrying the full result."""
    prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
    weights = prof["weights"] if prof else None
    detour = prof["detour_factor"] if prof else None

    def gen():
        try:
            for ev in roads.plan_events(
                (from_lat, from_lng), (to_lat, to_lng), preference,
                weights=weights, detour_factor=detour,
                profile_id=(prof["id"] if prof else None),
                avoid_motorways=avoid_motorways, min_scenic=min_scenic,
                explore_all=explore_all,
            ):
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") == "done":
                    try:
                        c = ev["result"]["chosen"]
                        store.log_history(from_lat, from_lng, to_lat, to_lng,
                                          preference, ev["result"].get("profile"),
                                          c["distance_km"], c["duration_min"],
                                          c["avg_scenic_score"])
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/route/compare")
def route_compare(
    from_lat: float, from_lng: float, to_lat: float, to_lng: float,
    profile: str = "balanced",
):
    """Fastest vs. most-scenic on real roads."""
    prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
    weights = prof["weights"] if prof else None
    detour = prof["detour_factor"] if prof else None
    try:
        fastest = roads.plan((from_lat, from_lng), (to_lat, to_lng),
                             preference=0.0, weights=weights, detour_factor=detour)
        scenic = roads.plan((from_lat, from_lng), (to_lat, to_lng),
                            preference=1.0, weights=weights, detour_factor=detour)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Routing failed: {exc}"}, status_code=502)
    return {"fastest": fastest["chosen"], "scenic": scenic["chosen"]}


# Serve the frontend at root.
app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
