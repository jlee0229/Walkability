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
import numpy as np
import osmnx as ox
import pandas as pd

from walkability.graph.inventory import BOSTON_PROFILE, CityProfile
from walkability.scoring.weights import (
    ARTERIAL_IMPUTE_K,
    ARTERIAL_IMPUTE_MAX_M,
    ARTERIAL_REACH_M,
    CAR_SAFETY_CEIL,
    CEMETERY_OPENNESS_FACTOR,
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
    FRONTAGE_FREEWAY_BUFFER_M,
    FRONTAGE_FREEWAY_FRAC,
    FREEWAY_HAZARD_OPEN_EXP,
    FREEWAY_HAZARD_OPEN_REACH_M,
    FREEWAY_HAZARD_REACH_M,
    OPENNESS_REACH_M,
    OPENSPACE_MIN_AREA_M2,
    PARKING_ACTIVITY_DISCOUNT,
    PARKING_ENCLOSURE_DISCOUNT,
    PARKING_FRONTAGE_NEAR_M,
    PARKING_MIN_AREA_M2,
    PARKING_REACH_M,
    PARKING_SETBACK_FAR_M,
    PEDESTRIAN_HIGHWAYS,
    POI_NOISE_AMENITIES,
    SEPARATION_REACH_M,
)

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
# The cached OSM feature inputs (produced by graph/download_environment.py) and
# the metric CRS are per-city: they come from the CityProfile passed to
# build_environment_index (default BOSTON_PROFILE), via profile.env_layer_path()
# and profile.metric_crs. Adding a city needs no change here.

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

def _missing_inputs(profile: CityProfile) -> list[Path]:
    """Return the cached feature files that don't yet exist (required inputs)."""
    return [profile.env_layer_path(n) for n in ("arterials", "buildings", "pois", "openspace")
            if not profile.env_layer_path(n).exists()]


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


def load_arterials(profile: CityProfile) -> gpd.GeoDataFrame:
    """Cached arterial road geometry (incl. motorway/trunk), in the city's metric
    CRS, with underground (tunnel / layer<0) segments dropped."""
    gdf = gpd.read_file(profile.env_layer_path("arterials")).to_crs(profile.metric_crs)
    # Keep only line geometry — distance-to-road is meaningless for stray points.
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    return _drop_underground(gdf)


def load_buildings(profile: CityProfile) -> gpd.GeoDataFrame:
    """Cached building footprints, in the city's metric CRS."""
    return gpd.read_file(profile.env_layer_path("buildings")).to_crs(profile.metric_crs)


def load_pois(profile: CityProfile) -> gpd.GeoDataFrame:
    """Cached shop/amenity POIs in the city's metric CRS, with a foot-traffic
    ``weight`` column.

    Street furniture / parking (POI_NOISE_AMENITIES) weigh 0; every shop and any
    other amenity weighs 1 (active frontage). Older caches without the type
    columns fall back to weight 1 for all.
    """
    gdf = gpd.read_file(profile.env_layer_path("pois")).to_crs(profile.metric_crs)
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


def load_openspace(profile: CityProfile) -> gpd.GeoDataFrame:
    """Cached large open-space polygons (parks + water + cemeteries ≥
    OPENSPACE_MIN_AREA_M2), in the city's metric CRS. Small pocket
    parks/playgrounds are dropped — only meaningful open space gives the
    openness/sightlines that read as safe. The ``kind`` column
    (water/park/cemetery) lets ``_openness_scores`` discount cemeteries (see
    CEMETERY_OPENNESS_FACTOR)."""
    gdf = gpd.read_file(profile.env_layer_path("openspace")).to_crs(profile.metric_crs)
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    gdf = gdf[gdf.geometry.area >= OPENSPACE_MIN_AREA_M2]
    return gdf


def load_landuse(profile: CityProfile) -> gpd.GeoDataFrame:
    """Cached industrial landuse polygons (A), in the city's metric CRS. Optional
    input — callers must handle its absence (industrial_exposure then → 0)."""
    gdf = gpd.read_file(profile.env_layer_path("landuse")).to_crs(profile.metric_crs)
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    return gdf


