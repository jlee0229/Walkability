"""
Per-city "loaded correctly" gate — is this city's enriched graph wired up right?

    python notebooks/verify_city.py --city boston --update    # seed the baseline
    python notebooks/verify_city.py --city boston             # run the gate
    python notebooks/verify_city.py --city austin

Adding a city is "adding a CityProfile", but a profile can be silently wrong in
ways that still *build* (a collapsed inventory join, an inverted condition scale,
an environment layer narrower than the graph, a download filter that severs
bridge decks, a constant width, an unmapped material vocabulary). This gate
catches those for ANY city by combining:

  1. Static per-city checks (failure modes A–K) over the enriched graph, reusing
     verify_system's G-parameterised invariants (schema/bounds, baked==recompute,
     cost model, snapping) plus city-driven coverage/scale/seam checks here.
  2. The auto-derived route-type battery (notebooks/route_types.py): the global
     taxonomy instantiated into concrete routes from the graph + profile + OSM
     layers, with zero manual coordinates. Each type self-skips when its
     precondition is absent, so the same gate runs everywhere.
  3. The CSR↔MultiDiGraph parity gate (route type #12) and a per-city drift
     baseline (problem_routes metrics + crc32 path fingerprints).

It emits ONE genuinely-manual artefact — an auto-picked calibration deck — and
prints that filling it (subjective ratings into ground_truth.<city>.csv) is the
only human step; whether scores match human perception can't be automated, but
everything structural above is. Exit code is non-zero on any gated failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# repo root + notebooks on path (run from repo root, like the other QA scripts)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from walkability.graph.build import load_graph  # noqa: E402
from walkability.graph.csr import source_fingerprint  # noqa: E402
from walkability.graph.inventory import CITY_PROFILES  # noqa: E402
from walkability.scoring.factors import _EMPTY_WALK, _as_float, _as_str  # noqa: E402

import route_types  # noqa: E402
import verify_system as vsys  # noqa: E402
import verify_csr_parity  # noqa: E402
from problem_routes import classify, route_metrics  # noqa: E402
import calibration_survey  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Static per-city checks (failure modes A–K)
# ---------------------------------------------------------------------------

def check_static(G, profile) -> None:
    total = n_city = env_none = empty = wscore_present = 0
    ind: list[float] = []
    sep: list[float] = []
    park: list[float] = []
    csurf: list[float] = []
    ccond: list[float] = []
    cmat_present = cmat_total = 0
    cwidth: list[float] = []
    walk_vals: list[float] = []

    for u, v, k, d in G.edges(keys=True, data=True):
        total += 1
        is_city = _as_str(d.get("data_source")) == "city_inventory"
        if is_city:
            n_city += 1
        if _as_float(d.get("environment_score")) is None:
            env_none += 1
        ws = _as_float(d.get("walk_score"))
        if ws is not None:
            walk_vals.append(ws)
            if abs(ws - _EMPTY_WALK) < 1e-6:
                empty += 1
        if _as_float(d.get("width_score")) is not None:
            wscore_present += 1
        for arr, key in ((ind, "industrial_exposure"), (sep, "road_separation"),
                         (park, "parking_exposure")):
            val = _as_float(d.get(key))
            if val is not None:
                arr.append(val)
        if is_city:
            ss = _as_float(d.get("surface_score"))
            cd = _as_float(d.get("sidewalk_condition"))
            if ss is not None and cd is not None:
                csurf.append(ss)
                ccond.append(cd)
            cmat_total += 1
            if _as_float(d.get("surface_material_score")) is not None:
                cmat_present += 1
            wf = _as_float(d.get("sidewalk_width_ft"))
            if wf is not None:
                cwidth.append(wf)

    # A — inventory join neither collapsed (0) nor degenerate (~all)
    frac_city = n_city / total if total else 0.0
    okA = 0.02 < frac_city < 0.98
    check("A inventory-join share in (2%, 98%)", okA,
          f"{frac_city:.1%} of {total} edges are city_inventory")
    if not okA:
        print("      → diagnose: python -c \"from walkability.graph.build import "
              f"diagnose_spatial_join; from walkability.graph.inventory import "
              f"CITY_PROFILES as C; diagnose_spatial_join(profile=C['{profile.name}'])\"")

    # B/E — the environment (safety) factor covers every edge
    check("B/E environment_score present on every edge", env_none == 0,
          f"{env_none}/{total} missing (env layers narrower than the graph?)")

    # C — optional layers, if present, produce a non-degenerate signal
    for arr, key, layer in ((ind, "industrial_exposure", "landuse"),
                            (sep, "road_separation", "roads"),
                            (park, "parking_exposure", "parking")):
        if not profile.env_layer_path(layer).exists():
            print(f"    [INFO] C {key}: {layer} layer absent — sub-signal off by design")
            continue
        std = float(np.std(arr)) if arr else 0.0
        check(f"C {key} varies ({layer} layer actually applied)", std > 1e-9,
              f"std={std:.4f} over {len(arr)} edges (all-constant ⇒ layer not used)")

    # D — condition scale not inverted / mis-scaled
    rt_bad = rt_n = 0
    for i in range(0, 101, 5):
        m = i / 100.0
        try:
            back = profile.condition_to_score(profile.aggregate_condition(m))
        except Exception:
            back = None
        if back is None:
            continue
        rt_n += 1
        if abs(back - m) > 0.02:
            rt_bad += 1
    check("D condition round-trip identity (aggregate∘to_score)", rt_bad == 0,
          f"{rt_bad}/{rt_n} grid points off by >0.02")
    if csurf:
        gerr = sum(1 for ss, cd in zip(csurf, ccond)
                   if profile.condition_to_score(cd) is None
                   or abs(profile.condition_to_score(cd) - ss) > 0.05)
        check("D condition scale applied correctly on city edges", gerr == 0,
              f"{gerr}/{len(csurf)} surface_score ≠ condition_to_score(raw)")
    else:
        print("    [INFO] D graph-side skipped — no city edges carry a raw condition")

    # F — material vocabulary maps (not everything falls to None)
    if cmat_total:
        frac_mat = cmat_present / cmat_total
        check("F material vocabulary maps (material score not all-None on city edges)",
              frac_mat > 0.0, f"{frac_mat:.1%} of {cmat_total} city edges scored")

    # G — phantom leakage (build-time; only a note here)
    print("    [INFO] G phantom-leakage full check is build-time (raw inventory + "
          "profile.is_phantom); not re-derivable from the graph alone")

    # H — width handled per the profile
    if profile.width_field is None:
        check("H width dropped: width_score null everywhere (profile honoured)",
              wscore_present == 0, f"{wscore_present} unexpected non-null width_score")
    else:
        check("H width_score present on some edges", wscore_present > 0,
              f"{wscore_present} edges")
        if cwidth:
            top, topn = Counter(round(w, 1) for w in cwidth).most_common(1)[0]
            frac = topn / len(cwidth)
            check("H width not a single dominating constant", frac < 0.60,
                  f"{frac:.0%} of city widths logged as {top} ft")

    # K — empty-factor saturation (a spike at _EMPTY_WALK = schema assembly failure)
    frac_empty = empty / len(walk_vals) if walk_vals else 0.0
    check("K empty-factor saturation < 5%", frac_empty < 0.05,
          f"{frac_empty:.1%} of edges at walk={_EMPTY_WALK}")


def fold_invariants(G) -> None:
    """Run verify_system's G-parameterised invariants and fold their PASS/FAIL
    into this gate's totals (reuse, not re-implement)."""
    global _PASS, _FAIL
    vsys._PASS = 0
    vsys._FAIL = 0
    vsys.check_schema_and_bounds(G)
    vsys.check_renormalization()
    vsys.check_coercion()
    vsys.check_baked_consistency(G)
    vsys.check_cost_model()
    vsys.check_snapping(G)
    _PASS += vsys._PASS
    _FAIL += vsys._FAIL


