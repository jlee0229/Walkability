"""
The GLOBAL route-type taxonomy — the list of route *kinds* that must pass for a
city's enriched graph to be considered "correctly loaded", plus the machinery
that turns that global list into a concrete per-city battery **with zero manual
coordinates**.

Run indirectly through ``notebooks/verify_city.py`` (the orchestrator); this
module is importable/reviewable in isolation. Iterating ``ROUTE_TYPES`` over one
city's ``Ctx`` *is* the instantiation of the global list into that city's
battery: every ``RouteType.selector`` auto-derives its origin/destination pairs
from the graph + city profile + downloaded OSM layers, so nothing is hand-typed
and the same file works for any city in ``CITY_PROFILES``.

Design spine
------------
1. Every selector samples only from the graph's **largest routable component**
   (``clip._routable_mask``) and draws pairs with the shared
   ``verify_csr_parity.sample_band_pairs`` primitive, so pairs are always
   reachable and reproducible (seeded).
2. Spatial route types (data-seam, over-water, quality anchors) just filter the
   routable node set by a graph/profile-derived geometric predicate *before*
   sampling, and **self-skip** (selector returns ``[]``) when their precondition
   is absent — an inventory that covers the whole graph (no seam), a city with no
   water layer, a cluster too small. So the same battery runs everywhere, lighting
   up only the types a given city can support.
3. The seam boundary is derived from **inventory-match contiguity on the graph**
   (which nodes touch a ``data_source=="city_inventory"`` edge), NOT the
   inventory bounding box — Boston's Brookline/hull are OSM-tier *enclaves inside*
   Boston's bbox, so a bbox test would miss them; contiguity does not.
"""

from __future__ import annotations

import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# repo root on path so `walkability` imports resolve when run as a script, and
# sibling notebooks (verify_csr_parity) import as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geopandas as gpd  # noqa: E402
import networkx as nx  # noqa: E402
import numpy as np  # noqa: E402
from shapely.geometry import LineString  # noqa: E402

from walkability.routing import clip  # noqa: E402
from walkability.routing.clip import haversine_m  # noqa: E402
from walkability.routing.router import find_routes  # noqa: E402
from walkability.scoring.factors import (  # noqa: E402
    RESTRICTED_FOOT_ACCESS,
    _as_float,
    _as_str,
    edge_walkability,
)

from verify_csr_parity import sample_band_pairs  # noqa: E402  (shared primitive)

try:
    from diagnostics import audit_route  # noqa: E402  (Tier-1 statistical flags)
except Exception:  # pragma: no cover - diagnostics is a sibling notebook
    audit_route = None

# --- tuning knobs (all query-time; no rebuild) -----------------------------
CELL_DEG: float = 0.004           # ~350–450 m grid cell for jurisdiction/anchors
SEAM_SCORE_TOL: float = 0.10      # max OSM-tier walk_score gap across the seam
ANCHOR_MARGIN: float = 0.10       # required median(high) − median(low) walk gap
WATER_MIN_AREA_M2: float = 50_000.0
WATER_MAX_SHORT_AXIS_M: float = 1500.0   # "crossable" — excludes wide open lakes
WATER_REACH_M: float = 500.0             # sample banks within this of the water
LONG_DETOUR_FACTOR: float = 5.0          # long route ≤ this × straight-line
WATER_DETOUR_FACTOR: float = 4.0


# ---------------------------------------------------------------------------
# Small value types
# ---------------------------------------------------------------------------

@dataclass
class Spec:
    """One auto-derived origin→destination test, ready for ``find_routes``."""
    o: tuple[float, float]        # (lat, lon)
    d: tuple[float, float]        # (lat, lon)
    alpha: float
    label: str
    role: str = ""               # anchor role: "high"/"low"; else ""


@dataclass
class Outcome:
    spec: Spec
    route: object | None = None   # best RouteResult, or None if no route
    error: str | None = None


