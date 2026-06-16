"""
Problem-route regression harness.

Log routes that behave badly, then re-run them after any scoring/data/routing
change to see whether each one improved, regressed, or stayed the same. This
turns anecdotal "this route looks wrong" observations into a repeatable suite.

Usage
-----
    python notebooks/problem_routes.py              # run + compare to baseline
    python notebooks/problem_routes.py --update      # save current results as the new baseline
    python notebooks/problem_routes.py --dev         # use the fast Beacon Hill subset

Workflow
--------
1. Notice a bad route → add an entry to PROBLEM_ROUTES with what you observed and
   your hypothesis (data gap? weight problem? — see diagnostics.py to investigate).
2. Run with --update once to record the current (bad) behaviour as the baseline.
3. Make a scoring/weights/data change and rebuild if needed.
4. Run without --update: the harness reports walk-score / length deltas and
   whether the actual path changed, so you can confirm a fix and catch regressions.

The baseline is stored as JSON next to this file so it can be reviewed in diffs.
"""

from __future__ import annotations

import argparse
import json
import zlib
from pathlib import Path

from walkability.graph.build import (
    DEV_REGIONS,
    ENRICHED_PATH,
    dev_region_path,
    load_graph,
)
from walkability.routing.cost import ALPHA_DEFAULT
from walkability.routing.router import find_routes

BASELINE_PATH = Path(__file__).with_name("problem_routes_baseline.json")

# Tolerances for classifying a change as meaningful rather than numerical noise.
WALK_EPS = 0.005      # walk_score change below this = "same"
LEN_EPS_M = 5.0       # length change below this = "same"


# ---------------------------------------------------------------------------
# Registry — add a dict here whenever you spot a route worth tracking.
# Fields: name, region (a DEV_REGIONS key — groups routes by area), origin/dest
# (lat, lon), alpha (defaults to ALPHA_DEFAULT), and free-text observed_problem /
# hypothesis notes. Routes are evaluated on the FULL graph (it covers every
# region), so any region's routes resolve; `region` is a label for grouping and
# for linking ground-truth observations (see ground_truth.csv).
# ---------------------------------------------------------------------------
PROBLEM_ROUTES: list[dict] = [
    # --- beacon_hill (walkable reference) ---------------------------------
    {
        "name": "bh_charles_to_state_house",
        "region": "beacon_hill",
        "origin": (42.3601, -71.0709),   # Charles St (west base)
        "dest": (42.3589, -71.0640),     # near the State House (east top)
        "alpha": 2.0,
        "observed_problem": "TODO: walk it — does it prefer brick lanes or main streets?",
        "hypothesis": "TODO",
    },
    {
        "name": "bh_acorn_chestnut_brick",
        "region": "beacon_hill",
        "origin": (42.3585, -71.0698),   # Chestnut/Willow area
        "dest": (42.3576, -71.0681),     # toward Charles/Beacon
        "alpha": 2.0,
        "observed_problem": "TODO: brick is scored 0.70 flat regardless of how uneven it is",
        "hypothesis": "TODO: weight problem (brick comfort) vs data (SCI condition)?",
    },
    {
        "name": "bh_louisburg_to_charles",
        "region": "beacon_hill",
        "origin": (42.3582, -71.0696),   # Louisburg Square
        "dest": (42.3590, -71.0710),     # Charles St
        "alpha": 2.0,
        "observed_problem": "TODO",
        "hypothesis": "TODO",
    },
    # --- less-walkable regions (seed routes; verified to resolve) ----------
    {
        "name": "charlestown_sullivan_cross",
        "region": "charlestown_sullivan",
        "origin": (42.3820, -71.0710),
        "dest": (42.3858, -71.0688),
        "alpha": 2.0,
        "observed_problem": "TODO: car-dominated; 6 crossings flagged",
        "hypothesis": "TODO",
    },
    {
        "name": "newmarket_massave_arterial",
        "region": "newmarket_massave",
        "origin": (42.3345, -71.0685),
        "dest": (42.3318, -71.0640),
        "alpha": 2.0,
        "observed_problem": "TODO: ARTERIAL_EXPOSURE + LOW_SCORE_HEAVY flagged — industrial arterials",
        "hypothesis": "TODO: data gap (missing sidewalks) vs genuinely low walkability?",
    },
    {
        "name": "nubian_roxbury_washington",
        "region": "nubian_roxbury",
        "origin": (42.3270, -71.0855),
        "dest": (42.3312, -71.0808),
        "alpha": 2.0,
        "observed_problem": "TODO: high arterial exposure around Washington St / Nubian Sq",
        "hypothesis": "TODO",
    },
]


