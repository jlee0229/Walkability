from walkability.config import OSM_DIR, CACHE_DIR
import pandas as pd

import osmnx as ox

ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

G = ox.graph_from_place("Boston, Massachusetts, USA", network_type="walk")
ox.save_graphml(G, OSM_DIR / "boston_walk.graphml")

# print(f"Nodes: {len(G.nodes)}, Edges: {len(G.edges)}")

nodes, edges = ox.graph_to_gdfs(G)
# print(edges.columns.tolist())      # what fields exist?

# pd.set_option('display.max_rows', None)

# 2. Run your value_counts
# print(edges['highway'].value_counts())

# 3. Reset back to default if needed
# pd.reset_option('display.max_rows')



print(edges.groupby("highway")["width"].apply(lambda x: x.notna().mean()).sort_values(ascending=False))



# for col in ['width', 'access', 'service', 'bridge', 'tunnel']:
#     filled = edges[col].notna().sum()
#     total = len(edges)
#     print(f"{col}: {filled}/{total} ({100*filled/total:.1f}% populated)")


# print(nodes.columns.tolist()) 

# for type in edges.columns.tolist():
#     print(edges[type].value_counts())


# for type in nodes.columns.tolist():
#     print(nodes[type].value_counts())

# print(edges["highway"].value_counts())  # road types
# print(edges["sidewalk"].value_counts()) # how complete is this?