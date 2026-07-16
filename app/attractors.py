"""Pluggable scenic attractors for explore-mode diversions.

Compact centroid packs (not a full WDPA dump). The UK national-park list is one
region pack; world packs cover iconic parks and landscape hotspots (~150–250
points total). Explore mode uses every pack whose points fall near the corridor.
"""
from __future__ import annotations

from . import config

# Each pack: list of (name, lat, lng).
UK_NATIONAL_PARKS: list[tuple[str, float, float]] = list(config.NATIONAL_PARKS)

UK_EXTRA: list[tuple[str, float, float]] = [
    ("Glen Coe", 56.682, -5.102),
    ("Torridon", 57.550, -5.550),
    ("Isle of Skye (Cuillin)", 57.230, -6.150),
    ("Assynt / Suilven", 58.120, -5.100),
    ("Snowdon summit approach", 53.068, -4.076),
    ("Wastwater", 54.450, -3.300),
    ("Malham Cove", 54.072, -2.158),
    ("Cheddar Gorge", 51.284, -2.764),
    # Bristol / North Somerset corridor (Yate ↔ Nailsea / Clevedon)
    ("Avon Gorge / Clifton", 51.455, -2.628),
    ("Clevedon seafront", 51.438, -2.854),
    ("Blagdon Lake", 51.333, -2.717),
    ("Mendip Hills (Black Down)", 51.318, -2.741),
    ("Jurassic Coast (Lulworth)", 50.620, -2.250),
    ("Giant's Causeway", 55.241, -6.511),
]

US_ICONIC: list[tuple[str, float, float]] = [
    ("Yosemite", 37.865, -119.538),
    ("Grand Canyon", 36.107, -112.113),
    ("Zion", 37.298, -113.026),
    ("Yellowstone", 44.600, -110.500),
    ("Grand Teton", 43.790, -110.700),
    ("Rocky Mountain NP", 40.342, -105.683),
    ("Glacier NP", 48.760, -113.787),
    ("Olympic NP", 47.802, -123.604),
    ("Great Smoky Mountains", 35.653, -83.507),
    ("Acadia", 44.339, -68.273),
    ("Arches", 38.733, -109.593),
    ("Bryce Canyon", 37.593, -112.187),
    ("Death Valley", 36.505, -117.079),
    ("Big Sur coast", 36.270, -121.807),
    ("Sequoia / Kings Canyon", 36.565, -118.773),
    ("Joshua Tree", 33.873, -115.901),
    ("Canyonlands", 38.327, -109.878),
    ("Capitol Reef", 38.367, -111.262),
    ("Mesa Verde", 37.231, -108.462),
    ("Badlands", 43.855, -102.340),
    ("Mount Rainier", 46.852, -121.760),
    ("Crater Lake", 42.944, -122.109),
    ("Redwood coast", 41.213, -124.005),
    ("Shenandoah", 38.293, -78.680),
    ("White Mountains NH", 44.270, -71.300),
    ("Maroon Bells", 39.071, -106.987),
    ("Trail Ridge Road", 40.392, -105.715),
    ("Going-to-the-Sun Road", 48.696, -113.718),
    ("Beartooth Highway", 45.000, -109.400),
    ("Pacific Coast Highway (Malibu)", 34.025, -118.780),
]

ALPS_DOLOMITES: list[tuple[str, float, float]] = [
    ("Chamonix / Mont Blanc", 45.923, 6.869),
    ("Interlaken", 46.686, 7.863),
    ("Jungfrau region", 46.548, 7.985),
    ("Zermatt / Matterhorn", 46.020, 7.749),
    ("Dolomites / Cortina", 46.540, 12.135),
    ("Grossglockner", 47.074, 12.693),
    ("Stelvio Pass", 46.529, 10.453),
    ("Berchtesgaden", 47.631, 13.002),
    ("Grindelwald", 46.624, 8.041),
    ("Lauterbrunnen", 46.593, 7.908),
    ("Aosta / Gran Paradiso", 45.603, 7.345),
    ("Courmayeur", 45.797, 6.969),
    ("Sella Pass", 46.509, 11.767),
    ("Tre Cime", 46.619, 12.303),
    ("Dachstein", 47.475, 13.606),
    ("Silvretta", 46.900, 10.100),
    ("Furka Pass", 46.573, 8.415),
    ("Gotthard", 46.559, 8.569),
    ("Engadin / St Moritz", 46.491, 9.835),
    ("Annecy lake", 45.899, 6.129),
    ("Lake Como (Bellagio)", 45.987, 9.258),
    ("Lake Garda (Riva)", 45.885, 10.845),
]

