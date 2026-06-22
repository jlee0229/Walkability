"""
Walkability scoring weights and priority orderings.

This is the single source of truth for all scored/ranked values.

Design principle
----------------
Numerical scores are defined once. Priority orderings (used by the tag
resolver when picking between competing OSM tag values) are derived
automatically by sorting on those scores — highest score first.

This means there is only one thing to edit when you want to re-rank a
highway type or surface: change its score. The priority list updates
itself, and tag_resolver.py (which imports the derived list) picks up
the change with no further edits.
"""

# ---------------------------------------------------------------------------
# Highway scores
# ---------------------------------------------------------------------------
# Higher score = more walkable. Range 0.0–1.0.
#
# NOTE: "steps" is intentionally absent. It is always handled as a
# special case by tag_resolver.py because it affects routing geometry
# and accessibility rather than being a point on the walkability spectrum.

HIGHWAY_SCORES: dict[str, float] = {
    "pedestrian":    1.00,  # Fully pedestrianised street/plaza
    "footway":       0.90,  # Dedicated foot path
    "path":          0.80,  # Generic path (often shared use)
    "living_street": 0.70,  # Shared surface, very low vehicle speeds
    "residential":   0.65,  # Local residential street (calibration: 25 mph / 1–2 lanes,
                            # a normal neighbourhood sidewalk — 0.55 undersold it)
    "unclassified":  0.45,  # Minor road, no specific classification
    "service":       0.35,  # Access roads, parking aisles
    "tertiary":      0.25,  # Local connector road
    "secondary":     0.15,  # Moderate-traffic road
    "primary":       0.08,  # High-traffic arterial
    "trunk":         0.03,  # Near-motorway speeds
    "motorway":      0.00,  # Should rarely appear in a walk graph
}

# Derived: sorted highest score → lowest. This is what tag_resolver.py
# uses to pick the most walkable type when an edge has multiple values.
HIGHWAY_PRIORITY: list[str] = sorted(
    HIGHWAY_SCORES, key=HIGHWAY_SCORES.__getitem__, reverse=True
)

# ---------------------------------------------------------------------------
# Surface scores
# ---------------------------------------------------------------------------
# Higher score = better walking surface. Range 0.0–1.0.

SURFACE_SCORES: dict[str, float] = {
    "asphalt":       1.00,
    "concrete":      0.90,
    "paved":         0.80,  # Generic paved (unspecified material)
    "paving_stones": 0.70,
    "compacted":     0.55,
    "fine_gravel":   0.40,
    "gravel":        0.30,
    "unpaved":       0.20,
    "dirt":          0.15,
    "grass":         0.10,
    "sand":          0.05,
    "mud":           0.00,
}

# Derived: sorted highest score → lowest.
SURFACE_PRIORITY: list[str] = sorted(
    SURFACE_SCORES, key=SURFACE_SCORES.__getitem__, reverse=True
)

# ---------------------------------------------------------------------------
# Highway distinctiveness scores
# ---------------------------------------------------------------------------
# Measures how strongly a highway type signals its own category — i.e.
# how much evidential weight it should carry when inferring the type of
# a nearby untagged edge.
#
# This is SEPARATE from walkability scores and goes in the opposite
# direction: high-traffic roads (primary, trunk) are rare and specific,
# so seeing one nearby is a strong signal. Residential roads are
# ubiquitous, so they are weak evidence — an untagged road between two
# residential edges could easily be something else entirely.
#
# Used exclusively by osm/fallback.py for context inference.
# NOT used by the scoring layer.

HIGHWAY_DISTINCTIVENESS: dict[str, float] = {
    "motorway":      1.00,  # Unmistakable; extremely specific
    "trunk":         0.90,  # Near-motorway; rare in urban cores
    "primary":       0.80,  # Major arterial; strong signal
    "secondary":     0.70,  # Moderate-traffic; fairly specific
    "living_street": 0.65,  # Deliberately designed; specific enough
    "tertiary":      0.50,  # Common but still meaningful
    "service":       0.40,  # Ambiguous (could be many things)
    "pedestrian":    0.35,  # Not used as evidence, included for completeness
    "footway":       0.35,  # Not used as evidence, included for completeness
    "path":          0.30,  # Not used as evidence, included for completeness
    "unclassified":  0.15,  # By definition generic; very weak signal
    "residential":   0.20,  # Extremely common; weak signal
}

