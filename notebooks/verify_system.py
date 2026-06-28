"""
Automated verification harness — the machine-checkable invariants.

    python notebooks/verify_system.py            # invariants on dev + clip check on full
    python notebooks/verify_system.py --quick     # skip the full-graph clip check

These checks confirm the system *does what the code claims* (invariants and
internal consistency). They do NOT — and cannot — confirm that a walk_score is
"correct" in the real world or that a route is subjectively good; that requires
human ground truth (see Research/work_and_verification_outline.md and the
ground_truth.csv workflow). Exit code is non-zero if any invariant fails.
"""

from __future__ import annotations

import argparse
import math
import sys

import numpy as np

from walkability.graph.build import DEV_ENRICHED_PATH, ENRICHED_PATH, load_graph
from walkability.routing import clip
from walkability.routing.cost import RESTRICTED_ACCESS_PENALTY, edge_cost
from walkability.routing.router import _collect_candidates, find_routes
from walkability.scoring.factors import _as_float, _as_str, edge_walkability

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


# --- (1) schema + scoring bounds -------------------------------------------

def check_schema_and_bounds(G) -> None:
    bad_field = bad_walk = bad_conf = bad_env = 0
    for *_, d in G.edges(keys=True, data=True):
        if _as_float(d.get("highway_score")) is None or d.get("length") is None:
            bad_field += 1
        w, c = edge_walkability(d)
        if not (0.0 <= w <= 1.0):
            bad_walk += 1
        if not (0.0 <= c <= 1.0):
            bad_conf += 1
        # env = sqrt(car·eyes); the graded car ceiling (env-rework B) must keep it
        # in [0,1] (car can now exceed 0.85 on separated paths, but never 1.0).
        env = _as_float(d.get("environment_score"))
        if env is not None and not (0.0 <= env <= 1.0):
            bad_env += 1
    check("every edge has highway_score + length", bad_field == 0, f"{bad_field} missing")
    check("walk_score in [0,1] for all edges", bad_walk == 0, f"{bad_walk} out of range")
    check("confidence in [0,1] for all edges", bad_conf == 0, f"{bad_conf} out of range")
    check("environment_score in [0,1] for all edges", bad_env == 0, f"{bad_env} out of range")


# --- (2) missing-factor renormalisation ------------------------------------

def check_renormalization() -> None:
    only_hwy = edge_walkability({"highway_score": 0.5, "highway_confidence": 1.0})[0]
    check("missing surface does not drag score down (renormalised)",
          abs(only_hwy - 0.5) < 1e-9, f"walk={only_hwy} (expected 0.5)")
    with_surf = edge_walkability({"highway_score": 0.5, "highway_confidence": 1.0,
                                  "surface_score": 1.0, "surface_confidence": 1.0})[0]
    check("adding a present factor changes the score", with_surf > only_hwy,
          f"{only_hwy:.3f} -> {with_surf:.3f}")


# --- (3) GraphML string coercion -------------------------------------------

def check_coercion() -> None:
    ok = (_as_float("0.55") == 0.55 and _as_float("None") is None
          and _as_float(None) is None and _as_float("") is None
          and _as_str("None") is None and _as_str(" footway ") == "footway")
    check("_as_float/_as_str coerce GraphML strings + 'None' sentinels", ok)


# --- (4) baked fast-path == full recompute ---------------------------------

def check_baked_consistency(G) -> None:
    worst = 0.0
    n_checked = 0
    for *_, d in G.edges(keys=True, data=True):
        baked = _as_float(d.get("walk_score"))
        if baked is None:
            continue
        fresh = edge_walkability({k: v for k, v in d.items()
                                  if k not in ("walk_score", "walk_confidence")})[0]
        worst = max(worst, abs(baked - fresh))
        n_checked += 1
    check("baked walk_score matches a full recompute", worst < 1e-3,
          f"max |delta|={worst:.5f} over {n_checked} edges")


# --- (5) cost model: monotonic + access rules ------------------------------

