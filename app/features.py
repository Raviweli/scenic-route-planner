"""Browsable feature catalog for Scenic Route Planner capabilities."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query

router = APIRouter()
_CATALOG_CACHE: dict[str, Any] | None = None

_EXPORT_FORMATS = ("gpx", "geojson", "kml", "csv")

_SIGNAL_CONTROLS = [
    ("colour-weight", "Colour Weight", "Adjusts how strongly colour-derived scenic signals influence route scoring."),
    ("terrain-weight", "Terrain Weight", "Adjusts how much elevation and terrain variation influence scenic scoring."),
    ("landcover-weight", "Landcover Weight", "Adjusts the contribution of woodland, water, parkland, and other landcover signals."),
    ("detour-factor", "Detour Factor", "Limits how much longer a scenic alternative may be compared with the fastest route."),
    ("preference-slider", "Preference Slider", "Balances shortest-time routing against maximum scenic value from 0.0 to 1.0."),
    ("sample-spacing", "Sample Spacing", "Controls how frequently route geometry is sampled for scenic scoring."),
    ("tile-zoom", "Tile Zoom", "Sets the map tile zoom level used when collecting colour and terrain signals."),
    ("downscale", "Downscale", "Reduces source imagery resolution to trade detail for faster scoring throughput."),
]

_CORE_ACTIONS = [
    ("plan-route", "Plan Route", "Calculate a route between selected start and destination points."),
    ("compare-fastest-vs-scenic", "Compare Fastest vs Scenic", "Compare the quickest route with the most scenic available alternative."),
    ("save-route", "Save Route", "Persist a planned route for later retrieval."),
    ("name-route", "Name Route", "Assign a user-friendly name to a saved route."),
    ("tag-route", "Tag Route", "Attach searchable tags such as coastal, forest, family, or weekend."),
    ("rate-route", "Rate Route", "Record a user rating that can help sort and revisit routes."),
    ("favourite-route", "Favourite Route", "Mark a route as a favourite for quick access."),
    ("route-history", "Route History", "Browse previously planned routes and comparisons."),
    ("geocode-search", "Geocode Search", "Search for places and convert them into route waypoints."),
    ("export-route", "Export Route", "Download the active route in a supported interchange format."),
    ("poi-toggle", "POI Toggle", "Show or hide selected point-of-interest overlay categories."),
    ("region-jump", "Region Jump", "Move the map directly to a known planning region."),
    ("profile-select", "Profile Select", "Choose the scenic scoring profile used for route planning."),
    ("waypoint-scenic-search", "Waypoint Scenic Search", "Find scenic waypoint candidates near the current route corridor."),
    ("live-scoring", "Live Scoring", "Score a coordinate or route segment on demand using the scenic pipeline."),
]

_KEYBOARD_SHORTCUTS = [
    ("p", "P", "Plan the current route."),
    ("s", "S", "Save the current route."),
    ("c", "C", "Compare fastest and scenic routes."),
    ("f", "F", "Toggle favourite on the active route."),
    ("slash", "/", "Focus the geocode search field."),
    ("e", "E", "Open route export options."),
    ("o", "O", "Toggle POI overlays."),
    ("r", "R", "Open the region jump list."),
    ("g", "G", "Show route history."),
    ("t", "T", "Tag the active saved route."),
    ("plus", "+", "Increase scenic preference."),
    ("minus", "-", "Decrease scenic preference."),
    ("digits-1-9", "1-9", "Jump to the corresponding pinned region."),
]


def _slug(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or fallback


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


def _item(item_id: str, name: str, description: str, kind: str) -> dict[str, str]:
    return {"id": item_id, "name": name, "description": description, "kind": kind}


def _category(category_id: str, name: str, items: list[dict[str, str]]) -> dict[str, Any]:
    return {"id": category_id, "name": name, "count": len(items), "items": items}


def _load_catalog_data() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        from app import catalog_data  # type: ignore
    except Exception:  # noqa: BLE001 - teammate module may be temporarily unavailable.
        return [], [], []

    profiles = getattr(catalog_data, "PROFILES", []) or []
    presets = getattr(catalog_data, "PRESETS", []) or []
    regions = getattr(catalog_data, "REGIONS", []) or []
    return list(profiles), list(presets), list(regions)


def _load_exports() -> tuple[dict[str, Any], list[Any]]:
    try:
        from app import exports  # type: ignore
    except Exception:  # noqa: BLE001 - export module may be temporarily unavailable.
        return {}, list(_EXPORT_FORMATS)

    categories = getattr(exports, "CATEGORIES", {}) or {}
    formats = None
    for attr in ("EXPORT_FORMATS", "FORMATS", "FORMATS_LIST", "SUPPORTED_FORMATS", "EXPORTS"):
        value = getattr(exports, attr, None)
        if value:
            formats = value
            break
    return dict(categories), list(formats or _EXPORT_FORMATS)


def _format_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("id") or value.get("name") or value.get("format")
    return str(value).strip().lower()


def build_catalog() -> dict[str, Any]:
    profiles, presets, regions = _load_catalog_data()
    poi_categories, export_formats = _load_exports()

    categories: list[dict[str, Any]] = []

    profile_items = [
        _item(
            f"profile-{_slug(profile.get('id') or profile.get('name'), str(index))}",
            _text(profile.get("name"), f"Scenic Profile {index + 1}"),
            _text(profile.get("description"), "Scenic scoring profile available for route planning."),
            "scenic-profile",
        )
        for index, profile in enumerate(profiles)
        if isinstance(profile, dict)
    ]
    categories.append(_category("scenic-profiles", "Scenic Profiles", profile_items))

    preset_items = [
        _item(
            f"preset-{_slug(preset.get('id') or preset.get('name'), str(index))}",
            _text(preset.get("name"), f"Scenic Drive Preset {index + 1}"),
            _text(preset.get("description"), "Prebuilt scenic drive preset available in the planner."),
            "scenic-drive-preset",
        )
        for index, preset in enumerate(presets)
        if isinstance(preset, dict)
    ]
    categories.append(_category("scenic-drive-presets", "Scenic Drive Presets", preset_items))

    region_items = [
        _item(
            f"region-{_slug(region.get('id') or region.get('name'), str(index))}",
            _text(region.get("name"), f"Region {index + 1}"),
            "Quick-jump planning region available on the map.",
            "region",
        )
        for index, region in enumerate(regions)
        if isinstance(region, dict)
    ]
    categories.append(_category("regions", "Regions", region_items))

    poi_items = []
    for category_id, tags in sorted(poi_categories.items(), key=lambda pair: str(pair[0])):
        tag_list = ", ".join(str(tag) for tag in tags) if isinstance(tags, (list, tuple, set)) else str(tags)
        name = str(category_id).replace("_", " ").replace("-", " ").title()
        poi_items.append(
            _item(
                f"poi-{_slug(category_id, 'overlay')}",
                name,
                f"Point-of-interest overlay backed by tags: {tag_list}.",
                "poi-overlay",
            )
        )
    categories.append(_category("poi-overlays", "POI Overlays", poi_items))

    export_items = []
    seen_formats: set[str] = set()
    for fmt in export_formats:
        fmt_name = _format_name(fmt)
        if not fmt_name or fmt_name in seen_formats:
            continue
        seen_formats.add(fmt_name)
        export_items.append(
            _item(
                f"export-{_slug(fmt_name, 'format')}",
                fmt_name.upper(),
                f"Export the active route as {fmt_name.upper()} for use in mapping and navigation tools.",
                "export-format",
            )
        )
    categories.append(_category("exports", "Exports", export_items))

    signal_items = [
        _item(f"signal-{control_id}", name, description, "signal-control")
        for control_id, name, description in _SIGNAL_CONTROLS
    ]
    categories.append(_category("signals-controls", "Signals & Controls", signal_items))

    core_items = [
        _item(f"action-{action_id}", name, description, "core-action")
        for action_id, name, description in _CORE_ACTIONS
    ]
    categories.append(_category("core-actions", "Core Actions", core_items))

    shortcut_items = [
        _item(f"shortcut-{shortcut_id}", key, action, "keyboard-shortcut")
        for shortcut_id, key, action in _KEYBOARD_SHORTCUTS
    ]
    categories.append(_category("keyboard-shortcuts", "Keyboard Shortcuts", shortcut_items))

    tuned_items = []
    if profiles and regions:
        max_tuned = 1500
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = _slug(profile.get("id") or profile.get("name"), "profile")
            profile_name = _text(profile.get("name"), "Scenic Profile")
            for region in regions:
                if not isinstance(region, dict):
                    continue
                region_id = _slug(region.get("id") or region.get("name"), "region")
                region_name = _text(region.get("name"), "Region")
                tuned_items.append(
                    _item(
                        f"tuned-{profile_id}-{region_id}",
                        f"{profile_name} in {region_name}",
                        f"Run the real {profile_name} scoring profile against the {region_name} planning region.",
                        "tuned-scenic-drive",
                    )
                )
                if len(tuned_items) >= max_tuned:
                    break
            if len(tuned_items) >= max_tuned:
                break
    categories.append(_category("tuned-scenic-drives", "Tuned Scenic Drives (Profile × Region)", tuned_items))

    total = sum(category["count"] for category in categories)
    return {"total": total, "categories": categories}


def _get_catalog() -> dict[str, Any]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        _CATALOG_CACHE = build_catalog()
    return _CATALOG_CACHE


@router.get("/api/features")
def get_features() -> dict[str, Any]:
    return _get_catalog()


@router.get("/api/features/categories")
def get_feature_categories() -> list[dict[str, Any]]:
    catalog = _get_catalog()
    return [
        {"id": category["id"], "name": category["name"], "count": category["count"]}
        for category in catalog["categories"]
    ]


@router.get("/api/features/search")
def search_features(q: str = Query("", description="Case-insensitive search text.")) -> dict[str, Any]:
    query = q.strip().lower()
    if not query:
        return {"count": 0, "items": []}

    matches = []
    for category in _get_catalog()["categories"]:
        for item in category["items"]:
            haystack = f"{item['name']} {item['description']}".lower()
            if query in haystack:
                matches.append(item)
    return {"count": len(matches), "items": matches}


@router.get("/api/features/stats")
def get_feature_stats() -> dict[str, int]:
    catalog = _get_catalog()
    return {"total": catalog["total"], "categories": len(catalog["categories"])}