@dataclass
class RouteType:
    name: str
    verifies: str
    catches: tuple[str, ...]                    # failure-mode letters (see taxonomy MD)
    selector: Optional[Callable[["Ctx"], list[Spec]]] = None
    assertions: Optional[Callable] = None       # (ctx, list[Outcome], check) -> None
    gates: bool = True                          # False → reported as INFO, never fails
    pin_survey: bool = False                    # always include in the calibration deck
    kind: str = "battery"                       # "battery" | "external" (verify_city runs)
    look_for: str = ""                          # calibration-deck hint
    note: str = ""


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _edge_latlon(G, u, v, key) -> list[tuple[float, float]]:
    """(lat, lon) vertices of one edge, from its shapely geometry when present."""
    d = G[u][v][key]
    geom = d.get("geometry")
    if geom is not None and hasattr(geom, "coords"):
        return [(lat, lon) for lon, lat in geom.coords]
    return [(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])]


def _route_latlon(G, route) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for u, v, key in route.edges:
        ec = _edge_latlon(G, u, v, key)
        coords.extend(ec if not coords else ec[1:])
    return coords


def _route_line_wgs(G, route) -> LineString | None:
    """Route polyline as a shapely LineString in (lon, lat) for intersection tests."""
    coords = _route_latlon(G, route)
    if len(coords) < 2:
        return None
    return LineString([(lon, lat) for lat, lon in coords])


def _chains(route) -> bool:
    """True if consecutive edges join head-to-tail (structural route integrity)."""
    e = route.edges
    return all(e[i][1] == e[i + 1][0] for i in range(len(e) - 1))


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return (int(math.floor(lat / CELL_DEG)), int(math.floor(lon / CELL_DEG)))


def _densest_cluster(nodes: list[tuple], rng) -> list[tuple]:
    """Densest ~CELL_DEG cell (plus its 8 neighbours) of a node subset.

    ``nodes`` is a list of ``(id, lat, lon)``. Returns the nodes in that
    3×3-cell neighbourhood — a spatially compact corridor to sample O/D within.
    """
    cells: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for nid, la, lo in nodes:
        cells[_cell(la, lo)].append((nid, la, lo))
    if not cells:
        return []
    best = max(cells, key=lambda c: len(cells[c]))
    out: list[tuple] = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            out.extend(cells.get((best[0] + dy, best[1] + dx), []))
    return out


# ---------------------------------------------------------------------------
# Ctx — everything a selector needs, built once per run and cached lazily
# ---------------------------------------------------------------------------

