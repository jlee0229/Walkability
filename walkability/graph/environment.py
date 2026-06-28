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
import re
import warnings
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd

from walkability.config import OSM_DIR
from walkability.scoring.weights import (
    ARTERIAL_REACH_M,
    CAR_SAFETY_CEIL,
    DEFAULT_MAXSPEED_MPH,
    EYES_CEIL,
    ENV_CONFIDENCE,
    EYES_BLDG_SAT,
    EYES_BUFFER_M,
    EYES_POI_SAT,
    INDUSTRIAL_CAR_PENALTY,
    INDUSTRIAL_ENCLOSURE_DISCOUNT,
    INDUSTRIAL_REACH_M,
    MAXSPEED_SAFETY_ANCHORS,
    OPENNESS_REACH_M,
    OPENSPACE_MIN_AREA_M2,
    PEDESTRIAN_HIGHWAYS,
    POI_NOISE_AMENITIES,
    SEPARATION_REACH_M,
)

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
# Cached OSM feature inputs produced by graph/download_environment.py.
ARTERIALS_PATH = OSM_DIR / "boston_arterials.gpkg"
BUILDINGS_PATH = OSM_DIR / "boston_buildings.gpkg"
POIS_PATH      = OSM_DIR / "boston_pois.gpkg"
OPENSPACE_PATH = OSM_DIR / "boston_openspace.gpkg"
LANDUSE_PATH   = OSM_DIR / "boston_landuse.gpkg"   # industrial polygons (A); optional
ROADS_PATH     = OSM_DIR / "boston_roads.gpkg"     # all roads, for separation (B); optional

# Metric CRS for distance/buffer maths — UTM Zone 19N covers Boston. Matches
# graph/build.py::METRIC_CRS (duplicated here to avoid a circular import; build
# imports this module).
METRIC_CRS = "EPSG:32619"

# When enriching a dev SUBSET, features just outside the clip still influence its
# boundary edges (an arterial 50 m past the edge of Beacon Hill is still real).
# Load features over the edge bounding box plus this margin so proximity/eyes are
# not underestimated at the subset boundary.
AREA_MARGIN_M: float = 250.0

# Fallback reach for an arterial whose class we can't resolve (shouldn't happen —
# we only pull ARTERIAL_HIGHWAY_TAGS). Use the shortest reach.
_DEFAULT_REACH_M: float = min(ARTERIAL_REACH_M.values())


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _missing_inputs() -> list[Path]:
    """Return the cached feature files that don't yet exist."""
    return [p for p in (ARTERIALS_PATH, BUILDINGS_PATH, POIS_PATH, OPENSPACE_PATH)
            if not p.exists()]


