"""
Environment factor: per-edge "what kind of place is this" scoring.

This is the enrichment tier behind the ``environment_score`` edge field
(scoring/weights.py::FACTOR_WEIGHTS["environment"]). It captures two things that
``highway=*`` alone does not — and that a weight tweak alone could never fix
(the "environment overrating" bug in CLAUDE.md):

  1. arterial_proximity_score — CAR SAFETY. How far the edge is from a
     high-speed arterial (motorway / trunk / primary / secondary). A quiet
     residential street pinned against an expressway is hostile even though its
     own ``highway`` tag looks benign. Crucially the WALK graph excludes
     motorway/trunk, so the arterial geometry is pulled separately
     (graph/download_environment.py) and matched here by nearest distance.
  2. eyes_score — PERCEIVED SOCIAL SAFETY ("eyes on the street", Jane Jacobs).
     Driven by active frontage (shops/amenities) and built enclosure
     (buildings) near the edge, knocked down for back-alley geometry. A street
     dotted with shops feels watched and safe; an isolated footpath or a back
     alley behind blank walls does not.

The two are combined as a GEOMETRIC mean so the composite is high only when BOTH
are high — exactly the desired behaviour: car-unsafe OR socially-unsafe streets
both collapse toward 0, and only a street that is both calm and watched scores
high.

All numerical parameters live in scoring/weights.py (single source of truth).
This module is build-time only: it reads cached OSM feature files and writes
floats onto edges, which then bake into ``walk_score``. The deployed runtime
loads those baked fields and never touches this module.

Spatial work uses geopandas ``sjoin`` / ``sjoin_nearest`` (shapely 2.x STRtree)
— deliberately no scipy / scikit-learn (see CLAUDE.md "Dev workflow note").
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd

from walkability.config import OSM_DIR
from walkability.scoring.weights import (
    ARTERIAL_REACH_M,
    ENV_CONFIDENCE,
    EYES_ALLEY_FACTOR,
    EYES_BLDG_SAT,
    EYES_BUFFER_M,
    EYES_POI_SAT,
    EYES_W_BLDG,
    EYES_W_POI,
)

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
# Cached OSM feature inputs produced by graph/download_environment.py.
ARTERIALS_PATH = OSM_DIR / "boston_arterials.gpkg"
BUILDINGS_PATH = OSM_DIR / "boston_buildings.gpkg"
POIS_PATH      = OSM_DIR / "boston_pois.gpkg"

# Metric CRS for distance/buffer maths — UTM Zone 19N covers Boston. Matches
# graph/build.py::METRIC_CRS (duplicated here to avoid a circular import; build
# imports this module).
METRIC_CRS = "EPSG:32619"

# When enriching a dev SUBSET, features just outside the clip still influence its
# boundary edges (an arterial 50 m past the edge of Beacon Hill is still real).
# Load features over the edge bounding box plus this margin so proximity/eyes are
# not underestimated at the subset boundary.
AREA_MARGIN_M: float = 250.0

# Smallest arterial reach — fallback for an arterial whose class we can't resolve
# (shouldn't happen, since we only pull ARTERIAL_HIGHWAY_TAGS).
_DEFAULT_REACH_M: float = min(ARTERIAL_REACH_M.values())


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _missing_inputs() -> list[Path]:
    """Return the cached feature files that don't yet exist."""
    return [p for p in (ARTERIALS_PATH, BUILDINGS_PATH, POIS_PATH) if not p.exists()]


def load_arterials() -> gpd.GeoDataFrame:
    """Cached arterial road geometry (incl. motorway/trunk), in METRIC_CRS."""
    gdf = gpd.read_file(ARTERIALS_PATH).to_crs(METRIC_CRS)
    # Keep only line geometry — distance-to-road is meaningless for stray points.
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    return gdf


def load_buildings() -> gpd.GeoDataFrame:
    """Cached building footprints, in METRIC_CRS."""
    return gpd.read_file(BUILDINGS_PATH).to_crs(METRIC_CRS)


def load_pois() -> gpd.GeoDataFrame:
    """Cached shop/amenity POIs (active frontage), in METRIC_CRS."""
    return gpd.read_file(POIS_PATH).to_crs(METRIC_CRS)


# ---------------------------------------------------------------------------
# Scalar score helpers (parameters from scoring/weights.py)
# ---------------------------------------------------------------------------

def _arterial_reach(highway) -> float | None:
    """Reach (m) for an arterial's ``highway`` tag, or None if not an arterial.

    ``_link`` ramps inherit their base class's reach. Multi-valued tags take the
    longest reach (the most hostile class present).
    """
    if isinstance(highway, (list, tuple)):
        reaches = [r for r in (_arterial_reach(h) for h in highway) if r is not None]
        return max(reaches) if reaches else None
    if not isinstance(highway, str):
        return None
    base = highway[:-5] if highway.endswith("_link") else highway
    return ARTERIAL_REACH_M.get(base)


def arterial_proximity_score(distance_m: float, reach_m: float) -> float:
    """0 on/adjacent to the arterial, ramping linearly to 1 at its reach."""
    if reach_m <= 0:
        return 1.0
    return max(0.0, min(1.0, distance_m / reach_m))


def eyes_score(poi_count: int, bldg_count: int, *, is_alley: bool) -> float:
    """Perceived-safety score from nearby frontage and enclosure.

    Each count passes through a saturating curve (the first few matter most),
    POIs weighted above raw buildings (a shopfront has more "eyes" than a blank
    wall), then an explicit penalty for back-alley geometry.
    """
    activity = 1.0 - math.exp(-poi_count / EYES_POI_SAT)
    presence = 1.0 - math.exp(-bldg_count / EYES_BLDG_SAT)
    score = EYES_W_POI * activity + EYES_W_BLDG * presence
    if is_alley:
        score *= EYES_ALLEY_FACTOR
    return max(0.0, min(1.0, score))


