# AGENTS.md

Guidance for Codex working in this repo. Terse by design — it captures the
non-obvious rules and gotchas, not tutorials.

## Commands

```bash
pip install -e .                                    # required before anything

# Pipeline (per-city; --city selects a CityProfile, default boston). Each step
# reads paths/schema/CRS from the profile.
python -m walkability.graph.download --city austin              # base OSM walk graph
python -m walkability.graph.download_environment --city austin  # env feature layers → data/osm/<city>_*.gpkg
python -m walkability.graph.build --city austin                 # enriched graph (skips if output exists)
python -m walkability.graph.build --force                       # rebuild after enrichment/scoring changes
python -m walkability.graph.compact --city austin --csr         # → *.csr.pkl (app-preferred); also plain (runtime.pkl)

# Dev subsets (Boston test beds; see DEV_REGIONS in build.py)
python -m walkability.graph.build --dev --region nubian_roxbury
python -m walkability.graph.build --list-regions

# Inspect / diagnose (REPL)
python -c "from walkability.graph.build import inspect_inventory_fields; inspect_inventory_fields()"
python -c "from walkability.graph.build import diagnose_spatial_join; diagnose_spatial_join()"   # CRS/bbox/sjoin 0-match causes
# inspect_edges(n=, source={city_inventory|osm_tag|context|geometric}, highway=) — enriched edge samples

# Routing smoke test (dev subset)
python -m walkability.routing.router
```

```bash
# Verification / QA (run from repo root; notebooks/ import each other as siblings)
python notebooks/verify_system.py [--quick]                     # machine invariants (Boston)
python notebooks/verify_city.py --city austin [--update]        # per-city "loaded correctly" gate (see below)
python notebooks/verify_csr_parity.py --city austin             # CSR vs MultiDiGraph route parity
python notebooks/problem_routes.py --audit                      # problem-route regression vs baseline JSON
python notebooks/region_maps.py ; python notebooks/build_problem_route_maps.py   # HTML maps
```

Changing scoring logic, weights, or the category structure needs a `--force`
rebuild (to refresh the baked `walk_score`) **and** re-baselining
`notebooks/problem_routes_baseline.json` + `verify_city_baseline.<city>.json`.

## Architecture

### Enrichment pipeline (`walkability/graph/build.py`)

Attaches walkability scores to every OSM walk-graph edge. Inputs: the OSM graph
(covers `config.PLACES` = Boston + Brookline enclave + metro-hull towns
Cambridge/Somerville/Everett/Chelsea; env layers must cover the same extent or
border edges lose their safety factor) and the per-city sidewalk inventory.

Four tiers, first hit wins, recorded in `data_source`:
1. `city_inventory` — spatial join within 10 m → `surface_score` (condition),
   `surface_material_score` (material). Boston ~78%, Austin ~62%.
2. `highway=<type>` — OSM tag via `HIGHWAY_SCORES`.
3. `context:...` — bearing-weighted BFS over tagged neighbours (<1%).
4. `no_tag` — geometric fallback (rare).

Per-factor scores/confidences (`highway_score`, `surface_score`,
`surface_material_score`, …) are **never pre-combined** — routing re-weights them.
The pipeline bakes a composite `walk_score`/`walk_confidence` at the **default**
`FACTOR_WEIGHTS` as a routing fast path only.

### Key rules (don't regress)

- **Weights single source of truth:** `HIGHWAY_SCORES`/`SURFACE_SCORES` in
  `scoring/weights.py`; priorities derived via `sorted()`.
- **Tag resolution first:** osmnx collapses parallel ways, so `highway` may be a
  list. `osm/tag_resolver.py::resolve_edge_tags()` runs in `build_edge_schema()`;
  never pass raw edge data to `get_fallback()`.
- **Two surface fields, never merged:** `surface_score` = structural condition;
  `surface_material_score` = intrinsic material comfort (`None` for OT/unknown).
  `sidewalk_condition` stores the raw (aggregated) condition value for audit.
- **Both-sides aggregation** (`build.py::_aggregate_city_candidates`): a
  centerline edge matches both sidewalks → area/length-weighted mean for
  condition/width, conservative (lowest) material, divergence flag from per-`SIDE`
  representatives (`|Δ|>divergence_threshold` or different material) →
  `surface_confidence ×= DIVERGENCE_PENALTY`. Phantom polygons excluded; all-phantom
  edge falls to OSM tier.