def load_roads(profile: CityProfile) -> gpd.GeoDataFrame:
    """Cached all-roads geometry (B), in the city's metric CRS. Optional input —
    callers must handle its absence (road_separation then defaults to 0)."""
    gdf = gpd.read_file(profile.env_layer_path("roads")).to_crs(profile.metric_crs)
    gdf = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])].copy()
    return _drop_underground(gdf)


def load_parking(profile: CityProfile) -> gpd.GeoDataFrame:
    """Cached large surface-parking polygons (strip-mall "false eyes" fix), in the
    city's metric CRS. Kept only polygons with area ≥ PARKING_MIN_AREA_M2 so a few
    on-street spaces don't fire — the signal is a strip-mall lot between sidewalk
    and building. Optional input — callers handle its absence (parking_exposure
    then → 0)."""
    gdf = gpd.read_file(profile.env_layer_path("parking")).to_crs(profile.metric_crs)
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    return gdf[gdf.geometry.area >= PARKING_MIN_AREA_M2].copy()


def _empty_gdf(profile: CityProfile) -> gpd.GeoDataFrame:
    """An empty GeoDataFrame in the city's metric CRS — the no-op stand-in for an
    optional (landuse / roads) layer that hasn't been downloaded yet."""
    return gpd.GeoDataFrame(geometry=[], crs=profile.metric_crs)


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


def on_path_safety(highway, maxspeed, defaults: dict = DEFAULT_MAXSPEED_MPH,
                   imputed_speed: float | None = None) -> float:
    """Car-safety of the road you walk ALONG: 1.0 on a protected path, else from speed.

    ``defaults`` is the per-city fallback speed table (profile.maxspeed_defaults)
    used when the road has no ``maxspeed`` tag. ``imputed_speed`` — for an untagged
    ARTERIAL, the speed imputed from nearby same-class tagged arterials
    (impute_arterial_speeds) — is preferred over the class default when the edge
    carries no tag of its own."""
    if _is_pedestrian(highway):
        return 1.0
    speed = _parse_speed(maxspeed)
    if speed is None and imputed_speed is not None:
        speed = imputed_speed
    if speed is None:
        classes = _base_classes(highway)
        speeds = [defaults[c] for c in classes if c in defaults]
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
                     *, enclosure_blind: bool, industrial: float = 0.0,
                     parking: float = 0.0) -> tuple[float, float]:
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

    ``parking`` (on/beside a large surface lot, [0,1]) is the **strip-mall "false
    eyes" discount**: a parking moat between the sidewalk and the buildings means
    the shops (activity) and buildings (enclosure) are present but provide no
    street-level surveillance. It discounts BOTH — enclosure fully
    (PARKING_ENCLOSURE_DISCOUNT) and activity partially (PARKING_ACTIVITY_DISCOUNT,
    since some strip visitors do walk). openness is untouched. This is what stops a
    stroad lined with strip retail from reading as a lively, safe street.

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
    # Strip-mall parking moat: buildings/shops behind the lot give no street eyes.
    enclosure *= (1.0 - PARKING_ENCLOSURE_DISCOUNT * parking)
    activity  *= (1.0 - PARKING_ACTIVITY_DISCOUNT * parking)
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

