"""
One-time download of the OSM feature inputs for the environment factor.

Fetches three datasets for Boston and caches them as GeoPackages under
``data/osm/`` (build-time only — they are NOT deployed; the enriched GraphML
ships the baked ``environment_score``):

  * boston_arterials.gpkg — high-speed road geometry (motorway / trunk /
    primary / secondary + their _link ramps). Pulled SEPARATELY from the walk
    graph because ``network_type="walk"`` excludes motorway/trunk — the very
    classes a pedestrian feels most. Used for arterial-proximity.
  * boston_buildings.gpkg — building footprints (built enclosure / "eyes").
  * boston_pois.gpkg       — shop + amenity points (active frontage / "eyes").

Run once:
    python walkability/graph/download_environment.py
    python walkability/graph/download_environment.py --force   # re-fetch

Consumed by graph/environment.py::build_environment_index during the build.
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import osmnx as ox

from walkability.config import CACHE_DIR
from walkability.graph.environment import (
    ARTERIALS_PATH,
    BUILDINGS_PATH,
    POIS_PATH,
)
from walkability.scoring.weights import ARTERIAL_HIGHWAY_TAGS

ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

PLACE = "Boston, Massachusetts, USA"


def _save(gdf: gpd.GeoDataFrame, path) -> None:
    """Write a GeoPackage, keeping the index out (GPKG dislikes MultiIndex)."""
    gdf = gdf.reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, driver="GPKG")
    print(f"  Saved {len(gdf):>7} features → {path.name}")


def download_arterials(force: bool = False) -> None:
    if ARTERIALS_PATH.exists() and not force:
        print(f"Arterials already cached at {ARTERIALS_PATH.name} (use --force).")
        return
    print(f"Fetching arterials ({', '.join(ARTERIAL_HIGHWAY_TAGS)}) ...")
    gdf = ox.features_from_place(PLACE, tags={"highway": ARTERIAL_HIGHWAY_TAGS})
    # Keep clean single-string highway values (drops the rare list/None rows so
    # the GeoPackage stays serialisable and reach lookup is unambiguous).
    gdf = gdf[gdf["highway"].isin(ARTERIAL_HIGHWAY_TAGS)]
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
    _save(gdf[["geometry", "highway"]], ARTERIALS_PATH)


def download_buildings(force: bool = False) -> None:
    if BUILDINGS_PATH.exists() and not force:
        print(f"Buildings already cached at {BUILDINGS_PATH.name} (use --force).")
        return
    print("Fetching building footprints ...")
    gdf = ox.features_from_place(PLACE, tags={"building": True})
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    _save(gdf[["geometry"]], BUILDINGS_PATH)


def download_pois(force: bool = False) -> None:
    if POIS_PATH.exists() and not force:
        print(f"POIs already cached at {POIS_PATH.name} (use --force).")
        return
    print("Fetching shop + amenity POIs ...")
    gdf = ox.features_from_place(PLACE, tags={"shop": True, "amenity": True})
    # Points and polygon centroids both count as frontage; keep geometry only.
    _save(gdf[["geometry"]], POIS_PATH)


def main(force: bool = False) -> None:
    download_arterials(force)
    download_buildings(force)
    download_pois(force)
    print("Done. Now rebuild with --force so the environment factor bakes in.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download OSM feature inputs for the environment factor."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch even if the cached GeoPackages already exist.",
    )
    args = parser.parse_args()
    main(force=args.force)
