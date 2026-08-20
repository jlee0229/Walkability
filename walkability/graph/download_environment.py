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
  * boston_openspace.gpkg  — large parks + water + cemeteries (openness /
    sightlines). Cemeteries are tagged kind="cemetery" so their openness is
    discounted vs parks (see CEMETERY_OPENNESS_FACTOR).
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
from walkability.graph.inventory import BOSTON_PROFILE, CITY_PROFILES, CityProfile
from walkability.scoring.weights import (
    ARTERIAL_HIGHWAY_TAGS,
    LANDUSE_TAGS,
    PARKING_TAGS,
    ROAD_HIGHWAY_TAGS,
)

OPENSPACE_LEISURE = ["park", "garden", "nature_reserve", "recreation_ground",
                     "common", "playground"]

ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

# The extent (profile.places) and output layer paths (profile.env_layer_path)
# come from the CityProfile. They MUST cover the same extent as that city's walk
# graph — if the feature layers lagged behind a widened graph, border edges would
# silently get environment_score/safety = 0 (verify_system.py::check_data_source_seam).


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


def download_arterials(profile: CityProfile, force: bool = False) -> None:
    path = profile.env_layer_path("arterials")
    if path.exists() and not force:
        print(f"Arterials already cached at {path.name} (use --force).")
        return
    print(f"Fetching arterials ({', '.join(ARTERIAL_HIGHWAY_TAGS)}) ...")
    gdf = ox.features_from_place(profile.places, tags={"highway": ARTERIAL_HIGHWAY_TAGS})
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
    _save(gdf[cols], path)


def download_buildings(profile: CityProfile, force: bool = False) -> None:
    path = profile.env_layer_path("buildings")
    if path.exists() and not force:
        print(f"Buildings already cached at {path.name} (use --force).")
        return
    print("Fetching building footprints ...")
    gdf = ox.features_from_place(profile.places, tags={"building": True})
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    _save(gdf[["geometry"]], path)


def download_pois(profile: CityProfile, force: bool = False) -> None:
    path = profile.env_layer_path("pois")
    if path.exists() and not force:
        print(f"POIs already cached at {path.name} (use --force).")
        return
    print("Fetching shop + amenity POIs ...")
    gdf = ox.features_from_place(profile.places, tags={"shop": True, "amenity": True})
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
    _save(gdf, path)


def _openspace_kind(natural, landuse, amenity) -> str:
    """Classify an open-space polygon → water / cemetery / park. Cemeteries are
    kept separate so environment.py can DISCOUNT their openness (real sightlines /
    separation, but less pleasant than a park — see CEMETERY_OPENNESS_FACTOR)."""
    if str(natural) == "water":
        return "water"
    if str(landuse) == "cemetery" or str(amenity) == "grave_yard":
        return "cemetery"
    return "park"


def download_openspace(profile: CityProfile, force: bool = False) -> None:
    path = profile.env_layer_path("openspace")
    if path.exists() and not force:
        print(f"Open space already cached at {path.name} (use --force).")
        return
    print("Fetching open space (water + parks + cemeteries) ...")
    gdf = ox.features_from_place(
        profile.places, tags={"natural": "water", "leisure": OPENSPACE_LEISURE,
                              "landuse": "cemetery", "amenity": "grave_yard"})
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    cols = {c: (gdf[c] if c in gdf.columns else [None] * len(gdf))
            for c in ("natural", "landuse", "amenity")}
    gdf["kind"] = [_openspace_kind(n, l, a)
                   for n, l, a in zip(cols["natural"], cols["landuse"], cols["amenity"])]
    _save(gdf[["geometry", "kind"]], path)


def download_landuse(profile: CityProfile, force: bool = False) -> None:
    """landuse=industrial polygons → the truck-corridor / warehouse signal (A)."""
    path = profile.env_layer_path("landuse")
    if path.exists() and not force:
        print(f"Landuse already cached at {path.name} (use --force).")
        return
    print(f"Fetching landuse ({', '.join(LANDUSE_TAGS)}) ...")
    gdf = ox.features_from_place(profile.places, tags={"landuse": LANDUSE_TAGS})
    gdf = gdf[gdf["landuse"].isin(LANDUSE_TAGS)]
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    _save(gdf[["geometry", "landuse"]], path)


def download_roads(profile: CityProfile, force: bool = False) -> None:
    """ALL car-carrying road classes → distance-to-nearest-road for separation (B).

    Superset of arterials: includes residential/service/etc. so a path can be told
    apart from a road-free greenway. Geometry only (the distance is all we use)."""
    path = profile.env_layer_path("roads")
    if path.exists() and not force:
        print(f"Roads already cached at {path.name} (use --force).")
        return
    print(f"Fetching all roads ({len(ROAD_HIGHWAY_TAGS)} classes) ...")
    gdf = ox.features_from_place(profile.places, tags={"highway": ROAD_HIGHWAY_TAGS})
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
    _save(gdf[cols], path)


def download_parking(profile: CityProfile, force: bool = False) -> None:
    """Surface parking polygons → the strip-mall "false eyes" signal.

    A large surface lot between sidewalk and building marks a car-oriented strip;
    environment.py (load_parking) keeps only lots ≥ PARKING_MIN_AREA_M2 and
    discounts the eyes credit near them. Geometry only (proximity is all we use)."""
    path = profile.env_layer_path("parking")
    if path.exists() and not force:
        print(f"Parking already cached at {path.name} (use --force).")
        return
    print("Fetching surface parking (amenity=parking, parking=surface) ...")
    gdf = ox.features_from_place(profile.places, tags={"amenity": "parking"})
    # Keep surface lots: parking=surface, or untagged (OSM's default is surface).
    # Explicitly drop multi-storey / underground decks — those are buildings, not
    # the open tarmac moat we're penalising.
    if "parking" in gdf.columns:
        pk = gdf["parking"].astype("string")
        gdf = gdf[pk.isin(PARKING_TAGS) | pk.isna()]
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    _save(gdf[["geometry"]], path)


def main(profile: CityProfile, force: bool = False) -> None:
    print(f"[{profile.name}] Environment layers for: {', '.join(profile.places)}")
    download_arterials(profile, force)
    download_buildings(profile, force)
    download_pois(profile, force)
    download_openspace(profile, force)
    download_landuse(profile, force)
    download_roads(profile, force)
    download_parking(profile, force)
    print("Done. Now rebuild with --force so the environment factor bakes in.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download OSM feature inputs for the environment factor."
    )
    parser.add_argument(
        "--city", default=BOSTON_PROFILE.name, choices=sorted(CITY_PROFILES),
        help=f"City to fetch environment layers for (default: {BOSTON_PROFILE.name}).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch even if the cached GeoPackages already exist.",
    )
    args = parser.parse_args()
    main(CITY_PROFILES[args.city], force=args.force)
