"""
Spatial pre-filtering for routing on the full city graph.

Running Yen's k-shortest paths over all ~52k nodes / ~150k edges of Boston is
wasteful for a single walking trip: any sensible route stays near the straight
line between origin and destination. This module clips the graph to the region
that could plausibly contain the optimal route before routing.

The geometric tool is an **ellipse with foci at O and D**: for any node ``n`` on
a path from O to D, ``dist(O, n) + dist(n, D)`` is at most the path's length.
Keeping nodes whose foci-distance sum is within a budget therefore keeps every
node that any route shorter than that budget could use. We size the budget as a
multiple of the O–D distance (``detour_factor``) with an absolute floor for
short trips.

Choosing the budget — correctness vs. speed
--------------------------------------------
A walkability route can be longer than the shortest path: for the cost model
``cost = length·(1 + α·(1 − walk))`` the optimal route's length is bounded by
``(1 + α)×`` the shortest-path length. So a larger ``α`` warrants a larger
``detour_factor``. Rather than always paying for the worst case, router.py
starts tight and **widens-and-retries** when the best route hugs the ellipse
boundary (see ``find_routes``), falling back to the full graph if needed.

No scipy here (not installed); numpy vectorises the distance maths, and a small
coordinate cache is memoised on the graph so the node arrays are built once.
"""

from __future__ import annotations

import math

import networkx as nx
import numpy as np

from walkability.scoring.factors import EXCLUDED_FOOT_ACCESS, _as_str

# Clip tuning ---------------------------------------------------------------
DETOUR_FACTOR_DEFAULT: float = 1.5    # ellipse budget = this × O–D distance ...
MIN_BUFFER_M:          float = 400.0  # ... but never tighter than O–D + this
EARTH_RADIUS_M:        float = 6_371_000.0

_COORD_CACHE_KEY = "_walk_coord_cache"
_ROUTABLE_MASK_KEY = "_walk_routable_mask"


# ---------------------------------------------------------------------------
# Coordinate cache (built once per graph)
# ---------------------------------------------------------------------------

def _node_coords(G: nx.MultiDiGraph) -> tuple[list, np.ndarray, np.ndarray]:
    """Return (node_ids, lats, lons), memoised on ``G.graph``.

    node_ids is a plain list so original node-key types are preserved; lats and
    lons are float ndarrays for vectorised distance maths. Re-derived if the
    node count changes (cheap invalidation heuristic).
    """
    cache = G.graph.get(_COORD_CACHE_KEY)
    if cache is not None and cache[0] == G.number_of_nodes():
        return cache[1], cache[2], cache[3]

    ids: list = []
    lats: list[float] = []
    lons: list[float] = []
    for n, data in G.nodes(data=True):
        ids.append(n)
        lats.append(float(data["y"]))
        lons.append(float(data["x"]))

    lat_arr = np.asarray(lats, dtype=float)
    lon_arr = np.asarray(lons, dtype=float)
    G.graph[_COORD_CACHE_KEY] = (G.number_of_nodes(), ids, lat_arr, lon_arr)
    return ids, lat_arr, lon_arr


def _routable_mask(G: nx.MultiDiGraph) -> np.ndarray:
    """Boolean array (aligned to ``_node_coords`` order): True where a node lies in
    the **largest walkable connected component**.

    Snapping must avoid two traps near an address: ``foot=no`` dead-end stubs
    (no routable edge at all) and small disconnected footway fragments (routable
    but unreachable from the rest of the city — e.g. an isolated pedestrian-bridge
    spur). Both make every route fail. We build the walkable subgraph (edges that
    aren't ``foot=no``), take its biggest component, and only snap to that.
    Memoised on ``G.graph`` (keyed by node+edge count); pedestrian edges are
    almost all bidirectional, so undirected (weak) components suffice and are
    cheap.
    """
    cache = G.graph.get(_ROUTABLE_MASK_KEY)
    key = (G.number_of_nodes(), G.number_of_edges())
    if cache is not None and cache[0] == key:
        return cache[1]

    H = nx.Graph()
    for u, v, data in G.edges(data=True):
        if _as_str(data.get("foot_access")) not in EXCLUDED_FOOT_ACCESS:
            H.add_edge(u, v)
    main = max(nx.connected_components(H), key=len) if H.number_of_nodes() else set()

    ids, _, _ = _node_coords(G)
    mask = np.fromiter((n in main for n in ids), dtype=bool, count=len(ids))
    G.graph[_ROUTABLE_MASK_KEY] = (key, mask)
    return mask