# ---------------------------------------------------------------------------
# Factor weights and category structure  (scoring/factors.py)
# ---------------------------------------------------------------------------
# Walkability is scored in TWO LEVELS (Human Development Index style), so a
# failure in one DIMENSION of walking cannot be bought back by excellence in
# another (a pristine surface must not rescue a walk along a highway):
#
#   1. WITHIN a category — a weighted ARITHMETIC mean of the present factors.
#      Factors in the same category are SUBSTITUTABLE (good structural condition
#      offsets so-so material). FACTOR_WEIGHTS below are these WITHIN-category
#      relative weights, renormalised over whatever factors are present.
#   2. ACROSS categories — an importance-WEIGHTED (CATEGORY_WEIGHTS) GEOMETRIC
#      mean of the category values, each floored to [CATEGORY_FLOOR, 1].
#      Categories are NON-SUBSTITUTABLE, so a single weak category dominates. The
#      floor stops one zero category (motorway road_type, foot=no, on-arterial
#      environment) from annihilating all discrimination — exactly as HDI bounds
#      each dimension above zero.
#
# CATEGORY_MAP assigns every factor to one of three dimensions — "Will I be safe,
# will it be easy, is it a real walking route?":
#   safety  : environment (= arterial proximity × eyes-on-street)
#   comfort : surface condition, surface material, sidewalk width
#   path    : road type, foot access
#
# This SUPERSEDES the earlier flat-mean tuning where road_type (4.5) was set just
# above the surface SUM (4.0). Non-substitutability is now STRUCTURAL (the
# geometric mean), so the cross-factor balance no longer lives in these numbers —
# they only set relative weight WITHIN a category. Changing the structure or these
# weights requires a --force rebuild (baked walk_score) + re-baseline.
#
# foot_access is ALSO a hard routing constraint (foot=no excludes the edge,
# foot=private penalises its cost) — see routing/cost.py. The weight here is only
# the soft preference folded into the Path category.
#
# Removed pending edge data: crossing_quality, poi_density, elevation_change —
# no enrichment tier produces them yet (elevation_change deferred until a hilly
# target city; Research/break_research_2026-06-17.md §2.3). New factors slot into
# a category in CATEGORY_MAP once their edge field exists.

FACTOR_WEIGHTS: dict[str, float] = {
    # Path legitimacy — is this a real walking route?
    "road_type":         3.0,   # edge["highway_score"] — dominant within Path
    "foot_access":       1.0,   # edge["foot_access"] — soft signal (hard rule in cost.py)
    # Safety — will cars or strangers harm me? (sole factor for now)
    "environment":       1.0,   # edge["environment_score"] — arterial proximity × eyes
    # Comfort — is it physically easy and pleasant underfoot?
    "surface_quality":   2.0,   # edge["surface_score"] — structural condition (SCI)
    "surface_material":  2.0,   # edge["surface_material_score"] — material comfort
    "surface_width":     1.0,   # edge["width_score"] — sidewalk room (city data; often absent)
}

# Factor → category. Single source of truth for the two-level aggregation in
# scoring/factors.py::edge_walkability.
CATEGORY_MAP: dict[str, str] = {
    "road_type":        "path",
    "foot_access":      "path",
    "environment":      "safety",
    "surface_quality":  "comfort",
    "surface_material": "comfort",
    "surface_width":    "comfort",
}

# Lower bound on a category value before the cross-category geometric mean, so a
# single zero category punishes hard without zeroing the whole score. STARTING
# value — tune against notebooks/ground_truth.csv. NOTE: this is the zero-collapse
# safety valve, NOT an importance dial — to make a category matter more/less use
# CATEGORY_WEIGHTS below (a high floor would clip and destroy discrimination).
CATEGORY_FLOOR: float = 0.15

