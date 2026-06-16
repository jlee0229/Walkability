"""
Walkability route explorer — Streamlit UI.

Run with:
    streamlit run app/streamlit_app.py

Pick an origin and destination (by address, lat/lon, or by clicking the map),
tune the distance/walkability tradeoff (alpha) and the per-factor weights, and
the app shows walkability-ranked walking routes coloured by per-edge score.

The enriched graph is loaded once per session (`@st.cache_resource`) — the full
Boston graph is ~10 s on first load, then instant. Routing clips to a local
ellipse, so nearby trips stay fast even on the full graph.
"""

from __future__ import annotations

import streamlit as st

# --- Make the `walkability` package importable when run via `streamlit run` ---
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import folium
from streamlit_folium import st_folium

from walkability.graph.build import (
    DEV_REGIONS,
    ENRICHED_PATH,
    dev_region_path,
    load_graph,
)
from walkability.routing.cost import ALPHA_DEFAULT
from walkability.routing.router import find_routes
from walkability.scoring.factors import _as_float, _as_str, edge_walkability
from walkability.scoring.weights import FACTOR_WEIGHTS

st.set_page_config(page_title="Boston Walkability Routes", page_icon="🚶", layout="wide")

# Distinct colours for the ranked candidate routes (best → worst).
_ROUTE_COLORS = ["#1f77b4", "#9467bd", "#111111", "#17becf", "#e377c2"]


# ---------------------------------------------------------------------------
# Graph loading (cached once per session, keyed by path)
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Loading the walk graph (one-time, ~10 s for full Boston)…")
def get_graph(path_str: str):
    """Load and cache an enriched GraphML graph by path."""
    return load_graph(Path(path_str))


def _graph_center(G) -> tuple[float, float]:
    """Rough (lat, lon) centre of the graph's nodes (memoised on the graph)."""
    cached = G.graph.get("_center")
    if cached is None:
        ys = [d["y"] for _, d in G.nodes(data=True)]
        xs = [d["x"] for _, d in G.nodes(data=True)]
        cached = (sum(ys) / len(ys), sum(xs) / len(xs))
        G.graph["_center"] = cached
    return cached


# ---------------------------------------------------------------------------
# Geometry / rendering helpers (small inlines so we don't import notebooks/)
# ---------------------------------------------------------------------------

def _edge_coords(G, u, v, key):
    """(lat, lon) vertices of an edge — its geometry if present, else endpoints."""
    geom = G[u][v][key].get("geometry")
    if geom is not None:
        return [(lat, lon) for lon, lat in geom.coords]  # shapely is (lon, lat)
    return [(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])]


def _walk_color(walk: float) -> str:
    """Red (bad) → green (good) hex for a walk_score in [0, 1]."""
    return f"#{int(255 * (1 - walk)):02x}{int(255 * walk):02x}00"


