# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install package in editable mode (required before running anything)
pip install -e .

# Download the base OSM walk graph for Boston (run once)
python walkability/graph/download.py

# Download the OSM feature inputs for the environment factor (run once):
# arterials (incl. motorway/trunk, which the walk graph excludes), buildings,
# shop/amenity POIs (with type), open space (parks/water), landuse=industrial
# (truck-corridor down-weight), and all roads (road-separation for the graded
# car-safety ceiling) → data/osm/*.gpkg. Missing core files disable the factor;
# missing landuse/roads just disable their (optional) sub-signal.
python walkability/graph/download_environment.py

# Build the full enriched graph (skips rebuild if output already exists)
python -m walkability.graph.build

# Force a full rebuild after changing enrichment logic
python -m walkability.graph.build --force

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

The core of the project is an enrichment pipeline in `walkability/graph/build.py` that attaches walkability scores to every edge of the Boston OSM walk graph. Two input datasets feed it:

- `data/osm/boston_walk.graphml` — OSM walk graph downloaded via osmnx
- `data/boston/sidewalk_inventory/` — Boston DPW shapefile with per-sidewalk condition, width, material, and survey date

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

**Both-sides surface aggregation** (`build.py::_aggregate_city_candidates`): a street's two sidewalks both fall within the 10 m match buffer of its single centerline edge. The join used to keep the single nearest-centroid polygon (an arbitrary "coin flip" that silently dropped the other side); now it takes an **area-weighted mean** over all valid candidates for `surface_score`/width and the **conservative (lowest-comfort) material**, and **flags divergence** — where the two sides genuinely disagree (one representative per inventory `SIDE`, largest-area; `|ΔSCI| > DIVERGENCE_THRESHOLD_SCI` or different material). Divergent edges have their `surface_confidence` multiplied by `DIVERGENCE_PENALTY` (0.85), since a single edge value is then less trustworthy. **Divergence is measured from per-`SIDE` representatives, not raw max−min over every matched polygon** — a tiny corner fragment of a perpendicular street is within the buffer too and would otherwise over-flag (~65% → ~26% divergent). Phantom polygons (`new_insp_d` pre-2000 **and** `inspected` null/NaN — never field-surveyed) are excluded from the aggregate; an edge whose candidates are all phantom/invalid falls through to the OSM tier. (This fixed a latent bug: the old per-row check `city_row.get("inspected") is None` never caught pandas `NaN`, so ~4.5k never-surveyed polygons were wrongly used as city data — hence the ~81%→~78% city-match shift.) Changing the aggregation or its constants requires a `--force` rebuild + re-baseline.

**Baked `walk_score` / `walk_confidence` fast path**: `_build_canonical_schema` calls `scoring.factors.edge_walkability()` with the *default* `FACTOR_WEIGHTS` and writes the result onto each edge. `edge_walkability()` reads this baked value back **only when called with the literal `FACTOR_WEIGHTS` object** (identity check); any other weights dict (e.g. UI sliders) forces a full recompute. This keeps the "weights are tunable without a rebuild" guarantee while giving the common default-weight query path zero per-edge work. **A `--force` rebuild is required to (re)populate the baked field** after changing scoring logic or weights; graphs built before the bake simply recompute and still work.

**Foot-access classification is one source of truth** in `scoring/factors.py`: `EXCLUDED_FOOT_ACCESS` (`foot=no` → impassable), `RESTRICTED_FOOT_ACCESS` (`private`/`customers`/`permit`/`residents`/… → walkable but penalised), and `FOOT_ACCESS_SCORE` (soft signal). `routing/cost.py` imports these sets so the hard routing rule and the soft score never drift apart. Boston OSM uses a wide access vocabulary (`customers` alone is ~1,100 edges) — add new values to these sets, not to scattered string checks.

**Two-level (HDI-style) scoring** (`scoring/factors.py::edge_walkability`, structure in `scoring/weights.py`): `walk_score` is built in two levels so a failure in one *dimension* of walking can't be bought back by excellence in another (a pristine surface must not rescue a walk along a highway):
1. **Within a category** — a weighted **arithmetic** mean of the present factors (factors there are *substitutable*; e.g. good condition offsets so-so material). `FACTOR_WEIGHTS` are these **within-category** relative weights, renormalised over whatever factors are present.
2. **Across categories** — an importance-weighted (`CATEGORY_WEIGHTS`) **geometric** mean of the category values, each floored to `[CATEGORY_FLOOR, 1]` (default 0.15). Categories are *non-substitutable*; one weak category dominates, and Safety ≥ Path outweigh Comfort (ratios 1.15 / 1.0 / 0.7 — the 2026-06-24 survey nudged safety modestly above path so low-safety stretches are punished harder; the bump is kept small to avoid the classic prior-model failure of over-indexing safety, since safety/path already share a car-danger signal). The floor stops a single zero category (motorway `road_type`, `foot=no`, on-arterial `environment`) from annihilating all discrimination — exactly as the Human Development Index bounds each dimension above zero. `CATEGORY_WEIGHTS` express *importance* (symmetric); `CATEGORY_FLOOR` is only the zero-collapse valve, **not** an importance dial (a high floor would clip and lose discrimination).

