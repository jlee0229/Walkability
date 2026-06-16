"""
OSM tag resolver.

OSMnx can return list values for edge attributes when it collapses
parallel edges (e.g. highway=['footway', 'residential']). This module
resolves any such multi-value tags into a single canonical value before
they reach the scoring layer.

Priority orderings are imported from scoring/weights.py so that there
is exactly one place to change them.
"""

from __future__ import annotations

from typing import Any

from walkability.scoring.weights import HIGHWAY_PRIORITY, SURFACE_PRIORITY

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> list:
    """Normalise a raw OSM tag value to a list of strings.

    OSMnx may give us:
      - A plain string:      "footway"
      - A Python list:       ["footway", "residential"]
      - A semicolon string:  "footway;residential"  (rare but valid OSM)
      - None / NaN:          missing data
    """
    if value is None:
        return []
    # Handle pandas NA / numpy nan without importing pandas here
    try:
        if not isinstance(value, (str, list)) and (value != value):  # NaN check
            return []
    except TypeError:
        pass

    if isinstance(value, list):
        return [str(v).strip() for v in value if v is not None]

    s = str(value).strip()
    if ";" in s:
        return [v.strip() for v in s.split(";")]
    return [s] if s else []


def _resolve_by_priority(values: list[str], priority: list[str]) -> str | None:
    """Return whichever value in *values* appears earliest in *priority*.

    If none of the values appear in the priority list, returns the first
    raw value so that we never silently discard data. Returns None only
    when values is empty.
    """
    if not values:
        return None

    for candidate in priority:
        if candidate in values:
            return candidate

    # Fall back to the first value even if unrecognised
    return values[0]


# ---------------------------------------------------------------------------
# Public resolvers
# ---------------------------------------------------------------------------

# Sentinel returned when steps are detected so callers can route differently.
STEPS_SENTINEL = "steps"


def resolve_highway(value: Any) -> str | None:
    """Resolve a (possibly multi-value) highway tag to one canonical value.

    Special case — steps:
      If *any* value in the list is "steps", return "steps" immediately.
      Steps are not just another point on the walkability spectrum; they
      affect routing geometry and accessibility. The scoring layer treats
      them separately.

    Otherwise, pick whichever highway type ranks highest in
    HIGHWAY_PRIORITY (lowest index = most pedestrian-friendly).

    Returns None if the input is missing or empty.
    """
    values = _as_list(value)
    if not values:
        return None

    if STEPS_SENTINEL in values:
        return STEPS_SENTINEL

    return _resolve_by_priority(values, HIGHWAY_PRIORITY)

def resolve_surface(value: Any) -> str | None:
    """Resolve a (possibly multi-value) surface tag to one canonical value.

    Picks whichever surface ranks highest in SURFACE_PRIORITY
    (lowest index = best for walking).

    Returns None if the input is missing or empty.
    """
    values = _as_list(value)
    if not values:
        return None

    return _resolve_by_priority(values, SURFACE_PRIORITY)


def resolve_boolean_tag(value: Any) -> str | None:
    """Resolve tags like foot=, access=, lit= that OSM encodes as yes/no.

    Returns the *most permissive* value found, i.e. "yes" beats "no",
    so that an edge tagged foot=['yes','no'] on parallel ways is treated
    as walkable (conservative choice — better to allow and let surface /
    road type penalise than to silently block a usable path).

    Returns None if the input is missing or empty.
    """
    BOOLEAN_PRIORITY = ["yes", "designated", "permissive", "no", "private"]
    values = _as_list(value)
    if not values:
        return None

    return _resolve_by_priority(values, BOOLEAN_PRIORITY)

# ---------------------------------------------------------------------------
# Top-level edge resolver
# ---------------------------------------------------------------------------

def resolve_edge_tags(edge_data: dict) -> dict:
    """Apply all tag resolvers to a raw OSMnx edge attribute dictionary.

    Accepts the raw dict from G[u][v][key] and returns a new dict with
    canonical single values for all tags the scoring layer cares about.
    Unrecognised keys are passed through unchanged so no data is lost.

    Example
    -------
    >>> raw = {"highway": ["footway", "residential"], "surface": "asphalt"}
    >>> resolve_edge_tags(raw)
    {"highway": "footway", "surface": "asphalt", "foot": None, "lit": None, ...}
    """
    resolved = dict(edge_data)  # shallow copy — don't mutate caller's dict

    resolved["highway"] = resolve_highway(edge_data.get("highway"))
    resolved["surface"] = resolve_surface(edge_data.get("surface"))
    resolved["foot"]    = resolve_boolean_tag(edge_data.get("foot"))
    resolved["access"]  = resolve_boolean_tag(edge_data.get("access"))
    resolved["lit"]     = resolve_boolean_tag(edge_data.get("lit"))

    return resolved