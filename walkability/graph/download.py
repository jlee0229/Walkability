from walkability.config import OSM_DIR, CACHE_DIR, PLACES
import pandas as pd

import osmnx as ox

ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

# PLACES unions Boston + Brookline (see config.py) so the graph is one connected
# pedestrian component; the filename stays boston_walk.graphml (content expands)
# to avoid churn in build.py / compact.py, which reference it by fixed path.
G = ox.graph_from_place(PLACES, network_type="walk")
ox.save_graphml(G, OSM_DIR / "boston_walk.graphml")

print(f"Nodes: {len(G.nodes)}, Edges: {len(G.edges)}")

nodes, edges = ox.graph_to_gdfs(G)
