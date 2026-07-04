# walkability/config.py
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OSM_DIR = DATA_DIR / "osm"
CACHE_DIR = OSM_DIR / "cache"

# Create all dirs on import
for d in [OSM_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Municipalities the walk graph and its OSM feature layers cover. osmnx's
# graph_from_place / features_from_place accept a list and union the polygons,
# so this stays a tight boundary (no bbox harbor/ocean overshoot). Brookline is
# included because it is an independent town enclaved into Boston's southwest —
# without it, Allston/Brighton is a disconnected pedestrian component and
# cross-Boston routes (e.g. Allston → Jamaica Plain) fail or detour. Cambridge,
# Somerville, Everett, and Chelsea close the northern hull: without them every
# Charlestown trip is forced over the single North Washington St bridge (the
# alternates run through Somerville/Everett), the Charles River basin crossings
# dead-end at the Cambridge bank, and East Boston ↔ Chelsea is unreachable.
# Only Boston carries a city sidewalk inventory; every other municipality falls
# through to the OSM tier (graceful degradation) — see
# notebooks/verify_system.py::check_data_source_seam.
PLACES = [
    "Boston, Massachusetts, USA",
    "Brookline, Massachusetts, USA",
    "Cambridge, Massachusetts, USA",
    "Somerville, Massachusetts, USA",
    "Everett, Massachusetts, USA",
    "Chelsea, Massachusetts, USA",
]