def build_environment_index(
    G: nx.MultiDiGraph,
    profile: CityProfile = BOSTON_PROFILE,
) -> dict[tuple, dict]:
    """Map every edge (u, v, key) → its environment sub-scores.

    Returns a dict keyed by (u, v, key) with ``{maxspeed_safety_score,
    arterial_proximity_score, car_safety_score, eyes_score, environment_score,
    environment_confidence}``. If the cached feature inputs are missing, returns
    an empty dict (a warning is printed) so the rest of the build still runs —
    edges simply get no environment_score and the factor drops out of the
    weighted mean (consistent with the pipeline's None≠0 philosophy). Feature
    layers + metric CRS come from ``profile`` (default Boston).
    """
    missing = _missing_inputs(profile)
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
    edges_metric = edges_gdf[keep].to_crs(profile.metric_crs)

    # Load features once, clipped to the edge bbox + margin (matters for subsets).
    minx, miny, maxx, maxy = edges_metric.total_bounds
    minx, miny = minx - AREA_MARGIN_M, miny - AREA_MARGIN_M
    maxx, maxy = maxx + AREA_MARGIN_M, maxy + AREA_MARGIN_M
    arterials = load_arterials(profile).cx[minx:maxx, miny:maxy]
    buildings = load_buildings(profile).cx[minx:maxx, miny:maxy]
    pois      = load_pois(profile).cx[minx:maxx, miny:maxy]
    openspace = load_openspace(profile).cx[minx:maxx, miny:maxy]
    # Optional layers (A: industrial down-weight, B: road separation). Absent file
    # ⇒ empty ⇒ the signal defaults off (exposure 0 / separation 0 = today's model).
    landuse = (load_landuse(profile).cx[minx:maxx, miny:maxy]
               if profile.env_layer_path("landuse").exists() else _empty_gdf(profile))
    roads   = (load_roads(profile).cx[minx:maxx, miny:maxy]
               if profile.env_layer_path("roads").exists() else _empty_gdf(profile))
    parking = (load_parking(profile).cx[minx:maxx, miny:maxy]
               if profile.env_layer_path("parking").exists() else _empty_gdf(profile))
    print(f"  Features in area: {len(arterials)} arterials, {len(buildings)} buildings, "
          f"{len(pois)} POIs, {len(openspace)} open spaces, {len(landuse)} industrial, "
          f"{len(roads)} roads, {len(parking)} parking")

    # Impute untagged arterials' posted speed from nearby same-class tagged
    # arterials ONCE, then share it with both the off-path (nearby-arterial) and
    # on-path (walk-along-arterial) car-safety paths so a road reads one speed.
    if not arterials.empty:
        arterials = arterials.copy()
        arterials["imp_speed"] = impute_arterial_speeds(arterials, profile.maxspeed_defaults)

    n = len(edges_metric)
    off_scores  = _arterial_scores(edges_metric, arterials, profile.maxspeed_defaults)  # off-path
    on_impute   = _on_path_imputed_speeds(edges_metric, arterials)  # untagged arterial on-path
    poi_weight  = _buffer_sum(edges_metric, pois, EYES_BUFFER_M, weight_col="weight")
    bldg_counts = _buffer_sum(edges_metric, buildings, EYES_BUFFER_M)
    openness    = _openness_scores(edges_metric, openspace)
    industrial  = _industrial_scores(edges_metric, landuse)   # A: truck/warehouse exposure
    separation  = _separation_scores(edges_metric, roads)     # B: distance from any road
    parking_exp = _parking_scores(edges_metric, parking)      # strip-mall false-eyes discount
    bldg_dist   = _building_dist(edges_metric, buildings)     # setback gate for the moat
    freeway_haz = _freeway_hazard_scores(edges_metric, arterials, openspace)  # barrier-effect veto input

    service_col  = (edges_metric["service"]  if "service"  in edges_metric.columns else [None] * n)
    maxspeed_col = (edges_metric["maxspeed"] if "maxspeed" in edges_metric.columns else [None] * n)

    cap = profile.eyes_rescue_cap
    index: dict[tuple, dict] = {}
    for eid, hwy, svc, ms in zip(edges_metric["edge_id"], edges_metric["highway"],
                                 service_col, maxspeed_col):
        ind = industrial.get(eid, 0.0)
        sep = separation.get(eid, 0.0)
        # Gate raw parking proximity by building setback → the moat signal: a lot
        # only kills the eyes where it REPLACES active frontage (buildings set back).
        park = _parking_moat(parking_exp.get(eid, 0.0), bldg_dist.get(eid, float("inf")))
        on  = on_path_safety(hwy, ms, profile.maxspeed_defaults,  # the road you walk along
                             imputed_speed=on_impute.get(eid))
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
            enclosure_blind=_enclosure_blind(hwy, svc), industrial=ind, parking=park)
        # Eyes can't rescue a low-car_safety road beyond a per-city cap: a
        # strip-mall's foot traffic shouldn't make a 45 mph stroad read as safe.
        e_eff = min(e, car + cap) if cap is not None else e
        index[eid] = {
            "maxspeed_safety_score":    round(on, 4),
            "arterial_proximity_score": round(off, 4),
            "car_safety_score":         round(car, 4),
            "eyes_score":               round(e, 4),
            "environment_score":        round(math.sqrt(car * e_eff), 4),
            "environment_confidence":   ENV_CONFIDENCE,
            # Sub-signals exposed for diagnostics + offline lever isolation: recompute
            # car/env with INDUSTRIAL_CAR_PENALTY=0, sep→0, or a different EYES_CEIL
            # grading (eyes_uncapped is the pre-cap noisy-OR) without a rebuild.
            "industrial_exposure":      round(ind, 4),
            "road_separation":          round(sep, 4),
            "parking_exposure":         round(park, 4),
            "freeway_hazard":           round(freeway_haz.get(eid, 0.0), 4),
            "eyes_uncapped":            round(e_unc, 4),
            "openness_score":           round(openness.get(eid, 0.0), 4),
        }
    print(f"  Scored environment for {len(index)}/{n} edges")
    return index


