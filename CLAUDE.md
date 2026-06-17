# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install package in editable mode (required before running anything)
pip install -e .

# Download the base OSM walk graph for Boston (run once)
python walkability/graph/download.py

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

1. **City inventory match** (`data_source = "city_inventory"`) — spatial join within 10 m. Provides `surface_score` (SCI/100, structural condition) and `surface_material_score` (from MATERIAL code). ~81% of edges.
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

**`sidewalk_condition`** stores the raw SCI value (0–100) for auditing. The normalized [0, 1] score used in scoring is `surface_score`.

**Baked `walk_score` / `walk_confidence` fast path**: `_build_canonical_schema` calls `scoring.factors.edge_walkability()` with the *default* `FACTOR_WEIGHTS` and writes the result onto each edge. `edge_walkability()` reads this baked value back **only when called with the literal `FACTOR_WEIGHTS` object** (identity check); any other weights dict (e.g. UI sliders) forces a full recompute. This keeps the "weights are tunable without a rebuild" guarantee while giving the common default-weight query path zero per-edge work. **A `--force` rebuild is required to (re)populate the baked field** after changing scoring logic or weights; graphs built before the bake simply recompute and still work.

**Foot-access classification is one source of truth** in `scoring/factors.py`: `EXCLUDED_FOOT_ACCESS` (`foot=no` → impassable), `RESTRICTED_FOOT_ACCESS` (`private`/`customers`/`permit`/`residents`/… → walkable but penalised), and `FOOT_ACCESS_SCORE` (soft signal). `routing/cost.py` imports these sets so the hard routing rule and the soft score never drift apart. Boston OSM uses a wide access vocabulary (`customers` alone is ~1,100 edges) — add new values to these sets, not to scattered string checks.

**`FACTOR_WEIGHTS` ordering is deliberate** (`scoring/weights.py`): `road_type=4.5` is set *just above* the combined surface weight (`surface_quality 2.0 + surface_material 2.0 = 4.0`), so the road type a pedestrian walks along is the single dominant signal but surface can still move the score. This was a deliberate correction from an earlier `road_type=3.0` (where the two surface factors collectively outvoted road type and inflated pristine sidewalks in hostile environments). Don't lower `road_type` back below the surface sum without re-checking the ground-truth survey. **Changing any weight requires a `--force` rebuild** to refresh the baked `walk_score` (the default-weight fast path), and re-baselining `notebooks/problem_routes_baseline.json`.

### Routing and scoring (query time)

Composite scoring and routing live in `walkability/scoring/factors.py` and `walkability/routing/`. The flow for one query (`routing.router.find_routes(G, orig, dest, alpha=...)`):

