from __future__ import annotations

import json
import re

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .config import HTTP_TIMEOUT, OVERPASS_ENDPOINTS

router = APIRouter()

EXPORT_FORMATS = [
    {"id": "gpx", "name": "GPS Exchange Format", "ext": "gpx", "media": "application/gpx+xml"},
    {"id": "geojson", "name": "GeoJSON", "ext": "geojson", "media": "application/geo+json"},
    {"id": "kml", "name": "Keyhole Markup Language", "ext": "kml", "media": "application/vnd.google-earth.kml+xml"},
    {"id": "csv", "name": "Comma-separated Values", "ext": "csv", "media": "text/csv"},
]

_FORMATS_BY_ID = {item["id"]: item for item in EXPORT_FORMATS}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
OVERPASS_URL = OVERPASS_ENDPOINTS[0] if OVERPASS_ENDPOINTS else "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = max(45, int(HTTP_TIMEOUT))

CATEGORIES = {
    "viewpoint": [("tourism", "viewpoint")],
    "waterfall": [("waterway", "waterfall")],
    "peak": [("natural", "peak")],
    "castle": [("historic", "castle")],
    "lake": [("natural", "water")],
    "beach": [("natural", "beach")],
    "nature_reserve": [("leisure", "nature_reserve")],
    "picnic": [("tourism", "picnic_site")],
    "camp": [("tourism", "camp_site")],
    "ruins": [("historic", "ruins")],
    "monument": [("historic", "monument")],
    "attraction": [("tourism", "attraction")],
}


def _friendly_name(value: str) -> str:
    return value.replace("_", " ").title()


def _safe_filename(name: str | None) -> str:
    safe = _SAFE_NAME_RE.sub("_", (name or "route").strip()).strip("._-")
    return safe or "route"


def _xml_escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _validate_coordinates(coordinates: list[list[float]]) -> list[tuple[float, float]]:
    if not coordinates:
        raise HTTPException(status_code=400, detail="empty coordinates")
    if len(coordinates) > 50_000:
        raise HTTPException(status_code=400, detail="coordinates exceed limit of 50000 points")

    parsed: list[tuple[float, float]] = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise HTTPException(status_code=400, detail="coordinates must be [lng, lat] pairs")
        lng = float(point[0])
        lat = float(point[1])
        if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
            raise HTTPException(status_code=400, detail="coordinates out of range")
        parsed.append((lng, lat))
    return parsed


def _to_gpx(name: str, coordinates: list[tuple[float, float]]) -> str:
    trackpoints = "".join(
        f'      <trkpt lat="{lat:.8f}" lon="{lng:.8f}" />\n' for lng, lat in coordinates
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="ScenicRoutePlanner" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <metadata><name>{_xml_escape(name)}</name></metadata>\n"
        "  <trk>\n"
        f"    <name>{_xml_escape(name)}</name>\n"
        "    <trkseg>\n"
        f"{trackpoints}"
        "    </trkseg>\n"
        "  </trk>\n"
        "</gpx>\n"
    )


def _to_geojson(name: str, coordinates: list[tuple[float, float]]) -> str:
    feature = {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {
            "type": "LineString",
            "coordinates": [[lng, lat] for lng, lat in coordinates],
        },
    }
    return json.dumps(feature, ensure_ascii=False)


def _to_kml(name: str, coordinates: list[tuple[float, float]]) -> str:
    coord_text = " ".join(f"{lng:.8f},{lat:.8f},0" for lng, lat in coordinates)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        "  <Document>\n"
        f"    <name>{_xml_escape(name)}</name>\n"
        "    <Placemark>\n"
        f"      <name>{_xml_escape(name)}</name>\n"
        "      <LineString>\n"
        f"        <coordinates>{coord_text}</coordinates>\n"
        "      </LineString>\n"
        "    </Placemark>\n"
        "  </Document>\n"
        "</kml>\n"
    )