def _nearest_openness(
    edges_metric: gpd.GeoDataFrame,
    openspace:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge openness against one open-space subset: 1 adjacent, ramping to 0
    at OPENNESS_REACH_M (one nearest-open-space join)."""
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


def _openness_scores(
    edges_metric: gpd.GeoDataFrame,
    openspace:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge openness from the nearest large open space.

    Cemeteries count as open space (real sightlines / traffic separation) but are
    less pleasant than a park, so their openness is DISCOUNTED by
    CEMETERY_OPENNESS_FACTOR. Park/water and cemetery openness are computed
    separately and the stronger (post-discount) signal wins per edge — so a nearer
    cemetery never masks an almost-as-close park, and an edge beside only a cemetery
    (Grove St / Walnut Hills) still gets a real, if discounted, openness lift."""
    if openspace.empty:
        return {}
    kind = openspace["kind"].astype("string") if "kind" in openspace.columns else None
    if kind is None or not (kind == "cemetery").any():
        return _nearest_openness(edges_metric, openspace)

    parks = _nearest_openness(edges_metric, openspace[kind != "cemetery"])
    cems  = _nearest_openness(edges_metric, openspace[kind == "cemetery"])
    scores = dict(parks)
    for eid, val in cems.items():
        scores[eid] = max(scores.get(eid, 0.0), CEMETERY_OPENNESS_FACTOR * val)
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


def _parking_scores(
    edges_metric: gpd.GeoDataFrame,
    parking:      gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge surface-parking exposure: 1 on/beside a large lot, ramping to 0 at
    PARKING_REACH_M (one nearest-polygon join). Missing/empty ⇒ {} (0)."""
    if parking.empty:
        return {}
    joined = gpd.sjoin_nearest(
        edges_metric[["edge_id", "geometry"]],
        parking[["geometry"]],
        how="left",
        distance_col="dist",
    )
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")
    scores: dict[tuple, float] = {}
    for eid, dist in zip(joined["edge_id"], joined["dist"]):
        scores[eid] = 0.0 if pd.isna(dist) else max(0.0, 1.0 - float(dist) / PARKING_REACH_M)
    return scores


def _building_dist(
    edges_metric: gpd.GeoDataFrame,
    buildings:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge distance (m) to the nearest building — the setback / active-frontage
    measure that gates the parking moat. Missing/empty ⇒ {} (⇒ treated as no
    frontage, so the gate opens; harmless since parking layer is then usually
    absent too)."""
    if buildings.empty:
        return {}
    joined = gpd.sjoin_nearest(
        edges_metric[["edge_id", "geometry"]],
        buildings[["geometry"]],
        how="left",
        distance_col="dist",
    )
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")
    return {eid: (float("inf") if pd.isna(d) else float(d))
            for eid, d in zip(joined["edge_id"], joined["dist"])}


def _parking_moat(parking_exp: float, bldg_dist: float) -> float:
    """Gate raw parking exposure by building setback → the strip-mall MOAT signal.

    No penalty when a building fronts within PARKING_FRONTAGE_NEAR_M (active
    frontage, e.g. South Congress — lots are beside/behind, not a moat); full
    exposure once the nearest building is past PARKING_SETBACK_FAR_M (the lot sits
    between sidewalk and building)."""
    span = PARKING_SETBACK_FAR_M - PARKING_FRONTAGE_NEAR_M
    gate = 1.0 if span <= 0 else (bldg_dist - PARKING_FRONTAGE_NEAR_M) / span
    return parking_exp * max(0.0, min(1.0, gate))


def _frontage_roads(arterials: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Non-freeway arterials that run parallel-adjacent to an at-grade motorway/trunk
    for ≥ FRONTAGE_FREEWAY_FRAC of their length — the freeway frontage-road detector.

    The fraction-of-length-within-buffer metric separates a PARALLEL frontage
    (frac→1) from a PERPENDICULAR crossing (frac→0, briefly near) with no bearing
    math. ``arterials`` is already underground-dropped by load_arterials, so a buried
    freeway grows no frontage (Boston's Big Dig)."""
    if arterials.empty:
        return arterials.iloc[0:0]
    base = arterials["highway"].map(lambda h: (_base_classes(h) or [""])[0])
    free = arterials[base.isin(("motorway", "trunk"))]
    cand = arterials[~base.isin(("motorway", "trunk"))]
    if free.empty or cand.empty:
        return arterials.iloc[0:0]
    buf = free.geometry.buffer(FRONTAGE_FREEWAY_BUFFER_M).union_all()
    frac = (cand.geometry.intersection(buf).length
            / cand.geometry.length.replace(0, np.nan)).fillna(0.0)
    return cand[frac >= FRONTAGE_FREEWAY_FRAC]


def _freeway_hazard_scores(
    edges_metric: gpd.GeoDataFrame,
    arterials:    gpd.GeoDataFrame,
    openspace:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-edge freeway-frontage hazard in [0,1] — the barrier-effect / severance
    signal that the route veto (factors.apply_freeway_veto) acts on.

    ``hazard = ramp(dist to [at-grade motorway/trunk ∪ frontage road], REACH)
               · (1 − openness)^OPEN_EXP``, where openness ramps over a WIDE reach
    (FREEWAY_HAZARD_OPEN_REACH_M) so a separated/open pedestrian setting (riverside
    path, greenway) is exempted; the convex exponent spares mostly-open edges while
    leaving a bare frontage (openness≈0) at full hazard. Empty when no freeway is
    near (⇒ 0, no veto)."""
    base = arterials["highway"].map(lambda h: (_base_classes(h) or [""])[0]) if not arterials.empty else None
    free = arterials[base.isin(("motorway", "trunk"))] if base is not None else arterials
    frontage = _frontage_roads(arterials)
    parts = [g for g in (free, frontage) if not g.empty]
    if not parts:
        return {}
    haz_geom = pd.concat([p[["geometry"]] for p in parts], ignore_index=True)

    jh = gpd.sjoin_nearest(edges_metric[["edge_id", "geometry"]],
                           haz_geom, how="left", distance_col="dist")
    jh = jh.sort_values("dist").drop_duplicates("edge_id", keep="first")
    haz_dist = dict(zip(jh["edge_id"], jh["dist"]))

    open_dist: dict = {}
    if not openspace.empty:
        jo = gpd.sjoin_nearest(edges_metric[["edge_id", "geometry"]],
                               openspace[["geometry"]], how="left", distance_col="dist")
        jo = jo.sort_values("dist").drop_duplicates("edge_id", keep="first")
        open_dist = dict(zip(jo["edge_id"], jo["dist"]))

    scores: dict[tuple, float] = {}
    for eid in edges_metric["edge_id"]:
        d = haz_dist.get(eid)
        if d is None or pd.isna(d):
            continue
        ramp = max(0.0, 1.0 - float(d) / FREEWAY_HAZARD_REACH_M)
        if ramp <= 0.0:
            continue
        od = open_dist.get(eid)
        openness = 0.0 if od is None or pd.isna(od) else max(0.0, 1.0 - float(od) / FREEWAY_HAZARD_OPEN_REACH_M)
        haz = ramp * (1.0 - openness) ** FREEWAY_HAZARD_OPEN_EXP
        if haz > 0.0:
            scores[eid] = haz
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


def impute_arterial_speeds(
    arterials: gpd.GeoDataFrame,
    defaults:  dict = DEFAULT_MAXSPEED_MPH,
    k:         int = ARTERIAL_IMPUTE_K,
    max_dist:  float = ARTERIAL_IMPUTE_MAX_M,
) -> np.ndarray:
    """Posted speed (mph) for every arterial row, imputing the untagged ones.

    A tagged arterial keeps its own ``maxspeed``. An untagged one takes the MEDIAN
    speed of the nearest ``k`` tagged arterials of the SAME base class within
    ``max_dist`` metres, falling back to the class default only when none is that
    close. Posted speed tracks location as much as class (a downtown secondary
    ~30 mph, a suburban one ~45), so this replaces the flat class default with the
    local level implied by the tagged roads nearby. Matching on class stops a
    fast motorway ramp from inflating a calm secondary; the median is robust to a
    lone mis-tagged neighbour. ``arterials`` must already be in a metric CRS.

    Returns a float array aligned to ``arterials.index`` order."""
    n = len(arterials)
    bases = arterials["highway"].map(lambda h: (_base_classes(h) or ["secondary"])[0]).to_numpy()
    ms_col = arterials["maxspeed"] if "maxspeed" in arterials.columns else [None] * n
    own = np.array([_parse_speed(m) for m in ms_col], dtype=float)  # NaN where untagged
    cent = arterials.geometry.centroid
    xy = np.column_stack([cent.x.to_numpy(), cent.y.to_numpy()])

    speeds = own.copy()
    for base in np.unique(bases):
        cls = bases == base
        default = defaults.get(base, 30.0)
        tagged = cls & ~np.isnan(own)
        todo = np.where(cls & np.isnan(own))[0]
        if todo.size == 0:
            continue
        t_idx = np.where(tagged)[0]
        if t_idx.size == 0:                       # no same-class anchor anywhere
            speeds[todo] = default
            continue
        t_xy, t_spd = xy[t_idx], own[t_idx]
        for i in todo:
            d = np.hypot(t_xy[:, 0] - xy[i, 0], t_xy[:, 1] - xy[i, 1])
            near = np.argsort(d)[:k]
            near = near[d[near] <= max_dist]
            speeds[i] = float(np.median(t_spd[near])) if near.size else default
    return speeds


def _arterial_scores(
    edges_metric: gpd.GeoDataFrame,
    arterials:    gpd.GeoDataFrame,
    defaults:     dict = DEFAULT_MAXSPEED_MPH,
) -> dict[tuple, float]:
    """Per-edge OFF-PATH safety (1 − nearest-arterial hostility·falloff) via one join.

    ``defaults`` is the per-city fallback speed table for untagged arterials."""
    if arterials.empty:
        return {}

    art = arterials.copy()
    bases = art["highway"].map(lambda h: (_base_classes(h) or ["secondary"])[0])
    # Resolve each arterial's speed: its real maxspeed tag if present, else the
    # speed IMPUTED from nearby same-class tagged arterials (falling back to the
    # class default only when isolated) — see impute_arterial_speeds. Reuse the
    # column if the caller already imputed (shared with the on-path resolution),
    # else compute it here. Hostility (penalty DEPTH) follows speed; reach (penalty
    # DISTANCE) stays class-based — a big road's threat extends further regardless
    # of posted speed.
    speeds = (art["imp_speed"].to_numpy() if "imp_speed" in art.columns
              else impute_arterial_speeds(art, defaults))
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


def _on_path_imputed_speeds(
    edges_metric: gpd.GeoDataFrame,
    arterials:    gpd.GeoDataFrame,
) -> dict[tuple, float]:
    """Per-arterial-edge imputed ON-PATH speed for edges lacking their own tag.

    An arterial edge in the walk graph coincides with a feature in the (already
    imputed) ``arterials`` layer, so a nearest join hands each untagged arterial
    edge the same imputed speed used off-path — one consistent speed per road.
    Non-arterial edges are not imputed (their off-tag speed is the class default,
    as before). Returns ``{edge_id: speed_mph}`` only for arterial edges."""
    if arterials.empty or "imp_speed" not in arterials.columns:
        return {}
    art_edges = edges_metric[edges_metric["highway"].map(_is_arterial)]
    if art_edges.empty:
        return {}
    joined = gpd.sjoin_nearest(
        art_edges[["edge_id", "geometry"]],
        arterials[["geometry", "imp_speed"]],
        how="left",
        distance_col="dist",
    )
    joined = joined.sort_values("dist").drop_duplicates("edge_id", keep="first")
    return {eid: float(s) for eid, s in zip(joined["edge_id"], joined["imp_speed"])
            if pd.notna(s)}


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
