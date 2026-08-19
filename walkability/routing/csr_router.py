"""
CSR-native routing over a :class:`~walkability.graph.csr.RoutingGraph`.

This is the Phase-2 twin of ``routing/router.py`` + ``routing/clip.py``: the same
pipeline (snap → ellipse clip → cheapest-parallel projection → A* + penalty-method
alternatives → widen/retry → confidence re-rank → phase-2 side refinement) but
operating on flat CSR arrays and **integer node indices** instead of a NetworkX
MultiDiGraph, so there is no per-edge Python object on the query path.

Parity with the NetworkX path is the bar, so the graph-touching primitives are
faithful ports:

  * snapping reuses the exact equirectangular-argmin maths of ``clip.snap_to_node``;
  * the projection reproduces ``_routable_digraph``'s cheapest-parallel-edge rule
    with **first-appearance order** and **strict-`<` first-wins** tie-break;
  * :func:`_astar_csr` is a line-for-line port of ``networkx.astar_path`` (same
    heap, same global push counter, same ``enqueued``/``explored`` logic) run over
    the projected adjacency, so it returns the **identical node path**, not merely
    an equal-cost one.

Everything *pure* — the cost model, the two-level HDI scoring, the route
aggregation, the re-rank, the tuning constants, ``RouteResult`` — is imported from
``routing/cost.py``, ``scoring/factors.py`` and ``routing/router.py`` and shared
verbatim, so scoring can never drift between the two substrates.
"""

from __future__ import annotations

import math
from collections import defaultdict
from heapq import heappop, heappush
from itertools import count

import numpy as np

from walkability.graph.csr import RoutingGraph
from walkability.routing import clip
from walkability.routing.cost import (
    ALPHA_DEFAULT,
    RESTRICTED_ACCESS_PENALTY,
    _MISSING_LENGTH,
    edge_cost,
)
from walkability.routing.router import (
    ALT_MAX_STRETCH,
    ALT_PENALTY,
    BOUNDARY_EPS,
    CONF_TIEBREAK_BETA,
    K_DEFAULT,
    MAX_CANDIDATES,
    MAX_WIDENS,
    MIN_CONFIDENCE,
    REFINE_ALPHA,
    REFINE_CROSSING_CREDIT,
    REFINE_SCORE_TOL,
    TIE_EPSILON,
    WIDEN_FACTOR,
    RouteResult,
    _aggregate_route_dimensions,
    _rank_score,
)
from walkability.scoring.factors import (
    RESTRICTED_FOOT_ACCESS,
    _EMPTY_WALK,
    _as_str,
    apply_freeway_veto,
    combine_categories,
    edge_category_scores,
    edge_walkability,
)
from walkability.scoring.weights import FACTOR_WEIGHTS

_DEG_TO_M = clip._DEG_TO_M


# ---------------------------------------------------------------------------
# Per-graph cached masks (routable component + node walk-quality), memoised on
# the RoutingGraph, mirroring clip.py's caches keyed by node+edge count.
# ---------------------------------------------------------------------------

