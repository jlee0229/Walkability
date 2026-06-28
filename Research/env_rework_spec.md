# Environment-factor rework — implementation spec

Two independent improvements to the **safety** dimension, landed in **one
`--force` rebuild**. Both are grounded in the 2026-06-26 Newmarket investigation
(see memory `newmarket-safety-high-needs-landuse`).

- **A — Industrial down-weight.** Lower safety on truck-heavy industrial
  corridors. Fixes the two graded inflations found on Newmarket: `car_safety`
  pinned at the 0.85 ceiling beside an industrial arterial, and `enclosure`
  (eyes) crediting warehouse footprints that provide no real "eyes".
- **B — Graded car-safety top.** Replace the `min(CAR_SAFETY_CEIL, …)` *clip*
  (which flattens every road-adjacent AND every separated path to exactly 0.85)
  with a graded ceiling: road-adjacent paths still top at 0.85, but genuinely
  road-separated paths (greenways, the HarborWalk, pedestrian bridges) climb
  toward 1.0. Separation becomes a rewarded signal instead of a lost dimension.

**A lowers Newmarket; B raises greenways. They are orthogonal** — A does not
touch separated paths, B does not touch road-adjacent ones. Implement both but
keep them isolable (see §6).

---

## 1. New data inputs (`graph/download_environment.py`)

### 1a. Landuse → `boston_landuse.gpkg` (feeds A)
```python
def download_landuse(force=False):
    gdf = ox.features_from_place(PLACE, tags={"landuse": LANDUSE_TAGS})
    gdf = gdf[gdf["landuse"].isin(LANDUSE_TAGS)]
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])]
    _save(gdf[["geometry", "landuse"]], LANDUSE_PATH)
```
`LANDUSE_TAGS` starts as `["industrial"]`; optionally add `["retail",
"commercial"]` at a lower car-penalty weight later. Polygons only.

### 1b. All-roads → `boston_roads.gpkg` (feeds B)
```python
def download_roads(force=False):
    gdf = ox.features_from_place(PLACE, tags={"highway": ROAD_HIGHWAY_TAGS})
    gdf = gdf[gdf["highway"].isin(ROAD_HIGHWAY_TAGS)]
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
    _save(gdf[["geometry"]], ROADS_PATH)
```
`ROAD_HIGHWAY_TAGS` = all car-carrying classes (`motorway, trunk, primary,
secondary, tertiary, unclassified, residential, living_street, service` +
`_link`s). This is the **distance-to-nearest-ANY-road** source B needs to tell a
park path from a calm-street sidewalk — the walk graph itself can't, because
footway-dense cores (Beacon Hill) carry no road centrelines.

### 1c. Wiring
- New `LANDUSE_PATH`, `ROADS_PATH` in `environment.py` (next to `ARTERIALS_PATH`).
- Add both to `_missing_inputs()`, `main()`, and `load_landuse()` /
  `load_roads()` cached loaders (mirror `load_arterials`).
- **Backward-compatible:** if either file is missing, the corresponding signal
  defaults to a no-op (`industrial_exposure=0`, `road_separation=0` → today's
  behaviour), consistent with the pipeline's "missing input just disables it".

> **`download_buildings` fallback (only if 1a coverage is sparse):** change
> line 87 to keep `["geometry", "building"]` so `enclosure` can down-weight
> `building in {warehouse, industrial, commercial}` directly. Decide after the
> §3 gate.

---

## 2. weights.py — new constants (single source of truth)
```python
# --- Industrial down-weight (A) ---
LANDUSE_TAGS: list[str] = ["industrial"]          # + retail/commercial later
INDUSTRIAL_REACH_M: float = 30.0                  # buffer for "in/near industrial"
INDUSTRIAL_CAR_PENALTY: float = 0.35              # car *= (1 - p·exposure); 0.85→~0.55
INDUSTRIAL_ENCLOSURE_DISCOUNT: float = 1.0        # enclosure *= (1 - d·exposure); 1.0 = full

# --- Graded car-safety top (B) ---
ROAD_HIGHWAY_TAGS: list[str] = [ ...all car classes + _links... ]
SEPARATION_REACH_M: float = 25.0                  # no road within this ⇒ fully separated
CAR_ADJACENT_CEIL: float = 0.85                   # ceiling for a road-ADJACENT path
                                                   # (replaces CAR_SAFETY_CEIL's role)
```
Keep `CAR_SAFETY_CEIL` name as an alias of `CAR_ADJACENT_CEIL` or rename
everywhere. `EYES_CEIL` is **untouched** (the eyes cap has the same flattening
critique but is out of scope here — note it for a later pass).

---

## 3. Viability gate — run BEFORE any tuning
This sandbox blocks Overpass, so this must be run in a normal shell:
1. `python walkability/graph/download_environment.py --force` (now fetches
   landuse + roads too).
2. Verify on the Newmarket route (and 1–2 other industrial spots, e.g.
   Southampton St, Widett Circle): does `landuse=industrial` actually cover the
   corridor, and is the roads layer complete? Print `% route length within
   INDUSTRIAL_REACH_M of industrial` and `% within SEPARATION_REACH_M of a road`.
3. **If landuse coverage is sparse →** fall back to the building-type signal
   (§1c note) for A's enclosure half, and reconsider A's car half (it may need
   the building-type proxy too).

The gate is the same check I couldn't close earlier (network-blocked). Ground it
before changing constants — same discipline that refuted the worst-segment and
speed-based levers.

---

## 4. environment.py — scoring changes

