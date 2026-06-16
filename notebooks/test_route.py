"""
Quick route tester — edit the values below and run:

    python notebooks/test_route.py

Tries a walk route between two lat/lon points, prints the top candidates and
the actual coordinate path, and (optionally) writes an interactive HTML map.
`ALPHA` is the distance/walkability tradeoff knob:
    0.0  → shortest path (walkability ignored)
    2.0  → balanced (default); detours toward more walkable edges
    5.0+ → strongly prefer walkable, accept longer routes
"""

from walkability.graph.build import ENRICHED_PATH, DEV_ENRICHED_PATH, load_graph
from walkability.routing.router import find_routes, inspect_route
from walkability.config import PROJECT_ROOT

# ---------------------------------------------------------------------------
# EDIT THESE
# ---------------------------------------------------------------------------
ORIGIN      = (42.3588, -71.0707)   # (lat, lon) — Beacon Hill
DESTINATION = (42.3601, -71.0631)   # (lat, lon) — Downtown Crossing-ish

ALPHA = 2.0                         # tradeoff knob (see module docstring)

GRAPH_PATH = ENRICHED_PATH          # full Boston graph; swap to DEV_ENRICHED_PATH
                                    # for the fast Beacon Hill subset

N_ROUTES = 3                        # how many candidate routes to print

# Set to a list of alphas to compare side by side, or None to use ALPHA only.
COMPARE_ALPHAS = [0.0, 2.0, 5.0]               # e.g. [0.0, 2.0, 5.0]

PRINT_COORDS = True                 # print the best route's lat/lon sequence
SHOW_MAP     = True                 # write an interactive HTML map of the route(s)
MAP_PATH     = PROJECT_ROOT / "notebooks" / "route_map.html"
# ---------------------------------------------------------------------------

G = load_graph(GRAPH_PATH)
print(f"\nOrigin:      {ORIGIN}\nDestination: {DESTINATION}\n")

alphas = COMPARE_ALPHAS or [ALPHA]


def route_coords(route):
    """RouteResult -> list of (lat, lon) along the path, in order."""
    return [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in route.nodes]


# Print stats (inspect_route) and keep the best route for each alpha.
best_by_alpha = []
for a in alphas:
    routes = inspect_route(G, ORIGIN, DESTINATION, alpha=a, n=N_ROUTES)
    print()
    if routes:
        best_by_alpha.append((a, routes[0]))

# --- Print the actual coordinate path of the best route at the primary alpha ---
if PRINT_COORDS and best_by_alpha:
    a, best = best_by_alpha[0]
    coords = route_coords(best)
    print(f"=== Best route at alpha={a}: {len(coords)} points, "
          f"{best.total_length:.0f} m ===")
    for lat, lon in coords:
        print(f"  {lat:.6f}, {lon:.6f}")
    print()

# --- Interactive map (one colour-coded polyline per alpha) ---
if SHOW_MAP and best_by_alpha:
    import folium

    mid = ((ORIGIN[0] + DESTINATION[0]) / 2, (ORIGIN[1] + DESTINATION[1]) / 2)
    fmap = folium.Map(location=mid, zoom_start=15, tiles="cartodbpositron")

    colours = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e"]
    for i, (a, best) in enumerate(best_by_alpha):
        folium.PolyLine(
            route_coords(best),
            color=colours[i % len(colours)],
            weight=5,
            opacity=0.8,
            tooltip=f"alpha={a}  len={best.total_length:.0f}m  "
                    f"walk={best.walk_score:.3f}  conf={best.confidence:.3f}",
        ).add_to(fmap)

    folium.Marker(ORIGIN, tooltip="Origin",
                  icon=folium.Icon(color="green", icon="play")).add_to(fmap)
    folium.Marker(DESTINATION, tooltip="Destination",
                  icon=folium.Icon(color="red", icon="stop")).add_to(fmap)

    fmap.save(str(MAP_PATH))
    print(f"Map written to {MAP_PATH}\n  Open it in a browser to see the path.")