`CATEGORY_MAP` (`scoring/weights.py`) assigns each factor to one of three dimensions: **safety** (`environment`), **comfort** (`surface_quality`, `surface_material`, `surface_width`), **path** (`road_type`, `foot_access`). A new factor must be added to `CATEGORY_MAP` to count. A zero (UI-slider) weight drops the factor entirely; an all-empty category drops out of the geometric mean (never imputed). `confidence` stays a plain weight-weighted arithmetic mean (tiebreaker only — not geometric).

**Comfort top-compression** (`scoring/factors.py::compress_comfort`, 2026-06-28): the **comfort** dimension value is compressed above a knee — `comfort' = COMFORT_COMPRESS_KNEE + COMFORT_COMPRESS_K·(comfort − KNEE)` (0.80 / 0.50) — before `combine_categories`. City SCI/material run optimistic (wear, age, narrowness uncaptured), so comfort saturates at 0.91–0.94 on ordinary "fine" streets and gives the whole score a mild upward bias (the survey ran +0.9 high; this brings it to +0.2, MAE 2.75→2.35). It is **comfort-only and Seaport-safe by construction**: safety-gated routes (Seaport, gated by low safety in the geometric mean) barely move, so it trims overshoot without deepening the accepted low-end undershoots (see `prefer-underscoring-to-overscoring`). Applied at the two dimension-finalization points — the edge `category_values` (`edge_walkability`) and the route `dimension_scores` (`router._aggregate_route_dimensions`) — **not** in `combine_categories` itself (kept pure) and **not** on per-edge `edge_category_scores` (so the route power-mean is over raw comfort and the trim lands once on the aggregate; the exposed `dimension_scores` bars stay matching the score). `K=1.0` disables it. Shifts the baked `walk_score` → needs a `--force` rebuild + re-baseline.

This **supersedes** the earlier flat-mean tuning (where `road_type=4.5` sat just above the surface sum `4.0`): non-substitutability is now *structural*, so the cross-factor balance no longer lives in the weight numbers — they only set relative weight *within* a category. The two-level edge aggregate is a different axis from the route-level aggregate (the *same* two-level structure applied across distance — per-dimension power means `ROUTE_DIMENSION_EXPONENTS`, then the same geometric category combine; see routing step 7) — the two compose, don't duplicate. **Changing the structure or any weight requires a `--force` rebuild** to refresh the baked `walk_score` (the default-weight fast path), and re-baselining `notebooks/problem_routes_baseline.json`.

### Routing and scoring (query time)

Composite scoring and routing live in `walkability/scoring/factors.py` and `walkability/routing/`. The flow for one query (`routing.router.find_routes(G, orig, dest, alpha=...)`):