NZ_HIGHLIGHTS: list[tuple[str, float, float]] = [
    ("Fiordland / Milford", -44.672, 167.926),
    ("Queenstown", -45.031, 168.663),
    ("Wanaka", -44.694, 169.132),
    ("Aoraki / Mount Cook", -43.735, 170.096),
    ("Tongariro", -39.157, 175.632),
    ("Abel Tasman", -40.850, 173.000),
    ("Franz Josef Glacier", -43.387, 170.182),
    ("Fox Glacier", -43.465, 170.018),
    ("Doubtful Sound", -45.300, 166.980),
    ("Catlins coast", -46.550, 169.350),
    ("Coromandel", -36.760, 175.500),
    ("Bay of Islands", -35.250, 174.100),
    ("Rotorua geothermal", -38.136, 176.250),
    ("Kaikoura", -42.400, 173.680),
    ("Arthur's Pass", -42.945, 171.563),
]

PATAGONIA: list[tuple[str, float, float]] = [
    ("Torres del Paine", -51.000, -73.000),
    ("El Chaltén / Fitz Roy", -49.331, -72.886),
    ("Bariloche", -41.133, -71.310),
    ("Ushuaia", -54.802, -68.303),
    ("Perito Moreno Glacier", -50.496, -73.038),
    ("El Calafate", -50.340, -72.270),
    ("Puntiagudo / Osorno", -41.100, -72.500),
    ("Carretera Austral (Coyhaique)", -45.570, -72.070),
    ("Valdés Peninsula", -42.500, -63.900),
    ("Mendoza Andes", -32.890, -68.850),
]

ANDES_EXTRA: list[tuple[str, float, float]] = [
    ("Sacred Valley / Cusco", -13.520, -71.980),
    ("Machu Picchu approach", -13.163, -72.545),
    ("Lake Titicaca", -15.840, -69.340),
    ("Atacama / San Pedro", -22.910, -68.200),
    ("Cotopaxi", -0.677, -78.437),
    ("Quilotoa", -0.850, -78.900),
    ("Salar de Uyuni", -20.134, -67.489),
    ("Colca Canyon", -15.600, -71.900),
]

JAPAN_ALPS: list[tuple[str, float, float]] = [
    ("Kamikochi", 36.253, 137.637),
    ("Hakuba", 36.698, 137.862),
    ("Tateyama", 36.576, 137.619),
    ("Fuji five lakes", 35.489, 138.763),
    ("Nikko", 36.750, 139.600),
    ("Izu peninsula", 34.900, 139.000),
    ("Shirakawa-go", 36.257, 136.906),
    ("Kumano Kodo / Nachi", 33.668, 135.890),
    ("Aso caldera", 32.884, 131.104),
    ("Shiretoko", 44.000, 145.100),
    ("Daisetsuzan", 43.650, 142.850),
    ("Okinawa coast (Nago)", 26.590, 127.980),
]

SCANDINAVIAN_FJORDS: list[tuple[str, float, float]] = [
    ("Geirangerfjord", 62.101, 7.207),
    ("Nærøyfjord", 60.943, 6.932),
    ("Lofoten", 68.150, 13.600),
    ("Jotunheimen", 61.580, 8.300),
    ("Hardangervidda", 60.200, 7.500),
    ("Abisko", 68.349, 18.830),
    ("Trollstigen", 62.457, 7.675),
    ("Senja", 69.300, 17.600),
    ("Atlantic Road", 63.015, 7.350),
    ("Preikestolen", 58.986, 6.190),
    ("Sognefjord (Flåm)", 60.863, 7.114),
    ("Kungsleden / Kebnekaise", 67.900, 18.550),
]