class Ctx:
    """Per-city routing context: the routable node set plus lazily-derived
    jurisdiction / water / node-quality material every selector filters on."""

    def __init__(self, G: nx.MultiDiGraph, profile, seed: int = 7, scale: int = 1):
        self.G = G
        self.profile = profile
        self.seed = seed
        self.scale = max(1, scale)      # multiply per-type pair counts
        self._cache: dict = {}

    def rng(self, salt: str) -> random.Random:
        return random.Random(f"{self.seed}:{salt}")

    def _c(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # --- routable nodes -----------------------------------------------------
    @property
    def routable_nodes(self) -> list[tuple]:
        def build():
            ids, lats, lons = clip._node_coords(self.G)
            mask = clip._routable_mask(self.G)
            return [(ids[i], float(lats[i]), float(lons[i]))
                    for i in range(len(ids)) if mask[i]]
        return self._c("routable_nodes", build)

    @property
    def routable_coords(self) -> list[tuple[float, float]]:
        return [(la, lo) for _, la, lo in self.routable_nodes]

    @property
    def graph_diag_m(self) -> float:
        def build():
            lats = [la for _, la, _ in self.routable_nodes]
            lons = [lo for _, _, lo in self.routable_nodes]
            if not lats:
                return 0.0
            return float(haversine_m(min(lats), min(lons), max(lats), max(lons)))
        return self._c("diag", build)

    # --- inventory jurisdiction (graph-contiguity, enclave-robust) ----------
    @property
    def _city_cells(self) -> set:
        """Grid cells containing a node that touches a ``city_inventory`` edge."""
        def build():
            cells: set = set()
            G = self.G
            for u, v, d in G.edges(data=True):
                if _as_str(d.get("data_source")) == "city_inventory":
                    for n in (u, v):
                        nd = G.nodes[n]
                        cells.add(_cell(float(nd["y"]), float(nd["x"])))
            return cells
        return self._c("city_cells", build)

    def is_deep_outside(self, lat: float, lon: float) -> bool:
        """True if no city-inventory node lies in the node's 3×3 cell block —
        i.e. it is well inside a no-inventory municipality (Brookline/hull),
        robust to the fact that those sit *inside* Boston's bounding box."""
        cy, cx = _cell(lat, lon)
        cells = self._city_cells
        return all((cy + dy, cx + dx) not in cells
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1))

    def _is_inside(self, lat: float, lon: float) -> bool:
        return _cell(lat, lon) in self._city_cells

    @property
    def inside_coords(self) -> list[tuple[float, float]]:
        return self._c("inside", lambda: [
            (la, lo) for _, la, lo in self.routable_nodes if self._is_inside(la, lo)])

    @property
    def deep_outside_coords(self) -> list[tuple[float, float]]:
        return self._c("outside", lambda: [
            (la, lo) for _, la, lo in self.routable_nodes
            if self.is_deep_outside(la, lo)])

    # --- water (largest CROSSABLE body: narrow enough to bridge) ------------
    @property
    def water(self):
        """(poly_wgs, poly_metric) for the largest crossable water body, or None."""
        def build():
            path = self.profile.env_layer_path("openspace")
            if not path.exists():
                return None
            g = gpd.read_file(path)
            if "kind" not in g.columns:
                return None
            w = g[g["kind"] == "water"]
            if w.empty:
                return None
            wm = w.to_crs(self.profile.metric_crs)
            areas = wm.geometry.area
            keep = wm[areas > WATER_MIN_AREA_M2]
            if keep.empty:
                return None

            def short_axis(poly):
                pts = list(zip(*poly.minimum_rotated_rectangle.exterior.coords.xy))
                return min(math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2]))

            sa = keep.geometry.map(short_axis)
            cross = keep[sa <= WATER_MAX_SHORT_AXIS_M]
            if cross.empty:
                return None
            idx = cross.geometry.area.idxmax()
            return (w.geometry.loc[idx], cross.geometry.loc[idx])  # (wgs, metric)
        return self._c("water", build)

    def _nodes_near_water(self, reach_m: float) -> list[tuple]:
        """Routable nodes within ``reach_m`` of the crossable water polygon."""
        def build():
            wat = self.water
            if wat is None:
                return []
            poly_wgs, poly_m = wat
            minx, miny, maxx, maxy = poly_wgs.bounds
            pad = reach_m / 90_000.0  # generous deg padding (~1.2× worst case)
            cand = [(nid, la, lo) for nid, la, lo in self.routable_nodes
                    if (minx - pad) <= lo <= (maxx + pad)
                    and (miny - pad) <= la <= (maxy + pad)]
            if not cand:
                return []
            pts = gpd.GeoSeries.from_xy(
                [lo for _, _, lo in cand], [la for _, la, _ in cand], crs="EPSG:4326"
            ).to_crs(self.profile.metric_crs)
            dist = pts.distance(poly_m)
            return [cand[i] for i in range(len(cand)) if dist.iloc[i] <= reach_m]
        return self._c(f"near_water_{int(reach_m)}", build)

    # --- per-node walkability / road-type (for anchors) ---------------------
    @property
    def node_walk(self) -> dict:
        """node id → mean incident edge walk_score (baked fast path)."""
        def build():
            tot: dict = defaultdict(float)
            cnt: dict = defaultdict(int)
            for u, v, d in self.G.edges(data=True):
                w = edge_walkability(d)[0]
                for n in (u, v):
                    tot[n] += w
                    cnt[n] += 1
            return {n: tot[n] / cnt[n] for n in tot}
        return self._c("node_walk", build)

    @property
    def node_hwy(self) -> np.ndarray:
        """Per-node max incident highway_score, aligned to clip._node_coords order."""
        return clip._node_walk_quality(self.G)