1. **Composite score** (`factors.edge_walkability`) — the two-level HDI-style aggregate (weighted-arithmetic within each `CATEGORY_MAP` category, floored, then equal-weight geometric mean across categories; see "Two-level scoring" above). Missing factors drop and weights renormalise so a missing score never penalises an edge. Returns `(walk_score, confidence)`, both [0,1]. This is also the boundary that **coerces GraphML strings** (`ox.load_graphml` returns custom fields as `"0.55"`, and `None` as a real `None`, an absent key, *or* the literal `"None"`) — use `_as_float`/`_as_str` rather than casting elsewhere.
2. **Cost** (`routing/cost.py`) — `cost = length × (1 + α·(1 − walk_score))`. `α` is the single distance/walkability knob (0 = shortest path; higher = detour toward walkable edges). (Crossings are not in the cost — they're handled by the phase-2 tube refinement; see "Two-phase side-aware routing" below.) `foot=no` returns `None` (edge dropped); restricted access multiplies by `RESTRICTED_ACCESS_PENALTY` **except on terminal edges** — `edge_cost(is_terminal=True)` skips the penalty for an edge leaving the origin or entering the destination, since you'd legitimately use a customers-only path at your own endpoint (the "zoo entrance" case). `_routable_digraph` marks terminal edges via `u == o_node`/`v == d_node`. Both `edge_cost` and the projection take an optional `weights` dict (defaults to the `FACTOR_WEIGHTS` object for the baked fast path) that `find_routes` threads through from the UI sliders.
3. **Spatial clip** (`routing/clip.py`) — clips the graph to an **ellipse with O and D as foci** before routing (`dist(O,n)+dist(n,D) ≤ budget`), so Yen's runs on a small local subgraph instead of all ~52k nodes. Node coords are cached on `G.graph`; snapping is a vectorised numpy `argmin`. **`find_routes` snaps with `snap_to_node(..., routable_only=True)`**: the geometrically nearest node to a real address is often a `foot=no` stub or a tiny disconnected footway fragment (e.g. an isolated pedestrian-bridge spur near the State House), which silently yields *zero routes*. `routable_only` restricts snapping to the **largest walkable connected component** (`clip._routable_mask`, built from non-`foot=no` edges and memoised on `G.graph`). `find_routes` also passes **`walk_bias=SNAP_WALK_BIAS_M`**: the chosen node minimises `dist_m + (1 − highway_score)·bias`, so an address prefers a nearby sidewalk/footway over an *arterial centreline* a few metres closer (which would otherwise force the route to start ON the arterial and U-turn — the Newmarket case). `walk_bias=0` (the default) stays exact-nearest so the `snap_to_node` invariant test still holds.
4. **A\* + penalty-method alternatives** (`_collect_candidates`) — the (clipped) `MultiDiGraph` is projected to a simple `DiGraph` (cheapest parallel edge per `(u,v)`, remembering its `key`; foot=no excluded). The best route is found with **A\*** (`nx.astar_path`) using a haversine straight-line heuristic — admissible/consistent because `cost ≥ length ≥ straight-line` for any alpha/weights, so it's exact under the UI sliders. Alternatives come from the **penalty method**: a per-edge multiplier (`ALT_PENALTY`, passed via A*'s `weight` callback — DG is never mutated) inflates a found route's edges so the next A* run diverges; an alternative is kept only if its true cost is within `ALT_MAX_STRETCH` of the optimum. This replaced Yen's `nx.shortest_simple_paths`, which dominated long-route latency (≈2.4 s → ≈0.5 s at 5 km).
5. **Confidence is a tiebreaker, not a cost term** — kept entirely out of the edge cost. After A* yields candidates, a re-rank adds a confidence bonus that decays to zero outside a small `walk_score` window (`tie_epsilon`), so it only reorders near-equal routes. If every candidate is below a confidence floor, more A* runs are pulled (expansion). **At `alpha=0` the walk re-rank is skipped** — pure-shortest-path mode keeps cost (length) order so it's a true length floor.
6. **Clip auto-widens** — if the best route hugs the ellipse boundary the clip widens (`WIDEN_FACTOR`, up to `MAX_WIDENS`) and finally falls back to the full graph, so clipping can never silently drop the true optimum.
7. **Route-level walk_score is a two-level HDI aggregate, one level up** (`_build_route` → `_aggregate_route_dimensions` + `factors.combine_categories`) — the route's reported `walk_score` mirrors the *edge* two-level structure across distance. **Per dimension** (safety/comfort/path from `edge_category_scores`), the route is a length-weighted **power mean** of that dimension's edge values over the edges where it's present, exponent from `ROUTE_DIMENSION_EXPONENTS` (`scoring/weights.py`; lower → more worst-segment-sensitive), floored to `[CATEGORY_FLOOR, 1]`. **Across dimensions**, those route-level values are combined with the **same** `CATEGORY_WEIGHTS` geometric mean as a single edge (the shared `combine_categories`). Aggregating *per dimension before* the cross-category combine is the point: a bad safety (or path) block can't be bought back by good comfort on the *same edge* — under the old single combined power mean the bad dimension was diluted within the edge (geometric-combined with the edge's good dimensions) *before* the route ever saw it. The floored per-dimension values are exposed on `RouteResult.dimension_scores` so the survey/diagnostics bars match the score exactly. `confidence` stays a plain length-weighted mean (tiebreaker only). Terminal restricted-access edges have their `foot_access` dropped before aggregation (matching the cost exemption). Query-time only — **no `--force` rebuild needed** to change `ROUTE_DIMENSION_EXPONENTS`, but re-baseline `notebooks/problem_routes_baseline.json` since both the scores and (via `_rank_score`/the phase-3 guard) the chosen routes can shift. **This is re-ranking/reporting, not re-search**: worst-segment is a whole-route quantity, so it can't enter the per-edge A\* cost without breaking admissibility — it changes which *already-found* candidate ranks #1 and whether a refined route is kept, never which corridors A\* explores. **Currently at neutral exponents (all = `ROUTE_SCORE_EXPONENT` = 0.5).** Step A isolated the dimension-wise *restructure* at neutral exponents (removing the old per-edge×route double worst-segment penalty — a small upward shift). **Step B (safety → 0.3) was trialled and reverted**: on the flagged-HIGH routes (Newmarket etc.) it was a near-no-op (≤0.5 walk pts), because those score high from *long* stretches of *moderate* (0.6–0.7) safety, not a short diluted bad block — the length-weighted power mean barely moves, and lowering the exponent further would let a single short floored edge collapse every route. The real lever for those routes is the safety **values** on industrial arterials (the provisional `perceived_safety`-runs-high issue), not the route-level exponent.

Performance: clipping makes local trips fast (≈0.1s at 700 m on the full graph) but barely helps long cross-city trips (large ellipse). Load the graph **once** (e.g. Streamlit `@st.cache_resource`) — the GraphML load alone is ~10 s.

**Routing scaling.** Profiling (full Boston) originally showed a 1.3 km route at ~0.2 s but a 5 km route at ~2.4 s, ~95% of it in Yen's `nx.shortest_simple_paths` (the clip grows with O–D distance and Yen's runs many edge-removal Dijkstras per candidate, pure-Python). Status of the planned fixes:
- **Tier 1 — DONE: A\* + penalty-method alternatives** (see routing step 4 above). Long routes dropped ~2.4 s → ~0.5 s at 5 km, ~0.3 s at 6 km, with no new dependency (NetworkX `astar_path`; `scikit-learn` is **not** required for A*, unlike `ox.nearest_nodes`). A* is exact (verified its cost == Dijkstra's).
- **Tier 2 (open): cache the projected DiGraph** across clip-widen retries / trim `k`/expansion pulls. Lower priority now that Tier 1 landed.
- **Tier 3 (long-term "real" answer): Customizable Contraction Hierarchies (CCH).** Our `alpha` slider and per-factor weight sliders change the **cost metric per query**, which is exactly what Customizable Route Planning / CCH are built for: a slow **metric-independent** preprocessing of the graph *topology* (once), a fast **customization** phase whenever the weights change, and **near-instant queries**. A plain static Contraction Hierarchy will **not** work for us because its shortcuts bake in one fixed weighting. This is the correct fix for dynamic-weight routing at city scale but a large implementation effort — only pursue if Tier 1/2 prove insufficient. Refs: Customizable Route Planning (Microsoft Research), Customizable Contraction Hierarchies (arXiv 1402.0402).

### Dev subsets and regions

`build.py` defines `DEV_REGIONS` — named neighbourhood subsets for fast iteration and for exercising the diagnostics on areas with different walkability. `beacon_hill` is the walkable reference (and keeps the legacy filename `boston_walk_dev.graphml` via `dev_region_path`); the others (`charlestown_sullivan`, `newmarket_massave`, `nubian_roxbury`) were chosen *and verified* to be less walkable so the audit flags actually fire. `build_dev_subset(region=...)` writes each to its own `boston_walk_dev_<region>.graphml`. To add a region, add a `DEV_REGIONS` entry (lat/lon/radius/note) — don't hardcode centres elsewhere.

### Diagnostics & verification tooling (`notebooks/`)

These are dev/QA scripts, not part of the package. They import each other as siblings, so **run them from the repo root** (`python notebooks/<file>.py`), which puts `notebooks/` on `sys.path`.

- `diagnostics.py` — the reusable toolkit. Three-tier inspection: `audit_route` (Tier 1, statistical flags — crossings counted from `highway=crossing` **nodes**, not edges, since our edges carry no crossing tag), `inspect_route_map` / `score_heatmap` / `routes_over_heatmap` (Tier 2, folium HTML), and street-imagery URL helpers (Tier 3). Also `breakdown_route`, `edge_vs_detour`, `audit_scoring_coverage`. Every function takes a graph `G`, so it works on the full graph or any region subset.
- `problem_routes.py` — region-tagged `PROBLEM_ROUTES` registry + regression harness (`measure`/`classify` vs `problem_routes_baseline.json`, crc32 path fingerprint). Routes run on the **full** graph by default (it covers every region); `region` is a grouping label.
- `verify_system.py` — automated invariants (see Commands).
- `region_maps.py` / `build_problem_route_maps.py` — batch HTML map generators.
- `test_route.py` — single edit-the-top-and-run route tester.
- `calibration_survey.py` — generates `calibration_survey.html`: a hand-picked set of routes across Boston's walkability spectrum, each as a zoomed map with numbered per-segment colouring, the per-DIMENSION breakdown (safety/comfort/path via `edge_category_scores`), Street View links, and the calibration questions. The tool for collecting `subj_walkability` ground truth to tune `CATEGORY_WEIGHTS` / `CATEGORY_FLOOR` / the `environment` constants.
- `ground_truth.csv` (+ `.README.md`) — region-tagged manual observation log; the human-judgment side of verification (subjective walkability, real surface/condition, route quality). `Research/work_and_verification_outline.md` explains the invariants-vs-validity boundary (what can/can't be automated).

**Crossings are not modeled directly.** They exist only as `highway=crossing` nodes and there is no crossing factor in `FACTOR_WEIGHTS` (reported `walk_score` is unaffected by them). `audit_route`'s crossing count and `RouteResult.crossing_count` are informational. Crossing-minimisation is structural — see "Two-phase side-aware routing" below.

**Two-phase side-aware routing (`router.find_routes`).** Where a street's two sides are distinct footways (most of footway-dense Boston), *crossing between them is free* in the cost, so a single-pass optimiser zigzags across streets and picks the "wrong side" — a side-switch costs nothing but *buys* `walk_score`, so a soft crossing penalty competes with walkability and is unreliable. The fix decomposes the problem (the graph has no side labels or street association — West Cedar St isn't even in it — so sides can't be chosen explicitly):
- **Phase 1 — corridor:** the existing walkability-aware A* + alternatives picks *which streets* (each candidate `R1`). Ranked by `_rank_score` first, so the corridor choice is made on the full walkable route.
- **Phase 2 — sides/crossings:** for each `R1` (in rank order), re-minimise **length** inside a narrow **tube** around it (`clip.clip_to_route`, half-width `TUBE_WIDTH_M`=35 m; `_collect_candidates` at `REFINE_ALPHA`=0). *Once the corridor is fixed, minimising length minimises gratuitous crossings* — a zigzag is strictly longer. The tube is wide enough for both sidewalks but narrower than a block, so phase 2 can switch sides but can't wander to a parallel street. No side labels, no crossing detection. Phase 2 **excludes `service` edges** not already on `R1` — pure length-min would otherwise take a parking-lot / back-alley shortcut (the South End "Boriken St" case), and the whole-route guard misses one diluted bad block.
- **Phase 3 — crossing-aware guard:** keep the shortened `R2` only if `R2.walk_score ≥ R1.walk_score − (REFINE_SCORE_TOL + REFINE_CROSSING_CREDIT·crossings_saved)` (0.04 + 0.05·ΔX); else revert to `R1` (the zigzag was avoiding a genuinely bad block). The **crossing credit** exists because crossings are free in the cost, so `R1`'s walk_score is partly inflated by weaving between parallel paths to harvest the best-scoring segment at each step — a fewer-crossing `R2` that scores a little lower is often the genuinely better route (the **Seaport** case: phase-1 wove 3 crossings for +8 walk pts; phase-2's length-min found the direct 1-crossing route at walk 70, which the flat-0.04 guard rejected and the crossing-aware guard accepts). The credit **only widens** the allowance when `R2` removes crossings, so it can never admit a route that doesn't cut crossings — confirmed across the survey: only Seaport flips (3→1), all 19 others unchanged. (Seaport's reported walk then drops 78→70, its *honest* value once the free-crossing harvest is removed; the residual "Seaport reads LOW" is the sparse-eyes issue, separate from routing.) `R2` is length-≤ `R1` by construction, so refinement never lengthens a route.

