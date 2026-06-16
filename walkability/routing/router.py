"""
Walkability-aware routing (A* with penalty-method alternatives).

Pipeline for one query
-----------------------
  1. Snap origin/destination lat-lon to the nearest *routable* graph nodes
     (vectorised; restricted to the largest walkable component — see clip.py).
  2. Clip the graph to an ellipse around O–D so the search runs over a small
     local subgraph instead of the whole city (see routing/clip.py).
  3. Project the clipped MultiDiGraph to a simple DiGraph (cheapest parallel
     edge), excluding foot=no edges.
  4. A* for the single best route on the walkability cost from routing/cost.py,
     using straight-line (haversine) distance as an admissible, consistent
     heuristic — it's a valid lower bound because cost = length·(1+α·(1−walk))
     ≥ length ≥ straight-line for ANY alpha/weights, so A* stays optimal under
     the UI sliders while exploring far fewer nodes than Dijkstra/Yen's.
  5. Alternatives via the *penalty method*: inflate the weights of edges on the
     routes found so far (a per-edge multiplier passed to A*'s weight callback)
     and re-run A* to get a diverging route. Repeat until k distinct routes, or
     (confidence expansion) until at least one clears the confidence floor, up
     to max_candidates attempts. Far cheaper than Yen's many edge-removal
     Dijkstras, which dominated long-route latency.
  6. Widen-and-retry: if the best route hugs the clip boundary (the true
     optimum may lie outside the ellipse), widen the ellipse and re-route,
     finally falling back to the full graph so a route is never missed.
  7. Re-rank: confidence breaks ties only between routes whose walk_scores are
     close (within tie_epsilon). Outside that window the walkability ordering
     is preserved exactly.

Why two stages (cost then confidence)
-------------------------------------
A* optimises a single scalar edge cost, so anything folded into that cost
influences ranking *always*. To honour "confidence is a tiebreaker, not a
primary factor", confidence is kept out of the cost entirely and applied only
in the post-hoc re-rank over the small candidate set.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import networkx as nx

from walkability.routing import clip
from walkability.routing.cost import ALPHA_DEFAULT, edge_cost
from walkability.scoring.factors import (
    RESTRICTED_FOOT_ACCESS,
    _as_str,
    edge_walkability,
)
from walkability.scoring.weights import FACTOR_WEIGHTS, ROUTE_SCORE_EXPONENT

# Re-rank / expansion tuning ------------------------------------------------
K_DEFAULT:        int   = 5      # routes to surface by default
MAX_CANDIDATES:   int   = 25     # hard cap on A* runs (penalty-method attempts)
MIN_CONFIDENCE:   float = 0.40   # below this for ALL candidates → keep expanding
TIE_EPSILON:      float = 0.05   # walk_score window within which confidence breaks ties
CONF_TIEBREAK_BETA: float = 0.05 # max confidence contribution to the rank score
ALT_PENALTY:      float = 1.4    # weight multiplier applied to a found route's
                                 # edges to push the next A* run onto a different path
ALT_MAX_STRETCH:  float = 0.30   # an alternative is only kept if its true cost is
                                 # within (1+this)× the optimum — keeps the candidate
                                 # pool near-optimal (like Yen's k-shortest) so the
                                 # walk_score re-rank can't surface a wildly long route

# Clip widen-and-retry tuning -----------------------------------------------
WIDEN_FACTOR:  float = 1.7    # multiply detour_factor each time we widen
MAX_WIDENS:    int   = 2      # widen attempts before falling back to full graph
BOUNDARY_EPS:  float = 0.05   # route "hugs" the ellipse if a node's foci-sum
                              # exceeds (1 − this) × budget → widen and retry


@dataclass
class RouteResult:
    """One candidate route and its length-weighted aggregate scores.

    walk_score and confidence are length-weighted means over the route's
    edges (consistent with the length-based cost), both in [0, 1].
    """
    nodes:        list[int]
    edges:        list[tuple]      # (u, v, key)
    total_length: float            # metres
    total_cost:   float
    walk_score:   float
    confidence:   float


# ---------------------------------------------------------------------------
# Routable simple-graph projection
# ---------------------------------------------------------------------------
# nx.shortest_simple_paths (Yen's) is not implemented for multigraphs, so we
# project the MultiDiGraph down to a simple DiGraph: for each (u, v) keep only
# the cheapest traversable parallel edge, remembering its key so the original
# edge data can be recovered for scoring. foot=no edges are dropped here (their
# cost is None), guaranteeing they never appear in a route.

def _routable_digraph(
    G: nx.MultiDiGraph,
    alpha: float,
    o_node,
    d_node,
    weights: dict[str, float] = FACTOR_WEIGHTS,
) -> nx.DiGraph:
    """Cheapest-parallel-edge DiGraph projection of G for the given alpha.

    Each edge carries ``weight`` (the cost Yen's minimises) and ``key`` (the
    winning parallel-edge key in G, for reconstruction).

    Terminal edges — those leaving the origin (``u == o_node``) or entering the
    destination (``v == d_node``) — are costed without the restricted-access
    penalty: any simple path from O to D uses an out-edge of O only as its first
    hop and an in-edge of D only as its last, so this is exactly the "customer at
    your own destination" exemption (see ``edge_cost(is_terminal=...)``).
    """
    DG = nx.DiGraph()
    for u, v, key, data in G.edges(keys=True, data=True):
        is_terminal = (u == o_node) or (v == d_node)
        c = edge_cost(data, alpha, is_terminal=is_terminal, weights=weights)
        if c is None:
            continue  # foot=no — not routable
        existing = DG.get_edge_data(u, v)
        if existing is None or c < existing["weight"]:
            DG.add_edge(u, v, weight=c, key=key)
    return DG


# ---------------------------------------------------------------------------
# Route reconstruction
# ---------------------------------------------------------------------------

def _build_route(
    G: nx.MultiDiGraph,
    DG: nx.DiGraph,
    nodes: list[int],
    weights: dict[str, float] = FACTOR_WEIGHTS,
) -> RouteResult:
    """Assemble a RouteResult from a node path, scoring against original edges.

    The route's ``walk_score`` is a length-weighted **power mean** of its edge
    scores (exponent ``ROUTE_SCORE_EXPONENT`` < 1), so one genuinely bad block
    drags the whole route down rather than being averaged away — a route is only
    as pleasant as its worst stretch. ``confidence`` stays a plain length-weighted
    mean (it is only a tiebreaker).

    The first and last edges are terminal: if such an edge is restricted-access
    (a customers-only zoo entrance, a private drive at the destination) its
    ``foot_access`` penalty is dropped before aggregation, matching the routing
    exemption in ``_routable_digraph`` so a forced endpoint neither distorts the
    chosen route nor tanks its reported score.
    """
    edges: list[tuple] = []
    per_edge: list[tuple[float, float, float]] = []  # (walk, confidence, length)
    total_length = 0.0
    total_cost = 0.0

    n_edges = len(nodes) - 1
    for i, (u, v) in enumerate(zip(nodes, nodes[1:])):
        dg_edge = DG[u][v]
        key, cost = dg_edge["key"], dg_edge["weight"]
        data = G[u][v][key]
        length = data.get("length") or 0.0

        is_terminal = i == 0 or i == n_edges - 1
        if is_terminal and _as_str(data.get("foot_access")) in RESTRICTED_FOOT_ACCESS:
            # Recompute without foot_access. Dropping the baked walk_score/
            # walk_confidence keys forces a clean recompute over the remaining
            # factors (factors.py renormalises over whatever is present).
            data = {
                k: val for k, val in data.items()
                if k not in ("foot_access", "walk_score", "walk_confidence")
            }
        walk, conf = edge_walkability(data, weights)

        edges.append((u, v, key))
        per_edge.append((walk, conf, length))
        total_length += length
        total_cost += cost

    p = ROUTE_SCORE_EXPONENT
    if total_length > 0.0:
        walk_pow = sum((w ** p) * L for w, _, L in per_edge) / total_length
        confidence = sum(c * L for _, c, L in per_edge) / total_length
    else:
        # Degenerate zero-length path: fall back to unweighted means.
        n = max(len(per_edge), 1)
        walk_pow = sum(w ** p for w, _, _ in per_edge) / n
        confidence = sum(c for _, c, _ in per_edge) / n
    walk_score = walk_pow ** (1.0 / p)

    return RouteResult(
        nodes=nodes,
        edges=edges,
        total_length=total_length,
        total_cost=total_cost,
        walk_score=walk_score,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Re-ranking
# ---------------------------------------------------------------------------

def _rank_score(route: RouteResult, best_walk: float,
                tie_epsilon: float, conf_beta: float) -> float:
    """Walkability with a confidence bonus that decays to 0 outside the tie window.

    gap = best_walk − route.walk_score (≥ 0). Within `tie_epsilon` of the best
    walk_score the confidence term scales from full (gap=0) to zero (gap=ε);
    beyond ε it is exactly zero, so the pure walkability order is preserved for
    routes that are not genuinely close.
    """
    gap = best_walk - route.walk_score
    closeness = max(0.0, 1.0 - gap / tie_epsilon) if tie_epsilon > 0 else 0.0
    return route.walk_score + conf_beta * route.confidence * closeness


# ---------------------------------------------------------------------------
# Candidate collection (A* + penalty-method alternatives) over one (sub)graph
# ---------------------------------------------------------------------------

def _haversine_heuristic(graph: nx.MultiDiGraph, target):
    """A* heuristic: straight-line metres from a node to `target`.

    Admissible & consistent for our cost (cost ≥ length ≥ straight-line), so A*
    returns the true optimum regardless of alpha/weights. Target coords are
    captured once; the second arg (A* passes the target) is ignored.
    """
    ty, tx = graph.nodes[target]["y"], graph.nodes[target]["x"]

    def h(n, _t=None):
        nd = graph.nodes[n]
        return float(clip.haversine_m(nd["y"], nd["x"], ty, tx))

    return h


def _collect_candidates(
    graph: nx.MultiDiGraph,
    source,
    target,
    alpha: float,
    k: int,
    max_candidates: int,
    min_confidence: float,
    weights: dict[str, float] = FACTOR_WEIGHTS,
) -> list[RouteResult]:
    """Best route via A*, then diverging alternatives via the penalty method.

    Returns the best route first, then up to k−1 alternatives, with the
    confidence-expansion rule (keep generating until one route clears the
    confidence floor, capped at max_candidates A* runs). Empty if no path exists.
    """
    if source not in graph or target not in graph:
        return []

    DG = _routable_digraph(graph, alpha, source, target, weights)
    if source not in DG or target not in DG:
        return []

    heuristic = _haversine_heuristic(graph, target)

    # Per-edge penalty multiplier, applied via A*'s weight callback. The first
    # run (all multipliers 1.0) yields the true optimum; inflating a found
    # route's edges pushes later runs onto different paths. We never mutate DG.
    penalty: dict[tuple, float] = defaultdict(lambda: 1.0)

    def weight_fn(u, v, data):
        return data["weight"] * penalty[(u, v)]

    def inflate(nodes):
        for a, b in zip(nodes, nodes[1:]):
            penalty[(a, b)] *= ALT_PENALTY

    candidates: list[RouteResult] = []
    seen: set[tuple] = set()
    best_cost: float | None = None  # true (un-penalised) cost of the optimum

    def confident() -> bool:
        return bool(candidates) and max(c.confidence for c in candidates) >= min_confidence

    for _ in range(max(max_candidates, k)):
        if len(candidates) >= k and confident():
            break
        try:
            nodes = nx.astar_path(DG, source, target, heuristic=heuristic, weight=weight_fn)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            break
        sig = tuple(nodes)
        if sig not in seen:
            seen.add(sig)
            # Score against the un-penalised DG so total_cost/walk_score are real.
            route = _build_route(graph, DG, nodes, weights)
            if best_cost is None:
                best_cost = route.total_cost            # first A* run = the optimum
                candidates.append(route)
            elif route.total_cost <= best_cost * (1.0 + ALT_MAX_STRETCH):
                candidates.append(route)                # a reasonable alternative
            # else: too long a detour to be a sensible alternative — drop it
        inflate(nodes)  # diverge next run (even if this path was dropped/duplicate)

    return candidates


def _hugs_boundary(G, route: RouteResult, o_node, d_node, budget: float, eps: float) -> bool:
    """True if any route node sits within `eps` of the clip ellipse boundary.

    Such a route may have been cut short by the clip, so the true optimum could
    lie outside — the caller should widen the ellipse and re-route.
    """
    if math.isinf(budget):
        return False
    threshold = budget * (1.0 - eps)
    return any(clip.foci_sum(G, o_node, d_node, n) > threshold for n in route.nodes)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_routes(
    G: nx.MultiDiGraph,
    orig: tuple[float, float],
    dest: tuple[float, float],
    *,
    alpha: float = ALPHA_DEFAULT,
    weights: dict[str, float] = FACTOR_WEIGHTS,
    k: int = K_DEFAULT,
    max_candidates: int = MAX_CANDIDATES,
    min_confidence: float = MIN_CONFIDENCE,
    tie_epsilon: float = TIE_EPSILON,
    conf_beta: float = CONF_TIEBREAK_BETA,
    detour_factor: float = clip.DETOUR_FACTOR_DEFAULT,
    min_buffer_m: float = clip.MIN_BUFFER_M,
) -> list[RouteResult]:
    """Find walkability-ranked routes from `orig` to `dest`.

    Parameters
    ----------
    orig, dest :
        (lat, lon) tuples in WGS84; snapped to the nearest graph nodes.
    alpha :
        Distance/walkability tradeoff (see routing/cost.py). 0 = shortest path.
    weights :
        Per-factor walkability weights (see scoring/weights.py:FACTOR_WEIGHTS).
        Defaults to the FACTOR_WEIGHTS object so the baked-score fast path is
        used; UI sliders pass a different dict and force a per-edge recompute.
    k :
        Number of candidate routes to evaluate (and, normally, return).
    max_candidates :
        Cap on A* runs (penalty-method attempts) when expanding past `k` to
        escape a low-confidence top-k.
    min_confidence :
        If every candidate's confidence is below this, keep pulling more paths
        (up to `max_candidates`) before ranking.
    tie_epsilon, conf_beta :
        Re-rank tuning; see `_rank_score`.
    detour_factor, min_buffer_m :
        Clip-ellipse sizing (see routing/clip.py). The ellipse auto-widens by
        WIDEN_FACTOR and finally falls back to the full graph if the best route
        hugs the boundary, so these only set the *starting* (fastest) clip.

    Returns
    -------
    list[RouteResult] :
        Best route first. Empty if origin and destination are disconnected.
    """
    o_node = clip.snap_to_node(G, *orig, routable_only=True)
    d_node = clip.snap_to_node(G, *dest, routable_only=True)
    if o_node == d_node:
        return []

    # Start with a tight clip for speed; widen if the route hugs the boundary,
    # finally (factor=None) route on the full graph so a path is never missed.
    factor: float | None = detour_factor
    widens = 0
    candidates: list[RouteResult] = []
    while True:
        if factor is None:
            graph_for_routing: nx.MultiDiGraph = G
            budget = math.inf
        else:
            graph_for_routing, budget = clip.clip_to_ellipse(
                G, o_node, d_node, factor, min_buffer_m
            )

        candidates = _collect_candidates(
            graph_for_routing, o_node, d_node, alpha, k, max_candidates,
            min_confidence, weights,
        )

        if candidates and not _hugs_boundary(
            G, candidates[0], o_node, d_node, budget, BOUNDARY_EPS
        ):
            break  # confident the clip didn't cut off the optimum
        if factor is None:
            break  # already routed on the full graph — nothing more to widen to

        # No route in this clip → O and D are disconnected within it, so jump
        # straight to the full graph. Otherwise widen, up to MAX_WIDENS times.
        if not candidates or widens >= MAX_WIDENS:
            factor = None
        else:
            factor *= WIDEN_FACTOR
            widens += 1

    if not candidates:
        return []

    # alpha=0 means "ignore walkability" → keep pure cost (length) order, shortest
    # first, so it's a true shortest-path floor. For alpha>0 the user is weighting
    # walkability, so surface the most walkable candidate (confidence breaks ties).
    if alpha > 0:
        best_walk = max(c.walk_score for c in candidates)
        candidates.sort(
            key=lambda r: _rank_score(r, best_walk, tie_epsilon, conf_beta),
            reverse=True,
        )
    return candidates


# ---------------------------------------------------------------------------
# Manual inspection (no automated test suite yet — mirrors build.inspect_edges)
# ---------------------------------------------------------------------------

def inspect_route(
    G: nx.MultiDiGraph,
    orig: tuple[float, float],
    dest: tuple[float, float],
    *,
    alpha: float = ALPHA_DEFAULT,
    n: int = 3,
    **kwargs,
) -> list[RouteResult]:
    """Run find_routes and print a human-readable summary of the top `n` routes."""
    routes = find_routes(G, orig, dest, alpha=alpha, **kwargs)
    print(f"=== Routes (alpha={alpha}, {len(routes)} candidates) ===")
    if not routes:
        print("  No route found.")
        return routes
    for i, r in enumerate(routes[:n]):
        print(
            f"  [{i}] length={r.total_length:7.1f}m  cost={r.total_cost:8.1f}  "
            f"walk={r.walk_score:.3f}  conf={r.confidence:.3f}  "
            f"hops={len(r.edges)}"
        )
    return routes


def _corner_nodes(G: nx.MultiDiGraph) -> tuple[tuple[float, float], tuple[float, float]]:
    """(SW corner, NE corner) lat-lon of the node bounding box — far-apart endpoints."""
    ys = [d["y"] for _, d in G.nodes(data=True)]
    xs = [d["x"] for _, d in G.nodes(data=True)]
    return (min(ys), min(xs)), (max(ys), max(xs))


if __name__ == "__main__":
    from walkability.graph.build import DEV_ENRICHED_PATH, load_graph

    G = load_graph(DEV_ENRICHED_PATH)
    sw, ne = _corner_nodes(G)
    print(f"Origin (SW corner): {sw}\nDest   (NE corner): {ne}\n")

    # alpha=0 ≈ shortest path; raising alpha should trade length for walkability.
    for alpha in (0.0, 2.0, 5.0):
        inspect_route(G, sw, ne, alpha=alpha)
        print()
