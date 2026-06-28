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

from walkability.scoring.factors import EXCLUDED_FOOT_ACCESS, _as_float, _as_str

# Clip tuning ---------------------------------------------------------------
DETOUR_FACTOR_DEFAULT: float = 1.5    # ellipse budget = this × O–D distance ...
MIN_BUFFER_M:          float = 400.0  # ... but never tighter than O–D + this
EARTH_RADIUS_M:        float = 6_371_000.0

# Phase-2 side-refinement tube (see clip_to_route / router.find_routes). The
# half-width of the corridor around a phase-1 route within which length is
# re-minimised. Wide enough to include both sidewalks of the widest street (so a
# side-switch is reachable), narrow enough to exclude the next parallel STREET
# (~block width), so phase 2 cannot wander to a different corridor. Kept at 35 m:
# 50 m was tried but let JP Centre (#10) jump to a shorter PARALLEL corridor
# (the "too wide" failure) instead of switching sides, and didn't fix its real
# segs #1-2 side issue — that needs the safety-value fix, not a wider tube.
TUBE_WIDTH_M: float = 35.0

# Walkability-biased snapping: an address snaps to the node minimising
# dist_m + (1 − highway_score)·SNAP_WALK_BIAS_M, so it prefers a sidewalk/footway
# a little farther over an arterial centreline right next to it (which would force
# the route to start ON the arterial). ~50 m means a footway up to ~that much
# farther can win over a road node, but a distant footway never beats a close one.
SNAP_WALK_BIAS_M: float = 50.0
_DEG_TO_M: float = math.pi / 180.0 * EARTH_RADIUS_M

_COORD_CACHE_KEY = "_walk_coord_cache"
_ROUTABLE_MASK_KEY = "_walk_routable_mask"
_NODE_QUALITY_KEY = "_walk_node_quality"


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


def _node_walk_quality(G: nx.MultiDiGraph) -> np.ndarray:
    """Per-node best road-type walkability (max ``highway_score`` over incident
    edges), aligned to ``_node_coords`` order and memoised on ``G.graph``.

    Used to bias snapping toward pedestrian-friendly nodes: a node touching a
    footway scores ~0.9, one only on a primary ~0.08, so the snap prefers the
    sidewalk over the arterial centreline. Uses ``highway_score`` (road type),
    not the composite walk_score, so it keys purely on "is this a walking way or
    a road" — independent of surface/environment.
    """
    cache = G.graph.get(_NODE_QUALITY_KEY)
    key = (G.number_of_nodes(), G.number_of_edges())
    if cache is not None and cache[0] == key:
        return cache[1]

    ids, _, _ = _node_coords(G)
    index = {n: i for i, n in enumerate(ids)}
    quality = np.zeros(len(ids), dtype=float)
    for u, v, data in G.edges(data=True):
        hs = _as_float(data.get("highway_score"))
        if hs is None:
            continue
        for n in (u, v):
            i = index[n]
            if hs > quality[i]:
                quality[i] = hs
    G.graph[_NODE_QUALITY_KEY] = (key, quality)
    return quality


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

