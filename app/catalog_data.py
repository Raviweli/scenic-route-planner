from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException


def _slug(text: str) -> str:
    """Return a stable URL-safe slug for catalogue identifiers."""
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value or "item"


def _normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(value)) for value in weights.values())
    if total <= 0:
        return {"colour": 1 / 3, "terrain": 1 / 3, "landcover": 1 / 3}
    return {key: round(max(0.0, float(weights.get(key, 0.0))) / total, 3) for key in ("colour", "terrain", "landcover")}


def _blend_weights(base: dict[str, float], emphasis: dict[str, float], amount: float = 0.28) -> dict[str, float]:
    return _normalise_weights({key: base[key] * (1 - amount) + emphasis[key] * amount for key in base})


def build_profiles() -> list[dict[str, Any]]:
    """Build scenic scoring profiles used by the route planner UI and API."""
    profiles: list[dict[str, Any]] = [
        {
            "id": "balanced",
            "name": "Balanced",
            "description": "A reliable all-round scenic blend for colour, terrain and land-cover variety.",
            "category": "All-rounder",
            "weights": _normalise_weights({"colour": 1.0, "terrain": 1.0, "landcover": 1.0}),
            "detour_factor": 3.5,
        }
    ]

    archetypes = [
        ("All-rounder", "Balanced Explorer", "Balanced views without a strong bias.", {"colour": 0.34, "terrain": 0.33, "landcover": 0.33}, 1.00),
        ("Coastal", "Coastal", "Sea cliffs, beaches, estuaries and bright open colour.", {"colour": 0.38, "terrain": 0.17, "landcover": 0.45}, 1.05),
        ("Mountain", "Mountain", "High passes, steep relief and dramatic ridge scenery.", {"colour": 0.18, "terrain": 0.62, "landcover": 0.20}, 1.10),
        ("Woodland", "Woodland", "Tree cover, forest roads and enclosed green corridors.", {"colour": 0.25, "terrain": 0.16, "landcover": 0.59}, 1.00),
        ("Waterside", "Waterside", "Lakes, rivers, reservoirs and reflective valley roads.", {"colour": 0.42, "terrain": 0.20, "landcover": 0.38}, 1.05),
        ("Pastoral", "Pastoral", "Rolling farmland, villages, hedgerows and gentle lanes.", {"colour": 0.32, "terrain": 0.18, "landcover": 0.50}, 0.95),
        ("Moorland", "Moorland", "Open heather, upland moor roads and exposed horizons.", {"colour": 0.31, "terrain": 0.46, "landcover": 0.23}, 1.10),
        ("Urban Avoider", "Urban Avoider", "Prioritises greener land-cover and accepts larger rural detours.", {"colour": 0.20, "terrain": 0.22, "landcover": 0.58}, 1.35),
        ("Fast Scenic", "Fast Scenic", "Keeps detours short while still preferring scenic colour.", {"colour": 0.44, "terrain": 0.28, "landcover": 0.28}, 0.70),
        ("Photographer", "Photographer", "Viewpoint-rich routes with strong colour and landscape contrast.", {"colour": 0.52, "terrain": 0.30, "landcover": 0.18}, 1.20),
        ("Autumn Colour", "Autumn Colour", "Woodland and seasonal colour, especially for October drives.", {"colour": 0.58, "terrain": 0.10, "landcover": 0.32}, 1.05),
        ("Big Sky", "Big Sky", "Open horizons, exposed roads and broad scenic panoramas.", {"colour": 0.35, "terrain": 0.42, "landcover": 0.23}, 1.15),
        ("Heritage", "Heritage", "Villages, historic landscapes and visually varied countryside.", {"colour": 0.40, "terrain": 0.20, "landcover": 0.40}, 1.00),
        ("Wildlife", "Wildlife", "Quieter green routes near reserves, woods, rivers and coasts.", {"colour": 0.25, "terrain": 0.20, "landcover": 0.55}, 1.10),
        ("Valley", "Valley", "Roads following valley floors beneath visible slopes and ridges.", {"colour": 0.24, "terrain": 0.50, "landcover": 0.26}, 1.00),
        ("Remote", "Remote", "Sparse, wild-feeling landscapes where longer detours are acceptable.", {"colour": 0.22, "terrain": 0.48, "landcover": 0.30}, 1.30),
    ]
    detours = [
        ("gentle", "Gentle", 2.3),
        ("moderate", "Moderate", 4.2),
        ("adventurous", "Adventurous", 7.0),
    ]
    variants = [
        ("colour", "Colour-leaning", {"colour": 0.70, "terrain": 0.15, "landcover": 0.15}),
        ("terrain", "Terrain-leaning", {"colour": 0.15, "terrain": 0.70, "landcover": 0.15}),
        ("landcover", "Land-cover-leaning", {"colour": 0.15, "terrain": 0.15, "landcover": 0.70}),
    ]

    for category, label, summary, base_weights, detour_multiplier in archetypes:
        for detour_key, detour_label, detour_value in detours:
            for variant_key, variant_label, emphasis in variants:
                weights = _blend_weights(_normalise_weights(base_weights), emphasis)
                detour_factor = round(max(2.0, min(10.0, detour_value * detour_multiplier)), 1)
                profiles.append(
                    {
                        "id": f"{_slug(label)}-{detour_key}-{variant_key}",
                        "name": f"{label} ? {detour_label} ? {variant_label}",
                        "description": f"{summary} Uses a {detour_label.lower()} detour appetite with extra emphasis on {variant_key}.",
                        "category": category,
                        "weights": weights,
                        "detour_factor": detour_factor,
                    }
                )

    seen: set[str] = set()
    unique_profiles: list[dict[str, Any]] = []
    for profile in profiles:
        if profile["id"] not in seen:
            unique_profiles.append(profile)
            seen.add(profile["id"])
    return unique_profiles


