# sparse_eyes_targets.csv — point-estimate ground truth for the sparse-eyes set

The `calibration_targets.csv` counterpart for the **sparse-eyes** route set
(`sparse_eyes_routes.py`) — the new/waterfront/converted-industrial,
wide-but-quiet routes added to give the "Seaport problem" (low `eyes_score`
where the street is actually pleasant) a real sample instead of n=1.

Same schema and intent as `calibration_targets.csv` (read that README first):
one committed human **point estimate** per route, with `confidence`/`tier`, for
fitting the score scale and catching per-dimension drift. Judge from the
map / Street View, not the model number.

## Model snapshot
The `model_*` columns were generated **2026-06-28** against the enriched graph
rebuilt that day with the **tunnel-arterial fix** (underground roads dropped from
off-path car-safety) **plus the re-anchor levers** (graded `EYES_CEIL` by openness,
`PED_PATH_COMFORT`, `CAR_SAFETY_CEIL` 0.85→0.82, `CATEGORY_WEIGHTS` reweight,
`eyes_uncapped`/`openness_score` fields) **and comfort top-compression**
(`COMFORT_COMPRESS_KNEE`=0.80 / `_K`=0.50) baked in, via
`find_routes(..., alpha=2.0).walk_score` / `.dimension_scores` — the same call
`calibration_survey._survey` uses. Re-snapshot this date if the model changes.
(Comfort compression nudged this set down ~1 pt: Fort Point 75→74, Northern Ave
71→70, Convention 71→69 — within noise; Fort Point still well above the old 62.)

## Survey verdict (2026-06-28, post-fix)
The env-rework + tunnel fix bring the Seaport family close to ground truth (vs the
original `seaport_congress` miss of 70 model / 80 ideal):

| route | ideal | model before | model after | note |
|---|---|---|---|---|
| seaport_northern_ave | 72 | 70 | 71 | accurate |
| seaport_blvd_convention | 69 | 71 | 71 | accurate; segs #3-4 safety slightly low / surface slightly high |
| fort_point_channel | 70 | 62 | 75 | tunnel fix: seg #2 env 0.11→0.46; slight overshoot |
| eastie_jeffries_point | 70 | 71 | 74 | accurate (levers lifted comfort) |
| southie_marine_park | 62 | 51 | 51 | low score is genuine (industrial + barred end); no tunnel nearby |

### Fixed: tunneled arterials were counted as at-grade (Fort Point seg #2)
Off-path car-safety treated the **tunneled** Mass Pike / Big Dig (`tunnel=yes`,
`layer=-1`) as an at-grade hostile road, cratering `car_safety` to ~0.05 on
surface footways directly above it (`arterial_proximity_score` ≈ 0.05). The
`boston_arterials.gpkg` download dropped the `tunnel`/`layer` tags, so the scorer
couldn't tell underground from at-grade. Boston's Big Dig buries I-90/I-93 through
Fort Point / downtown / Seaport, systematically suppressing otherwise-fine
waterfront edges. **Fix (landed 2026-06-28):** `download_environment.py` now keeps
`tunnel`/`layer` on arterials and roads; `environment.py::_drop_underground` drops
`tunnel=yes`/`layer<0` segments from both off-path proximity and road-separation.
127 underground arterial segments dropped citywide. Scoped to underground only —
elevated/bridge roads are left in (a pedestrian under a viaduct does feel it).
Residual Fort Point gap (seg #2 env 0.46, not 0.80) is the sparse-eyes (low
`eyes_score`) issue, not the tunnel.