# ---------------------------------------------------------------------------
# Selectors + assertions
# ---------------------------------------------------------------------------

def _band(lo: float, hi: float, count: int, alpha: float, tag: str):
    """Selector: ``count`` reachable pairs with straight-line distance in [lo, hi]."""
    def sel(ctx: Ctx) -> list[Spec]:
        hi_eff = min(hi, 0.9 * ctx.graph_diag_m) if ctx.graph_diag_m else hi
        if hi_eff <= lo:
            return []
        rng = ctx.rng(tag)
        pairs = sample_band_pairs(ctx.routable_coords, lo, hi_eff, count * ctx.scale, rng)
        return [Spec(o, d, alpha, f"{tag}#{i}") for i, (o, d) in enumerate(pairs)]
    return sel


def _band_assert(max_detour: float | None = None):
    def check_band(ctx: Ctx, outcomes: list[Outcome], check):
        n = len(outcomes)
        found = [o for o in outcomes if o.route]
        check(f"all {n} pairs routed", len(found) == n, f"{len(found)}/{n} found")
        bad_bounds = bad_len = bad_chain = bad_detour = 0
        for o in found:
            r = o.route
            hav = float(haversine_m(*o.spec.o, *o.spec.d))
            if not (0.0 <= r.walk_score <= 1.0):
                bad_bounds += 1
            if r.total_length < hav - 1.0:
                bad_len += 1
            if not _chains(r):
                bad_chain += 1
            if max_detour is not None and hav > 1.0 and r.total_length > max_detour * hav:
                bad_detour += 1
        check("walk_score in [0,1]", bad_bounds == 0, f"{bad_bounds} out of range")
        check("length ≥ straight-line", bad_len == 0, f"{bad_len} shorter than crow-fly")
        check("edges chain head-to-tail", bad_chain == 0, f"{bad_chain} broken")
        if max_detour is not None:
            check(f"length ≤ {max_detour:g}× straight-line",
                  bad_detour == 0, f"{bad_detour} gross detours")
    return check_band


def _seam_selector(ctx: Ctx) -> list[Spec]:
    inside, outside = ctx.inside_coords, ctx.deep_outside_coords
    if len(inside) < 8 or len(outside) < 8:
        return []  # inventory ≈ whole graph → no seam to straddle (e.g. Austin)
    rng = ctx.rng("seam")
    specs: list[Spec] = []
    tries = 0
    want = 6 * ctx.scale
    while len(specs) < want and tries < want * 500:
        tries += 1
        o = rng.choice(inside)
        d = rng.choice(outside)
        if 300.0 <= haversine_m(o[0], o[1], d[0], d[1]) <= 6000.0:
            specs.append(Spec(o, d, 2.0, f"seam#{len(specs)}"))
    return specs


def _seam_assert(ctx: Ctx, outcomes: list[Outcome], check):
    found = [o for o in outcomes if o.route]
    check("seam routes found", len(found) == len(outcomes),
          f"{len(found)}/{len(outcomes)}")
    # (a) every edge on a border-crossing route carries an environment_score
    missing_env = 0
    for o in found:
        for u, v, k in o.route.edges:
            if _as_float(ctx.G[u][v][k].get("environment_score")) is None:
                missing_env += 1
    check("every seam-route edge has environment_score", missing_env == 0,
          f"{missing_env} edges missing env (env layers narrower than graph?)")
    # (b) OSM-tier scoring uniform across the seam (uses ALL edges, not just routes)
    _seam_uniformity(ctx, check)


