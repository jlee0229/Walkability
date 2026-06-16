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
# Factor weights  (will be user-adjustable in the Streamlit UI)
# ---------------------------------------------------------------------------
# Each key maps to a scoring factor in scoring/factors.py. Values are relative
# weights — scoring/factors.py renormalises them over whichever factors are
# actually present on an edge, so the raw values here are easy to reason about
# and missing data never silently penalises an edge.
#
# Ordering reflects what a pedestrian experiences most directly: the road
# type they walk along dominates, then the surface underfoot (its structural
# condition and its material comfort, weighted equally), then explicit foot
# access as a soft signal.
#
# road_type is set ABOVE the COMBINED surface weight (surface_quality +
# surface_material = 4.0, so road_type = 4.5). This was confirmed by the
# 10-route ground-truth survey (notebooks/ground_truth.csv): with the earlier
# 3.0, the two surface factors summed to 4.0 > 3.0 and collectively outvoted
# the road type, so a pristine sidewalk in a hostile environment (e.g. the
# Newmarket industrial route) over-scored. Keeping road_type just above the
# surface sum makes the environment the primary signal while still letting
# surface meaningfully move the score.
#
# foot_access is ALSO a hard routing constraint (foot=no excludes the edge,
# foot=private penalises its cost) — see routing/cost.py. The weight here is
# only the soft preference folded into the composite walkability score.
#
# Removed pending edge data: crossing_quality, poi_density, elevation_change.
# These were placeholders — no enrichment tier in graph/build.py produces them
# yet, so scoring on them is impossible. Re-add with matching edge fields once
# that data exists.

FACTOR_WEIGHTS: dict[str, float] = {
    "road_type":         4.5,   # edge["highway_score"] — slightly above the surface SUM (4.0)
    "surface_quality":   2.0,   # edge["surface_score"] — structural condition (SCI)
    "surface_material":  2.0,   # edge["surface_material_score"] — intrinsic material comfort
    "foot_access":       1.0,   # edge["foot_access"] — soft signal (hard rule lives in cost.py)
}


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