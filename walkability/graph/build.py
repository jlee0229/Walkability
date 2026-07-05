"""
Assembles the canonical walkability edge schema onto the OSM graph.

Pipeline
--------
For each edge (u, v, key) in the OSM walk graph:
  1. Resolve raw OSM tags via tag_resolver (normalises multi-value fields)
  2. Run fallback pipeline (tag → context → geometry) for missing attributes
  3. Spatial join: find matching Boston sidewalk inventory feature within
     SPATIAL_JOIN_CUTOFF_M metres
  4. If a match is found, override/supplement fallback values with city data
     and compute city-data confidence (accounting for survey date and
     OSM consistency)
  5. Write canonical schema back onto the edge in-place

Output schema (per edge)
------------------------
  highway_score        float [0,1]   Road type walkability score
  highway_confidence   float [0,1]   Certainty about highway_score
  surface_score          float|None    Structural condition score (SCI/100 from city, or OSM surface fallback)
  surface_material_score float|None    Intrinsic comfort score from material type (city MATERIAL or OSM surface tag)
  surface_confidence     float|None    Certainty about surface_score
  width_score              float|None  Sidewalk-width Comfort sub-score (from sidewalk_width_ft)
  environment_score        float|None  Arterial proximity × eyes-on-street (graph/environment.py)
  arterial_proximity_score float|None  Car-safety sub-signal (0 on an arterial → 1 far away)
  eyes_score               float|None  Perceived-safety sub-signal (frontage + enclosure)
  environment_confidence   float|None  Certainty about environment_score
  walk_score             float [0,1]   Composite walkability at DEFAULT weights (routing fast path)
  walk_confidence        float [0,1]   Composite confidence at DEFAULT weights
  foot_access            str|None      "yes" / "no" / "private" / None
  is_pedestrian_dedicated  bool
  edge_class             str           "pedestrian" | "shared" | "road" | "unknown"
  length                 float         Edge length in metres
  data_source            str           Highest-tier source that contributed
  sidewalk_condition     str|None      Raw SCI value (0–100) from city data
  sidewalk_width_ft      float|None    Sidewalk width in feet from city data
  sidewalk_survey_date   str|None      ISO date string from city data

DEVELOPMENT NOTE — query-time scoring
--------------------------------------
This module stores raw factor scores on each edge. The composite walkability
score is computed at ROUTING TIME using FACTOR_WEIGHTS from scoring/weights.py.
This allows weights to be adjusted (via Streamlit sliders or learning-to-rank)
without rebuilding the graph.

PRODUCTION TODO: Once weights are finalised, add a --precompute flag to
build() that calls scoring/factors.py and writes the composite score onto
each edge at build time. Query-time blending should become an optional
fallback for interactive weight adjustment only.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, cast

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd

from walkability.config import OSM_DIR, DATA_DIR
from walkability.graph.environment import build_environment_index
from walkability.graph.inventory import (
    BOSTON_PROFILE,
    CITY_PROFILES,
    CityProfile,
)
from walkability.osm.fallback import get_fallback
from walkability.osm.tag_resolver import resolve_edge_tags
from walkability.scoring.factors import edge_walkability
from walkability.scoring.weights import (
    SIDEWALK_WIDTH_GOOD_FT,
    SIDEWALK_WIDTH_MIN_FT,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# The default city. Every municipality-specific detail (inventory schema,
# condition scale, material vocabulary, metric CRS, I/O paths) lives in a
# CityProfile — see walkability/graph/inventory.py. Adding a city is
# adding a profile there; the pipeline below reads from the profile it is given.
DEFAULT_PROFILE = BOSTON_PROFILE

# Backward-compatible module-level paths (external code imports these). They
# alias the default profile so existing notebooks/app keep working unchanged.
GRAPH_PATH        = DEFAULT_PROFILE.graph_path
INVENTORY_PATH    = DEFAULT_PROFILE.inventory_path
ENRICHED_PATH     = DEFAULT_PROFILE.enriched_path
CENTERLINE_PATH   = DATA_DIR / "boston" / "sidewalk_centerline" / "sidewalk_centerline.shp"

# ---------------------------------------------------------------------------
# Spatial join parameters
# ---------------------------------------------------------------------------

# Hard cutoff for matching OSM edges to sidewalk inventory features.
# Deliberately smaller than the 15m buffer used in footway geometric
# inference — dense Boston grid needs tighter tolerance.
SPATIAL_JOIN_CUTOFF_M: float = 10.0

# ---------------------------------------------------------------------------
# City data confidence parameters
# ---------------------------------------------------------------------------

CONF_CITY_RECENT       = 1.00                              # Survey date 2020 or later
CONF_CITY_OLDER        = 0.85                              # Survey date 2000–2019
CONF_CITY_DATE_MISSING = round(CONF_CITY_OLDER * 0.85, 4) # 1970 placeholder, but inspected=yes
CONF_CITY_NO_DATE      = 0.60                              # Date field null or unparseable
# 1970 placeholder + inspected=null → city row skipped entirely (see _build_canonical_schema)

# When OSM classifies an edge as a major road (highway_score below this)
# but city data reports excellent sidewalk quality (above QUALITY_HIGH),
# the spatial join may have matched a nearby sidewalk rather than the road
# edge itself. Apply a confidence penalty.
OSM_MAJOR_ROAD_THRESHOLD = 0.20
CITY_QUALITY_HIGH        = 0.80
CONSISTENCY_PENALTY      = 0.75

# Both-sides aggregation (replaces the single-nearest "coin flip"): a street's
# two sidewalks both fall within SPATIAL_JOIN_CUTOFF_M of the centerline, so we
# average all matched candidates. When they genuinely disagree the single edge
# value is less trustworthy, so we down-weight its confidence (a routing/re-rank
# tiebreaker — never a hard cost term).
# Divergence sensitivity is per-city (profile.divergence_threshold), expressed in
# normalised [0,1] condition units so it is scale-agnostic (Boston 0.15 == 15 SCI
# points). The penalty applied when the two sides disagree is city-agnostic:
DIVERGENCE_PENALTY       = 0.85   # multiply surface_confidence on divergent edges

# Lever 3 (re-anchor): a pedestrian-DEDICATED recreational path (greenway / park
# path / promenade) with no city inventory data shouldn't be docked to the generic
# OSM-footway surface fallback (0.75) just for lacking a paved-surface tag — an
# unpaved park path is comfortable by design, not degraded (the jamaica_pond /
# Seaport HarborWalk case, both surface-fallback edges the user flagged). Where such
# an edge is also clearly recreational (open or road-separated), treat its unknown
# surface as a pleasant path rather than a middling default.
PED_PATH_COMFORT          = 0.90   # surface + material comfort for the above
PED_PATH_RECREATIONAL_MIN = 0.30   # min openness OR road_separation to qualify


# ---------------------------------------------------------------------------
# Inspection utility
# ---------------------------------------------------------------------------

def inspect_inventory_fields(path: Path = INVENTORY_PATH) -> None:
    """Print sidewalk inventory columns and sample values.

    Run this once after receiving a new vintage of the inventory to verify that
    the profile's field names (see inventory.py) are still correct.
    """
    gdf = gpd.read_file(path)
    print(f"Sidewalk inventory: {len(gdf)} features, CRS={gdf.crs}")
    print(f"\nColumns ({len(gdf.columns)}):")
    for col in gdf.columns:
        sample = gdf[col].dropna().iloc[:3].tolist() if not gdf[col].dropna().empty else []
        dtype  = gdf[col].dtype
        print(f"  {col:30s}  dtype={str(dtype):15s}  samples={sample}")


def diagnose_spatial_join(
    graph_path:     Path | None = None,
    inventory_path: Path | None = None,
    profile:        CityProfile = DEFAULT_PROFILE,
) -> None:
    """Diagnose why the spatial join may be returning 0 matches.

    Checks three common failure modes in order:
      1. CRS missing or wrong on the sidewalk file
      2. Bounding boxes don't overlap after reprojection
      3. sjoin column naming (geopandas version differences)

    Run with:
        python -c "from walkability.graph.build import diagnose_spatial_join; diagnose_spatial_join()"
    """
    import osmnx as ox

    graph_path     = graph_path if graph_path is not None else profile.graph_path
    inventory_path = inventory_path if inventory_path is not None else profile.inventory_path
    metric_crs     = profile.metric_crs

    print("=== Spatial join diagnostics ===\n")

    # --- 1. CRS check ---
    sidewalks = gpd.read_file(inventory_path)
    print(f"[1] Sidewalk CRS (raw from shapefile): {sidewalks.crs}")
    if sidewalks.crs is None:
        print("    ERROR: CRS is None. The shapefile is missing a .prj file.")
        print("    Fix: set it explicitly, e.g.")
        print("         sidewalks = sidewalks.set_crs('EPSG:26986')")
        print("    (26986 = NAD83 Massachusetts State Plane, common for Boston DPW data)")
        return

    G = ox.load_graphml(graph_path)
    nodes_gdf, edges_gdf = ox.graph_to_gdfs(G)
    print(f"    OSM edges CRS (from osmnx):         {edges_gdf.crs}")

    # --- 2. Bounding box overlap after reprojection ---
    swk_metric   = sidewalks.to_crs(metric_crs)
    edges_metric = edges_gdf[["geometry"]].to_crs(metric_crs)

    swk_bounds   = swk_metric.total_bounds    # [minx, miny, maxx, maxy]
    edge_bounds  = edges_metric.total_bounds

    print(f"\n[2] Bounding boxes in {metric_crs}:")
    print(f"    Sidewalks : x=[{swk_bounds[0]:.0f}, {swk_bounds[2]:.0f}]  "
          f"y=[{swk_bounds[1]:.0f}, {swk_bounds[3]:.0f}]")
    print(f"    OSM edges : x=[{edge_bounds[0]:.0f}, {edge_bounds[2]:.0f}]  "
          f"y=[{edge_bounds[1]:.0f}, {edge_bounds[3]:.0f}]")

    x_overlap = (swk_bounds[0] < edge_bounds[2]) and (edge_bounds[0] < swk_bounds[2])
    y_overlap = (swk_bounds[1] < edge_bounds[3]) and (edge_bounds[1] < swk_bounds[3])
    if not (x_overlap and y_overlap):
        print("    ERROR: bounding boxes do not overlap — no matches are possible.")
        print("    Likely cause: CRS was not read correctly, or wrong shapefile.")
        return
    print("    OK: bounding boxes overlap.")

    # --- 3. sjoin column naming ---
    edges_gdf = edges_gdf.reset_index()
    edges_gdf["_edge_id"] = list(zip(edges_gdf["u"], edges_gdf["v"], edges_gdf["key"]))
    edges_metric = edges_gdf[["_edge_id", "geometry"]].to_crs(metric_crs)

    swk_metric = swk_metric.copy()
    swk_metric["_swk_idx"] = swk_metric.index

    sample_edges = edges_metric.iloc[:200].copy()
    sample_edges["geometry"] = sample_edges.geometry.buffer(SPATIAL_JOIN_CUTOFF_M)

    joined = gpd.sjoin(
        sample_edges[["_edge_id", "geometry"]],
        swk_metric,
        how="left",
        predicate="intersects",
    )

    print(f"\n[3] sjoin result columns: {list(joined.columns)}")
    n_before = len(joined)
    n_matched = joined["_swk_idx"].notna().sum() if "_swk_idx" in joined.columns else 0

    if "_swk_idx" not in joined.columns:
        actual = [c for c in joined.columns if "swk" in c.lower() or "idx" in c.lower()]
        print(f"    ERROR: '_swk_idx' column missing after sjoin.")
        print(f"    Columns with 'swk'/'idx': {actual}")
        print(f"    Fix: update dropna and groupby to use '{actual[0] if actual else '?'}'")
    else:
        print(f"    OK: '_swk_idx' present. {n_matched}/{n_before} of first 200 "
              f"edges matched a sidewalk feature.")
        if n_matched == 0:
            print("    No matches in sample — geometry overlap exists but no features "
                  "within 10 m. Try increasing SPATIAL_JOIN_CUTOFF_M.")

    print("\n=== Done ===")


def inspect_edges(
    n:          int  = 5,
    source:     str  = "all",
    highway:    str | None = None,
    path:       Path = ENRICHED_PATH,
    seed:       int  = 42,
) -> None:
    """Print a random sample of enriched edges for a sanity check.

    Parameters
    ----------
    n       : number of edges to sample
    source  : which data tier to sample from —
                "all"            — no filter
                "city_inventory" — matched to Boston sidewalk shapefile
                "osm_tag"        — recognised OSM highway tag, no city match
                "context"        — inferred from neighbouring tagged edges
                "geometric"      — no tag at all, fell through to length heuristic
    highway : if given, filter to edges whose raw highway tag contains
              this string (e.g. "footway", "residential")
    path    : path to enriched GraphML (defaults to full graph)
    seed    : random seed for reproducibility

    Examples
    --------
    # 5 random edges across all tiers
    inspect_edges()

    # Confirm city-matched footways have real SCI scores
    inspect_edges(n=10, source="city_inventory", highway="footway")

    # OSM-tagged edges that didn't match the sidewalk inventory
    inspect_edges(n=5, source="osm_tag")

    # True geometric fallbacks (no highway tag, inferred from length)
    inspect_edges(n=8, source="geometric")

    # Reproducible sample — change seed to get a different draw
    inspect_edges(n=5, seed=99)
    """
    import random

    G = load_graph(path)

    FIELDS = [
        "highway", "highway_score", "highway_confidence",
        "surface_score", "surface_material_score", "surface_confidence",
        "walk_score", "walk_confidence",
        "foot_access", "is_pedestrian_dedicated", "edge_class",
        "length", "data_source",
        "sidewalk_condition", "sidewalk_width_ft", "sidewalk_survey_date",
    ]

    def _source_match(data_source: str) -> bool:
        if source == "all":
            return True
        if source == "city_inventory":
            return data_source == "city_inventory"
        if source == "osm_tag":
            return data_source.startswith("highway=") or data_source == "steps"
        if source == "context":
            return data_source.startswith("context")
        if source == "geometric":
            return data_source.startswith("no_tag")
        return True

    candidates = [
        (u, v, k, data)
        for u, v, k, data in G.edges(data=True, keys=True)
        if _source_match(data.get("data_source", ""))
        and (highway is None or highway in str(data.get("highway", "")))
    ]

    if not candidates:
        print(f"No edges match source={source!r}, highway={highway!r}.")
        return

    random.seed(seed)
    sample = random.sample(candidates, min(n, len(candidates)))

    label = f"source={source!r}" + (f", highway={highway!r}" if highway else "")
    print(f"=== Edge sample ({len(sample)} edges, {label}) ===\n")

    for u, v, k, data in sample:
        print(f"  Edge ({u}, {v}, key={k})")
        for field in FIELDS:
            val = data.get(field, "<missing>")
            print(f"    {field:<26}  {val}")
        print()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_graph(path: Path = GRAPH_PATH) -> nx.MultiDiGraph:
    """Load the saved OSM walk graph from GraphML."""
    print(f"Loading OSM graph from {path} ...")
    G = ox.load_graphml(path)
    n_edges = G.number_of_edges()
    print(f"  {G.number_of_nodes()} nodes, {n_edges} edges")
    return G


def load_sidewalk_inventory(
    path:    Path | None = None,
    profile: CityProfile = DEFAULT_PROFILE,
) -> gpd.GeoDataFrame:
    """Load and lightly validate a city's sidewalk inventory."""
    path = path if path is not None else profile.inventory_path
    print(f"Loading sidewalk inventory from {path} ...")
    gdf = gpd.read_file(path)
    print(f"  {len(gdf)} features, CRS={gdf.crs}")

    missing = [f for f in profile.required_fields if f not in gdf.columns]
    if missing:
        warnings.warn(
            f"Expected field(s) not found in {profile.name} sidewalk inventory: "
            f"{missing}. Run inspect_inventory_fields() to see available columns "
            f"and update the {profile.name} profile in inventory.py."
        )
    return gdf


