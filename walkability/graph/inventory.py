"""Per-city profiles for the walkability pipeline.

Every municipality publishes its sidewalk inventory with different column names,
a different condition scale (Boston SCI is 0–100; Austin's rating is a 1–5
ordinal, and *inverted*), a different material vocabulary, a different "never
surveyed" marker, and a different metric CRS; it also has its own OSM query
extent and its own set of environment-feature layers. Rather than scatter those
assumptions through ``build.py`` / ``environment.py`` / the download scripts, a
:class:`CityProfile` captures everything municipality-specific in one place, and
the generic pipeline reads from a profile. **Adding a city is adding a profile
here** — the pipeline code does not change.

The one hard rule: :data:`BOSTON_PROFILE` must reproduce the pre-refactor Boston
behaviour *exactly* (same arithmetic, same rounding, same file paths), so the
existing enriched graph and ``problem_routes_baseline.json`` stay valid without a
``--force`` rebuild. The condition round-trip (normalised mean → native audit
value → re-normalised in the schema) is preserved for that reason: see
``aggregate_condition`` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from walkability.config import DATA_DIR, OSM_DIR, PLACES
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


# Austin ``pedestrian_facility_type`` values for facilities that do not
# physically exist yet (planned / potential). They carry no real surface, so they
# are treated as phantom even if a stray rating is present. EXISTING_SIDEWALK,
# DRIVEWAY, SHARED_USE_PATH, SHARED_STREET, PROTECTED_STREET_PATH are real
# walking surfaces and are kept.
_AUSTIN_NONEXISTENT_FACILITY = {
    "POTENTIAL_SIDEWALK", "PLANNED_SIDEWALK", "PLANNED_SHARED_STREET",
}


def _austin_is_phantom(row: Mapping[str, Any]) -> bool:
    """Austin: skip segments that were never field-surveyed or don't yet exist.

    ``functional_condition == "PENDING ASSESSMENT"`` (84.7k rows) marks a segment
    not yet assessed; ``pedestrian_facility_type`` in the planned/potential set
    marks a facility that does not physically exist. Both carry no usable surface
    condition. Tolerates missing keys (returns False), so it is safe to call on
    the synthetic aggregate record too (where neither key is present).
    """
    fc = str(row.get("functional_condition") or "").strip().upper()
    if fc == "PENDING ASSESSMENT":
        return True
    pft = str(row.get("pedestrian_facility_type") or "").strip().upper()
    return pft in _AUSTIN_NONEXISTENT_FACILITY


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

# Austin ``sidewalk_surface`` tokens → OSM surface labels (keys in SURFACE_SCORES).
# Full vocabulary confirmed from the live vchz-d9ng API (counts, most→least
# common): CONCRETE 255687, EXPOSED_AGGREGATE 11746, ASPHALT 997, PAVER-BRICK 577,
# CRUSHED_STONE 140, PAVER-CONCRETE 83, COLORED 69, PAVER-GRANITE 61,
# PAVER-SANDSTONE 43, EXPERIMENTAL 29, STAMPED 9, PLASTIC_PANEL 4,
# PERVIOUS_CONCRETE 2, RUBBERIZED 1 (+82823 null). Explicit mapping is required
# (the partial-match fallback in surface_score would e.g. score PAVER-CONCRETE as
# concrete 0.9 instead of paving_stones 0.7). Unmapped/experimental → None so
# they never over-score.
_AUSTIN_MATERIAL_MAP: dict[str, str] = {
    "CONCRETE":          "concrete",      # 0.9
    "PERVIOUS_CONCRETE": "concrete",
    "EXPOSED_AGGREGATE": "concrete",      # aggregate-finish concrete — walks like concrete
    "COLORED":           "concrete",      # colored concrete
    "STAMPED":           "concrete",      # stamped concrete
    "ASPHALT":           "asphalt",       # 1.0
    "PAVER-BRICK":       "paving_stones", # 0.7
    "PAVER-CONCRETE":    "paving_stones",
    "PAVER-GRANITE":     "paving_stones",
    "PAVER-SANDSTONE":   "paving_stones",
    "CRUSHED_STONE":     "compacted",     # 0.55 — compacted granular, better than loose gravel
    # EXPERIMENTAL / PLASTIC_PANEL / RUBBERIZED intentionally unmapped → None.
}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CityProfile:
    """Everything municipality-specific about one city's walkability build.

    ``build.py`` / ``environment.py`` / the download scripts read these fields
    instead of hard-coding Boston's schema, extent, paths, and CRS.
    """

    name: str

    # --- OSM extent ---
    places: list[str]      # osmnx graph_from_place / features_from_place query

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

    def env_layer_path(self, layer: str) -> Path:
        """Path to one cached environment-feature GeoPackage for this city.

        ``layer`` is one of arterials/buildings/pois/openspace/landuse/roads.
        Both ``download_environment.py`` (writer) and ``environment.py`` (reader)
        resolve layer files through here, so a city's env layers live under
        ``data/osm/<name>_<layer>.gpkg`` (e.g. boston_arterials.gpkg).
        """
        return OSM_DIR / f"{self.name}_{layer}.gpkg"


# ---------------------------------------------------------------------------
# The reference city — MUST stay behaviourally identical to the pre-refactor code
# ---------------------------------------------------------------------------

BOSTON_PROFILE = CityProfile(
    name="boston",
    places=PLACES,  # config.PLACES — Boston + Brookline + the metro-hull towns
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

AUSTIN_PROFILE = CityProfile(
    name="austin",
    places=["Austin, Texas, USA"],  # city proper — matches the city-only inventory extent
    graph_path=OSM_DIR / "austin_walk.graphml",
    enriched_path=OSM_DIR / "austin_walk_enriched.graphml",
    # Downloaded via paginated SODA export of dataset vchz-d9ng → GeoPackage
    # (the single-shot .geojson stream truncates on this 352k-feature layer).
    inventory_path=DATA_DIR / "austin" / "sidewalk_inventory" / "sidewalks.gpkg",
    metric_crs="EPSG:32614",  # UTM 14N — Austin
    # rating_no_veg = pure concrete/engineering condition (vegetation ignored) —
    # the clean structural analog of Boston's SCI (coverage 265,657 rows, ~75%,
    # confirmed via the live vchz-d9ng API). We deliberately do NOT use
    # rating_overall (which demotes structurally-fine sidewalks for hedge/tree
    # overgrowth of the 80" clearance corridor — it differs from rating_no_veg on
    # 23,667 rows / ~9% of assessed): folding vegetation into surface_score would
    # inject a spurious cross-city comfort gradient vs Boston, which has no
    # equivalent signal. That rating_overall − rating_no_veg gap is instead a
    # candidate NEW factor (obstruction/passability), not part of comfort.
    condition_field="rating_no_veg",
    surface_field="sidewalk_surface",
    width_field="width_sidewalk",   # feet (confirmed); a sparser numeric `width` also exists
    date_field="assessment_date",
    area_field=None,   # multiline segments — weight by geometry length, not polygon area
    side_field=None,   # no side attribute in this inventory (confirmed) → top-2 by length
    condition_to_score=_austin_condition_to_score,   # INVERTED scale (1 best … 5 worst)
    aggregate_condition=lambda mean01: round(5.0 - mean01 * 4.0, 2),  # [0,1] → 1–5 (inverse)
    material_map=_AUSTIN_MATERIAL_MAP,
    is_phantom=_austin_is_phantom,
    divergence_threshold=0.15,  # keep the same normalised sensitivity as Boston
    required_fields=("rating_no_veg", "functional_condition", "pedestrian_facility_type"),
)


CITY_PROFILES: dict[str, CityProfile] = {
    BOSTON_PROFILE.name: BOSTON_PROFILE,
    AUSTIN_PROFILE.name: AUSTIN_PROFILE,
}

# Backward-compatible alias (the profile grew from inventory-only to full city).
CityInventoryProfile = CityProfile
