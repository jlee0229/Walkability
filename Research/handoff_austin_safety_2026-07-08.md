# Hand-off — Austin safety calibration (2026-07-07/08)

Branch: `chapter-d-austin-inventory`. Working the low-end over-scores from the
2026-07-06 Austin ground-truth survey (root causes #1 missing sidewalks, #2 safety
too high on car exposure, #3 crossings unmodeled).

## Current state (one line)
Root cause **#2 (car exposure) largely fixed** via a freeway-frontage veto (Airport
+47→+3 vs ideal); **#1 (missing sidewalks) is the biggest remaining error and is
deferred**; #3 untouched.

## Git / build state
- **Committed `3f922a9`** (not pushed): arterial speed imputation, parking-moat eyes
  discount, freeway-frontage veto. 8 code files.
- **Uncommitted:** `notebooks/calibration_survey.py` only — the N.Lamar coordinate
  fix (@Rundberg mis-routed onto residential side streets). Decide whether to commit.
- **Austin enriched graph rebuilt `--force`** — bakes `parking_exposure` +
  `freeway_hazard`. **Boston NOT rebuilt** → both levers inert there until it is
  (needs `build --city boston --force` + `compact` + release re-upload for the app).
- `data/osm/austin_arterials.gpkg` was re-downloaded with a now-unused `lanes`
  column (harmless; lanes amplifier was reverted).

## What shipped (committed, baked into Austin)
1. **Arterial speed imputation** (`environment.impute_arterial_speeds`) — untagged
   arterial → median speed of nearest same-class tagged arterials. Fixed downtown
   flatness (Congress 72→81).
2. **Parking-moat eyes discount** — surface-lot polygons, setback-gated. Marginal
   (−1 to −3). **Parking is now a closed question:** street-vs-lot differentiation
   refuted on coverage (`parking:lane` only 0–16% tagged citywide).
3. **Freeway-frontage HAZARD VETO** — the win. Per-edge `freeway_hazard` (at-grade
   motorway/trunk ∪ frontage-road proximity, convex-openness-gated so riverside/
   greenway paths are spared) → a NON-COMPENSATORY route multiplier applied OUTSIDE
   the floored geometric mean (`factors.apply_freeway_veto`), worst-segment weighted.
   Locked **k=2 / p=3 / VETO=0.9** (conservative). Boston immune (buried I-93 dropped
   by `_drop_underground`). Bake-time knobs (reaches/frac/OPEN_EXP) need a rebuild;
   `FREEWAY_VETO_EXPONENT`/`_STRENGTH` are query-time.
   **Results: Airport 73→30, Ben White 66→42, real I-35/US-183 frontages → 9/15.**

## What was refuted (do not retry without new evidence)
- **eyes_rescue_cap tightening** — barely moved stroads, hurt walkable Rainey.
- **Lanes (stroad-width) amplifier** — reverted. Hit walkable multi-lane streets
  (Guadalupe/The Drag −8) HARDEST; width doesn't separate a walkable busy street
  from a stroad (the pedestrian environment does, already modeled).

## Progress vs ground truth (model now → your 2026-07-06 ideal)
- ✅ Airport 30→27, Ben White 42→32, top routes 81–86 (well-calibrated).
- ❌ **Travis Heights 79→52 (+27)**, **North Loop 81→57 (+24)** — the "no sidewalk"
  residential routes; the deferred #1 lever is their fix.
- ❌ Research Blvd 52→77 (−25 UNDER) — separated sidewalk floored too hard; veto may
  nudge it lower. Untouched.

## Open items (priority order)
1. **Missing-sidewalk lever (#1)** — scoped, deferred, but it's the biggest remaining
   error (Travis Heights/North Loop). Signal = OSM-tier road-class edge with no city
   inventory match (`data_source`). **CRITICAL confound: gate to the inventory's
   jurisdiction** (Boston hull towns have sidewalks but aren't in Boston's inventory).
   Keep minimal (one parameter), Boston-null-validated. See `memory/missing-sidewalk-lever.md`.
2. **Human re-survey** to re-anchor before more tuning — parameters are provisional
   (fit to ~20 routes). You already revised E.Riverside ideal 62→70 this way.
3. **Research Blvd under-scoring** (separated sidewalk) + **crossings (#3)** — untouched.
4. **Boston rebuild** to propagate parking + veto (and re-baseline problem_routes then).

## Verification (all green as of hand-off)
- `verify_system.py`: 23/0.
- `problem_routes.py`: 0 diffs on Boston (levers inert there — correct).

## Resume commands
```
python notebooks/calibration_survey.py --city austin   # regenerate survey (no rebuild)
python -m walkability.graph.build --city austin --force # only if changing baked scores
python notebooks/verify_system.py                       # invariants
```

## Grounding caveat (important)
Direction & structure are literature-grounded (barrier effect / community severance;
non-compensatory / peak-end / weakest-link). The specific knobs are NOT — they're fit
to a small, subjective route set. Treat scores as provisional; validate via the
calibration survey, not by tuning to individual routes. See
`memory/austin-safety-levers-2026-07.md`, `memory/austin-groundtruth-calibration.md`,
`memory/prefer-underscoring-to-overscoring.md`.