def snap_to_node(G: nx.MultiDiGraph, lat: float, lon: float,
                 routable_only: bool = False, walk_bias: float = 0.0):
    """Return the id of the graph node nearest to (lat, lon).

    Vectorised equirectangular nearest-node search over the cached coordinate
    arrays (Δlon scaled by cos(lat) so east-west isn't over-counted). Avoids
    the scikit-learn dependency of ox.nearest_nodes.

    With ``routable_only=True``, nodes that have no walkable edge (``foot=no``
    stubs) are excluded, so an address never snaps to a dead end that can't be
    routed from. Falls back to the unrestricted nearest node if, implausibly,
    no routable node exists.

    With ``walk_bias > 0`` (metres), the choice minimises ``dist_m + (1 −
    highway_score)·walk_bias`` instead of raw distance, so an address prefers a
    nearby sidewalk/footway over an arterial centreline a few metres closer (which
    would otherwise force the route to start ON the arterial). ``walk_bias = 0``
    (the default) is the exact nearest node.
    """
    ids, lats, lons = _node_coords(G)
    if not ids:
        raise ValueError("Graph has no nodes to snap to.")
    cos_lat = math.cos(math.radians(lat))
    dy = lats - lat
    dx = (lons - lon) * cos_lat
    d2 = dy * dy + dx * dx
    if walk_bias > 0.0:
        # metres ≈ angular distance × earth radius (the lat/lon are in degrees)
        cost = np.sqrt(d2) * _DEG_TO_M + (1.0 - _node_walk_quality(G)) * walk_bias
    else:
        cost = d2
    if routable_only:
        mask = _routable_mask(G)
        if mask.any():
            cost = np.where(mask, cost, np.inf)
    return ids[int(np.argmin(cost))]


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


# ---------------------------------------------------------------------------
# Route tube clip (phase-2 side refinement)
# ---------------------------------------------------------------------------

def clip_to_route(G: nx.MultiDiGraph, route_nodes, width_m: float = TUBE_WIDTH_M) -> nx.MultiDiGraph:
    """Return a ``G.subgraph`` view of nodes within ``width_m`` of the route polyline.

    The polyline is the piecewise-linear path through ``route_nodes`` (a node-id
    sequence). Used by phase-2 refinement: re-routing for length inside this tube
    keeps the phase-1 corridor while letting the path pick the shorter side and
    drop zigzag crossings. The route's own nodes (incl. O and D) are always kept.

    Vectorised point-to-segment distance in a local equirectangular frame (Δlon
    scaled by cos(lat)); numpy only, no shapely — keeps the query path light.
    """
    if route_nodes is None or len(route_nodes) < 2:
        return G.subgraph(list(route_nodes or []))

    ids, lats, lons = _node_coords(G)
    rlat = np.fromiter((G.nodes[n]["y"] for n in route_nodes), dtype=float, count=len(route_nodes))
    rlon = np.fromiter((G.nodes[n]["x"] for n in route_nodes), dtype=float, count=len(route_nodes))

    lat0, lon0 = float(rlat.mean()), float(rlon.mean())
    cos0 = math.cos(math.radians(lat0))
    # planar metres relative to (lat0, lon0)
    px = (lons - lon0) * cos0 * _DEG_TO_M
    py = (lats - lat0) * _DEG_TO_M
    rx = (rlon - lon0) * cos0 * _DEG_TO_M
    ry = (rlat - lat0) * _DEG_TO_M

    # bbox prefilter so the per-segment maths runs on few nodes
    in_bbox = ((px >= rx.min() - width_m) & (px <= rx.max() + width_m)
               & (py >= ry.min() - width_m) & (py <= ry.max() + width_m))
    cand = np.nonzero(in_bbox)[0]
    if cand.size == 0:
        return G.subgraph(list(route_nodes))
    cpx, cpy = px[cand], py[cand]

    min_d2 = np.full(cand.size, np.inf)
    for i in range(len(rx) - 1):
        ax, ay, bx, by = rx[i], ry[i], rx[i + 1], ry[i + 1]
        abx, aby = bx - ax, by - ay
        L2 = abx * abx + aby * aby
        if L2 == 0.0:
            d2 = (cpx - ax) ** 2 + (cpy - ay) ** 2
        else:
            t = np.clip(((cpx - ax) * abx + (cpy - ay) * aby) / L2, 0.0, 1.0)
            d2 = (cpx - (ax + t * abx)) ** 2 + (cpy - (ay + t * aby)) ** 2
        np.minimum(min_d2, d2, out=min_d2)

    keep = [ids[cand[i]] for i in np.nonzero(min_d2 <= width_m * width_m)[0]]
    return G.subgraph(keep)
