"""
Routing diagnostics — a small toolkit for figuring out *why* the router chose a
route, and whether a bad route is a DATA problem or a WEIGHT problem.

Run the built-in demo:

    python notebooks/diagnostics.py

Or import the pieces from another notebook / the REPL:

    from diagnostics import (route_between, breakdown_route, audit_scoring_coverage,
                             edge_vs_detour, score_heatmap)

Adapted to THIS codebase
------------------------
The generic "crossing penalty" debugging framework doesn't map directly here,
so this toolkit targets what our scorer actually uses:

  * Our routes come from `routing.router.find_routes` and carry per-edge
    `walk_score` / `surface_score` / `highway_score` / `foot_access`.
  * Our cost model is `length * (1 + alpha*(1 - walk_score))` — NOT
    `length / penalty`. The sensitivity analysis below uses the real model.
  * Crossings are NOT a scoring factor. They exist only as `highway=crossing`
    NODES (~8k of them); the edge-cost router ignores node attributes entirely.
    `audit_scoring_coverage` reports the crossing-node count so you can see
    they're present, but flags that routing does not currently use them.

So "data gap vs. weight problem" here means: is an edge scored badly because a
factor (surface/material/foot_access) is MISSING and it fell back to a coarse
tier (data gap), or because the factor is present but the weights/penalty don't
push routing the way you'd expect (weight problem)?
"""

from __future__ import annotations

import networkx as nx

from walkability.graph.build import ENRICHED_PATH, DEV_ENRICHED_PATH, load_graph
from walkability.routing import clip
from walkability.routing.cost import ALPHA_DEFAULT, edge_cost
from walkability.routing.router import find_routes
from walkability.scoring.factors import (
    RESTRICTED_FOOT_ACCESS,
    _as_float,
    _as_str,
    edge_walkability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def route_between(G, orig, dest, alpha: float = ALPHA_DEFAULT, **kwargs):
    """Best RouteResult between two (lat, lon) points, or None if no route."""
    routes = find_routes(G, orig, dest, alpha=alpha, **kwargs)
    return routes[0] if routes else None


def _edge_coords(G, u, v, key):
    """(lat, lon) vertices of an edge — its geometry if present, else the endpoints."""
    geom = G[u][v][key].get("geometry")
    if geom is not None:
        return [(lat, lon) for lon, lat in geom.coords]   # shapely is (lon, lat)
    return [(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])]


def _route_coords(G, route):
    """(lat, lon) vertices along a RouteResult, in order."""
    return [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in route.nodes]


def _nodes_within(G, center, dist_m):
    """Set of node ids within `dist_m` metres of (lat, lon) `center`."""
    ids, lats, lons = clip._node_coords(G)
    dsum = clip.haversine_m(lats, lons, center[0], center[1])
    return {ids[i] for i in range(len(ids)) if dsum[i] <= dist_m}


# ---------------------------------------------------------------------------
# Step 1: route introspection — what did the router actually choose, and why?
# ---------------------------------------------------------------------------

def breakdown_route(G, route, alpha: float = ALPHA_DEFAULT) -> None:
    """Print a per-edge breakdown of a RouteResult: scores, cost, and source tier.

    The `cost` column is the real routing cost of each edge at this alpha, so
    you can see which segments dominate the path's total cost.
    """
    if route is None:
        print("  (no route)")
        return

    print(f"=== Route breakdown (alpha={alpha}) ===")
    print(f"  total: {route.total_length:.0f} m  walk={route.walk_score:.3f}  "
          f"conf={route.confidence:.3f}  cost={route.total_cost:.0f}  "
          f"hops={len(route.edges)}\n")
    hdr = (f"  {'hwy':<12}{'hwy_s':>6}{'surf':>6}{'matl':>6}{'walk':>6}"
           f"{'foot':>11}{'len':>7}{'cost':>8}  source")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for u, v, key in route.edges:
        d = G[u][v][key]
        walk, _ = edge_walkability(d)
        cost = edge_cost(d, alpha)

        def f(x):
            x = _as_float(d.get(x))
            return f"{x:.2f}" if x is not None else "  -"

        print(f"  {str(_as_str(d.get('highway')) or '-'):<12}"
              f"{f('highway_score'):>6}{f('surface_score'):>6}"
              f"{f('surface_material_score'):>6}{walk:>6.2f}"
              f"{str(_as_str(d.get('foot_access')) or '-'):>11}"
              f"{(_as_float(d.get('length')) or 0):>7.0f}"
              f"{(cost if cost is not None else float('nan')):>8.0f}"
              f"  {d.get('data_source')}")


