"""
Generate a walk_score heatmap for each dev region, so the same diagnostic
questions can be asked of areas with different walkability profiles.

    python notebooks/region_maps.py                 # all built regions
    python notebooks/region_maps.py --region nubian_roxbury
    python notebooks/region_maps.py --metric cost

Build a region first if it's missing:

    python -m walkability.graph.build --dev --region nubian_roxbury
    python -m walkability.graph.build --list-regions

Each map lands in notebooks/regions/<region>_<metric>.html (red = bad, green =
good). Beacon Hill is the walkable reference; the others were chosen for lower
walkability so the audit flags in diagnostics.py actually fire. Once you spot a
bad route on a map, log it the usual way: add it to PROBLEM_ROUTES, then use
diagnostics.audit_route / inspect_route_map / breakdown_route to diagnose it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from walkability.graph.build import DEV_REGIONS, dev_region_path, load_graph
from walkability.scoring.factors import (
    EXCLUDED_FOOT_ACCESS,
    RESTRICTED_FOOT_ACCESS,
    _as_float,
    _as_str,
    edge_walkability,
)

from diagnostics import score_heatmap   # sibling module in notebooks/

OUT_DIR = Path(__file__).with_name("regions")
_ARTERIAL_HWY_SCORE = 0.15


def summarize(G) -> str:
    """One-line walkability profile of a (sub)graph."""
    walks, art, restr, n = [], 0, 0, 0
    for *_, d in G.edges(keys=True, data=True):
        walks.append(edge_walkability(d)[0])
        if (_as_float(d.get("highway_score")) or 1.0) <= _ARTERIAL_HWY_SCORE:
            art += 1
        if _as_str(d.get("foot_access")) in (RESTRICTED_FOOT_ACCESS | EXCLUDED_FOOT_ACCESS):
            restr += 1
        n += 1
    if not n:
        return "no edges"
    return (f"mean_walk={sum(walks)/n:.3f}  min={min(walks):.2f}  "
            f"arterial={100*art/n:.1f}%  restricted/no={100*restr/n:.1f}%  edges={n}")


def make_region_map(region: str, metric: str = "walk_score") -> None:
    path = dev_region_path(region)
    if not path.exists():
        print(f"  [{region}] not built — run: "
              f"python -m walkability.graph.build --dev --region {region}")
        return
    cfg = DEV_REGIONS[region]
    G = load_graph(path)
    print(f"  [{region}] {summarize(G)}")
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{region}_{metric}.html"
    # Region subsets only contain nodes within the network radius, so a generous
    # geographic radius from the centre simply includes them all.
    score_heatmap(G, center=(cfg["lat"], cfg["lon"]),
                  dist_m=cfg["radius_m"] + 400.0, metric=metric, out=out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Heatmaps for dev regions.")
    ap.add_argument("--region", default=None,
                    help=f"One region (default: all). Choices: {sorted(DEV_REGIONS)}")
    ap.add_argument("--metric", default="walk_score", choices=["walk_score", "cost"],
                    help="Colour edges by walkability (default) or routing cost.")
    args = ap.parse_args()

    regions = [args.region] if args.region else list(DEV_REGIONS)
    print(f"=== Region maps ({args.metric}) ===")
    for r in regions:
        make_region_map(r, metric=args.metric)