### 4a. Per-edge signals (in `build_environment_index`, mirroring `_arterial_scores`)
```python
industrial = _proximity_score(edges_metric, landuse, INDUSTRIAL_REACH_M)   # [0,1], 1=in/near
separation = _separation_score(edges_metric, roads, SEPARATION_REACH_M)    # [0,1], 1=no road near
```
`_proximity_score` = ramp on distance-to-nearest polygon (buffer/sjoin_nearest);
`_separation_score(d) = min(1, d / SEPARATION_REACH_M)` on distance-to-nearest
road (0 when on top of a road, 1 when ≥ reach away). Both are one `sjoin_nearest`
each — same cost as the existing arterial/openness joins.

### 4b. car_safety — graded ceiling + industrial penalty
```python
on  = on_path_safety(hwy, ms)
off = 1.0 if _is_arterial(hwy) else off_scores.get(eid, 1.0)
ceil = CAR_ADJACENT_CEIL + (1.0 - CAR_ADJACENT_CEIL) * separation   # B: 0.85 → 1.0
car  = min(ceil, on, off)
car  = car * (1.0 - INDUSTRIAL_CAR_PENALTY * industrial)            # A: truck penalty
```
- **B** only ever *raises* the ceiling for separated edges; a fast road on/near
  the path still dominates via `min`, so low values are untouched (discrimination
  preserved). Road-adjacent edges keep `ceil=0.85` ⇒ Newmarket unchanged by B.
- **A** multiplies after, so an industrial-adjacent footway drops 0.85 → ~0.55.

### 4c. enclosure — warehouse discount (A)
Thread `industrial` into `perceived_safety`:
```python
enclosure = 0.0 if enclosure_blind else _sat(bldg_count, EYES_BLDG_SAT)
enclosure *= (1.0 - INDUSTRIAL_ENCLOSURE_DISCOUNT * industrial)     # A
noisy_or  = 1.0 - (1-activity)*(1-enclosure)*(1-openness)
```
A warehouse-dense block now contributes little/no enclosure "eyes"; activity and
openness still can (a genuinely busy industrial frontage keeps its activity).

### 4d. Expose the sub-signals on the edge (for §6 isolation + diagnostics)
Add `industrial_exposure` and `road_separation` to the per-edge dict returned by
`build_environment_index` (alongside `car_safety_score`, `eyes_score`). Lets a
diagnostic recompute car with each lever toggled **without a second rebuild**,
and lets `safety_breakdown` show them.

---

## 5. Build & verification
1. `python walkability/graph/download_environment.py --force`  → §3 gate.
2. `python -m walkability.graph.build --force`  (re-bakes `environment_score` +
   `walk_score`).
3. `python notebooks/verify_system.py`  — invariants. **Check none hard-code
   car ≤ 0.85** (B now allows >0.85); add an invariant that
   `environment_score ∈ [0,1]` still holds (it must: car,eyes ≤ 1).
4. `python notebooks/problem_routes.py --update`  — re-baseline.
5. `python notebooks/calibration_survey.py`  — regen, then **re-survey** with
   three explicit checks:
   - **A works:** Newmarket / Allston safety drops (target Newmarket walk → mid-60s).
   - **B works:** a greenway / HarborWalk / Comm Ave Mall route car-safety now
     exceeds 0.85 and the route score rises.
   - **No regression:** calm-street sidewalks (Beacon Hill, Back Bay, North End)
     do **not** spuriously rise — confirm their `road_separation ≈ 0` (the
     misclassification risk if the roads layer is incomplete).
6. `safety_breakdown` (D1) on Newmarket (car should drop) + one greenway (car
   should exceed 0.85, separation ≈ 1).

---

## 6. Lever isolation (one-lever discipline)
A and B share one rebuild but must be measured separately:
- Because §4d **stores `industrial_exposure` and `road_separation` on each
  edge**, a diagnostic can recompute `car_safety` / `environment_score` with
  `INDUSTRIAL_CAR_PENALTY=0` (A off) or `separation→0` (B off) **offline, no
  rebuild** — read the structural effect of each independently from the single
  baked graph.
- Tuning order: rebuild once → measure A-only and B-only via the toggle →
  then set `INDUSTRIAL_CAR_PENALTY` / `SEPARATION_REACH_M` from the survey, one
  at a time. Re-baseline after each constant change (query-time once baked? no —
  these bake, so a constant change needs a rebuild; minimise by tuning on the
  stored sub-signals first to pick values, then one final rebuild).

---

## 7. Open decisions to confirm before coding
1. **Roads layer** (proper, robust) vs **openness-as-separation proxy** (no new
   layer, but only rewards park-side separation, misses ped bridges over
   highways). Spec assumes the roads layer — confirm the extra download is worth
   it. (Recommended: yes, we're rebuilding anyway and it's the honest signal.)
2. `LANDUSE_TAGS` = industrial only, or also commercial/retail at lower penalty?
   (Recommended: industrial only first; widen after survey.)
3. `INDUSTRIAL_CAR_PENALTY` starting value (0.35 → 0.85·0.65 ≈ 0.55 car). Tune
   from survey.
4. Keep `EYES_CEIL` flattening for a later pass (out of scope) — confirm.

## Files touched
- `graph/download_environment.py` (+`download_landuse`, +`download_roads`, wiring)
- `graph/environment.py` (paths, loaders, `_proximity_score`, `_separation_score`,
  car/enclosure changes, sub-signal exposure)
- `scoring/weights.py` (new constants §2)
- `notebooks/diagnostics.py` (`safety_breakdown` shows industrial/separation)
- `notebooks/verify_system.py` (car>0.85 allowed; env∈[0,1] invariant)
- `notebooks/problem_routes_baseline.json` (re-baseline)
- `CLAUDE.md` (environment-factor section: graded ceiling + industrial signal)