REGION_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "lake-district",
        "name": "Lake District",
        "center": {"lat": 54.50, "lng": -3.10},
        "zoom": 10,
        "bbox": [54.10, -3.45, 54.75, -2.65],
        "profile": "waterside-moderate-colour",
        "tags": ["lakes", "mountains", "national-park"],
        "anchors": [
            ("Keswick", 54.6013, -3.1347, ["lake", "market-town"]),
            ("Buttermere", 54.5414, -3.2751, ["lake", "pass"]),
            ("Glenridding", 54.5440, -2.9510, ["lake", "fell"]),
            ("Grasmere", 54.4590, -3.0257, ["village", "lake"]),
            ("Ambleside", 54.4287, -2.9620, ["lake", "town"]),
            ("Windermere", 54.3809, -2.9070, ["lake", "town"]),
            ("Coniston", 54.3683, -3.0756, ["lake", "fell"]),
            ("Kendal", 54.3280, -2.7460, ["gateway", "market-town"]),
        ],
    },
    {
        "id": "snowdonia",
        "name": "Snowdonia / Eryri",
        "center": {"lat": 53.08, "lng": -3.90},
        "zoom": 10,
        "bbox": [52.65, -4.30, 53.35, -3.45],
        "profile": "mountain-moderate-terrain",
        "tags": ["mountains", "wales", "national-park"],
        "anchors": [
            ("Betws-y-Coed", 53.0930, -3.8060, ["forest", "river"]),
            ("Capel Curig", 53.1040, -3.9180, ["mountain", "lake"]),
            ("Llanberis", 53.1190, -4.1290, ["mountain", "lake"]),
            ("Beddgelert", 53.0110, -4.1020, ["village", "river"]),
            ("Harlech", 52.8600, -4.1090, ["coast", "castle"]),
            ("Dolgellau", 52.7430, -3.8860, ["mountain", "market-town"]),
            ("Bala", 52.9110, -3.5980, ["lake", "market-town"]),
        ],
    },
    {
        "id": "peak-district",
        "name": "Peak District",
        "center": {"lat": 53.28, "lng": -1.75},
        "zoom": 10,
        "bbox": [52.95, -2.05, 53.50, -1.45],
        "profile": "valley-moderate-terrain",
        "tags": ["moors", "limestone", "national-park"],
        "anchors": [
            ("Bakewell", 53.2130, -1.6750, ["market-town", "river"]),
            ("Hathersage", 53.3310, -1.6520, ["valley", "edges"]),
            ("Castleton", 53.3430, -1.7770, ["cave", "pass"]),
            ("Edale", 53.3690, -1.8160, ["valley", "moor"]),
            ("Buxton", 53.2570, -1.9120, ["spa-town", "upland"]),
            ("Ashbourne", 53.0160, -1.7330, ["gateway", "market-town"]),
            ("Matlock Bath", 53.1210, -1.5620, ["gorge", "river"]),
        ],
    },
    {
        "id": "scottish-highlands",
        "name": "Scottish Highlands",
        "center": {"lat": 57.25, "lng": -5.10},
        "zoom": 7,
        "bbox": [56.40, -6.40, 58.10, -3.80],
        "profile": "remote-adventurous-terrain",
        "tags": ["mountains", "lochs", "remote"],
        "anchors": [
            ("Fort William", 56.8198, -5.1052, ["mountain", "loch"]),
            ("Glencoe", 56.6820, -5.1030, ["glen", "mountain"]),
            ("Mallaig", 57.0050, -5.8300, ["coast", "ferry"]),
            ("Applecross", 57.4320, -5.8120, ["pass", "coast"]),
            ("Torridon", 57.5430, -5.5150, ["mountain", "loch"]),
            ("Ullapool", 57.8950, -5.1600, ["coast", "loch"]),
            ("Inverness", 57.4778, -4.2247, ["city", "loch"]),
        ],
    },
    {
        "id": "cotswolds",
        "name": "Cotswolds",
        "center": {"lat": 51.85, "lng": -1.85},
        "zoom": 10,
        "bbox": [51.35, -2.35, 52.15, -1.45],
        "profile": "heritage-moderate-colour",
        "tags": ["villages", "pastoral", "aonb"],
        "anchors": [
            ("Bourton-on-the-Water", 51.8850, -1.7590, ["village", "river"]),
            ("Stow-on-the-Wold", 51.9300, -1.7230, ["market-town", "village"]),
            ("Chipping Campden", 52.0490, -1.7800, ["market-town", "heritage"]),
            ("Broadway", 52.0360, -1.8610, ["village", "viewpoint"]),
            ("Bibury", 51.7580, -1.8340, ["village", "river"]),
            ("Cirencester", 51.7180, -1.9680, ["market-town", "heritage"]),
            ("Castle Combe", 51.4940, -2.2290, ["village", "heritage"]),
        ],
    },
    {
        "id": "yorkshire-dales",
        "name": "Yorkshire Dales",
        "center": {"lat": 54.20, "lng": -2.10},
        "zoom": 9,
        "bbox": [53.85, -2.55, 54.55, -1.65],
        "profile": "pastoral-moderate-landcover",
        "tags": ["dales", "limestone", "national-park"],
        "anchors": [
            ("Skipton", 53.9620, -2.0160, ["market-town", "gateway"]),
            ("Grassington", 54.0710, -1.9990, ["village", "dale"]),
            ("Malham", 54.0610, -2.1540, ["limestone", "cove"]),
            ("Settle", 54.0680, -2.2770, ["market-town", "limestone"]),
            ("Hawes", 54.3050, -2.1960, ["dale", "market-town"]),
            ("Reeth", 54.3890, -1.9440, ["dale", "village"]),
            ("Kirkby Stephen", 54.4720, -2.3490, ["eden", "market-town"]),
        ],
    },
    {
        "id": "north-coast-500",
        "name": "North Coast 500",
        "center": {"lat": 58.05, "lng": -4.70},
        "zoom": 7,
        "bbox": [57.35, -5.95, 58.70, -3.00],
        "profile": "coastal-adventurous-landcover",
        "tags": ["coast", "scotland", "road-trip"],
        "anchors": [
            ("Inverness", 57.4778, -4.2247, ["city", "gateway"]),
            ("Applecross", 57.4320, -5.8120, ["pass", "coast"]),
            ("Gairloch", 57.7280, -5.6910, ["coast", "beach"]),
            ("Ullapool", 57.8950, -5.1600, ["coast", "harbour"]),
            ("Lochinver", 58.1470, -5.2420, ["coast", "mountain"]),
            ("Durness", 58.5680, -4.7450, ["beach", "cliffs"]),
            ("Thurso", 58.5940, -3.5230, ["coast", "town"]),
            ("John o' Groats", 58.6370, -3.0680, ["coast", "landmark"]),
            ("Wick", 58.4390, -3.0930, ["harbour", "coast"]),
        ],
    },
    {
        "id": "cornwall",
        "name": "Cornwall",
        "center": {"lat": 50.35, "lng": -5.05},
        "zoom": 9,
        "bbox": [49.90, -5.80, 50.90, -4.20],
        "profile": "coastal-moderate-colour",
        "tags": ["coast", "beaches", "atlantic"],
        "anchors": [
            ("St Ives", 50.2110, -5.4800, ["beach", "harbour"]),
            ("Penzance", 50.1190, -5.5370, ["coast", "harbour"]),
            ("Land's End", 50.0660, -5.7130, ["cliffs", "landmark"]),
            ("The Lizard", 49.9590, -5.2060, ["coast", "cliffs"]),
            ("Fowey", 50.3360, -4.6360, ["estuary", "harbour"]),
            ("Padstow", 50.5420, -4.9360, ["harbour", "estuary"]),
            ("Tintagel", 50.6640, -4.7520, ["cliffs", "heritage"]),
            ("Bude", 50.8280, -4.5450, ["beach", "coast"]),
        ],
    },
    {
        "id": "brecon-beacons",
        "name": "Brecon Beacons / Bannau Brycheiniog",
        "center": {"lat": 51.88, "lng": -3.35},
        "zoom": 10,
        "bbox": [51.65, -3.90, 52.10, -2.95],
        "profile": "moorland-moderate-terrain",
        "tags": ["mountains", "wales", "national-park"],
        "anchors": [
            ("Brecon", 51.9470, -3.3910, ["market-town", "mountain"]),
            ("Talybont-on-Usk", 51.8970, -3.2900, ["reservoir", "canal"]),
            ("Crickhowell", 51.8590, -3.1370, ["river", "market-town"]),
            ("Abergavenny", 51.8240, -3.0170, ["market-town", "mountain"]),
            ("Hay-on-Wye", 52.0740, -3.1270, ["river", "books"]),
            ("Llandovery", 51.9950, -3.7960, ["market-town", "upland"]),
            ("Merthyr Tydfil", 51.7480, -3.3810, ["valley", "gateway"]),
        ],
    },
    {
        "id": "cairngorms",
        "name": "Cairngorms",
        "center": {"lat": 57.10, "lng": -3.60},
        "zoom": 8,
        "bbox": [56.60, -4.20, 57.45, -2.90],
        "profile": "mountain-adventurous-terrain",
        "tags": ["mountains", "forest", "national-park"],
        "anchors": [
            ("Aviemore", 57.1950, -3.8250, ["mountain", "forest"]),
            ("Kingussie", 57.0800, -4.0520, ["strath", "village"]),
            ("Grantown-on-Spey", 57.3300, -3.6080, ["river", "forest"]),
            ("Tomintoul", 57.2520, -3.3790, ["upland", "village"]),
            ("Braemar", 57.0060, -3.3970, ["mountain", "village"]),
            ("Ballater", 57.0500, -3.0400, ["royal-deeside", "river"]),
            ("Pitlochry", 56.7030, -3.7350, ["gateway", "woodland"]),
        ],
    },
    {
        "id": "exmoor",
        "name": "Exmoor",
        "center": {"lat": 51.15, "lng": -3.65},
        "zoom": 10,
        "bbox": [50.95, -3.95, 51.30, -3.35],
        "profile": "moorland-moderate-colour",
        "tags": ["moor", "coast", "national-park"],
        "anchors": [
            ("Lynton", 51.2290, -3.8350, ["cliffs", "village"]),
            ("Lynmouth", 51.2300, -3.8270, ["harbour", "cliffs"]),
            ("Porlock", 51.2090, -3.5960, ["coast", "village"]),
            ("Minehead", 51.2050, -3.4780, ["coast", "town"]),
            ("Dunster", 51.1840, -3.4440, ["castle", "village"]),
            ("Simonsbath", 51.1370, -3.7540, ["moor", "river"]),
            ("Dulverton", 51.0410, -3.5500, ["river", "market-town"]),
        ],
    },
    {
        "id": "dartmoor",
        "name": "Dartmoor",
        "center": {"lat": 50.58, "lng": -3.95},
        "zoom": 10,
        "bbox": [50.40, -4.20, 50.75, -3.65],
        "profile": "moorland-moderate-terrain",
        "tags": ["moor", "tors", "national-park"],
        "anchors": [
            ("Tavistock", 50.5500, -4.1440, ["market-town", "gateway"]),
            ("Princetown", 50.5430, -3.9890, ["moor", "village"]),
            ("Widecombe-in-the-Moor", 50.5760, -3.8120, ["village", "moor"]),
            ("Moretonhampstead", 50.6610, -3.7640, ["market-town", "moor"]),
            ("Chagford", 50.6730, -3.8400, ["village", "river"]),
            ("Ashburton", 50.5160, -3.7550, ["market-town", "gateway"]),
            ("Buckfastleigh", 50.4820, -3.7790, ["river", "abbey"]),
        ],
    },
    {
        "id": "northumberland",
        "name": "Northumberland",
        "center": {"lat": 55.25, "lng": -2.05},
        "zoom": 9,
        "bbox": [54.85, -2.70, 55.75, -1.55],
        "profile": "big-sky-adventurous-terrain",
        "tags": ["coast", "hills", "dark-sky"],
        "anchors": [
            ("Alnwick", 55.4130, -1.7060, ["castle", "market-town"]),
            ("Bamburgh", 55.6070, -1.7170, ["coast", "castle"]),
            ("Seahouses", 55.5800, -1.6550, ["coast", "harbour"]),
            ("Wooler", 55.5480, -2.0110, ["hills", "market-town"]),
            ("Rothbury", 55.3100, -1.9080, ["hills", "river"]),
            ("Kielder", 55.2350, -2.5860, ["forest", "reservoir"]),
            ("Hexham", 54.9710, -2.1010, ["abbey", "market-town"]),
            ("Haltwhistle", 54.9710, -2.4590, ["hadrians-wall", "market-town"]),
        ],
    },
    {
        "id": "pembrokeshire",
        "name": "Pembrokeshire",
        "center": {"lat": 51.82, "lng": -4.95},
        "zoom": 10,
        "bbox": [51.55, -5.35, 52.10, -4.55],
        "profile": "coastal-moderate-landcover",
        "tags": ["coast", "wales", "national-park"],
        "anchors": [
            ("St Davids", 51.8820, -5.2690, ["cathedral", "coast"]),
            ("Fishguard", 51.9930, -4.9760, ["harbour", "coast"]),
            ("Newport Pembrokeshire", 52.0160, -4.8330, ["coast", "estuary"]),
            ("Milford Haven", 51.7140, -5.0340, ["harbour", "waterway"]),
            ("Pembroke", 51.6760, -4.9160, ["castle", "town"]),
            ("Tenby", 51.6720, -4.7000, ["beach", "harbour"]),
            ("Saundersfoot", 51.7090, -4.7000, ["beach", "village"]),
        ],
    },
    {
        "id": "isle-of-skye",
        "name": "Isle of Skye",
        "center": {"lat": 57.35, "lng": -6.25},
        "zoom": 9,
        "bbox": [57.00, -6.75, 57.75, -5.65],
        "profile": "photographer-adventurous-colour",
        "tags": ["island", "coast", "mountains"],
        "anchors": [
            ("Portree", 57.4120, -6.1940, ["harbour", "town"]),
            ("Staffin", 57.6270, -6.2070, ["coast", "quiraing"]),
            ("Uig", 57.5860, -6.3760, ["ferry", "coast"]),
            ("Dunvegan", 57.4370, -6.5800, ["castle", "loch"]),
            ("Broadford", 57.2410, -5.9120, ["coast", "gateway"]),
            ("Elgol", 57.1460, -6.1090, ["coast", "cuillin"]),
            ("Armadale", 57.0650, -5.8990, ["ferry", "gardens"]),
        ],
    },
]