- **Baked fast path:** `edge_walkability()` returns the baked value **only** when
  called with the literal `FACTOR_WEIGHTS` object (identity check); any other
  weights recompute. `--force` repopulates after changes.
- **Foot access = one source of truth** (`scoring/factors.py`):
  `EXCLUDED_FOOT_ACCESS` (`foot=no`→impassable), `RESTRICTED_FOOT_ACCESS`
  (private/customers/…→penalised), `FOOT_ACCESS_SCORE`. `routing/cost.py` imports
  these — add new tag values here, not to scattered checks.
- **GraphML coercion:** `ox.load_graphml` returns custom fields as strings (`"0.55"`,
  `"None"`). Use `_as_float`/`_as_str` (in `factors.py`), never raw casts.

### Two-level HDI scoring (`scoring/factors.py::edge_walkability`)

So one weak *dimension* can't be bought back by excellence in another:
1. **Within a category** — weighted arithmetic mean of present factors
   (substitutable); `FACTOR_WEIGHTS` = within-category weights, renormalised over
   present factors (missing → dropped, never zeroed).
2. **Across categories** — importance-weighted (`CATEGORY_WEIGHTS`) geometric mean,
   each floored to `[CATEGORY_FLOOR, 1]` (0.15; the zero-collapse valve, not an
   importance dial). Non-substitutable.

`CATEGORY_MAP`: safety=`environment`; comfort=`surface_quality`/`surface_material`/
`surface_width`; path=`road_type`/`foot_access`. A new factor must be added here to
count. `confidence` is a plain arithmetic mean (tiebreaker only).

**Comfort top-compression** (`compress_comfort`): comfort compressed above a knee
(0.80/0.50) because city condition/material run optimistic; applied at edge
`category_values` and route `dimension_scores`, not in `combine_categories` or on
`edge_category_scores`. `K=1.0` disables.

### Environment / safety factor (`graph/environment.py`)

`environment_score = sqrt(car_safety × perceived_safety)`.
- `car_safety = min(graded_ceiling, on_path, off_path) × industrial_penalty`.
  *on_path* from own `maxspeed` (crash-risk curve, class default when missing);
  *off_path* proximity to a nearby fast road (non-arterials only; tunneled roads
  dropped via `_drop_underground`); *graded_ceiling* rises above `CAR_SAFETY_CEIL`
  (0.85) with `road_separation` (all-roads layer); *industrial_penalty* from
  `landuse=industrial` proximity.
- `perceived_safety` (`eyes_score`) = noisy-OR of activity (POIs), enclosure
  (buildings; discounted by industrial exposure), openness (park/water/cemetery,
  cemetery discounted).
- landuse/roads/parking layers are **optional** — absent ⇒ their sub-signal is
  silently 0 (degraded, no error; `verify_city` mode C catches this).
- `environment` routes through `find_routes` but **has no UI slider yet**.

### Routing (query time, `walkability/routing/`)

`find_routes(G, orig, dest, alpha=, weights=, refine_sides=)` takes `(lat,lon)`
tuples, snaps internally, returns `list[RouteResult]` best-first. Dispatches a CSR
`RoutingGraph` to `csr_router.py` transparently.

1. `cost = length × (1 + α·(1 − walk_score))`. α=0 → shortest path. `foot=no`→None;
   restricted access ×`RESTRICTED_ACCESS_PENALTY` **except on terminal edges** (own
   origin/destination — the "zoo entrance" exemption).
2. **Clip** to an O–D-foci ellipse (`clip.py`); snap via vectorised numpy `argmin`
   restricted to the largest walkable component (`_routable_mask`) with
   `walk_bias=SNAP_WALK_BIAS_M` (prefer a sidewalk over an arterial centreline).
3. **A\* + penalty-method alternatives** (replaced Yen's; large long-route speedup,
   no sklearn). Confidence is a **re-rank tiebreaker**, never in the cost; skipped at
   α=0 (true length floor). Clip **auto-widens** → full-graph fallback so it can't
   drop the optimum.
4. **Route-level score** = same two-level HDI aggregate one level up: per-dimension
   length-weighted power mean (`ROUTE_DIMENSION_EXPONENTS`, currently neutral 0.5),
   then the `CATEGORY_WEIGHTS` geometric mean; floored values on
   `RouteResult.dimension_scores`. Query-time — re-baseline after tuning.