def build_map(G, orig, dest, routes, weights, frame=True, zoom=14):
    """A folium map with current O/D markers and any returned routes.

    `frame` controls whether the camera is fitted to the current points/route.
    Set it True only when the map is being remounted on purpose (after a search /
    clear / region switch via the changing component key). The declared
    `location` is kept constant (graph centre) so a click never moves the camera —
    st_folium holds the user's current pan/zoom; re-framing happens only on a
    deliberate remount, where fit_bounds frames the route.
    """
    center = _graph_center(G)
    fmap = folium.Map(
        location=center, zoom_start=zoom, tiles="cartodbpositron",
        # Smooth zoom: zoom_snap=0 lets the map sit at any fractional zoom (no
        # clunky staircase), wheel_px_per_zoom_level=80 keeps one flick gentle.
        zoom_snap=0, zoom_delta=0.5, wheel_px_per_zoom_level=80,
    )

    # Draw routes worst→best so the best route ends up on top.
    for idx in range(len(routes) - 1, -1, -1):
        route = routes[idx]
        if idx == 0:
            # Best route: colour each edge by its own walk_score with tooltips.
            for i, (u, v, key) in enumerate(route.edges):
                d = G[u][v][key]
                walk, conf = edge_walkability(d, weights)
                tip = (
                    f"edge {i}: walk={walk:.2f} (conf {conf:.2f})<br>"
                    f"highway: {_as_str(d.get('highway')) or '—'}<br>"
                    f"foot_access: {_as_str(d.get('foot_access')) or '—'}<br>"
                    f"length: {_as_float(d.get('length')) or 0:.0f} m"
                )
                folium.PolyLine(
                    _edge_coords(G, u, v, key), color=_walk_color(walk),
                    weight=7, opacity=0.9, tooltip=tip,
                ).add_to(fmap)
        else:
            # Alternative routes: a single thin grey-ish line.
            coords = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in route.nodes]
            folium.PolyLine(
                coords, color=_ROUTE_COLORS[idx % len(_ROUTE_COLORS)],
                weight=3, opacity=0.5,
                tooltip=f"alternative #{idx}: walk={route.walk_score:.2f}",
            ).add_to(fmap)

    if orig is not None:
        folium.Marker(orig, icon=folium.Icon(color="green"), tooltip="origin").add_to(fmap)
    if dest is not None:
        folium.Marker(dest, icon=folium.Icon(color="red"), tooltip="destination").add_to(fmap)

    # Fit the camera only on a deliberate remount (frame=True). On click reruns
    # frame=False, so fit_bounds is never emitted and the user's view is kept.
    if frame:
        if routes:
            lats, lons = [], []
            for u, v, key in routes[0].edges:
                cs = _edge_coords(G, u, v, key)
                lats += [c[0] for c in cs]
                lons += [c[1] for c in cs]
            fmap.fit_bounds([(min(lats), min(lons)), (max(lats), max(lons))], padding=(30, 30))
        elif orig is not None and dest is not None:
            fmap.fit_bounds([orig, dest], padding=(40, 40))

    return fmap


# Nominatim, scoped to a generous Boston-area bounding box. Bounding the search
# narrows candidates (faster + more accurate) and the box is wide enough to cover
# Boston proper plus nearby Cambridge/Brookline/East Boston.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_BOSTON_VIEWBOX = "-71.20,42.43,-70.98,42.22"  # lon_min,lat_max,lon_max,lat_min
_GEO_HEADERS = {"User-Agent": "walkability-route-app/0.1 (educational project)"}


