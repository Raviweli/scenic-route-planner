"""Colour climate selection, world coverage, and HSV band behaviour."""
from __future__ import annotations

from PIL import Image

from app.climates import (
    ARID_HOT,
    CLIMATE_IDS,
    TEMPERATE_OCEANIC,
    TEMPERATE_NW_EUROPE,
    TROPICAL_RAINFOREST,
    TUNDRA_POLAR,
    classify_climate,
    get_climate,
    select_climate,
)
from app.roads import score_route
from app.scoring import analyse_image


def _solid(rgb: tuple[int, int, int], size: int = 64) -> Image.Image:
    return Image.new("RGB", (size, size), rgb)


# ~20 probes across continents + polar + ocean — none may be generic.
_WORLD_PROBES = [
    (54.5, -3.2, "temperate_oceanic"),       # Keswick / Lake District
    (51.5, -0.1, "temperate_oceanic"),       # London
    (47.5, -122.3, "temperate_oceanic"),     # Pacific NW
    (48.8, 2.3, "temperate_oceanic"),        # Paris fringe / NW Europe box
    (52.5, 13.4, "temperate_continental"),   # Berlin
    (40.7, -74.0, "temperate_continental"),  # NYC
    (41.9, 12.5, "mediterranean"),           # Rome
    (34.0, -118.2, "mediterranean"),         # LA / California
    (-3.1, -60.0, "tropical_rainforest"),    # Manaus / Amazon
    (0.3, 15.0, "tropical_rainforest"),      # Congo basin
    (20.0, 78.0, "tropical_monsoon_savanna"),  # Central India
    (33.0, -112.0, "arid_hot"),              # Phoenix / Sonoran
    (25.0, 30.0, "arid_hot"),                # Sahara / Egypt
    (-25.0, 133.0, "arid_hot"),              # Australian outback
    (43.0, 105.0, "arid_cold"),              # Gobi
    (30.0, -90.0, "subtropical_humid"),      # New Orleans / SE US
    (60.0, 25.0, "boreal"),                  # Finland
    (80.0, 0.0, "tundra_polar"),             # High Arctic
    (-75.0, 0.0, "tundra_polar"),            # Antarctica
    (30.0, -40.0, "oceanic_islands"),        # Mid-Atlantic ocean
]


def test_select_climate_uk_is_temperate_oceanic():
    c = select_climate(54.5, -3.2)
    assert c.id == "temperate_oceanic"
    # Alias still resolvable for older callers.
    assert get_climate("temperate_nw_europe").id == "temperate_nw_europe"
    assert get_climate("temperate_nw_europe").val_forest == TEMPERATE_OCEANIC.val_forest


def test_world_probes_never_generic():
    for lat, lng, expected in _WORLD_PROBES:
        cid = classify_climate(lat, lng)
        assert cid != "generic", f"{lat},{lng} fell through to generic"
        assert cid in CLIMATE_IDS, f"{lat},{lng} → unknown {cid}"
        assert select_climate(lat, lng).id == cid
        assert cid == expected, f"{lat},{lng}: got {cid}, want {expected}"


def test_grid_matches_classify_for_probes():
    for lat, lng, _ in _WORLD_PROBES:
        via_select = select_climate(lat, lng).id
        via_rules = classify_climate(lat, lng)
        assert via_select == via_rules


def test_alpine_elev_overlay():
    low = select_climate(46.5, 8.0, elev_m=500)
    high = select_climate(46.5, 8.0, elev_m=3000)
    assert high.id == "alpine"
    assert low.id != "alpine"


def test_temperate_green_scores_high():
    green = analyse_image(_solid((40, 120, 50)), climate=TEMPERATE_OCEANIC)
    grey = analyse_image(_solid((130, 130, 130)), climate=TEMPERATE_OCEANIC)
    assert green.score > grey.score
    # Alias climate must match oceanic numbers (UK lock).
    alias = analyse_image(_solid((40, 120, 50)), climate=TEMPERATE_NW_EUROPE)
    assert abs(alias.score - green.score) < 1e-6


def test_arid_protects_bright_sand_from_urban_penalty():
    sand = _solid((210, 200, 180))  # bright, low-sat
    temperate = analyse_image(sand, climate=TEMPERATE_OCEANIC)
    arid = analyse_image(sand, climate=ARID_HOT)
    assert arid.score > temperate.score
    assert arid.score >= 40
    # Protected sand must not be reported as grey/urban fraction.
    assert arid.grey_frac < 0.5


def test_desert_canyon_red_rock_scores_scenic():
    """Utah-style red sandstone must score scenic under arid_hot (not urban)."""
    canyon = analyse_image(_solid((180, 90, 55)), climate=ARID_HOT)
    city = analyse_image(_solid((145, 145, 148)), climate=ARID_HOT)
    assert canyon.score >= 55
    assert canyon.score - city.score >= 15
    assert canyon.green_frac < 0.2  # scenic without lush green


