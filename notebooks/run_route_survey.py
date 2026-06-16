"""
Route-survey harness (one-shot, machine-readable output).

Runs ~8 routes per dev region: the existing PROBLEM_ROUTES seeds for that region
PLUS generated crossing routes (origin/dest sampled on opposite bearings around
the region centre, so each route traverses the subset). For every route it:

  * alpha-sweeps {0, 2, 5} to see whether higher alpha actually changes the path
    (a route that never diverges from the alpha=0 shortest path is a "weights
    can't move it" signal),
  * records the best route's audit flags (diagnostics.audit_route),
  * dumps a per-edge breakdown (highway/surface/material/walk/foot/cost/source)
    plus each edge's midpoint lat/lon, so flagged segments can be Street-Viewed,
  * tags the 3 worst-walk edges for ground-truthing.

Everything is written to notebooks/route_run_dump.json (no console paste needed).

    python notebooks/run_route_survey.py
    python notebooks/run_route_survey.py --routes-per-region 8 --dump out.json
"""

from __future__ import annotations

import argparse
import json
import math
import zlib
from datetime import datetime, timezone
from pathlib import Path

from walkability.graph.build import DEV_REGIONS, dev_region_path, load_graph
from walkability.routing.cost import ALPHA_DEFAULT, edge_cost
from walkability.routing.router import find_routes
from walkability.scoring.factors import edge_walkability, _as_float, _as_str

# These two live next to this script in notebooks/ (on sys.path[0] when run as
# `python notebooks/run_route_survey.py`).
from problem_routes import PROBLEM_ROUTES
from diagnostics import audit_route

ALPHAS = (0.0, 2.0, 5.0)

# Generated crossings: (origin_bearing_deg, dest_bearing_deg, fraction_of_radius).
# bearing 0=N (+lat), 90=E (+lon). Opposite bearings => the route spans the
# region through its centre. Fractions < 0.6 keep both endpoints comfortably
# inside the network-truncated subset so they always snap + resolve.
GEN_BEARINGS = [
    (0, 180, 0.55),    # N <-> S
    (90, 270, 0.55),   # E <-> W
    (45, 225, 0.55),   # NE <-> SW
    (135, 315, 0.55),  # NW <-> SE
    (20, 200, 0.50),
    (70, 250, 0.50),
    (110, 290, 0.45),
    (160, 340, 0.45),
]


def _offset(lat: float, lon: float, bearing_deg: float, dist_m: float):
    b = math.radians(bearing_deg)
    dlat = dist_m * math.cos(b) / 111111.0
    dlon = dist_m * math.sin(b) / (111111.0 * math.cos(math.radians(lat)))
    return (round(lat + dlat, 6), round(lon + dlon, 6))


def _gen_routes(region: str) -> list[dict]:
    cfg = DEV_REGIONS[region]
    lat, lon, r = cfg["lat"], cfg["lon"], cfg["radius_m"]
    out = []
    for i, (ob, db, frac) in enumerate(GEN_BEARINGS, start=1):
        out.append({
            "name": f"{region}_gen{i}",
            "region": region,
            "origin": _offset(lat, lon, ob, r * frac),
            "dest": _offset(lat, lon, db, r * frac),
            "alpha": ALPHA_DEFAULT,
            "generated": True,
        })
    return out


def _cases_for(region: str, n: int) -> list[dict]:
    seeds = [dict(c, generated=False) for c in PROBLEM_ROUTES if c.get("region") == region]
    gens = _gen_routes(region)
    need = max(0, n - len(seeds))
    return seeds + gens[:need]


def _path_fp(route) -> int:
    return zlib.crc32(",".join(map(str, route.nodes)).encode())


