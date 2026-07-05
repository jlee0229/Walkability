"""City sidewalk-inventory profiles.

Every municipality publishes its sidewalk inventory with different column names,
a different condition scale (Boston SCI is 0–100; Austin ``rating_final`` is a 1–5
ordinal), a different material vocabulary, a different "never surveyed" marker,
and a different metric CRS. Rather than scatter those assumptions through
``build.py``, a :class:`CityInventoryProfile` captures everything municipality-
specific in one place, and the generic aggregation / schema logic in ``build.py``
reads from a profile. **Adding a city is adding a profile here** — the pipeline
code does not change.

The one hard rule: :data:`BOSTON_PROFILE` must reproduce the pre-refactor Boston
behaviour *exactly* (same arithmetic, same rounding), so the existing enriched
graph and ``problem_routes_baseline.json`` stay valid without a ``--force``
rebuild. The condition round-trip (normalised mean → native audit value →
re-normalised in the schema) is preserved for that reason: see
``aggregate_condition`` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from walkability.config import DATA_DIR, OSM_DIR
from walkability.scoring.weights import SURFACE_SCORES


# ---------------------------------------------------------------------------
# Scale / vocabulary adapters (per-city, referenced by the profiles below)
# ---------------------------------------------------------------------------

def _boston_condition_to_score(raw: Any) -> float | None:
    """Boston SCI (0–100 numeric string) → [0, 1], or None when invalid.

    Identical to the pre-refactor ``build._condition_to_score``. SCI is defined
    on 0–100; the source field is partly corrupt (the literal ``"NaN"`` and
    negatives down to ~-68000, a city calc error). Those are *no valid
    measurement*, not "destroyed sidewalk = 0", so anything outside [0, 100]
    returns None and the edge falls through to the OSM-tag tier.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        sci = float(raw)
    except (ValueError, TypeError):
        return None
    if not (0.0 <= sci <= 100.0):
        return None
    return round(sci / 100.0, 4)


