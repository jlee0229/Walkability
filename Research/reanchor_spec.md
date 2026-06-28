# Distribution re-anchor — implementation spec

Stretch the compressed top so the human tier bands from
`notebooks/calibration_targets.csv` (2026-06-26, 30 routes) are reproduced:

| tier | target band |
|---|---|
| car_free | ~93 |
| buffered | 86–89 |
| good | 80–85 |
| mixed | 73–79 |
| poor | 60–65 |

Today the car-shared world clusters 84–89 and the top is **unreachable** (max ~89
even for a car-free path). Model error is structured, not a global offset
(mean Δ +0.8, but: too HIGH on car-shared commercial — mission_hill/chinatown +6,
allston +5; too LOW on the car-free top — jamaica_pond −7 — and Seaport −10).

## Two problems the data exposes — an output transform fixes NEITHER
1. **Inverted ranking.** Model ranks smooth-pavement car-shared (allston 88) ABOVE
   rough-surface car-free (jamaica_pond 86); the human wants the reverse (83 vs 93).
   A monotonic raw→display transform preserves order, so it cannot fix this — it
   needs a *scoring* change.
2. **Ceiling-capped top.** jamaica_pond's max dimension is 0.90, so reweighting
   can't push its walk past ~90 (simulated: every `CATEGORY_WEIGHTS` set caps at
   88–89). To reach 93 a dimension must exceed 0.90 ⇒ the safety ceiling must rise.

So the re-anchor is a **scoring change (raise ceilings + rebalance), not a display
rescale**. Four levers, simulate-first, tuned one at a time against the targets.

## Lever 1 — Raise the safety ceiling for separated routes (headline; unblocks the top)
`safety = environment_score = sqrt(car × eyes)`. `car` is already graded by
`road_separation` (env-rework B: `CAR_SAFETY_CEIL + (1−CEIL)·sep`, reaches ~1.0).
The remaining cap is **`EYES_CEIL = 0.85`**, which pins safety at
`sqrt(car_high × 0.85) ≈ 0.90`. **Grade `EYES_CEIL` by separation, mirroring B:**
```
eyes_ceil = EYES_CEIL + (1 − EYES_CEIL) · road_separation
eyes      = min(eyes_ceil, noisy_or)
```
A genuinely separated route (sep→1) can then reach safety ~0.95; road-adjacent
(sep~0) stays at 0.85 — the top band opens *only* for pedestrian-designed routes,
exactly the "no route >90 unless car-free" principle.
- **Expose `eyes_uncapped`** (the pre-cap noisy-OR) as a stored edge field — like
  `industrial_exposure`/`road_separation` — so this lever is tunable offline from a
  single rebuild (today `eyes_score` is stored post-cap, so the grading can't be
  simulated without it).
- **Alternative grading signal to test:** `openness` rather than `road_separation`
  — jamaica_pond's safety comes from pond *openness*, and a remote separated path
  has *fewer* eyes, not more. Decide A/B from the survey (both are stored signals).

## Lever 2 — Rebalance CATEGORY_WEIGHTS (fix ranking + lower the car-shared cluster)
`safety` up, `comfort` down: **~1.6 / 0.5 / 1.0** (from 1.15 / 0.7 / 1.0).
Simulated offline on the stored dimension scores: flips jamaica_pond > allston and
nudges MAE 2.8 → 2.5. Makes car-freeness/safety dominate pavement smoothness and
pulls the car-shared cluster down (their lower safety now counts for more). Tunable
fully offline from `calibration_targets.csv` (recompute `combine_categories` with
trial weights) — pick the value *after* Lever 1 lands, since the two interact.

## Lever 3 — Unpaved-path comfort fix (the top + the user's explicit note)
jamaica_pond comfort = 0.77 from an unknown/natural path surface; the user: "score
suffers from low surface rating on paths without clearly defined surfaces." A
**pedestrian-dedicated** path shouldn't be penalised for being unpaved (it's a
feature). **Grounding step first:** confirm the pond edges' `surface_score` and what
drives 0.77 (likely the geometric/OSM-tag fallback). Then give unknown surface on
`is_pedestrian_dedicated` ways a neutral comfort, not a low one — without lifting
genuinely bad paved surfaces. Scope to surface scoring in `build.py`.

## Lever 4 (optional finish) — lower CAR_SAFETY_CEIL base
If the `good` car-shared cluster is still above 80–85 after 1–3, lower the base
0.85 → ~0.80 to bring it into band. Simulated alone it's a uniform ~3-pt downshift
(no spread), so it's a finishing trim, not a primary lever.

## Order, method, validation
1. **Implement Levers 1–3 + expose `eyes_uncapped`; one `--force` rebuild.**
2. **Tune offline against `calibration_targets.csv`, one lever at a time** using the
   stored sub-signals (`road_separation`, `openness`, `eyes_uncapped`, dimension
   scores): pick the `EYES_CEIL` grading, then `CATEGORY_WEIGHTS`, then the comfort
   rule, then (if needed) the `CAR_SAFETY_CEIL` base.
3. **Validate:** each tier lands in its band; rankings match (esp. car_free >
   buffered > good > mixed > poor); minimise MAE vs `ideal_score`,
   **confidence-weighted** (down-weight `rough` rows); `buffered` must stay 86–89,
   not jump to 93 (the over-lift guard).
4. **`--force` rebuild → re-baseline `problem_routes_baseline.json` → re-survey the
   30** (regenerate `calibration_survey.html` + refresh the `model_*` columns).

## Risks
- Raising the top can re-inflate routes we want mid (guard: buffered stays 86–89).
- Comfort-down can under-value genuinely bad surfaces (watch comfort-sensitive routes).
- Multi-lever ⇒ re-survey is mandatory; this supersedes the per-route ideal *ranges*
  (now point targets) and likely shifts which routes the survey flags.

## Dependency note — the low end is under-sampled (next-city phase)
Boston barely populates `poor`/`hostile` (the survey's "hostile" parkways came back
`good`/"feels fine"; only morrissey/dorchester_fields/charlestown_sullivan sit low).
So the re-anchor calibrates the **top and middle** well but the **bottom anchor is
soft**. See memory `less-walkable-city-next` — a genuinely car-dependent city is the
way to settle 50–65, and should follow the re-anchor (the Boston scale defines the
reference the new city is read against).