# ---------------------------------------------------------------------------
# City data helpers
# ---------------------------------------------------------------------------
# The condition→score and material→score adapters are per-city and live on the
# CityProfile (profile.condition_to_score / profile.surface_score); the
# helpers below are city-agnostic.

def _width_to_score(width_ft: float | None) -> float | None:
    """Map a sidewalk width (feet) to a [0, 1] Comfort sub-score.

    Linear ramp between SIDEWALK_WIDTH_MIN_FT (→0) and SIDEWALK_WIDTH_GOOD_FT
    (→1). Returns None for missing / non-positive widths so the factor drops out
    rather than penalising an edge with no width data.
    """
    if width_ft is None or width_ft <= 0.0:
        return None
    span = SIDEWALK_WIDTH_GOOD_FT - SIDEWALK_WIDTH_MIN_FT
    score = (width_ft - SIDEWALK_WIDTH_MIN_FT) / span if span > 0 else 1.0
    return round(min(1.0, max(0.0, score)), 4)


def _date_confidence(raw_date: Any) -> float:
    """Return a confidence value based on the survey date field.

    Pre-2000 dates (e.g. 1970-01-01) are Unix-epoch placeholders in the Boston
    inventory — the field survey happened but the date was mis-logged.  Rows
    where inspected=null on top of a 1970 date are skipped entirely before this
    function is reached (see _build_canonical_schema).
    """
    if raw_date is None or (isinstance(raw_date, float) and pd.isna(raw_date)):
        return CONF_CITY_NO_DATE
    try:
        year = pd.to_datetime(raw_date).year
        if year < 2000:
            return CONF_CITY_DATE_MISSING  # Date corrupted but survey data is real
        elif year >= 2020:
            return CONF_CITY_RECENT
        else:
            return CONF_CITY_OLDER
    except Exception:
        return CONF_CITY_NO_DATE