@st.cache_data(show_spinner=False)
def _nominatim_boston(q: str) -> tuple[float, float]:
    """Boston-bounded Nominatim lookup. Cached; raises on no result (so misses
    aren't cached and can be retried)."""
    import requests

    base = {"q": q, "format": "json", "limit": 1,
            "countrycodes": "us", "viewbox": _BOSTON_VIEWBOX}
    for bounded in (1, 0):  # strict box first, then the box only as a soft bias
        resp = requests.get(_NOMINATIM_URL, params={**base, "bounded": bounded},
                            headers=_GEO_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    raise ValueError(f"no geocoding result for {q!r}")


def geocode(query: str):
    """Geocode a free-text place to (lat, lon), scoped to Boston. None on failure."""
    q = query.strip()
    if not q:
        return None
    if "boston" not in q.lower() and "," not in q:
        q = f"{q}, Boston, Massachusetts, USA"
    try:
        return _nominatim_boston(q)
    except Exception:
        # Last-ditch fallback: osmnx's own (unbounded) Nominatim geocoder.
        try:
            import osmnx as ox
            lat, lon = ox.geocode(q)
            return (float(lat), float(lon))
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

st.session_state.setdefault("orig", None)
st.session_state.setdefault("dest", None)
st.session_state.setdefault("routes", [])
st.session_state.setdefault("last_click", None)
st.session_state.setdefault("searched", False)   # has a route search been run for the current points?
st.session_state.setdefault("view_token", 0)     # bumping it remounts the map → re-frames the view
st.session_state.setdefault("framed_token", -1)  # last view_token we actually fitted the camera for
st.session_state.setdefault("region", None)


def _reframe():
    """Force the map to remount and re-fit its view on the next render."""
    st.session_state.view_token += 1


def _run_search(G, o, d, alpha, weights):
    """Run routing for one O/D pair and update result state."""
    st.session_state.orig, st.session_state.dest = o, d
    st.session_state.routes = find_routes(G, o, d, alpha=alpha, weights=weights)
    st.session_state.searched = True
    _reframe()  # frame the new result once


def _clear_points():
    st.session_state.orig = st.session_state.dest = None
    st.session_state.routes = []
    st.session_state.last_click = None
    st.session_state.searched = False
    _reframe()


# ---------------------------------------------------------------------------
# Sidebar: graph + routing controls
# ---------------------------------------------------------------------------

st.sidebar.header("Graph")
_region_labels = {"full": "Full Boston (~10 s load)"}
_region_labels.update({r: f"Dev: {r} (fast)" for r in DEV_REGIONS})
region = st.sidebar.selectbox(
    "Area", list(_region_labels), format_func=_region_labels.get,
    help="Full Boston works for any address. Dev regions load instantly and are great for quick demos.",
)
graph_path = str(ENRICHED_PATH if region == "full" else dev_region_path(region))
if st.session_state.region != region:           # switching graphs: old points/view no longer apply
    st.session_state.region = region
    _clear_points()

st.sidebar.header("Routing")
alpha = st.sidebar.slider(
    "Walkability vs. distance (alpha)", 0.0, 6.0, float(ALPHA_DEFAULT), 0.5,
    help="0 = shortest path. Higher = detour further toward walkable streets.",
)

st.sidebar.subheader("Factor weights")
st.sidebar.caption("Relative importance of each signal in the walkability score.")
w_road = st.sidebar.slider("Road type", 0.0, 10.0, FACTOR_WEIGHTS["road_type"], 0.5)
w_surf = st.sidebar.slider("Surface quality", 0.0, 10.0, FACTOR_WEIGHTS["surface_quality"], 0.5)
w_mat = st.sidebar.slider("Surface material", 0.0, 10.0, FACTOR_WEIGHTS["surface_material"], 0.5)
w_foot = st.sidebar.slider("Foot access", 0.0, 10.0, FACTOR_WEIGHTS["foot_access"], 0.5)

_custom = {
    "road_type": w_road, "surface_quality": w_surf,
    "surface_material": w_mat, "foot_access": w_foot,
}
# Pass the FACTOR_WEIGHTS object itself when untouched, to keep the baked fast path.
weights = FACTOR_WEIGHTS if _custom == FACTOR_WEIGHTS else _custom


# ---------------------------------------------------------------------------
# Main: title + input
# ---------------------------------------------------------------------------

st.title("🚶 Boston Walkability Routes")
st.caption(
    "Routes are ranked by walkability. A single bad block drags the whole route's "
    "score down (worst-segment penalty), and a forced customers-only endpoint "
    "(e.g. a zoo entrance) isn't penalised."
)

G = get_graph(graph_path)

mode = st.radio(
    "Set origin & destination by", ["Address", "Latitude / Longitude", "Click on map"],
    horizontal=True,
)

# Process a pending map click BEFORE the buttons are drawn, so "Find routes"
# reflects the point just placed (otherwise the button lags a click behind).
# st_folium stores its return in session_state under its key, so the latest
# click is already available here at the top of the click-triggered rerun.
map_key = f"route_map_{st.session_state.view_token}"
if mode == "Click on map":
    lc = (st.session_state.get(map_key) or {}).get("last_clicked")
    if lc:
        pt = (lc["lat"], lc["lng"])
        if pt != st.session_state.last_click:          # a genuinely new click
            st.session_state.last_click = pt
            if st.session_state.orig is None:
                st.session_state.orig = pt
                st.session_state.routes = []
                st.session_state.searched = False
            elif st.session_state.dest is None:
                st.session_state.dest = pt
                st.session_state.routes = []
                st.session_state.searched = False
            # both already set → ignore further clicks (use "Clear points")

if mode == "Address":
    c1, c2 = st.columns(2)
    o_addr = c1.text_input("Origin address", placeholder="e.g. Massachusetts State House")
    d_addr = c2.text_input("Destination address", placeholder="e.g. Charles/MGH Station")
    if st.button("Find routes", type="primary"):
        o = geocode(o_addr)
        d = geocode(d_addr)
        if o is None:
            st.error(f"Couldn't geocode origin: “{o_addr}”. Try a more specific address.")
        elif d is None:
            st.error(f"Couldn't geocode destination: “{d_addr}”. Try a more specific address.")
        else:
            _run_search(G, o, d, alpha, weights)

elif mode == "Latitude / Longitude":
    c1, c2 = st.columns(2)
    o_lat = c1.number_input("Origin lat", value=42.3588, format="%.6f")
    o_lon = c1.number_input("Origin lon", value=-71.0707, format="%.6f")
    d_lat = c2.number_input("Destination lat", value=42.3601, format="%.6f")
    d_lon = c2.number_input("Destination lon", value=-71.0656, format="%.6f")
    if st.button("Find routes", type="primary"):
        _run_search(G, (o_lat, o_lon), (d_lat, d_lon), alpha, weights)

else:  # Click on map
    st.info(
        "Click the map: first click sets the **origin**, second the **destination**. "
        "Then press **Find routes**. Use **Clear points** to start over."
    )
    cc1, cc2, _ = st.columns([1, 1, 2])
    if cc1.button("Find routes", type="primary",
                  disabled=not (st.session_state.orig and st.session_state.dest)):
        _run_search(G, st.session_state.orig, st.session_state.dest, alpha, weights)
    if cc2.button("Clear points"):
        _clear_points()


# ---------------------------------------------------------------------------
# Results summary
# ---------------------------------------------------------------------------

routes = st.session_state.routes
if routes:
    best = routes[0]
    m1, m2, m3 = st.columns(3)
    m1.metric("Best walk score", f"{best.walk_score:.2f}")
    m2.metric("Length", f"{best.total_length:.0f} m")
    m3.metric("Confidence", f"{best.confidence:.2f}")
    st.dataframe(
        [
            {
                "rank": i,
                "walk_score": round(r.walk_score, 3),
                "confidence": round(r.confidence, 3),
                "length_m": round(r.total_length),
                "hops": len(r.edges),
            }
            for i, r in enumerate(routes)
        ],
        hide_index=True, width="stretch",
    )
elif st.session_state.searched and st.session_state.orig and st.session_state.dest:
    st.warning(
        "No walkable route found between these points. Some destinations are only "
        "reachable via foot-prohibited (`foot=no`) edges and correctly return no route."
    )


# ---------------------------------------------------------------------------
# Map (renders markers + routes; captures clicks in click mode)
# ---------------------------------------------------------------------------

# The map key only changes when we deliberately re-frame (after a search / clear
# / region switch). While it's stable, st_folium keeps the user's pan/zoom and
# does NOT rerun on pan/zoom — only a click changes `last_clicked` and reruns,
# which is why limiting `returned_objects` to last_clicked stops the page from
# jumping to the top every time you scroll-zoom. We fit the camera (frame=True)
# only on the first render after such a re-frame, never on a click.
frame = st.session_state.framed_token != st.session_state.view_token
st.session_state.framed_token = st.session_state.view_token

fmap = build_map(G, st.session_state.orig, st.session_state.dest, routes, weights, frame=frame)
st_folium(fmap, key=map_key, height=560, use_container_width=True,
          returned_objects=["last_clicked"])