**Two-phase side-aware routing** (`router.find_routes`): where a street's two sides
are distinct footways, crossing is free in the cost, so a single pass can zigzag.
Phase 1 picks the corridor (walkability A*); phase 2 re-minimises **length** in a
narrow tube (`TUBE_WIDTH_M`=35 m, the one knob; excludes off-route `service` edges);
phase 3 keeps the shortened route only if `walk_score ≥ R1 − (REFINE_SCORE_TOL +
REFINE_CROSSING_CREDIT·crossings_saved)`. Query-time; `refine_sides=False` to A/B;
skipped at α=0. Re-baseline after tuning.

**Crossings are not modeled** — only `highway=crossing` nodes exist; no crossing
factor. `RouteResult.crossing_count` / `audit_route` counts are informational.

### City profiles (`walkability/graph/inventory.py`)

Everything municipality-specific lives in one frozen `CityProfile`; the whole
pipeline reads the profile it's passed. **Adding a city = adding a profile.**
`CITY_PROFILES = {boston, austin}`. Fields: `places`, I/O paths + `metric_crs`,
source columns (`condition_field`/`surface_field`/`width_field`/`date_field`/
`area_field`/`side_field`), scale adapters (`condition_to_score` ↔
`aggregate_condition`), `material_map`/`surface_score()`, `is_phantom`,
`divergence_threshold`, `maxspeed_defaults`, `eyes_rescue_cap`, `custom_filter`,
`env_layer_path()`.

- `area_field=None` ⇒ weight by geometry length (line inventory, Austin);
  `side_field=None` ⇒ divergence from top-2 by weight.
- **`custom_filter`** = osmnx Overpass filter (`None` = stock `network_type="walk"`).
  **Austin sets one keeping `highway=cycleway`** — its ped network (Butler Trail +
  Lady Bird Lake bridge decks) is cycleway-tagged and the stock filter severs it.
- **Invariant:** `BOSTON_PROFILE` reproduces pre-refactor Boston **byte-for-byte**.
- **Austin** rating is **inverted vs Boston SCI** (1=Excellent…5=Failed →
  `(5−r)/4`); `surface_score` from `rating_no_veg` (not veg-demoted `rating_overall`);
  `width_field=None` (73% logged 3 ft, untrustworthy).

Boston columns: `SCI` (0–100; ~430 corrupt negatives + `"NaN"` → `condition_to_score`
returns None ⇒ OSM tier), `SWK_WIDTH` (ft), `MATERIAL` (CC/BR/BIT/AC/GR/OT), `new_insp_d`
(1970 = epoch placeholder, 17%). **1970-date split** (`_build_canonical_schema`):
`inspected="yes"` → use data, lower confidence; `inspected` null → phantom, excluded
pre-aggregation.

### Verification tooling (`notebooks/`)

Dev/QA scripts, not packaged. Run from repo root.

- **`verify_system.py`** — machine invariants (schema/bounds, baked==recompute, cost
  model, route integrity, alpha floor, snapping, Boston data-source seam). What is/
  isn't machine-checkable: `Research/work_and_verification_outline.md`.
- **`verify_city.py --city <c>`** — the per-city "loaded correctly" gate. Static
  checks (failure modes A–K: inventory-join share, env coverage, optional-layer
  variance, condition-scale round-trip, material vocab, width handling, empty-factor
  saturation) + an auto-derived route-type battery (`route_types.py`, the global
  taxonomy: short/mid/long bands, data-seam straddle, over-water crossing, high-vs-
  arterial anchors, alpha floor, determinism, foot=no, terminal restricted, CSR
  parity) with **zero manual coords** — each selector derives O–D pairs from the
  graph/profile/OSM layers and self-skips when unsupported. Per-city drift baseline
  + auto calibration deck. First run needs `--update`. Taxonomy doc:
  `Research/route_type_taxonomy.md`. The one manual step: rate
  `<city>_calibration_survey.auto.html` into `ground_truth.<city>.csv`.
- **`verify_csr_parity.py --city <c>`** — CSR vs MultiDiGraph route parity;
  `source_fingerprint` refuses to compare across mismatched build snapshots.