def _seam_uniformity(ctx: Ctx, check):
    """Assert no-inventory-town OSM-tier edges are not PENALISED relative to the
    city's own OSM-tier edges (the mode-E trap: border edges silently scoring
    lower). This is deliberately **one-sided**: the city's OSM-tier edges are a
    biased *residual* (the ~half that didn't match the inventory skew toward
    arterials), so the border towns' full street mix scoring *higher* is benign
    composition, not a bug. Only the border scoring *lower* signals lost degradation.
    """
    out_scores: list[float] = []
    in_scores: list[float] = []
    G = ctx.G
    for u, v, d in G.edges(data=True):
        if _as_str(d.get("data_source")) == "city_inventory":
            continue  # only OSM-tier edges are comparable across the seam
        yu, xu = float(G.nodes[u]["y"]), float(G.nodes[u]["x"])
        yv, xv = float(G.nodes[v]["y"]), float(G.nodes[v]["x"])
        w = edge_walkability(d)[0]
        if ctx.is_deep_outside(yu, xu) and ctx.is_deep_outside(yv, xv):
            out_scores.append(w)
        elif ctx._is_inside(yu, xu) or ctx._is_inside(yv, xv):
            in_scores.append(w)
    if len(out_scores) < 20 or len(in_scores) < 20:
        print(f"    [INFO] seam uniformity skipped — "
              f"{len(out_scores)} outside / {len(in_scores)} inside OSM-tier edges")
        return
    in_med, out_med = float(np.median(in_scores)), float(np.median(out_scores))
    penalty = in_med - out_med   # positive ⇒ border edges score LOWER (the bug)
    check("OSM-tier edges across the seam are not penalised (border keeps its score)",
          penalty <= SEAM_SCORE_TOL,
          f"inside {in_med:.3f} vs outside {out_med:.3f} (border deficit {penalty:+.3f})")


def _water_selector(ctx: Ctx) -> list[Spec]:
    wat = ctx.water
    if wat is None:
        return []
    poly_wgs, _ = wat
    cand = ctx._nodes_near_water(WATER_REACH_M)
    if len(cand) < 8:
        return []
    rng = ctx.rng("water")
    specs: list[Spec] = []
    tries = 0
    want = 5 * ctx.scale
    while len(specs) < want and tries < want * 800:
        tries += 1
        _, ola, olo = rng.choice(cand)
        _, dla, dlo = rng.choice(cand)
        hav = haversine_m(ola, olo, dla, dlo)
        if not (100.0 <= hav <= 2500.0):
            continue
        # opposite banks at a real crossing: the straight O–D segment cuts the water
        if LineString([(olo, ola), (dlo, dla)]).intersects(poly_wgs):
            specs.append(Spec((ola, olo), (dla, dlo), 2.0, f"water#{len(specs)}"))
    return specs


def _water_assert(ctx: Ctx, outcomes: list[Outcome], check):
    wat = ctx.water
    poly_wgs = wat[0] if wat else None
    n = len(outcomes)
    found = [o for o in outcomes if o.route]
    # headline: a severed bridge deck shows up as an unreachable crossing
    check("water crossings routable (bridge decks survived the graph filter)",
          len(found) == n, f"{len(found)}/{n} crossings found")
    crossed = detour = 0
    for o in found:
        line = _route_line_wgs(ctx.G, o.route)
        if poly_wgs is not None and line is not None and line.intersects(poly_wgs):
            crossed += 1
        hav = float(haversine_m(*o.spec.o, *o.spec.d))
        if hav > 1.0 and o.route.total_length > WATER_DETOUR_FACTOR * hav:
            detour += 1
    check("routes actually cross the water (not a walk-around)",
          crossed == len(found), f"{len(found) - crossed} did not intersect the water")
    check(f"no gross detour (≤ {WATER_DETOUR_FACTOR:g}× straight-line)",
          detour == 0, f"{detour} detoured far (severed nearby crossing?)")


