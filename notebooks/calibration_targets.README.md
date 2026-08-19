# calibration_targets.\<city\>.csv — the standing calibration ground truth

Human walkability **point estimates** for fitting/checking the score scale. This
is the **go-forward calibration format** — it replaced the rigid 1–5
`subj_walkability` column in `ground_truth.csv`. The recurring human task is now
just two things per route: **give an `ideal_score` (0–100) and a reason.**

Per-city, mirroring the `ground_truth.<city>.csv` convention:
`calibration_targets.csv` is Boston; every other city gets a sibling
`calibration_targets.<city>.csv` (e.g. `calibration_targets.austin.csv`).

Division of labour with the other ground-truth file:
- **this file** — one committed **number per route** for calibrating the score
  (the standing, recurring pass).
- **`ground_truth.csv`** — per-segment field observations (surface, condition,
  sides) used **on demand** to diagnose *why* a specific route is mis-scored. Not
  a recurring chore; reach for it only when a row here is wrong and you want to
  attribute it to a data gap vs. data-quality vs. weight problem.

## Where the rows come from (don't hand-pick)
The routes are **auto-derived** from the `verify_city.py` route-type battery (the
global taxonomy in `route_types.py`) — the same battery that drives the auto
calibration deck. Every run regenerates:
- `notebooks/<city>_calibration_survey.auto.html` — the visual deck (map, numbered
  segments, per-dimension bars, Street View links), and
- `notebooks/calibration_targets.<city>.csv` — this file, with the `model_*`
  reference columns **pre-filled and refreshed**, human columns left blank.

Regenerate either way:
```bash
python notebooks/verify_city.py --city austin            # emits deck + syncs this CSV
python notebooks/calibration_survey.py --city austin --auto --k 16          # deck + CSV
python notebooks/calibration_survey.py --city austin --auto --k 16 --blind  # blind deck
```
The sync is **merge-preserving**: it keys on `route_name`, so re-running refreshes
the `model_*` snapshot **without touching any `ideal_score` you've already filled**;
new battery routes appear blank, dropped ones fall off. Safe to run every build.

### Rate blind (recommended for the calibration pass)
`--blind` writes `<city>_calibration_survey.auto.blind.html`: the model's verdict
(overall score, dimension bars, audit flags) is hidden and the routes are shuffled,
so your `ideal_score` is an **independent** judgment instead of an echo of the model
— which is the whole point of a calibration target, and the only way to catch
systematic over/under-scoring (the model can't grade its own homework). Segment
colours + Street View stay as a navigation aid. The `model_*` columns are still
written to the CSV (blindness only governs what you *see* while rating). Use the
plain (non-blind) deck when you instead want to *verify* believability — there the
model number is the stimulus you react to.

## Workflow
1. Regenerate (command above) → open `<city>_calibration_survey.auto.html`.
2. For each card, read the map / Street View and decide your `ideal_score`. The
   grey chip after the card title (e.g. `walkability_anchors:high#0`) is the
   **`route_name`** — the row key. The copy-paste block at the bottom of each card
   mirrors the four human columns.
3. Fill `ideal_score` (+ `confidence`/`tier`/`notes`) in the matching row.
4. Later: fit / drift-check against `model_score` (see `Research/reanchor_spec.md`).

## What to actually sweat (read before filling)
The **absolute** number is the fuzzy part and drifts. What calibration most needs,
and what human judgment is most reliable at, is **relative**:
1. **Ordering** — rank the routes, especially within the tails (is the car-free
   greenway above the busy commercial street? by how much?).
2. **Tier gaps** — how *much* higher is a pedestrian-designed route than a normal
   sidewalk-beside-traffic? 3 points or 12? That gap magnitude is the signal.
3. **Hard anchors** — two or three confident endpoints ("worst here ≈ 55", "best
   car-free ≈ low 90s") pin the scale; the middle interpolates.

So give an exact `ideal_score`, but invest your confidence in the *ordering and
tier-gaps*, not 82-vs-83. Tag `confidence` so the fit can down-weight a guess and
not over-fit it — this is what buys back the flexibility of a range without the
false-precision trap of committing to two numbers.

**Judge from the map / Street View, not the model number.** `model_score` is in
the file (and on the card) for drift analysis, but anchoring your estimate to it
defeats the point.

## Why a point + confidence, not a range
The earlier "ideal 80–85" ranges were a pass-band for flagging. For calibrating a
scale they're too coarse: the compression we resolve is only ~3–5 points, so a
5-wide range is as wide as the signal and can't pin ordering/spacing. A point
forces commitment; `confidence=rough` restores the "I'm not sure" flexibility by
down-weighting soft estimates in the fit.

## Columns
Human-filled:
- `ideal_score` — your walkability point estimate, **0–100**.
- `confidence` — `sure` | `rough`.
- `tier` — coarse bucket for the relative ordering. Suggested vocabulary
  (roughly high → low):
  `car_free` (greenway / pedestrian mall / fully separated path) ·
  `buffered` (cars present but clearly separated — wide sidewalk, bike lane / cycle
    track / planting-strip / parked-car buffer between you and the travel lanes;
    e.g. Comm Ave sidewalks) ·
  `ped_priority` (cars present but calm/slow & pedestrian-first — Newbury, 10 mph
    North End) ·
  `good` · `mixed` · `poor` · `hostile` (fast parkway / arterial / industrial).
  `buffered` and `ped_priority` are two flavors of "high but not car-free" (earned
  via distance-from-traffic vs calm-traffic respectively) — both typically high 80s,
  above a bare sidewalk, below a true car-free path. NB: `road_separation` (distance
  to nearest road) is exactly the signal meant to reward `buffered`, so these rows
  test whether the graded ceiling lifts a buffer enough — but it can't see buffer
  *quality* (a bike lane vs empty asphalt read the same).
- `notes` — anything: which dimension feels off, a bad detour, "should be higher
  because…"; name a segment # if something's factually wrong on the ground.

Key + reference (auto-filled — do not edit by hand):
- `route_name` — the battery route id and the merge key. Matches the card chip.
- `area` — human-readable label for the route.
- `model_score`, `model_safety`, `model_comfort`, `model_path`, `model_len_m`
  — the model's current values, refreshed on every regenerate, for drift analysis
  and to see *which dimension* diverges from your number.

## History
The prior 30-route hand-picked deck used for the 2026-06-28 → 07-04 distribution
re-anchor (fit MAE ≈ 2.30, bias ≈ +0.3) is archived at
`notebooks/archive/calibration_targets.reanchor-2026-06-28.csv`. As of **2026-07-13**
this file is a fresh reset onto the auto-battery routes for a uniform, city-agnostic
system (Boston + Austin); those routes are re-rated from scratch.
