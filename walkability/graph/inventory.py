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
from walkability.scoring.weights import DEFAULT_MAXSPEED_MPH, SURFACE_SCORES

# Austin's arterials run far faster than Boston's (Sun Belt stroads). OSM tags
# maxspeed on only ~42% of them, so the untagged majority falls to these class
# defaults — the Boston-calibrated globals (secondary 30, primary 35) badly
# under-penalize Austin, where tagged speeds show secondary/primary arterials at
# 40–55 mph (the ground-truth survey flagged safety-too-high on every stroad).
_AUSTIN_MAXSPEED_DEFAULTS: dict[str, float] = {
    "living_street": 10.0, "service": 15.0, "residential": 25.0,
    "unclassified": 30.0, "tertiary": 35.0, "secondary": 40.0,
    "primary": 45.0, "trunk": 55.0, "motorway": 65.0,
}


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


def _piecewise(x: float, knots: tuple[tuple[float, float], ...]) -> float:
    """Piecewise-linear interpolation of ``x`` through ``knots`` (ascending x),
    clamped to the endpoints outside the range."""
    if x <= knots[0][0]:
        return knots[0][1]
    if x >= knots[-1][0]:
        return knots[-1][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x <= x1:
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return knots[-1][1]


# Candidate-A condition curve (calibrated 2026-07-06). Direction is INVERTED from
# Boston's SCI (Austin 1 = Excellent … 5 = Failed/impassable, per the Sidewalk
# Master Plan matrix). The earlier LINEAR (5-rating)/4 map over-penalized Austin's
# stricter ADA-based scale vs Boston's lenient SCI — a "fully walkable" rating-2
# became 0.75 and a "marginal but walkable" rating-3 became 0.50, biasing comfort
# pessimistically (Austin surface mean 0.65 vs Boston 0.87; the ground-truth
# survey confirmed 2s/3s over-penalized). This curve SOFTENS 2s and 3s (they are
# walkable) while keeping the genuinely-bad 4/5 low (real trip hazards / impassable).
# ``_austin_aggregate_condition`` below is its EXACT inverse (swapped knots), so
# the both-sides aggregation round-trip (mean surface_score → rating → re-map)
# preserves the aggregated value even though the curve is non-linear.
_AUSTIN_RATING_KNOTS = ((1.0, 1.0), (2.0, 0.85), (3.0, 0.65), (4.0, 0.35), (5.0, 0.0))
_AUSTIN_SCORE_KNOTS  = ((0.0, 5.0), (0.35, 4.0), (0.65, 3.0), (0.85, 2.0), (1.0, 1.0))


def _austin_condition_to_score(raw: Any) -> float | None:
    """Austin structural rating (1–5 ordinal) → [0, 1], or None when invalid.

    Piecewise-linear through ``_AUSTIN_RATING_KNOTS`` (1→1.0, 2→0.85, 3→0.65,
    4→0.35, 5→0.0). Also handles the fractional aggregated rating from the
    both-sides mean. Non-numeric / out-of-range (e.g. PENDING ASSESSMENT rows
    carrying null) return None so the edge falls through to the OSM-tag tier.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        rating = float(raw)
    except (ValueError, TypeError):
        return None
    if not (1.0 <= rating <= 5.0):
        return None
    return round(_piecewise(rating, _AUSTIN_RATING_KNOTS), 4)


def _austin_aggregate_condition(mean01: float) -> float:
    """Inverse of ``_austin_condition_to_score``: normalised mean surface_score
    [0,1] → the equivalent 1–5 rating (audit value). Exact inverse of the curve
    above, so the schema's re-map reproduces the aggregated surface_score."""
    return round(_piecewise(max(0.0, min(1.0, mean01)), _AUSTIN_SCORE_KNOTS), 2)


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

    # --- environment: per-city fallback speeds for untagged roads (crash-risk
    # curve). Defaults to the global (Boston-calibrated) DEFAULT_MAXSPEED_MPH. ---
    maxspeed_defaults: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_MAXSPEED_MPH))

    # Cap on how much eyes-on-street can rescue a low-car_safety road:
    # eyes_eff = min(eyes, car_safety + cap) before environment = sqrt(car·eyes).
    # None = no cap (Boston, unchanged). A small cap stops a strip-mall's foot
    # traffic from making a hostile fast arterial read as safe (Austin stroads).
    eyes_rescue_cap: float | None = None

    # --- OSM download filter ---
    # osmnx Overpass filter passed to graph_from_place. None → the stock
    # network_type="walk" filter. A city whose pedestrian network runs largely on
    # SHARED-USE paths tagged highway=cycleway (Austin's Butler Hike-and-Bike
    # Trail + the Lady Bird Lake bridge decks) needs a custom filter that KEEPS
    # cycleways — the stock walk filter drops them, severing every central lake
    # crossing and forcing routes ~1.3 km west to MoPac. Still excludes motor
    # roads / foot=no / private, so it stays a pedestrian graph.
    custom_filter: str | None = None

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
    # width DROPPED (2026-07-06): width_sidewalk is untrustworthy — 73% logged as a
    # 3 ft default (→ width_score 0 on the ramp, mean 0.073, cratering comfort), and
    # 26% of rating-1 sidewalks are logged 3 ft despite rating-1 requiring >4 ft. Per
    # the codebase's "don't mis-score unreliable data" pattern (corrupt SCI→None), we
    # don't use it: width_field=None drops the factor so Austin comfort = mean(condition,
    # material). Boston keeps its width. Revisit if a future city has clean width data.
    width_field=None,
    date_field="assessment_date",
    area_field=None,   # multiline segments — weight by geometry length, not polygon area
    side_field=None,   # no side attribute in this inventory (confirmed) → top-2 by length
    condition_to_score=_austin_condition_to_score,   # non-linear, softened 2s/3s (candidate A)
    aggregate_condition=_austin_aggregate_condition,  # exact inverse of the curve
    material_map=_AUSTIN_MATERIAL_MAP,
    is_phantom=_austin_is_phantom,
    divergence_threshold=0.15,  # keep the same normalised sensitivity as Boston
    maxspeed_defaults=_AUSTIN_MAXSPEED_DEFAULTS,  # faster arterials than Boston
    eyes_rescue_cap=0.15,  # strip-mall foot traffic can't make a stroad feel safe
    # Keep highway=cycleway: Austin's Butler Hike-and-Bike Trail + the Lady Bird
    # Lake pedestrian-bridge decks are tagged cycleway (foot unset), and the stock
    # walk filter drops them — severing every central lake crossing (verified:
    # Capitol→Zilker routed 10.5 km via MoPac instead of ~2.6 km). Still excludes
    # motor roads / foot=no / private / non-pedestrian ways, so it stays a walking
    # graph. Mirrors osmnx's walk filter with `cycleway` removed from the exclusion.
    custom_filter=(
        '["highway"]["area"!~"yes"]'
        '["highway"!~"abandoned|bus_guideway|construction|motor|no|planned|'
        'platform|proposed|raceway|razed"]'
        '["foot"!~"no"]["service"!~"private"]["access"!~"private|no"]'
    ),
    required_fields=("rating_no_veg", "functional_condition", "pedestrian_facility_type"),
)


CITY_PROFILES: dict[str, CityProfile] = {
    BOSTON_PROFILE.name: BOSTON_PROFILE,
    AUSTIN_PROFILE.name: AUSTIN_PROFILE,
}

# Backward-compatible alias (the profile grew from inventory-only to full city).
CityInventoryProfile = CityProfile
