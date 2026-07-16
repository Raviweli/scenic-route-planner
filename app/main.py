"""FastAPI backend + static frontend for the Scenic Route Planner."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Annotated, Iterator
from urllib.parse import parse_qsl, urlparse

import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config, store, scoring, roads, enrich
from . import saved, catalog_data, exports, features, field_route

# Controllable logging for local debugging (default INFO).
_level_name = os.environ.get("SCENIC_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _level_name, logging.INFO),
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

_API_KEY_IN_TEXT = re.compile(r"([?&]api_key=)([^&\s\"]+)", re.IGNORECASE)
_API_KEY_IN_QS = re.compile(r"(^|[?&])(api_key=)([^&]*)", re.IGNORECASE)


def redact_api_key_text(text: str) -> str:
    """Replace raw ``api_key`` query values with ``***`` in log/URL strings."""
    if not text or "api_key=" not in text.lower():
        return text
    # Full URLs use ?/&; bare query strings may start with api_key=
    out = _API_KEY_IN_TEXT.sub(r"\1***", text)
    if text.lower().startswith("api_key="):
        out = _API_KEY_IN_QS.sub(
            lambda m: f"{m.group(1)}{m.group(2)}***" if m.group(3) else m.group(0),
            out,
        )
    return out


def redact_query_string(qs: bytes) -> bytes:
    """Redact ``api_key`` values in an ASGI query_string (keep other params)."""
    if not qs or b"api_key=" not in qs.lower():
        return qs
    raw = qs.decode("latin-1")
    redacted = _API_KEY_IN_QS.sub(
        lambda m: f"{m.group(1)}{m.group(2)}***" if m.group(3) else m.group(0),
        raw,
    )
    return redacted.encode("latin-1")


class _RedactApiKeyLogFilter(logging.Filter):
    """Ensure uvicorn access (and related) logs never print raw api_key values."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_api_key_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_api_key_text(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_api_key_text(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


def _install_api_key_log_redaction() -> None:
    filt = _RedactApiKeyLogFilter()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn", __name__):
        logging.getLogger(name).addFilter(filt)


_install_api_key_log_redaction()

app = FastAPI(title="Scenic Route Planner", version="2.0")

store.init_db()

# Feature routers (built as standalone modules).
app.include_router(saved.router)
app.include_router(catalog_data.router)
app.include_router(exports.router)
app.include_router(features.router)

Lat = Annotated[float, Query(ge=-90.0, le=90.0)]
Lng = Annotated[float, Query(ge=-180.0, le=180.0)]

# Paths that amplify upstream APIs or mutate local state — gated in public mode.
_PROTECTED_PREFIXES = (
    "/api/route",
    "/api/geocode",
    "/api/routes",
    "/api/history",
    "/api/export",
    "/api/score",
)

_plan_slots = threading.Semaphore(config.MAX_INFLIGHT_PLANS)
_inflight_lock = threading.Lock()
_inflight_plans = 0


def _osrm_mode_label() -> str:
    return "byo" if os.environ.get("SCENIC_OSRM_URL", "").strip() else "public_demo"


def _log_plan_finished(result: dict) -> None:
    """Structured plan finish line — no secrets, no full coordinate dumps."""
    log.info(
        "plan_finished elapsed_ms=%s budget_exhausted=%s budget_reasons=%s osrm_mode=%s",
        result.get("elapsed_ms"),
        result.get("budget_exhausted"),
        result.get("budget_reasons") or [],
        _osrm_mode_label(),
    )


@contextmanager
def _plan_slot() -> Iterator[bool]:
    """Acquire a plan concurrency slot; yields False if the host is at capacity."""
    global _inflight_plans
    acquired = _plan_slots.acquire(blocking=False)
    if acquired:
        with _inflight_lock:
            _inflight_plans += 1
    try:
        yield acquired
    finally:
        if acquired:
            with _inflight_lock:
                _inflight_plans -= 1
            _plan_slots.release()


def _acquire_plan_slot() -> bool:
    """Non-context acquire for streaming endpoints (pair with ``_release_plan_slot``)."""
    global _inflight_plans
    acquired = _plan_slots.acquire(blocking=False)
    if acquired:
        with _inflight_lock:
            _inflight_plans += 1
    return acquired


def _release_plan_slot() -> None:
    global _inflight_plans
    with _inflight_lock:
        _inflight_plans -= 1
    _plan_slots.release()