- **`problem_routes.py`** — region-tagged `PROBLEM_ROUTES` regression vs
  `problem_routes_baseline.json` (crc32 path fingerprint); `--audit`/`--inspect`/`--map`.
- **`diagnostics.py`** — reusable, city-agnostic: `audit_route` (Tier-1 flags),
  `breakdown_route`, `safety_breakdown`, `score_heatmap`, `inspect_route_map`,
  `audit_scoring_coverage`. Every fn takes `G`.
- **`calibration_survey.py`** — hand-picked or `--auto` (battery-derived) survey deck;
  per-dimension bars + Street View. `ground_truth.csv` (+ per-city siblings) = the
  human-judgment side. `archive/` = superseded history, not imported.

### App — "Humanpath" Streamlit (`app/streamlit_app.py`)

`streamlit run app/streamlit_app.py`. Warm editorial design; fixed-width rail +
full-height map. Load-bearing behaviours (don't regress):

- **Map backend:** MapLibre GL vector map default (`HUMANPATH_MAP`, build-less
  Streamlit component in `app/components/maplibre_map/frontend/`, persistent via
  postMessage, no remount). Basemap = self-hosted Protomaps **PMTiles on R2**
  (Boston brand-tinted cut; re-cut over `config.PLACES` bbox when it widens; other
  cities fall back to OpenFreeMap positron). Libs **vendored** under `frontend/vendor/`.
  Graceful fallback to **st_folium** on client failure (`maplibre_failed` latch;
  `HUMANPATH_MAP_FORCE_FAIL=1` to test). Routes source needs **`tolerance:0, buffer:512`**
  (else mid-zoom segments vanish); `_route_lonlat` de-dupes coincident vertices.
- **Graph load once** `@st.cache_resource(max_entries=1)` (one city resident;
  switching evicts). `get_graph` prefers `*.csr.pkl` → `*.runtime.pkl` → GraphML,
  each local then from the `data-v1` GitHub Release. `_prewarm` builds snap caches.
- **City selector** (`_AREAS`, `city=True` = Boston `full` + `austin`) at top of rail;
  switching resets endpoints + clears routes. Component keyed by area. Adding a city
  = a `city=True` `_AREAS` entry (bbox/bias/landmarks + its `CITY_PROFILES` graph).
- **Geocoding:** address-only; **Photon primary, Nominatim timed fallback** (both
  hard-timeout — never hangs). No town appended; disambiguation via the area's `bbox`
  (part of the cache key) — **keep an area's bbox in sync with its graph**.
  `geocode_label` shows the matched place.
- **Config via `_cfg()`** — `os.environ` OR `st.secrets` then default (deploy needs no
  config).
- **Sliders:** α = slider/100·5; per-factor weights thread through; untouched passes
  the `FACTOR_WEIGHTS` object (baked path). **Deferred recompute** — map/cards render
  from committed `active_weights`, not live sliders.
- **CSS:** rail 446px `!important` scoped to the **open** state only
  (`[aria-expanded="true"]`); `@media (max-width:932px)` narrows + 16px inputs (iOS);
  collapse/expand chevrons kept.

### Status notes

- **RAM: Phase 1 (runtime pickle) + Phase 2 (CSR substrate) DONE.** Enriched GraphML
  (~2.7 GB/17 s) → runtime `MultiDiGraph` (0.45 GB/0.5 s) → CSR `RoutingGraph`
  (`graph/csr.py`, Austin ~78 MB/0.026 s). CSR router (`csr_router.py`) is faster
  than Nx and shares all pure logic via the `CsrEdge` flyweight; Nx path untouched.
  **Owed:** upload `*.csr.pkl` to the `data-v1` Release + live smoke test.
- **Owed for a polished Austin:** a brand-tinted Austin PMTiles cut (currently
  OpenFreeMap).
- **Removed factors** (`crossing_quality`, `poi_density`, `elevation_change`) — re-add
  a weight only with the edge field that feeds it; `elevation_change` waits for a hilly
  city. Ground-truth candidates: turn-count minimisation, step-free toggle, greenery.

### Dev environment

**No `scikit-learn` or `scipy`.** Don't call `ox.nearest_nodes()` or
`scipy.spatial.cKDTree`; use `clip.snap_to_node` (vectorised numpy `argmin` over
cached coords). `venv/bin/python` (the IDE preview sandbox can't read `venv/`).
