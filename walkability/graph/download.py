from walkability.config import OSM_DIR, CACHE_DIR
import pandas as pd

import osmnx as ox

ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

G = ox.graph_from_place("Boston, Massachusetts, USA", network_type="walk")
ox.save_graphml(G, OSM_DIR / "boston_walk.graphml")

print(f"Nodes: {len(G.nodes)}, Edges: {len(G.edges)}")

nodes, edges = ox.graph_to_gdfs(G)
