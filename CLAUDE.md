# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install package in editable mode (required before running anything)
pip install -e .

# Download the base OSM walk graph for Boston (run once)
python walkability/graph/download.py

# Download the OSM feature inputs for the environment (safety) factor: arterials,
# buildings, POIs, open space, landuse=industrial, and all roads → data/osm/*.gpkg.
# Missing core files disable the factor; missing landuse/roads just disable their
# (optional) sub-signal.
python walkability/graph/download_environment.py

# Build the full enriched graph (skips rebuild if output already exists)
python -m walkability.graph.build

# Force a full rebuild after changing enrichment logic
python -m walkability.graph.build --force

# Convert the enriched GraphML(s) → slim runtime pickles the app loads
# (≈0.5 s / ≈0.45 GB vs ≈17 s / ≈2.7 GB; a post-process, no --force rebuild).
python -m walkability.graph.compact            # full graph
python -m walkability.graph.compact --all      # full + every dev region
python -m walkability.graph.compact --dev      # beacon_hill dev subset

# Build a dev subset (default: ~500 m around Beacon Hill). Other named regions
# exist for less-walkable test beds — see DEV_REGIONS in build.py.
python -m walkability.graph.build --dev                            # beacon_hill
python -m walkability.graph.build --dev --region nubian_roxbury     # a less-walkable region
python -m walkability.graph.build --list-regions                   # list all regions
python -m walkability.graph.build --dev --force                    # rebuild subset

# Inspect sidewalk inventory columns and sample values
python -c "from walkability.graph.build import inspect_inventory_fields; inspect_inventory_fields()"

# Diagnose spatial join failures (CRS, bounding box, column naming)
python -c "from walkability.graph.build import diagnose_spatial_join; diagnose_spatial_join()"

# Inspect enriched edges (from notebook or REPL)
from walkability.graph.build import inspect_edges
inspect_edges()                                          # 5 random edges, all tiers
inspect_edges(n=10, source="city_inventory", highway="footway")
inspect_edges(source="osm_tag")                         # OSM-tagged, no city match
inspect_edges(source="geometric")                       # true no-tag fallbacks only
```

```bash
# Smoke-test routing: snaps two far-apart dev nodes, prints top routes at
# several alpha values (alpha=0 ≈ shortest path; higher alpha trades length
# for walkability). Runs against the cached dev subset.
python -m walkability.routing.router
```

```bash
# Automated invariant checks (the closest thing to a test suite). Exit code is
# non-zero on failure; --quick skips the ~10 s full-graph clip check.
python notebooks/verify_system.py

# Problem-route regression harness: re-runs tracked routes vs a JSON baseline,
# grouped by region. --update writes the baseline; --audit/--inspect/--map add
# the three diagnostic tiers; --region / --dev scope to one region.
python notebooks/problem_routes.py --audit
python notebooks/problem_routes.py --region nubian_roxbury --update