All **query-time, no rebuild** (no new edge fields). `find_routes(..., refine_sides=False)` disables phase 2 to reproduce the phase-1-only "previous" model for A/B. Skipped at `alpha=0` (the corridor is already the shortest path, preserving the length floor). Empirically: −4.2% total length across the survey routes, 0 routes lengthened, 0 walk-score drops beyond tol; Beacon Hill route #1 went 6→4 hops with the West Cedar zigzag removed. **The single knob is `TUBE_WIDTH_M`** (too narrow → can't reach the needed side on a wide street; too wide → phase 2 can jump to a shorter parallel street). Tuning it or `REFINE_*` is query-time but **re-baseline `notebooks/problem_routes_baseline.json`**.

### Boston sidewalk inventory field mapping

The shapefile columns do not match generic names — use these constants in `build.py`:

| Constant | Column | Notes |
|---|---|---|
| `SWK_CONDITION_FIELD` | `SCI` | Sidewalk Condition Index, numeric string 0–100. **Partly corrupt**: ~430 negatives (down to ~−68000, a city calc error) + the string `"NaN"`. `_condition_to_score` returns `None` for anything outside 0–100, so those ~5,250 edges fall through to the OSM tier instead of mis-scoring `surface_score=0.0`. |
| `SWK_WIDTH_FIELD` | `SWK_WIDTH` | Width in feet |
| `SWK_SURFACE_FIELD` | `MATERIAL` | Codes: `CC`=concrete, `BR`=brick, `BIT`/`AC`=asphalt, `GR`=granite, `OT`=other (scores as None) |
| `SWK_DATE_FIELD` | `new_insp_d` | Most recent re-inspection date; 1970-01-01 is a Unix-epoch placeholder (17% of rows, concentrated in West Roxbury and Downtown — a data-entry batch issue, not a spatial quality signal) |

