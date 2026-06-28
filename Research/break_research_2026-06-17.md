# Walkability Project — Break Research (2026-06-17)

Research into three questions posed before a two-day break: (1) comparable
projects + how to scale to many regions while keeping RAM and load times low;
(2) which cities suit this project and which are hard; (3) — most important —
how to improve the routing software, what exists beyond A\*, and how to learn
good weights.

This is a strategy/landscape document, not a task list. Recommendations are
tied to the current stack (NetworkX + GraphML, `alpha` + per-factor weight
sliders, A\* + penalty-method alternatives, baked `walk_score` fast path).

---

## 1. Comparable projects, their memory profiles, and scaling to many regions

### 1.1 The projects that are closest to what you're building

**AccessMap (UW Taskar Center)** is the single closest analogue and worth
studying in depth. It's an OSM-based *routable pedestrian network* that scores
sidewalk segments and lets users tune routing to their needs — avoid hills above
a chosen grade, route around construction, prefer curb ramps. It launched for
Seattle in 2017 and has expanded across Washington State (Everett, Mount Vernon,
Bellingham). Crucially, its data layer is **OpenSidewalks**, a schema for
representing sidewalks, crossings, curb ramps, and inclines as first-class OSM
features rather than tags on road centerlines. The architectural lesson: they
separated the *data schema* (OpenSidewalks) from the *app* (AccessMap) so the
same network feeds multiple front-ends. Your `data_source`-tiered enrichment is
philosophically similar, but you bake scores onto road-centerline edges whereas
they model the sidewalk geometry itself.

**Project Sidewalk (UW Makeability Lab)** is the closest analogue on the *data*
side, and is the most important external resource for your question 2. It's a
web crowdsourcing tool where volunteers label sidewalk accessibility from Street
View imagery (missing sidewalks, surface problems, obstructions, curb ramps).
As of 2025 it's deployed in **35 cities across 8 countries** (US, Canada,
Mexico, Ecuador, Netherlands, Switzerland, New Zealand) with **1.1M+ labels over
~21,350 km**. This is the largest open sidewalk-condition dataset in existence
and is exactly the kind of `surface_score`/condition input you currently get
from Boston's DPW inventory — but available in many more cities.

**The big general-purpose engines** — Valhalla, OSRM, GraphHopper,
openrouteservice — all do pedestrian profiles but **none score sidewalk
*quality/condition*** the way you do. They route on network topology + coarse
profile costs. Your differentiator (surface condition, material comfort,
arterial hostility, per-factor re-weighting at query time) is genuinely
under-served by the off-the-shelf engines. That's the moat; don't lose it by
adopting an engine that can't express per-factor costs.

### 1.2 Memory profiles — the honest comparison

Your CLAUDE.md already records the core problem: the full enriched Boston graph
is **~2.2 GB resident** in NetworkX (52k nodes / 150k edges from a 122 MB
GraphML), driven by Python per-object overhead and ~80k shapely geometry
objects. For context, here's where the production engines sit:

| Engine | Representation | RAM (full planet) | Notes |
|---|---|---|---|
| **OSRM** | CH, in-memory (mmap-able) | ~55–65 GB planet | Fastest queries; one fixed metric baked into the CH |
| **GraphHopper** | CH/LM, JVM heap | 8–16 GB (continent), 40–128 GB planet | Java overhead; CH "speed mode" = fixed profiles |
| **Valhalla** | **Tiled, memory-mapped** | **4–8 GB serve-time**, tiles loaded on demand | Tile design is the key idea for you (see below) |
| **Your app (NetworkX)** | Python objects + shapely | 2.2 GB for *one mid-size city* | Doesn't scale past ~1 city per GB |

The takeaway is stark: NetworkX is fine for one city on a 16 GB box (Hugging
Face Spaces, your chosen home), but it is the wrong substrate for "many regions."
At 2.2 GB/city you'd hit a wall at a handful of cities even before considering
load time (~10 s GraphML parse per city). The production engines get 1–2 orders
of magnitude better density because they don't store Python objects.

### 1.3 How to load many regions while keeping RAM and load time low

There are three levers, in increasing order of effort and payoff.

