# walkability/config.py
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OSM_DIR = DATA_DIR / "osm"
CACHE_DIR = OSM_DIR / "cache"

# Create all dirs on import
for d in [OSM_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)