def _city_surface_confidence(
    raw_date: Any,
    highway_score: float,
    city_quality: float | None,
) -> float:
    """Compute city-data confidence for a surface score.

    Accounts for:
      - Survey date (recent = higher confidence)
      - OSM consistency (if OSM says major road but city says excellent
        sidewalk, the spatial join may have matched the wrong feature)

    Parameters
    ----------
    raw_date      : raw date value from the inventory
    highway_score : OSM-derived highway score for this edge
    city_quality  : condition score derived from city data, or None
    """
    date_conf = _date_confidence(raw_date)

    # Consistency check: major road + excellent sidewalk = possible mismatch
    consistency = 1.0
    if (city_quality is not None
            and highway_score < OSM_MAJOR_ROAD_THRESHOLD
            and city_quality > CITY_QUALITY_HIGH):
        consistency = CONSISTENCY_PENALTY

    return round(date_conf * consistency, 4)


def _aggregate_city_candidates(
    rows:    gpd.GeoDataFrame,
    profile: CityProfile,
) -> dict | None:
    """Aggregate every sidewalk-inventory feature matched to one OSM edge.

    Replaces the earlier single-nearest "coin flip": a street's two sidewalks
    both fall within SPATIAL_JOIN_CUTOFF_M of the centerline, and keeping
    whichever centroid happened to be closer was arbitrary (and silently dropped
    the other side). Here we take a weight-weighted mean over the *valid*
    candidates (valid condition, width > 0; phantom never-surveyed features
    removed via ``profile.is_phantom``) and flag divergence — candidates whose
    normalised condition differs by more than ``profile.divergence_threshold``,
    or whose material disagrees — so the caller can down-weight confidence where
    our single edge value is least trustworthy.

    The per-candidate weight is polygon area (``profile.area_field``) where the
    inventory is polygonal (Boston), or the feature's projected geometry length
    where it is line/segment based (``area_field is None``; Austin). ``rows`` is
    already in ``profile.metric_crs`` (see ``_build_spatial_index``), so
    ``.geometry.length`` is in metres.

    Returns a dict keyed by the profile's own field names (so the schema consumer
    reads it like a source row), plus ``_cond_divergent`` / ``_n_valid`` /
    ``_cond_spread`` (normalised) for the confidence penalty and diagnostics.
    Returns ``None`` when nothing usable matched (→ edge falls through to OSM).
    """
    cond_field = profile.condition_field
    surf_field = profile.surface_field
    width_field = profile.width_field
    date_field = profile.date_field

    contrib = []  # tuples: (row, raw_date, cond01, weight)
    for _, r in rows.iterrows():
        if profile.is_phantom(r):
            continue  # feature exists but was never field-surveyed
        cond01 = profile.condition_to_score(r.get(cond_field))
        if cond01 is None:
            continue  # no valid condition measurement — don't pollute the mean
        weight = 0.0
        if profile.area_field is not None:
            try:
                weight = float(r.get(profile.area_field))
            except (TypeError, ValueError):
                weight = 0.0
        else:
            try:
                weight = float(r.geometry.length)  # metric CRS → metres
            except Exception:
                weight = 0.0
        if not (weight > 0):
            weight = 1.0
        raw_date = r.get(date_field) if date_field else None
        contrib.append((r, raw_date, cond01, weight))

    if not contrib:
        return None  # nothing usable → fall through to the OSM-tag tier

    wsum = sum(c[3] for c in contrib)
    cond_mean01 = sum(c[2] * c[3] for c in contrib) / wsum

    # Weight-weighted mean width over candidates that report a positive width.
    width_mean = None
    if width_field is not None:
        wnum = wden = 0.0
        for c in contrib:
            try:
                wid = float(c[0].get(width_field))
            except (TypeError, ValueError):
                wid = None
            if wid is not None and wid > 0:
                wnum += wid * c[3]
                wden += c[3]
        width_mean = round(wnum / wden, 2) if wden > 0 else None

    # Material: conservative — the lowest-comfort recognised token (don't average
    # categorical comfort upward).
    material_code = None
    if surf_field is not None:
        mats = []
        for c in contrib:
            code = c[0].get(surf_field)
            score = profile.surface_score(code)
            if score is not None:
                mats.append((score, str(code).strip().upper()))
        material_code = min(mats, key=lambda x: x[0])[1] if mats else None

    # Divergence: compare ONE representative per inventory SIDE (the largest-weight
    # feature on each side — the real flanking sidewalk), not a raw max−min over
    # every matched fragment. A tiny corner fragment from a perpendicular street is
    # within the buffer too, so raw spread wildly over-flags; the per-side
    # largest-weight pick is robust to that while still catching a genuine
    # left-vs-right disagreement.
    #
    # This is only meaningful when the inventory labels the SIDE. Without it
    # (``side_field is None``, e.g. Austin's line-segment inventory), a single
    # street edge matches MANY short segments strung ALONG its length — "top-2"
    # would compare two points on the same side with genuinely different
    # condition, i.e. along-street variance, not side disagreement (it flagged
    # ~70% of Austin edges). That is a different (and today unused) signal, so we
    # don't fabricate a side-divergence penalty from it.
    cond_spread = 0.0
    material_divergent = False
    if profile.side_field is not None:
        by_side: dict[str, tuple] = {}
        for c in contrib:
            side = str(c[0].get(profile.side_field)).upper()
            if side not in by_side or c[3] > by_side[side][3]:
                by_side[side] = c
        side_reps = sorted(by_side.values(), key=lambda c: c[3], reverse=True)[:2]
        if len(side_reps) >= 2:
            a, b = side_reps[0], side_reps[1]
            cond_spread = abs(a[2] - b[2])  # normalised [0, 1] condition units
            ma = profile.surface_score(a[0].get(surf_field)) if surf_field else None
            mb = profile.surface_score(b[0].get(surf_field)) if surf_field else None
            material_divergent = (
                ma is not None and mb is not None
                and str(a[0].get(surf_field)).strip().upper()
                != str(b[0].get(surf_field)).strip().upper()
            )

    # Freshest contributing date + inspected flag — these drive _date_confidence
    # and the phantom re-check in _build_canonical_schema. Because phantom rows are
    # already excluded, the re-check will not wrongly drop the aggregate.
    def _dt(c):
        try:
            return pd.to_datetime(c[1])
        except Exception:
            return pd.Timestamp.min

    agg_date = max(contrib, key=_dt)[1]
    inspected_val = "yes" if any(
        str(c[0].get("inspected")).lower() == "yes" for c in contrib
    ) else None

    divergent = (cond_spread > profile.divergence_threshold) or material_divergent

    agg: dict = {
        cond_field:        profile.aggregate_condition(cond_mean01),
        "inspected":       inspected_val,
        "_cond_divergent": divergent,
        "_n_valid":        len(contrib),
        "_cond_spread":    round(cond_spread, 4),
    }
    if surf_field is not None:
        agg[surf_field] = material_code
    if width_field is not None:
        agg[width_field] = width_mean
    if date_field is not None:
        agg[date_field] = agg_date
    return agg