**1970-date two-level treatment** (in `_build_canonical_schema`): rows with a pre-2000 date are split by the `inspected` column before confidence is assigned:
- `inspected = "yes"` → survey happened, date was mis-logged. Use SCI/MATERIAL; apply `CONF_CITY_DATE_MISSING = CONF_CITY_OLDER × 0.85` (≈ 0.72). Do **not** treat these as lower-quality edges — the West Roxbury concentration would introduce a spurious spatial confidence gradient.
- `inspected = null`/NaN → sidewalk polygon exists but was never field-surveyed. Such **phantom candidates are excluded inside `_aggregate_city_candidates`** before the both-sides mean is taken; an edge whose candidates are *all* phantom yields no aggregate and falls through to the OSM-tag tier. (The legacy single-row check `city_row.get("inspected") is None` in `_build_canonical_schema` only caught a literal missing key, never pandas `NaN` — the aggregator now handles both, which dropped ~4.5k wrongly-used phantom polygons.)

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

- **Graph load once + download-on-startup** (`@st.cache_resource`, keyed by path).
  The graph files are too big for the repo (enriched ≈122 MB), so `get_graph` fetches
  any missing file from a **GitHub Release** (`_GRAPH_RELEASE`, tag `data-v1`) via
  streaming `requests` to a `.part` temp then atomic rename — this is what makes a
  deployed instance (Streamlit Cloud) work without the data in git. Locally the files
  already exist so nothing downloads. The region selector (`key="region_select"`)
  sits at the bottom of the rail; its value is read at the **top** of the next run via
  the widget key so the graph can load before the widget renders (default `full`).