# Cross-category IMPORTANCE weights for the geometric mean. Only RATIOS matter;
# equal weights reproduce a plain (HDI-style) geometric mean. Per ground-truth:
# Safety = Path legitimacy ≥ Comfort. Safety is deliberately NOT set above Path —
# over-indexing on safety is the classic failure of prior walkability models (a
# calm dangerous-looking street still gets you there), and safety/path already
# share a car-danger signal so up-weighting safety would double-count it. Comfort
# is the gradient "nice-to-have". Tune against notebooks/ground_truth.csv.
CATEGORY_WEIGHTS: dict[str, float] = {
    "safety":  1.0,
    "path":    1.0,
    "comfort": 0.7,
}

# Sidewalk width → comfort score ramp (feet). Below MIN ≈ no buffer / forced
# single-file; at/above GOOD ≈ comfortable two-abreast. Linear between. From the
# city inventory (SWK_WIDTH) only — absent on most edges, so it drops out where
# unknown (never penalises an edge for missing width).
SIDEWALK_WIDTH_MIN_FT:  float = 3.0   # ramp start → score 0.0
SIDEWALK_WIDTH_GOOD_FT: float = 8.0   # ramp end   → score 1.0


# ---------------------------------------------------------------------------
# Environment factor parameters  (graph/environment.py — single source of truth)
# ---------------------------------------------------------------------------
# The environment factor (= the SAFETY dimension) is `car_safety × eyes`, combined
# as a GEOMETRIC mean so it is high only when BOTH are high. car_safety itself
# decomposes into two NON-overlapping signals combined by min() ("weakest link"),
# so they never double-count:
#
#   ON-PATH  (maxspeed_safety) — danger from the road you walk ALONG, from its
#     maxspeed. A pedestrian-dedicated way (footway/pedestrian/path) carries no
#     through traffic → 1.0; a road uses its maxspeed tag, or a class default.
#   OFF-PATH (arterial_proximity_score) — danger from a nearby fast road you are
#     NOT on (footways / quiet streets beside an arterial). Computed ONLY for
#     non-arterial edges — an arterial's own danger is already on-path — so the
#     two signals are orthogonal by construction. The walk graph excludes
#     motorway/trunk, so arterial geometry is pulled separately.
#
#   car_safety = min(on_path, off_path);  environment = sqrt(car_safety × eyes).
#
# This SUPERSEDES the earlier arterial class-floor hack (a class-as-speed proxy):
# real maxspeed now rates a calm 25 mph secondary (Newbury, Comm Ave) as safe to
# walk along instead of penalising it for its class.

# maxspeed (mph) → on-path car-safety [0,1], piecewise-linear between anchors.
# Anchored to pedestrian crash-fatality risk + comfort walking alongside traffic
# (Tefft 2011 / AAA: fatality risk ~10% at 23 mph, ~25% at 32, ~50% at 42), so it
# is UNIVERSAL — a 30 mph street is genuinely less safe than a 25 mph one wherever
# that feels normal.
MAXSPEED_SAFETY_ANCHORS: list[tuple[float, float]] = [
    (20.0, 1.00), (25.0, 0.95), (30.0, 0.70),
    (35.0, 0.45), (40.0, 0.25), (45.0, 0.12), (50.0, 0.05),
]

# Default speed (mph) by highway class — the on-path score when an edge has no
# maxspeed tag, and the source of each arterial's off-path "hostility".
DEFAULT_MAXSPEED_MPH: dict[str, float] = {
    "living_street": 10.0, "service": 15.0, "residential": 25.0,
    "unclassified":  25.0, "tertiary": 30.0, "secondary": 30.0,
    "primary": 35.0, "trunk": 45.0, "motorway": 60.0,
}

# Pedestrian-dedicated ways carry no through traffic → on-path safety 1.0.
PEDESTRIAN_HIGHWAYS: frozenset[str] = frozenset({
    "pedestrian", "footway", "path", "steps",
})