def _routable_mask(g: RoutingGraph) -> np.ndarray:
    """Boolean per-node mask: True where the node is in the largest walkable
    component (edges whose ``foot_access`` is not ``no``). Mirrors
    ``clip._routable_mask`` but via union-find over the CSR arrays (no networkx)."""
    key = (g.num_nodes(), g.num_edges())
    cached = getattr(g, "_csr_routable_mask", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    n = g.num_nodes()
    excluded_code = (g.foot_vocab.index("no") + 1) if "no" in g.foot_vocab else 0
    parent = np.arange(n, dtype=np.int64)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    dst = g.dst
    src = g.edge_src
    foot = g.foot_codes
    for e in range(g.num_edges()):
        if excluded_code and foot[e] == excluded_code:
            continue
        ra, rb = find(int(src[e])), find(int(dst[e]))
        if ra != rb:
            parent[ra] = rb

    roots = np.fromiter((find(i) for i in range(n)), dtype=np.int64, count=n)
    if n:
        vals, counts = np.unique(roots, return_counts=True)
        main = vals[int(np.argmax(counts))]
        mask = roots == main
    else:
        mask = np.zeros(0, dtype=bool)
    g._csr_routable_mask = (key, mask)
    return mask


def _node_quality(g: RoutingGraph) -> np.ndarray:
    """Per-node max ``highway_score`` over incident edges (walk-bias snapping),
    mirroring ``clip._node_walk_quality``."""
    key = (g.num_nodes(), g.num_edges())
    cached = getattr(g, "_csr_node_quality", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    hs = g.float_fields["highway_score"]
    quality = np.zeros(g.num_nodes(), dtype=float)
    src = g.edge_src
    dst = g.dst
    for e in range(g.num_edges()):
        v = hs[e]
        if v != v:  # NaN
            continue
        v = float(v)
        su, sv = int(src[e]), int(dst[e])
        if v > quality[su]:
            quality[su] = v
        if v > quality[sv]:
            quality[sv] = v
    g._csr_node_quality = (key, quality)
    return quality


# ---------------------------------------------------------------------------
# Snapping (vectorised equirectangular argmin — port of clip.snap_to_node)
# ---------------------------------------------------------------------------

def _snap(g: RoutingGraph, lat: float, lon: float,
          routable_only: bool = False, walk_bias: float = 0.0) -> int:
    """Nearest node **index** to (lat, lon); see ``clip.snap_to_node``."""
    lats, lons = g.node_y, g.node_x
    if lats.size == 0:
        raise ValueError("Graph has no nodes to snap to.")
    cos_lat = math.cos(math.radians(lat))
    dy = lats - lat
    dx = (lons - lon) * cos_lat
    d2 = dy * dy + dx * dx
    if walk_bias > 0.0:
        cost = np.sqrt(d2) * _DEG_TO_M + (1.0 - _node_quality(g)) * walk_bias
    else:
        cost = d2
    if routable_only:
        mask = _routable_mask(g)
        if mask.any():
            cost = np.where(mask, cost, np.inf)
    return int(np.argmin(cost))


# ---------------------------------------------------------------------------
# Ellipse / tube clip → allowed-node boolean masks (no subgraph materialised)
# ---------------------------------------------------------------------------

def _foci_sum(g: RoutingGraph, o_idx: int, d_idx: int, n_idx: int) -> float:
    oy, ox = g.node_y[o_idx], g.node_x[o_idx]
    dy, dx = g.node_y[d_idx], g.node_x[d_idx]
    ny, nx_ = g.node_y[n_idx], g.node_x[n_idx]
    return float(clip.haversine_m(ny, nx_, oy, ox) + clip.haversine_m(ny, nx_, dy, dx))


def _ellipse_mask(g: RoutingGraph, o_idx: int, d_idx: int,
                  detour_factor: float, min_buffer_m: float) -> tuple[np.ndarray, float]:
    """(allowed-node mask, budget) for the O–D clip ellipse (see clip.clip_to_ellipse)."""
    oy, ox = g.node_y[o_idx], g.node_x[o_idx]
    dy, dx = g.node_y[d_idx], g.node_x[d_idx]
    d_od = float(clip.haversine_m(oy, ox, dy, dx))
    budget = max(d_od * detour_factor, d_od + min_buffer_m)
    sum_dist = (clip.haversine_m(g.node_y, g.node_x, oy, ox)
                + clip.haversine_m(g.node_y, g.node_x, dy, dx))
    return sum_dist <= budget, budget


def _tube_mask(g: RoutingGraph, route_idx: list[int], width_m: float) -> np.ndarray:
    """Allowed-node mask within ``width_m`` of the route polyline (see clip.clip_to_route)."""
    n = g.num_nodes()
    if not route_idx or len(route_idx) < 2:
        mask = np.zeros(n, dtype=bool)
        for i in route_idx:
            mask[i] = True
        return mask

    lats, lons = g.node_y, g.node_x
    rlat = lats[route_idx]
    rlon = lons[route_idx]
    lat0, lon0 = float(rlat.mean()), float(rlon.mean())
    cos0 = math.cos(math.radians(lat0))
    px = (lons - lon0) * cos0 * _DEG_TO_M
    py = (lats - lat0) * _DEG_TO_M
    rx = (rlon - lon0) * cos0 * _DEG_TO_M
    ry = (rlat - lat0) * _DEG_TO_M

    in_bbox = ((px >= rx.min() - width_m) & (px <= rx.max() + width_m)
               & (py >= ry.min() - width_m) & (py <= ry.max() + width_m))
    cand = np.nonzero(in_bbox)[0]
    mask = np.zeros(n, dtype=bool)
    if cand.size == 0:
        for i in route_idx:
            mask[i] = True
        return mask
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
    mask[cand[min_d2 <= width_m * width_m]] = True
    for i in route_idx:
        mask[i] = True  # the route's own nodes are always kept
    return mask


# ---------------------------------------------------------------------------
# Cheapest-parallel-edge projection (port of _routable_digraph over CSR)
# ---------------------------------------------------------------------------

def _foot_code_sets(g: RoutingGraph) -> tuple[int, np.ndarray]:
    """(excluded_code, restricted_codes) for ``foot_codes``, cached on the graph.
    Code 0 is None/absent; codes index ``foot_vocab`` + 1."""
    cached = getattr(g, "_csr_foot_sets", None)
    if cached is not None:
        return cached
    vocab = g.foot_vocab
    excl = (vocab.index("no") + 1) if "no" in vocab else -1
    restricted = np.asarray(
        [vocab.index(s) + 1 for s in RESTRICTED_FOOT_ACCESS if s in vocab],
        dtype=np.int64,
    )
    g._csr_foot_sets = (excl, restricted)
    return excl, restricted


def _fast_cost_array(g: RoutingGraph, alpha: float, o_idx: int, d_idx: int) -> np.ndarray:
    """Vectorised per-edge routable cost for the **baked fast path** (default
    ``FACTOR_WEIGHTS``). Bit-exact to ``edge_cost``: ``length·(1+α·(1−walk_score))``
    with missing length → ``_MISSING_LENGTH``, ``foot=no`` → ``inf`` (dropped), and
    restricted-but-passable → ``×RESTRICTED_ACCESS_PENALTY`` **except** on terminal
    edges (leaving ``o_idx`` / entering ``d_idx``). Rare edges lacking a baked
    ``walk_score`` are patched individually via ``edge_cost`` so parity holds even
    where the fast path would fall through."""
    ws = g.float_fields["walk_score"]
    length = g.float_fields["length"]
    L = np.where(np.isnan(length), _MISSING_LENGTH, length)
    cost = L * (1.0 + alpha * (1.0 - ws))  # NaN where walk_score is NaN

    excl, restricted = _foot_code_sets(g)
    foot = g.foot_codes
    terminal = (g.edge_src == o_idx) | (g.dst == d_idx)
    if restricted.size:
        rmask = np.isin(foot, restricted) & ~terminal
        cost = np.where(rmask, cost * RESTRICTED_ACCESS_PENALTY, cost)
    if excl > 0:
        cost = np.where(foot == excl, np.inf, cost)

    nan_ws = np.isnan(ws)
    if nan_ws.any():  # no baked score → recompute exactly via edge_cost
        view = g.edge_view(0)
        for e in np.nonzero(nan_ws)[0]:
            e = int(e)
            view.idx = e
            is_terminal = (int(g.edge_src[e]) == o_idx) or (int(g.dst[e]) == d_idx)
            c = edge_cost(view, alpha, is_terminal=is_terminal, weights=FACTOR_WEIGHTS)
            cost[e] = np.inf if c is None else c
    return cost


def _project(
    g: RoutingGraph,
    alpha: float,
    o_idx: int,
    d_idx: int,
    weights: dict,
    allowed: np.ndarray | None = None,
    edge_ok=None,
):
    """Projected adjacency over ``allowed`` nodes.

    Returns ``(succ, pair)`` where ``succ[u]`` is a list ``[(v, base_cost, eidx), …]``
    of the cheapest traversable parallel edge to each successor ``v``, in
    first-appearance (source-adjacency) order, and ``pair[(u, v)] = (base_cost,
    eidx)`` for O(1) hop lookup during route reconstruction. ``foot=no`` edges
    (``edge_cost`` → None) are dropped, exactly like ``_routable_digraph``.

    Terminal edges (leaving ``o_idx`` or entering ``d_idx``) are costed without the
    restricted-access penalty (``edge_cost(is_terminal=True)``), matching the Nx
    projection's endpoint exemption.
    """
    succ: dict[int, list] = {}
    pair: dict[tuple, tuple] = {}
    indptr = g.indptr
    dst = g.dst

    # Fast path (default weights): read a vectorised, bit-exact cost array instead
    # of calling edge_cost per edge. Custom weights recompute per edge.
    fast = weights is FACTOR_WEIGHTS
    cost_arr = _fast_cost_array(g, alpha, o_idx, d_idx) if fast else None
    view = None if fast else g.edge_view(0)  # reused flyweight (mutated per edge)

    # Iterate only clipped nodes (np.nonzero) — a small ellipse over a 160k-node
    # graph must not pay a 160k-node skip loop.
    node_iter = range(g.num_nodes()) if allowed is None else (int(i) for i in np.nonzero(allowed)[0])
    for u in node_iter:
        lo, hi = int(indptr[u]), int(indptr[u + 1])
        row = None
        for e in range(lo, hi):
            v = int(dst[e])
            if allowed is not None and not allowed[v]:
                continue
            if edge_ok is not None and not edge_ok(e):
                continue
            if fast:
                c = cost_arr[e]
                if not math.isfinite(c):
                    continue  # foot=no — not routable
                c = float(c)
            else:
                view.idx = e
                is_terminal = (u == o_idx) or (v == d_idx)
                c = edge_cost(view, alpha, is_terminal=is_terminal, weights=weights)
                if c is None:
                    continue  # foot=no — not routable
            key = (u, v)
            existing = pair.get(key)
            if existing is None:
                if row is None:
                    row = []
                    succ[u] = row
                row.append((v, c, e))
                pair[key] = (c, e)
            elif c < existing[0]:
                # strict-< first-wins: replace value, keep first-appearance slot
                pair[key] = (c, e)
                for i, (vv, _, _) in enumerate(row):
                    if vv == v:
                        row[i] = (v, c, e)
                        break
    return succ, pair


# ---------------------------------------------------------------------------
# A* — faithful port of networkx.astar_path over the projected adjacency
# ---------------------------------------------------------------------------

def _astar_csr(succ, source, target, heuristic, penalty) -> list[int] | None:
    """Shortest path source→target over ``succ`` under a per-(u,v) penalty
    multiplier. Byte-for-byte the tie-breaking behaviour of ``networkx.astar_path``
    (global push counter, ``enqueued``/``explored`` lazy decrease-key)."""
    push, pop = heappush, heappop
    c = count()
    queue = [(0, next(c), source, 0, None)]
    enqueued: dict = {}
    explored: dict = {}
    while queue:
        _, __, curnode, dist, parent = pop(queue)
        if curnode == target:
            path = [curnode]
            node = parent
            while node is not None:
                path.append(node)
                node = explored[node]
            path.reverse()
            return path
        if curnode in explored:
            if explored[curnode] is None:
                continue
            qcost, h = enqueued[curnode]
            if qcost < dist:
                continue
        explored[curnode] = parent
        for (neighbor, base, _eidx) in succ.get(curnode, ()):  # first-appearance order
            ncost = dist + base * penalty[(curnode, neighbor)]
            if neighbor in enqueued:
                qcost, h = enqueued[neighbor]
                if qcost <= ncost:
                    continue
            else:
                h = heuristic(neighbor)
            enqueued[neighbor] = (ncost, h)
            push(queue, (ncost + h, next(c), neighbor, ncost, curnode))
    return None


def _heuristic(g: RoutingGraph, target: int):
    ty, tx = float(g.node_y[target]), float(g.node_x[target])

    def h(n_idx: int) -> float:
        return float(clip.haversine_m(g.node_y[n_idx], g.node_x[n_idx], ty, tx))

    return h


# ---------------------------------------------------------------------------
# Route reconstruction (port of _build_route in index space)
# ---------------------------------------------------------------------------

def _build_route(g: RoutingGraph, pair: dict, path: list[int], weights: dict) -> RouteResult:
    """Assemble a RouteResult from a node-index path (see router._build_route)."""
    edges: list[tuple] = []
    edge_indices: list[int] = []
    cat_by_edge: list[tuple[dict, float]] = []
    conf_lengths: list[tuple[float, float]] = []
    haz_lengths: list[tuple[float, float]] = []
    total_length = 0.0
    total_cost = 0.0

    n_edges = len(path) - 1
    for i, (u, v) in enumerate(zip(path, path[1:])):
        base_cost, eidx = pair[(u, v)]
        data = g.edge_view(eidx)
        length = data.get("length") or 0.0

        is_terminal = i == 0 or i == n_edges - 1
        if is_terminal and _as_str(data.get("foot_access")) in RESTRICTED_FOOT_ACCESS:
            data = {
                k: val for k, val in data.items()
                if k not in ("foot_access", "walk_score", "walk_confidence")
            }
        cats = edge_category_scores(data, weights)
        _, conf = edge_walkability(data, weights)
        haz = data.get("freeway_hazard")

        edges.append(g.edge_endpoints(eidx))
        edge_indices.append(eidx)
        cat_by_edge.append((cats, length))
        conf_lengths.append((conf, length))
        haz_lengths.append((float(haz) if haz is not None else 0.0, length))
        total_length += length
        total_cost += base_cost

    dimension_scores = _aggregate_route_dimensions(cat_by_edge)
    walk_score = combine_categories(dimension_scores) if dimension_scores else _EMPTY_WALK
    walk_score = apply_freeway_veto(walk_score, haz_lengths)
    if total_length > 0.0:
        confidence = sum(c * L for c, L in conf_lengths) / total_length
    else:
        confidence = sum(c for c, _ in conf_lengths) / max(len(conf_lengths), 1)

    crossing_count = sum(1 for idx in path[1:] if g.is_crossing[idx])

    return RouteResult(
        nodes=[int(g.node_ids[i]) for i in path],
        edges=edges,
        total_length=total_length,
        total_cost=total_cost,
        walk_score=walk_score,
        confidence=confidence,
        crossing_count=crossing_count,
        dimension_scores=dimension_scores,
        edge_indices=edge_indices,
    )


# ---------------------------------------------------------------------------
# Candidate collection (A* + penalty method) — port of _collect_candidates
# ---------------------------------------------------------------------------

def _collect_candidates(
    g: RoutingGraph,
    source: int,
    target: int,
    alpha: float,
    k: int,
    max_candidates: int,
    min_confidence: float,
    weights: dict,
    allowed: np.ndarray | None = None,
    edge_ok=None,
) -> list[RouteResult]:
    if allowed is not None and (not allowed[source] or not allowed[target]):
        return []
    succ, pair = _project(g, alpha, source, target, weights, allowed, edge_ok)
    if source not in succ and source != target:
        return []

    heuristic = _heuristic(g, target)
    penalty: dict = defaultdict(lambda: 1.0)

    def inflate(path):
        for a, b in zip(path, path[1:]):
            penalty[(a, b)] *= ALT_PENALTY

    candidates: list[RouteResult] = []
    seen: set = set()
    best_cost: float | None = None

    def confident() -> bool:
        return bool(candidates) and max(c.confidence for c in candidates) >= min_confidence

    for _ in range(max(max_candidates, k)):
        if len(candidates) >= k and confident():
            break
        path = _astar_csr(succ, source, target, heuristic, penalty)
        if path is None:
            break
        sig = tuple(path)
        if sig not in seen:
            seen.add(sig)
            route = _build_route(g, pair, path, weights)
            if best_cost is None:
                best_cost = route.total_cost
                candidates.append(route)
            elif route.total_cost <= best_cost * (1.0 + ALT_MAX_STRETCH):
                candidates.append(route)
        inflate(path)

    return candidates


def _hugs_boundary(g, route, o_idx, d_idx, budget, eps) -> bool:
    if math.isinf(budget):
        return False
    threshold = budget * (1.0 - eps)
    return any(_foci_sum(g, o_idx, d_idx, g.id_to_idx[n]) > threshold for n in route.nodes)


# ---------------------------------------------------------------------------
# Phase-2 side refinement (port of _refine_route)
# ---------------------------------------------------------------------------

def _refine_route(g: RoutingGraph, r1: RouteResult, o_idx: int, d_idx: int, weights: dict) -> RouteResult:
    route_idx = [g.id_to_idx[n] for n in r1.nodes]
    mask = _tube_mask(g, route_idx, clip.TUBE_WIDTH_M)
    if not mask[o_idx] or not mask[d_idx]:
        return r1
    # Exclude `service` shortcuts not already on r1 (parking-lot / back-alley cuts),
    # matching the Nx refiner's edge_subgraph filter.
    r1_edge_set = set(r1.edge_indices)
    is_service = g.is_service

    def edge_ok(e: int) -> bool:
        return (not is_service[e]) or (e in r1_edge_set)

    cands = _collect_candidates(
        g, o_idx, d_idx, REFINE_ALPHA, k=1, max_candidates=1,
        min_confidence=0.0, weights=weights, allowed=mask, edge_ok=edge_ok,
    )
    if not cands:
        return r1
    r2 = cands[0]
    crossings_saved = max(0, r1.crossing_count - r2.crossing_count)
    allowance = REFINE_SCORE_TOL + REFINE_CROSSING_CREDIT * crossings_saved
    return r2 if r2.walk_score >= r1.walk_score - allowance else r1


# ---------------------------------------------------------------------------
# Public entry point (port of router.find_routes)
# ---------------------------------------------------------------------------

def find_routes(
    g: RoutingGraph,
    orig: tuple[float, float],
    dest: tuple[float, float],
    *,
    alpha: float = ALPHA_DEFAULT,
    weights: dict = FACTOR_WEIGHTS,
    refine_sides: bool = True,
    k: int = K_DEFAULT,
    max_candidates: int = MAX_CANDIDATES,
    min_confidence: float = MIN_CONFIDENCE,
    tie_epsilon: float = TIE_EPSILON,
    conf_beta: float = CONF_TIEBREAK_BETA,
    detour_factor: float = clip.DETOUR_FACTOR_DEFAULT,
    min_buffer_m: float = clip.MIN_BUFFER_M,
) -> list[RouteResult]:
    """CSR twin of ``router.find_routes`` — see that docstring for semantics."""
    o_idx = _snap(g, *orig, routable_only=True, walk_bias=clip.SNAP_WALK_BIAS_M)
    d_idx = _snap(g, *dest, routable_only=True, walk_bias=clip.SNAP_WALK_BIAS_M)
    if o_idx == d_idx:
        return []

    factor: float | None = detour_factor
    widens = 0
    candidates: list[RouteResult] = []
    while True:
        if factor is None:
            allowed = None
            budget = math.inf
        else:
            allowed, budget = _ellipse_mask(g, o_idx, d_idx, factor, min_buffer_m)

        candidates = _collect_candidates(
            g, o_idx, d_idx, alpha, k, max_candidates, min_confidence, weights,
            allowed=allowed,
        )

        if candidates and not _hugs_boundary(g, candidates[0], o_idx, d_idx, budget, BOUNDARY_EPS):
            break
        if factor is None:
            break
        if not candidates or widens >= MAX_WIDENS:
            factor = None
        else:
            factor *= WIDEN_FACTOR
            widens += 1

    if not candidates:
        return []

    if alpha > 0:
        best_walk = max(c.walk_score for c in candidates)
        candidates.sort(
            key=lambda r: _rank_score(r, best_walk, tie_epsilon, conf_beta),
            reverse=True,
        )

    if refine_sides and alpha > 0:
        refined: list[RouteResult] = []
        seen: set = set()
        for r1 in candidates:
            r2 = _refine_route(g, r1, o_idx, d_idx, weights)
            sig = tuple(r2.nodes)
            if sig not in seen:
                seen.add(sig)
                refined.append(r2)
        candidates = refined

    return candidates
