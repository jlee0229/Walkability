# Work & Verification — Outline

A structure for the "work" and "verification" sections of the writeup, with a
clear line drawn between what is verified automatically and what cannot be, and
*why*.

---

## Part A — Work (what was built)

1. **Data enrichment pipeline** (`walkability/graph/build.py`)
   - Four-tier edge enrichment (city inventory → OSM tag → context inference →
     geometric fallback); independent per-factor scores kept separate.
   - Baked `walk_score`/`walk_confidence` at default weights as a routing fast path.
2. **Composite scoring** (`walkability/scoring/factors.py`)
   - `edge_walkability` → `(walk_score, confidence)`, renormalised over present
     factors; single source of truth for foot-access classification.
3. **Routing** (`walkability/routing/`)
   - Cost model `length × (1 + α·(1 − walk_score))`; foot=no excluded,
     restricted access penalised.
   - Yen's k-shortest paths on a simple-graph projection; confidence as a
     post-hoc tiebreaker; ellipse clipping with auto-widen for performance.
4. **Diagnostics & workflow tooling** (`notebooks/`)
   - Three-tier inspection: `audit_route` (Tier 1), `inspect_route_map` /
     `score_heatmap` / `routes_over_heatmap` (Tier 2), street-imagery links (Tier 3).
   - Problem-route regression harness with a JSON baseline (`problem_routes.py`).
   - Multi-region dev subsets + heatmaps (`region_maps.py`, `DEV_REGIONS`).
   - Region-tagged ground-truth log (`ground_truth.csv`).

---

## Part B — Verification

### The dividing principle

> **Invariants can be automated; validity cannot.**
> An *invariant* is "the system does what the code claims" — bounded scores,
> connected routes, the clip never dropping the optimum. These are
> machine-checkable: there is a definite right answer derivable from the code.
> *Validity* is "what the code claims is actually true of the world" — is a
> `walk_score` of 0.86 *correct* for that block, is this route *good*? These
> have no machine-checkable ground truth, because the model encodes **subjective
> preferences** (weights) and consumes **input data that can diverge from
> physical reality** (stale SCI, missing sidewalks). Neither can be asserted.

### B.1 — Automated verification (`notebooks/verify_system.py`) — DONE

Run: `python notebooks/verify_system.py` (18 invariants pass; exit code gates CI).
Each is automatable because its correct answer follows from the code itself:

| Check | Invariant asserted |
|---|---|
| schema + bounds | every edge has `highway_score`/`length`; `walk_score`, `confidence` ∈ [0,1] |
| renormalisation | a missing surface factor does **not** drag the score to 0 |
| coercion | `_as_float`/`_as_str` handle `"0.55"`, `"None"`, `None`, `""` |
| baked consistency | baked `walk_score` == full recompute (max Δ ≈ 5e-5) |
| cost model | cost rises as walk falls; `foot=no` → impassable; restricted → ×penalty |
| route integrity | edges chain head-to-tail, all exist, no `foot=no`, length == Σ edge lengths |
| alpha floor | `α=0` route is never longer than a higher-α route |
| determinism | identical query → identical route |
| snapping | vectorised `snap_to_node` == brute-force nearest |
| clip correctness | clipped route == unclipped optimum (auto-widen guarantee) |

Also automated (separate harness): the **problem-route regression suite**
(`problem_routes.py`) — re-runs tracked routes against a baseline and reports
improved/regressed/same. This is *change detection*, not validity: it tells you
a route changed, not whether the new route is better.

### B.2 — Manual verification (cannot be automated) — and why

Each item below is followed by the reason automation is impossible *in principle*,
not just unimplemented.

1. **Is a `walk_score` correct in absolute terms?**
   *Why not automatable:* there is no ground-truth "true walkability" dataset to
   test against, and the score is a weighted blend of factors whose relative
   importance is a **subjective product decision** (is surface > road type?).
   There is no objective answer to check against. → Human ground truth
   (`ground_truth.csv`, `subj_walkability`).

2. **Is a chosen route subjectively good?** ("zig-zags", "feels unsafe",
   "avoids the obvious nice path")
   *Why not automatable:* "good" is a human preference over routes. Automating it
   requires a labelled set of human-preferred routes — which is itself a manual
   ground-truthing exercise (and the basis of any future learning-to-rank).

3. **Does the graph data match physical reality?** (SCI condition, `MATERIAL`,
   whether a sidewalk even exists)
   *Why not automatable:* the only ground truth is the physical world. City data
   can be stale or mis-tagged; the model can't detect this from within. → Field
   visit, Street View, or Mapillary (Tier 3 links).

4. **Do crossings physically exist / what is their quality?**
   *Why not automatable:* crossings aren't in our scored data at all (they're
   unused `highway=crossing` nodes), so there is nothing to assert against.
   Confirming one requires imagery.

5. **Did node-snapping pick the *intended* origin/destination?**
   *Why not automatable:* "nearest node" is verified automatically (B.1), but
   whether the nearest node is the location the *user meant* is a human call.

### B.3 — Becomes automatable *after* human data collection

These are blocked only by the human-labelling bottleneck, not by principle:

- **Calibration:** once `ground_truth.csv` has enough `subj_walkability` rows,
  compute correlation between model `walk_score` and human rating per region —
  then a regression test can assert the correlation doesn't drop.
- **Data-quality flags:** recorded `material_actual` / `condition_actual` vs the
  model's fields can be diffed automatically to flag stale/mismatched city data.

→ The high-leverage manual work is **collecting labelled observations**; once
they exist, the *checking* of them is cheap to automate.

---

## Suggested section ordering for the writeup
1. Work (Part A, condensed).
2. Verification philosophy (the invariants-vs-validity line, B intro).
3. Automated verification + results (B.1) — cite `verify_system.py` output.
4. Manual verification and why it's irreducible (B.2).
5. What the ground-truth log unlocks next (B.3).
