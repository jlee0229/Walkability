"""
Composite per-edge walkability scoring.

This is where the independent metrics written onto each edge by
graph/build.py are finally combined into a single walkability score. The
enrichment pipeline deliberately keeps them separate (highway_score,
surface_score, surface_material_score, foot_access, plus their confidences)
so that the weighting can change here — or via the Streamlit sliders — without
rebuilding the graph.

Design
------
``edge_walkability`` returns a ``(walk_score, confidence)`` pair, both in
[0, 1], 1.0 = best. The two are returned separately, never pre-combined: the
routing layer turns walk_score into an edge cost (routing/cost.py) and uses
confidence only as a near-tie breaker between candidate routes
(routing/router.py).

Missing factors are dropped and the weights renormalised over whatever is
present. This mirrors build.py's care to keep ``None`` distinct from ``0.0``:
an edge with no surface data should not be scored as if its surface were the
worst possible — it should be scored on the factors we *do* know about.

This module is pure: no networkx, no I/O, no side effects. It takes a plain
edge-attribute dict (``G[u][v][key]``) so it can be unit-checked in isolation.
"""

from __future__ import annotations

from typing import Any

from walkability.scoring.weights import FACTOR_WEIGHTS


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
        ``surface_score``, ``surface_material_score``, ``surface_confidence``
        and ``foot_access``. Any of the surface/foot fields may be ``None``.
    weights :
        Relative factor weights. Defaults to ``FACTOR_WEIGHTS``; pass an
        override (e.g. from UI sliders) to re-weight without rebuilding.

    Returns
    -------
    (walk_score, confidence) :
        Both length-independent and in [0, 1]. ``walk_score`` is the
        weighted mean of the present factors' values; ``confidence`` is the
        weighted mean of those same factors' confidences, using the identical
        weights so the two stay comparable.

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

    # Each entry: (value, weight, confidence). Only factors with real data are
    # appended, so absent factors drop out of both weighted means.
    contributions: list[tuple[float, float, float]] = []

    highway_score = _as_float(edge.get("highway_score"))
    if highway_score is not None:
        w = weights.get("road_type", 0.0)
        conf = _as_float(edge.get("highway_confidence"))
        contributions.append((highway_score, w, conf if conf is not None else _DEFAULT_SURFACE_CONFIDENCE))

    surface_conf = _as_float(edge.get("surface_confidence"))
    surface_conf = surface_conf if surface_conf is not None else _DEFAULT_SURFACE_CONFIDENCE

    surface_score = _as_float(edge.get("surface_score"))
    if surface_score is not None:
        contributions.append((surface_score, weights.get("surface_quality", 0.0), surface_conf))

    material_score = _as_float(edge.get("surface_material_score"))
    if material_score is not None:
        contributions.append((material_score, weights.get("surface_material", 0.0), surface_conf))

    foot_value = FOOT_ACCESS_SCORE.get(_as_str(edge.get("foot_access")))
    if foot_value is not None:
        # foot access is an explicit categorical tag — confident when present.
        contributions.append((foot_value, weights.get("foot_access", 0.0), 1.0))

    total_weight = sum(w for _, w, _ in contributions)
    if total_weight <= 0.0:
        return _EMPTY_WALK, _EMPTY_CONFIDENCE

    walk = sum(value * w for value, w, _ in contributions) / total_weight
    confidence = sum(conf * w for _, w, conf in contributions) / total_weight
    return walk, confidence