# Per-region walk_score heatmaps (HTML); per-route inspector maps + index.
python notebooks/region_maps.py
python notebooks/build_problem_route_maps.py
```

The automated invariant harness is `notebooks/verify_system.py` (schema, score
bounds, clip-vs-unclipped, etc. — see `Research/work_and_verification_outline.md`
for what is and isn't machine-checkable). Beyond it, the `inspect_*` functions in
`build.py` and the diagnostics in `notebooks/` (see below) are the manual
verification harness — prefer extending those over ad-hoc scripts.

## Architecture

### Data pipeline

The core of the project is an enrichment pipeline in `walkability/graph/build.py` that attaches walkability scores to every edge of the OSM walk graph. Two input datasets feed it:

- `data/osm/boston_walk.graphml` — OSM walk graph downloaded via osmnx. Despite the filename (kept to avoid path churn), it covers the union of `config.PLACES`: Boston + Brookline (enclave — without it Allston/Brighton is a disconnected component) + the metro-hull towns Cambridge/Somerville/Everett/Chelsea (2026-07-04 — without them every Charlestown trip is forced over the single North Washington St bridge and East Boston ↔ Chelsea is unreachable). The environment layers (`download_environment.py`) cover the same `PLACES` extent — they must widen together or border edges silently lose their safety factor (guarded by `verify_system.py::check_data_source_seam`).
- `data/boston/sidewalk_inventory/` — Boston DPW shapefile with per-sidewalk condition, width, material, and survey date. **Boston-only**: every non-Boston municipality has no city inventory, so its edges fall through to the OSM tier (comfort drops out of the geometric mean, it is never zeroed) — verified uniform with Boston's own OSM-tier edges (city-agnosticism).

For each edge the pipeline runs four tiers in order, stopping at the first hit:

1. **City inventory match** (`data_source = "city_inventory"`) — spatial join within 10 m. Provides `surface_score` (SCI/100, structural condition) and `surface_material_score` (from MATERIAL code). ~78% of edges. **Both sidewalks are aggregated, not coin-flipped** — see "Both-sides surface aggregation" below.
2. **OSM tag** (`data_source = "highway=<type>"`) — resolved highway tag mapped through `HIGHWAY_SCORES`. ~18% of edges.
3. **Context inference** (`data_source = "context:..."`) — bearing-weighted BFS over neighbouring tagged edges. <1% of edges.
4. **Geometric fallback** (`data_source = "no_tag"`) — edge length heuristic. Rare.

Every tier produces a `FallbackResult` (defined in `walkability/osm/fallback.py`) with separate `highway_score` and `highway_confidence` fields. **The per-factor scores and confidences are never pre-combined into one another** — highway/surface/material stay independent so routing can re-weight them. The pipeline *does* bake a composite `walk_score`/`walk_confidence` onto each edge (see below), but only at the **default** `FACTOR_WEIGHTS`, purely as a routing fast path; non-default weights recompute from the per-factor fields at query time.

### Key design decisions

**Single source of truth for scores** (`walkability/scoring/weights.py`): `HIGHWAY_SCORES` and `SURFACE_SCORES` are the only place to change numerical values. `HIGHWAY_PRIORITY` and `SURFACE_PRIORITY` are derived automatically via `sorted()`, so tag_resolver.py picks up changes without edits.

**OSMnx multi-edge normalisation**: osmnx collapses parallel ways, so `highway` on an edge can be a list like `["footway", "residential"]`. `walkability/osm/tag_resolver.py` must be called before any scoring or fallback logic. `resolve_edge_tags()` is called in `build_edge_schema()` and the resolved dict is passed to `get_fallback()` — **never pass raw edge data directly to fallback**.

**`data_source` field** encodes the enrichment tier for every edge:
- `"city_inventory"` — city shapefile was the source
- `"highway=footway"` (etc.) — OSM tag, no city match
- `"context:dominant=..."` — context inference
- `"no_tag"` — geometric fallback

**Two surface score fields, not one:**
- `surface_score` — structural condition: SCI/100 from city data, or OSM surface tag score as fallback. Answers "how degraded is this surface?"
- `surface_material_score` — intrinsic comfort: MATERIAL code → `SURFACE_SCORES` from city data (e.g. BR → paving_stones → 0.70), or the same OSM surface score when no city material is available, or `None` when the material code is unrecognised (OT/other). Answers "how comfortable is this surface type?" These are kept separate so `factors.py` can weight structural condition and material comfort independently at routing time. Never pre-combine them with `min()` or blend — that conflates two different dimensions.

**`sidewalk_condition`** stores the raw (now aggregated) SCI value (0–100) for auditing. The normalized [0, 1] score used in scoring is `surface_score`.

**Both-sides surface aggregation** (`build.py::_aggregate_city_candidates`): a street's two sidewalks both fall within the 10 m match buffer of its single centerline edge, so the join takes an **area-weighted mean** over all valid candidates for `surface_score`/width and the **conservative (lowest-comfort) material**, and **flags divergence** — where the two sides genuinely disagree (one representative per inventory `SIDE`, largest-area; `|ΔSCI| > DIVERGENCE_THRESHOLD_SCI` or different material). Divergent edges have their `surface_confidence` multiplied by `DIVERGENCE_PENALTY` (0.85). **Divergence is measured from per-`SIDE` representatives, not raw max−min over every matched polygon** — a tiny corner fragment of a perpendicular street is within the buffer too and would otherwise over-flag. Phantom polygons (`new_insp_d` pre-2000 **and** `inspected` null/NaN — never field-surveyed) are excluded from the aggregate; an edge whose candidates are all phantom/invalid falls through to the OSM tier. Changing the aggregation or its constants requires a `--force` rebuild + re-baseline.

**Baked `walk_score` / `walk_confidence` fast path**: `_build_canonical_schema` calls `scoring.factors.edge_walkability()` with the *default* `FACTOR_WEIGHTS` and writes the result onto each edge. `edge_walkability()` reads this baked value back **only when called with the literal `FACTOR_WEIGHTS` object** (identity check); any other weights dict (e.g. UI sliders) forces a full recompute. **A `--force` rebuild is required to (re)populate the baked field** after changing scoring logic or weights; graphs built before the bake simply recompute and still work.

**Foot-access classification is one source of truth** in `scoring/factors.py`: `EXCLUDED_FOOT_ACCESS` (`foot=no` → impassable), `RESTRICTED_FOOT_ACCESS` (`private`/`customers`/`permit`/`residents`/… → walkable but penalised), and `FOOT_ACCESS_SCORE` (soft signal). `routing/cost.py` imports these sets so the hard routing rule and the soft score never drift apart. Boston OSM uses a wide access vocabulary (`customers` alone is ~1,100 edges) — add new values to these sets, not to scattered string checks.

**Two-level (HDI-style) scoring** (`scoring/factors.py::edge_walkability`, structure in `scoring/weights.py`): `walk_score` is built in two levels so a failure in one *dimension* of walking can't be bought back by excellence in another (a pristine surface must not rescue a walk along a highway):
1. **Within a category** — a weighted **arithmetic** mean of the present factors (factors there are *substitutable*; e.g. good condition offsets so-so material). `FACTOR_WEIGHTS` are these **within-category** relative weights, renormalised over whatever factors are present.
2. **Across categories** — an importance-weighted (`CATEGORY_WEIGHTS`) **geometric** mean of the category values, each floored to `[CATEGORY_FLOOR, 1]` (default 0.15). Categories are *non-substitutable*; one weak category dominates. Current ratios: safety(1.15) ≥ path(1.0) > comfort(0.7). The floor stops a single zero category (motorway `road_type`, `foot=no`, on-arterial `environment`) from annihilating all discrimination — exactly as the Human Development Index bounds each dimension above zero. `CATEGORY_WEIGHTS` express *importance* (symmetric); `CATEGORY_FLOOR` is only the zero-collapse valve, **not** an importance dial.

`CATEGORY_MAP` (`scoring/weights.py`) assigns each factor to one of three dimensions: **safety** (`environment`), **comfort** (`surface_quality`, `surface_material`, `surface_width`), **path** (`road_type`, `foot_access`). A new factor must be added to `CATEGORY_MAP` to count. A zero (UI-slider) weight drops the factor entirely; an all-empty category drops out of the geometric mean (never imputed). `confidence` stays a plain weight-weighted arithmetic mean (tiebreaker only — not geometric). **Changing the category structure or any weight requires a `--force` rebuild** to refresh the baked `walk_score`, and re-baselining `notebooks/problem_routes_baseline.json`.

**Comfort top-compression** (`scoring/factors.py::compress_comfort`): the **comfort** dimension value is compressed above a knee — `comfort' = COMFORT_COMPRESS_KNEE + COMFORT_COMPRESS_K·(comfort − KNEE)` (0.80 / 0.50) — before `combine_categories`, since city SCI/material run optimistic (wear, age, narrowness uncaptured) and comfort otherwise saturates near 1.0 on ordinary "fine" streets, biasing the whole score up. Applied at the two dimension-finalization points (edge `category_values` and route `dimension_scores`) — **not** in `combine_categories` itself (kept pure) and **not** on per-edge `edge_category_scores` (so the route power-mean is over raw comfort and the trim lands once on the aggregate). `K=1.0` disables it. Shifts the baked `walk_score` → needs a `--force` rebuild + re-baseline.