def build_regions() -> list[dict[str, Any]]:
    """Build map quick-jump regions for the UK scenic catalogue."""
    return [
        {
            "id": region["id"],
            "name": region["name"],
            "center": dict(region["center"]),
            "zoom": int(region["zoom"]),
            "bbox": list(region["bbox"]),
        }
        for region in REGION_DEFINITIONS
    ]


def _anchor(region_id: str, name: str, lat: float, lng: float, tags: list[str]) -> dict[str, Any]:
    return {
        "id": f"{region_id}-{_slug(name)}",
        "name": name,
        "lat": float(lat),
        "lng": float(lng),
        "tags": list(tags),
    }


def _route_record(
    route_from: dict[str, Any],
    route_to: dict[str, Any],
    region: dict[str, Any],
    sequence: int,
    reverse: bool = False,
) -> dict[str, Any]:
    start, end = (route_to, route_from) if reverse else (route_from, route_to)
    direction = "westbound" if reverse else "eastbound"
    tags = sorted(set(region["tags"] + start["tags"] + end["tags"] + [direction]))
    preference = round(min(0.95, 0.62 + 0.015 * (sequence % 10) + (0.06 if "coast" in tags or "mountain" in tags else 0.0)), 2)
    return {
        "id": f"{start['id']}-to-{end['id']}",
        "name": f"{start['name']} to {end['name']} Scenic Drive",
        "region": region["name"],
        "description": f"A routable scenic drive through {region['name']}, linking {start['name']} with {end['name']} via real UK roads.",
        "from": {"lat": start["lat"], "lng": start["lng"]},
        "to": {"lat": end["lat"], "lng": end["lng"]},
        "preference": preference,
        "profile": region.get("profile", "balanced"),
        "tags": tags,
    }


