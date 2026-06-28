"""
Composite per-edge walkability scoring.

This is where the independent metrics written onto each edge by
graph/build.py are finally combined into a single walkability score. The
enrichment pipeline deliberately keeps them separate (highway_score,
surface_score, surface_material_score, foot_access, plus their confidences)
so that the weighting can change here — or via the Streamlit sliders — without
rebuilding the graph.

Design — two-level (HDI-style) aggregation
------------------------------------------
``edge_walkability`` returns a ``(walk_score, confidence)`` pair, both in
[0, 1], 1.0 = best. The two are returned separately, never pre-combined: the
routing layer turns walk_score into an edge cost (routing/cost.py) and uses
confidence only as a near-tie breaker between candidate routes
(routing/router.py).

walk_score is built in two levels, mirroring the Human Development Index, so a
failure in one DIMENSION of walking cannot be bought back by excellence in
another (a pristine surface must not rescue a walk along a highway):

  1. WITHIN a category (``CATEGORY_MAP``) — a weighted ARITHMETIC mean of the
     present factors (factors there are substitutable), clamped to
     [``CATEGORY_FLOOR``, 1].
  2. ACROSS categories — an importance-weighted (``CATEGORY_WEIGHTS``) GEOMETRIC
     mean of the category values (categories are non-substitutable; one weak
     category dominates, and Safety/Path outweigh Comfort). The floor keeps a
     single zero category from annihilating all discrimination.

Missing factors are dropped and the within-category weights renormalised over
whatever is present; an entirely-empty category is dropped from the geometric
mean (never imputed). This mirrors build.py's care to keep ``None`` distinct
from ``0.0``. confidence stays a plain weight-weighted arithmetic mean over the
present factors (NOT power-/geometric-meaned) — it is only a tiebreaker.

This module is pure: no networkx, no I/O, no side effects. It takes a plain
edge-attribute dict (``G[u][v][key]``) so it can be unit-checked in isolation.
"""

from __future__ import annotations

import math
from typing import Any

from walkability.scoring.weights import (
    CATEGORY_FLOOR,
    CATEGORY_MAP,
    CATEGORY_WEIGHTS,
    COMFORT_COMPRESS_CATEGORY,
    COMFORT_COMPRESS_K,
    COMFORT_COMPRESS_KNEE,
    FACTOR_WEIGHTS,
)


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
# GraphML stores everything as strings, and ox.load_graphml only auto-casts the
# attributes it knows about — our custom scores come back as "0.55", and a None
# may arrive as a genuine None, an absent key, or the literal string "None".
# factors.py is the boundary where edge metrics enter scoring, so it normalises
# here once rather than scattering casts through the routing layer.

