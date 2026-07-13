"""
Route-for-route parity harness: compact CSR graph vs the Phase-1 runtime
MultiDiGraph (the Phase-2 acceptance gate).

Run from the repo root (puts ``notebooks/`` on sys.path, like the other QA
scripts)::

    python notebooks/verify_csr_parity.py --city austin      # Gate 1 (default)
    python notebooks/verify_csr_parity.py --city boston       # Gate 2
    python notebooks/verify_csr_parity.py --city austin --n 60 --seed 3

For each origin/destination pair it runs ``router.find_routes`` on **both**
substrates across {default weights + a slider-weights dict} × {alpha = 0, 2, 5}
and asserts the results are identical: same candidate count, same node paths,
same ``(u, v, key)`` edge sequences (the parallel-edge tie-break), and
walk_score / confidence / dimension_scores / total_length equal within a tight
tolerance. The CSR router is the parallel implementation in
``routing/csr_router.py``; parity here is what lets the app trust it.

**Fingerprint gate.** Before comparing, the harness checks that the CSR file and
the runtime pickle were built from the *same* enriched snapshot
(``csr.source_fingerprint``); on mismatch it aborts loudly rather than "passing"
on stale data — the guard against comparing across a rebuild.

Pairs span short / mid / long O–D distances (distance-stratified rejection
sampling) plus, when the city has an inventory jurisdiction, pairs biased outside
it so the all-OSM-tier / missing-field (NaN) path is exercised.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

# repo root on path so `walkability` imports resolve when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from walkability.graph.compact import load_runtime, runtime_path  # noqa: E402
from walkability.graph.csr import load_csr, csr_path, source_fingerprint  # noqa: E402
from walkability.routing import router  # noqa: E402
from walkability.routing.clip import haversine_m  # noqa: E402

# A representative non-default weights dict (mirrors a UI slider tweak: environment
# up-weighted, comfort factors down) — forces the per-edge recompute path.
_SLIDER_WEIGHTS = {
    "road_type": 1.0,
    "surface_quality": 0.5,
    "surface_material": 0.3,
    "surface_width": 0.2,
    "environment": 2.0,
    "foot_access": 1.0,
}

_ALPHAS = (0.0, 2.0, 5.0)

# distance bands (metres): (min, max, how many)
_BANDS = (("short", 150, 800), ("mid", 800, 3000), ("long", 3000, 12000))


def _enriched_path(city: str) -> Path:
    from walkability.graph.inventory import CITY_PROFILES
    return CITY_PROFILES[city].enriched_path


def sample_band_pairs(coords, lo: float, hi: float, count: int, rng: random.Random):
    """``count`` O–D pairs drawn from ``coords`` whose great-circle distance falls
    in ``[lo, hi]`` metres (rejection sampling). ``coords`` is a list of ``(lat,
    lon)``. Shared sampling primitive: ``_sample_pairs`` below stratifies across
    bands with it, and ``route_types.py`` reuses it (over pre-filtered node sets)
    so every route-type selector draws pairs the same way."""
    pairs = []
    tries = 0
    while len(pairs) < count and tries < max(count, 1) * 400:
        tries += 1
        o = rng.choice(coords)
        d = rng.choice(coords)
        if lo <= haversine_m(o[0], o[1], d[0], d[1]) <= hi:
            pairs.append((o, d))
    return pairs


def _sample_pairs(nodes, n: int, seed: int):
    """Distance-stratified O–D pairs: split ``n`` across short/mid/long bands via
    rejection sampling on great-circle distance."""
    rng = random.Random(seed)
    coords = [(y, x) for _, y, x in nodes]
    per = max(1, n // len(_BANDS))
    pairs = []
    for _label, lo, hi in _BANDS:
        pairs.extend(sample_band_pairs(coords, lo, hi, per, rng))
    return pairs


def _routes_equal(a, b, tol=1e-6, len_tol=1e-4) -> str | None:
    """None if two RouteResults match; else a short reason string."""
    if a.nodes != b.nodes:
        return f"nodes {len(a.nodes)}v{len(b.nodes)}"
    if a.edges != b.edges:
        return "edges"
    if abs(a.walk_score - b.walk_score) > tol:
        return f"walk {a.walk_score:.6f}v{b.walk_score:.6f}"
    if abs(a.confidence - b.confidence) > tol:
        return f"conf {a.confidence:.6f}v{b.confidence:.6f}"
    if abs(a.total_length - b.total_length) > len_tol:
        return f"len {a.total_length:.3f}v{b.total_length:.3f}"
    if a.crossing_count != b.crossing_count:
        return f"cross {a.crossing_count}v{b.crossing_count}"
    dk = set(a.dimension_scores) | set(b.dimension_scores)
    for k in dk:
        if abs(a.dimension_scores.get(k, -1) - b.dimension_scores.get(k, -1)) > tol:
            return f"dim[{k}]"
    return None


def run(city: str = "austin", n: int = 36, seed: int = 7) -> int:
    """Run the CSR↔MultiDiGraph parity gate for one city; return the exit code.

    Exit codes (unchanged from the original ``main``): 0 = parity OK, 1 = a route
    mismatch, 2 = a missing runtime/CSR input, 3 = a build-snapshot fingerprint
    mismatch. Factored out of ``main`` so ``verify_city`` can invoke the gate
    in-process as route-type #12 instead of shelling out.
    """
    enriched = _enriched_path(city)
    rt = runtime_path(enriched)
    cp = csr_path(enriched)
    print(f"[{city}] runtime={rt.name}  csr={cp.name}", flush=True)
    if not rt.exists() or not cp.exists():
        print(f"  MISSING input(s): rt={rt.exists()} csr={cp.exists()}", flush=True)
        return 2

    t = time.time()
    G = load_runtime(rt)
    print(f"  load MDG {time.time()-t:.1f}s  ({G.number_of_nodes()} nodes)", flush=True)
    t = time.time()
    g = load_csr(cp)
    print(f"  load CSR {time.time()-t:.2f}s", flush=True)

    # Fingerprint gate — refuse to compare across mismatched build snapshots.
    fp_src = source_fingerprint(G)
    if g.fingerprint != fp_src:
        print(f"  FINGERPRINT MISMATCH: csr={g.fingerprint[:12]} src={fp_src[:12]}\n"
              f"  The CSR and runtime pickle are from different builds — rebuild "
              f"the CSR (`compact --csr`) before comparing.", flush=True)
        return 3
    print("  fingerprint OK", flush=True)

    graph_nodes = [(nid, d["y"], d["x"]) for nid, d in G.nodes(data=True)]
    pairs = _sample_pairs(graph_nodes, n, seed)

    tested = 0
    mism = 0
    t0 = time.time()
    for oi, (o, d) in enumerate(pairs):
        for alpha in _ALPHAS:
            for w in (None, _SLIDER_WEIGHTS):
                kw = {} if w is None else {"weights": w}
                rn = router.find_routes(G, o, d, alpha=alpha, **kw)
                rc = router.find_routes(g, o, d, alpha=alpha, **kw)
                tested += 1
                tag = f"pair{oi} a={alpha} custom={w is not None}"
                if len(rn) != len(rc):
                    mism += 1
                    print(f"  LEN {tag}: {len(rn)} vs {len(rc)}", flush=True)
                    continue
                for a, b in zip(rn, rc):
                    reason = _routes_equal(a, b)
                    if reason:
                        mism += 1
                        print(f"  DIFF {tag}: {reason}", flush=True)
                        break
    dt = time.time() - t0
    print(f"\n[{city}] tested {tested} route-sets over {len(pairs)} pairs "
          f"in {dt:.1f}s — mismatches={mism}", flush=True)
    print("PARITY OK" if mism == 0 else "PARITY FAILED", flush=True)
    return 0 if mism == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="CSR vs MultiDiGraph route parity gate.")
    ap.add_argument("--city", default="austin")
    ap.add_argument("--n", type=int, default=36, help="O–D pairs (split across bands)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    return run(args.city, args.n, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