def _to_csv(name: str, coordinates: list[tuple[float, float]]) -> str:
    del name
    rows = ["index,lat,lng"]
    rows.extend(f"{idx},{lat:.8f},{lng:.8f}" for idx, (lng, lat) in enumerate(coordinates))
    return "\n".join(rows) + "\n"


_CONVERTERS = {
    "gpx": _to_gpx,
    "geojson": _to_geojson,
    "kml": _to_kml,
    "csv": _to_csv,
}


def convert_route(fmt: str, name: str, coordinates: list[list[float]]) -> tuple[str, str, str]:
    fmt = fmt.lower()
    if fmt not in _CONVERTERS:
        raise HTTPException(status_code=400, detail="unknown export format")
    parsed = _validate_coordinates(coordinates)
    meta = _FORMATS_BY_ID[fmt]
    return _CONVERTERS[fmt](name, parsed), meta["media"], meta["ext"]


@router.post("/api/export/{fmt}")
def export_route(fmt: str, payload: dict) -> Response:
    name = str(payload.get("name") or "route")
    content, media_type, ext = convert_route(fmt, name, payload.get("coordinates") or [])
    filename = f"{_safe_filename(name)}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/export/formats")
def export_formats() -> dict:
    return {"formats": EXPORT_FORMATS}


@router.get("/api/poi/categories")
def poi_categories() -> dict:
    """Experimental: POI category list — not wired into the planner UI."""
    return {
        "categories": [
            {"id": category_id, "name": _friendly_name(category_id)} for category_id in CATEGORIES
        ]
    }


def _tag_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _selected_categories(categories: str | None) -> list[str]:
    if not categories:
        return list(CATEGORIES)
    return [item for item in (part.strip() for part in categories.split(",")) if item in CATEGORIES]


def _category_for_tags(tags: dict, selected: list[str]) -> str | None:
    for category_id in selected:
        for key, value in CATEGORIES[category_id]:
            if tags.get(key) == value:
                return category_id
    return None


def _build_overpass_query(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    selected: list[str],
    limit: int,
) -> str:
    bbox = f"{min_lat:.4f},{min_lng:.4f},{max_lat:.4f},{max_lng:.4f}"
    selectors = []
    for category_id in selected:
        for key, value in CATEGORIES[category_id]:
            key = _tag_escape(key)
            value = _tag_escape(value)
            selectors.append(f'  node["{key}"="{value}"]({bbox});')
            selectors.append(f'  way["{key}"="{value}"]({bbox});')
    return "\n".join([
        f"[out:json][timeout:{OVERPASS_TIMEOUT}];",
        "(",
        *selectors,
        ");",
        f"out center {limit};",
    ])


def _fetch_pois(query: str) -> list[dict]:
    try:
        response = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": "ScenicRoutePlanner/1.0", "Accept": "application/json"},
            timeout=OVERPASS_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("elements", []) if isinstance(data, dict) else []
    except requests.RequestException:
        return []
    except ValueError:
        return []


@router.get("/api/poi")
def get_pois(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    categories: str | None = None,
    limit: int = Query(default=300, ge=1, le=1000),
) -> dict:
    """Experimental: Overpass POI overlay — not wired into the planner UI."""
    if (max_lat - min_lat) * (max_lng - min_lng) > 4.0:
        raise HTTPException(status_code=400, detail="bbox too large")

    selected = _selected_categories(categories)
    if not selected:
        return {"count": 0, "pois": []}

    query = _build_overpass_query(min_lat, min_lng, max_lat, max_lng, selected, limit)
    pois = []
    for element in _fetch_pois(query):
        tags = element.get("tags") or {}
        category = _category_for_tags(tags, selected)
        if not category:
            continue

        lat = element.get("lat")
        lng = element.get("lon")
        if lat is None or lng is None:
            center = element.get("center") or {}
            lat = center.get("lat")
            lng = center.get("lon")
        if lat is None or lng is None:
            continue

        pois.append({
            "lat": lat,
            "lng": lng,
            "name": tags.get("name") or _friendly_name(category),
            "category": category,
        })
        if len(pois) >= limit:
            break

    return {"count": len(pois), "pois": pois}