# ---------------------------------------------------------------------------
# Metrics + comparison
# ---------------------------------------------------------------------------

def measure(G, case: dict) -> dict:
    """Compute the metrics we track for one problem route."""
    alpha = case.get("alpha", ALPHA_DEFAULT)
    routes = find_routes(G, tuple(case["origin"]), tuple(case["dest"]), alpha=alpha)
    if not routes:
        return {"found": False}
    best = routes[0]
    return {
        "found": True,
        "length": round(best.total_length, 1),
        "walk_score": round(best.walk_score, 4),
        "confidence": round(best.confidence, 4),
        "hops": len(best.edges),
        # Path fingerprint: detects a changed route even when length is similar.
        # crc32 (not hash()) so it's stable across processes regardless of node id type.
        "path_fp": zlib.crc32(",".join(map(str, best.nodes)).encode()),
    }


def classify(name: str, base: dict | None, cur: dict) -> str:
    """One-line verdict comparing current metrics to the baseline."""
    if base is None:
        return "NEW      (no baseline yet)"
    if base.get("found") and not cur.get("found"):
        return "REGRESSED  route lost (was reachable, now no route)"
    if not base.get("found") and cur.get("found"):
        return "IMPROVED   route found (was unreachable)"
    if not base.get("found") and not cur.get("found"):
        return "same       (still no route)"

    dw = cur["walk_score"] - base["walk_score"]
    dl = cur["length"] - base["length"]
    path_changed = cur["path_fp"] != base["path_fp"]

    if dw > WALK_EPS:
        verdict = f"IMPROVED   walk {base['walk_score']:.3f}→{cur['walk_score']:.3f} (+{dw:.3f})"
    elif dw < -WALK_EPS:
        verdict = f"REGRESSED  walk {base['walk_score']:.3f}→{cur['walk_score']:.3f} ({dw:.3f})"
    elif abs(dl) > LEN_EPS_M:
        arrow = "shorter" if dl < 0 else "longer"
        verdict = f"changed    same walk, {abs(dl):.0f} m {arrow}"
    elif path_changed:
        verdict = "changed    different path, same length/walk"
    else:
        verdict = "same"
    return verdict


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _cases(region: str | None = None) -> list[dict]:
    """PROBLEM_ROUTES, optionally filtered to one region."""
    if region is None:
        return PROBLEM_ROUTES
    return [c for c in PROBLEM_ROUTES if c.get("region") == region]


def run_regression(G, update: bool = False, region: str | None = None) -> None:
    baseline = {}
    if BASELINE_PATH.exists():
        baseline = json.loads(BASELINE_PATH.read_text())

    cases = _cases(region)
    scope = f"region={region}" if region else "all regions"
    print(f"=== Problem-route regression ({len(cases)} cases, {scope}) ===\n")

    # Only measure the in-scope cases (others may be unreachable on a region's
    # dev subset). Start from the existing baseline so --update preserves the
    # rows for regions not in scope rather than wiping them.
    results = dict(baseline)
    last_region = None
    for case in cases:
        name = case["name"]
        cur = measure(G, case)
        results[name] = cur
        if case.get("region") != last_region:
            last_region = case.get("region")
            print(f"  [{last_region}]")
        verdict = classify(name, baseline.get(name), cur)
        detail = (f"len={cur['length']:.0f}m walk={cur['walk_score']:.3f} "
                  f"conf={cur['confidence']:.3f} hops={cur['hops']}"
                  if cur["found"] else "no route")
        print(f"    {name}")
        print(f"      {detail}")
        print(f"      {verdict}")
        if case.get("observed_problem"):
            print(f"      note: {case['observed_problem']}")
        print()

    if update:
        BASELINE_PATH.write_text(json.dumps(results, indent=2))
        print(f"Baseline updated → {BASELINE_PATH}")
    elif not baseline:
        print("No baseline on disk. Re-run with --update to record the current results.")