1. **Composite score** (`factors.edge_walkability`) — weighted mean of the present per-factor scores, **renormalised over whatever factors exist** so a missing surface score never penalises an edge. Returns `(walk_score, confidence)`, both [0,1]. This is also the boundary that **coerces GraphML strings** (`ox.load_graphml` returns custom fields as `"0.55"`, and `None` as a real `None`, an absent key, *or* the literal `"None"`) — use `_as_float`/`_as_str` rather than casting elsewhere.
2. **Cost** (`routing/cost.py`) — `cost = length × (1 + α·(1 − walk_score))`. `α` is the single distance/walkability knob (0 = shortest path; higher = detour toward walkable edges). `foot=no` returns `None` (edge dropped); restricted access multiplies by `RESTRICTED_ACCESS_PENALTY` **except on terminal edges** — `edge_cost(is_terminal=True)` skips the penalty for an edge leaving the origin or entering the destination, since you'd legitimately use a customers-only path at your own endpoint (the "zoo entrance" case). `_routable_digraph` marks terminal edges via `u == o_node`/`v == d_node`. Both `edge_cost` and the projection take an optional `weights` dict (defaults to the `FACTOR_WEIGHTS` object for the baked fast path) that `find_routes` threads through from the UI sliders.
3. **Spatial clip** (`routing/clip.py`) — clips the graph to an **ellipse with O and D as foci** before routing (`dist(O,n)+dist(n,D) ≤ budget`), so Yen's runs on a small local subgraph instead of all ~52k nodes. Node coords are cached on `G.graph`; snapping is a vectorised numpy `argmin`. **`find_routes` snaps with `snap_to_node(..., routable_only=True)`**: the geometrically nearest node to a real address is often a `foot=no` stub or a tiny disconnected footway fragment (e.g. an isolated pedestrian-bridge spur near the State House), which silently yields *zero routes*. `routable_only` restricts snapping to the **largest walkable connected component** (`clip._routable_mask`, built from non-`foot=no` edges and memoised on `G.graph`). The unrestricted default is unchanged so the `snap_to_node` invariant test still holds.
4. **A\* + penalty-method alternatives** (`_collect_candidates`) — the (clipped) `MultiDiGraph` is projected to a simple `DiGraph` (cheapest parallel edge per `(u,v)`, remembering its `key`; foot=no excluded). The best route is found with **A\*** (`nx.astar_path`) using a haversine straight-line heuristic — admissible/consistent because `cost ≥ length ≥ straight-line` for any alpha/weights, so it's exact under the UI sliders. Alternatives come from the **penalty method**: a per-edge multiplier (`ALT_PENALTY`, passed via A*'s `weight` callback — DG is never mutated) inflates a found route's edges so the next A* run diverges; an alternative is kept only if its true cost is within `ALT_MAX_STRETCH` of the optimum. This replaced Yen's `nx.shortest_simple_paths`, which dominated long-route latency (≈2.4 s → ≈0.5 s at 5 km).
5. **Confidence is a tiebreaker, not a cost term** — kept entirely out of the edge cost. After A* yields candidates, a re-rank adds a confidence bonus that decays to zero outside a small `walk_score` window (`tie_epsilon`), so it only reorders near-equal routes. If every candidate is below a confidence floor, more A* runs are pulled (expansion). **At `alpha=0` the walk re-rank is skipped** — pure-shortest-path mode keeps cost (length) order so it's a true length floor.
6. **Clip auto-widens** — if the best route hugs the ellipse boundary the clip widens (`WIDEN_FACTOR`, up to `MAX_WIDENS`) and finally falls back to the full graph, so clipping can never silently drop the true optimum.
7. **Route-level walk_score is a worst-segment-aware power mean** (`_build_route`) — the route's reported `walk_score` is a length-weighted *power mean* of its edge scores with exponent `ROUTE_SCORE_EXPONENT` (`scoring/weights.py`, default 0.5 < 1), so one bad block drags the whole route down instead of being averaged away. `confidence` stays a plain length-weighted mean (tiebreaker only). Terminal edges that are restricted-access have their `foot_access` factor dropped before aggregation (recomputed by removing the `foot_access`/baked keys), matching the cost exemption so a forced endpoint neither distorts route choice nor tanks the reported score. This aggregation is query-time only — **no `--force` rebuild needed** to change `ROUTE_SCORE_EXPONENT`, but re-baseline `notebooks/problem_routes_baseline.json` since routing paths can shift.

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
- `ground_truth.csv` (+ `.README.md`) — region-tagged manual observation log; the human-judgment side of verification (subjective walkability, real surface/condition, route quality). `Research/work_and_verification_outline.md` explains the invariants-vs-validity boundary (what can/can't be automated).

**Crossings are unscored.** They exist only as `highway=crossing` nodes; the edge-cost router ignores node attributes, and there is no crossing factor in `FACTOR_WEIGHTS`. `audit_route`'s crossing count is informational, not a score input.

### Boston sidewalk inventory field mapping

The shapefile columns do not match generic names — use these constants in `build.py`:

| Constant | Column | Notes |
|---|---|---|
| `SWK_CONDITION_FIELD` | `SCI` | Sidewalk Condition Index, numeric string 0–100 |
| `SWK_WIDTH_FIELD` | `SWK_WIDTH` | Width in feet |
| `SWK_SURFACE_FIELD` | `MATERIAL` | Codes: `CC`=concrete, `BR`=brick, `BIT`/`AC`=asphalt, `GR`=granite, `OT`=other (scores as None) |
| `SWK_DATE_FIELD` | `new_insp_d` | Most recent re-inspection date; 1970-01-01 is a Unix-epoch placeholder (17% of rows, concentrated in West Roxbury and Downtown — a data-entry batch issue, not a spatial quality signal) |

**1970-date two-level treatment** (in `_build_canonical_schema`): rows with a pre-2000 date are split by the `inspected` column before confidence is assigned:
- `inspected = "yes"` → survey happened, date was mis-logged. Use SCI/MATERIAL; apply `CONF_CITY_DATE_MISSING = CONF_CITY_OLDER × 0.85` (≈ 0.72). Do **not** treat these as lower-quality edges — the West Roxbury concentration would introduce a spurious spatial confidence gradient.
- `inspected = null` → sidewalk polygon exists but was never field-surveyed. `city_row` is set to `None` in `_build_canonical_schema` before any city fields are read, so these fall through to the OSM-tag tier.

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
  `elevation_change`) were removed — no enrichment tier produces that edge data
  yet. Re-add a weight only alongside the edge field that feeds it.
- Candidate changes from the 10-route ground-truth survey (logged in
  `notebooks/ground_truth.csv`), in rough priority. **Done:** the route-terminal
  restricted-access exemption (the "customer at your own destination" case — now in
  `edge_cost(is_terminal=...)` and `_build_route`). **Still open:** (2) a
  crossing/turn-minimisation objective; (3) an accessibility (step-free) toggle;
  (4) amenity/greenery and safety dimensions.
- **Environment overrating (open).** The `road_type` weight was raised to 4.5
  (slightly above the combined surface weight of 4.0), which improved arterial
  avoidance but did **not** fix the
  newmarket-style overrating: industrial streets tagged `highway=residential`
  get `highway_score=0.55` (not low enough) and pristine surfaces still pull the
  composite up. The real fix is a `HIGHWAY_SCORES` value adjustment or a new
  environment/arterial-proximity factor — a weight tweak alone cannot fix a
  `highway_score` *value* that is too high for the actual environment.

### Dev workflow note

Neither `scikit-learn` nor `scipy` is installed. Do not call `ox.nearest_nodes()` (needs scikit-learn) or reach for `scipy.spatial.cKDTree`. Node snapping uses a vectorised numpy `argmin` over cached coordinate arrays in `routing/clip.py` (`snap_to_node`) — reuse it rather than re-scanning `G.nodes(data=True)`.