def _edge_mid(G, u, v, key):
    """(lat, lon) midpoint of an edge — geometry midpoint if present else node mean."""
    d = G[u][v][key]
    geom = d.get("geometry")
    if geom is not None:
        cs = list(geom.coords)              # shapely coords are (lon, lat)
        lon_m, lat_m = cs[len(cs) // 2]
        return round(lat_m, 6), round(lon_m, 6)
    return (round((G.nodes[u]["y"] + G.nodes[v]["y"]) / 2, 6),
            round((G.nodes[u]["x"] + G.nodes[v]["x"]) / 2, 6))


def _f(d, field):
    return _as_float(d.get(field))


def _edge_record(G, u, v, key, idx, alpha):
    d = G[u][v][key]
    walk, conf = edge_walkability(d)
    cost = edge_cost(d, alpha)
    lat_m, lon_m = _edge_mid(G, u, v, key)
    return {
        "i": idx,
        "highway": _as_str(d.get("highway")),
        "name": _as_str(d.get("name")),
        "highway_score": _f(d, "highway_score"),
        "surface_score": _f(d, "surface_score"),
        "surface_material_score": _f(d, "surface_material_score"),
        "walk": round(walk, 3),
        "conf": round(conf, 3),
        "foot_access": _as_str(d.get("foot_access")),
        "length": round(_f(d, "length") or 0.0, 1),
        "cost": None if cost is None else round(cost, 1),
        "data_source": d.get("data_source"),
        "sidewalk_condition": _f(d, "sidewalk_condition"),
        "lat": lat_m,
        "lon": lon_m,
    }


def _best(G, case, alpha):
    routes = find_routes(G, tuple(case["origin"]), tuple(case["dest"]), alpha=alpha)
    return routes[0] if routes else None


def _survey_route(G, case) -> dict:
    case_alpha = case.get("alpha", ALPHA_DEFAULT)

    sweep = {}
    for a in ALPHAS:
        r = _best(G, case, a)
        sweep[str(a)] = (
            {"found": False} if r is None else {
                "found": True,
                "length": round(r.total_length, 1),
                "walk": round(r.walk_score, 4),
                "conf": round(r.confidence, 4),
                "hops": len(r.edges),
                "path_fp": _path_fp(r),
            }
        )
    # Does the path ever change as alpha rises? (weights able to move it at all?)
    fps = {s["path_fp"] for s in sweep.values() if s.get("found")}
    path_moves_with_alpha = len(fps) > 1

    best = _best(G, case, case_alpha)
    rec = {
        "name": case["name"],
        "region": case["region"],
        "generated": case.get("generated", False),
        "alpha": case_alpha,
        "origin": list(case["origin"]),
        "dest": list(case["dest"]),
        "observed_problem": case.get("observed_problem"),
        "hypothesis": case.get("hypothesis"),
        "sweep": sweep,
        "path_moves_with_alpha": path_moves_with_alpha,
    }

    if best is None:
        rec["found"] = False
        return rec

    edges = [_edge_record(G, u, v, k, i, case_alpha)
             for i, (u, v, k) in enumerate(best.edges)]
    worst = sorted(edges, key=lambda e: e["walk"])[:3]

    rec.update({
        "found": True,
        "length": round(best.total_length, 1),
        "walk_score": round(best.walk_score, 4),
        "confidence": round(best.confidence, 4),
        "total_cost": round(best.total_cost, 1),
        "hops": len(best.edges),
        "audit": audit_route(G, best, alpha=case_alpha),
        "worst_edges": [{"i": e["i"], "walk": e["walk"], "lat": e["lat"],
                         "lon": e["lon"], "highway": e["highway"],
                         "name": e["name"], "data_source": e["data_source"]}
                        for e in worst],
        "edges": edges,
    })
    return rec


def main():
    ap = argparse.ArgumentParser(description="Survey routes across all dev regions.")
    ap.add_argument("--routes-per-region", type=int, default=8)
    ap.add_argument("--dump", default=str(Path(__file__).with_name("route_run_dump.json")))
    args = ap.parse_args()

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "alphas_swept": list(ALPHAS),
        "routes_per_region": args.routes_per_region,
        "regions": {},
    }

    for region, cfg in DEV_REGIONS.items():
        path = dev_region_path(region)
        print(f"\n=== {region} ===  ({path.name})")
        if not path.exists():
            print(f"  MISSING graph: {path} — skipping")
            out["regions"][region] = {"note": cfg.get("note"), "error": "graph missing",
                                      "routes": []}
            continue
        G = load_graph(path)
        cases = _cases_for(region, args.routes_per_region)
        routes = []
        for case in cases:
            try:
                rec = _survey_route(G, case)
            except Exception as exc:  # keep going; record the failure
                rec = {"name": case["name"], "region": region,
                       "generated": case.get("generated", False),
                       "error": f"{type(exc).__name__}: {exc}"}
            routes.append(rec)
            tag = "no route" if not rec.get("found") else (
                f"len={rec['length']:.0f}m walk={rec['walk_score']:.3f} "
                f"flags={len(rec['audit']['flags'])} "
                f"alpha_moves={rec['path_moves_with_alpha']}")
            print(f"  {case['name']:<32} {tag}")
        out["regions"][region] = {"note": cfg.get("note"),
                                  "center": [cfg["lat"], cfg["lon"]],
                                  "radius_m": cfg["radius_m"],
                                  "routes": routes}

    dump_path = Path(args.dump)
    dump_path.write_text(json.dumps(out, indent=2))
    n_routes = sum(len(r["routes"]) for r in out["regions"].values())
    print(f"\nWrote {n_routes} routes across {len(out['regions'])} regions → {dump_path}")


if __name__ == "__main__":
    main()