def _austin_condition_to_score(raw: Any) -> float | None:
    """Austin structural rating (1–5 ordinal) → [0, 1], or None when invalid.

    Direction is **INVERTED from Boston's SCI**: Austin 1 = Excellent (best),
    5 = Failed/impassable (worst), per the Sidewalk Master Plan engineering
    matrix (1: ADA-compliant, <2% cross-slope, faults <0.25"; … 5: missing /
    cross-slope >12% / faults >4"). So a good sidewalk is a *low* number →
    ``(5 - rating) / 4`` maps 1→1.0, 3→0.5, 5→0.0. Non-numeric / out-of-range
    (e.g. PENDING ASSESSMENT rows carrying null) return None so the edge falls
    through to the OSM-tag tier.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        rating = float(raw)
    except (ValueError, TypeError):
        return None
    if not (1.0 <= rating <= 5.0):
        return None
    return round((5.0 - rating) / 4.0, 4)


def _boston_is_phantom(row: Mapping[str, Any]) -> bool:
    """Boston: a pre-2000 (Unix-epoch placeholder) date with ``inspected`` null
    marks a sidewalk polygon that exists but was never field-surveyed. Skip it.
    """
    raw_date = row.get("new_insp_d")
    try:
        year = pd.to_datetime(raw_date).year
    except Exception:
        year = None
    insp = row.get("inspected")
    insp_null = insp is None or (isinstance(insp, float) and pd.isna(insp))
    return year is not None and year < 2000 and insp_null


def _austin_is_phantom(row: Mapping[str, Any]) -> bool:
    """Austin: ``functional_condition == "PENDING ASSESSMENT"`` marks a segment
    in the network that has not been assessed yet — skip it.

    TODO(austin-verify): confirm PENDING rows also carry a null ``rating_final``
    (in which case ``condition_to_score`` already drops them and this is a
    belt-and-braces guard). Tolerates a missing key (returns False), so it is
    safe to call on the synthetic aggregate record too.
    """
    fc = row.get("functional_condition")
    return str(fc).strip().upper() == "PENDING ASSESSMENT" if fc is not None else False


# Boston DPW material codes → OSM surface labels (keys in SURFACE_SCORES).
# OT (Other) is intentionally absent — unknown material, so surface_score()
# returns None and never overrides a better OSM surface tag.
_BOSTON_MATERIAL_MAP: dict[str, str] = {
    "CC":  "concrete",       # Concrete — most common Boston sidewalk
    "BR":  "paving_stones",  # Brick / cobblestone
    "BIT": "asphalt",        # Bituminous asphalt
    "AC":  "asphalt",        # Asphalt (alternate code)
    "GR":  "paving_stones",  # Granite slab (similar walking quality to pavers)
}

# Austin ``sidewalk_surface`` tokens → OSM surface labels.
# TODO(austin-verify): confirm the full vocabulary on download (the portal
# preview showed CONCRETE / EXPOSED_AGGREGATE / ASPHALT / PAVER-BRICK "and
# others"). Unknown tokens fall through to None (never over-score).
_AUSTIN_MATERIAL_MAP: dict[str, str] = {
    "CONCRETE":          "concrete",
    "EXPOSED_AGGREGATE": "concrete",      # aggregate-finish concrete — walks like concrete
    "ASPHALT":           "asphalt",
    "PAVER-BRICK":       "paving_stones",
    "PAVERS":            "paving_stones",
    "BRICK":             "paving_stones",
}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CityInventoryProfile:
    """Everything municipality-specific about one sidewalk inventory.

    ``build.py`` reads these fields instead of hard-coding Boston's schema.
    """

    name: str

    # --- I/O ---
    graph_path:     Path   # OSM walk graph (osmnx download)
    enriched_path:  Path   # enriched GraphML output
    inventory_path: Path   # sidewalk-inventory vector file
    metric_crs:     str    # local UTM CRS for metre-accurate distance/length

    # --- source column names ---
    condition_field: str
    surface_field:   str | None
    width_field:     str | None
    date_field:      str | None
    area_field:      str | None   # polygon-area weight; None → weight by geometry length
    side_field:      str | None   # left/right label for divergence; None → top-2 by weight

    # --- scale / vocabulary adapters ---
    condition_to_score: Callable[[Any], float | None]  # native condition → [0, 1] | None
    aggregate_condition: Callable[[float], Any]         # normalised mean [0,1] → native audit value
    material_map: dict[str, str]                        # source token (upper) → OSM surface label

    # --- data-quality predicate ---
    is_phantom: Callable[[Mapping[str, Any]], bool]     # row → never field-surveyed?

    # --- divergence, in normalised [0, 1] condition units (not raw points) ---
    divergence_threshold: float

    # --- validation ---
    required_fields: tuple[str, ...] = field(default_factory=tuple)

    def surface_score(self, raw: Any) -> float | None:
        """Map a source material token to a [0, 1] comfort score via SURFACE_SCORES.

        Accepts the city's own codes (translated through ``material_map``) and
        raw OSM surface labels. Unrecognised → None so it never overrides a
        better OSM surface tag. Behaviour matches the pre-refactor
        ``build._surface_label_to_score`` for Boston.
        """
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            return None
        token = str(raw).strip()
        label = self.material_map.get(token.upper(), token.lower())
        if label in SURFACE_SCORES:
            return SURFACE_SCORES[label]
        for key in SURFACE_SCORES:  # partial match, e.g. "asphalt_concrete" → asphalt
            if key in label:
                return SURFACE_SCORES[key]
        return None


# ---------------------------------------------------------------------------
# The reference city — MUST stay behaviourally identical to the pre-refactor code
# ---------------------------------------------------------------------------

BOSTON_PROFILE = CityInventoryProfile(
    name="boston",
    graph_path=OSM_DIR / "boston_walk.graphml",
    enriched_path=OSM_DIR / "boston_walk_enriched.graphml",
    inventory_path=DATA_DIR / "boston" / "sidewalk_inventory" / "Sidewalk_Inventory.shp",
    metric_crs="EPSG:32619",  # UTM 19N — Boston
    condition_field="SCI",
    surface_field="MATERIAL",
    width_field="SWK_WIDTH",
    date_field="new_insp_d",
    area_field="SWK_AREA",
    side_field="SIDE",
    condition_to_score=_boston_condition_to_score,
    # Preserve the exact round-trip: normalised mean → 0–100 rounded to 1 dp, so
    # the schema's re-normalisation reproduces the current baked walk_score.
    aggregate_condition=lambda mean01: round(mean01 * 100.0, 1),
    material_map=_BOSTON_MATERIAL_MAP,
    is_phantom=_boston_is_phantom,
    divergence_threshold=0.15,  # == 15 SCI points on the 0–100 scale
    required_fields=("SCI", "new_insp_d"),
)


# ---------------------------------------------------------------------------
# Chapter D target — scaffold; TODO markers resolve once the data is downloaded
# ---------------------------------------------------------------------------

AUSTIN_PROFILE = CityInventoryProfile(
    name="austin",
    graph_path=OSM_DIR / "austin_walk.graphml",
    enriched_path=OSM_DIR / "austin_walk_enriched.graphml",
    # TODO(austin-verify): confirm the downloaded filename/format (Socrata export
    # of dataset vchz-d9ng; GeoJSON or shapefile).
    inventory_path=DATA_DIR / "austin" / "sidewalk_inventory" / "sidewalks.geojson",
    metric_crs="EPSG:32614",  # UTM 14N — Austin
    # rating_no_veg = pure concrete/engineering condition (vegetation ignored) —
    # the clean structural analog of Boston's SCI. We deliberately do NOT use
    # rating_overall (which demotes structurally-fine sidewalks for hedge/tree
    # overgrowth of the 80" clearance corridor): folding vegetation into
    # surface_score would inject a spurious cross-city comfort gradient vs Boston,
    # which has no equivalent signal. rating_overall's vegetation/clearance signal
    # is a candidate NEW factor (obstruction/passability), not part of comfort.
    # TODO(austin-verify): confirm rating_no_veg exists + its coverage; the portal
    # metadata also lists rating_final/rating_overall — pick the pure-structural one.
    condition_field="rating_no_veg",
    surface_field="sidewalk_surface",
    width_field="width_sidewalk",
    date_field="assessment_date",
    area_field=None,   # multiline segments — weight by geometry length, not polygon area
    side_field=None,   # TODO(austin-verify): check for a side attribute; else top-2 by length
    condition_to_score=_austin_condition_to_score,   # INVERTED scale (1 best … 5 worst)
    aggregate_condition=lambda mean01: round(5.0 - mean01 * 4.0, 2),  # [0,1] → 1–5 (inverse)
    material_map=_AUSTIN_MATERIAL_MAP,
    is_phantom=_austin_is_phantom,
    divergence_threshold=0.15,  # keep the same normalised sensitivity as Boston
    required_fields=("rating_no_veg", "functional_condition"),
)


CITY_PROFILES: dict[str, CityInventoryProfile] = {
    BOSTON_PROFILE.name: BOSTON_PROFILE,
    AUSTIN_PROFILE.name: AUSTIN_PROFILE,
}