# ---------------------------------------------------------------------------
# Spatial join (bulk, pre-computed before the edge loop)
# ---------------------------------------------------------------------------

def _build_spatial_index(
    G: nx.MultiDiGraph,
    sidewalks: gpd.GeoDataFrame,
    profile: CityProfile = DEFAULT_PROFILE,
) -> dict[tuple, dict]:
    """Bulk spatial join: map each OSM edge to an aggregate of its matched sidewalks.

    Returns a dict keyed by (u, v, key) → an aggregated record (see
    _aggregate_city_candidates) carrying mean SCI / conservative material / mean
    width plus a divergence flag. Edges with no usable match within
    SPATIAL_JOIN_CUTOFF_M are absent from the dict.

    Steps:
      1. Build edge GeoDataFrame from OSM graph (uses stored edge geometry
         where available, otherwise straight line between node coords)
      2. Reproject both datasets to metric CRS
      3. Buffer OSM edges by SPATIAL_JOIN_CUTOFF_M to create match zones
      4. Spatial join against sidewalk features
      5. Aggregate ALL candidate matches per edge (both sidewalks of a street),
         not just the nearest, flagging divergence for confidence down-weighting
    """
    print("Building edge GeoDataFrame from OSM graph ...")
    nodes_gdf, edges_gdf = ox.graph_to_gdfs(G)

    # edges_gdf index is (u, v, key) — preserve it as a column for lookup
    edges_gdf = edges_gdf.reset_index()    # columns: u, v, key, geometry, ...
    edges_gdf["_edge_id"] = list(zip(edges_gdf["u"], edges_gdf["v"], edges_gdf["key"]))

    print(f"Reprojecting to {profile.metric_crs} for distance calculations ...")
    edges_metric = edges_gdf[["_edge_id", "geometry"]].to_crs(profile.metric_crs)
    swk_metric   = sidewalks.to_crs(profile.metric_crs).copy()
    swk_metric["_swk_idx"] = swk_metric.index   # preserve original index

    # Buffer edges — anything within SPATIAL_JOIN_CUTOFF_M matches
    edges_buffered = edges_metric.copy()
    edges_buffered["geometry"] = edges_metric.geometry.buffer(SPATIAL_JOIN_CUTOFF_M)

    print(f"Spatial join (cutoff={SPATIAL_JOIN_CUTOFF_M}m) ...")
    joined = gpd.sjoin(
        edges_buffered[["_edge_id", "geometry"]],
        swk_metric,
        how="left",
        predicate="intersects",
    )
    joined = joined.dropna(subset=["_swk_idx"])

    if joined.empty:
        warnings.warn("Spatial join returned no matches. Check CRS and file paths.")
        return {}

    # Aggregate ALL candidates per edge (both sidewalks of a street fall within
    # the buffer) rather than keeping a single arbitrary nearest match — see
    # _aggregate_city_candidates.
    best_matches: dict[tuple, dict] = {}
    for edge_id, group in joined.groupby("_edge_id"):
        swk_rows = swk_metric.loc[swk_metric["_swk_idx"].isin(group["_swk_idx"].values)]
        if swk_rows.empty:
            continue
        agg = _aggregate_city_candidates(swk_rows, profile)
        if agg is None:
            continue
        best_matches[cast(tuple, edge_id)] = agg

    n_matched = len(best_matches)
    n_total   = len(edges_gdf)
    n_multi   = sum(1 for a in best_matches.values() if a["_n_valid"] >= 2)
    n_div     = sum(1 for a in best_matches.values() if a["_cond_divergent"])
    # Spread is normalised [0,1] condition units; print in "points" (×100) so the
    # numbers read like Boston's old 0–100 SCI-point spread for continuity.
    spreads   = sorted(a["_cond_spread"] for a in best_matches.values() if a["_n_valid"] >= 2)
    print(f"  Matched {n_matched}/{n_total} edges ({100*n_matched//max(n_total,1)}%) "
          f"to sidewalk inventory features")
    if spreads:
        p50 = spreads[len(spreads) // 2]
        p90 = spreads[min(len(spreads) - 1, int(len(spreads) * 0.9))]
        print(f"  Both-sides aggregate: {n_multi} multi-candidate edges "
              f"({100*n_multi//max(n_matched,1)}%), {n_div} divergent "
              f"({100*n_div//max(n_matched,1)}%); condition spread (×100) "
              f"median={p50*100:.0f} p90={p90*100:.0f}")
    return best_matches


# ---------------------------------------------------------------------------
# Canonical schema builder (per edge)
# ---------------------------------------------------------------------------

def _build_canonical_schema(
    raw_data:   dict,
    fallback:   Any,           # FallbackResult
    city_row:   dict | None,   # aggregated record from _aggregate_city_candidates
    env:        dict | None = None,
    profile:    CityProfile = DEFAULT_PROFILE,
) -> dict:
    """Produce the canonical attribute dict for one edge.

    City data overrides fallback values where present and confidence
    justifies it. Both score and confidence are always stored separately.
    """
    resolved    = resolve_edge_tags(raw_data)
    foot_access = resolved.get("foot") or resolved.get("access")

    # --- Start from fallback values ---
    highway_score          = fallback.highway_score
    highway_confidence     = fallback.highway_confidence
    surface_score          = fallback.surface_score      # structural condition
    surface_material_score = fallback.surface_score      # intrinsic comfort; mirrors condition when OSM surface tag is the only source
    surface_confidence     = None
    data_source            = fallback.inferred_from[0] if fallback.inferred_from else "fallback"

    # Raw city fields for audit trail
    sidewalk_condition   = None
    sidewalk_width_ft    = None
    sidewalk_survey_date = None

    # --- Override with city data if available ---
    if city_row is not None:
        # A phantom aggregate (e.g. Boston's 1970-placeholder date + inspected
        # null, or Austin's PENDING ASSESSMENT) means the sidewalk was never
        # field-surveyed; only the geometry exists.  Skip city data so we fall
        # through to the OSM-tag tier rather than using a phantom match. (Phantom
        # candidates are already excluded inside the aggregate, so this is a
        # belt-and-braces re-check on the synthetic aggregate record.)
        if profile.is_phantom(city_row):
            city_row = None

    if city_row is not None:
        raw_cond   = city_row.get(profile.condition_field)
        raw_date   = city_row.get(profile.date_field) if profile.date_field else None
        raw_width  = city_row.get(profile.width_field) if profile.width_field else None
        raw_surf   = city_row.get(profile.surface_field) if profile.surface_field else None

        city_quality = profile.condition_to_score(raw_cond)
        city_conf    = _city_surface_confidence(raw_date, highway_score, city_quality)

        # Down-weight confidence where the two sides genuinely disagree (the
        # aggregate is then a less trustworthy single value for the edge).
        if city_row.get("_cond_divergent"):
            city_conf = round(city_conf * DIVERGENCE_PENALTY, 4)

        if city_quality is not None:
            surface_score      = city_quality   # normalised condition — structural
            surface_confidence = city_conf
            data_source        = "city_inventory"

        city_surf_score = profile.surface_score(raw_surf)
        if city_surf_score is not None:
            surface_material_score = city_surf_score  # material → intrinsic comfort
        else:
            surface_material_score = None             # unknown material — don't fabricate

        sidewalk_condition   = str(raw_cond) if raw_cond is not None else None
        try:
            sidewalk_width_ft = float(raw_width) if raw_width is not None and not pd.isna(raw_width) else None
        except (ValueError, TypeError):
            sidewalk_width_ft = None
        sidewalk_survey_date = str(pd.to_datetime(raw_date).date()) if raw_date is not None else None

    # --- Lever 3: unpaved recreational-path comfort lift (no city data) ---
    if (data_source != "city_inventory" and fallback.is_pedestrian_dedicated and env
            and max(env.get("openness_score") or 0.0,
                    env.get("road_separation") or 0.0) >= PED_PATH_RECREATIONAL_MIN):
        surface_score          = max(surface_score or 0.0, PED_PATH_COMFORT)
        surface_material_score = max(surface_material_score or 0.0, PED_PATH_COMFORT)

    schema = {
        # Scores and confidence
        "highway_score":           round(highway_score, 4),
        "highway_confidence":      round(highway_confidence, 4),
        "surface_score":           round(surface_score, 4) if surface_score is not None else None,
        "surface_material_score":  round(surface_material_score, 4) if surface_material_score is not None else None,
        "surface_confidence":      round(surface_confidence, 4) if surface_confidence is not None else None,

        # Comfort: sidewalk room from city width data (None where unknown).
        "width_score":             _width_to_score(sidewalk_width_ft),

        # Environment = car_safety × eyes-on-street (graph/environment.py), where
        # car_safety = min(on-path maxspeed, off-path arterial proximity). Absent
        # when feature inputs weren't available — the factor then drops out.
        "environment_score":        (env or {}).get("environment_score"),
        "car_safety_score":         (env or {}).get("car_safety_score"),
        "maxspeed_safety_score":    (env or {}).get("maxspeed_safety_score"),
        "arterial_proximity_score": (env or {}).get("arterial_proximity_score"),
        "eyes_score":               (env or {}).get("eyes_score"),
        "environment_confidence":   (env or {}).get("environment_confidence"),
        # Sub-signals for diagnostics + offline lever isolation (env-rework §6).
        "industrial_exposure":      (env or {}).get("industrial_exposure"),
        "road_separation":          (env or {}).get("road_separation"),
        "eyes_uncapped":            (env or {}).get("eyes_uncapped"),
        "openness_score":           (env or {}).get("openness_score"),

        # Categorical
        "foot_access":            foot_access,
        "is_pedestrian_dedicated": fallback.is_pedestrian_dedicated,
        "edge_class":             fallback.edge_class,

        # Physical
        "length":                 raw_data.get("length"),

        # Audit
        "data_source":            data_source,

        # City data (raw, for debugging and future refinement)
        "sidewalk_condition":     sidewalk_condition,
        "sidewalk_width_ft":      sidewalk_width_ft,
        "sidewalk_survey_date":   sidewalk_survey_date,
    }

    # Precompute the composite walkability score with the DEFAULT factor weights
    # so routing has a zero-parse fast path (see scoring/factors.edge_walkability
    # and the "query-time scoring" note in this module's docstring). The router
    # recomputes from the per-factor fields above whenever non-default weights
    # are supplied (UI sliders), so this bake never locks the weighting in.
    walk_score, walk_confidence = edge_walkability(schema)
    schema["walk_score"]      = round(walk_score, 4)
    schema["walk_confidence"] = round(walk_confidence, 4)

    return schema


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_edge_schema(
    G:        nx.MultiDiGraph,
    sidewalks: gpd.GeoDataFrame,
    profile:  CityProfile = DEFAULT_PROFILE,
) -> nx.MultiDiGraph:
    """Enrich every edge in G with the canonical walkability schema.

    Modifies G in-place and returns it.
    """
    city_matches = _build_spatial_index(G, sidewalks, profile)
    env_matches  = build_environment_index(G, profile)

    print("Enriching edges ...")
    n_edges   = G.number_of_edges()
    n_city    = 0
    n_context = 0
    n_osm     = 0
    n_geom    = 0

    for i, (u, v, key, data) in enumerate(G.edges(data=True, keys=True)):
        if i % 5000 == 0:
            print(f"  {i}/{n_edges} edges processed ...")

        resolved  = resolve_edge_tags(data)
        fallback  = get_fallback(resolved, G=G, u=u, v=v, key=key)
        city_row  = city_matches.get((u, v, key))
        env       = env_matches.get((u, v, key))
        schema    = _build_canonical_schema(data, fallback, city_row, env, profile)

        G[u][v][key].update(schema)

        # Tally sources for summary.
        # data_source values by tier:
        #   "city_inventory"     — city shapefile match
        #   "context:..."        — inferred from neighbouring tagged edges
        #   "highway=<type>"     — explicit OSM tag, no city match
        #   "no_tag"             — no tag, fell through to geometry
        src = schema["data_source"]
        if src == "city_inventory":
            n_city    += 1
        elif src.startswith("context"):
            n_context += 1
        elif src.startswith("highway=") or src == "steps":
            n_osm     += 1
        else:
            n_geom    += 1

    print(f"\nEnrichment complete:")
    print(f"  City inventory match : {n_city:>6} edges")
    print(f"  OSM tag only         : {n_osm:>6} edges")
    print(f"  Context inference    : {n_context:>6} edges")
    print(f"  Geometric fallback   : {n_geom:>6} edges")
    return G


def save_graph(G: nx.MultiDiGraph, path: Path = ENRICHED_PATH) -> None:
    """Serialise the enriched graph to GraphML."""
    print(f"Saving enriched graph to {path} ...")
    ox.save_graphml(G, path)
    print("  Done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build(
    graph_path:     Path | None = None,
    inventory_path: Path | None = None,
    output_path:    Path | None = None,
    force:          bool = False,
    profile:        CityProfile = DEFAULT_PROFILE,
) -> nx.MultiDiGraph:
    """Full pipeline: load → enrich → save → return enriched graph.

    Paths default to the profile's own I/O (``profile.graph_path`` etc.), so
    ``build(profile=AUSTIN_PROFILE)`` is fully wired once Austin's inputs exist.

    If *output_path* already exists and *force* is False, the cached
    enriched graph is returned immediately without rebuilding.  Pass
    ``force=True`` (or ``--force`` on the CLI) after changing enrichment
    logic so the cache is regenerated.
    """
    graph_path     = graph_path if graph_path is not None else profile.graph_path
    inventory_path = inventory_path if inventory_path is not None else profile.inventory_path
    output_path    = output_path if output_path is not None else profile.enriched_path

    if not force and output_path.exists():
        print(f"Enriched graph already cached at {output_path} — loading it.")
        print("  Pass force=True (or --force on the CLI) to rebuild.")
        return load_graph(output_path)

    G         = load_graph(graph_path)
    sidewalks = load_sidewalk_inventory(inventory_path, profile)
    G         = build_edge_schema(G, sidewalks, profile)
    save_graph(G, output_path)
    return G


_DEV_DEFAULT_RADIUS_M: float = 500.0

# Beacon Hill keeps the original filename so existing caches/baselines/notebooks
# that reference DEV_ENRICHED_PATH keep working.
DEV_ENRICHED_PATH = OSM_DIR / "boston_walk_dev.graphml"

# Named dev regions for diagnostics. Beacon Hill is the walkable reference; the
# others were chosen (and verified) for LOWER walkability so the audit flags
# actually fire — see the mean-walk / arterial figures in each note. Apply the
# same diagnostics (notebooks/diagnostics.py) to any of them.
DEV_REGIONS: dict[str, dict] = {
    "beacon_hill": {
        "lat": 42.3588, "lon": -71.0707, "radius_m": 500.0,
        "note": "reference — uniformly walkable historic core (mean walk ~0.79, ~1% arterial)",
    },
    "charlestown_sullivan": {
        "lat": 42.3840, "lon": -71.0700, "radius_m": 600.0,
        "note": "Rutherford Ave / Sullivan Sq — car-dominated (mean walk ~0.71, ~6% arterial)",
    },
    "newmarket_massave": {
        "lat": 42.3330, "lon": -71.0660, "radius_m": 600.0,
        "note": "Mass Ave / Melnea Cass — industrial arterials (mean walk ~0.61, lowest; 7% restricted)",
    },
    "nubian_roxbury": {
        "lat": 42.3290, "lon": -71.0830, "radius_m": 600.0,
        "note": "Nubian Sq / Washington St — highest arterial exposure (~11% arterial)",
    },
    "brookline_seam": {
        "lat": 42.3450, "lon": -71.1170, "radius_m": 800.0,
        "note": "St Mary's St / BU Bridge — Boston↔Brookline data seam; Brookline "
                "side is OSM-tier only (no city sidewalk inventory)",
    },
    "metro_hull_seam": {
        "lat": 42.3730, "lon": -71.0740, "radius_m": 800.0,
        "note": "Gilmore Bridge / Charlestown↔East Cambridge — Boston↔hull-town "
                "data seam (Cambridge/Somerville/Everett/Chelsea are OSM-tier "
                "only, like Brookline)",
    },
}


def dev_region_path(region: str) -> Path:
    """GraphML path for a named dev region (beacon_hill keeps the legacy name)."""
    if region == "beacon_hill":
        return DEV_ENRICHED_PATH
    return OSM_DIR / f"boston_walk_dev_{region}.graphml"


def build_dev_subset(
    region:         str = "beacon_hill",
    *,
    radius_m:       float | None = None,
    inventory_path: Path | None = None,
    force:          bool  = False,
    profile:        CityProfile = DEFAULT_PROFILE,
) -> nx.MultiDiGraph:
    """Build and enrich the edges within a radius of a named region's centre.

    Intended for fast iteration and for applying the diagnostics to areas with
    different walkability profiles. Each region is saved to its own GraphML
    file (see ``dev_region_path``) so subsets never overwrite each other or the
    full enriched graph.

    Parameters
    ----------
    region
        Key into ``DEV_REGIONS``. ``"beacon_hill"`` is the walkable reference;
        the others are deliberately less walkable test beds.
    radius_m
        Override the region's default radius. 500 m ≈ a 10-minute walk diameter.
    force
        If False and the subset already exists, return the cached copy.
    """
    if region not in DEV_REGIONS:
        raise ValueError(f"Unknown region {region!r}. Known: {sorted(DEV_REGIONS)}")
    cfg = DEV_REGIONS[region]
    center_lat, center_lon = cfg["lat"], cfg["lon"]
    radius_m = radius_m if radius_m is not None else cfg["radius_m"]
    output_path = dev_region_path(region)

    if not force and output_path.exists():
        print(f"Dev subset '{region}' already cached at {output_path} — loading it.")
        print("  Pass force=True to rebuild the subset.")
        return load_graph(output_path)

    print(f"Building dev region '{region}': {cfg['note']}")
    print(f"Clipping graph to {radius_m:.0f} m network radius around "
          f"({center_lat}, {center_lon}) ...")
    G_full      = load_graph(profile.graph_path)
    center_node = min(
        G_full.nodes(data=True),
        key=lambda n: (n[1]["y"] - center_lat) ** 2 + (n[1]["x"] - center_lon) ** 2,
    )[0]
    G_subset    = ox.truncate.truncate_graph_dist(G_full, center_node, dist=radius_m)
    n_edges = G_subset.number_of_edges()
    print(f"  Subset: {G_subset.number_of_nodes()} nodes, {n_edges} edges "
          f"({100 * n_edges // G_full.number_of_edges()}% of full graph)")

    sidewalks = load_sidewalk_inventory(inventory_path, profile)
    G_subset  = build_edge_schema(G_subset, sidewalks, profile)
    save_graph(G_subset, output_path)
    return G_subset


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the enriched walk graph for a city."
    )
    parser.add_argument(
        "--city", default=DEFAULT_PROFILE.name, choices=sorted(CITY_PROFILES),
        help=f"City inventory profile to build (default: {DEFAULT_PROFILE.name}).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Rebuild even if the enriched graph already exists.",
    )
    parser.add_argument(
        "--dev", action="store_true",
        help="Build a small dev subset instead of the full graph.",
    )
    parser.add_argument(
        "--region", default="beacon_hill",
        help=f"Dev region to build (default: beacon_hill). Choices: {sorted(DEV_REGIONS)}",
    )
    parser.add_argument(
        "--list-regions", action="store_true",
        help="List the available dev regions and exit.",
    )
    parser.add_argument(
        "--radius", type=float, default=None,
        help="Override the region's default radius in metres.",
    )
    args = parser.parse_args()

    profile = CITY_PROFILES[args.city]

    if args.list_regions:
        print("Available dev regions:")
        for name, cfg in DEV_REGIONS.items():
            print(f"  {name:22} {cfg['note']}")
    elif args.dev:
        build_dev_subset(region=args.region, radius_m=args.radius,
                         force=args.force, profile=profile)
    else:
        build(force=args.force, profile=profile)