def test_savannah_gold_scores_without_deep_green():
    from app.climates import TROPICAL_MONSOON_SAVANNA

    gold = analyse_image(_solid((170, 140, 60)), climate=TROPICAL_MONSOON_SAVANNA)
    city = analyse_image(_solid((145, 145, 148)), climate=TROPICAL_MONSOON_SAVANNA)
    assert gold.score >= 55
    assert gold.score > city.score


def test_tropical_deep_green_scores_high():
    deep = _solid((20, 90, 35))
    tropical = analyse_image(deep, climate=TROPICAL_RAINFOREST)
    assert tropical.score >= 55
    assert tropical.green_frac > 0.5


def test_polar_bright_snow_not_urban():
    snow = _solid((235, 235, 240))
    polar = analyse_image(snow, climate=TUNDRA_POLAR)
    temperate = analyse_image(snow, climate=TEMPERATE_OCEANIC)
    assert polar.score > temperate.score
    assert polar.score >= 50


def test_temperate_oceanic_hsv_locked():
    """UK baseline HSV edges must not drift (retune other climates instead)."""
    c = TEMPERATE_OCEANIC
    assert c.h_green_min == 60.0
    assert c.h_green_max == 172.0
    assert c.s_green_min == 0.10
    assert c.h_water_min == 172.0
    assert c.h_water_max == 285.0
    assert c.s_grey_max == 0.10
    assert c.v_urban_min == 0.60
    assert c.val_forest == 0.92
    assert c.val_urban == 0.08


def test_non_oceanic_city_lower_than_countryside():
    """Golden solid fixtures: urban grey < countryside for calibrated climates."""
    from app.climates import ALPINE, MEDITERRANEAN, TEMPERATE_CONTINENTAL

    city = _solid((145, 145, 148))
    forest = _solid((35, 110, 45))
    olive = _solid((90, 110, 55))
    sand = _solid((210, 195, 165))
    snow = _solid((230, 232, 235))

    # Continental: forest countryside vs city
    cont_city = analyse_image(city, climate=TEMPERATE_CONTINENTAL)
    cont_green = analyse_image(forest, climate=TEMPERATE_CONTINENTAL)
    assert cont_city.score < 50
    assert cont_green.score >= 60
    assert cont_green.score - cont_city.score >= 15

    # Mediterranean: olive scrub vs city
    med_city = analyse_image(city, climate=MEDITERRANEAN)
    med_olive = analyse_image(olive, climate=MEDITERRANEAN)
    assert med_city.score < 50
    assert med_olive.score >= 55
    assert med_olive.score > med_city.score

    # Arid: sand countryside vs city
    arid_city = analyse_image(city, climate=ARID_HOT)
    arid_sand = analyse_image(sand, climate=ARID_HOT)
    assert arid_city.score < 50
    assert arid_sand.score >= 55
    assert arid_sand.score > arid_city.score

    # Alpine: snow/rock vs city
    alp_city = analyse_image(city, climate=ALPINE)
    alp_snow = analyse_image(snow, climate=ALPINE)
    assert alp_city.score < 50
    assert alp_snow.score >= 60
    assert alp_snow.score > alp_city.score


def test_all_twelve_climates_have_hsv_bands():
    for cid in CLIMATE_IDS:
        c = get_climate(cid)
        assert c.h_water_max > c.h_water_min
        assert c.h_green_max > c.h_green_min
        assert 0 < c.blend_colour < 1
        assert c.s_grey_max > 0


def test_multi_climate_route_samples_differ():
    """Synthetic corridor: samples in UK and Sahara get different climates."""
    route = {
        "coords": [
            (54.5, -3.2),   # temperate oceanic
            (54.6, -3.1),
            (40.0, 10.0),   # stepping stone
            (25.0, 30.0),   # arid hot
            (25.1, 30.1),
        ],
        "distance_km": 4000.0,
        "duration_min": 2400.0,
        "motorway_km": 0.0,
    }
    scored = score_route(route, features=None, source="synthetic", weights=None)
    used = set(scored["climates_used"])
    assert "temperate_oceanic" in used
    assert "arid_hot" in used
    assert scored["climate"] in used
    render_climates = {pt.get("climate") for pt in scored["render"] if pt.get("climate")}
    assert "temperate_oceanic" in render_climates
    assert "arid_hot" in render_climates


def test_alpine_and_arid_demo_corridor_climates():
    """Featured world corridors map to expected colour climates at midpoints."""
    from app.climates import select_climate

    # Denver → Aspen corridor spans arid_cold foothills into alpine high country.
    assert select_climate(39.739, -104.99).id == "arid_cold"
    assert select_climate(39.191, -106.818).id == "arid_cold"
    # Utah canyon country is arid_hot (not Great Basin steppe).
    assert select_climate(37.298, -113.026).id == "arid_hot"
    assert select_climate(37.593, -112.187).id == "arid_hot"
    # California Sierra / Big Sur must not fall into open-ocean islands.
    assert select_climate(37.745, -119.594).id == "mediterranean"
    assert select_climate(36.270, -121.807).id == "mediterranean"
    assert select_climate(46.353, 7.806).id in ("alpine", "mediterranean", "temperate_oceanic")
    assert select_climate(-44.85, 168.3).id == "temperate_oceanic"