**Environment (safety) factor** (`graph/environment.py`) — lives in the **safety** category, not a flat weight, because fixing over-scored highway segments needed a new edge feature, not just a weight tweak: `environment_score = sqrt(car_safety × perceived_safety)` (geometric mean — both must be reasonably good).
- `car_safety = min(graded_ceiling, on_path, off_path) × industrial_penalty` (weakest link, no double-counting):
  - *on_path* — the road you walk **along**, scored from its `maxspeed` via a crash-risk curve (`MAXSPEED_SAFETY_ANCHORS`; footway → 1.0, 25 mph → 0.90, 35 → 0.45, missing → class default `DEFAULT_MAXSPEED_MPH`).
  - *off_path* — proximity to a nearby fast road you're **not** on (footways/quiet streets beside an arterial), computed only for non-arterial edges, using the arterial's actual `maxspeed` (`_arterial_scores`) with class-based reach (`ARTERIAL_REACH_M`). Underground (tunneled) roads are excluded (`environment.py::_drop_underground`, driven by `tunnel`/`layer` tags) so a footway above a buried road isn't scored as if the road were at grade.
  - *graded_ceiling* = `CAR_SAFETY_CEIL + (1 − CAR_SAFETY_CEIL)·road_separation`, where `road_separation = min(1, dist_to_nearest_road / SEPARATION_REACH_M)` from the all-roads layer (`boston_roads.gpkg`) — a road-adjacent path tops at `CAR_SAFETY_CEIL` (0.85) but a genuinely separated path (greenway, HarborWalk, pedestrian bridge) can climb toward 1.0.
  - *industrial_penalty* — `industrial_exposure` = proximity to a `landuse=industrial` polygon (`boston_landuse.gpkg`, `INDUSTRIAL_REACH_M`); `car_safety ×= (1 − INDUSTRIAL_CAR_PENALTY·exposure)`, since `maxspeed` alone misses truck/industrial-corridor danger.
  - The landuse/roads layers are optional — absent ⇒ exposure/separation 0 ⇒ car_safety falls back to the on/off-path-only model.
