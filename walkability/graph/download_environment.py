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
  * boston_openspace.gpkg  — large parks + water (openness / sightlines).
  * boston_landuse.gpkg    — landuse=industrial polygons (truck-corridor
    down-weight of car-safety + warehouse enclosure discount).
  * boston_roads.gpkg      — ALL car-carrying road classes, for the distance-to-
    nearest-road that grades the car-safety ceiling (a separated greenway scores
    above the road-adjacent 0.85 cap; a calm-street sidewalk does not).

Run once:
    python walkability/graph/download_environment.py
    python walkability/graph/download_environment.py --force   # re-fetch

Consumed by graph/environment.py::build_environment_index during the build.
"""

from __future__ import annotations

import argparse
import math

import geopandas as gpd
import osmnx as ox

from walkability.config import CACHE_DIR
from walkability.graph.environment import (
    ARTERIALS_PATH,
    BUILDINGS_PATH,
    LANDUSE_PATH,
    OPENSPACE_PATH,
    POIS_PATH,
    ROADS_PATH,
)
from walkability.scoring.weights import (
    ARTERIAL_HIGHWAY_TAGS,
    LANDUSE_TAGS,
    ROAD_HIGHWAY_TAGS,
)

OPENSPACE_LEISURE = ["park", "garden", "nature_reserve", "recreation_ground",
                     "common", "playground"]

ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

PLACE = "Boston, Massachusetts, USA"


def _save(gdf: gpd.GeoDataFrame, path) -> None:
    """Write a GeoPackage, keeping the index out (GPKG dislikes MultiIndex)."""
    gdf = gdf.reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, driver="GPKG")
    print(f"  Saved {len(gdf):>7} features → {path.name}")


def _flatten(series):
    """Flatten an OSM tag column (list / NaN / number) to a clean str-or-None so
    the GeoPackage stays serialisable — used for maxspeed / tunnel / layer."""
    def f(v):
        if isinstance(v, list):
            v = v[0] if v else None
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return str(v)
    return series.map(f)


def download_arterials(force: bool = False) -> None:
    if ARTERIALS_PATH.exists() and not force:
        print(f"Arterials already cached at {ARTERIALS_PATH.name} (use --force).")
        return
    print(f"Fetching arterials ({', '.join(ARTERIAL_HIGHWAY_TAGS)}) ...")
    gdf = ox.features_from_place(PLACE, tags={"highway": ARTERIAL_HIGHWAY_TAGS})
    # Keep clean single-string highway values (drops the rare list/None rows so
    # the GeoPackage stays serialisable and reach lookup is unambiguous).
    gdf = gdf[gdf["highway"].isin(ARTERIAL_HIGHWAY_TAGS)]
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    # Keep maxspeed so off-path car-safety can use the road's ACTUAL posted speed
    # (most Boston arterials are 25 mph, not the class default). Also keep tunnel /
    # layer so load_arterials can drop UNDERGROUND segments — Boston's Big Dig
    # buries I-90/I-93 under fine surface footways, and a tunneled road imposes no
    # street-level pedestrian hostility. Flatten list/NaN to str so GPKG serialises.
    cols = ["geometry", "highway"]
    for c in ("maxspeed", "tunnel", "layer"):
        if c in gdf.columns:
            gdf[c] = _flatten(gdf[c])
            cols.append(c)
    _save(gdf[cols], ARTERIALS_PATH)


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
    # Keep the TYPE (amenity / shop) so the environment factor can weight
    # high-foot-traffic POIs (restaurant, cafe, …) above street furniture
    # (bench, waste_basket). Flatten any list-valued tags so GPKG can store them.
    cols = ["geometry"] + [c for c in ("amenity", "shop") if c in gdf.columns]
    gdf = gdf[cols].copy()
    for c in ("amenity", "shop"):
        if c in gdf.columns:
            gdf[c] = gdf[c].map(
                lambda v: v[0] if isinstance(v, list) and v
                else (v if isinstance(v, str) else None)
            )
    _save(gdf, POIS_PATH)


def download_openspace(force: bool = False) -> None:
    if OPENSPACE_PATH.exists() and not force:
        print(f"Open space already cached at {OPENSPACE_PATH.name} (use --force).")
        return
    print("Fetching open space (water + parks) ...")
    gdf = ox.features_from_place(
        PLACE, tags={"natural": "water", "leisure": OPENSPACE_LEISURE})
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    gdf["kind"] = ["water" if str(n) == "water" else "park"
                   for n in (gdf["natural"] if "natural" in gdf.columns else [None] * len(gdf))]
    _save(gdf[["geometry", "kind"]], OPENSPACE_PATH)


def download_landuse(force: bool = False) -> None:
    """landuse=industrial polygons → the truck-corridor / warehouse signal (A)."""
    if LANDUSE_PATH.exists() and not force:
        print(f"Landuse already cached at {LANDUSE_PATH.name} (use --force).")
        return
    print(f"Fetching landuse ({', '.join(LANDUSE_TAGS)}) ...")
    gdf = ox.features_from_place(PLACE, tags={"landuse": LANDUSE_TAGS})
    gdf = gdf[gdf["landuse"].isin(LANDUSE_TAGS)]
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    _save(gdf[["geometry", "landuse"]], LANDUSE_PATH)


def download_roads(force: bool = False) -> None:
    """ALL car-carrying road classes → distance-to-nearest-road for separation (B).

    Superset of arterials: includes residential/service/etc. so a path can be told
    apart from a road-free greenway. Geometry only (the distance is all we use)."""
    if ROADS_PATH.exists() and not force:
        print(f"Roads already cached at {ROADS_PATH.name} (use --force).")
        return
    print(f"Fetching all roads ({len(ROAD_HIGHWAY_TAGS)} classes) ...")
    gdf = ox.features_from_place(PLACE, tags={"highway": ROAD_HIGHWAY_TAGS})
    gdf = gdf[gdf["highway"].isin(ROAD_HIGHWAY_TAGS)]
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    # Keep tunnel / layer so load_roads can drop underground segments too: a path
    # ABOVE a buried road is genuinely road-SEPARATED, so the tunnel must not count
    # against road_separation (which would wrongly cap its car-safety ceiling).
    cols = ["geometry"]
    for c in ("tunnel", "layer"):
        if c in gdf.columns:
            gdf[c] = _flatten(gdf[c])
            cols.append(c)
    _save(gdf[cols], ROADS_PATH)


def main(force: bool = False) -> None:
    download_arterials(force)
    download_buildings(force)
    download_pois(force)
    download_openspace(force)
    download_landuse(force)
    download_roads(force)
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