def _anchor_selector(ctx: Ctx) -> list[Spec]:
    walk = ctx.node_walk
    routable_ids = {nid for nid, _, _ in ctx.routable_nodes}
    vals = [walk[n] for n in walk if n in routable_ids]
    if len(vals) < 40:
        return []
    p90 = float(np.percentile(vals, 90))
    high_nodes = [(nid, la, lo) for nid, la, lo in ctx.routable_nodes
                  if walk.get(nid, 0.0) >= p90]

    ids, _, _ = clip._node_coords(ctx.G)
    hwy = ctx.node_hwy
    hwy_by_id = {ids[i]: float(hwy[i]) for i in range(len(ids))}
    low_nodes = [(nid, la, lo) for nid, la, lo in ctx.routable_nodes
                 if hwy_by_id.get(nid, 1.0) <= 0.15]

    hi_cluster = _densest_cluster(high_nodes, ctx.rng("hi"))
    lo_cluster = _densest_cluster(low_nodes, ctx.rng("lo"))
    if len(hi_cluster) < 3 or len(lo_cluster) < 3:
        return []

    specs: list[Spec] = []
    hi_coords = [(la, lo) for _, la, lo in hi_cluster]
    lo_coords = [(la, lo) for _, la, lo in lo_cluster]
    for i, (o, d) in enumerate(
            sample_band_pairs(hi_coords, 120.0, 1500.0, 4 * ctx.scale, ctx.rng("hip"))):
        specs.append(Spec(o, d, 2.0, f"high#{i}", role="high"))
    # α=0 pins the low anchor onto the stroad (a detour would defeat the point)
    for i, (o, d) in enumerate(
            sample_band_pairs(lo_coords, 120.0, 1500.0, 4 * ctx.scale, ctx.rng("lop"))):
        specs.append(Spec(o, d, 0.0, f"low#{i}", role="low"))
    return specs


def _anchor_assert(ctx: Ctx, outcomes: list[Outcome], check):
    hi = [o.route.walk_score for o in outcomes if o.route and o.spec.role == "high"]
    lo = [o.route.walk_score for o in outcomes if o.route and o.spec.role == "low"]
    if not hi or not lo:
        check("walkability anchors resolved on both ends", False,
              f"high={len(hi)} low={len(lo)} routes")
        return
    hm, lm = float(np.median(hi)), float(np.median(lo))
    print(f"    [INFO] anchor medians — high-walk {hm:.3f}, arterial {lm:.3f}")
    check(f"high-walk corridor scores ≥ arterial by {ANCHOR_MARGIN:g} "
          f"(condition/safety layers discriminate)",
          hm - lm >= ANCHOR_MARGIN, f"margin {hm - lm:+.3f}")


def _alpha_floor_selector(ctx: Ctx) -> list[Spec]:
    rng = ctx.rng("alpha")
    pairs = sample_band_pairs(ctx.routable_coords, 800.0, 3000.0, 5 * ctx.scale, rng)
    return [Spec(o, d, 0.0, f"alpha#{i}") for i, (o, d) in enumerate(pairs)]


def _alpha_floor_assert(ctx: Ctx, outcomes: list[Outcome], check):
    viol = tested = 0
    for o in outcomes:
        if not o.route:
            continue
        hi = find_routes(ctx.G, o.spec.o, o.spec.d, alpha=3.0)
        if not hi:
            continue
        tested += 1
        if o.route.total_length > hi[0].total_length + 1.0:   # α=0 is the length floor
            viol += 1
    check("α=0 route never longer than α=3 (length floor)",
          viol == 0, f"{viol}/{tested} violated")


def _determinism_selector(ctx: Ctx) -> list[Spec]:
    pairs = sample_band_pairs(ctx.routable_coords, 200.0, 800.0, 1, ctx.rng("det"))
    return [Spec(o, d, 2.0, "det#0") for o, d in pairs]


def _determinism_assert(ctx: Ctx, outcomes: list[Outcome], check):
    if not outcomes or not outcomes[0].route:
        check("determinism route found", False, "no route to test")
        return
    s = outcomes[0].spec
    a = find_routes(ctx.G, s.o, s.d, alpha=s.alpha)
    b = find_routes(ctx.G, s.o, s.d, alpha=s.alpha)
    same = bool(a) and bool(b) and a[0].nodes == b[0].nodes
    check("identical query → identical route", same, "" if same else "node paths differ")