CANADA_ROCKIES: list[tuple[str, float, float]] = [
    ("Banff", 51.496, -115.928),
    ("Jasper", 52.874, -118.081),
    ("Icefields Parkway", 52.200, -117.200),
    ("Lake Louise", 51.425, -116.177),
    ("Moraine Lake", 51.322, -116.186),
    ("Yoho / Emerald Lake", 51.443, -116.529),
    ("Kootenay", 50.700, -115.900),
    ("Whistler", 50.116, -122.957),
    ("Sea-to-Sky (Squamish)", 49.700, -123.150),
    ("Cabot Trail", 46.650, -60.850),
]

MEDITERRANEAN: list[tuple[str, float, float]] = [
    ("Amalfi Coast", 40.634, 14.603),
    ("Tuscany / Chianti", 43.450, 11.200),
    ("Cinque Terre", 44.127, 9.709),
    ("Provence / Luberon", 43.800, 5.200),
    ("Corsica (Calanques)", 42.270, 8.560),
    ("Costa Brava", 41.900, 3.200),
    ("Picos de Europa", 43.200, -4.800),
    ("Algarve cliffs", 37.080, -8.300),
    ("Santorini caldera", 36.393, 25.461),
    ("Dubrovnik coast", 42.650, 18.090),
    ("Plitvice", 44.865, 15.582),
    ("Lake Bled", 46.364, 14.094),
]

OTHER_WORLD: list[tuple[str, float, float]] = [
    ("Cape Town / Table Mountain", -33.962, 18.409),
    ("Garden Route", -33.980, 22.450),
    ("Ring of Kerry", 51.950, -9.900),
    ("Blue Ridge Parkway", 35.600, -82.500),
    ("Drakensberg", -29.000, 29.400),
    ("Namib dunes (Sossusvlei)", -24.730, 15.290),
    ("Victoria Falls approach", -17.924, 25.857),
    ("Serengeti", -2.333, 34.833),
    ("NgoroNgoro", -3.210, 35.560),
    ("Atlas / Todra Gorge", 31.550, -5.560),
    ("Petra approach", 30.329, 35.444),
    ("Wadi Rum", 29.582, 35.420),
    ("Cappadocia", 38.643, 34.829),
    ("Pamukkale", 37.916, 29.120),
    ("Himalaya (Pokhara)", 28.210, 83.990),
    ("Annapurna foothills", 28.400, 83.900),
    ("Ladakh (Leh)", 34.152, 77.577),
    ("Guilin / Yangshuo", 24.780, 110.500),
    ("Zhangjiajie", 29.117, 110.479),
    ("Jiuzhaigou", 33.200, 103.900),
    ("Tasmania (Cradle Mountain)", -41.683, 145.950),
    ("Great Ocean Road", -38.670, 143.100),
    ("Blue Mountains NSW", -33.700, 150.300),
    ("Uluru", -25.344, 131.037),
    ("Iceland (Golden Circle)", 64.255, -21.120),
    ("Iceland (South coast)", 63.400, -19.000),
    ("Faroe Islands", 62.000, -6.800),
    ("Scottish Highlands (Glen Coe)", 56.682, -5.102),
]

ATTRACTOR_PACKS: dict[str, list[tuple[str, float, float]]] = {
    "uk_national_parks": UK_NATIONAL_PARKS,
    "uk_extra": UK_EXTRA,
    "us_iconic": US_ICONIC,
    "alps_dolomites": ALPS_DOLOMITES,
    "nz": NZ_HIGHLIGHTS,
    "patagonia": PATAGONIA,
    "andes": ANDES_EXTRA,
    "japan_alps": JAPAN_ALPS,
    "scandinavian_fjords": SCANDINAVIAN_FJORDS,
    "canada_rockies": CANADA_ROCKIES,
    "mediterranean": MEDITERRANEAN,
    "other_world": OTHER_WORLD,
}


def all_attractors() -> list[tuple[str, float, float]]:
    """Flatten all packs (stable pack order, then list order)."""
    out: list[tuple[str, float, float]] = []
    seen: set[tuple[str, float, float]] = set()
    for pack in ATTRACTOR_PACKS.values():
        for item in pack:
            key = (item[0], round(item[1], 3), round(item[2], 3))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out
