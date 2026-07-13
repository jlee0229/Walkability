# Route-type taxonomy — verifying a city is loaded correctly

This is the **global list** of route *kinds* that must pass for a city's enriched
graph to count as "correctly loaded", plus the boundary between what is verified
automatically and the one thing that genuinely needs a human. It is the
human-readable companion to the executable spec in
[`notebooks/route_types.py`](../notebooks/route_types.py) (`ROUTE_TYPES`) and the
gate that runs it, [`notebooks/verify_city.py`](../notebooks/verify_city.py).

> **How the global list becomes a per-city list.** Every route type carries a
> `selector(ctx)` that auto-derives its origin→destination pairs from the graph +
> `CityProfile` + downloaded OSM layers — **no coordinates are hand-typed**.
> Running `verify_city.py --city <name>` builds one `Ctx` for that city and
> iterates `ROUTE_TYPES` over it; that iteration *is* the instantiation. A type
> **self-skips** (its selector returns nothing) when the city can't support it
> (no data seam, no water layer, too small a cluster), so the same file works for
> Boston, Austin, or any future city in `CITY_PROFILES`.

## Why route testing (not just field checks)

A `CityProfile` can be wrong in ways that still *build successfully*. The load
failure modes we must catch:

| | Failure mode | Symptom |
|---|---|---|
| **A** | Inventory join collapsed / degenerate | 0% (or ~100%) of edges are `city_inventory` — wrong `metric_crs`, non-overlapping bbox |
| **B/E** | Required env layer missing / narrower than graph | some/all edges have no `environment_score` (whole safety factor gone) |
| **C** | Optional layer (roads/landuse/parking) missing | `road_separation`/`industrial_exposure`/`parking_exposure` silently all-zero |
| **D** | Condition scale inverted / mis-scaled | Austin's 1–5 vs Boston's SCI applied the wrong way |
| **F** | Material vocabulary drift | `surface_material_score` all-None despite raw tokens |
| **G** | Phantom / never-surveyed leakage | a "doesn't exist yet" sidewalk scored as real |
| **H** | Width mishandled | `width_score` present when it should be dropped, or a constant spike |
| **I** | Baked ≠ recompute / out-of-bounds | stale weights table, partial schema |
| **J** | Download filter / connectivity regression | severed bridge decks (Austin's Lady Bird Lake), gross detours |
| **K** | Empty-factor saturation | a spike of edges at the `_EMPTY_WALK` (0.40) floor |

Static field checks (in `verify_city.check_static`) catch A–K at the edge level.
The **route battery** below catches the ones that only show up end-to-end
(connectivity J, seam degradation B/E, relative discrimination D/C) and proves the
router actually works on this city's graph.

## The global list

| # | Route type | Verifies / catches | Auto-selector (graph/profile only) | Gates on |
|---|---|---|---|---|
| 1 | `short_band` | local routing valid 150–800 m (I, K) | distance-stratified pairs from the routable component | all found; walk∈[0,1]; edges chain; len ≥ crow-fly |
| 2 | `mid_band` | 800–3000 m | same, mid band | same as #1 |
| 3 | `long_band` | cross-city, no gross detours (J) | same, long band (capped at 0.9× graph diagonal) | #1 **+** len ≤ 5× crow-fly |
| 4 | `data_seam_straddling` | border edges keep env + OSM-tier score (B, E) | one endpoint in inventory territory, one deep in a no-inventory town — via **graph inventory-match contiguity**, not a bbox; **self-skips** if inventory ≈ whole graph | route found; every edge has `environment_score`; OSM-tier median walk gap ≤ 0.10 across the seam |
| 5 | `over_water_crossing` | a pedestrian bridge deck survived the download filter (J) | opposite banks of the largest **crossable** water body (short axis ≤ 1500 m); pairs whose straight O–D segment cuts the water; **self-skips** if no water layer | route found; route **crosses** the water; len ≤ 4× crow-fly |
| 6+7 | `walkability_anchors` | a good corridor outscores a stroad (D, C, H, K) | densest cell of P90-walk nodes vs densest cell of arterial (`highway_score` ≤ 0.15) nodes | **median(high) − median(low) ≥ 0.10** (relative — needs no human calibration) |
| 8 | `alpha_floor_monotonicity` | cost model: α=0 is the length floor | reuse mid-band pairs | len(α=0) ≤ len(α=3) + 1 m |
| 9 | `determinism` | identical query → identical route | one short pair | identical node path twice |
| 10 | `foot_no_integrity` | the router never emits a `foot=no` edge | pairs across all bands | no `foot=no` edge on any route |
| 11 | `terminal_restricted_access` | terminal restricted-access handling | a destination reachable only via `customers`/`private` | **INFO only** — reports `audit_route` flags; not gated (open design decision) |
| 12 | `csr_nx_parity` | the compact CSR router == the MultiDiGraph router | (external) invokes `verify_csr_parity.run(city)` over distance-stratified pairs | identical routes; exit 0 |

The single tuning knobs live at the top of `route_types.py` (`CELL_DEG`,
`SEAM_SCORE_TOL`, `ANCHOR_MARGIN`, `WATER_*`, detour factors). Selectors are
seeded, so a run is reproducible; `--scale` multiplies pairs per type for a
heavier sweep.

## The automated / genuinely-manual boundary

**Fully automated** (structural correctness — does the data join, score, route,
and hold together): everything above, plus the static A–K checks and the reused
`verify_system` invariants (schema/bounds, baked==recompute, cost model,
snapping). A gated failure exits non-zero.

**Genuinely manual — the one gap:** whether the scores match *human perception*
has no machine ground truth. `verify_city` auto-picks routes spanning the
observed walk-score spectrum (plus the anchor/seam/water cases) and emits a
calibration deck (`<city>_calibration_survey.auto.html`, via
`calibration_survey.build_auto_survey`) with per-dimension bars, numbered
segments and Street View links. The human fills subjective ratings into
`ground_truth.<city>.csv`. This is the invariants-vs-validity line from
[`work_and_verification_outline.md`](work_and_verification_outline.md): a build
can be *internally consistent* and still *mis-rate the world*, and only a person
can say which.

**Also not fully automatable (flagged, handled conservatively):**
- **Anchor absolute floors** — only the *relative* margin (high − low ≥ 0.10)
  gates; the absolute anchor scores are heuristics needing human calibration
  (reported as INFO).
- **Terminal restricted-access (#11)** — detectable, but whether it *should* be
  penalized is an open design call; reported, not gated.
- **Material strong form (F)** — a full check needs the raw material token
  retained on the edge (build-time); the graph-side "not all-None" form is
  automated.
- **Phantom leakage (G)** — phantoms are dropped before the join, so the full
  check belongs at build time with the raw inventory + `profile.is_phantom`.
- **Over-water banking** — the "crossable largest water body" heuristic is robust
  for a dominant river/lake (Charles, Lady Bird Lake) and self-skips on a
  non-elongated / absent water layer.

## Running it

```bash
# First run per city seeds the drift baseline:
python notebooks/verify_city.py --city boston --update
python notebooks/verify_city.py --city austin --update

# Thereafter, the gate (non-zero exit on any gated failure):
python notebooks/verify_city.py --city boston
python notebooks/verify_city.py --city austin

# Standalone: regenerate just the auto calibration deck for any city
python notebooks/calibration_survey.py --city austin --auto
```

Expected per city: **Boston** lights up the seam (Brookline/hull) and water
(Charles); **Austin** self-skips the seam (inventory ≈ whole graph) and lights up
the water (Lady Bird Lake) — where dropping `custom_filter` would sever the bridge
decks and this gate would catch it.