- **Address-only input.** Click-on-map and lat/lon entry were **removed** (they
  fought st_folium reruns and added clutter). Origin/destination are addresses,
  geocoded by `geocode()` → **Nominatim scoped to a Boston bounding box**
  (`bounded=1`, then the box as a soft bias), wrapped in `@st.cache_data`;
  `osmnx.geocode` is the unbounded fallback.
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
- **st_folium camera.** `returned_objects=[]` (the map needs no round-trip, so
  pan/zoom never rerun). `build_map` centres on and `fit_bounds` to the **focused
  route**; st_folium only re-renders (re-fits) when the figure actually changes —
  a search, "Show on map", or a segment toggle — so editing sliders/addresses/units
  leaves the view alone and the camera never snaps to the city default. (Earlier
  `framed_token` gating is gone — fitting the focused route subsumes it.)
- **Wheel zoom.** Native Leaflet zoom with `zoom_snap=0` (fractional) +
  `wheel_px_per_zoom_level=40` (brisk). The **Leaflet.SmoothWheelZoom** plugin was
  tried for Google-Maps-style continuous zoom but **does not execute inside
  st_folium's iframe** (and disabling native zoom alongside it left the map
  un-zoomable), so it was reverted — don't re-add it without confirming it actually
  runs in the component.
- **CSS gotchas:** the rail is **fixed-width and non-collapsible** — both the
  resize handle (`stSidebarResizeHandle`) and the collapse/expand control
  (`stSidebarCollapseButton` / `stSidebarCollapsedControl`) are hidden, so the
  horizontal dimensions never change. The main area's overflow is locked so the map
  doesn't spawn a page scrollbar.

### What's not yet implemented

- **Reduce graph RAM footprint (deployment TODO).** The full enriched graph
  loads to **~2.2 GB resident** in NetworkX (52k nodes / 150k edges; the 122 MB
  GraphML balloons due to Python per-object overhead + ~80k shapely geometry
  objects). This exceeds **Streamlit Community Cloud's 1 GB cap**, so the deployed
  app there can load the graph (sequential allocation tolerates swap) but *routing*
  thrashes on swap (random-access traversal) → multi-minute hangs. Locally (16 GB)
  and on **Hugging Face Spaces free CPU-basic (16 GB)** it fits fine; the latter is
  the chosen home for the full-Boston deploy. Routing adds ~0 MB at query time — the
  cost is entirely the resident graph. Options to shrink it, **none done yet**:
  (a) drop `geometry` + unused OSM attrs (`osmid`, `edge_class`, `sidewalk_*` raw
  fields, `oneway`/`reversed`, `service`/`maxspeed`/`lanes`/`width`/etc.) — measured
  **2.2 GB → 1.47 GB**, still over 1 GB, and loses curved map lines (UI falls back to
  straight node-to-node segments); (b) also drop the per-factor score fields and rely
  on baked `walk_score`/`walk_confidence` (sacrifices slider fine-tune fidelity);
  (c) abandon the osmnx/GraphML in-memory representation for a compact arrays/pickle
  structure (largest effort, only path likely to clear 1 GB). Needs a `--force`
  rebuild + a new release asset + a matching loader. Pursue only if returning to a
  1 GB host.
- **More map areas (UI TODO).** The "Map area" selector is parked in an expander at
  the bottom of the rail and currently offers Full Boston + the `DEV_REGIONS` test
  beds. When real additional areas/cities are added, promote it to a first-class
  control (and reconsider placement). Areas come from `DEV_REGIONS` in `build.py`.