def _footno_selector(ctx: Ctx) -> list[Spec]:
    rng = ctx.rng("footno")
    specs: list[Spec] = []
    for lo, hi, tag in ((150.0, 800.0, "fn_s"), (800.0, 3000.0, "fn_m"),
                        (3000.0, 12000.0, "fn_l")):
        hi_eff = min(hi, 0.9 * ctx.graph_diag_m) if ctx.graph_diag_m else hi
        if hi_eff <= lo:
            continue
        for i, (o, d) in enumerate(sample_band_pairs(ctx.routable_coords, lo, hi_eff,
                                                     3 * ctx.scale, rng)):
            specs.append(Spec(o, d, 2.0, f"{tag}#{i}"))
    return specs


def _footno_assert(ctx: Ctx, outcomes: list[Outcome], check):
    bad = 0
    for o in outcomes:
        if not o.route:
            continue
        for u, v, k in o.route.edges:
            if _as_str(ctx.G[u][v][k].get("foot_access")) == "no":
                bad += 1
    check("no foot=no edge ever appears on a route", bad == 0,
          f"{bad} impassable edges traversed")


def _restricted_selector(ctx: Ctx) -> list[Spec]:
    G = ctx.G
    rng = ctx.rng("restricted")
    dests = []
    for u, v, k, d in G.edges(keys=True, data=True):
        if _as_str(d.get("foot_access")) in RESTRICTED_FOOT_ACCESS:
            dests.append(v)
            if len(dests) > 2000:
                break
    if not dests:
        return []
    rng.shuffle(dests)
    coords = ctx.routable_coords
    specs: list[Spec] = []
    for v in dests:
        if len(specs) >= 3 * ctx.scale:
            break
        dlat, dlon = float(G.nodes[v]["y"]), float(G.nodes[v]["x"])
        near = [(la, lo) for la, lo in (rng.choice(coords) for _ in range(60))
                if 200.0 <= haversine_m(la, lo, dlat, dlon) <= 700.0]
        if near:
            specs.append(Spec(near[0], (dlat, dlon), 2.0, f"restricted@{v}"))
    return specs


def _restricted_assert(ctx: Ctx, outcomes: list[Outcome], check):
    found = [o for o in outcomes if o.route]
    flagged = 0
    if audit_route is not None:
        for o in found:
            fl = audit_route(ctx.G, o.route, alpha=o.spec.alpha).get("flags", [])
            if any("RESTRICTED" in f for f in fl):
                flagged += 1
    print(f"    [INFO] terminal restricted-access: {len(found)}/{len(outcomes)} "
          f"reachable, {flagged} used restricted access at an endpoint "
          f"(design choice — not gated)")


# ---------------------------------------------------------------------------
# THE GLOBAL LIST
# ---------------------------------------------------------------------------