def _as_float(value: Any) -> float | None:
    """Coerce a GraphML attribute to float, or None if missing/unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s == "" or s.lower() == "none":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _as_str(value: Any) -> str | None:
    """Coerce a GraphML attribute to a clean string, or None for missing/'None'."""
    if value is None:
        return None
    s = str(value).strip()
    return None if (s == "" or s.lower() == "none") else s

# ---------------------------------------------------------------------------
# Foot-access classification — single source of truth (shared with cost.py)
# ---------------------------------------------------------------------------
# Boston OSM uses a wide vocabulary of foot/access values. We group them into
# three classes, applied two ways:
#   - here: a soft walkability contribution via FOOT_ACCESS_SCORE
#   - in routing/cost.py: a hard rule (exclude vs. penalise) using the sets below
#
# Philosophy (matches osm/tag_resolver): when in doubt, allow and penalise
# rather than exclude — over-excluding disconnects the graph. Only an explicit
# foot=no is treated as truly impassable.

# Pedestrians are welcome — positive signal.
_ALLOWED_FOOT_ACCESS: dict[str, float] = {
    "yes":        1.0,
    "designated": 1.0,
    "official":   1.0,
    "permissive": 0.8,
}

# Walkable in practice but gated behind permission / a role you may not have
# (customer, resident, employee, permit-holder, …). Penalised in cost.py and
# scored 0.0 here. "customers" alone is ~1,100 edges in the Boston graph.
RESTRICTED_FOOT_ACCESS: frozenset[str] = frozenset({
    "private", "permit", "customers", "residents", "destination",
    "employees", "delivery", "agricultural", "forestry", "military", "emergency",
})

# Cannot legally walk — excluded from the routable graph entirely.
EXCLUDED_FOOT_ACCESS: frozenset[str] = frozenset({"no"})

# Map a resolved foot-access tag to a soft walkability contribution.
# Anything not listed (None, "unknown", "service", …) is intentionally absent:
# the foot_access factor is then dropped and the remaining weights renormalised,
# rather than guessing a neutral score. EXCLUDED/RESTRICTED values also trigger
# hard handling in routing/cost.py — this dict is only the soft signal.
FOOT_ACCESS_SCORE: dict[str, float] = {
    **_ALLOWED_FOOT_ACCESS,
    **{v: 0.0 for v in RESTRICTED_FOOT_ACCESS},
    **{v: 0.0 for v in EXCLUDED_FOOT_ACCESS},
}

# Confidence assumed for a surface score when build.py left surface_confidence
# as None (e.g. the OSM-tag tier provides a default surface score but no
# survey-based confidence). Mid-scale: better than a guess, worse than a
# field survey.
_DEFAULT_SURFACE_CONFIDENCE: float = 0.5

# Returned when an edge somehow carries no scorable factor at all. Matches the
# geometric-fallback floor in osm/fallback.py so behaviour is consistent.
_EMPTY_WALK: float = 0.40
_EMPTY_CONFIDENCE: float = 0.10


def _edge_contributions(
    edge: dict, weights: dict[str, float]
) -> list[tuple[str, float, float, float]]:
    """Per-factor ``(key, value, weight, confidence)`` for the present factors.

    The single place edge fields are read into scoring — shared by
    ``edge_walkability`` and ``edge_category_scores`` so they never drift.
    Zero/negative-weight factors are dropped (a UI slider at 0 = "ignore it").
    """
    contributions: list[tuple[str, float, float, float]] = []

    highway_score = _as_float(edge.get("highway_score"))
    if highway_score is not None:
        conf = _as_float(edge.get("highway_confidence"))
        contributions.append(("road_type", highway_score, weights.get("road_type", 0.0),
                              conf if conf is not None else _DEFAULT_SURFACE_CONFIDENCE))

    surface_conf = _as_float(edge.get("surface_confidence"))
    surface_conf = surface_conf if surface_conf is not None else _DEFAULT_SURFACE_CONFIDENCE

    surface_score = _as_float(edge.get("surface_score"))
    if surface_score is not None:
        contributions.append(("surface_quality", surface_score, weights.get("surface_quality", 0.0), surface_conf))

    material_score = _as_float(edge.get("surface_material_score"))
    if material_score is not None:
        contributions.append(("surface_material", material_score, weights.get("surface_material", 0.0), surface_conf))

    width_score = _as_float(edge.get("width_score"))
    if width_score is not None:
        contributions.append(("surface_width", width_score, weights.get("surface_width", 0.0), surface_conf))

    # Environment: arterial proximity × eyes-on-street (graph/environment.py).
    # Carries its own confidence (a documented heuristic over dense OSM data).
    environment_score = _as_float(edge.get("environment_score"))
    if environment_score is not None:
        env_conf = _as_float(edge.get("environment_confidence"))
        contributions.append(("environment", environment_score, weights.get("environment", 0.0),
                              env_conf if env_conf is not None else _DEFAULT_SURFACE_CONFIDENCE))

    foot_value = FOOT_ACCESS_SCORE.get(_as_str(edge.get("foot_access")))
    if foot_value is not None:
        # foot access is an explicit categorical tag — confident when present.
        contributions.append(("foot_access", foot_value, weights.get("foot_access", 0.0), 1.0))

    return [c for c in contributions if c[2] > 0.0]


def _category_values(
    contributions: list[tuple[str, float, float, float]]
) -> dict[str, float]:
    """Level-1 aggregate: floored weighted-arithmetic mean within each category.

    Factors with no mapped category (``CATEGORY_MAP``) are skipped — a new factor
    must be added to the map to count.
    """
    cat_factors: dict[str, list[tuple[float, float]]] = {}
    for key, value, w, _ in contributions:
        category = CATEGORY_MAP.get(key)
        if category is not None:
            cat_factors.setdefault(category, []).append((value, w))

    out: dict[str, float] = {}
    for category, items in cat_factors.items():
        tw = sum(w for _, w in items)
        cat_mean = sum(v * w for v, w in items) / tw  # tw > 0: zero-weight dropped above
        out[category] = min(1.0, max(CATEGORY_FLOOR, cat_mean))
    return out


def compress_comfort(category_values: dict[str, float]) -> dict[str, float]:
    """Top-compress the comfort dimension above ``COMFORT_COMPRESS_KNEE``.

    City SCI/material run optimistic, so comfort saturates near-ceiling on ordinary
    streets and biases the score high; this trims that overshoot without touching
    safety-gated routes. ``K``=1.0 (or value ≤ knee) is a no-op. See weights.py.

    Applied to a finalized per-aggregate dimension dict — the edge ``category_values``
    (``edge_walkability``) and the route ``dimension_scores``
    (``router._aggregate_route_dimensions``) — *before* ``combine_categories``, so the
    compressed value is the one both the score and the exposed ``dimension_scores``
    bars use. NOT applied to per-edge ``edge_category_scores`` (raw), so the route's
    per-dimension power mean is over raw comfort and the trim lands once on the
    aggregate. Returns the input unchanged when comfort is absent / below the knee.
    """
    v = category_values.get(COMFORT_COMPRESS_CATEGORY)
    if v is None or COMFORT_COMPRESS_K >= 1.0 or v <= COMFORT_COMPRESS_KNEE:
        return category_values
    out = dict(category_values)
    out[COMFORT_COMPRESS_CATEGORY] = (
        COMFORT_COMPRESS_KNEE + COMFORT_COMPRESS_K * (v - COMFORT_COMPRESS_KNEE)
    )
    return out


def combine_categories(category_values: dict[str, float]) -> float:
    """Level-2 aggregate: importance-weighted GEOMETRIC mean across categories.

    The cross-category combine shared by the edge aggregate (``edge_walkability``)
    and the route aggregate (``routing/router.py::_build_route``), so the two can
    never drift. Each ``category_values`` entry is assumed already floored to
    ``[CATEGORY_FLOOR, 1]`` **and comfort-compressed** (``compress_comfort``, applied
    by the callers so the exposed dimension values match the score). Absent
    categories simply don't contribute. Weights come from ``CATEGORY_WEIGHTS``
    (importance — Safety ≥ Path > Comfort). Non-substitutable: one weak dimension
    dominates.
    """
    cat_wsum = sum(CATEGORY_WEIGHTS.get(c, 1.0) for c in category_values)
    return math.exp(
        sum(CATEGORY_WEIGHTS.get(c, 1.0) * math.log(v) for c, v in category_values.items())
        / cat_wsum
    )


def edge_category_scores(
    edge: dict, weights: dict[str, float] = FACTOR_WEIGHTS
) -> dict[str, float]:
    """The floored per-category (safety/comfort/path) values for one edge.

    The Level-1 half of ``edge_walkability``, exposed for diagnostics and
    calibration (e.g. notebooks/calibration_survey.py) so the cross-category
    geometric mean can be inspected dimension by dimension. Absent categories are
    omitted. Does not use the baked fast path.
    """
    return _category_values(_edge_contributions(edge, weights))


def edge_walkability(
    edge: dict,
    weights: dict[str, float] = FACTOR_WEIGHTS,
) -> tuple[float, float]:
    """Combine an edge's metrics into ``(walk_score, confidence)`` in [0, 1].

    Parameters
    ----------
    edge :
        An edge-attribute dict as stored on the enriched graph
        (``G[u][v][key]``). Reads ``highway_score``, ``highway_confidence``,
        ``surface_score``, ``surface_material_score``, ``surface_confidence``,
        ``environment_score``, ``environment_confidence`` and ``foot_access``.
        Any of the surface/environment/foot fields may be ``None``.
    weights :
        Relative factor weights. Defaults to ``FACTOR_WEIGHTS``; pass an
        override (e.g. from UI sliders) to re-weight without rebuilding.

    Reads ``highway_score``/``highway_confidence``, ``surface_score``,
    ``surface_material_score``, ``width_score``, ``surface_confidence``,
    ``environment_score``/``environment_confidence`` and ``foot_access``.

    Returns
    -------
    (walk_score, confidence) :
        Both length-independent and in [0, 1]. ``walk_score`` is the two-level
        aggregate (weighted arithmetic mean within each category of
        ``CATEGORY_MAP``, floored to ``CATEGORY_FLOOR``, then an importance-weighted
        (``CATEGORY_WEIGHTS``) geometric mean across the present categories).
        ``confidence`` is a plain
        weight-weighted arithmetic mean of the present factors' confidences.

    Fast path
    ---------
    When called with the default weights (the literal ``FACTOR_WEIGHTS``
    object), a precomputed ``walk_score``/``walk_confidence`` baked onto the
    edge by graph/build.py is used directly — skipping all string parsing and
    the weighted combine. Passing any other ``weights`` object (e.g. from UI
    sliders) forces a full recompute, so re-weighting without a rebuild still
    works. Edges built before the bake simply lack the field and recompute.
    """
    if weights is FACTOR_WEIGHTS:
        cached = _as_float(edge.get("walk_score"))
        if cached is not None:
            conf = _as_float(edge.get("walk_confidence"))
            return cached, conf if conf is not None else _EMPTY_CONFIDENCE

    # Read present factors (drops zero-weight) — shared with edge_category_scores.
    contributions = _edge_contributions(edge, weights)
    if not contributions:
        return _EMPTY_WALK, _EMPTY_CONFIDENCE

    # Level 1: floored weighted-arithmetic mean within each category.
    category_values = _category_values(contributions)
    if not category_values:
        return _EMPTY_WALK, _EMPTY_CONFIDENCE

    # Level 2: importance-WEIGHTED (CATEGORY_WEIGHTS) GEOMETRIC mean across the
    # present categories (comfort top-compressed first). Absent categories drop.
    walk = combine_categories(compress_comfort(category_values))

    # Confidence stays a plain weight-weighted arithmetic mean (tiebreaker only).
    total_weight = sum(w for _, _, w, _ in contributions)
    confidence = sum(conf * w for _, _, w, conf in contributions) / total_weight
    return walk, confidence