**Lever A — Shrink the per-edge representation (compact arrays / CSR).** This is
the highest-leverage change and the one your "abandon GraphML for a compact
arrays/pickle structure" TODO already anticipates. The standard structure is
**CSR (Compressed Sparse Row)**: two arrays — `indptr` (per-node offset into the
neighbor list) and `indices` (destination node ids) — plus parallel `float32`/
`uint8` arrays for each edge attribute (`length`, `walk_score`,
per-factor scores, a `data_source` enum byte). Neighbor lookup is O(1) slicing;
the whole thing is contiguous NumPy, so it memory-maps directly and loads in
**milliseconds** instead of ~10 s. A 150k-edge graph in CSR with ~6 float32
attributes is on the order of **5–15 MB resident**, versus 2.2 GB — a
~100–400× reduction. You lose curved `geometry` (store it in a *separate*
mmap'd blob loaded only for the routes actually drawn, not for routing), and you
write your own Dijkstra/A\* over the arrays (a day's work; you already have the
cost function). This alone makes "dozens of cities on one box" trivial and
clears the 1 GB Streamlit cap you noted.

**Lever B — Tile + load-on-demand (the Valhalla pattern).** Once cities are
small CSR blobs, don't hold them all resident. Keep a registry of
`{region: path}`, mmap a region's arrays on first request, and let the OS page
them in/out. With mmap, "loading" a region is near-free and the kernel evicts
cold regions under pressure — you get Valhalla's 4–8 GB-serves-a-continent
behavior for free because mmap'd pages are reclaimable, unlike Python heap. For
truly continental scale you'd tile *within* a city too, but per-city tiling is
almost certainly enough for your foreseeable scope.

**Lever C — Don't store what you can bake or drop.** Your routing only needs:
node coords (for the A\* heuristic + snapping), `length`, the per-factor scores,
`foot_access`, and the baked `walk_score`/`walk_confidence`. Everything else
(`osmid`, `oneway`, `reversed`, `service`, `maxspeed`, `lanes`, raw
`sidewalk_*`, `SCI` once normalized) is audit metadata — keep it in a
*side file* (Parquet) keyed by edge id, not in the routing structure. Your own
measurement (2.2 → 1.47 GB just from dropping geometry + unused attrs) confirms
the attrs are a big chunk; CSR + side-file makes the split clean.

**Recommended path:** Lever A (CSR + mmap) is the real answer and subsumes most
of Lever C. It is the same conclusion your CLAUDE.md reached ("option (c),
largest effort, only path likely to clear 1 GB") — this research confirms it's
the right call and that the effort is moderate (a day or two), not large, *if*
you keep the geometry in a separate lazily-loaded blob and write a minimal
array-based A\*. Do this before adding cities, not after; retrofitting the loader
across many GraphML files is worse.

---

## 2. Which cities suit the project — and which are hard

### 2.1 The data dependency that decides everything

Your pipeline's quality comes from **tier 1: a municipal sidewalk inventory with
condition + material** (Boston's SCI/MATERIAL shapefile). The single best
predictor of "is this city a good fit" is: *does it publish an open sidewalk
inventory with a condition index?* Where it doesn't, you fall back to OSM tags
(tier 2), which carries far less signal — that's the Newmarket overrating
problem already in your TODO, and it gets worse in cities with thin OSM
sidewalk coverage.

### 2.2 Tier-1 fit: cities with open sidewalk + condition data

These are the strongest expansion candidates because they can replicate the
Boston tier-1 path with minimal new pipeline logic:

- **Washington, DC** — DC GIS publishes planimetric sidewalk polygons for all
  public walkways, *and* an infrastructure-quality index built from 311 calls,
  the DDOT street database, and **Project Sidewalk** crowdsourced data. This is
  arguably a *better* data environment than Boston and the obvious next city.
- **New York City** — has a Sidewalk Management System (inspections, violations,
  repair status) plus the best OSM coverage in the US. Condition data is more
  about defects/violations than a continuous index, so you'd adapt the SCI
  mapping, but the raw material is there. Caveat: graph size — NYC pedestrian
  network is much larger than Boston, which makes Lever A above a prerequisite,
  not a nicety.
- **Seattle** — already has AccessMap/OpenSidewalks coverage; you could ingest
  the OpenSidewalks network directly rather than rebuilding from a city
  shapefile. Good validation target since there's an existing app to compare to.
- **Project Sidewalk cities** broadly (35 cities, 8 countries) — anywhere with
  dense Project Sidewalk labels gives you a condition signal even absent a
  municipal inventory. Chicago, Newberg OR, Mexico City/San Pedro, Amsterdam,
  Zurich are explicitly active. This is your portable tier-1 substitute.

### 2.3 Cities that pose a genuine challenge

- **San Francisco / Pittsburgh / Seattle hills** — *terrain*. SF in particular
  laid a grid over hills, producing extreme grades and even stepped sidewalks.
  Your model has **no elevation/grade factor** (it's in the removed-factors
  list). In a flat-ish city that's a minor omission; in SF it's
  disqualifying — a pristine 20% grade sidewalk would score high and route
  pedestrians up a wall. SF is the canonical "needs an elevation factor before
  you go there" city. AccessMap treats grade as a first-class, user-tunable cost
  for exactly this reason. **Recommendation: don't ship a hilly city until
  `elevation_change` is a real factor** fed by a DEM (SRTM/USGS 3DEP) sampled
  along edges.
- **Cities with thin OSM sidewalk coverage** — suburban/low-density places
  (the "Folsom problem": roads mapped, residential sidewalks entirely missing).
  Here both tier 1 (no inventory) and tier 2 (no OSM sidewalks) are weak, and
  you'd be routing on road centerlines with almost no walkability signal.
- **Global South / informal settlements** — urban form diverges from the grid
  assumptions, sidewalks are often informal or absent in OSM, and condition data
  doesn't exist. High social value, but a different data-acquisition project
  (imagery + crowdsourcing) before routing is meaningful.
- **Very large networks (NYC, LA, London)** — not a *data* challenge but a
  *scale* one; they force the CSR/mmap rework and make the clip-then-route
  strategy more important (long cross-city ellipses are where your current
  latency lives).

### 2.4 Suggested expansion order

1. **Washington, DC** — best data environment, flat enough, has a condition
   index. Lowest-risk second city and a strong proof that the pipeline
   generalizes beyond Boston.
2. **Seattle** — ingest OpenSidewalks; gives you a head-to-head validation
   against AccessMap.
3. **NYC** — high value, but only after CSR/mmap lands.
4. **San Francisco** — only after an elevation factor exists; it's the test case
   that forces the most-needed missing factor.

---

## 3. Improving the routing software (the main question)

Three sub-questions: what's beyond A\*, how to choose weights (A/B vs ML vs
other), and what the right overall strategy is. Short version: **A\* is not your
problem and A/B testing is the wrong first tool.** The leverage is in the *cost
model* (what the weights are and whether they're trustworthy), with the
*algorithm* a distant second that only matters once you scale.

### 3.1 Routing algorithms beyond A\* — what they are and whether you need them

Your A\* + penalty-method alternatives is already a good, *exact* local-query
design. The alternatives to A\* are almost all about **going faster at scale**,
not finding better routes. Ranked by relevance to you:

- **Contraction Hierarchies (CH)** — preprocess shortcuts, then queries are
  milliseconds. The catch you already identified: a static CH **bakes in one
  fixed metric**. Your `alpha` and per-factor sliders change the metric every
  query, so plain CH is unusable for the tunable path. (It *would* work for the
  default-weights fast path only — not worth the complexity.)
- **Customizable Route Planning (CRP) / Customizable Contraction Hierarchies
  (CCH)** — the correct family for you, and the one your CLAUDE.md already flags
  as the Tier-3 answer. They split preprocessing into a slow
  **metric-independent** phase over the *topology* (done once per city) and a
  fast **customization** phase whenever the weights change (sub-second), after
  which queries are near-instant. This is purpose-built for "user moves a slider,
  re-cost the graph, route instantly." CCH uses a nested-dissection order; recent
  surveys (Bläsius et al., 2025) and separator-based alternative-path methods
  (ATMOS 2025) show it also supports the *alternative routes* you currently get
  from the penalty method. **This is the only algorithmic upgrade worth doing —
  and only when query latency on the full graph becomes the user-facing
  bottleneck.** It is a large implementation effort with no mature, maintained
  Python library (RoutingKit is C++; you'd be binding or porting).
- **ALT (A\*, Landmarks, Triangle inequality)** — precompute distances to a
  handful of landmarks for a much better A\* heuristic. **This is the sweet spot
  between your current A\* and full CCH:** modest preprocessing, no metric
  baking (landmark distances under a lower-bound metric stay admissible), pure
  Python-feasible, and it directly attacks your long-route latency. If A\* ever
  feels slow before you're ready for CCH, do ALT first.
- **Multi-criteria / Pareto routing (e.g. NSGA-II, multi-objective label-
  setting)** — instead of collapsing distance and walkability into one `alpha`,
  compute the **Pareto front** of routes (each not dominated on both distance and
  walkability) and present a few. This is a genuinely different *product* idea,
  not just a speed trick: rather than asking the user to pick `alpha` up front,
  show them "shortest," "most walkable," and 1–2 knee-point compromises and let
  them choose. Worth prototyping because it sidesteps the "what alpha do I want?"
  cognitive load. Downside: more expensive, and the front can be large.

**Bottom line on algorithms:** keep A\* + penalty alternatives. Add **ALT** if/
when latency bites. Reserve **CCH** for when you're multi-city and queries on
full graphs dominate UX. Prototype **Pareto/multi-objective** as a product
experiment, not a performance one.

### 3.2 Choosing weights — the real lever, and why A/B testing is the wrong start

Your weights (`road_type=4.5`, surface 2.0+2.0, etc.) are currently
expert-tuned against a 10-route ground-truth survey. The question is how to make
them principled. The options, from worst-fit to best-fit for your situation:

**A/B testing — not yet, and maybe never in the naive form.** Online A/B tests
need (a) real traffic and (b) a clean outcome metric. You have neither a user
base nor an unambiguous "this route was better" signal (did they walk it? did
they enjoy it? you can't see it). Worse, route recommendation A/B testing has
known pitfalls: **off-policy/counterfactual evaluation** (you only observe
outcomes for routes you *showed*, so you can't fairly score a new weighting that
would have shown different routes — the "insufficient support" / zero-propensity
problem), and Simpson's-paradox effects in offline replay. A/B testing is a
*late-stage* tool for picking between two already-reasonable weightings once you
have live users — not a way to *discover* weights from scratch.

**Revealed-preference route-choice modeling — the right primary approach.**
There is a directly applicable, peer-reviewed method using exactly your city:
Basu & Sevtsuk (2022), *"How do street attributes affect willingness-to-walk?"*,
estimated a **path-size logit route-choice model from ~11,165 real pedestrian
GPS trajectories in Boston** (and a companion SF study), recovering coefficients
for street attributes and converting them into interpretable
**"willingness-to-walk" trade-offs** (e.g. "pedestrians will walk X extra meters
to avoid attribute Y"). This is the gold-standard way to get *empirically
grounded* weights:
  1. Take observed pedestrian routes (GPS traces) between O–D pairs.
  2. For each, generate a choice set of plausible alternatives (your penalty
     method already does this).
  3. Fit a discrete-choice model (multinomial / path-size / mixed logit) where
     each route's utility is a weighted sum of your per-edge factors.
  4. The estimated coefficients **are your `FACTOR_WEIGHTS`**, now with standard
     errors and a willingness-to-walk interpretation you can put in the UI.
  This replaces "expert guess + 10-route check" with "fit to thousands of real
  choices," and it's the same factor-linear-utility structure you already have,
  so it drops into your cost function. The data hurdle is acquiring pedestrian
  GPS traces (the studies bought them from a third-party app vendor); a smaller
  starting point is your own logged routes once you have users, or a
  **stated-preference / conjoint survey** (below).

**Stated-preference / conjoint analysis — the bootstrap when you lack GPS
traces.** Show people pairs/sets of hypothetical route segments varying in
surface, road type, material, etc., and ask which they'd walk (or rate
willingness). A D-efficient conjoint design (used in the Varanasi/Kharagpur and
other walkability studies) lets you estimate the same utility coefficients from
a few hundred survey responses — no GPS data, no live users. **This is the most
practical *first* step to put your weights on an empirical footing**: it's
cheap, you control it, and it produces exactly the factor weights your model
consumes. You could even run it as a lightweight web task (the conjoint version
of Project Sidewalk).

**Inverse reinforcement learning (IRL) — the powerful-but-heavy frontier.**
Deep IRL (e.g. MEDIRL-IC, 2024; adversarial IRL for route choice, 2023) learns a
reward function over edges from observed trajectories without assuming a fixed
functional form, and can incorporate individual covariates (a frail walker vs a
jogger). It's strictly more expressive than logit and there's a literature
applying it to pedestrians in Boston specifically. But it needs lots of
trajectory data, is harder to interpret (you lose the clean "willingness-to-walk
in meters" story), and a learned non-linear reward doesn't drop into your
linear-weighted cost as cleanly. **Treat IRL as a later upgrade** if/when you
have abundant traces and the linear logit model is demonstrably leaving signal
on the table. Don't start here.

### 3.3 Recommended weight-learning strategy (concrete)

1. **Now (no users needed): run a stated-preference conjoint study.** Design ~15
   segment comparisons varying your existing factors; collect a few hundred
   responses; fit a multinomial logit; adopt the coefficients (normalized) as
   `FACTOR_WEIGHTS`. This immediately upgrades the road_type=4.5 / surface=2.0
   numbers from "expert judgment" to "fit to human preference data" and gives you
   error bars. It also directly tests the open Newmarket overrating problem —
   if people penalize industrial/arterial context more than your tags do, the
   fitted coefficients will say so.
2. **As soon as you have any users: log routes shown + chosen + (if possible)
   walked.** This is the substrate for everything downstream. Without logging you
   can never do off-policy evaluation later.
3. **Once you have trajectory data: fit a path-size logit (revealed
   preference).** Reuse your penalty-method alternatives as the choice set.
   Replace the conjoint weights with these. This is the Basu–Sevtsuk recipe and
   is the realistic endpoint for a one-person project — it's rigorous, published,
   and matches your model structure.
4. **Only after all that, and only to choose between two good weightings: A/B
   test online, with proper off-policy correction** (IPS-style propensity
   weighting) if you replay logs offline.
5. **IRL is optional and last** — pursue only if linear utility is provably
   insufficient and trace data is plentiful.

### 3.4 Cost-model improvements independent of weight-learning

Two model changes will move route quality more than any algorithm swap, and both
are already half-identified in your TODOs:

- **Add an elevation/grade factor.** Required for hilly cities (§2.3) and
  cheaply available from a DEM. It's also the factor most validated by the
  route-choice literature (pedestrians strongly avoid grade). This is probably
  the single highest-value *modeling* change on the board.
- **Add an arterial-proximity / environment factor** to fix the Newmarket
  overrating that a weight tweak alone can't (your own conclusion). A street's
  hostility isn't fully captured by `highway=*`; proximity to a high-speed
  arterial, presence of a buffer, and traffic volume are separate signals. Even
  a coarse "distance to nearest `highway=primary/secondary/trunk`" edge feature
  would help, and it's derivable from the graph you already have.

Both of these are *new edge features*, which is the recurring pattern: re-add a
weight only alongside the data that feeds it (your existing discipline). The
conjoint study in §3.3 should include these factors so you learn their weights
at the same time you learn the rest.

---

## Summary of recommendations

**RAM / multi-region:** Move off NetworkX/GraphML to a **CSR array structure +
memory-mapped per-city files**, with curved geometry and audit attributes in
separate lazily-loaded side files. This is a ~1–2 day effort (not "large") if you
keep geometry out of the routing structure, and it converts 2.2 GB/city into
~10s of MB/city — clearing the 1 GB host cap and making dozens of cities
feasible. Do it *before* adding cities.

**Cities:** Expand to **Washington, DC first** (best open condition data, flat),
then **Seattle** (ingest OpenSidewalks, validate against AccessMap), then **NYC**
(after CSR), then **San Francisco** (only after an elevation factor exists).
Treat **Project Sidewalk** (35 cities) as your portable condition-data source
where municipal inventories don't exist.

**Routing (most important):**
- *Algorithm:* keep A\* + penalty alternatives; add **ALT** if latency bites;
  reserve **CCH/CRP** for multi-city full-graph scale; prototype
  **Pareto/multi-objective** as a product experiment.
- *Weights:* **don't start with A/B testing** (no users, no clean outcome,
  off-policy pitfalls). Start with a **stated-preference conjoint study** to put
  weights on empirical footing now; graduate to a **revealed-preference path-size
  logit on GPS traces** (the Basu–Sevtsuk Boston recipe) once you have
  trajectories; keep IRL and online A/B as later-stage tools.
- *Model:* add an **elevation/grade factor** and an **arterial-proximity factor**
  — these will improve route quality more than any algorithm change, and they
  fix the two open problems (hilly cities, Newmarket overrating) directly.

---

## Sources

Projects & memory:
- [AccessMap — routable OSM pedestrian network (State of the Map US 2024)](https://openstreetmap.us/events/state-of-the-map-us/2024/accessmap-a-routable-network-based-on-osm-pedestrian-features/)
- [OpenSidewalks (UW DSSG)](https://uwescience.github.io/DSSG2016-Sidewalks/)
- [AccessMap launch — avoid hills, construction, barriers (UW News)](https://www.washington.edu/news/2017/02/01/new-route-finding-map-lets-seattle-pedestrians-avoid-hills-construction-accessibility-barriers/)
- [Taskar project — statewide accessible routes (CREATE @ UW)](https://create.uw.edu/taskar-project-helps-pedestrians-find-accessible-routes-all-over-washington-state/)
- [GraphHopper vs OSRM vs Valhalla — self-hosted engines compared (Pi Stack, 2026)](https://www.pistack.xyz/posts/2026-04-25-graphhopper-vs-osrm-vs-valhalla-self-hosted-routing-engines-guide-2026/)
- [FOSS routing engines overview (gis-ops)](https://github.com/gis-ops/tutorials/blob/master/general/foss_routing_engines_overview.md)
- [Reducing the memory footprint of OSM map (GraphHopper forum)](https://discuss.graphhopper.com/t/reducing-the-memory-footprint-of-osm-map/5853)
- [CSR / sparse graph representation (Network Data Science)](https://bdpedigo.github.io/networks-course/representing_networks.html)
- [scipy.sparse.csgraph — compressed sparse graph routines](https://docs.scipy.org/doc/scipy/reference/sparse.csgraph.html)

Cities & data:
- [Project Sidewalk (UW Makeability Lab)](https://makeabilitylab.cs.washington.edu/project/sidewalk/)
- [Cities lack consistent sidewalk data — crowdsourcing (Next City)](https://nextcity.org/urbanist-news/cities-crowdsourced-ai-sidewalk-data-project-sidewalk)
- [Data.gov sidewalk datasets](https://catalog.data.gov/dataset?tags=sidewalk&q=Sidewalks)
- [EPA National Walkability Index](https://catalog.data.gov/dataset/walkability-index)
- [Crowdsourcing and sidewalk data — OSM trustworthiness in the US (arXiv 2210.02350)](https://arxiv.org/abs/2210.02350)
- [Mapping sidewalks for pedestrian routing (State of the Map US 2017)](https://2017.stateofthemap.us/program/mapping-sidewalks-for-pedestrian-routing.html)

Routing algorithms & weight learning:
- [Customizable Route Planning (Microsoft Research)](https://www.microsoft.com/en-us/research/wp-content/uploads/2013/01/crp_web_130724.pdf)
- [Customizable Contraction Hierarchies — A Survey (arXiv 2502.10519)](https://arxiv.org/pdf/2502.10519)
- [Separator-Based Alternative Paths in CCH (ATMOS 2025)](https://drops.dagstuhl.de/entities/document/10.4230/OASIcs.ATMOS.2025.12)
- [Contraction Hierarchies (Wikipedia)](https://en.wikipedia.org/wiki/Contraction_hierarchies)
- [How do street attributes affect willingness-to-walk? Boston & SF GPS route choice (Basu & Sevtsuk, 2022)](https://www.sciencedirect.com/science/article/abs/pii/S0965856422001616)
- [A big data approach to pedestrian route choice — San Francisco](https://www.sciencedirect.com/science/article/abs/pii/S2214367X21000569)
- [Deep IRL for route choice with context-dependent rewards (arXiv 2206.10598)](https://arxiv.org/abs/2206.10598)
- [MEDIRL-IC — pedestrian route choice via max-entropy deep IRL (IEEE, 2024)](https://ieeexplore.ieee.org/document/10689250/)
- [A stated preference approach for measuring walking accessibility (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S1361920923002730)
- [Pedestrian preferences via conjoint experiments — two Indian cities (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2185556024000312)
- [Offline A/B testing for Recommender Systems (arXiv 1801.07030)](https://arxiv.org/pdf/1801.07030)
- [Counterfactual evaluation for recommendation systems (Eugene Yan)](https://eugeneyan.com/writing/counterfactual-evaluation/)