def _is_alley(highway, service) -> bool:
    """True for tagged back alleys (``service=alley``)."""
    svals = service if isinstance(service, (list, tuple)) else [service]
    return any(s == "alley" for s in svals)


# ---------------------------------------------------------------------------
# Bulk per-edge index (mirrors build.py::_build_spatial_index)
# ---------------------------------------------------------------------------

def build_environment_index(G: nx.MultiDiGraph) -> dict[tuple, dict]:
    """Map every edge (u, v, key) → its environment sub-scores.

    Returns a dict keyed by (u, v, key) with
    ``{arterial_proximity_score, eyes_score, environment_score,
    environment_confidence}``. If the cached feature inputs are missing, returns
    an empty dict (a warning is printed) so the rest of the build still runs —
    edges simply get no environment_score and the factor drops out of the
    weighted mean (consistent with the pipeline's None≠0 philosophy).
    """
    missing = _missing_inputs()
    if missing:
        warnings.warn(
            "Environment feature inputs missing: "
            f"{[p.name for p in missing]}. Skipping the environment factor. "
            "Run `python walkability/graph/download_environment.py` to fetch them."
        )
        return {}

    print("Building edge GeoDataFrame for environment factor ...")
    _, edges_gdf = ox.graph_to_gdfs(G)
    edges_gdf = edges_gdf.reset_index()    # columns: u, v, key, geometry, highway, ...
    # NB: keep column names free of a leading underscore — pandas itertuples /
    # some geopandas paths rename underscore-prefixed columns.
    edges_gdf["edge_id"] = list(zip(edges_gdf["u"], edges_gdf["v"], edges_gdf["key"]))

    keep = ["edge_id", "geometry", "highway"]
    if "service" in edges_gdf.columns:
        keep.append("service")
    edges_metric = edges_gdf[keep].to_crs(METRIC_CRS)

    # Load features once, clipped to the edge bbox + margin (matters for subsets).
    minx, miny, maxx, maxy = edges_metric.total_bounds
    minx, miny = minx - AREA_MARGIN_M, miny - AREA_MARGIN_M
    maxx, maxy = maxx + AREA_MARGIN_M, maxy + AREA_MARGIN_M
    arterials = load_arterials().cx[minx:maxx, miny:maxy]
    buildings = load_buildings().cx[minx:maxx, miny:maxy]
    pois      = load_pois().cx[minx:maxx, miny:maxy]
    print(f"  Features in area: {len(arterials)} arterials, "
          f"{len(buildings)} buildings, {len(pois)} POIs")

    n = len(edges_metric)
    art_scores  = _arterial_scores(edges_metric, arterials)
    poi_counts  = _buffer_counts(edges_metric, pois)
    bldg_counts = _buffer_counts(edges_metric, buildings)

    service_col = (edges_metric["service"] if "service" in edges_metric.columns
                   else [None] * n)

    index: dict[tuple, dict] = {}
    for eid, hwy, svc in zip(edges_metric["edge_id"], edges_metric["highway"], service_col):
        is_alley = _is_alley(hwy, svc)
        a = art_scores.get(eid, 1.0)                 # no arterial nearby → safe
        e = eyes_score(poi_counts.get(eid, 0),
                       bldg_counts.get(eid, 0),
                       is_alley=is_alley)
        index[eid] = {
            "arterial_proximity_score": round(a, 4),
            "eyes_score":               round(e, 4),
            "environment_score":        round(math.sqrt(a * e), 4),
            "environment_confidence":   ENV_CONFIDENCE,
        }
    print(f"  Scored environment for {len(index)}/{n} edges")
    return index


def _arterial_scores(
    edges_metric: gpd.GeoDataFrame,
    arterials:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge arterial_proximity_score via one nearest-arterial join."""
    if arterials.empty:
        return {}

    art = arterials.copy()
    art["reach"] = art["highway"].map(
        lambda h: _arterial_reach(h) or _DEFAULT_REACH_M
    )
    joined = gpd.sjoin_nearest(
        edges_metric[["edge_id", "geometry"]],
        art[["geometry", "reach"]],
        how="left",
        distance_col="dist",
    )
    # Ties (equidistant arterials) yield multiple rows — keep the nearest.
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")

    scores: dict[tuple, float] = {}
    for eid, dist, reach in zip(joined["edge_id"], joined["dist"], joined["reach"]):
        if pd.isna(dist) or pd.isna(reach):
            scores[eid] = 1.0
        else:
            scores[eid] = arterial_proximity_score(float(dist), float(reach))
    return scores


def _buffer_counts(
    edges_metric: gpd.GeoDataFrame,
    features:     gpd.GeoDataFrame,
) -> dict[tuple, int]:
    """Count features within EYES_BUFFER_M of each edge (one buffered sjoin)."""
    if features.empty:
        return {}

    buffered = edges_metric[["edge_id", "geometry"]].copy()
    buffered["geometry"] = edges_metric.geometry.buffer(EYES_BUFFER_M)
    joined = gpd.sjoin(
        buffered,
        features[["geometry"]],
        how="inner",
        predicate="intersects",
    )
    counts = joined.groupby("edge_id").size()
    return {eid: int(c) for eid, c in counts.items()}