# ---------------------------------------------------------------------------
# Drift baseline (per city) — problem_routes metrics + crc32 path fingerprint
# ---------------------------------------------------------------------------

def run_baseline(outcomes, city: str, fp: str, seed: int, update: bool) -> None:
    path = Path(__file__).with_name(f"verify_city_baseline.{city}.json")
    stored = json.loads(path.read_text()) if path.exists() else {}
    meta = stored.get("_meta", {})
    base_rows = {k: v for k, v in stored.items() if k != "_meta"}

    cur = {f"{rt.name}:{o.spec.label}": (route_metrics(o.route) if o.route
                                         else {"found": False})
           for rt, o in outcomes}

    lost = drifted = 0
    for key, c in cur.items():
        verdict = classify(key, base_rows.get(key), c)
        if "route lost" in verdict:
            lost += 1
            print(f"    [LOST] {key}: {verdict.strip()}")
        elif verdict.split()[0] in ("IMPROVED", "REGRESSED", "changed"):
            drifted += 1
    if meta.get("graph_fingerprint") and meta["graph_fingerprint"] != fp:
        print("    [INFO] graph fingerprint changed since baseline (a rebuild) — "
              "drift is expected; re-run with --update to re-anchor")

    check("no battery route lost vs baseline", lost == 0, f"{lost} routes lost")
    print(f"    baseline: {len(cur)} rows, {drifted} drifted, {lost} lost")

    if update:
        out = {"_meta": {"city": city, "seed": seed, "graph_fingerprint": fp,
                         "n_rows": len(cur)}, **cur}
        path.write_text(json.dumps(out, indent=2))
        print(f"    baseline updated → {path.name}")
    elif not base_rows:
        print("    no baseline yet — re-run with --update to record it")


