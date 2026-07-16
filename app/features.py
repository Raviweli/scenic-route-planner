"""Browsable feature catalog for Scenic Route Planner capabilities.

Experimental API (`/api/features*`) — not wired into the planner UI.
Catalog text matches what the shell actually supports: curated scenery styles,
featured presets (`FEATURED_PRESET_IDS`), plan/compare/save/history/export.
POI overlays and heatmap tooling are listed as experimental / not in UI.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query

router = APIRouter()
_CATALOG_CACHE: dict[str, Any] | None = None

_EXPORT_FORMATS = ("gpx", "geojson", "kml", "csv")

# Curated scenery styles shown in the UI (must stay aligned with frontend STYLES).
_UI_SCENERY_STYLES = [
    ("balanced", "A bit of everything", "Default balanced scenic blend."),
    ("coastal-moderate-landcover", "Coast & sea", "Favours coastal land cover and open colour."),
    ("mountain-moderate-terrain", "Mountains & hills", "Favours terrain relief."),
    ("waterside-moderate-colour", "Lakes & rivers", "Favours waterside colour and lakes."),
    ("woodland-moderate-landcover", "Forests & woodland", "Favours woodland land cover."),
    ("pastoral-moderate-landcover", "Countryside & villages", "Favours pastoral countryside."),
]

_SIGNAL_CONTROLS = [
    ("preference-slider", "Preference Slider", "Balances shortest-time routing against maximum scenic value from 0.0 to 1.0."),
    ("min-scenic", "Minimum Scenic Target", "Hard floor (0–100); only qualifying routes are recommended; unmet → most scenic, not fastest."),
    ("avoid-motorways", "Avoid Motorways", "Hard ban on motorway km when any non-motorway candidate exists (no motorways)."),
    ("explore-all", "Explore Everything", "Disregards travel time and diverts through nearby national parks."),
    ("time-budget", "Plan Time Budget", "Optional ~30s wall-clock gate; turn off in Advanced for full explore/hard-target on cold corridors."),
]

_CORE_ACTIONS = [
    ("plan-route", "Plan Route", "Live-streamed scenic search between start and end on real roads."),
    ("compare-fastest-vs-scenic", "Compare Fastest vs Scenic", "Side-by-side fastest and most-scenic routes."),
    ("save-route", "Save Route", "Persist a planned route for later retrieval."),
    ("name-route", "Name Route", "Assign a user-friendly name to a saved route."),
    ("tag-route", "Tag Route", "Attach searchable tags such as coastal, forest, family, or weekend."),
    ("rate-route", "Rate Route", "Record a user rating that can help sort and revisit routes."),
    ("favourite-route", "Favourite Route", "Mark a route as a favourite for quick access."),
    ("route-history", "Route History", "Browse previously planned routes and reuse A/B."),
    ("geocode-search", "Geocode Search", "Search for places and set start/end markers."),
    ("export-route", "Export Route", "Download the active route as GPX, GeoJSON, KML, or CSV."),
    ("region-jump", "Region Jump", "Move the map directly to a known planning region."),
    ("profile-select", "Scenery Style", "Choose one of the curated scenery styles (not the full profile catalogue)."),
    ("featured-preset", "Featured Preset", "Load a curated drive from FEATURED_PRESET_IDS via /api/presets?featured=true."),
    ("live-scoring-explain", "Segment Explainability", "Hover a road segment to see colour / terrain / land-cover breakdown."),
]

_EXPERIMENTAL_NOTES = [
    ("poi-overlay", "POI Overlay", "Experimental /api/poi — not wired into the UI."),
    ("heatmap-cells", "Heatmap Cells", "Experimental /api/cells + build_grid — optional tooling, not the live planner."),
    ("feature-catalog", "Feature Catalog", "This /api/features dump — experimental; UI does not consume it."),
]

_KEYBOARD_SHORTCUTS = [
    ("p", "P", "Plan the current route."),
    ("s", "S", "Open the save-route dialog when a route is selected."),
    ("slash", "/", "Focus the place-search field."),
    ("escape", "Esc", "Close the save dialog or disarm point placement."),
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
    featured_fn = getattr(catalog_data, "featured_presets", None)
    if callable(featured_fn):
        presets = list(featured_fn() or [])
    else:
        presets = list(getattr(catalog_data, "PRESETS", []) or [])[:20]
    regions = getattr(catalog_data, "REGIONS", []) or []
    return list(profiles), presets, list(regions)


def _load_export_formats() -> list[Any]:
    try:
        from app import exports  # type: ignore
    except Exception:  # noqa: BLE001
        return list(_EXPORT_FORMATS)

    for attr in ("EXPORT_FORMATS", "FORMATS", "FORMATS_LIST", "SUPPORTED_FORMATS", "EXPORTS"):
        value = getattr(exports, attr, None)
        if value:
            return list(value)
    return list(_EXPORT_FORMATS)


def _format_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("id") or value.get("name") or value.get("format")
    return str(value).strip().lower()


def build_catalog() -> dict[str, Any]:
    _profiles, featured_presets, regions = _load_catalog_data()
    export_formats = _load_export_formats()

    categories: list[dict[str, Any]] = []

    # UI-facing curated styles (not the giant combinatorial profile list).
    style_items = [
        _item(f"style-{_slug(sid, str(i))}", name, desc, "scenery-style")
        for i, (sid, name, desc) in enumerate(_UI_SCENERY_STYLES)
    ]
    categories.append(_category("scenery-styles", "Scenery Styles (UI)", style_items))

    preset_items = [
        _item(
            f"preset-{_slug(preset.get('id') or preset.get('name'), str(index))}",
            _text(preset.get("name"), f"Scenic Drive Preset {index + 1}"),
            _text(preset.get("description"), "Featured scenic drive preset shown in the UI dropdown."),
            "featured-preset",
        )
        for index, preset in enumerate(featured_presets)
        if isinstance(preset, dict)
    ]
    categories.append(_category("featured-presets", "Featured Presets (UI)", preset_items))

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
    categories.append(_category("signals-controls", "Planner Controls (UI)", signal_items))

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

    experimental_items = [
        _item(f"experimental-{eid}", name, description, "experimental")
        for eid, name, description in _EXPERIMENTAL_NOTES
    ]
    categories.append(_category("experimental", "Experimental (not in UI)", experimental_items))

    total = sum(category["count"] for category in categories)
    return {
        "total": total,
        "categories": categories,
        "note": (
            "Experimental catalogue. UI uses curated scenery styles + FEATURED_PRESET_IDS only; "
            "full /api/profiles and /api/presets remain available for API consumers."
        ),
    }


def _get_catalog() -> dict[str, Any]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        _CATALOG_CACHE = build_catalog()
    return _CATALOG_CACHE


@router.get("/api/features")
def get_features() -> dict[str, Any]:
    """Experimental capability catalogue — not used by the planner UI."""
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
