"""
Geometric and tag-based fallback for OSM edges.

Position in the pipeline
------------------------
This module sits at the bottom of the tiered data pipeline:

    Boston sidewalk data   (highest confidence — field-surveyed)
         ↓ if no spatial match
    OSM tag resolution     (medium confidence — volunteered data)
         ↓ if tags still missing or unrecognised
    Context inference      (low-medium confidence — inferred from neighbors)
         ↓ if insufficient neighboring evidence
    Geometric fallback     (lowest confidence — inferred from edge length)

Output schema
-------------
Every tier produces a FallbackResult with two separate fields per attribute:
  - highway_score      : float [0, 1]  — walkability quality of this road type
  - highway_confidence : float [0, 1]  — how certain we are about that score

These are stored separately on the edge and are NOT pre-combined. The scoring
layer in factors.py will use both. The Streamlit front end will eventually
combine them into a single display value, but during development having them
separate makes it easy to see whether a suspicious score comes from bad data
or genuine low walkability.

Confidence scale
----------------
  1.00  Explicit OSM highway tag, recognised type
  0.65  Context inference, one type clearly dominates
  0.50  Context inference, weaker dominance
  0.35  Context inference, meaningful conflict detected
  0.30  Geometric fallback, clear signal from edge length
  0.20  Geometric fallback, ambiguous length zone
  0.10  No tag, no context, no geometry — floor value

Context inference design
------------------------
When an edge lacks a highway tag, we examine neighboring edges that DO
have explicit tags and use them as evidence. Key design decisions:

  Pedestrian-only exclusion:
    Footways and paths commonly run alongside primary/secondary roads.
    If we allowed them as evidence, a footway parallel to a primary would
    incorrectly suggest the unmarked edge is pedestrian-type. So any edge
    whose highway type is in PEDESTRIAN_ONLY_TYPES is silently excluded
    from the evidence pool. living_street is NOT excluded — it has cars.

  Bearing-weighted continuation:
    An edge continuing in the same axis as the target (same or opposite
    bearing) is strong evidence. A perpendicular cross-street is weak
    evidence. This captures the "between two primaries" case while still
    allowing regional signals from surrounding streets.

  Distinctiveness weighting:
    A primary alongside an untagged edge is strong evidence. A residential
    is weak evidence. This prevents common road types from dominating just
    by volume. See HIGHWAY_DISTINCTIVENESS in weights.py.

  Conservative bias:
    When evidence conflicts, we take the most distinctive signal and lower
    confidence. We never assign confidence=1.0 from context alone.
    Philosophy: a route scored too low (sent on a detour) is far less bad
    than a route scored too high (sent along a busy arterial).

  Conflict detection uses raw evidence (bearing × hop decay) not
  distinctiveness-weighted evidence — so a primary and a residential
  each contributing one same-axis edge registers as genuine conflict
  regardless of their relative distinctiveness.

  Hop decay:
    Evidence from 2 hops away carries half the weight of 1-hop evidence.
    Chains of untagged edges reduce confidence naturally.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from walkability.scoring.weights import (
    HIGHWAY_DISTINCTIVENESS,
    HIGHWAY_SCORES,
)

if TYPE_CHECKING:
    import networkx as nx

# ---------------------------------------------------------------------------
# Confidence anchors
# ---------------------------------------------------------------------------
# Named constants so the numeric values are only written once.
# Every confidence assignment in this file references one of these.

CONF_TAG_KNOWN         = 1.00   # Explicit recognised highway tag
CONF_CONTEXT_STRONG    = 0.65   # Context: clear dominant signal
CONF_CONTEXT_WEAK      = 0.50   # Context: weaker dominance
CONF_CONTEXT_CONFLICT  = 0.35   # Context: meaningful conflict detected
CONF_GEOM_CLEAR        = 0.30   # Geometry: unambiguous length signal
CONF_GEOM_AMBIGUOUS    = 0.20   # Geometry: length in ambiguous zone
CONF_NONE              = 0.10   # No tag, no context, no geometry — floor

# ---------------------------------------------------------------------------
# Other constants
# ---------------------------------------------------------------------------

PEDESTRIAN_LENGTH_MAX: float = 80.0    # metres — below this → likely footway
ROAD_LENGTH_MIN:       float = 40.0    # metres — above this → likely road

# Highway types excluded from context evidence.
# Pedestrian-only: expected alongside roads, carry no information about
# what road type the untagged edge is.
# living_street intentionally absent — it has cars.
PEDESTRIAN_ONLY_TYPES: frozenset[str] = frozenset({
    "pedestrian", "footway", "path", "steps"
})

# Context inference tuning
_DOMINANCE_STRONG:   float = 0.65    # dominance ratio for CONF_CONTEXT_STRONG
_EVIDENCE_WEIGHT_MIN: float = 0.40   # minimum total weight for strong confidence
_HOP_DECAY:          float = 0.50    # weight multiplier per additional hop
_BEARING_FLOOR:      float = 0.15    # minimum bearing weight (perpendicular floor)
_CONFLICT_RAW_RATIO: float = 0.30    # challenger raw weight / dominant → conflict
_OVERRIDE_STRENGTH_RATIO: float = 0.40  # challenger signal / dominant → conservative override

# Score class boundaries (derived from HIGHWAY_SCORES, computed once at import)
_PEDESTRIAN_THRESHOLD: float = HIGHWAY_SCORES["living_street"]  # 0.70
_SHARED_THRESHOLD:     float = HIGHWAY_SCORES["service"]         # 0.35


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class FallbackResult:
    """Canonical edge attributes produced by the fallback layer.

    The score and confidence fields are always stored separately.
    Do not pre-combine them here — that is the scoring layer's job.

    Fields
    ------
    edge_class : "pedestrian" | "shared" | "road" | "unknown"
        Broad classification used by the scoring layer.
    highway_score : float [0, 1]
        Walkability quality for this road type. 1.0 = best for walking.
    highway_confidence : float [0, 1]
        Certainty about highway_score. See module-level confidence scale.
    is_pedestrian_dedicated : bool
        True for edge types where pedestrians are the primary/only user.
    surface_score : float | None
        Default surface score for the edge class. None = unknown;
        treat differently from 0.0 in downstream scoring.
    inferred_from : list[str]
        Audit trail for debugging unexpected scores.
    """
    edge_class:              str
    highway_score:           float
    highway_confidence:      float
    is_pedestrian_dedicated: bool
    surface_score:           float | None
    inferred_from:           list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers — classification
# ---------------------------------------------------------------------------

def _classify_score(score: float) -> tuple[str, bool]:
    """Map a highway score to (edge_class, is_pedestrian_dedicated)."""
    if score >= _PEDESTRIAN_THRESHOLD:
        return "pedestrian", True
    elif score >= _SHARED_THRESHOLD:
        return "shared", False
    else:
        return "road", False


def _default_surface_for_class(edge_class: str) -> float | None:
    return {
        "pedestrian": 0.75,
        "shared":     0.55,
        "road":       0.40,
        "unknown":    None,
    }.get(edge_class)


# ---------------------------------------------------------------------------
# Internal helpers — bearing geometry
# ---------------------------------------------------------------------------

def _node_bearing(G: nx.MultiDiGraph, u: int, v: int) -> float:
    """Compass bearing from node u to node v (degrees, 0=N clockwise)."""
    lat1 = math.radians(G.nodes[u]["y"])
    lon1 = math.radians(G.nodes[u]["x"])
    lat2 = math.radians(G.nodes[v]["y"])
    lon2 = math.radians(G.nodes[v]["x"])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return math.degrees(math.atan2(x, y)) % 360


def _bearing_difference(b1: float, b2: float) -> float:
    """Axis-aware bearing difference in [0, 90] degrees.

    Opposite directions (0° vs 180°) are treated as the same axis —
    a road going north and a road going south are continuations of each
    other. Returns 0 for same axis, 90 for perpendicular.
    """
    diff = abs(b1 - b2) % 360
    if diff > 180:
        diff = 360 - diff
    if diff > 90:
        diff = 180 - diff
    return diff


def _bearing_weight(diff: float) -> float:
    """Map axis difference [0°, 90°] to a weight [_BEARING_FLOOR, 1.0].

    Same axis (0°)      → 1.0  — strong continuation signal
    Perpendicular (90°) → 0.15 — small but non-zero regional signal

    The non-zero floor means a uniform surrounding neighborhood (e.g.
    all residential) still registers even for cross-streets.
    """
    return _BEARING_FLOOR + (1.0 - _BEARING_FLOOR) * max(0.0, 1.0 - diff / 90.0)


# ---------------------------------------------------------------------------
# Tier 1: tag-based classification
# ---------------------------------------------------------------------------

STEPS_SENTINEL = "steps"


def classify_by_tag(highway: str | None) -> FallbackResult | None:
    """Build a FallbackResult from a resolved highway tag.

    Returns None if highway is None or unrecognised so the caller
    can fall through to context inference or geometry.
    """
    if isinstance(highway, list):
        raise TypeError(f"Expected resolved highway tag, got {highway}")
    if highway is None:
        return None

    if highway == STEPS_SENTINEL:
        return FallbackResult(
            edge_class="pedestrian",
            highway_score=0.50,
            highway_confidence=CONF_TAG_KNOWN,
            is_pedestrian_dedicated=True,
            surface_score=None,
            inferred_from=["highway=steps"],
        )

    score = HIGHWAY_SCORES.get(highway)
    if score is None:
        return None

    edge_class, is_ped = _classify_score(score)
    return FallbackResult(
        edge_class=edge_class,
        highway_score=score,
        highway_confidence=CONF_TAG_KNOWN,
        is_pedestrian_dedicated=is_ped,
        surface_score=_default_surface_for_class(edge_class),
        inferred_from=[f"highway={highway}"],
    )


# ---------------------------------------------------------------------------
# Tier 2: context inference from neighboring edges
# ---------------------------------------------------------------------------

def infer_by_context(
    G: nx.MultiDiGraph,
    u: int,
    v: int,
    key: int = 0,
    max_hops: int = 2,
) -> FallbackResult | None:
    """Infer highway type from the surrounding tagged road network.

    Performs a bearing-weighted BFS from both endpoints of edge (u, v).
    Evidence collected only from edges that:
      (a) have an explicit, recognised highway tag
      (b) are NOT in PEDESTRIAN_ONLY_TYPES

    Each piece of evidence is weighted by:
      distinctiveness × bearing_weight × hop_decay  (for selection)
      bearing_weight × hop_decay                    (for conflict detection)

    Returns None if no usable evidence was found.
    """
    from walkability.osm.tag_resolver import resolve_highway

    target_bearing = _node_bearing(G, u, v)

    # evidence[hw]     = distinctiveness × bearing × hop_decay  (who wins)
    # raw_evidence[hw] = bearing × hop_decay only               (is there conflict)
    evidence:     dict[str, float] = defaultdict(float)
    raw_evidence: dict[str, float] = defaultdict(float)

    visited_edges: set[tuple] = {(u, v, key), (v, u, key)}
    queue: deque = deque()

    for start_node in (u, v):
        for eu, ev, ek, edata in G.out_edges(start_node, data=True, keys=True):
            if (eu, ev, ek) in visited_edges:
                continue
            visited_edges.add((eu, ev, ek))
            edge_bearing = _node_bearing(G, eu, ev)
            bdiff        = _bearing_difference(target_bearing, edge_bearing)
            bweight      = _bearing_weight(bdiff)
            queue.append((eu, ev, ek, edata, 1, bweight))

    while queue:
        eu, ev, ek, edata, hop, cum_bweight = queue.popleft()

        if hop > max_hops:
            continue

        hw        = resolve_highway(edata.get("highway"))
        hop_decay = _HOP_DECAY ** (hop - 1)

        if hw is not None and hw not in PEDESTRIAN_ONLY_TYPES:
            raw_weight       = cum_bweight * hop_decay
            distinctiveness  = HIGHWAY_DISTINCTIVENESS.get(hw, 0.20)
            evidence[hw]    += distinctiveness * raw_weight
            raw_evidence[hw] += raw_weight

        elif hw is None and hop < max_hops:
            for nu, nv, nk, ndata in G.out_edges(ev, data=True, keys=True):
                if (nu, nv, nk) in visited_edges:
                    continue
                visited_edges.add((nu, nv, nk))
                edge_bearing = _node_bearing(G, nu, nv)
                bdiff        = _bearing_difference(target_bearing, edge_bearing)
                new_bweight  = cum_bweight * _bearing_weight(bdiff)
                queue.append((nu, nv, nk, ndata, hop + 1, new_bweight))

        # Pedestrian-only: silently skip — no evidence, no traversal

    if not evidence:
        return None

    total_weight = sum(evidence.values())

    def _signal_strength(hw: str) -> float:
        return evidence[hw] * HIGHWAY_DISTINCTIVENESS.get(hw, 0.20)

    dominant_hw    = max(evidence, key=_signal_strength)
    dominant_score = HIGHWAY_SCORES.get(dominant_hw, 0.40)
    dom_strength   = _signal_strength(dominant_hw)
    dom_raw        = raw_evidence[dominant_hw]
    dominance_ratio = evidence[dominant_hw] / total_weight

    # ------------------------------------------------------------------
    # Conflict detection (raw evidence) and conservative override
    # (signal strength).
    # ------------------------------------------------------------------
    conflict = False
    for hw in list(evidence):
        if hw == dominant_hw:
            continue
        hw_score    = HIGHWAY_SCORES.get(hw, 0.40)
        hw_strength = _signal_strength(hw)
        hw_raw      = raw_evidence[hw]

        if hw_raw >= dom_raw * _CONFLICT_RAW_RATIO:
            conflict = True

        if hw_score < dominant_score and hw_strength >= dom_strength * _OVERRIDE_STRENGTH_RATIO:
            dominant_hw    = hw
            dominant_score = hw_score

    # ------------------------------------------------------------------
    # Confidence assignment
    # ------------------------------------------------------------------
    if conflict:
        confidence = CONF_CONTEXT_CONFLICT
    elif dominance_ratio > _DOMINANCE_STRONG and total_weight > _EVIDENCE_WEIGHT_MIN:
        confidence = CONF_CONTEXT_STRONG
    else:
        confidence = CONF_CONTEXT_WEAK

    edge_class, is_ped = _classify_score(dominant_score)

    return FallbackResult(
        edge_class=edge_class,
        highway_score=dominant_score,
        highway_confidence=confidence,
        is_pedestrian_dedicated=is_ped,
        surface_score=_default_surface_for_class(edge_class),
        inferred_from=[
            f"context:dominant={dominant_hw}",
            f"conflict={conflict}",
            f"dominance={dominance_ratio:.2f}",
            f"total_evidence_weight={total_weight:.2f}",
            f"types_seen={sorted(evidence)}",
        ],
    )


# ---------------------------------------------------------------------------
# Tier 3: geometric fallback
# ---------------------------------------------------------------------------

def classify_by_geometry(
    length: float | None,
    geometry: Any = None,
) -> FallbackResult:
    """Infer edge class from edge length when tag and context both fail.

    Always returns a result — this is the floor of the pipeline.
    The geometry parameter is reserved for future bearing/curvature use.
    """
    if length is None:
        return FallbackResult(
            edge_class="unknown",
            highway_score=0.40,
            highway_confidence=CONF_NONE,
            is_pedestrian_dedicated=False,
            surface_score=None,
            inferred_from=["no_tag", "no_context", "no_geometry"],
        )

    # Conditions are ordered so the ambiguous zone (ROAD_LENGTH_MIN to
    # PEDESTRIAN_LENGTH_MAX) is an explicit else — not a dead branch.
    #   < 40m  → clearly short enough to be a footway stub
    #   > 80m  → clearly long enough to be a road segment
    #   40–80m → could be either; return unknown with lower confidence

    if length < ROAD_LENGTH_MIN:
        return FallbackResult(
            edge_class="pedestrian",
            highway_score=HIGHWAY_SCORES["footway"],
            highway_confidence=CONF_GEOM_CLEAR,
            is_pedestrian_dedicated=True,
            surface_score=_default_surface_for_class("pedestrian"),
            inferred_from=[
                "no_tag", "no_context",
                f"length={length:.1f}m < {ROAD_LENGTH_MIN}m",
            ],
        )

    if length > PEDESTRIAN_LENGTH_MAX:
        return FallbackResult(
            edge_class="road",
            highway_score=HIGHWAY_SCORES["residential"],
            highway_confidence=CONF_GEOM_CLEAR,
            is_pedestrian_dedicated=False,
            surface_score=_default_surface_for_class("road"),
            inferred_from=[
                "no_tag", "no_context",
                f"length={length:.1f}m > {PEDESTRIAN_LENGTH_MAX}m",
            ],
        )

    # Ambiguous zone: between ROAD_LENGTH_MIN and PEDESTRIAN_LENGTH_MAX
    return FallbackResult(
        edge_class="unknown",
        highway_score=0.40,
        highway_confidence=CONF_GEOM_AMBIGUOUS,
        is_pedestrian_dedicated=False,
        surface_score=None,
        inferred_from=[
            "no_tag", "no_context",
            f"length={length:.1f}m (ambiguous {ROAD_LENGTH_MIN}–{PEDESTRIAN_LENGTH_MAX}m)",
        ],
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def get_fallback(
    edge_data: dict,
    G: nx.MultiDiGraph | None = None,
    u: int | None = None,
    v: int | None = None,
    key: int = 0,
) -> FallbackResult:
    """Full fallback pipeline for one edge. Always returns a FallbackResult.

    Tier order:
      1. Explicit highway tag    (confidence: 1.00)
      2. Context inference       (confidence: 0.35–0.65) — requires G, u, v
      3. Geometric fallback      (confidence: 0.10–0.30)

    Parameters
    ----------
    edge_data : dict
        Resolved edge attributes from tag_resolver.resolve_edge_tags().
    G, u, v, key : optional
        OSMnx graph and edge identifiers. Required for context inference.
    """
    result = classify_by_tag(edge_data.get("highway"))
    if result is not None:
        return result

    if G is not None and u is not None and v is not None:
        try:
            result = infer_by_context(G, u, v, key)
            if result is not None:
                return result
        except Exception:
            pass

    return classify_by_geometry(
        length=edge_data.get("length"),
        geometry=edge_data.get("geometry"),
    )