def check_cost_model() -> None:
    base = {"surface_score": "0.8", "length": 100.0}
    hi = edge_cost({**base, "highway_score": "0.9"}, alpha=2.0)
    lo = edge_cost({**base, "highway_score": "0.2"}, alpha=2.0)
    check("cost rises as walk_score falls", lo > hi, f"hi_walk={hi:.1f} lo_walk={lo:.1f}")
    excluded = edge_cost({**base, "highway_score": "0.8", "foot_access": "no"}, alpha=2.0)
    check("foot=no edge is impassable (cost None)", excluded is None)
    # A restricted edge's foot_access ALSO lowers its walk_score (soft foot
    # factor = 0.0), so isolate the penalty against this edge's OWN base cost
    # rather than a plain edge — comparing to 'plain' would wrongly conflate the
    # two effects (this is the intended double signal; see routing/cost.py).
    restr_edge = {**base, "highway_score": "0.8", "foot_access": "customers"}
    walk_r, _ = edge_walkability(restr_edge)
    base_cost = 100.0 * (1.0 + 2.0 * (1.0 - walk_r))
    restr = edge_cost(restr_edge, alpha=2.0)
    check("restricted access multiplies cost by penalty",
          restr is not None and abs(restr - base_cost * RESTRICTED_ACCESS_PENALTY) < 1e-6,
          f"base={base_cost:.1f} restricted={restr:.1f} (x{RESTRICTED_ACCESS_PENALTY})")


# --- (6) route structural integrity ----------------------------------------

def _sample_pairs(G):
    from walkability.routing.router import _corner_nodes
    sw, ne = _corner_nodes(G)
    mid = ((sw[0] + ne[0]) / 2, sw[1])
    return [(sw, ne), (sw, mid), (mid, ne)]


def check_route_integrity(G) -> None:
    bad_adj = bad_edge = bad_footno = 0
    len_err = 0.0
    n_routes = 0
    for orig, dest in _sample_pairs(G):
        routes = find_routes(G, orig, dest, alpha=2.0)
        if not routes:
            continue
        r = routes[0]
        n_routes += 1
        # consecutive edges chain head-to-tail
        for (u1, v1, _), (u2, v2, _) in zip(r.edges, r.edges[1:]):
            if v1 != u2:
                bad_adj += 1
        # each edge exists and is not foot=no
        summed = 0.0
        for u, v, k in r.edges:
            if not G.has_edge(u, v, k):
                bad_edge += 1
                continue
            d = G[u][v][k]
            if _as_str(d.get("foot_access")) == "no":
                bad_footno += 1
            summed += _as_float(d.get("length")) or 0.0
        len_err = max(len_err, abs(summed - r.total_length))
    check("sampled routes were found", n_routes > 0, f"{n_routes} routes")
    check("route edges chain head-to-tail", bad_adj == 0, f"{bad_adj} breaks")
    check("route edges all exist in graph", bad_edge == 0, f"{bad_edge} missing")
    check("no foot=no edge appears in any route", bad_footno == 0, f"{bad_footno} found")
    check("total_length equals sum of edge lengths", len_err < 1.0, f"max err {len_err:.3f} m")


# --- (7) alpha=0 is the length floor ---------------------------------------

def check_alpha_floor(G) -> None:
    violations = 0
    n = 0
    for orig, dest in _sample_pairs(G):
        r0 = find_routes(G, orig, dest, alpha=0.0)
        r3 = find_routes(G, orig, dest, alpha=3.0)
        if not r0 or not r3:
            continue
        n += 1
        # alpha=0 minimises pure length, so it can't be longer than any alpha>0 route
        if r0[0].total_length > r3[0].total_length + 1.0:
            violations += 1
    check("alpha=0 route is no longer than a higher-alpha route", violations == 0,
          f"{violations}/{n} violations")


# --- (8) determinism -------------------------------------------------------