def build_presets() -> list[dict[str, Any]]:
    """Build named, routable UK scenic drive presets from real anchor places."""
    presets: list[dict[str, Any]] = []

    for region in REGION_DEFINITIONS:
        anchors = [_anchor(region["id"], name, lat, lng, tags) for name, lat, lng, tags in region["anchors"]]
        pairs: list[tuple[int, int]] = []
        pairs.extend((index, index + 1) for index in range(len(anchors) - 1))
        pairs.extend((index, index + 2) for index in range(len(anchors) - 2))
        if len(anchors) >= 5:
            pairs.extend([(0, len(anchors) - 1), (1, len(anchors) - 2)])

        for sequence, (start_index, end_index) in enumerate(pairs):
            outbound = _route_record(anchors[start_index], anchors[end_index], region, sequence, reverse=False)
            inbound = _route_record(anchors[start_index], anchors[end_index], region, sequence, reverse=True)
            presets.extend([outbound, inbound])

    long_drives = [
        ("highland-to-skye", "Fort William", 56.8198, -5.1052, "Portree", 57.4120, -6.1940, "Scottish Highlands to Isle of Skye", "remote-adventurous-terrain", ["mountains", "island", "lochs"]),
        ("dales-to-lakes", "Skipton", 53.9620, -2.0160, "Kendal", 54.3280, -2.7460, "Yorkshire Dales to Lake District", "pastoral-moderate-landcover", ["dales", "lakes", "market-towns"]),
        ("peaks-to-dales", "Bakewell", 53.2130, -1.6750, "Grassington", 54.0710, -1.9990, "Peak District to Yorkshire Dales", "valley-moderate-terrain", ["limestone", "dales", "national-parks"]),
        ("cotswolds-to-brecon", "Cirencester", 51.7180, -1.9680, "Brecon", 51.9470, -3.3910, "Cotswolds to Brecon Beacons", "heritage-moderate-colour", ["villages", "mountains", "cross-country"]),
        ("exmoor-to-dartmoor", "Lynton", 51.2290, -3.8350, "Tavistock", 50.5500, -4.1440, "Exmoor to Dartmoor", "moorland-moderate-terrain", ["moors", "national-parks", "devon"]),
        ("cornwall-coast-to-dartmoor", "St Ives", 50.2110, -5.4800, "Princetown", 50.5430, -3.9890, "Cornwall Coast to Dartmoor", "coastal-moderate-colour", ["coast", "moor", "south-west"]),
        ("snowdonia-to-pembrokeshire", "Betws-y-Coed", 53.0930, -3.8060, "St Davids", 51.8820, -5.2690, "Snowdonia to Pembrokeshire", "mountain-moderate-terrain", ["wales", "mountains", "coast"]),
        ("northumberland-to-dales", "Bamburgh", 55.6070, -1.7170, "Hawes", 54.3050, -2.1960, "Northumberland Coast to Yorkshire Dales", "big-sky-adventurous-terrain", ["coast", "dales", "big-sky"]),
        ("cairngorms-to-highlands", "Aviemore", 57.1950, -3.8250, "Ullapool", 57.8950, -5.1600, "Cairngorms to West Highlands", "mountain-adventurous-terrain", ["mountains", "lochs", "scotland"]),
        ("nc500-to-skye", "Ullapool", 57.8950, -5.1600, "Dunvegan", 57.4370, -6.5800, "North Coast 500 to Isle of Skye", "coastal-adventurous-landcover", ["coast", "island", "road-trip"]),
    ]
    for index, (slug, start_name, start_lat, start_lng, end_name, end_lat, end_lng, region_name, profile, tags) in enumerate(long_drives):
        base = {
            "id": slug,
            "name": f"{start_name} to {end_name} Grand Scenic Drive",
            "region": region_name,
            "description": f"A longer routable UK scenic drive from {start_name} to {end_name}, crossing well-known landscape regions.",
            "from": {"lat": start_lat, "lng": start_lng},
            "to": {"lat": end_lat, "lng": end_lng},
            "preference": round(0.84 + (index % 4) * 0.02, 2),
            "profile": profile,
            "tags": sorted(set(tags + ["long-drive"])),
        }
        reverse = {
            **base,
            "id": f"{slug}-reverse",
            "name": f"{end_name} to {start_name} Grand Scenic Drive",
            "from": {"lat": end_lat, "lng": end_lng},
            "to": {"lat": start_lat, "lng": start_lng},
        }
        presets.extend([base, reverse])

    seen: set[str] = set()
    unique_presets: list[dict[str, Any]] = []
    for preset in presets:
        if preset["id"] not in seen:
            unique_presets.append(preset)
            seen.add(preset["id"])
    return unique_presets


