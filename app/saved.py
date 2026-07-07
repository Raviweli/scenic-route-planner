"""Saved route and route history API endpoints."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .store import connect


router = APIRouter()


class SavedRouteCreate(BaseModel):
    name: str
    notes: str | None = None
    tags: list[str] | None = None
    favourite: bool = False
    rating: int = Field(default=0, ge=0, le=5)
    from_lat: float
    from_lng: float
    to_lat: float
    to_lng: float
    preference: float = 0.7
    profile: str = "balanced"
    distance_km: float | None = None
    duration_min: float | None = None
    scenic_score: float | None = None
    geojson: Any | None = None


class SavedRoutePatch(BaseModel):
    name: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    favourite: bool | None = None
    rating: int | None = Field(default=None, ge=0, le=5)


class RouteHistoryCreate(BaseModel):
    from_lat: float
    from_lng: float
    to_lat: float
    to_lng: float
    preference: float
    profile: str
    distance_km: float
    duration_min: float
    scenic_score: float


def _model_values(model: BaseModel, *, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)


def _dump_tags(tags: list[str] | None) -> str:
    if not tags:
        return ""
    return ",".join(tag.strip() for tag in tags if tag.strip())


def _load_tags(tags: str | None) -> list[str]:
    if not tags:
        return []
    return [tag for tag in (part.strip() for part in tags.split(",")) if tag]


def _load_geojson(value: str | None) -> Any | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _serialize_route(row) -> dict[str, Any]:
    route = dict(row)
    route["favourite"] = bool(route.get("favourite"))
    route["tags"] = _load_tags(route.get("tags"))
    route["geojson"] = _load_geojson(route.get("geojson"))
    return route


def _get_route_or_404(route_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM saved_routes WHERE id = ?", (route_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return _serialize_route(row)


@router.post("/api/routes")
def create_route(route: SavedRouteCreate) -> dict[str, int]:
    values = _model_values(route)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO saved_routes
                (name, notes, tags, favourite, rating, from_lat, from_lng, to_lat, to_lng,
                 preference, profile, distance_km, duration_min, scenic_score, geojson)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["name"],
                values.get("notes") or "",
                _dump_tags(values.get("tags")),
                1 if values.get("favourite") else 0,
                values.get("rating", 0),
                values["from_lat"],
                values["from_lng"],
                values["to_lat"],
                values["to_lng"],
                values.get("preference", 0.7),
                values.get("profile") or "balanced",
                values.get("distance_km"),
                values.get("duration_min"),
                values.get("scenic_score"),
                json.dumps(values.get("geojson")) if values.get("geojson") is not None else None,
            ),
        )
        route_id = cursor.lastrowid
    return {"id": int(route_id)}


@router.get("/api/routes")
def list_routes(
    favourite: bool | None = None,
    tag: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if favourite is not None:
        clauses.append("favourite = ?")
        params.append(1 if favourite else 0)
    if q:
        clauses.append("(name LIKE ? OR notes LIKE ?)")
        pattern = f"%{q}%"
        params.extend([pattern, pattern])

    sql = "SELECT * FROM saved_routes"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, created_at DESC, id DESC"

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    routes = [_serialize_route(row) for row in rows]
    if tag:
        routes = [route for route in routes if tag in route["tags"]]
    return {"count": len(routes), "routes": routes}


@router.get("/api/routes/{route_id}")
def get_route(route_id: int) -> dict[str, Any]:
    return _get_route_or_404(route_id)


@router.patch("/api/routes/{route_id}")
def update_route(route_id: int, patch: SavedRoutePatch) -> dict[str, Any]:
    values = _model_values(patch, exclude_unset=True)
    if not values:
        return _get_route_or_404(route_id)

    assignments: list[str] = []
    params: list[Any] = []
    for field, value in values.items():
        if value is None:
            continue
        if field == "tags":
            value = _dump_tags(value)
        elif field == "favourite":
            value = 1 if value else 0
        assignments.append(f"{field} = ?")
        params.append(value)

    if not assignments:
        return _get_route_or_404(route_id)

    params.append(route_id)
    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE saved_routes SET {', '.join(assignments)}, updated_at = datetime('now') WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Route not found")
    return _get_route_or_404(route_id)


@router.delete("/api/routes/{route_id}")
def delete_route(route_id: int) -> dict[str, int]:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM saved_routes WHERE id = ?", (route_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Route not found")
    return {"deleted": route_id}


@router.post("/api/routes/{route_id}/favourite")
def toggle_favourite(route_id: int) -> dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE saved_routes
            SET favourite = CASE favourite WHEN 1 THEN 0 ELSE 1 END,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (route_id,),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Route not found")
    return _get_route_or_404(route_id)


@router.get("/api/history")
def list_history(limit: int = Query(default=50, ge=1)) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM route_history ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    history = [dict(row) for row in rows]
    return {"count": len(history), "history": history}


@router.post("/api/history")
def create_history(entry: RouteHistoryCreate) -> dict[str, int]:
    values = _model_values(entry)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO route_history
                (from_lat, from_lng, to_lat, to_lng, preference, profile,
                 distance_km, duration_min, scenic_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["from_lat"],
                values["from_lng"],
                values["to_lat"],
                values["to_lng"],
                values["preference"],
                values["profile"],
                values["distance_km"],
                values["duration_min"],
                values["scenic_score"],
            ),
        )
        history_id = cursor.lastrowid
    return {"id": int(history_id)}


@router.delete("/api/history")
def clear_history() -> dict[str, int]:
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM route_history").fetchone()["n"]
        conn.execute("DELETE FROM route_history")
    return {"cleared": int(count)}