ROUTE_TYPES: list[RouteType] = [
    RouteType("short_band", "local routing valid at 150–800 m", ("I", "K"),
              selector=_band(150.0, 800.0, 6, 2.0, "short"),
              assertions=_band_assert(),
              look_for="A short walk in a dense area — is the score plausible block by block?"),
    RouteType("mid_band", "routing valid at 800–3000 m", ("I", "K"),
              selector=_band(800.0, 3000.0, 6, 2.0, "mid"),
              assertions=_band_assert(),
              look_for="A neighbourhood-scale walk — corridor and alternatives sensible?"),
    RouteType("long_band", "cross-city routing without gross detours", ("I", "J"),
              selector=_band(3000.0, 12000.0, 6, 2.0, "long"),
              assertions=_band_assert(max_detour=LONG_DETOUR_FACTOR),
              look_for="A long cross-city walk — any absurd detours?"),
    RouteType("data_seam_straddling",
              "border edges keep environment + OSM-tier scoring across a data seam",
              ("B", "E"), selector=_seam_selector, assertions=_seam_assert,
              pin_survey=True,
              look_for="Crosses from city-inventory territory into an OSM-only town — "
                       "does the score degrade smoothly, not cliff?"),
    RouteType("over_water_crossing",
              "a pedestrian bridge deck survived the graph download filter", ("J",),
              selector=_water_selector, assertions=_water_assert, pin_survey=True,
              look_for="Crosses the main river/lake — is a real bridge used, "
                       "or is it forced miles around?"),
    RouteType("walkability_anchors",
              "a good corridor outscores a stroad (relative discrimination)",
              ("D", "C", "H", "K"), selector=_anchor_selector, assertions=_anchor_assert,
              pin_survey=True,
              look_for="Compare the high-walk anchor vs the arterial anchor — "
                       "is the gap believable?"),
    RouteType("alpha_floor_monotonicity", "cost model: α=0 is the length floor", (),
              selector=_alpha_floor_selector, assertions=_alpha_floor_assert),
    RouteType("determinism", "identical query returns an identical route", (),
              selector=_determinism_selector, assertions=_determinism_assert),
    RouteType("foot_no_integrity", "the router never emits a foot=no edge", (),
              selector=_footno_selector, assertions=_footno_assert),
    RouteType("terminal_restricted_access",
              "terminal restricted-access handling (INFO — open design decision)", (),
              selector=_restricted_selector, assertions=_restricted_assert, gates=False,
              look_for="Destination sits behind customers/private access — reasonable?"),
    RouteType("csr_nx_parity", "the compact CSR router matches the MultiDiGraph router",
              (), kind="external",
              note="verify_city invokes verify_csr_parity.run(city); different substrate."),
]


# ---------------------------------------------------------------------------
# Battery runner (used by verify_city)
# ---------------------------------------------------------------------------

def _candidate(rt: RouteType, o: Outcome) -> dict:
    """A calibration-deck candidate from a routed outcome (see calibration_survey)."""
    return {
        "name": f"{rt.name}:{o.spec.label}".replace(" ", "_"),
        "area": f"{rt.name} · {o.spec.label} · walk={o.route.walk_score:.2f}",
        "origin": o.spec.o,
        "dest": o.spec.d,
        "alpha": o.spec.alpha,
        "walk": o.route.walk_score,
        "look_for": rt.look_for or "Is the model's score believable for this route?",
        "pin": rt.pin_survey,
    }


def run_battery(ctx: Ctx, check) -> tuple[list[dict], int, int, list[tuple]]:
    """Run every battery RouteType for one city; drive ``check`` with the results.

    ``check(name, ok, detail)`` is the orchestrator's PASS/FAIL accumulator.
    Non-gating types (``gates=False``) wrap ``check`` so they only print INFO.
    Returns (survey candidates, #types run, #types skipped, outcomes) where
    ``outcomes`` is a flat list of ``(RouteType, Outcome)`` for the per-city
    drift baseline (verify_city keys each by ``name:label``).
    """
    candidates: list[dict] = []
    all_outcomes: list[tuple] = []
    ran = skipped = 0
    for rt in ROUTE_TYPES:
        if rt.kind != "battery":
            continue
        specs = rt.selector(ctx) if rt.selector else []
        if not specs:
            skipped += 1
            print(f"  · {rt.name}: SKIP (precondition absent)")
            continue
        ran += 1
        outcomes = []
        for s in specs:
            try:
                routes = find_routes(ctx.G, s.o, s.d, alpha=s.alpha)
                outcomes.append(Outcome(s, routes[0] if routes else None))
            except Exception as exc:  # keep the battery going; record the failure
                outcomes.append(Outcome(s, None, f"{type(exc).__name__}: {exc}"))
        n_found = sum(1 for o in outcomes if o.route)
        print(f"  · {rt.name}: {n_found}/{len(outcomes)} routed"
              + ("" if rt.gates else "  [INFO]"))
        for o in outcomes:
            all_outcomes.append((rt, o))
            if o.route:
                candidates.append(_candidate(rt, o))
        rt_check = check if rt.gates else (lambda name, ok, detail="": None)
        if rt.assertions:
            rt.assertions(ctx, outcomes, rt_check)
    return candidates, ran, skipped, all_outcomes
