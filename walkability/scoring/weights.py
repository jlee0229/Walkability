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
    "residential":   0.55,  # Local residential street
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
# The environment factor combines two independent sub-signals, multiplied as a
# GEOMETRIC mean so the composite is high only when BOTH are high (a quiet street
# next to an expressway, or a back alley with no eyes, both collapse toward 0):
#
#   1. arterial_proximity_score — car safety. Class-floored on/adjacent to the
#      arterial (0 for a motorway, ~0.45 for a calm secondary), ramping to 1
#      beyond its reach; pedestrian-dedicated ways are exempt (PED_ARTERIAL_FLOOR).
#      The walk graph EXCLUDES motorway/trunk, so arterial geometry is pulled
#      separately (graph/download_environment.py).
#   2. eyes_score — perceived social safety ("eyes on the street", Jacobs).
#      Driven by active frontage (shops/amenities) and built enclosure
#      (buildings) near the edge, knocked down for back-alley geometry.
#
# All values below are STARTING heuristics — the eyes_score formula in
# particular needs tuning against notebooks/ground_truth.csv, since OSM does not
# cheaply expose building-entrance orientation.

# Arterial classes → (reach_m, floor). REACH = distance over which the road stops
# mattering. FLOOR = arterial_proximity_score when you're right on it — NOT every
# arterial is equally hostile: a motorway is a wall of fast traffic (floor 0), but
# a secondary urban street (Newbury, Tremont, Comm Ave) is calm enough to walk
# along (floor ~0.45), so it should not crater car-safety the way the old "0 on
# any arterial" rule did. This was the dominant calibration error: beloved walks
# (Comm Ave Mall) scored arterial≈0.09 purely from a nearby secondary roadway.
# Until a real traffic-SPEED factor exists, the floor encodes class-as-speed-proxy.
# _link ramps inherit their base class (resolved in graph/environment.py).
ARTERIAL_CLASSES: dict[str, tuple[float, float]] = {
    "motorway":  (150.0, 0.00),
    "trunk":     (150.0, 0.00),
    "primary":   (80.0,  0.25),
    "secondary": (50.0,  0.45),
}

# A pedestrian-DEDICATED way is physically protected from traffic, so a nearby
# CALM road shouldn't crush its car-safety (the Paul Revere Footpath scored 0.10):
# such edges get at least this arterial_proximity_score floor. BUT the exemption
# only applies next to a calm arterial (secondary or quieter, floor ≥
# PED_EXEMPT_MIN_FLOOR) — a footway hugging a roaring primary/expressway still
# feels the traffic (Newmarket's HarborWalk-style paths were over-credited).
PED_ARTERIAL_FLOOR: float = 0.6
PED_EXEMPT_MIN_FLOOR: float = 0.45
PEDESTRIAN_HIGHWAYS: frozenset[str] = frozenset({
    "pedestrian", "footway", "path", "steps", "living_street",
})

# OSM highway tag values to pull as "arterials" (base classes + their link ramps).
ARTERIAL_HIGHWAY_TAGS: list[str] = [
    *ARTERIAL_CLASSES.keys(),
    *(f"{k}_link" for k in ARTERIAL_CLASSES),
]

# eyes_score: counts of nearby POIs / buildings within EYES_BUFFER_M of the edge
# are passed through a saturating curve (1 - exp(-count / sat)) so the first few
# matter most, then blended. BUILDING presence now leads (and saturates sooner):
# a residential neighbourhood with homes facing the street feels watched and safe
# even with no shops — the old POI-led weighting under-scored exactly those
# (ground truth: residential blocks felt safer than the model said). POIs remain a
# bonus on top for active commercial frontage.
EYES_BUFFER_M:    float = 30.0   # how far from the edge we look for frontage/buildings
EYES_POI_SAT:     float = 3.0    # ~3 POIs nearby ≈ a lively block
EYES_BLDG_SAT:    float = 7.0    # ~7 buildings nearby ≈ a built-up residential block
EYES_W_POI:       float = 0.45   # POI (active frontage) weight  (POI + BLDG = 1.0)
EYES_W_BLDG:      float = 0.55   # building (enclosure / homes = eyes) weight
EYES_ALLEY_FACTOR: float = 0.4   # multiplier for service=alley / back-alley edges

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