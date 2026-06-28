# calibration_targets.csv — point-estimate calibration ground truth

Human walkability **point estimates** for fitting the score scale (the planned
distribution re-anchor, and future calibration work). This is the *calibration*
counterpart to `ground_truth.csv`: where `ground_truth.csv` logs per-segment
field observations (surface, condition, sides) for **flagging** routes OK/HIGH/LOW
against fuzzy ranges, this file collects a single committed **number per route**
for **fitting** the raw→displayed transform.

## Why points, not ranges
The earlier "ideal 80–85" ranges were a pass-band for flagging. For calibrating a
transform they're too coarse: the compression we're resolving is only ~3–5 points,
so a 5-wide range is as wide as the signal and can't pin the ordering/spacing the
re-anchor must reproduce. A point forces commitment; a transform needs a target.

## What to actually sweat (read before filling)
The **absolute** number is the fuzzy part and drifts. What the re-anchor most needs,
and what human judgment is most reliable at, is **relative**:
1. **Ordering** — rank the routes, especially within the tails (is the car-free
   greenway above the busy commercial street? by how much?).
2. **Tier gaps** — how *much* higher is a pedestrian-designed route than a normal
   sidewalk-beside-traffic? 3 points or 12? That gap magnitude *is* the re-anchor.
3. **Hard anchors** — two or three confident endpoints ("worst here ≈ 55", "best
   car-free ≈ low 90s") pin the scale; the middle interpolates.

So: give an exact `ideal_score`, but invest your confidence in getting the *ordering
and tier-gaps* right, not 82-vs-83. Tag `confidence` so the fit can down-weight a
guess and not over-fit it (the false-precision trap of point estimates).

**Judge from the map / Street View, not the model number.** `model_score` is in the
file for later drift analysis, but reading it first anchors your estimate to it.

## Columns
Human-filled (left side):
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
  are the test of whether B's graded ceiling lifts a buffer enough — but it can't
  see buffer *quality* (a bike lane vs empty asphalt read the same).
- `notes` — anything: which dimension feels off, a bad detour, "should be higher because…".

Reference (right side, pre-filled — do not edit by hand):
- `model_score`, `model_safety`, `model_comfort`, `model_path`, `model_len_m`
  — the model's current values, for drift analysis and to see *which dimension*
  diverges from your number.

## Model snapshot
The `model_*` columns were generated **2026-06-26** against the model state:
env-rework (A industrial down-weight + B graded car ceiling) + crossing-aware
phase-3 guard, at `alpha=2.0`. The scale is currently compressed at the top —
no route exceeds ~88 even when pedestrian-designed (e.g. `jamaica_pond_loop`,
`road_separation` 0.65, scores 86 — same as an ordinary sidewalk). The re-anchor
will use *your* `ideal_score`s to give the top band headroom (let separation lift
the whole score) while pulling the car-shared cluster down.

## Extending / re-running
Append new routes by adding them to `SURVEY_ROUTES` (calibration_survey.py) and
regenerating the `model_*` columns. To refresh the reference values after a scoring
change (without touching your filled-in `ideal_score`s, re-merge on `route_name`):
the model columns come from `find_routes(...).walk_score` and `.dimension_scores`
at `alpha=2.0` — the same call used to build the survey HTML. Re-snapshot the model
version/date in this README each time so a row's `model_*` is never ambiguous about
which model produced it.