def _best_route(G, case):
    routes = find_routes(G, tuple(case["origin"]), tuple(case["dest"]),
                         alpha=case.get("alpha", ALPHA_DEFAULT))
    return routes[0] if routes else None


def build_map(G, region: str | None = None):
    """Overlay the (optionally region-filtered) problem routes on the heatmap."""
    from diagnostics import routes_over_heatmap   # local import: only needs folium here

    labeled = [(c["name"], _best_route(G, c)) for c in _cases(region)]
    suffix = f"_{region}" if region else ""
    out = Path(__file__).with_name(f"problem_routes_map{suffix}.html")
    routes_over_heatmap(G, labeled, metric="walk_score", out=out)


def run_audit(G, region: str | None = None):
    """Tier 1: rank problem routes by number of audit flags (most suspicious first)."""
    from diagnostics import audit_route

    rows = []
    for case in _cases(region):
        r = _best_route(G, case)
        rows.append((case["name"], case.get("region"),
                     audit_route(G, r) if r else {"flags": ["NO_ROUTE"]}))
    rows.sort(key=lambda t: len(t[2]["flags"]), reverse=True)

    print("=== Tier 1 audit (most-flagged first) ===\n")
    for name, reg, a in rows:
        flags = ", ".join(a["flags"]) if a["flags"] else "(clean)"
        print(f"  {name}  [{reg}]")
        print(f"    flags: {flags}")
        if "mean_walk" in a:
            print(f"    mean_walk={a['mean_walk']} min_walk={a['min_walk']} "
                  f"crossings={a['n_crossings']} arterials={a['n_arterials']} "
                  f"len={a['total_length_m']:.0f}m")
        print()
    return rows


def inspect_one(G, name=None, region: str | None = None):
    """Tier 2: write an inspector HTML for `name` (default: the most-flagged route)."""
    from diagnostics import audit_route, inspect_route_map

    if name is None:
        rows = [(c["name"], _best_route(G, c)) for c in _cases(region)]
        rows = [(n, r) for n, r in rows if r is not None]
        if not rows:
            print("inspect_one: no resolvable routes.")
            return
        name, route = max(rows, key=lambda t: len(audit_route(G, t[1])["flags"]))
    else:
        case = next((c for c in PROBLEM_ROUTES if c["name"] == name), None)
        if case is None:
            print(f"inspect_one: no problem route named {name!r}.")
            return
        route = _best_route(G, case)
    out = Path(__file__).with_name(f"inspect_{name}.html")
    inspect_route_map(G, route, out=out, label=name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Re-run tracked problem routes.")
    ap.add_argument("--update", action="store_true",
                    help="Save current results as the new baseline.")
    ap.add_argument("--region", default=None,
                    help=f"Only routes in this region. Choices: {sorted(DEV_REGIONS)}")
    ap.add_argument("--dev", action="store_true",
                    help="Route on a region's dev subset (fast). Implies --region "
                         "(defaults to beacon_hill) and filters to that region.")
    ap.add_argument("--map", action="store_true",
                    help="Write an HTML overlaying the problem routes on the score heatmap.")
    ap.add_argument("--audit", action="store_true",
                    help="Tier 1: rank problem routes by audit flags (no visuals).")
    ap.add_argument("--inspect", nargs="?", const="", metavar="NAME",
                    help="Tier 2: write an inspector HTML for NAME "
                         "(omit NAME for the most-flagged route).")
    args = ap.parse_args()

    # --dev routes on one region's subset (fast) and only that region's routes
    # are meaningful there, so it forces the region filter. Otherwise the full
    # graph covers everything and --region is just an optional filter.
    region = args.region
    if args.dev:
        region = region or "beacon_hill"
        G = load_graph(dev_region_path(region))
    else:
        G = load_graph(ENRICHED_PATH)

    print()
    run_regression(G, update=args.update, region=region)
    if args.audit:
        print()
        run_audit(G, region=region)
    if args.map:
        print()
        build_map(G, region=region)
    if args.inspect is not None:
        print()
        inspect_one(G, name=args.inspect or None, region=region)