def _drop_underground(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Remove tunnel / below-grade road segments from a road layer.

    A buried road (``tunnel=yes`` or ``layer < 0``) imposes no street-level
    pedestrian hostility, but off-path proximity is purely 2D distance-to-line, so
    it would crater the car-safety of a fine surface footway directly above it.
    Boston's Big Dig buries I-90/I-93 under Fort Point / downtown / Seaport — the
    grounded case (Fort Point seg #2: nearest 'arterial' was the tunneled Mass
    Pike, car_safety ~0.05 on a pleasant block). Scoped to UNDERGROUND only;
    elevated/bridge roads are left in (a pedestrian under a viaduct does feel it).
    Missing tunnel/layer columns (older cache) → no-op (returns gdf unchanged)."""
    keep = pd.Series(True, index=gdf.index)
    if "tunnel" in gdf.columns:
        t = gdf["tunnel"].astype("string").str.lower()
        keep &= ~t.isin(["yes", "building_passage", "culvert"])
    if "layer" in gdf.columns:
        keep &= ~(pd.to_numeric(gdf["layer"], errors="coerce") < 0)
    return gdf[keep].copy()


def load_arterials() -> gpd.GeoDataFrame:
    """Cached arterial road geometry (incl. motorway/trunk), in METRIC_CRS,
    with underground (tunnel / layer<0) segments dropped."""
    gdf = gpd.read_file(ARTERIALS_PATH).to_crs(METRIC_CRS)
    # Keep only line geometry — distance-to-road is meaningless for stray points.
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    return _drop_underground(gdf)


def load_buildings() -> gpd.GeoDataFrame:
    """Cached building footprints, in METRIC_CRS."""
    return gpd.read_file(BUILDINGS_PATH).to_crs(METRIC_CRS)


def load_pois() -> gpd.GeoDataFrame:
    """Cached shop/amenity POIs in METRIC_CRS, with a foot-traffic ``weight`` column.

    Street furniture / parking (POI_NOISE_AMENITIES) weigh 0; every shop and any
    other amenity weighs 1 (active frontage). Older caches without the type
    columns fall back to weight 1 for all.
    """
    gdf = gpd.read_file(POIS_PATH).to_crs(METRIC_CRS)
    amenity = gdf["amenity"] if "amenity" in gdf.columns else None
    shop    = gdf["shop"]    if "shop"    in gdf.columns else None

    def _w(i) -> float:
        if shop is not None and isinstance(shop.iloc[i], str):
            return 1.0
        if amenity is not None:
            a = amenity.iloc[i]
            if isinstance(a, str):
                return 0.0 if a in POI_NOISE_AMENITIES else 1.0
        return 1.0

    gdf["weight"] = [_w(i) for i in range(len(gdf))]
    return gdf


def load_openspace() -> gpd.GeoDataFrame:
    """Cached large open-space polygons (parks + water ≥ OPENSPACE_MIN_AREA_M2),
    in METRIC_CRS. Small pocket parks/playgrounds are dropped — only meaningful
    open space gives the openness/sightlines that read as safe."""
    gdf = gpd.read_file(OPENSPACE_PATH).to_crs(METRIC_CRS)
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    gdf = gdf[gdf.geometry.area >= OPENSPACE_MIN_AREA_M2]
    return gdf


def load_landuse() -> gpd.GeoDataFrame:
    """Cached industrial landuse polygons (A), in METRIC_CRS. Optional input —
    callers must handle its absence (industrial_exposure then defaults to 0)."""
    gdf = gpd.read_file(LANDUSE_PATH).to_crs(METRIC_CRS)
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    return gdf


def load_roads() -> gpd.GeoDataFrame:
    """Cached all-roads geometry (B), in METRIC_CRS. Optional input — callers must
    handle its absence (road_separation then defaults to 0, today's flat ceiling)."""
    gdf = gpd.read_file(ROADS_PATH).to_crs(METRIC_CRS)
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    return _drop_underground(gdf)


def _empty_gdf() -> gpd.GeoDataFrame:
    """An empty GeoDataFrame in METRIC_CRS — the no-op stand-in for an optional
    (landuse / roads) layer that hasn't been downloaded yet."""
    return gpd.GeoDataFrame(geometry=[], crs=METRIC_CRS)


# ---------------------------------------------------------------------------
# Scalar score helpers (parameters from scoring/weights.py)
# ---------------------------------------------------------------------------

def maxspeed_safety(mph: float) -> float:
    """Map a road speed (mph) to on-path car-safety [0,1] via MAXSPEED_SAFETY_ANCHORS."""
    a = MAXSPEED_SAFETY_ANCHORS
    if mph <= a[0][0]:
        return a[0][1]
    if mph >= a[-1][0]:
        return a[-1][1]
    for (x0, y0), (x1, y1) in zip(a, a[1:]):
        if x0 <= mph <= x1:
            return y0 + (y1 - y0) * (mph - x0) / (x1 - x0)
    return a[-1][1]


def _parse_speed(maxspeed) -> float | None:
    """Parse an OSM maxspeed value (e.g. ``"25 mph"``, ``"30"``, a list) to mph."""
    if maxspeed is None:
        return None
    if isinstance(maxspeed, (list, tuple)):
        vals = [s for s in (_parse_speed(m) for m in maxspeed) if s is not None]
        return max(vals) if vals else None        # take the fastest if ambiguous
    m = re.search(r"\d+\.?\d*", str(maxspeed))
    return float(m.group()) if m else None


def _base_classes(highway) -> list[str]:
    """Base highway class(es) for an edge, ``_link`` stripped (handles lists)."""
    vals = highway if isinstance(highway, (list, tuple)) else [highway]
    out = []
    for v in vals:
        if isinstance(v, str):
            out.append(v[:-5] if v.endswith("_link") else v)
    return out


def _is_pedestrian(highway) -> bool:
    """True if the edge itself is a pedestrian-DEDICATED way (no through traffic)."""
    return any(c in PEDESTRIAN_HIGHWAYS for c in _base_classes(highway))


def _is_arterial(highway) -> bool:
    """True if the edge IS an arterial road (its danger is on-path, not off-path)."""
    return any(c in ARTERIAL_REACH_M for c in _base_classes(highway))


def on_path_safety(highway, maxspeed) -> float:
    """Car-safety of the road you walk ALONG: 1.0 on a protected path, else from speed."""
    if _is_pedestrian(highway):
        return 1.0
    speed = _parse_speed(maxspeed)
    if speed is None:
        classes = _base_classes(highway)
        speeds = [DEFAULT_MAXSPEED_MPH[c] for c in classes if c in DEFAULT_MAXSPEED_MPH]
        speed = max(speeds) if speeds else 25.0    # unknown road ≈ residential
    return maxspeed_safety(speed)


def _arterial_hostility(speed_mph: float) -> float:
    """Depth of an arterial's off-path penalty = 1 − that speed's on-path safety.

    Driven by the road's ACTUAL posted speed (resolved in _arterial_scores), so a
    calm 25 mph urban arterial barely penalises a nearby footway while a 40 mph
    parkway does — the same crash-risk curve on-path uses (on_path_safety)."""
    return 1.0 - maxspeed_safety(speed_mph)


def off_path_safety(distance_m: float, reach_m: float, hostility: float) -> float:
    """Safety from a nearby arterial: 1 − hostility·falloff (1 at d=0, 0 at reach)."""
    if reach_m <= 0:
        return 1.0
    falloff = max(0.0, 1.0 - distance_m / reach_m)
    return 1.0 - hostility * falloff


def _sat(x: float, sat: float) -> float:
    """Saturating curve 1 − exp(−x/sat): the first few count most."""
    return 1.0 - math.exp(-x / sat) if sat > 0 else 0.0


def perceived_safety(poi_weight: float, bldg_count: float, openness: float,
                     *, enclosure_blind: bool, industrial: float = 0.0) -> tuple[float, float]:
    """"Eyes" felt-safety as a probabilistic OR of three substitutable signals.

    activity (foot-traffic POIs), enclosure (buildings facing the street — dropped
    when ``enclosure_blind``, i.e. an alley/service edge whose buildings face away)
    and openness (adjacency to large open space, already in [0,1]). noisy-OR
    ``1 − ∏(1−s)``: high if ANY is strong, low only when ALL three are weak (the
    isolated alley); a second strong signal adds a little, never required.

    ``industrial`` (in/near industrial landuse, [0,1]) **discounts enclosure**: a
    warehouse footprint is a building but provides no residential "eyes", so it
    shouldn't credit felt-safety. activity and openness are untouched — a genuinely
    busy industrial frontage keeps its activity.

    **Graded ceiling (re-anchor Lever 1).** The cap is graded by ``openness`` (park /
    water adjacency), the eyes analog of env-rework B's graded car ceiling:
    ``eyes_ceil = EYES_CEIL + (1 − EYES_CEIL)·openness``. A normal street edge
    (openness 0) still tops at ``EYES_CEIL`` (0.85); a genuinely open pedestrian
    route (pond loop / riverside / HarborWalk, openness→1) may reach toward 1.0 — so
    ``safety = sqrt(car·eyes)`` can exceed the 0.85 plateau and a car-free, open route
    can clear 90 ("top band reserved for pedestrian-designed"). Openness is used over
    ``road_separation`` because a recreational path's safety comes from open
    sightlines and the people such places draw — a *remote* separated path has fewer
    eyes, not more — and many designed pedestrian spaces (the pond, the river) hug
    their access road yet read fully safe.

    Returns ``(eyes, eyes_uncapped)`` — the graded-capped value used in scoring and
    the raw noisy-OR, the latter stored on the edge so the grading is tunable
    offline (like ``industrial_exposure`` / ``road_separation``).
    """
    activity  = _sat(poi_weight, EYES_POI_SAT)
    enclosure = 0.0 if enclosure_blind else _sat(bldg_count, EYES_BLDG_SAT)
    enclosure *= (1.0 - INDUSTRIAL_ENCLOSURE_DISCOUNT * industrial)
    noisy_or  = 1.0 - (1.0 - activity) * (1.0 - enclosure) * (1.0 - openness)
    eyes_ceil = EYES_CEIL + (1.0 - EYES_CEIL) * openness
    return min(eyes_ceil, noisy_or), noisy_or


def _enclosure_blind(highway, service) -> bool:
    """True where adjacent buildings face AWAY from the edge — alleys & service roads."""
    svals = service if isinstance(service, (list, tuple)) else [service]
    if any(s == "alley" for s in svals):
        return True
    return any(c == "service" for c in _base_classes(highway))


# ---------------------------------------------------------------------------
# Bulk per-edge index (mirrors build.py::_build_spatial_index)
# ---------------------------------------------------------------------------

def build_environment_index(G: nx.MultiDiGraph) -> dict[tuple, dict]:
    """Map every edge (u, v, key) → its environment sub-scores.

    Returns a dict keyed by (u, v, key) with ``{maxspeed_safety_score,
    arterial_proximity_score, car_safety_score, eyes_score, environment_score,
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
    for opt in ("service", "maxspeed"):
        if opt in edges_gdf.columns:
            keep.append(opt)
    edges_metric = edges_gdf[keep].to_crs(METRIC_CRS)

    # Load features once, clipped to the edge bbox + margin (matters for subsets).
    minx, miny, maxx, maxy = edges_metric.total_bounds
    minx, miny = minx - AREA_MARGIN_M, miny - AREA_MARGIN_M
    maxx, maxy = maxx + AREA_MARGIN_M, maxy + AREA_MARGIN_M
    arterials = load_arterials().cx[minx:maxx, miny:maxy]
    buildings = load_buildings().cx[minx:maxx, miny:maxy]
    pois      = load_pois().cx[minx:maxx, miny:maxy]
    openspace = load_openspace().cx[minx:maxx, miny:maxy]
    # Optional layers (A: industrial down-weight, B: road separation). Absent file
    # ⇒ empty ⇒ the signal defaults off (exposure 0 / separation 0 = today's model).
    landuse = load_landuse().cx[minx:maxx, miny:maxy] if LANDUSE_PATH.exists() else _empty_gdf()
    roads   = load_roads().cx[minx:maxx, miny:maxy]   if ROADS_PATH.exists()   else _empty_gdf()
    print(f"  Features in area: {len(arterials)} arterials, {len(buildings)} buildings, "
          f"{len(pois)} POIs, {len(openspace)} open spaces, {len(landuse)} industrial, "
          f"{len(roads)} roads")

    n = len(edges_metric)
    off_scores  = _arterial_scores(edges_metric, arterials)   # off-path safety per edge
    poi_weight  = _buffer_sum(edges_metric, pois, EYES_BUFFER_M, weight_col="weight")
    bldg_counts = _buffer_sum(edges_metric, buildings, EYES_BUFFER_M)
    openness    = _openness_scores(edges_metric, openspace)
    industrial  = _industrial_scores(edges_metric, landuse)   # A: truck/warehouse exposure
    separation  = _separation_scores(edges_metric, roads)     # B: distance from any road

    service_col  = (edges_metric["service"]  if "service"  in edges_metric.columns else [None] * n)
    maxspeed_col = (edges_metric["maxspeed"] if "maxspeed" in edges_metric.columns else [None] * n)

    index: dict[tuple, dict] = {}
    for eid, hwy, svc, ms in zip(edges_metric["edge_id"], edges_metric["highway"],
                                 service_col, maxspeed_col):
        ind = industrial.get(eid, 0.0)
        sep = separation.get(eid, 0.0)
        on  = on_path_safety(hwy, ms)                       # the road you walk along
        off = 1.0 if _is_arterial(hwy) else off_scores.get(eid, 1.0)  # nearby arterials
        # B: GRADED ceiling. A road-adjacent path (sep 0) tops at CAR_SAFETY_CEIL;
        # a genuinely road-separated path (sep→1, a greenway / ped bridge) climbs
        # toward 1.0. min() keeps low/dangerous values untouched (discrimination).
        ceil = CAR_SAFETY_CEIL + (1.0 - CAR_SAFETY_CEIL) * sep
        car  = min(ceil, on, off)
        # A: industrial corridors carry truck danger that maxspeed misses — penalise.
        car  = car * (1.0 - INDUSTRIAL_CAR_PENALTY * ind)
        e, e_unc = perceived_safety(
            poi_weight.get(eid, 0.0), bldg_counts.get(eid, 0.0), openness.get(eid, 0.0),
            enclosure_blind=_enclosure_blind(hwy, svc), industrial=ind)
        index[eid] = {
            "maxspeed_safety_score":    round(on, 4),
            "arterial_proximity_score": round(off, 4),
            "car_safety_score":         round(car, 4),
            "eyes_score":               round(e, 4),
            "environment_score":        round(math.sqrt(car * e), 4),
            "environment_confidence":   ENV_CONFIDENCE,
            # Sub-signals exposed for diagnostics + offline lever isolation: recompute
            # car/env with INDUSTRIAL_CAR_PENALTY=0, sep→0, or a different EYES_CEIL
            # grading (eyes_uncapped is the pre-cap noisy-OR) without a rebuild.
            "industrial_exposure":      round(ind, 4),
            "road_separation":          round(sep, 4),
            "eyes_uncapped":            round(e_unc, 4),
            "openness_score":           round(openness.get(eid, 0.0), 4),
        }
    print(f"  Scored environment for {len(index)}/{n} edges")
    return index


def _openness_scores(
    edges_metric: gpd.GeoDataFrame,
    openspace:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge openness: 1 adjacent to a large open space, ramping to 0 at
    OPENNESS_REACH_M (one nearest-open-space join)."""
    if openspace.empty:
        return {}
    joined = gpd.sjoin_nearest(
        edges_metric[["edge_id", "geometry"]],
        openspace[["geometry"]],
        how="left",
        distance_col="dist",
    )
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")
    scores: dict[tuple, float] = {}
    for eid, dist in zip(joined["edge_id"], joined["dist"]):
        if pd.isna(dist):
            scores[eid] = 0.0
        else:
            scores[eid] = max(0.0, 1.0 - float(dist) / OPENNESS_REACH_M)
    return scores


def _industrial_scores(
    edges_metric: gpd.GeoDataFrame,
    landuse:      gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge industrial exposure (A): 1 on/inside an industrial polygon, ramping
    to 0 at INDUSTRIAL_REACH_M (one nearest-polygon join). Missing/empty ⇒ {} (0)."""
    if landuse.empty:
        return {}
    joined = gpd.sjoin_nearest(
        edges_metric[["edge_id", "geometry"]],
        landuse[["geometry"]],
        how="left",
        distance_col="dist",
    )
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")
    scores: dict[tuple, float] = {}
    for eid, dist in zip(joined["edge_id"], joined["dist"]):
        scores[eid] = 0.0 if pd.isna(dist) else max(0.0, 1.0 - float(dist) / INDUSTRIAL_REACH_M)
    return scores


def _separation_scores(
    edges_metric: gpd.GeoDataFrame,
    roads:        gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge road separation (B): 0 on top of a road, ramping to 1 once the
    nearest car-carrying road is ≥ SEPARATION_REACH_M away (one nearest-road join).
    Missing/empty roads layer ⇒ {} (separation 0 ⇒ today's flat car ceiling)."""
    if roads.empty:
        return {}
    joined = gpd.sjoin_nearest(
        edges_metric[["edge_id", "geometry"]],
        roads[["geometry"]],
        how="left",
        distance_col="dist",
    )
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")
    scores: dict[tuple, float] = {}
    for eid, dist in zip(joined["edge_id"], joined["dist"]):
        scores[eid] = 0.0 if pd.isna(dist) else min(1.0, float(dist) / SEPARATION_REACH_M)
    return scores


def _arterial_scores(
    edges_metric: gpd.GeoDataFrame,
    arterials:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge OFF-PATH safety (1 − nearest-arterial hostility·falloff) via one join."""
    if arterials.empty:
        return {}

    art = arterials.copy()
    bases = art["highway"].map(lambda h: (_base_classes(h) or ["secondary"])[0])
    # Resolve each arterial's speed: its real maxspeed tag if present, else the
    # class default. Hostility (penalty DEPTH) follows speed; reach (penalty
    # DISTANCE) stays class-based — a big road's threat extends further regardless
    # of posted speed.
    ms_col = art["maxspeed"] if "maxspeed" in art.columns else [None] * len(art)
    speeds = [(_parse_speed(m) or DEFAULT_MAXSPEED_MPH.get(b, 30.0))
              for b, m in zip(bases, ms_col)]
    art["reach"]     = [ARTERIAL_REACH_M.get(b, _DEFAULT_REACH_M) for b in bases]
    art["hostility"] = [_arterial_hostility(s) for s in speeds]
    joined = gpd.sjoin_nearest(
        edges_metric[["edge_id", "geometry"]],
        art[["geometry", "reach", "hostility"]],
        how="left",
        distance_col="dist",
    )
    # Ties (equidistant arterials) yield multiple rows — keep the nearest.
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")

    scores: dict[tuple, float] = {}
    for eid, dist, reach, hostility in zip(
        joined["edge_id"], joined["dist"], joined["reach"], joined["hostility"]
    ):
        if pd.isna(dist) or pd.isna(reach):
            scores[eid] = 1.0
        else:
            scores[eid] = off_path_safety(float(dist), float(reach), float(hostility))
    return scores


def _buffer_sum(
    edges_metric: gpd.GeoDataFrame,
    features:     gpd.GeoDataFrame,
    buffer_m:     float,
    weight_col:   str | None = None,
) -> dict[tuple, float]:
    """Per-edge count (or summed ``weight_col``) of features within ``buffer_m``."""
    if features.empty:
        return {}

    buffered = edges_metric[["edge_id", "geometry"]].copy()
    buffered["geometry"] = edges_metric.geometry.buffer(buffer_m)
    cols = ["geometry"] + ([weight_col] if weight_col else [])
    joined = gpd.sjoin(buffered, features[cols], how="inner", predicate="intersects")
    grouped = (joined.groupby("edge_id")[weight_col].sum() if weight_col
               else joined.groupby("edge_id").size())
    return {eid: float(x) for eid, x in grouped.items()}