PROFILES = build_profiles()
PROFILE_BY_ID = {profile["id"]: profile for profile in PROFILES}
PRESETS = build_presets()
REGIONS = build_regions()


def get_profile(pid: str) -> dict[str, Any] | None:
    """Resolve a profile by id for route scoring code."""
    return PROFILE_BY_ID.get(pid)


router = APIRouter()


@router.get("/api/profiles")
def list_profiles() -> dict[str, Any]:
    return {"count": len(PROFILES), "profiles": PROFILES}


@router.get("/api/profiles/{id}")
def read_profile(id: str) -> dict[str, Any]:
    profile = get_profile(id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.get("/api/presets")
def list_presets(region: str | None = None, tag: str | None = None, q: str | None = None) -> dict[str, Any]:
    filtered = PRESETS
    if region:
        region_text = region.lower()
        filtered = [preset for preset in filtered if region_text in preset["region"].lower() or _slug(region) in _slug(preset["region"])]
    if tag:
        tag_text = tag.lower()
        filtered = [preset for preset in filtered if any(tag_text == item.lower() for item in preset["tags"])]
    if q:
        query = q.lower()
        filtered = [
            preset
            for preset in filtered
            if query in preset["name"].lower()
            or query in preset["description"].lower()
            or query in preset["region"].lower()
            or any(query in item.lower() for item in preset["tags"])
        ]
    return {"count": len(filtered), "presets": filtered}


@router.get("/api/presets/{id}")
def read_preset(id: str) -> dict[str, Any]:
    for preset in PRESETS:
        if preset["id"] == id:
            return preset
    raise HTTPException(status_code=404, detail="Preset not found")


@router.get("/api/regions")
def list_regions() -> dict[str, Any]:
    return {"count": len(REGIONS), "regions": REGIONS}