# ---------------------------------------------------------------------------
# Distance helpers
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Works on scalars or numpy arrays."""
    p1 = np.radians(lat1)
    p2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlam = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


# ---------------------------------------------------------------------------
# Node snapping (vectorised)
# ---------------------------------------------------------------------------

def snap_to_node(G: nx.MultiDiGraph, lat: float, lon: float, routable_only: bool = False):
    """Return the id of the graph node nearest to (lat, lon).

    Vectorised equirectangular nearest-node search over the cached coordinate
    arrays (Δlon scaled by cos(lat) so east-west isn't over-counted). Avoids
    the scikit-learn dependency of ox.nearest_nodes.

    With ``routable_only=True``, nodes that have no walkable edge (``foot=no``
    stubs) are excluded, so an address never snaps to a dead end that can't be
    routed from. Falls back to the unrestricted nearest node if, implausibly,
    no routable node exists.
    """
    ids, lats, lons = _node_coords(G)
    if not ids:
        raise ValueError("Graph has no nodes to snap to.")
    cos_lat = math.cos(math.radians(lat))
    dy = lats - lat
    dx = (lons - lon) * cos_lat
    d2 = dy * dy + dx * dx
    if routable_only:
        mask = _routable_mask(G)
        if mask.any():
            d2 = np.where(mask, d2, np.inf)
    return ids[int(np.argmin(d2))]


# ---------------------------------------------------------------------------
# Ellipse clip
# ---------------------------------------------------------------------------

def ellipse_budget(
    G: nx.MultiDiGraph,
    o_node,
    d_node,
    detour_factor: float = DETOUR_FACTOR_DEFAULT,
    min_buffer_m: float = MIN_BUFFER_M,
) -> float:
    """The foci-distance-sum budget defining the clip ellipse for one query."""
    oy, ox = G.nodes[o_node]["y"], G.nodes[o_node]["x"]
    dy, dx = G.nodes[d_node]["y"], G.nodes[d_node]["x"]
    d_od = float(haversine_m(oy, ox, dy, dx))
    return max(d_od * detour_factor, d_od + min_buffer_m)


def foci_sum(G: nx.MultiDiGraph, o_node, d_node, node) -> float:
    """dist(O, node) + dist(node, D) for a single node (for boundary checks)."""
    oy, ox = G.nodes[o_node]["y"], G.nodes[o_node]["x"]
    dy, dx = G.nodes[d_node]["y"], G.nodes[d_node]["x"]
    ny, nx_ = G.nodes[node]["y"], G.nodes[node]["x"]
    return float(haversine_m(ny, nx_, oy, ox) + haversine_m(ny, nx_, dy, dx))


def clip_to_ellipse(
    G: nx.MultiDiGraph,
    o_node,
    d_node,
    detour_factor: float = DETOUR_FACTOR_DEFAULT,
    min_buffer_m: float = MIN_BUFFER_M,
) -> tuple[nx.MultiDiGraph, float]:
    """Return ``(subgraph_view, budget)`` keeping nodes inside the O–D ellipse.

    The subgraph is a read-only view into ``G`` (edge data shared, not copied),
    so it stays cheap. O and D are foci so they are always retained.
    """
    budget = ellipse_budget(G, o_node, d_node, detour_factor, min_buffer_m)
    oy, ox = G.nodes[o_node]["y"], G.nodes[o_node]["x"]
    dy, dx = G.nodes[d_node]["y"], G.nodes[d_node]["x"]

    ids, lats, lons = _node_coords(G)
    sum_dist = haversine_m(lats, lons, oy, ox) + haversine_m(lats, lons, dy, dx)
    mask = sum_dist <= budget
    keep = [ids[i] for i in np.nonzero(mask)[0]]
    return G.subgraph(keep), budget