# ---------------------------------------------------------------------------
# CSR parity + calibration deck
# ---------------------------------------------------------------------------

def run_parity(city: str, n: int) -> None:
    print("\n== CSR ↔ MultiDiGraph parity (route type #12) ==")
    print(f"    (smoke: n={n} pairs; full gate = python notebooks/verify_csr_parity.py "
          f"--city {city})")
    try:
        rc = verify_csr_parity.run(city, n=n)
    except Exception as exc:  # pragma: no cover
        check("csr parity gate", False, f"error {type(exc).__name__}: {exc}")
        return
    if rc == 2:
        print("    [INFO] parity skipped — runtime/csr pickle missing "
              "(build with: python -m walkability.graph.compact --city "
              f"{city} --csr)")
        return
    check("csr router matches the MultiDiGraph router", rc == 0, f"gate exit {rc}")


def emit_survey(ctx, city: str) -> None:
    print("\n== Calibration deck (the one manual step) ==")
    try:
        # Deck routes come from the city-wide spatial pool (short, representative,
        # score-spread) — NOT the battery candidates, which are QA stimuli.
        out = calibration_survey.build_auto_survey(ctx, city, k=15)
    except Exception as exc:  # pragma: no cover
        print(f"    [WARN] could not build calibration deck: {type(exc).__name__}: {exc}")
        return
    tname = ("calibration_targets.csv" if city == "boston"
             else f"calibration_targets.{city}.csv")
    print(f"    open {out.name} and fill ideal_score + notes into "
          f"{tname} (matched by route_name) — the ONLY human step "
          f"(structural checks are automated)")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Per-city 'loaded correctly' verification gate.")
    ap.add_argument("--city", default="boston", choices=sorted(CITY_PROFILES))
    ap.add_argument("--update", action="store_true", help="Write/refresh the drift baseline.")
    ap.add_argument("--seed", type=int, default=7, help="Battery sampling seed.")
    ap.add_argument("--scale", type=int, default=1, help="Multiply pairs per route type.")
    ap.add_argument("--no-parity", action="store_true", help="Skip the CSR parity gate.")
    ap.add_argument("--parity-n", type=int, default=6,
                    help="CSR parity O–D pairs (smoke; the Nx side is slow on long "
                         "routes). Full gate: python notebooks/verify_csr_parity.py.")
    ap.add_argument("--no-survey", action="store_true", help="Skip the calibration deck.")
    args = ap.parse_args()

    profile = CITY_PROFILES[args.city]
    print(f"=== verify_city: {args.city} ===")
    print(f"Loading enriched graph {profile.enriched_path.name} ...")
    G = load_graph(profile.enriched_path)
    fp = source_fingerprint(G)

    print("\n== Static per-city checks (A–K) ==")
    check_static(G, profile)

    print("\n== Invariants (reused from verify_system) ==")
    fold_invariants(G)

    print("\n== Route-type battery (the global taxonomy, instantiated) ==")
    ctx = route_types.Ctx(G, profile, seed=args.seed, scale=args.scale)
    # (battery candidates are no longer the deck source — see emit_survey)
    _candidates, ran, skipped, outcomes = route_types.run_battery(ctx, check)
    print(f"  battery: {ran} types run, {skipped} skipped")

    print("\n== Drift baseline ==")
    run_baseline(outcomes, args.city, fp, args.seed, args.update)

    if not args.no_parity:
        run_parity(args.city, args.parity_n)
    if not args.no_survey:
        emit_survey(ctx, args.city)

    print(f"\n{_PASS} passed, {_FAIL} failed.")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