# Off-path REACH (m) per arterial class — how far its threat extends to a nearby
# pedestrian. Its hostility (depth of penalty) comes from DEFAULT_MAXSPEED_MPH via
# the maxspeed curve, so a faster road both reaches further and penalises harder.
ARTERIAL_REACH_M: dict[str, float] = {
    "motorway": 150.0, "trunk": 150.0, "primary": 80.0, "secondary": 50.0,
}

# OSM highway tag values to pull as "arterials" (base classes + their link ramps).
ARTERIAL_HIGHWAY_TAGS: list[str] = [
    *ARTERIAL_REACH_M.keys(),
    *(f"{k}_link" for k in ARTERIAL_REACH_M),
]

# PERCEIVED SAFETY ("eyes_score" field) — "do I feel safe from people here?".
# A probabilistic OR (noisy-OR: 1 − ∏(1−s)) of three SUBSTITUTABLE signals: you
# feel safe if ANY is strong, and unsafe only when you lack ALL three (the
# isolated, enclosed, empty back alley). Having more than one is a slight
# improvement, not a requirement. Each signal is a saturating curve 1−exp(−x/sat):
#   activity   — active frontage: foot-traffic POIs nearby (shops, restaurants),
#                weighted so street furniture / parking (POI_NOISE_AMENITIES) don't
#                count. ~57% of raw OSM "POIs" are benches/parking — pure noise.
#   enclosure  — buildings facing the street (homes = eyes). Dropped for
#                alley/service edges, whose buildings face away.
#   openness   — adjacency to a large open space (park / water). A wide waterfront
#                promenade or a park edge feels safe through openness, sightlines,
#                and the people such places draw, even with few buildings or shops
#                (the Seaport HarborWalk). This DISCRIMINATES, unlike raw footway
#                density (which is ~universal in a city): only ~22% of edges sit
#                near meaningful open space, and 0% of the industrial routes do.
EYES_BUFFER_M:         float = 30.0   # radius for POIs / buildings
EYES_POI_SAT:          float = 3.0    # ~3 foot-traffic POIs ≈ a lively block
EYES_BLDG_SAT:         float = 7.0    # ~7 buildings ≈ a built-up block
OPENSPACE_MIN_AREA_M2: float = 5000.0 # ignore pocket parks/playgrounds; keep real open space
OPENNESS_REACH_M:      float = 50.0   # openness ramps 1 (adjacent) → 0 at this distance

# amenity values that are street furniture / parking, NOT foot-traffic — weighted
# 0 in `activity`. Any OTHER amenity, and every shop, counts as active frontage.
POI_NOISE_AMENITIES: frozenset[str] = frozenset({
    "bench", "waste_basket", "bicycle_parking", "parking", "parking_space",
    "parking_entrance", "motorcycle_parking", "vending_machine", "drinking_water",
    "fountain", "recycling", "post_box", "telephone", "charging_station",
    "bicycle_repair_station", "grit_bin", "clock", "shelter", "bbq", "give_box",
    "hunting_stand", "waste_disposal", "sanitary_dump_station", "loading_dock",
})

# Confidence assigned to the environment factor. It is derived from dense OSM
# data via a documented heuristic — more trustworthy than a guess, less than a
# field survey. Used only as a near-tie breaker between routes (routing/router.py).
ENV_CONFIDENCE: float = 0.7


# Route-level aggregation exponent for the length-weighted power mean that
# summarises a whole route's walk_score (routing/router.py:_build_route). A
# route is only as good as its worst stretch, so the aggregate must be sensitive
# to a single low-scoring block rather than averaging it away.
#
#   p = 1   → ordinary length-weighted arithmetic mean (no worst-segment bias)
#   p < 1   → low scores pull the aggregate down harder than high scores lift it
#   p → 0   → approaches the geometric mean
#
# Kept strictly > 0 so a single 0.0 edge can't zero out the entire route. 0.5 is
# a deliberately mild worst-segment penalty — tune downward to lean harder
# toward the worst edge once the broader scoring pass happens.
ROUTE_SCORE_EXPONENT: float = 0.5