def check_determinism(G) -> None:
    orig, dest = _sample_pairs(G)[0]
    a = find_routes(G, orig, dest, alpha=2.0)
    b = find_routes(G, orig, dest, alpha=2.0)
    same = bool(a) and bool(b) and a[0].nodes == b[0].nodes
    check("same query yields the same route", same)


# --- (9) vectorised snapping == brute force --------------------------------

def check_snapping(G) -> None:
    rng = np.random.default_rng(0)
    ids, lats, lons = clip._node_coords(G)
    mismatches = 0
    for _ in range(20):
        i = int(rng.integers(len(ids)))
        lat = lats[i] + rng.normal(0, 0.0005)
        lon = lons[i] + rng.normal(0, 0.0005)
        snapped = clip.snap_to_node(G, lat, lon)
        cos = math.cos(math.radians(lat))
        brute_i = int(np.argmin((lats - lat) ** 2 + ((lons - lon) * cos) ** 2))
        if snapped != ids[brute_i]:
            mismatches += 1
    check("vectorised snap_to_node matches brute-force nearest", mismatches == 0,
          f"{mismatches}/20 mismatched")


# --- (10) clip never drops the true optimum (full graph) -------------------

def check_clip_matches_full(Gfull) -> None:
    pairs = [
        ((42.3588, -71.0707), (42.3601, -71.0631)),   # Beacon Hill -> Downtown
        ((42.3588, -71.0707), (42.3550, -71.0830)),   # westward
    ]
    mism = 0
    n = 0
    for orig, dest in pairs:
        # Compare like-for-like: the cost-optimum found via find_routes (clip +
        # auto-widen) vs the cost-optimum on the full graph, using the SAME snap
        # nodes find_routes uses. We take min-by-total_cost on both sides rather
        # than candidate[0], because for alpha>0 find_routes re-ranks by
        # walk_score, so candidate[0] is the most walkable route, not the cheapest
        # — comparing it to the full graph's min-cost route would test the re-rank,
        # not the clip's optimum-preservation guarantee.
        o = clip.snap_to_node(Gfull, *orig, routable_only=True,
                              walk_bias=clip.SNAP_WALK_BIAS_M)
        d = clip.snap_to_node(Gfull, *dest, routable_only=True,
                              walk_bias=clip.SNAP_WALK_BIAS_M)
        # refine_sides=False: this tests the CLIP's optimum-preservation, not the
        # phase-2 length refinement (which intentionally changes the chosen path).
        clipped = find_routes(Gfull, orig, dest, alpha=2.0, refine_sides=False)
        full = _collect_candidates(Gfull, o, d, 2.0, 5, 25, 0.40)
        if not clipped and not full:
            continue
        n += 1
        if not clipped or not full:
            mism += 1
            continue
        best_full = min(full, key=lambda r: r.total_cost).total_length
        best_clip = min(clipped, key=lambda r: r.total_cost).total_length
        if abs(best_clip - best_full) > 1.0:
            mism += 1
    check("clipped route == unclipped optimum (auto-widen guarantee)", mism == 0,
          f"{mism}/{n} mismatched")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run automated invariant checks.")
    ap.add_argument("--quick", action="store_true",
                    help="Skip the full-graph clip-vs-unclipped check (saves the ~10s load).")
    args = ap.parse_args()

    print("Loading dev graph for invariant checks ...")
    G = load_graph(DEV_ENRICHED_PATH)
    print("\n=== Automated invariants (dev graph) ===")
    check_schema_and_bounds(G)
    check_renormalization()
    check_coercion()
    check_baked_consistency(G)
    check_cost_model()
    check_route_integrity(G)
    check_alpha_floor(G)
    check_determinism(G)
    check_snapping(G)

    if not args.quick:
        print("\n=== Clip correctness (full graph) ===")
        Gfull = load_graph(ENRICHED_PATH)
        check_clip_matches_full(Gfull)

    print(f"\n{_PASS} passed, {_FAIL} failed.")
    sys.exit(1 if _FAIL else 0)