- Additional factors in `FACTOR_WEIGHTS` (`crossing_quality`, `poi_density`,
  `elevation_change`) remain removed — no enrichment tier produces that edge data
  yet. Re-add a weight only alongside the edge field that feeds it. (`environment`
  followed exactly this rule — it was added together with `graph/environment.py`,
  which populates `environment_score`. `elevation_change` stays deferred until a
  hilly target city, per `Research/break_research_2026-06-17.md` §2.3.)
- **`environment` factor has no UI slider yet (TODO).** `environment` is a live
  `FACTOR_WEIGHTS` factor and routes through `find_routes`/`edge_cost` already, but
  it is **not** exposed as a per-factor weight slider in `app/streamlit_app.py`.
  Adding it was deliberately deferred until the planned routing changes land — add
  the slider then, mirroring the existing per-factor sliders.
- Candidate changes from the 10-route ground-truth survey (logged in
  `notebooks/ground_truth.csv`), in rough priority. **Done:** the route-terminal
  restricted-access exemption (the "customer at your own destination" case — now in
  `edge_cost(is_terminal=...)` and `_build_route`); and the **crossing-minimisation /
  side-awareness** half of (2) via the **two-phase tube routing** (phase-1 corridor →
  phase-2 length-min in a tube; see "Two-phase side-aware routing"), which fixes the
  "wrong side" zigzag in footway-dense areas without modeling sides. (An earlier soft
  crossing penalty + geometric `crossing_road_class` detection was tried and **removed**
  — a soft penalty competes with walkability and was unreliable.) **Still open:** (2b)
  turn-count/simplicity minimisation; (3) accessibility (step-free) toggle; (4)
  amenity/greenery factor.
