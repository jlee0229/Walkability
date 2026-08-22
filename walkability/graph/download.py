"""Download the base OSM walk graph for a city.

    python -m walkability.graph.download            # default city (boston)
    python -m walkability.graph.download --city austin

The extent (osmnx query) and output path come from the city's CityProfile
(walkability/graph/inventory.py): profile.places → graph_from_place, saved to
profile.graph_path. Boston keeps the filename boston_walk.graphml (its PLACES
union expanded over time) so build.py / compact.py references don't churn.
"""

from __future__ import annotations

import argparse

import osmnx as ox

from walkability.config import CACHE_DIR
from walkability.graph.inventory import BOSTON_PROFILE, CITY_PROFILES, CityProfile

ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

# Way tags osmnx keeps, extended beyond its defaults. Tags are captured at
# DOWNLOAD time — a tag absent here never reaches the enriched graph, whatever
# build.py could do with it (the 2026-08-22 surface-blindness finding: dirt
# hiking trails scored as paved because `surface` was silently dropped, and
# foot_access could only ever come from `access` because `foot` was dropped).
#   surface   → OSM-tier surface_material_score (scoring/weights.SURFACE_SCORES)
#   foot      → foot_access (build.py; EXCLUDED/RESTRICTED sets in scoring/factors.py)
#   sac_scale → not scored yet; captured so a hiking-grade gate needs no re-download
ox.settings.useful_tags_way = list(ox.settings.useful_tags_way) + [
    "surface", "foot", "sac_scale",
]


def download_walk_graph(profile: CityProfile) -> None:
    print(f"[{profile.name}] Downloading walk graph for: {', '.join(profile.places)}")
    if profile.custom_filter is not None:
        # City-specific pedestrian filter (e.g. Austin keeps highway=cycleway for
        # its shared-use trail network + lake-bridge decks); network_type is
        # ignored when a custom_filter is given.
        print("  using custom pedestrian filter (keeps shared-use cycleways)")
        G = ox.graph_from_place(profile.places, custom_filter=profile.custom_filter)
    else:
        G = ox.graph_from_place(profile.places, network_type="walk")
    ox.save_graphml(G, profile.graph_path)
    print(f"  Nodes: {len(G.nodes)}, Edges: {len(G.edges)}")
    print(f"  Saved → {profile.graph_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Download the base OSM walk graph for a city.")
    ap.add_argument("--city", default=BOSTON_PROFILE.name, choices=sorted(CITY_PROFILES),
                    help=f"City to download (default: {BOSTON_PROFILE.name}).")
    args = ap.parse_args()
    download_walk_graph(CITY_PROFILES[args.city])
