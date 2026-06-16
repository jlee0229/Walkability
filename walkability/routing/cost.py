"""
Turn a per-edge walkability score into a routable edge cost.

Cost model
----------
    cost = length × (1 + α·(1 − walk_score))

``walk_score`` ∈ [0, 1] comes from scoring/factors.py (1.0 = best). The cost is
anchored to physical length so routes stay geographically sane — a perfectly
walkable edge costs exactly its length, and the worst possible edge costs
``(1 + α)`` times its length. ``α`` is the single tradeoff knob:

    α = 0   → pure shortest path (walkability ignored)
    α = 2   → a fully unwalkable edge "feels" 3× as long; strong detour pull

α is intended to become a Streamlit slider, so it is a parameter everywhere
rather than a baked-in constant.

Foot access (hard constraints)
------------------------------
``foot='no'`` (EXCLUDED_FOOT_ACCESS) edges are not routable: ``edge_cost``
returns ``None`` and the router drops them. Restricted-access values
(RESTRICTED_FOOT_ACCESS — private, customers, permit, residents, …) stay usable
but are multiplied by ``RESTRICTED_ACCESS_PENALTY`` so they are chosen only when
there is no reasonable public alternative. ``'yes'``/``None`` are unaffected
here (the soft foot-access preference is already folded into walk_score). The
access classification itself lives in scoring/factors.py so scoring and cost
agree on a single source of truth.
"""

from __future__ import annotations

from walkability.scoring.factors import (
    EXCLUDED_FOOT_ACCESS,
    RESTRICTED_FOOT_ACCESS,
    _as_str,
    edge_walkability,
)
from walkability.scoring.weights import FACTOR_WEIGHTS

# Default distance/walkability tradeoff. Routing parameter, not a scoring
# weight, so it lives here rather than in scoring/weights.py.
ALPHA_DEFAULT: float = 2.0

# Cost multiplier for restricted-but-passable access (usable, discouraged).
RESTRICTED_ACCESS_PENALTY: float = 3.0

# Length used when an edge is missing the `length` attribute. Small and
# positive so such edges are cheap rather than crashing the weighted mean.
_MISSING_LENGTH: float = 1.0


def edge_cost(
    edge: dict,
    alpha: float = ALPHA_DEFAULT,
    *,
    is_terminal: bool = False,
    weights: dict[str, float] = FACTOR_WEIGHTS,
) -> float | None:
    """Routable cost for one edge, or ``None`` if the edge is impassable.

    Parameters
    ----------
    edge :
        Edge-attribute dict (``G[u][v][key]``).
    alpha :
        Tradeoff knob; see module docstring.
    is_terminal :
        True when this edge is the first or last edge of a route (it leaves the
        origin or enters the destination). Terminal edges skip the
        restricted-access penalty: if your destination is a customers-only path
        (a zoo entrance, a private drive you live on) you would legitimately use
        it, so penalising it only distorts route choice. ``foot=no`` is still
        excluded even on terminal edges.
    weights :
        Factor weights passed through to ``edge_walkability``. Defaults to the
        ``FACTOR_WEIGHTS`` object so the baked-score fast path is preserved;
        UI sliders pass a different dict and force a recompute.

    Returns
    -------
    float | None :
        Non-negative cost, or ``None`` when ``foot_access == "no"`` (caller
        must treat the edge as absent).
    """
    foot = _as_str(edge.get("foot_access"))
    if foot in EXCLUDED_FOOT_ACCESS:
        return None

    length = edge.get("length")
    if length is None:
        length = _MISSING_LENGTH
    else:
        length = float(length)

    walk, _ = edge_walkability(edge, weights)
    cost = length * (1.0 + alpha * (1.0 - walk))

    if foot in RESTRICTED_FOOT_ACCESS and not is_terminal:
        cost *= RESTRICTED_ACCESS_PENALTY

    return cost