def _busy_response():
    return JSONResponse(
        {
            "error": "Too many plans in flight. Try again in a moment.",
            "max_inflight": config.MAX_INFLIGHT_PLANS,
        },
        status_code=503,
    )


def _compare_leg(result: dict, label: str) -> dict:
    """Route card payload for compare endpoints with leg-level motorway metadata."""
    leg = dict(result["chosen"])
    leg["_label"] = label
    leg["avoid_motorways"] = result.get("avoid_motorways")
    leg["motorway_avoid_met"] = result.get("motorway_avoid_met")
    leg["motorway_avoid_reason"] = result.get("motorway_avoid_reason")
    return leg


def _compare_meta(result: dict, *, field_meta: dict | None = None) -> dict:
    return {
        "budget_exhausted": result.get("budget_exhausted"),
        "budget_reasons": result.get("budget_reasons"),
        "signals": result.get("signals"),
        "min_scenic_met": result.get("min_scenic_met"),
        "min_scenic": result.get("min_scenic"),
        "avoid_motorways": result.get("avoid_motorways"),
        "motorway_avoid_met": result.get("motorway_avoid_met"),
        "motorway_avoid_reason": result.get("motorway_avoid_reason"),
        "field_meta": field_meta,
    }


def _parse_vias(via: list[str] | None) -> list[tuple[float, float]]:
    """Parse repeatable ``via=lat,lng`` query params into coordinate pairs."""
    out: list[tuple[float, float]] = []
    for item in via or []:
        parts = str(item).split(",")
        if len(parts) != 2:
            continue
        try:
            lat, lng = float(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            continue
        if -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0:
            out.append((lat, lng))
    return out[:8]  # soft cap


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _extract_api_key(request: Request) -> str:
    """Accept X-API-Key, Bearer, or ``api_key`` query (EventSource cannot set headers)."""
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Prefer the pre-redaction value stashed by RedactApiKeyQueryMiddleware.
    stashed = getattr(request.state, "api_key_query", None)
    if stashed:
        return str(stashed).strip()
    q = request.query_params.get("api_key", "").strip()
    if q and q != "***":
        return q
    return ""


class RedactApiKeyQueryMiddleware:
    """ASGI middleware: stash ``api_key`` then redact it in ``scope`` for access logs.

    EventSource clients must pass ``api_key`` on the query string. Uvicorn's access
    logger prints that URL — redact after copying the real value onto ``scope``
    state so auth still works and logs never contain the raw secret.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            qs = scope.get("query_string", b"") or b""
            if b"api_key=" in qs.lower():
                scope = dict(scope)
                raw = qs.decode("latin-1")
                stashed_key = ""
                for k, v in parse_qsl(raw, keep_blank_values=True):
                    if k.lower() == "api_key" and v:
                        stashed_key = v
                        break
                state = dict(scope.get("state") or {})
                if stashed_key:
                    state["api_key_query"] = stashed_key
                scope["state"] = state
                scope["query_string"] = redact_query_string(qs)
        await self.app(scope, receive, send)


class PublicGateMiddleware(BaseHTTPMiddleware):
    """Require API key + per-IP rate limit when not on loopback / SCENIC_PUBLIC=1."""

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        window = 60.0
        q = self._hits[ip]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= config.RATE_LIMIT_PER_MIN:
            return False
        q.append(now)
        return True

    async def dispatch(self, request: Request, call_next):
        if not config.PUBLIC_MODE:
            return await call_next(request)

        path = request.url.path
        protected = any(
            path == p or path.startswith(p + "/") for p in _PROTECTED_PREFIXES
        )
        if not protected:
            return await call_next(request)

        if not config.API_KEY:
            return JSONResponse(
                {"error": "Public mode requires SCENIC_API_KEY."},
                status_code=503,
            )
        if _extract_api_key(request) != config.API_KEY:
            return JSONResponse(
                {
                    "error": (
                        "Unauthorized. Provide X-API-Key, Authorization: Bearer, "
                        "or api_key query param."
                    ),
                },
                status_code=401,
            )
        ip = _client_ip(request)
        if not self._rate_ok(ip):
            return JSONResponse(
                {"error": "Rate limit exceeded. Try again shortly."},
                status_code=429,
            )
        return await call_next(request)


# Outer ASGI redact runs first on the way in (last added = outermost in Starlette).
app.add_middleware(PublicGateMiddleware)
app.add_middleware(RedactApiKeyQueryMiddleware)


def _osrm_probe() -> dict:
    """Report public vs BYO OSRM; ping only when SCENIC_OSRM_URL is set."""
    custom = os.environ.get("SCENIC_OSRM_URL", "").strip()
    if not custom:
        return {"osrm_mode": "public_demo", "osrm_configured": False}
    # Cheap nearest-style ping: tiny coords — any non-5xx means the router is up.
    probe = custom.replace("{coords}", "0.0,0.0;0.001,0.001")
    reachable = False
    try:
        parsed = urlparse(probe)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            r = requests.get(probe, timeout=2.5, params={"overview": "false"})
            reachable = r.status_code < 500
    except Exception:  # noqa: BLE001
        reachable = False
    return {
        "osrm_mode": "byo",
        "osrm_configured": True,
        "osrm_reachable": reachable,
    }


@app.get("/api/health")
def health():
    """Fast local sanity check — no upstream pings unless BYO OSRM is configured."""
    with _inflight_lock:
        inflight = _inflight_plans
    body = {
        "status": "ok",
        "cells": store.count_cells(),
        "cache_entries": enrich.cache_entry_counts(),
        "public_mode": config.PUBLIC_MODE,
        "max_inflight_plans": config.MAX_INFLIGHT_PLANS,
        "inflight_plans": inflight,
        "workers": 1,  # friends path: single process; multi-worker breaks SQLite/caps
    }
    body.update(_osrm_probe())
    return body


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
    """Experimental: scenic scores for an optional precomputed heatmap grid."""
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
def score_point(
    lat: Lat,
    lng: Lng,
    source: str = "esri",
):
    """Score an arbitrary coordinate on demand (live pipeline)."""
    s = scoring.score_location(lat, lng, source=source)
    return {"lat": lat, "lng": lng, **s.to_dict()}


@app.get("/api/geocode")
def geocode(q: str = Query(..., min_length=1, max_length=200)):
    """Proxy Nominatim place search with an identifying User-Agent (localhost use)."""
    try:
        r = requests.get(
            config.NOMINATIM_URL,
            params={"format": "json", "limit": 5, "q": q},
            headers={
                "User-Agent": "ScenicRoutePlanner/1.0 (local MVP; contact via repo)",
                "Accept-Language": "en",
            },
            timeout=config.HTTP_TIMEOUT,
        )
        r.raise_for_status()
        arr = r.json()
    except Exception:  # noqa: BLE001
        log.warning("geocode_failed", exc_info=True)
        return JSONResponse({"error": "Geocoding failed."}, status_code=502)
    if not arr:
        return {"results": []}
    return {
        "results": [{
            "lat": float(hit["lat"]),
            "lng": float(hit["lon"]),
            "display_name": hit.get("display_name", q),
            "name": (hit.get("name") or hit.get("display_name") or q),
        } for hit in arr[:5]]
    }


@app.get("/api/route")
def route(
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    preference: float = Query(0.7, ge=0.0, le=1.0),
    profile: str = Query("balanced"),
    avoid_motorways: bool = Query(False),
    min_scenic: float = Query(0.0, ge=0.0, le=100.0),
    explore_all: bool = Query(False),
    time_budget: bool = Query(True),
    via: list[str] | None = Query(None),
):
    """Scenic route on real roads (OSRM) with enriched, profile-driven scoring."""
    with _plan_slot() as ok:
        if not ok:
            return _busy_response()
        prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
        weights = prof["weights"] if prof else None
        detour = prof["detour_factor"] if prof else None
        vias = _parse_vias(via)
        try:
            result = roads.plan(
                (from_lat, from_lng), (to_lat, to_lng), preference,
                weights=weights, detour_factor=detour,
                profile_id=(prof["id"] if prof else None),
                avoid_motorways=avoid_motorways,
                min_scenic=min_scenic,
                explore_all=explore_all,
                time_budget=time_budget,
                vias=vias,
            )
        except Exception:  # noqa: BLE001
            log.warning("route_failed", exc_info=True)
            return JSONResponse({"error": "Routing failed."}, status_code=502)

        _log_plan_finished(result)
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
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    preference: float = Query(0.7, ge=0.0, le=1.0),
    profile: str = Query("balanced"),
    avoid_motorways: bool = Query(False),
    min_scenic: float = Query(0.0, ge=0.0, le=100.0),
    explore_all: bool = Query(False),
    time_budget: bool = Query(True),
    via: list[str] | None = Query(None),
):
    """Same planner as /api/route, but streams live search progress as Server-
    Sent Events: each candidate route is emitted as it is found and scored, plus
    phase/round updates, ending with a `done` event carrying the full result."""
    acquired = _acquire_plan_slot()
    if not acquired:
        return _busy_response()

    prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
    weights = prof["weights"] if prof else None
    detour = prof["detour_factor"] if prof else None
    vias = _parse_vias(via)

    def gen():
        try:
            for ev in roads.plan_events(
                (from_lat, from_lng), (to_lat, to_lng), preference,
                weights=weights, detour_factor=detour,
                profile_id=(prof["id"] if prof else None),
                avoid_motorways=avoid_motorways, min_scenic=min_scenic,
                explore_all=explore_all, time_budget=time_budget, vias=vias,
            ):
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") == "done":
                    try:
                        _log_plan_finished(ev["result"])
                        c = ev["result"]["chosen"]
                        store.log_history(from_lat, from_lng, to_lat, to_lng,
                                          preference, ev["result"].get("profile"),
                                          c["distance_km"], c["duration_min"],
                                          c["avg_scenic_score"])
                    except Exception:
                        pass
        except Exception:  # noqa: BLE001
            log.warning("route_stream_failed", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Routing failed.'})}\n\n"
        finally:
            _release_plan_slot()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/route/field")
def route_field(
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    preference: float = Query(0.7, ge=0.0, le=1.0),
    profile: str = Query("balanced"),
    time_budget: bool = Query(True),
    include_grid: bool = Query(False),
    avoid_motorways: bool = Query(False),
):
    """Scenic field planner: heatmap → green corridors → OSRM via vias → road score."""
    with _plan_slot() as ok:
        if not ok:
            return _busy_response()
        prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
        weights = prof["weights"] if prof else None
        try:
            result = field_route.plan_field(
                (from_lat, from_lng), (to_lat, to_lng), preference,
                profile_id=(prof["id"] if prof else None),
                weights=weights,
                time_budget=time_budget,
                include_grid=include_grid,
                avoid_motorways=avoid_motorways,
            )
        except Exception:  # noqa: BLE001
            log.warning("route_field_failed", exc_info=True)
            return JSONResponse({"error": "Field routing failed."}, status_code=502)
        if result.get("error"):
            return JSONResponse({"error": result["error"]}, status_code=502)
        _log_plan_finished(result)
        try:
            c = result["chosen"]
            store.log_history(from_lat, from_lng, to_lat, to_lng, preference,
                              result.get("profile"), c["distance_km"],
                              c["duration_min"], c["avg_scenic_score"])
        except Exception:
            pass
        return result


@app.get("/api/route/field/stream")
def route_field_stream(
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    preference: float = Query(0.7, ge=0.0, le=1.0),
    profile: str = Query("balanced"),
    time_budget: bool = Query(True),
    include_grid: bool = Query(False),
    avoid_motorways: bool = Query(False),
):
    """SSE field planner: heatmap / corridors / OSRM / score + optional cell overlay."""
    acquired = _acquire_plan_slot()
    if not acquired:
        return _busy_response()

    prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
    weights = prof["weights"] if prof else None

    def gen():
        try:
            for ev in field_route.plan_field_events(
                (from_lat, from_lng), (to_lat, to_lng), preference,
                profile_id=(prof["id"] if prof else None),
                weights=weights,
                time_budget=time_budget,
                include_grid=include_grid,
                avoid_motorways=avoid_motorways,
            ):
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("type") == "done":
                    try:
                        _log_plan_finished(ev["result"])
                        c = ev["result"]["chosen"]
                        store.log_history(from_lat, from_lng, to_lat, to_lng,
                                          preference, ev["result"].get("profile"),
                                          c["distance_km"], c["duration_min"],
                                          c["avg_scenic_score"])
                    except Exception:
                        pass
                elif ev.get("type") == "error":
                    return
        except Exception:  # noqa: BLE001
            log.warning("route_field_stream_failed", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Field routing failed.'})}\n\n"
        finally:
            _release_plan_slot()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/route/compare")
def route_compare(
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    profile: str = Query("balanced"),
    preference: float = Query(1.0, ge=0.0, le=1.0),
    avoid_motorways: bool = Query(False),
    min_scenic: float = Query(0.0, ge=0.0, le=100.0),
    explore_all: bool = Query(False),
    time_budget: bool = Query(True),
    via: list[str] | None = Query(None),
):
    """Fastest vs most-scenic on real roads (same planner knobs as plan/stream).

    Fastest always uses preference=0. Scenic uses the requested ``preference``
    (default 1.0) plus avoid_motorways / min_scenic / explore_all.
    """
    with _plan_slot() as ok:
        if not ok:
            return _busy_response()
        prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
        weights = prof["weights"] if prof else None
        detour = prof["detour_factor"] if prof else None
        pid = prof["id"] if prof else None
        vias = _parse_vias(via)
        try:
            fastest = roads.plan(
                (from_lat, from_lng), (to_lat, to_lng),
                preference=0.0, weights=weights, detour_factor=detour,
                profile_id=pid, avoid_motorways=avoid_motorways,
                min_scenic=0.0, explore_all=False, time_budget=time_budget,
                vias=vias,
            )
            scenic = roads.plan(
                (from_lat, from_lng), (to_lat, to_lng),
                preference=preference, weights=weights, detour_factor=detour,
                profile_id=pid, avoid_motorways=avoid_motorways,
                min_scenic=min_scenic, explore_all=explore_all,
                time_budget=time_budget, vias=vias,
            )
        except Exception:  # noqa: BLE001
            log.warning("route_compare_failed", exc_info=True)
            return JSONResponse({"error": "Routing failed."}, status_code=502)
        return {
            "fastest": _compare_leg(fastest, "Fastest"),
            "scenic": _compare_leg(scenic, "Most scenic"),
            "fastest_meta": _compare_meta(fastest),
            "scenic_meta": _compare_meta(scenic),
        }


class DrawRouteBody(BaseModel):
    coords: list[list[float]] = Field(..., min_length=2, max_length=50)
    profile: str = "balanced"
    snap_to_roads: bool = True
    time_budget: bool = True


@app.post("/api/route/draw")
def route_draw(body: DrawRouteBody):
    """Score a user-drawn path: pairwise OSRM along click chords (default), full scenic pipeline."""
    with _plan_slot() as ok:
        if not ok:
            return _busy_response()
        prof = catalog_data.get_profile(body.profile) or catalog_data.get_profile("balanced")
        weights = prof["weights"] if prof else None
        pid = prof["id"] if prof else None
        try:
            result = roads.score_drawn_route(
                body.coords,
                profile_id=pid,
                weights=weights,
                snap_to_roads=body.snap_to_roads,
                time_budget=body.time_budget,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except Exception:  # noqa: BLE001
            log.warning("route_draw_failed", exc_info=True)
            return JSONResponse({"error": "Routing failed."}, status_code=502)
        _log_plan_finished({**result, "source": "drawn"})
        try:
            c = result["chosen"]
            store.log_history(
                result["from"][0], result["from"][1],
                result["to"][0], result["to"][1],
                0.7, result.get("profile"),
                c["distance_km"], c["duration_min"], c["avg_scenic_score"],
            )
        except Exception:
            pass
        return result


@app.get("/api/route/compare/stream")
def route_compare_stream(
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    profile: str = Query("balanced"),
    preference: float = Query(1.0, ge=0.0, le=1.0),
    avoid_motorways: bool = Query(False),
    min_scenic: float = Query(0.0, ge=0.0, le=100.0),
    explore_all: bool = Query(False),
    time_budget: bool = Query(True),
    via: list[str] | None = Query(None),
    compare_field: bool = Query(False),
):
    """SSE compare: fastest then scenic, or road-first then field-first when compare_field=1."""
    acquired = _acquire_plan_slot()
    if not acquired:
        return _busy_response()

    prof = catalog_data.get_profile(profile) or catalog_data.get_profile("balanced")
    weights = prof["weights"] if prof else None
    detour = prof["detour_factor"] if prof else None
    pid = prof["id"] if prof else None
    vias = _parse_vias(via)

    def gen():
        try:
            if compare_field:
                yield f"data: {json.dumps({'type': 'start', 'mode': 'compare_field'})}\n\n"
                yield f"data: {json.dumps({'type': 'phase', 'label': 'Comparing: planning road-first route…', 'leg': 'road'})}\n\n"
                road = None
                for ev in roads.plan_events(
                    (from_lat, from_lng), (to_lat, to_lng),
                    preference=preference, weights=weights, detour_factor=detour,
                    profile_id=pid, avoid_motorways=avoid_motorways,
                    min_scenic=min_scenic, explore_all=explore_all,
                    time_budget=time_budget, vias=vias,
                ):
                    if ev.get("type") == "done":
                        road = ev["result"]
                        _log_plan_finished(road)
                        yield f"data: {json.dumps({'type': 'leg_done', 'leg': 'road'})}\n\n"
                    elif ev.get("type") == "error":
                        yield f"data: {json.dumps(ev)}\n\n"
                        return
                    elif ev.get("type") in ("phase", "landcover", "round", "candidate"):
                        yield f"data: {json.dumps({**ev, 'leg': 'road'})}\n\n"

                yield f"data: {json.dumps({'type': 'phase', 'label': 'Comparing: planning scenic field route…', 'leg': 'field'})}\n\n"
                field = None
                for ev in field_route.plan_field_events(
                    (from_lat, from_lng), (to_lat, to_lng), preference,
                    profile_id=pid, weights=weights, time_budget=time_budget,
                    avoid_motorways=avoid_motorways,
                ):
                    if ev.get("type") == "done":
                        field = ev["result"]
                        _log_plan_finished(field)
                        yield f"data: {json.dumps({'type': 'leg_done', 'leg': 'field'})}\n\n"
                    elif ev.get("type") == "error":
                        yield f"data: {json.dumps(ev)}\n\n"
                        return
                    elif ev.get("type") in ("phase", "landcover", "cell"):
                        yield f"data: {json.dumps({**ev, 'leg': 'field'})}\n\n"

                if road is None or field is None:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Routing failed.'})}\n\n"
                    return
                result = {
                    "mode": "compare_field",
                    "road": _compare_leg(road, "Road-first recommended"),
                    "field": _compare_leg(field, "Field-first"),
                    "road_meta": _compare_meta(road, field_meta=None),
                    "field_meta": _compare_meta(field, field_meta=field.get("field_meta")),
                }
                yield f"data: {json.dumps({'type': 'done', 'result': result})}\n\n"
                return

            yield f"data: {json.dumps({'type': 'start', 'mode': 'compare'})}\n\n"
            yield f"data: {json.dumps({'type': 'phase', 'label': 'Comparing: planning fastest route…', 'leg': 'fastest'})}\n\n"
            fastest = None
            for ev in roads.plan_events(
                (from_lat, from_lng), (to_lat, to_lng),
                preference=0.0, weights=weights, detour_factor=detour,
                profile_id=pid, avoid_motorways=avoid_motorways,
                min_scenic=0.0, explore_all=False, time_budget=time_budget,
                vias=vias,
            ):
                if ev.get("type") == "done":
                    fastest = ev["result"]
                    _log_plan_finished(fastest)
                    yield f"data: {json.dumps({'type': 'leg_done', 'leg': 'fastest'})}\n\n"
                elif ev.get("type") == "error":
                    yield f"data: {json.dumps(ev)}\n\n"
                    return
                elif ev.get("type") in ("phase", "landcover", "round"):
                    tagged = {**ev, "leg": "fastest"}
                    yield f"data: {json.dumps(tagged)}\n\n"

            yield f"data: {json.dumps({'type': 'phase', 'label': 'Comparing: planning scenic route…', 'leg': 'scenic'})}\n\n"
            scenic = None
            for ev in roads.plan_events(
                (from_lat, from_lng), (to_lat, to_lng),
                preference=preference, weights=weights, detour_factor=detour,
                profile_id=pid, avoid_motorways=avoid_motorways,
                min_scenic=min_scenic, explore_all=explore_all,
                time_budget=time_budget, vias=vias,
            ):
                if ev.get("type") == "done":
                    scenic = ev["result"]
                    _log_plan_finished(scenic)
                    yield f"data: {json.dumps({'type': 'leg_done', 'leg': 'scenic'})}\n\n"
                elif ev.get("type") == "error":
                    yield f"data: {json.dumps(ev)}\n\n"
                    return
                elif ev.get("type") in ("phase", "landcover", "round", "candidate"):
                    tagged = {**ev, "leg": "scenic"}
                    yield f"data: {json.dumps(tagged)}\n\n"

            if fastest is None or scenic is None:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Routing failed.'})}\n\n"
                return
            result = {
                "fastest": _compare_leg(fastest, "Fastest"),
                "scenic": _compare_leg(scenic, "Most scenic"),
                "fastest_meta": _compare_meta(fastest),
                "scenic_meta": _compare_meta(scenic),
            }
            yield f"data: {json.dumps({'type': 'done', 'result': result})}\n\n"
        except Exception:  # noqa: BLE001
            log.warning("route_compare_stream_failed", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Routing failed.'})}\n\n"
        finally:
            _release_plan_slot()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# Serve the frontend at root.
app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
