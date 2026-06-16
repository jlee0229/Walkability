from walkability.config import OSM_DIR, CACHE_DIR
import osmnx as ox
import geopandas as gpd
import matplotlib.pyplot as plt

# 1. Load your existing sidewalk shapefile
sidewalks = gpd.read_file("data/boston/sidewalk_centerline/sidewalk_centerline.shp")

# 2. Download the background OSM network using OSMnx
# We will pull the pedestrian network for Boston to match your data
ox.settings.cache_folder = str(CACHE_DIR)
ox.settings.use_cache = True

G = ox.graph_from_place("Boston, Massachusetts, USA", network_type="walk")
ox.save_graphml(G, OSM_DIR / "boston_walk.graphml")

nodes, osm_edges = ox.graph_to_gdfs(G)

# 3. CRITICAL STEP: Align the Coordinate Reference Systems (CRS)
# If your shapefile is projected in feet/meters, we match it to OSMnx's Lat/Long (WGS84, EPSG:4326)
# if sidewalks.crs != osm_edges.crs:
#     print(f"Reprojecting shapefile from {sidewalks.crs} to match OSM network ({osm_edges.crs})...")
#     sidewalks = sidewalks.to_crs(osm_edges.crs)

# 4. Create a single matplotlib figure and axis object
fig, ax = plt.subplots(figsize=(12, 10))

# 5. Plot Layer 1: The background OSM network (Muted color so it acts as a base)
osm_edges.plot(
    ax=ax, 
    color="#dddddd", 
    linewidth=0.8, 
    zorder=1, 
    label="OSM Pedestrian Network"
)

# 6. Plot Layer 2: Your shapefile data overlayed directly on top
sidewalks.plot(
    ax=ax, 
    column='TYPE',      # Color the map based on a specific attribute column
    legend=True,         # Add a color legend
    cmap='Set3',         # Choose a matplotlib colormap
    edgecolor='black',   # Outline color for the boundaries
    linewidth=0.5,       # Outline thickness
    zorder=2             # Higher zorder places this on top of the OSM network
)

# 7. Customize titles, labels, and axis formatting
ax.set_title("Boston Sidewalk Centerlines Layered Over OSM Network", fontsize=14, fontweight='bold')
ax.set_xlabel("Longitude (Degrees East)", fontsize=11)
ax.set_ylabel("Latitude (Degrees North)", fontsize=11)

# Turn on the grid and ensure clean decimal formats for Lat/Long
ax.grid(True, linestyle="--", alpha=0.5)
ax.ticklabel_format(useOffset=False, style='plain')

plt.tight_layout()

plt.savefig('boston_overlayed_pedestrian.png')