- **`environment` factor (the SAFETY dimension; `graph/environment.py`).**
  `environment_score = sqrt(car_safety × perceived_safety)` — both must be high
  (geometric mean). It lives in the **safety** category of the two-level score, not
  a flat weight. It exists because a weight tweak alone can't fix a `highway_score`
  *value* too high for the actual environment — it needed a new edge feature.
  - **`car_safety = min(graded_ceiling, on_path, off_path) × industrial_penalty`**
    (weakest link → no double-count):
    *on_path* = the road you walk **along**, scored from its `maxspeed` via a
    crash-risk curve (`MAXSPEED_SAFETY_ANCHORS`; footway → 1.0, 25 mph → 0.90,
    35 → 0.45, missing → class default `DEFAULT_MAXSPEED_MPH`); *off_path* =
    proximity to a nearby fast road you're **not** on (for footways / quiet streets
    beside an arterial), computed **only for non-arterial edges** (an arterial's own
    danger is already on-path). **Off-path hostility uses the arterial's ACTUAL
    `maxspeed`** (resolved in `_arterial_scores`, falling back to the class default
    when untagged), mirroring on-path — so a calm 25 mph arterial barely penalises
    a nearby footway while a 40 mph parkway still does. Reach (penalty *distance*)
    stays class-based (`ARTERIAL_REACH_M`); only depth follows speed. This fixed
    the Back Bay/Seaport/Longwood "safety too low" cases (most Boston arterials are
    posted 25 mph, not the 30–35 class default). It **retired** the earlier arterial
    class-floor + pedestrian-exemption hack.
    - **Underground roads excluded (2026-06-28).** Off-path proximity is purely 2D
      distance-to-line, so a **tunneled** road would crater a fine surface footway
      directly above it — Boston's Big Dig buries I-90/I-93 under Fort Point /
      downtown / Seaport (the grounded case: Fort Point seg #2's nearest "arterial"
      was the tunneled Mass Pike, `car_safety` ~0.05 on a pleasant block, route 62
      vs ideal 70). `download_environment.py` now keeps the `tunnel`/`layer` tags on
      both the arterials and roads gpkgs, and `environment.py::_drop_underground`
      removes `tunnel=yes`/`layer<0` segments from `load_arterials()` (off-path) and
      `load_roads()` (separation — a path *above* a buried road is genuinely
      road-separated). Scoped to **underground only**; elevated/bridge roads stay (a
      pedestrian under a viaduct does feel it). 127 underground arterials dropped
      citywide; Fort Point 62→75. The residual Fort Point gap is the sparse-eyes
      (low `eyes_score`) issue, not the tunnel. Requires re-download + `--force`
      rebuild + re-baseline (done 2026-06-28).
    - **Graded car-safety ceiling (B; 2026-06-26 env-rework).** The ceiling is
      `CAR_SAFETY_CEIL + (1 − CAR_SAFETY_CEIL)·road_separation` rather than a flat
      `min(0.85,…)` clip. `road_separation` = `min(1, dist_to_nearest_road /
      SEPARATION_REACH_M)` from the new all-roads layer (`boston_roads.gpkg`): a
      road-adjacent path (separation 0) still tops at 0.85, but a genuinely road-
      SEPARATED path (greenway / HarborWalk / pedestrian bridge, separation→1)
      climbs toward 1.0. The old hard clip flattened a park path and a sidewalk-
      beside-a-calm-road to the *same* 0.85, erasing that distinction. `min()` still
      lets a real on/near road dominate, so low values are untouched. (The arterial
      layer alone can't do this — "no arterial near" ≠ "no road near".)
    - **Industrial truck penalty (A; same rework).** `industrial_exposure` =
      proximity to a `landuse=industrial` polygon (new `boston_landuse.gpkg`,
      `INDUSTRIAL_REACH_M`) multiplies `car_safety` down by
      `(1 − INDUSTRIAL_CAR_PENALTY·exposure)`. `maxspeed` misses truck/industrial
      danger, and off-path can't discriminate (industrial Mass Ave is posted 25 mph
      like a genteel street — the grounded Newmarket finding). A and B are
      orthogonal: A lowers road-adjacent industrial corridors, B raises separated
      greenways. Both layers are **optional** — absent ⇒ exposure/separation 0 ⇒
      the pre-rework model.
  - **`perceived_safety`** (stored as the `eyes_score` field) = a probabilistic
    **noisy-OR** (`1 − ∏(1−s)`) of three SUBSTITUTABLE signals — safe if ANY is
    strong, unsafe only when you lack ALL: *activity* (foot-traffic POIs,
    type-weighted so benches/parking — 57% of raw POIs — count 0 via
    `POI_NOISE_AMENITIES`), *enclosure* (buildings facing the street; dropped for
    alley/service edges, **and discounted by `industrial_exposure`** —
    `enclosure ×= (1 − INDUSTRIAL_ENCLOSURE_DISCOUNT·exposure)` — since a warehouse
    footprint provides no residential "eyes"; A's secondary half), *openness*
    (adjacency to large park/water, `OPENSPACE_MIN_AREA_M2`/`OPENNESS_REACH_M` — the
    Seaport HarborWalk fix, and *discriminating* unlike footway density, which is
    ~universal in a city).
  - **Calibrated** against the Boston survey (`calibration_survey.py` →
    `ground_truth.csv`): Back Bay 54→89, South End →85, Seaport 40→78 (openness),
    plus the Newmarket origin-snap fix (`SNAP_WALK_BIAS_M`, routing/clip.py) and the
    `surf=0.00` data-bug fix (`_condition_to_score` rejects out-of-range SCI;
    residential bumped 0.55→0.65). `CATEGORY_WEIGHTS` = safety ≥ path > comfort
    (1.15 / 1.0 / 0.7 after the 2026-06-24 survey).
  - **Env-rework (2026-06-26), grounded on Newmarket — code landed, awaiting a
    `--force` rebuild + re-survey** (see `Research/env_rework_spec.md` and memory
    `newmarket-safety-high-needs-landuse`). The earlier "perceived_safety runs HIGH"
    hypothesis was **partly refuted**: on industrial Newmarket `eyes` was already
    ~0.53 (fine); the real inflation was **`car_safety` pinned at the 0.85 ceiling**
    beside the industrial arterial (footway `on_path`=1.0 + weak off-path), with
    warehouse `enclosure` a secondary lift. Ruled out by grounding: the route-level
    worst-segment exponent (no-op — bad blocks are short, length-weighting protects
    them; reverted) and a speed-based off-path penalty (can't discriminate —
    industrial Mass Ave 0.87 vs charming Beacon Hill 0.91, both posted 25 mph). The
    landed fix is **A (industrial down-weight) + B (graded car ceiling)** above.
    **To activate:** `python walkability/graph/download_environment.py --force`
    (fetches `boston_landuse.gpkg` + `boston_roads.gpkg`) → check `landuse=industrial`
    coverage on Newmarket (the viability gate) → `python -m walkability.graph.build
    --force` → re-baseline → re-survey (Newmarket/Allston should drop; greenways /
    HarborWalk should rise; Beacon Hill/Back Bay must NOT spuriously rise). The
    per-edge `industrial_exposure`/`road_separation` fields let A and B be measured
    independently offline from the single rebuild (`INDUSTRIAL_CAR_PENALTY` /
    `SEPARATION_REACH_M` are the tuning knobs). `EYES_CEIL`'s own flattening is a
    deferred later pass.

### Dev workflow note

Neither `scikit-learn` nor `scipy` is installed. Do not call `ox.nearest_nodes()` (needs scikit-learn) or reach for `scipy.spatial.cKDTree`. Node snapping uses a vectorised numpy `argmin` over cached coordinate arrays in `routing/clip.py` (`snap_to_node`) — reuse it rather than re-scanning `G.nodes(data=True)`.