def safety_breakdown(G, route, alpha: float = ALPHA_DEFAULT) -> None:
    """Per-edge SAFETY sub-score breakdown (the D1 calibration inspector).

    Columns: on = on-path car-safety (maxspeed of the road you walk along),
    off = off-path arterial-proximity, car = min(on, off) graded by separation and
    knocked down by industrial exposure, eyes = perceived safety, env = the
    safety-dimension composite, ind = industrial_exposure (A), sep = road_separation
    (B). Lets you see *which segment* and *which sub-signal* drives a route's safety,
    so each calibration lever can be tuned one at a time. All fields are read from
    the baked edge data (ind/sep are blank on graphs built before the env-rework).
    """
    if route is None:
        print("  (no route)")
        return

    def wmean(field):
        num = den = 0.0
        for u, v, key in route.edges:
            x = _as_float(G[u][v][key].get(field))
            L = _as_float(G[u][v][key].get("length")) or 0.0
            if x is not None:
                num += x * L
                den += L
        return num / den if den else float("nan")

    print(f"=== Safety breakdown (alpha={alpha}) ===")
    print(f"  route means: on={wmean('maxspeed_safety_score'):.2f} "
          f"off={wmean('arterial_proximity_score'):.2f} "
          f"car={wmean('car_safety_score'):.2f} "
          f"eyes={wmean('eyes_score'):.2f} env={wmean('environment_score'):.2f}\n")
    hdr = (f"  {'street':<22}{'on':>5}{'off':>5}{'car':>5}{'eyes':>5}{'env':>5}"
           f"{'ind':>5}{'sep':>5}{'len':>7}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for u, v, key in route.edges:
        d = G[u][v][key]

        def f(x):
            x = _as_float(d.get(x))
            return f"{x:.2f}" if x is not None else "  -"

        name = _as_str(d.get("name")) or _as_str(d.get("highway")) or "-"
        print(f"  {name[:22]:<22}{f('maxspeed_safety_score'):>5}"
              f"{f('arterial_proximity_score'):>5}{f('car_safety_score'):>5}"
              f"{f('eyes_score'):>5}{f('environment_score'):>5}"
              f"{f('industrial_exposure'):>5}{f('road_separation'):>5}"
              f"{(_as_float(d.get('length')) or 0):>7.0f}")


def compare_alphas(G, orig, dest, alphas=(0.0, 2.0, 5.0), **kwargs) -> dict:
    """Run several alphas and print a one-line summary of each best route.

    Returns {alpha: RouteResult|None} so callers can breakdown_route() any of them.
    """
    out = {}
    print(f"=== alpha comparison  {orig} -> {dest} ===")
    for a in alphas:
        r = route_between(G, orig, dest, alpha=a, **kwargs)
        out[a] = r
        if r:
            print(f"  alpha={a:<4}  len={r.total_length:7.0f}m  walk={r.walk_score:.3f}  "
                  f"conf={r.confidence:.3f}  hops={len(r.edges)}")
        else:
            print(f"  alpha={a:<4}  (no route)")
    return out


# ---------------------------------------------------------------------------
# Step 2: data audit — what does the scorer have to work with?
# ---------------------------------------------------------------------------

def audit_scoring_coverage(G, center=None, dist_m: float = 400.0) -> None:
    """Report factor coverage and enrichment-tier mix for the whole graph.

    If `center` (lat, lon) is given, also reports the same for edges within
    `dist_m` of that point — useful for auditing a specific problem corridor.
    """
    def report(edges, label):
        n = len(edges)
        if n == 0:
            print(f"  {label}: no edges")
            return
        miss_surf = sum(1 for d in edges if _as_float(d.get("surface_score")) is None)
        miss_matl = sum(1 for d in edges if _as_float(d.get("surface_material_score")) is None)
        from collections import Counter
        tiers = Counter()
        for d in edges:
            src = str(d.get("data_source", ""))
            if src.startswith("highway="):
                tiers["osm_tag"] += 1
            elif src.startswith("context"):
                tiers["context"] += 1
            elif src.startswith("no_tag"):
                tiers["geometric"] += 1
            else:
                tiers[src or "?"] += 1
        print(f"  {label}: {n} edges")
        print(f"    missing surface_score          : {miss_surf:6} ({100*miss_surf//n}%) "
              f"→ scored on highway/material only")
        print(f"    missing surface_material_score : {miss_matl:6} ({100*miss_matl//n}%)")
        print(f"    enrichment tier mix            : {dict(tiers)}")

    print("=== Scoring-factor coverage ===")
    report([d for *_, d in G.edges(keys=True, data=True)], "whole graph")

    # Crossings are nodes, not edges, and the router ignores them. Report the
    # count so it's visible, but be explicit that it does not affect routing.
    n_cross = sum(1 for _, d in G.nodes(data=True) if _as_str(d.get("highway")) == "crossing")
    print(f"\n  highway=crossing NODES: {n_cross}  "
          f"(NOTE: routing is edge-cost based and does not use crossing nodes — "
          f"there is no crossing factor in FACTOR_WEIGHTS)")

    if center is not None:
        ids, lats, lons = clip._node_coords(G)
        dsum = clip.haversine_m(lats, lons, center[0], center[1])
        local_nodes = {ids[i] for i in range(len(ids)) if dsum[i] <= dist_m}
        local_edges = [d for u, v, d in G.edges(data=True)
                       if u in local_nodes and v in local_nodes]
        print()
        report(local_edges, f"within {dist_m:.0f} m of {center}")
        lc = sum(1 for n in local_nodes if _as_str(G.nodes[n].get("highway")) == "crossing")
        print(f"    highway=crossing nodes here    : {lc}")


# ---------------------------------------------------------------------------
# Step 3: score sensitivity — is taking a bad edge actually cheaper than detouring?
# ---------------------------------------------------------------------------

def edge_vs_detour(G, edge_id, detour_len_m: float,
                   alpha: float = ALPHA_DEFAULT, detour_walk: float = 0.9) -> None:
    """Compare the real cost of one edge against detouring around it.

    Uses the actual cost model `length * (1 + alpha*(1 - walk))`. Answers: would
    the router avoid `edge_id` if a `detour_len_m` alternative at walk=`detour_walk`
    existed? If the edge is still cheaper, the penalty (or alpha) is too weak to
    change the decision — a weight problem, not a data problem.
    """
    u, v, key = edge_id
    d = G[u][v][key]
    walk, _ = edge_walkability(d)
    c = edge_cost(d, alpha)
    detour_cost = detour_len_m * (1.0 + alpha * (1.0 - detour_walk))

    print(f"=== edge {edge_id} vs {detour_len_m:.0f} m detour (alpha={alpha}) ===")
    print(f"  edge   : len={_as_float(d.get('length')) or 0:.0f}m  walk={walk:.3f}  "
          f"foot={_as_str(d.get('foot_access'))}  cost={c}")
    print(f"  detour : len={detour_len_m:.0f}m  walk={detour_walk:.2f}  cost={detour_cost:.0f}")
    if c is None:
        print("  → edge is impassable (foot=no); router never uses it.")
    elif c <= detour_cost:
        print(f"  → router PREFERS the edge (cheaper by {detour_cost - c:.0f}). "
              f"To avoid it, raise alpha or lower the edge's walk_score.")
    else:
        print(f"  → router prefers the DETOUR (edge dearer by {c - detour_cost:.0f}).")


# ---------------------------------------------------------------------------
# Step 4: visualization — colour-coded score heatmap over an area
# ---------------------------------------------------------------------------

def _edge_color_tip(G, u, v, key, d, alpha, metric):
    """(color, tooltip) for one edge under the chosen metric. Red→green = bad→good."""
    walk, _ = edge_walkability(d)
    if metric == "cost":
        c = edge_cost(d, alpha)
        length = _as_float(d.get("length")) or 1.0
        # normalise cost/m to [0,1]: 1.0 (walk=1) → green, (1+alpha) → red
        norm = 0.0 if c is None else min(1.0, ((c / length) - 1.0) / max(alpha, 1e-9))
        quality = 1.0 - norm
        tip = f"cost/m={'inf' if c is None else f'{c/length:.2f}'} | walk={walk:.2f}"
    else:
        quality = walk
        tip = f"walk={walk:.2f} | {_as_str(d.get('highway'))} | {d.get('data_source')}"
    color = f"#{int(255*(1-quality)):02x}{int(255*quality):02x}00"
    return color, tip


def _add_score_layer(parent, G, node_set, alpha, metric):
    """Add a red→green edge polyline for every edge within `node_set` to `parent`."""
    import folium
    for u, v, key, d in G.edges(keys=True, data=True):
        if u not in node_set or v not in node_set:
            continue
        color, tip = _edge_color_tip(G, u, v, key, d, alpha, metric)
        folium.PolyLine(_edge_coords(G, u, v, key), color=color, weight=3,
                        opacity=0.75, tooltip=tip).add_to(parent)


def score_heatmap(G, center, dist_m: float = 500.0, alpha: float = ALPHA_DEFAULT,
                  metric: str = "walk_score", out=None):
    """Write an interactive HTML map colouring every edge near `center` red→green.

    metric="walk_score" colours by walkability (red=bad, green=good).
    metric="cost"       colours by cost-per-metre at this alpha (red=expensive).
    Returns the output path. Requires folium.
    """
    import folium
    from walkability.config import PROJECT_ROOT

    if out is None:
        out = PROJECT_ROOT / "notebooks" / "score_heatmap.html"

    fmap = folium.Map(location=list(center), zoom_start=16, tiles="cartodbpositron")
    _add_score_layer(fmap, G, _nodes_within(G, center, dist_m), alpha, metric)
    fmap.save(str(out))
    print(f"Heatmap ({metric}) written to {out}")
    return out


def routes_over_heatmap(G, labeled_routes, heatmap_alpha: float = ALPHA_DEFAULT,
                        metric: str = "walk_score", out=None, pad_m: float = 150.0):
    """Overlay route polylines on top of the score heatmap, in one HTML map.

    Parameters
    ----------
    labeled_routes : list of (label, RouteResult)
        Routes to draw on top. The map auto-fits to cover them all.
    heatmap_alpha, metric :
        Control the background edge colouring (see score_heatmap).
    pad_m :
        Extra metres of heatmap drawn beyond the route bounding region.

    The heatmap and the routes are separate toggleable layers (LayerControl),
    so you can switch the background off to see the paths alone. Returns the
    output path. Requires folium.
    """
    import folium
    from walkability.config import PROJECT_ROOT

    labeled_routes = [(lbl, r) for lbl, r in labeled_routes if r is not None]
    if not labeled_routes:
        print("routes_over_heatmap: no routes to draw.")
        return None
    if out is None:
        out = PROJECT_ROOT / "notebooks" / "routes_over_heatmap.html"

    # Auto-extent: centre on the mean of all route vertices, radius = farthest
    # vertex + padding, so the heatmap always covers every route.
    all_coords = [c for _, r in labeled_routes for c in _route_coords(G, r)]
    clat = sum(c[0] for c in all_coords) / len(all_coords)
    clon = sum(c[1] for c in all_coords) / len(all_coords)
    radius = max(float(clip.haversine_m(clat, clon, la, lo)) for la, lo in all_coords) + pad_m

    fmap = folium.Map(location=[clat, clon], zoom_start=16, tiles="cartodbpositron")

    heat = folium.FeatureGroup(name=f"{metric} heatmap", show=True)
    _add_score_layer(heat, G, _nodes_within(G, (clat, clon), radius), heatmap_alpha, metric)
    heat.add_to(fmap)

    # Routes drawn thick and in cool/dark colours that read over red↔green.
    colours = ["#0000ff", "#8800cc", "#000000", "#00aaff", "#cc0088"]
    routes_layer = folium.FeatureGroup(name="routes", show=True)
    for i, (label, r) in enumerate(labeled_routes):
        coords = _route_coords(G, r)
        folium.PolyLine(
            coords, color=colours[i % len(colours)], weight=6, opacity=0.9,
            tooltip=f"{label}  len={r.total_length:.0f}m  walk={r.walk_score:.3f}  "
                    f"conf={r.confidence:.3f}",
        ).add_to(routes_layer)
        folium.CircleMarker(coords[0], radius=5, color="green", fill=True,
                            fill_opacity=1, tooltip=f"{label} — start").add_to(routes_layer)
        folium.CircleMarker(coords[-1], radius=5, color="red", fill=True,
                            fill_opacity=1, tooltip=f"{label} — end").add_to(routes_layer)
    routes_layer.add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.save(str(out))
    print(f"Routes-over-heatmap ({metric}, {len(labeled_routes)} routes) written to {out}")
    return out


# ===========================================================================
# Three-tier inspection: cheap automated flags → visual map → street imagery
# ===========================================================================
# Tier 1 finds suspicious routes with no visual inspection. Tier 2 diagnoses
# them on a map in seconds. Tier 3 is the last resort for true ground truth.

# --- Tier 1: automated route auditing (no visuals) -------------------------

# A route's own edges with low highway_score ARE the arterial-exposure signal
# here (HIGHWAY_SCORES: trunk .03, primary .08, secondary .15). Threshold sits
# just above secondary so primary/secondary/trunk are caught.
_ARTERIAL_HWY_SCORE = 0.15
_LOW_EDGE_WALK = 0.20          # an edge this bad is worth a look
_LOW_SCORE_LENGTH_FRAC = 0.15  # >this share of length below 0.4 walk = flag
_COARSE_TIER_FRAC = 0.50       # >this share of edges off city data = low confidence


def audit_route(G, route, alpha: float = ALPHA_DEFAULT) -> dict:
    """Flag statistically suspicious properties of a route — no visuals needed.

    Adapted to our data: crossings are counted from `highway=crossing` NODES on
    the path (our edges carry no crossing tag), and arterial exposure is read
    from each edge's own `highway_score`. Crossing/step counts are informational
    (not scored); arterial/low-surface/restricted flags are what actually drag
    `walk_score` down. Uses the route's chosen parallel-edge keys.
    """
    from walkability.osm.tag_resolver import resolve_highway

    edges = [(u, v, k, G[u][v][k]) for u, v, k in route.edges]
    walks = [edge_walkability(d)[0] for *_, d in edges]
    lengths = [_as_float(d.get("length")) or 0.0 for *_, d in edges]
    total = sum(lengths) or 1.0

    n_crossings = sum(1 for n in route.nodes
                      if _as_str(G.nodes[n].get("highway")) == "crossing")
    n_arterials = sum(1 for *_, d in edges
                      if (_as_float(d.get("highway_score")) or 1.0) <= _ARTERIAL_HWY_SCORE)
    n_steps = sum(1 for *_, d in edges if resolve_highway(d.get("highway")) == "steps")
    n_restricted = sum(1 for *_, d in edges
                       if _as_str(d.get("foot_access")) in RESTRICTED_FOOT_ACCESS)
    low_len = sum(L for w, L in zip(walks, lengths) if w < 0.40)
    n_coarse = sum(1 for *_, d in edges if str(d.get("data_source", "")) != "city_inventory")

    flags = []
    if n_arterials >= 2:
        flags.append(f"ARTERIAL_EXPOSURE={n_arterials}")
    if n_steps >= 1:
        flags.append(f"STEPS={n_steps}")
    if n_restricted >= 1:
        flags.append(f"RESTRICTED_ACCESS={n_restricted}")
    if walks and min(walks) < _LOW_EDGE_WALK:
        flags.append(f"VERY_LOW_EDGE_WALK={min(walks):.2f}")
    if low_len / total > _LOW_SCORE_LENGTH_FRAC:
        flags.append(f"LOW_SCORE_HEAVY={low_len/total:.0%}")
    if n_coarse / len(edges) > _COARSE_TIER_FRAC:
        flags.append(f"LOW_CONFIDENCE_TIER={n_coarse}/{len(edges)}")
    if n_crossings >= 3:
        flags.append(f"HIGH_CROSSINGS={n_crossings} (informational; not scored)")

    return {
        "flags": flags,
        "n_crossings": n_crossings,
        "n_arterials": n_arterials,
        "n_steps": n_steps,
        "n_restricted": n_restricted,
        "min_walk": round(min(walks), 3) if walks else None,
        "mean_walk": round(sum(walks) / len(walks), 3) if walks else None,
        "total_length_m": round(total, 1),
        "coarse_tier_frac": round(n_coarse / len(edges), 2),
    }


# --- Tier 2: single-route inspector map ------------------------------------

def inspect_route_map(G, route, out=None, label: str = "route",
                      alpha: float = ALPHA_DEFAULT):
    """Per-edge red→green map of ONE route with rich hover tooltips.

    Each edge is coloured by its walk_score and its tooltip shows the fields
    that explain that score (highway, scores, surface, foot_access, source).
    `highway=crossing` nodes on the path are marked so you can see where the
    route crosses streets. This is the ~20-second "is this route actually bad?"
    check that replaces most Street View clicking. Requires folium.

    (To compare several routes, use `routes_over_heatmap` — it already overlays
    multiple paths; toggle the heatmap layer off to see them alone.)
    """
    import folium
    from walkability.config import PROJECT_ROOT

    if route is None:
        print("inspect_route_map: no route.")
        return None
    if out is None:
        out = PROJECT_ROOT / "notebooks" / "route_inspector.html"

    coords_all = _route_coords(G, route)
    center = coords_all[len(coords_all) // 2]
    fmap = folium.Map(location=center, zoom_start=16, tiles="cartodbpositron")

    for i, (u, v, key) in enumerate(route.edges):
        d = G[u][v][key]
        walk, conf = edge_walkability(d)
        color = f"#{int(255*(1-walk)):02x}{int(255*walk):02x}00"

        def g(field):
            val = _as_float(d.get(field))
            return f"{val:.2f}" if val is not None else "—"

        tooltip = (
            f"edge {i}: walk={walk:.2f} (conf {conf:.2f})<br>"
            f"highway: {_as_str(d.get('highway')) or '—'} "
            f"(score {g('highway_score')})<br>"
            f"surface: {g('surface_score')} | material: {g('surface_material_score')}<br>"
            f"foot_access: {_as_str(d.get('foot_access')) or '—'}<br>"
            f"length: {_as_float(d.get('length')) or 0:.0f} m<br>"
            f"source: {d.get('data_source')}"
        )
        folium.PolyLine(_edge_coords(G, u, v, key), color=color, weight=6,
                        opacity=0.85, tooltip=tooltip).add_to(fmap)

    # Crossing nodes (our crossings live on nodes, not edges).
    for n in route.nodes:
        if _as_str(G.nodes[n].get("highway")) == "crossing":
            folium.CircleMarker((G.nodes[n]["y"], G.nodes[n]["x"]), radius=7,
                                color="#cc0000", fill=True, fill_opacity=0.9,
                                tooltip="highway=crossing node (not scored)").add_to(fmap)

    folium.Marker(coords_all[0], icon=folium.Icon(color="green"),
                  tooltip=f"{label} — start").add_to(fmap)
    folium.Marker(coords_all[-1], icon=folium.Icon(color="red"),
                  tooltip=f"{label} — end").add_to(fmap)

    fmap.save(str(out))
    print(f"Route inspector ('{label}') written to {out}")
    return out


# --- Tier 3: street-level imagery links (last resort) ----------------------

def streetview_url(lat: float, lon: float) -> str:
    """Google Street View URL centred on a coordinate (no API key needed)."""
    return (f"https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={lat},{lon}")


def mapillary_url(lat: float, lon: float) -> str:
    """Mapillary viewer URL centred on a coordinate (no API key needed)."""
    return f"https://www.mapillary.com/app/?lat={lat}&lng={lon}&z=18"


def imagery_links_for_route(G, route, n: int = 3) -> None:
    """Print Mapillary + Street View links for a route's `n` worst edges.

    Use this only when the map tooltip data genuinely can't answer a ground-truth
    question (e.g. "is this brick run actually heaved?"). No token, no network —
    just clickable URLs at the midpoint of the lowest-walk_score edges.
    """
    scored = []
    for (u, v, key) in route.edges:
        d = G[u][v][key]
        scored.append((edge_walkability(d)[0], u, v, key))
    scored.sort(key=lambda t: t[0])

    print(f"=== Street-imagery links for {n} worst edges ===")
    for walk, u, v, key in scored[:n]:
        mid = _edge_coords(G, u, v, key)
        lat, lon = mid[len(mid) // 2]
        print(f"  walk={walk:.2f}  ({lat:.5f}, {lon:.5f})")
        print(f"    Mapillary  : {mapillary_url(lat, lon)}")
        print(f"    StreetView : {streetview_url(lat, lon)}")


def mapillary_coverage(lat: float, lon: float, radius_deg: float = 0.0005,
                       token: str | None = None, limit: int = 3):
    """OPTIONAL: query the Mapillary API for imagery near a point.

    Secondary to `imagery_links_for_route` — needs a free token (env
    MAPILLARY_TOKEN or the `token` arg) and the `requests` package. Returns a
    list of image records, or [] with a message if unavailable. Not wired into
    the main workflow; crossings (the usual reason to check coverage) aren't
    even scored here.
    """
    import os
    token = token or os.environ.get("MAPILLARY_TOKEN")
    if not token:
        print("mapillary_coverage: set MAPILLARY_TOKEN (free) to use this. Skipping.")
        return []
    try:
        import requests
    except ImportError:
        print("mapillary_coverage: `requests` not installed. Skipping.")
        return []
    resp = requests.get(
        "https://graph.mapillary.com/images",
        params={
            "access_token": token,
            "fields": "id,thumb_256_url,captured_at",
            "bbox": f"{lon-radius_deg},{lat-radius_deg},{lon+radius_deg},{lat+radius_deg}",
            "limit": limit,
        },
        timeout=10,
    )
    return resp.json().get("data", [])


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    GRAPH_PATH = ENRICHED_PATH        # swap to DEV_ENRICHED_PATH for the fast subset
    ORIGIN      = (42.3588, -71.0707)
    DESTINATION = (42.3601, -71.0631)

    G = load_graph(GRAPH_PATH)
    print()

    # Step 1
    routes = compare_alphas(G, ORIGIN, DESTINATION, alphas=(0.0, 2.0, 5.0))
    print()
    breakdown_route(G, routes[2.0], alpha=2.0)
    print()

    # Step 2
    audit_scoring_coverage(G, center=ORIGIN, dist_m=400.0)
    print()

    # Step 3 — first edge of the alpha=2 route vs a hypothetical 150 m detour
    if routes[2.0]:
        edge_vs_detour(G, routes[2.0].edges[0], detour_len_m=150.0, alpha=2.0)
        print()

    # Step 4
    score_heatmap(G, center=ORIGIN, dist_m=500.0, metric="walk_score")
    print()

    # Three-tier inspection on the alpha=2 route
    r = routes[2.0]
    if r:
        print("=== Tier 1: audit_route ===")
        print(" ", audit_route(G, r, alpha=2.0))
        print()
        inspect_route_map(G, r, label="demo alpha=2")   # Tier 2
        print()
        imagery_links_for_route(G, r, n=2)               # Tier 3