- `perceived_safety` (stored as `eyes_score`) = a **noisy-OR** (`1 − ∏(1−s)`) of three substitutable signals — safe if ANY is strong: *activity* (foot-traffic POIs, type-weighted via `POI_NOISE_AMENITIES` so benches/parking count 0), *enclosure* (buildings facing the street; dropped for alley/service edges, discounted by `industrial_exposure` since a warehouse provides no residential "eyes"), *openness* (adjacency to large park/water/**cemetery**, `OPENSPACE_MIN_AREA_M2`/`OPENNESS_REACH_M`; cemeteries give real sightlines/traffic-separation but read less pleasant than a park, so their openness is discounted by `CEMETERY_OPENNESS_FACTOR` (0.6) and the stronger park-vs-discounted-cemetery signal wins per edge — grounded on Grove St beside Walnut Hills Cemetery).
- `environment` is a live `FACTOR_WEIGHTS` factor routed through `find_routes`/`edge_cost` already, but **has no UI weight slider yet** in `app/streamlit_app.py` — add one mirroring the existing per-factor sliders when picked up.

### Routing and scoring (query time)

Composite scoring and routing live in `walkability/scoring/factors.py` and `walkability/routing/`. The flow for one query (`routing.router.find_routes(G, orig, dest, alpha=...)`):

1. **Composite score** (`factors.edge_walkability`) — the two-level HDI-style aggregate described above. Missing factors drop and weights renormalise so a missing score never penalises an edge. Returns `(walk_score, confidence)`, both [0,1]. This is also the boundary that **coerces GraphML strings** (`ox.load_graphml` returns custom fields as `"0.55"`, and `None` as a real `None`, an absent key, *or* the literal `"None"`) — use `_as_float`/`_as_str` rather than casting elsewhere.
2. **Cost** (`routing/cost.py`) — `cost = length × (1 + α·(1 − walk_score))`. `α` is the single distance/walkability knob (0 = shortest path; higher = detour toward walkable edges). Crossings are not in the cost — handled by the phase-2 tube refinement (below). `foot=no` returns `None` (edge dropped); restricted access multiplies by `RESTRICTED_ACCESS_PENALTY` **except on terminal edges** — `edge_cost(is_terminal=True)` skips the penalty for an edge leaving the origin or entering the destination (the "zoo entrance" case: you'd legitimately use a customers-only path at your own endpoint). `_routable_digraph` marks terminal edges via `u == o_node`/`v == d_node`. Both `edge_cost` and the projection take an optional `weights` dict (defaults to the `FACTOR_WEIGHTS` object for the baked fast path) that `find_routes` threads through from the UI sliders.
3. **Spatial clip** (`routing/clip.py`) — clips the graph to an **ellipse with O and D as foci** before routing (`dist(O,n)+dist(n,D) ≤ budget`), so candidate search runs on a small local subgraph instead of all ~52k nodes. Node coords are cached on `G.graph`; snapping is a vectorised numpy `argmin`. `find_routes` snaps with `snap_to_node(..., routable_only=True)`, restricted to the **largest walkable connected component** (`clip._routable_mask`) — the geometrically nearest node to an address is sometimes a `foot=no` stub or disconnected footway fragment, which would otherwise silently yield zero routes. `find_routes` also passes `walk_bias=SNAP_WALK_BIAS_M`: the chosen node minimises `dist_m + (1 − highway_score)·bias`, so an address prefers a nearby sidewalk over an arterial centreline a few metres closer. `walk_bias=0` (default) stays exact-nearest.
4. **A\* + penalty-method alternatives** (`_collect_candidates`) — the clipped `MultiDiGraph` is projected to a simple `DiGraph` (cheapest parallel edge per `(u,v)`, `foot=no` excluded). The best route is found with **A\*** (`nx.astar_path`, haversine heuristic — admissible/consistent since `cost ≥ length ≥ straight-line`). Alternatives come from the **penalty method**: a per-edge multiplier (`ALT_PENALTY`, passed via A*'s `weight` callback, never mutating the graph) inflates a found route's edges so the next A* run diverges; kept only if within `ALT_MAX_STRETCH` of optimum. This replaced Yen's `nx.shortest_simple_paths` for a large long-route speedup (see "Routing scaling" below).
5. **Confidence is a tiebreaker, not a cost term** — kept entirely out of the edge cost. After A* yields candidates, a re-rank adds a confidence bonus that decays to zero outside a small `walk_score` window (`tie_epsilon`), so it only reorders near-equal routes. If every candidate is below a confidence floor, more A* runs are pulled (expansion). **At `alpha=0` the walk re-rank is skipped** — pure-shortest-path mode keeps cost (length) order so it's a true length floor.
6. **Clip auto-widens** — if the best route hugs the ellipse boundary the clip widens (`WIDEN_FACTOR`, up to `MAX_WIDENS`) and finally falls back to the full graph, so clipping can never silently drop the true optimum.
7. **Route-level walk_score is a two-level HDI aggregate, one level up** (`_build_route` → `_aggregate_route_dimensions` + `factors.combine_categories`) — mirrors the edge two-level structure across distance. **Per dimension** (safety/comfort/path from `edge_category_scores`), the route is a length-weighted **power mean** over the edges where it's present, exponent from `ROUTE_DIMENSION_EXPONENTS` (lower → more worst-segment-sensitive), floored to `[CATEGORY_FLOOR, 1]`. **Across dimensions**, those route-level values combine with the same `CATEGORY_WEIGHTS` geometric mean as a single edge — this ordering matters: a bad safety (or path) block can't be bought back by good comfort on the *same edge*. Floored per-dimension values are exposed on `RouteResult.dimension_scores`. `confidence` stays a plain length-weighted mean (tiebreaker only). Terminal restricted-access edges have their `foot_access` dropped before aggregation (matching the cost exemption). Query-time only — no `--force` rebuild needed to change `ROUTE_DIMENSION_EXPONENTS`, but re-baseline `notebooks/problem_routes_baseline.json`. **Currently at neutral exponents** (all = `ROUTE_SCORE_EXPONENT` = 0.5) — lowering the safety exponent to punish worst segments harder was tried and reverted; it barely moved the flagged routes, since the real issue was the safety *values* on those edges, not this route-level weighting.

Performance: clipping makes local trips fast (≈0.1s at 700 m on the full graph) but barely helps long cross-city trips (large ellipse). Load the graph **once** (e.g. Streamlit `@st.cache_resource`) — a full GraphML load is ~10s (use the compact runtime pickle instead where possible, see below).

**Routing scaling.** A\* + penalty-method alternatives (step 4 above) replaced Yen's `nx.shortest_simple_paths`, which had dominated long-route latency, for a large speedup with no new dependency (NetworkX `astar_path`; `scikit-learn` is **not** required, unlike `ox.nearest_nodes`) — verified exact (A* cost == Dijkstra's). Remaining scaling ideas, lowest priority first: caching the projected DiGraph across clip-widen retries/expansion pulls; and, as a long-term "real" answer if per-query dynamic weights ever need sub-second citywide routing at scale, **Customizable Contraction Hierarchies** (metric-independent topology preprocessing + fast per-query customization) — a plain static Contraction Hierarchy won't work since our `alpha`/weight sliders change the cost metric per query. Large effort; only pursue if simpler fixes prove insufficient.

### Dev subsets and regions

`build.py` defines `DEV_REGIONS` — named neighbourhood subsets for fast iteration and for exercising the diagnostics on areas with different walkability. `beacon_hill` is the walkable reference (and keeps the legacy filename `boston_walk_dev.graphml` via `dev_region_path`); `charlestown_sullivan`, `newmarket_massave`, `nubian_roxbury` were chosen *and verified* to be less walkable so the audit flags actually fire; `brookline_seam` and `metro_hull_seam` sit on the Boston↔no-city-data boundaries (Brookline; Charlestown↔East Cambridge) to exercise the data-source seam. `build_dev_subset(region=...)` writes each to its own `boston_walk_dev_<region>.graphml`. To add a region, add a `DEV_REGIONS` entry (lat/lon/radius/note) — don't hardcode centres elsewhere.

### Diagnostics & verification tooling (`notebooks/`)

These are dev/QA scripts, not part of the package. They import each other as siblings, so **run them from the repo root** (`python notebooks/<file>.py`), which puts `notebooks/` on `sys.path`.

- `diagnostics.py` — the reusable toolkit. Three-tier inspection: `audit_route` (Tier 1, statistical flags — crossings counted from `highway=crossing` **nodes**, not edges, since our edges carry no crossing tag), `inspect_route_map` / `score_heatmap` / `routes_over_heatmap` (Tier 2, folium HTML), and street-imagery URL helpers (Tier 3). Also `breakdown_route`, `edge_vs_detour`, `audit_scoring_coverage`. Every function takes a graph `G`, so it works on the full graph or any region subset.
- `problem_routes.py` — region-tagged `PROBLEM_ROUTES` registry + regression harness (`measure`/`classify` vs `problem_routes_baseline.json`, crc32 path fingerprint). Routes run on the **full** graph by default (it covers every region); `region` is a grouping label.
- `verify_system.py` — automated invariants (see Commands).
- `region_maps.py` / `build_problem_route_maps.py` — batch HTML map generators.
- `test_route.py` — single edit-the-top-and-run route tester.
- `calibration_survey.py` — generates `calibration_survey.html`: a hand-picked set of routes across Boston's walkability spectrum, each as a zoomed map with numbered per-segment colouring, the per-DIMENSION breakdown (safety/comfort/path via `edge_category_scores`), Street View links, and the calibration questions. The tool for collecting `subj_walkability` ground truth to tune `CATEGORY_WEIGHTS` / `CATEGORY_FLOOR` / the `environment` constants.
- `ground_truth.csv` (+ `.README.md`) — region-tagged manual observation log; the human-judgment side of verification (subjective walkability, real surface/condition, route quality). `Research/work_and_verification_outline.md` explains the invariants-vs-validity boundary (what can/can't be automated).

`archive/` holds one-off exploration scripts and closed daily work-journals from the project's early setup, superseded by the structured tooling above. Nothing there is imported or referenced by the live code — kept only for history, mirroring the original `notebooks/`/`Research/` subpaths.

**Crossings are not modeled directly.** They exist only as `highway=crossing` nodes and there is no crossing factor in `FACTOR_WEIGHTS` (reported `walk_score` is unaffected by them). `audit_route`'s crossing count and `RouteResult.crossing_count` are informational. Crossing-minimisation is structural — see "Two-phase side-aware routing" below.

**Two-phase side-aware routing (`router.find_routes`).** Where a street's two sides are distinct footways (most of footway-dense Boston), crossing between them is free in the cost, so a single-pass optimiser can zigzag across streets picking the "wrong side" — a side-switch costs nothing but *buys* `walk_score`, so a soft crossing penalty would compete with walkability and be unreliable. The fix decomposes the problem (the graph has no side labels or street association, so sides can't be chosen explicitly):
- **Phase 1 — corridor:** the existing walkability-aware A* + alternatives picks *which streets* (each candidate `R1`), ranked by `_rank_score` first.
- **Phase 2 — sides/crossings:** for each `R1` (in rank order), re-minimise **length** inside a narrow **tube** around it (`clip.clip_to_route`, half-width `TUBE_WIDTH_M`=35 m; `_collect_candidates` at `REFINE_ALPHA`=0) — once the corridor is fixed, minimising length minimises gratuitous crossings, since a zigzag is strictly longer. The tube is wide enough for both sidewalks but narrower than a block. Phase 2 **excludes `service` edges** not already on `R1`, so pure length-min can't take a parking-lot/back-alley shortcut.
- **Phase 3 — crossing-aware guard:** keep the shortened `R2` only if `R2.walk_score ≥ R1.walk_score − (REFINE_SCORE_TOL + REFINE_CROSSING_CREDIT·crossings_saved)`; else revert to `R1` (the zigzag was avoiding a genuinely bad block). The **crossing credit** exists because crossings are free in the cost, so `R1`'s walk_score can be partly inflated by weaving between parallel paths to harvest the best-scoring segment at each step — a fewer-crossing `R2` that scores a little lower is often the genuinely better route. The credit only **widens** the allowance when `R2` removes crossings, so it can never admit a route that doesn't cut crossings. `R2` is length-≤ `R1` by construction, so refinement never lengthens a route.

All **query-time, no rebuild** (no new edge fields). `find_routes(..., refine_sides=False)` disables phase 2 for A/B comparison. Skipped at `alpha=0` (the corridor is already the shortest path, preserving the length floor). **The single tuning knob is `TUBE_WIDTH_M`** (too narrow → can't reach the needed side on a wide street; too wide → phase 2 can jump to a shorter parallel street). Tuning it or `REFINE_*` is query-time but **re-baseline `notebooks/problem_routes_baseline.json`**.

### Boston sidewalk inventory field mapping

The shapefile columns do not match generic names — use these constants in `build.py`:

| Constant | Column | Notes |
|---|---|---|
| `SWK_CONDITION_FIELD` | `SCI` | Sidewalk Condition Index, numeric string 0–100. **Partly corrupt**: ~430 negatives (down to ~−68000, a city calc error) + the string `"NaN"`. `_condition_to_score` returns `None` for anything outside 0–100, so those edges fall through to the OSM tier instead of mis-scoring `surface_score=0.0`. |
| `SWK_WIDTH_FIELD` | `SWK_WIDTH` | Width in feet |
| `SWK_SURFACE_FIELD` | `MATERIAL` | Codes: `CC`=concrete, `BR`=brick, `BIT`/`AC`=asphalt, `GR`=granite, `OT`=other (scores as None) |
| `SWK_DATE_FIELD` | `new_insp_d` | Most recent re-inspection date; 1970-01-01 is a Unix-epoch placeholder (17% of rows, concentrated in West Roxbury and Downtown — a data-entry batch issue, not a spatial quality signal) |

**1970-date two-level treatment** (in `_build_canonical_schema`): rows with a pre-2000 date are split by the `inspected` column before confidence is assigned:
- `inspected = "yes"` → survey happened, date was mis-logged. Use SCI/MATERIAL; apply `CONF_CITY_DATE_MISSING = CONF_CITY_OLDER × 0.85` (≈ 0.72). Do **not** treat these as lower-quality edges — the West Roxbury concentration would introduce a spurious spatial confidence gradient.
- `inspected = null`/NaN → sidewalk polygon exists but was never field-surveyed. Such **phantom candidates are excluded inside `_aggregate_city_candidates`** before the both-sides mean is taken; an edge whose candidates are *all* phantom yields no aggregate and falls through to the OSM-tag tier.

### Implemented UI — "Humanpath" Streamlit app

`app/streamlit_app.py` — run `streamlit run app/streamlit_app.py`. A warm editorial
design (parchment + terracotta; Spectral / Public Sans / IBM Plex Mono via a Google-
Fonts `@import`) with a fixed-width left control rail and a full-height map. Branding:
the "Humanpath" wordmark + a two-dot/connector logo (inline SVG in the rail header;
`app/humanpath_icon.png`, generated with PIL, is the favicon). The design source/
mockup is `app/Footpath Atlas.html`; theme defaults live in `.streamlit/config.toml`
and the rest is injected CSS. `.claude/launch.json` has a `walkability-ui` config (the
in-IDE preview sandbox can't read `venv/`, so launch from a normal shell). Key
behaviours, several of them hard-won — **don't regress**:

- **Map backend: MapLibre GL vector map is the default; st_folium is the fallback.**
  `_MAP_BACKEND` (`HUMANPATH_MAP`, default `maplibre`) selects a custom **build-less
  Streamlit component** (`app/components/maplibre_map/frontend/`, served via
  `declare_component(path=)`) that renders routes on GPU vector tiles with smooth
  zoom/pan + eased `fitBounds`. The component is **persistent** (hand-rolled
  postMessage handshake, no remount across reruns); Python passes a route GeoJSON
  (`_route_geojson` → roles alt/halo/focused/segment + O/D points) + an intent-only
  camera token (committed O/D + recenter nonce, so manual pan/focus-switch/slider
  edits don't reframe). **Basemap** = self-hosted **Protomaps PMTiles** metro
  extract on **Cloudflare R2** (`_PMTILES_BOSTON_URL`, default
  `boston_metro.pmtiles` — cut 2026-07-04 over the full `config.PLACES` hull;
  re-cut with `pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles
  data/pmtiles/boston_metro.pmtiles --bbox=...` whenever `PLACES` widens, then
  upload to R2; `pmtiles://` + HTTP range + open CORS), styled with
  `@protomaps/basemaps@5` brand-tinted in `main.js` `resolveStyle`. Libs are **vendored** under `frontend/vendor/` (maplibre-gl,
  pmtiles, @protomaps/basemaps — no runtime CDN dep except glyphs/sprites on
  protomaps.github.io). **Graceful fallback:** on a fatal client failure (no WebGL/
  lib load/init throw) the component reports via `setComponentValue`; Python
  latches `st.session_state.maplibre_failed` and re-renders the st_folium map with
  a notice (`HUMANPATH_MAP_FORCE_FAIL=1` injects that failure to test the path).
  **Config reads via `_cfg()`** — `os.environ` (HF Spaces) OR `st.secrets`
  (Streamlit Community Cloud, which does NOT expose secrets as env vars), then the
  code default — so a deploy needs **no** config at all. To re-vendor a lib,
  re-download it into `frontend/vendor/` and bump the pin in this note + index.html.
  The `routes` GeoJSON source is declared with **`tolerance: 0, buffer: 512`**
  (`main.js::addRouteLayers`) — **don't drop these**: routes have long straight
  spans with no intermediate vertices (bridge crossings, Esplanade footways), and
  geojson-vt's default per-zoom simplification + narrow tile buffer would drop a
  whole stretch (line + shared halo) in a mid-zoom band (verified: a Beacon St
  segment vanished at z14–14.5, restored by these opts). `_route_lonlat`
  (`streamlit_app.py`) also de-dupes the coincident vertex where consecutive edges
  meet, for the same reason (coincident points confuse GL clipping).
- **Graph load once + download-on-startup** (`@st.cache_resource`, keyed by path).
  The graph files are too big for the repo, so `get_graph` fetches any missing file
  from a **GitHub Release** (`_GRAPH_RELEASE`, tag `data-v1`) via streaming `requests`
  to a `.part` temp then atomic rename. `get_graph` **prefers the slim
  `*.runtime.pkl`** sibling (see "Graph RAM footprint" below), downloading it from
  the release rather than the full GraphML, and only falls back to the GraphML if
  the pickle is absent both locally and in the release. The region selector
  (`key="region_select"`) sits at the bottom of the rail; its value is read at the
  **top** of the next run via the widget key so the graph can load before the
  widget renders (default `full`).
- **Address-only input.** Click-on-map and lat/lon entry were **removed** (they
  fought st_folium reruns and added clutter). Origin/destination are addresses,
  geocoded by `geocode()`, Boston-biased and `@st.cache_data`-wrapped. **Primary is
  Photon** (komoot — OSM-based, no key, tolerant of server/cloud use); **Nominatim is
  a timed fallback**. Nominatim's public server rate-limits/blocks shared cloud IPs
  (Streamlit Community Cloud), which used to **hang** the deployed app on "Reading the
  streets…" via a no-timeout `osmnx.geocode` fallback — that fallback was removed and
  every call now has a hard timeout, so geocoding can never spin forever (worst case →
  "couldn't find that address").
- **`alpha` + per-factor weight sliders.** The 0–100 "how you'll walk" slider maps
  to `alpha = slider/100·5`. Weights thread through `find_routes` → `edge_cost`/
  `_build_route` → `edge_walkability`; untouched, the `FACTOR_WEIGHTS` object itself
  is passed to keep the baked fast path.
- **Distance units.** A `mi`/`km` segmented control (`key="units"`) defaults to
  **miles** (US); `dist_str(m, unit)` formats everything (route distance, weakest-
  stretch offset, per-segment lengths), using feet under 0.1 mi.
- **Deferred recompute.** The map and route cards render from the **committed**
  params (`st.session_state.active_weights`, frozen at the last search), NOT the
  live sliders — so moving a fine-tune slider only shows a "changed" nudge and flips
  the button to "Update routes"; nothing redraws until it's pressed.
- **Route cards + Details.** Each candidate is a card (walk score /100, bar,
  distance, walk-time). A visible **"Show on map"** button (an `on_click` callback
  setting `st.session_state.focus`, **no `st.rerun`**) emphasises that route; all
  routes are drawn at search time so switching focus is a single rerun with the view
  preserved. By default the focused route is a **single smooth line** (halo + one
  colour from its overall walk_score), like the faint alternatives. A per-route
  **Details** expander reveals confidence, the **weakest stretch** (its distance
  from the start, via `route_details` returning the cumulative offset to the
  lowest-scoring block), and a **"Show N segments"** toggle: it lists each block's
  score **and** switches the focused route on the map to per-block colouring
  (`seg_{focus}` flag, read by `build_map`).
- **st_folium camera — persistent base + dynamic layers (no remount).** *(This is
  now the **fallback** map — see "Map backend (B2)" above; it renders when
  `HUMANPATH_MAP=folium` or after a MapLibre client failure. The MapLibre component
  reimplements this same camera/route behaviour on vector tiles.)* The map
  is split three ways so route loads feel natural instead of reloading the whole
  iframe: (1) `build_base_map` renders the **tiles-only** map **once** with a
  *constant* centre/zoom and a **stable `key="route_map"`** — st_folium hashes the
  generated Leaflet JS (`generate_js_hash` strips folium's random `_<hash>` var
  suffixes), so a stable JS → stable hash → the iframe is **never remounted** (no
  white flash, no tile reload). (2) Routes + O/D markers are a **`FeatureGroup`**
  passed via `feature_group_to_add`, which swaps just that layer on the live map.
  (3) The camera is moved by passing **`center`/`zoom`** (from `camera_view` →
  `_bounds_to_view`, the Web-Mercator `getBoundsZoom` fit). st_folium's frontend
  `setView`s **only when center/zoom change vs the last pass** (it compares
  `JSON.stringify(center)` and `zoom !== last_zoom`), so the camera eases to a
  route on a **search or focus switch** but stays put on a **segment toggle, a
  slider/address edit, or a manual pan**. `returned_objects=[]` keeps it one-way
  (no round-trip rerun). **st_folium has no animated `flyTo`** (its bundle only
  calls `setView`), so transitions are an instant/short-pan, not a Mapbox-style
  arc — a true eased flyTo would need a custom Leaflet/MapLibre component (the
  considered "Option B"). Region switch changes `_graph_center` → base JS changes
  → hash changes → an intentional remount onto the new area.
- **Wheel zoom.** Native Leaflet zoom with `zoom_snap=0` (fractional) +
  `wheel_px_per_zoom_level=40` (brisk). The **Leaflet.SmoothWheelZoom** plugin was
  tried for Google-Maps-style continuous zoom but **does not execute inside
  st_folium's iframe** (and disabling native zoom alongside it left the map
  un-zoomable), so it was reverted — don't re-add it without confirming it actually
  runs in the component.
- **CSS gotchas:** the rail is **fixed-width (446px) and non-resizable but
  collapsible at every viewport width.** The resize handle
  (`stSidebarResizeHandle`) stays hidden, but the **collapse chevron**
  (`stSidebarCollapseButton`, in a slim always-visible `stSidebarHeader` strip)
  and the **reopen chevron** (`stExpandSidebarButton`, in the top toolbar — the
  1.58 test-id; the older `stSidebarCollapsedControl`/`collapsedControl` don't
  exist in 1.58) are kept so the user can tuck the rail away at any size. The
  toolbar is therefore **not** `display:none` (only the deploy button + menu are
  suppressed); the header is click-through except the expand chevron, which
  re-arms `pointer-events:auto`. **The 446px width `!important` is scoped to the
  OPEN rail (`section[data-testid="stSidebar"][aria-expanded="true"]`)** — forcing
  a width on the *collapsed* state fought Streamlit's own collapse transform and
  left the rail half-shown at some widths. A **`@media (max-width:932px)`** block
  narrows the open rail (300px, min 260, max 70vw), tightens display type, and
  bumps text inputs to 16px (kills iOS focus-zoom); on a successful search a
  one-shot mobile-only JS collapses the rail so the map gets the screen (see the
  `_collapse_rail_mobile` flag). The main area's overflow is locked so the map
  doesn't spawn a page scrollbar.

### What's not yet implemented

- **Graph RAM footprint — Phase 1 DONE (slim runtime pickle).** The enriched
  GraphML loaded to **~2.7 GB peak / ~2.2 GB resident** in **~17 s** (52k nodes /
  150k edges; the 178 MB GraphML balloons from Python per-object overhead, ~32
  string attrs/edge, and ~81k shapely geometry objects) — over **Streamlit
  Community Cloud's 1 GB cap**, where *routing* thrashed on swap → multi-minute
  hangs. `walkability/graph/compact.py` now converts the enriched GraphML into a
  **slim runtime `MultiDiGraph`** holding only the query-time keep-set (node `y`/`x`
  + crossing `highway`; per-edge `length`, `foot_access`, `highway`, `name`, the
  factor scores/confidences `edge_walkability` reads, baked `walk_score`/
  `walk_confidence`, and `geometry` packed to a `float32` (n,2) array instead of
  shapely), with scores pre-coerced to native `float`. Pickled, not GraphML.
  Measured **17 s → 0.5 s load, 2.74 GB → 0.45 GB peak RSS, 178 MB → 40 MB on
  disk**, with **verified route-for-route parity** (default + slider weights,
  α=0/2/5, short/mid/long) and packed-geometry fidelity (~0.4 m float32 error).
  This clears the 1 GB ceiling by a wide margin. It is still a plain
  `MultiDiGraph`, so **routing/clip/router are unchanged**; only the app's
  `_edge_coords` learns the packed-ndarray geometry type. Build with
  `python -m walkability.graph.compact [--all|--dev|--region <r>]` (a post-process
  on the enriched GraphML — **no `--force` rebuild**); the app's `get_graph` loads
  the `*.runtime.pkl` sibling, downloading it from the release in preference to the
  GraphML (which remains the fallback). The `*.runtime.pkl` assets are **uploaded to
  the `data-v1` GitHub Release**, so the live app loads the slim graph; this is what
  cleared the 1 GB cap and let the app move to **Streamlit Community Cloud**.
- **Graph RAM — Phase 2 (open): compact CSR arrays.** For sub-100 MB / instant
  load, replace the per-query NetworkX substrate with numpy CSR (`indptr` + flat
  `float32` edge fields, code-mapped categoricals, packed geometry) — no Python
  object per edge. Routing/clip would adapt to it; notebooks keep GraphML. Phase 1
  already clears 1 GB, so this is now an optimisation, not a deploy blocker.
- **More map areas (UI TODO).** The "Map area" selector is parked in an expander at
  the bottom of the rail and currently offers Full Boston + the `DEV_REGIONS` test
  beds. When real additional areas/cities are added, promote it to a first-class
  control (and reconsider placement). Areas come from `DEV_REGIONS` in `build.py`.
- **Routes are clipped to Boston's municipal boundary (known limitation).** The walk
  graph is the OSM extract for the **city of Boston only**, so any route whose
  geographically optimal path leaves the city is forced to detour and comes out
  suboptimal. The sharpest case is **Brookline** — a separate town wedged into Boston
  between Allston/Brighton and Jamaica Plain/Mission Hill: an Allston→Jamaica Plain
  walk would naturally cut through Brookline, but those streets aren't in the graph,
  so the router takes the longer way around *within* Boston. Fixing it means widening
  the OSM extract past the city line (in `graph/download.py`) and rebuilding/
  re-enriching; the scoring/routing code is unaffected.
- Additional factors in `FACTOR_WEIGHTS` (`crossing_quality`, `poi_density`,
  `elevation_change`) remain removed — no enrichment tier produces that edge data
  yet. Re-add a weight only alongside the edge field that feeds it. `elevation_change`
  stays deferred until a hilly target city (`Research/break_research_2026-06-17.md` §2.3).
- **`environment` factor has no UI slider yet** — see the "Environment (safety)
  factor" note under Key design decisions.
- Remaining candidates from the ground-truth survey (`notebooks/ground_truth.csv`):
  (2b) turn-count/simplicity minimisation; (3) accessibility (step-free) toggle;
  (4) amenity/greenery factor. (The route-terminal restricted-access exemption and
  crossing-minimisation/side-awareness items from this list have shipped — see
  "Two-phase side-aware routing" and the cost-exemption notes above.)

### Dev workflow note

Neither `scikit-learn` nor `scipy` is installed. Do not call `ox.nearest_nodes()` (needs scikit-learn) or reach for `scipy.spatial.cKDTree`. Node snapping uses a vectorised numpy `argmin` over cached coordinate arrays in `routing/clip.py` (`snap_to_node`) — reuse it rather than re-scanning `G.nodes(data=True)`.
