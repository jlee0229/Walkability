"""
Humanpath — walkability-aware walking routes for Boston (Streamlit UI).

Run with:
    streamlit run app/streamlit_app.py

Enter an origin and destination by address and choose how far you'll go for a
better walk (the `alpha` slider). Routes are scored block by block and ranked;
each route card can be expanded for the specifics (confidence, weakest stretch).

Design: a warm editorial look (parchment + terracotta) with a left control rail
and a full-height map. The graph loads once per session (`@st.cache_resource`).
"""

from __future__ import annotations

import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import folium
import numpy as np
import streamlit.components.v1 as components
from streamlit_folium import st_folium

from walkability.graph.build import (
    DEV_REGIONS,
    ENRICHED_PATH,
    dev_region_path,
    load_graph,
)
from walkability.graph.compact import load_runtime, runtime_path
from walkability.routing.router import find_routes
from walkability.scoring.factors import _as_float, _as_str, edge_walkability
from walkability.scoring.weights import FACTOR_WEIGHTS

_ICON_PATH = str(Path(__file__).parent / "humanpath_icon.png")
st.set_page_config(page_title="Humanpath", page_icon=_ICON_PATH, layout="wide")

# Palette (mirrors the Humanpath design direction).
ACCENT = "#b1592e"
INK = "#211e18"
WALK_SPEED_MPS = 1.33  # ~average pedestrian pace, for walk-time estimates

# Map backend flag (B2 rollout): "folium" (default, the shipped st_folium raster
# map) or "maplibre" (the in-progress GPU vector spike — set HUMANPATH_MAP=maplibre).
# Keeps the working folium map as a fallback while MapLibre is de-risked/built.
_MAP_BACKEND = os.environ.get("HUMANPATH_MAP", "folium").strip().lower()


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

def inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

        :root { --ink:#211e18; --muted:#5c564a; --faint:#8a8270; --line:#ece5d5; --accent:#b1592e; }

        /* Base type + background */
        html, body, [class*="css"], [data-testid="stAppViewContainer"] {
            font-family: 'Public Sans', system-ui, sans-serif;
            color: var(--ink);
        }
        [data-testid="stAppViewContainer"] { background: #faf8f2; }
        h1, h2, h3 { font-family: 'Spectral', Georgia, serif; letter-spacing: -0.01em; }

        /* Hide default Streamlit chrome for a cleaner app shell (but KEEP the
           sidebar collapse/expand control so the rail can be reopened). */
        /* Transparent header, but let clicks pass THROUGH it: it's an invisible
           bar over the top of the page that otherwise eats pointer events on the
           first content row (e.g. the top half of the "Fit route" button). The
           toolbar/menu it would host are hidden, so nothing in it needs clicks. */
        header[data-testid="stHeader"] { background: transparent; pointer-events: none; }
        #MainMenu, footer { visibility: hidden; }
        [data-testid="stToolbar"] { display: none; }
        [data-testid="stMainBlockContainer"] { padding-top: 1.0rem; }

        /* Left rail — fixed width and non-collapsible: hide the resize grip AND
           the collapse/expand control so the horizontal dimensions never change. */
        section[data-testid="stSidebar"] { width: 446px !important; min-width: 446px !important; background: #f6f1e6; border-right: 1px solid var(--line); }
        [data-testid="stSidebarResizeHandle"], [data-testid="stSidebarResizer"] { display: none !important; }
        [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"] { display: none !important; }
        /* The sidebar header reserved space for the (now hidden) collapse button —
           remove it and trim the content padding so the rail starts at the top. */
        [data-testid="stSidebarHeader"] { display: none !important; height: 0 !important; padding: 0 !important; }
        section[data-testid="stSidebar"] > div { padding-top: 0.4rem; }
        [data-testid="stSidebarUserContent"] { padding-top: 0 !important; }
        /* Right-align the mi/km units toggle to the rail's right edge */
        section[data-testid="stSidebar"] [data-testid="stSegmentedControl"] { justify-content: flex-end; }

        /* Mono labels */
        .fp-eyebrow { font-family:'IBM Plex Mono',monospace; font-size:11px; text-transform:uppercase; letter-spacing:0.18em; color:#a8a08c; display:flex; align-items:center; gap:9px; margin-bottom:12px; }
        .fp-eyebrow span.rule { display:inline-block; width:18px; height:1px; background:#cabfa6; }
        .fp-title { font-family:'Spectral',serif; font-weight:600; font-size:40px; line-height:1; margin:0 0 12px; }
        .fp-desc { font-size:14px; line-height:1.6; color:var(--muted); max-width:36ch; margin:0 0 6px; }
        .fp-mono { font-family:'IBM Plex Mono',monospace; font-size:10.5px; text-transform:uppercase; letter-spacing:0.14em; color:#a8a08c; }

        /* Text inputs */
        [data-testid="stTextInput"] input {
            font-family:'Public Sans',sans-serif; font-size:14.5px; border-radius:11px;
            border:1px solid #e6dfce; background:#fff; padding:11px 13px;
        }
        [data-testid="stTextInput"] input:focus { border-color: var(--accent); box-shadow:none; }

        /* Primary action button (Find / Update routes) */
        .stButton > button {
            width:100%; border:none; border-radius:13px; background:var(--accent); color:#fdfbf6;
            font-family:'Public Sans',sans-serif; font-weight:600; font-size:15px; padding:13px 15px;
            box-shadow:0 4px 14px rgba(177,89,46,.32); transition:filter .15s;
        }
        .stButton > button:hover { filter:brightness(1.05); color:#fff; }
        .stButton > button:focus { color:#fff; }

        /* "Fit route" map control — quiet outlined secondary, not the big terracotta */
        .st-key-fit_route_btn > button,
        .st-key-fit_route_btn button {
            background:#fffdf8; color:var(--accent); border:1px solid #e0c4b2;
            box-shadow:none; font-size:12px; font-weight:600; padding:6px 10px; border-radius:9px;
        }
        .st-key-fit_route_btn button:hover { filter:none; background:#f7e9e0; color:var(--accent); }
        .st-key-fit_route_btn button:focus { color:var(--accent); }

        /* Expander panels (route Details, Map area) — quiet, recessed */
        [data-testid="stExpander"] { border:1px solid #e6dfce; border-radius:13px; background:#fffdf8; }
        [data-testid="stExpander"] summary { font-weight:600; font-size:13.5px; color:var(--muted); }

        /* Route cards */
        .fp-card { border:1px solid #e6dfce; background:#fffdf8; border-radius:16px; padding:16px 17px 15px; margin-bottom:2px; }
        .fp-card.best { border-color:var(--accent); background:#fdf3ec; }
        .fp-card-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:13px; }
        .fp-card-name { font-family:'Spectral',serif; font-size:18px; font-weight:600; line-height:1.15; }
        .fp-card-via { font-size:12.5px; color:var(--faint); margin-top:2px; }
        .fp-badge { flex-shrink:0; font-family:'IBM Plex Mono',monospace; font-size:9px; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:var(--accent); background:#f6e3d8; padding:5px 9px; border-radius:8px; white-space:nowrap; }
        .fp-score-row { display:flex; align-items:flex-end; gap:8px; margin-bottom:9px; }
        .fp-score { font-family:'Spectral',serif; font-size:38px; font-weight:600; line-height:0.82; }
        .fp-score-100 { font-size:13px; color:#a8a08c; margin-bottom:3px; }
        .fp-score-tag { font-size:10.5px; color:var(--faint); margin-bottom:4px; margin-left:auto; text-transform:uppercase; letter-spacing:0.05em; font-family:'IBM Plex Mono',monospace; }
        .fp-bar { height:6px; border-radius:999px; background:#ece5d5; overflow:hidden; margin-bottom:14px; }
        .fp-bar-fill { height:100%; border-radius:999px; }
        .fp-meta { display:flex; gap:24px; }
        .fp-meta b { font-family:'IBM Plex Mono',monospace; font-size:15px; font-weight:500; color:#2b271f; }
        .fp-meta .lbl { font-size:10.5px; color:#a8a08c; text-transform:uppercase; letter-spacing:0.06em; margin-top:1px; }
        .fp-hair { height:1px; background:var(--line); margin:10px 0 18px; }

        /* The map fills the main area; don't let it spawn a page scrollbar */
        [data-testid="stMain"] { overflow: hidden; }
        [data-testid="stMain"]::-webkit-scrollbar { width:0; height:0; }
        .fp-card:not(.best) { cursor: default; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Data + geometry helpers
# ---------------------------------------------------------------------------

# Graph files are too large for the repo (the enriched graph is ~122 MB), so a
# deployed instance fetches them once from a GitHub Release on first use.
_GRAPH_RELEASE = "https://github.com/jlee0229/Walkability/releases/download/data-v1"


def _download_release_asset(p: Path) -> bool:
    """Stream a release asset into ``p`` (atomic). True on success, False on 404."""
    import requests

    p.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(f"{_GRAPH_RELEASE}/{p.name}", stream=True, timeout=120) as r:
        if r.status_code == 404:
            return False
        r.raise_for_status()
        tmp = p.with_suffix(p.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.replace(p)  # atomic: only a complete download becomes the real file
    return True


@st.cache_resource(show_spinner="Loading the walk graph (one-time)…")
def get_graph(path_str: str):
    """Load the slim runtime pickle (≈0.5 s, ≈0.45 GB) for an enriched GraphML path.

    Prefers the ``.runtime.pkl`` sibling everywhere: locally it already exists,
    and on a fresh deploy it is downloaded from the GitHub Release (40 MB vs the
    178 MB GraphML). Falls back to the full GraphML only if the pickle is absent
    both locally and in the release (e.g. an old release without the asset).
    """
    graphml = Path(path_str)
    rt = runtime_path(graphml)

    if rt.exists():
        return load_runtime(rt)

    # Not local → fetch the runtime pickle from the release (preferred, small).
    with st.spinner(f"Downloading map data ({rt.name}) — first run only…"):
        if _download_release_asset(rt):
            return load_runtime(rt)

    # Last resort: the heavyweight GraphML (older release without the pickle).
    if not graphml.exists():
        with st.spinner(f"Downloading map data ({graphml.name}) — first run only…"):
            _download_release_asset(graphml)
    return load_graph(graphml)


def _graph_center(G):
    cached = G.graph.get("_center")
    if cached is None:
        ys = [d["y"] for _, d in G.nodes(data=True)]
        xs = [d["x"] for _, d in G.nodes(data=True)]
        cached = (sum(ys) / len(ys), sum(xs) / len(xs))
        G.graph["_center"] = cached
    return cached


def _edge_coords(G, u, v, key):
    geom = G[u][v][key].get("geometry")
    if geom is not None:
        # Runtime pickle packs geometry as a float32 (n, 2) ndarray (lon, lat);
        # the enriched GraphML carries a shapely LineString. Both iterate as
        # (lon, lat) pairs, flipped here to folium's (lat, lon).
        # float(...) so packed float32 becomes plain float (folium/JSON-safe).
        coords = geom if isinstance(geom, np.ndarray) else geom.coords
        return [(float(lat), float(lon)) for lon, lat in coords]
    return [(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])]


# Nominatim, scoped to a Boston-area bounding box; cached so repeats are instant.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_BOSTON_VIEWBOX = "-71.20,42.43,-70.98,42.22"
_GEO_HEADERS = {"User-Agent": "walkability-route-app/0.1 (educational project)"}


@st.cache_data(show_spinner=False)
def _nominatim_boston(q: str):
    import requests

    base = {"q": q, "format": "json", "limit": 1, "countrycodes": "us", "viewbox": _BOSTON_VIEWBOX}
    for bounded in (1, 0):
        resp = requests.get(_NOMINATIM_URL, params={**base, "bounded": bounded},
                            headers=_GEO_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    raise ValueError(f"no geocoding result for {q!r}")


def geocode(query: str):
    q = query.strip()
    if not q:
        return None
    if "boston" not in q.lower() and "," not in q:
        q = f"{q}, Boston, Massachusetts, USA"
    try:
        return _nominatim_boston(q)
    except Exception:
        try:
            import osmnx as ox
            lat, lon = ox.geocode(q)
            return (float(lat), float(lon))
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def score_hex(s01: float) -> str:
    """Red → amber → green hex for a walk score in [0, 1]."""
    s = s01 * 100
    if s >= 80:
        return "#3f8f5f"
    if s >= 65:
        return "#789b3e"
    if s >= 50:
        return "#c8922f"
    return "#c0512f"


def dist_str(m: float, unit: str = "mi") -> str:
    """Format a distance in metres as miles (default, US) or km."""
    if unit == "km":
        return f"{m / 1000:.1f} km" if m >= 1000 else f"{round(m / 10) * 10:.0f} m"
    mi = m / 1609.34
    if mi < 0.1:  # short hops read better in feet
        return f"{round(m * 3.28084 / 10) * 10:.0f} ft"
    return f"{mi:.2f} mi"


def time_str(m: float) -> str:
    return f"{max(1, round(m / WALK_SPEED_MPS / 60))} min"


def alpha_word(slider: int) -> str:
    return ("Shortest path" if slider < 15 else "Lean shorter" if slider < 35
            else "Balanced" if slider < 58 else "Lean walkable" if slider < 82 else "Best walk")


def route_details(G, route, weights):
    """Weakest block (lowest-scoring edge), how far into the route it starts, and
    the route's dominant street name."""
    worst_walk, worst_dist = 1.0, 0.0
    cum = 0.0
    street_len: dict[str, float] = defaultdict(float)
    for u, v, key in route.edges:
        d = G[u][v][key]
        length = float(d.get("length") or 0.0)
        w, _ = edge_walkability(d, weights)
        if w < worst_walk:
            worst_walk, worst_dist = w, cum  # distance from start to the weakest block
        cum += length
        name = d.get("name")
        if isinstance(name, list):
            name = name[0] if name else None
        name = _as_str(name)
        if name:
            street_len[name] += length
    dominant = max(street_len, key=street_len.get) if street_len else None
    return worst_walk, worst_dist, dominant


# Widget callbacks (run before the rerun's script body, so state is consistent).
def _set_focus(i: int) -> None:
    st.session_state.focus = i


def _toggle(key: str) -> None:
    st.session_state[key] = not st.session_state.get(key, False)


def _recenter() -> None:
    # Bump a nonce so the camera value we pass to st_folium *changes* on the next
    # run, forcing a setView back onto the route. Needed because st_folium only
    # moves the camera when center/zoom differ from the last value we passed, and
    # a manual pan doesn't update that last value — so re-passing the same frame
    # would be a no-op. The nonce becomes an imperceptible jitter (see call site).
    st.session_state.recenter_nonce += 1


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

st.session_state.setdefault("routes", [])
st.session_state.setdefault("focus", 0)            # index of route emphasised on the map
st.session_state.setdefault("committed", None)     # params behind the shown routes
st.session_state.setdefault("active_weights", FACTOR_WEIGHTS)  # weights the shown routes/colours use
st.session_state.setdefault("region", None)
st.session_state.setdefault("error", None)
st.session_state.setdefault("recenter_nonce", 0)  # bumped by the "Fit route" button

inject_css()


# ---------------------------------------------------------------------------
# Left rail
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(
        '<div style="display:flex;align-items:center;gap:13px;margin-bottom:12px;">'
        '  <svg width="42" height="42" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0;">'
        '    <path d="M33 67 C 60 56, 40 44, 67 33" fill="none" stroke="#211e18" stroke-width="13" stroke-linecap="round"/>'
        '    <circle cx="27" cy="73" r="15" fill="#b1592e"/>'
        '    <circle cx="73" cy="27" r="15" fill="#b1592e"/>'
        '  </svg>'
        '  <div class="fp-title" style="margin:0;">Humanpath</div>'
        '</div>'
        '<p class="fp-desc">Walking routes scored block by block on street type, surface, and foot access.'
        ' Not just the shortest line. Choose how far you’ll go for a better walk.</p>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="fp-hair"></div>', unsafe_allow_html=True)

    st.markdown('<div class="fp-mono">Trip</div>', unsafe_allow_html=True)
    o_addr = st.text_input("From", value="Massachusetts State House", label_visibility="collapsed",
                           placeholder="From — e.g. Massachusetts State House")
    d_addr = st.text_input("To", value="Boston Public Garden", label_visibility="collapsed",
                           placeholder="To — e.g. Boston Public Garden")

    st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    c1.markdown('<div class="fp-mono">How you\'ll walk</div>', unsafe_allow_html=True)
    alpha_slider = st.slider("How you'll walk", 0, 100, 40, label_visibility="collapsed",
                             help="Left = shortest route. Right = detour further for a better walk.")
    c2.markdown(
        f'<div style="text-align:right; font-family:Spectral,serif; font-style:italic; '
        f'font-size:16px; color:{ACCENT};">{alpha_word(alpha_slider)}</div>',
        unsafe_allow_html=True,
    )
    sc1, sc2 = st.columns([1, 1])
    sc1.markdown('<span style="font-size:11.5px;color:#a8a08c;">Shortest way</span>', unsafe_allow_html=True)
    sc2.markdown('<div style="text-align:right;"><span style="font-size:11.5px;color:#a8a08c;">Best walk</span></div>', unsafe_allow_html=True)

    alpha = round(alpha_slider / 100 * 5, 2)  # 0 → shortest path; ~5 → strong walkability pull

    st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)
    uc1, uc2 = st.columns([1, 1])
    uc1.markdown('<div class="fp-mono" style="padding-top:6px;">Distance units</div>', unsafe_allow_html=True)
    with uc2:
        unit = st.segmented_control("Distance units", ["mi", "km"], default="mi",
                                    label_visibility="collapsed", key="units") or "mi"

    # Per-factor weight sliders were removed: the score is now a two-level
    # HDI-style category aggregate (scoring/factors.py), so flat per-factor
    # weights no longer map cleanly onto what the user sees. The default
    # FACTOR_WEIGHTS object is always used, which also keeps the baked
    # walk_score fast path.
    weights = FACTOR_WEIGHTS

    params = {"o": o_addr.strip(), "d": d_addr.strip(), "alpha": alpha}
    pending = st.session_state.committed is not None and params != st.session_state.committed
    # Render the nudge into a placeholder *above* the button, but fill it only after
    # we know whether the button was clicked — so it vanishes the moment Update is hit.
    nudge = st.empty()
    find = st.button("Update routes" if pending else "Find routes", type="primary")
    if pending and not find:
        nudge.markdown(
            '<div style="display:flex;align-items:center;gap:9px;margin:6px 0 10px;padding:10px 13px;'
            'border-radius:11px;background:#f7e9e0;border:1px solid #e7c9b6;">'
            '<div style="width:6px;height:6px;border-radius:50%;background:#b1592e;"></div>'
            '<span style="font-size:12.5px;color:#5c564a;">Settings changed — update to recompute.</span></div>',
            unsafe_allow_html=True,
        )

# Region selector lives at the bottom of the rail (rendered later); read its
# committed value here via the widget key so the graph can load first.
region = st.session_state.get("region_select", "full")
graph_path = str(ENRICHED_PATH if region == "full" else dev_region_path(region))
if st.session_state.region != region:
    st.session_state.region = region
    st.session_state.routes = []
    st.session_state.committed = None
    st.session_state.error = None

G = get_graph(graph_path)


# ---------------------------------------------------------------------------
# Run a search
# ---------------------------------------------------------------------------

if find:
    st.session_state.error = None
    with st.spinner("Reading the streets…"):
        o = geocode(o_addr)
        d = geocode(d_addr)
        routes_found = None if (o is None or d is None) else find_routes(G, o, d, alpha=alpha, weights=weights)
    if o is None:
        st.session_state.error = f"Couldn't find “{o_addr}”. Try a more specific address."
    elif d is None:
        st.session_state.error = f"Couldn't find “{d_addr}”. Try a more specific address."
    else:
        st.session_state.routes = routes_found
        st.session_state.committed = params
        st.session_state.active_weights = weights  # freeze rendering to the committed weights
        st.session_state.focus = 0

routes = st.session_state.routes


# ---------------------------------------------------------------------------
# Route cards (left rail, below controls)
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown('<div class="fp-hair" style="margin:22px 0 0;"></div>', unsafe_allow_html=True)
    head_l, head_r = st.columns([2, 1])
    head_l.markdown('<h2 style="font-size:23px;font-weight:600;margin:14px 0 2px;">Your routes</h2>',
                    unsafe_allow_html=True)
    head_r.markdown(
        f'<div style="text-align:right;font-family:IBM Plex Mono,monospace;font-size:11px;'
        f'color:#a8a08c;margin-top:20px;">{len(routes)} found</div>', unsafe_allow_html=True)

    if st.session_state.error:
        st.warning(st.session_state.error)
    elif not routes:
        st.markdown('<p style="font-size:13px;color:#8a8270;">Enter a trip and press '
                    '<b>Find routes</b>.</p>', unsafe_allow_html=True)
    else:
        best_score = max(r.walk_score for r in routes)
        shortest = min(r.total_length for r in routes)
        st.markdown(
            f'<p style="margin:2px 0 16px;font-size:12.5px;color:#8a8270;line-height:1.5;">'
            f'Best walk scores {round(best_score * 100)}/100 · shortest is {dist_str(shortest, unit)}. '
            f'Sorted by your priorities.</p>', unsafe_allow_html=True)

        rweights = st.session_state.active_weights  # render with committed weights, not live sliders
        st.session_state.focus = min(st.session_state.focus, len(routes) - 1)
        details = [route_details(G, r, rweights) for r in routes]

        for i, (r, (worst_walk, worst_dist, dominant)) in enumerate(zip(routes, details)):
            sc = r.walk_score
            col = score_hex(sc)
            via = f"via {dominant}" if dominant else f"{len(r.edges)} blocks"
            badge = '<span class="fp-badge">Best fit</span>' if i == 0 else ''
            focused = " best" if i == st.session_state.focus else ""
            st.markdown(
                f'<div class="fp-card{focused}">'
                f'  <div class="fp-card-head"><div style="min-width:0;">'
                f'    <div class="fp-card-name">{"Recommended" if i == 0 else f"Alternative {i}"}</div>'
                f'    <div class="fp-card-via">{via}</div></div>{badge}</div>'
                f'  <div class="fp-score-row"><span class="fp-score" style="color:{col};">{round(sc*100)}</span>'
                f'    <span class="fp-score-100">/ 100</span><span class="fp-score-tag">Walk score</span></div>'
                f'  <div class="fp-bar"><div class="fp-bar-fill" style="width:{max(4, round(sc*100))}%;background:{col};"></div></div>'
                f'  <div class="fp-meta">'
                f'    <div><b>{dist_str(r.total_length, unit)}</b><div class="lbl">Distance</div></div>'
                f'    <div><b>{time_str(r.total_length)}</b><div class="lbl">Walk time</div></div>'
                f'  </div></div>',
                unsafe_allow_html=True,
            )
            # Selecting a route just sets focus (via callback, no st.rerun) — all
            # routes are already drawn on the map, so this only re-emphasises.
            if i != st.session_state.focus:
                st.button("Show on map", key=f"focus_{i}", use_container_width=True,
                          on_click=_set_focus, args=(i,))
            else:
                st.markdown(
                    '<div style="font-family:IBM Plex Mono,monospace;font-size:10.5px;'
                    'letter-spacing:0.1em;text-transform:uppercase;color:#b1592e;'
                    'padding:4px 0 2px;">● Showing on map</div>', unsafe_allow_html=True)
            with st.expander("Details"):
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;font-size:12.5px;'
                    f'color:#5c564a;padding:3px 0;">'
                    f'<span>Confidence in this scoring</span>'
                    f'<b style="font-family:IBM Plex Mono,monospace;color:#2b271f;">{round(r.confidence*100)} / 100</b></div>'
                    f'<div style="display:flex;justify-content:space-between;font-size:12.5px;'
                    f'color:#5c564a;padding:3px 0;">'
                    f'<span>Weakest stretch — {dist_str(worst_dist, unit)} in</span>'
                    f'<b style="font-family:IBM Plex Mono,monospace;color:{score_hex(worst_walk)};">{round(worst_walk*100)} / 100</b></div>',
                    unsafe_allow_html=True,
                )
                seg_key = f"seg_{i}"
                seg_open = st.session_state.get(seg_key, False)
                st.button("Hide segments" if seg_open else f"Show {len(r.edges)} segments",
                          key=f"segbtn_{i}", on_click=_toggle, args=(seg_key,))
                if seg_open:
                    rows = []
                    for j, (u, v, ekey) in enumerate(r.edges):
                        d = G[u][v][ekey]
                        w, _ = edge_walkability(d, rweights)
                        hwy = _as_str(d.get("highway")) or "path"
                        length = _as_float(d.get("length")) or 0.0
                        rows.append(
                            f'<div style="display:flex;justify-content:space-between;gap:8px;padding:1px 0;">'
                            f'<span style="color:#8a8270;">{j + 1}. {hwy}</span>'
                            f'<span style="color:{score_hex(w)};">{round(w * 100)}/100 · {dist_str(length, unit)}</span></div>')
                    st.markdown(
                        '<div style="font-family:IBM Plex Mono,monospace;font-size:10.5px;'
                        'line-height:1.7;max-height:220px;overflow:auto;margin-top:4px;'
                        'border-top:1px solid #ece5d5;padding-top:6px;">' + "".join(rows) + "</div>",
                        unsafe_allow_html=True,
                    )


# Region selector — tucked at the very bottom of the rail (will grow once we add
# more areas). Its value is read at the top of the next run via the widget key.
with st.sidebar:
    st.markdown('<div class="fp-hair" style="margin:24px 0 8px;"></div>', unsafe_allow_html=True)
    with st.expander("Map area"):
        _region_labels = {"full": "Full Boston"}
        _region_labels.update({r: r.replace("_", " ").title() for r in DEV_REGIONS})
        st.selectbox("Area", list(_region_labels), format_func=_region_labels.get,
                     label_visibility="collapsed", key="region_select")


# ---------------------------------------------------------------------------
# Map (main area)
# ---------------------------------------------------------------------------

# Camera fit geometry. The base map is rendered once and never re-rendered (so
# the iframe never remounts / reloads tiles); the camera is moved by passing
# `center`/`zoom` to st_folium, which dynamically `setView`s the live map only
# when the value changes (see the call site). st_folium has no animated flyTo,
# so we compute an explicit (center, zoom) that fits the focused route's bbox —
# the Web-Mercator `getBoundsZoom` math Leaflet's fitBounds uses internally.
_MAP_PX_H = 660       # matches the st_folium height
_MAP_PX_W = 760       # conservative width estimate (container width is unknown
                      # server-side); erring narrow zooms out a touch so a wide
                      # route is never clipped left/right
_MAP_PAD_PX = 48      # breathing room around the route, like fitBounds padding
_MAP_ZOOM_MAX = 17.0  # don't zoom past street level on a very short walk
_BASE_ZOOM = 13.0     # initial base-map zoom before the first route fit


def _lat_rad(lat: float) -> float:
    s = math.sin(math.radians(lat))
    return max(min(math.log((1 + s) / (1 - s)) / 2, math.pi), -math.pi) / 2


def _bounds_to_view(min_lat, min_lon, max_lat, max_lon):
    """(center, zoom) that fits a lat/lon bbox in the map viewport with padding.

    Mirrors Leaflet/Google `getBoundsZoom`: the largest zoom at which the bbox
    still fits inside (viewport − padding). Fractional zoom is fine (the base
    map uses zoom_snap=0).
    """
    def _z(px, fraction):
        return math.log(max(px, 1) / 256.0 / fraction) / math.log(2) if fraction > 0 else _MAP_ZOOM_MAX
    lat_fraction = (_lat_rad(max_lat) - _lat_rad(min_lat)) / math.pi
    lng_fraction = ((max_lon - min_lon) % 360) / 360.0
    zoom = min(_z(_MAP_PX_H - 2 * _MAP_PAD_PX, lat_fraction),
               _z(_MAP_PX_W - 2 * _MAP_PAD_PX, lng_fraction),
               _MAP_ZOOM_MAX)
    return ((min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0), round(zoom, 2)


def build_base_map(G):
    """The tiles-only base map, rendered ONCE.

    It carries no routes and a constant centre/zoom so its generated Leaflet JS
    is stable across reruns — st_folium hashes that JS, so a stable hash means
    the component (iframe) is never remounted: no white flash, no tile reload.
    Routes ride in a FeatureGroup and the camera moves via center/zoom props.
    Native wheel zoom (zoom_snap=0 fractional, brisk wheel step); the
    SmoothWheelZoom plugin doesn't execute inside st_folium's iframe.
    """
    fmap = folium.Map(location=_graph_center(G), zoom_start=_BASE_ZOOM, tiles=None,
                      zoom_control=True, zoom_snap=0, wheel_px_per_zoom_level=40)
    folium.TileLayer(
        "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png",
        attr="© OpenStreetMap, © CARTO", subdomains="abcd", max_zoom=20, control=False,
    ).add_to(fmap)
    folium.TileLayer(
        "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png",
        attr="© CARTO", subdomains="abcd", max_zoom=20, control=False,
    ).add_to(fmap)
    return fmap


def build_route_layer(G, routes, focus, weights, segmented):
    """All routes + O/D markers as a single FeatureGroup for dynamic swapping.

    Passed to st_folium via `feature_group_to_add`, which replaces just this
    layer on the persistent base map (no rebuild). Alternatives are drawn first
    (faint), the focused route last and on top — a single smooth line, or
    per-block coloured pieces when `segmented`.
    """
    fg = folium.FeatureGroup(name="routes")
    if not routes:
        return fg

    order = [i for i in range(len(routes)) if i != focus] + [focus]
    for i in order:
        r = routes[i]
        if i != focus:
            coords = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in r.nodes]
            folium.PolyLine(coords, color=score_hex(r.walk_score), weight=4,
                            opacity=0.4, line_cap="round").add_to(fg)
        else:
            full = []
            for u, v, key in r.edges:
                full += _edge_coords(G, u, v, key)
            folium.PolyLine(full, color="#faf8f2", weight=10, opacity=1,
                            line_cap="round", line_join="round").add_to(fg)  # halo
            if segmented:
                for u, v, key in r.edges:
                    cs = _edge_coords(G, u, v, key)
                    w, _ = edge_walkability(G[u][v][key], weights)
                    folium.PolyLine(
                        cs, color=score_hex(w), weight=6, opacity=1, line_cap="round",
                        tooltip=f"walk {round(w*100)}/100 · {_as_str(G[u][v][key].get('highway')) or 'path'}",
                    ).add_to(fg)
            else:
                folium.PolyLine(
                    full, color=score_hex(r.walk_score), weight=6, opacity=1,
                    line_cap="round", line_join="round",
                    tooltip=f"Walk score {round(r.walk_score*100)}/100",
                ).add_to(fg)

    focal = routes[focus]
    o = (G.nodes[focal.nodes[0]]["y"], G.nodes[focal.nodes[0]]["x"])
    d = (G.nodes[focal.nodes[-1]]["y"], G.nodes[focal.nodes[-1]]["x"])
    folium.CircleMarker(o, radius=7, color="#faf8f2", weight=3, fill_color=ACCENT,
                        fill_opacity=1, tooltip="Start").add_to(fg)
    folium.CircleMarker(d, radius=7, color="#faf8f2", weight=3, fill_color=INK,
                        fill_opacity=1, tooltip="Destination").add_to(fg)
    return fg


def camera_view(G, routes, focus):
    """(center, zoom) to frame the focused route, or the city default if none.

    Returned to the call site and handed to st_folium as `center`/`zoom`. It is
    a pure function of the focused route, so it stays constant across reruns that
    don't change the route — st_folium then leaves the camera (and any manual
    pan/zoom) untouched — and changes only on a new search or a focus switch,
    when st_folium `setView`s to the new frame.
    """
    if not routes:
        return _graph_center(G), _BASE_ZOOM
    fpts = []
    for u, v, key in routes[focus].edges:
        fpts += _edge_coords(G, u, v, key)
    if not fpts:
        return _graph_center(G), _BASE_ZOOM
    lats = [p[0] for p in fpts]
    lons = [p[1] for p in fpts]
    return _bounds_to_view(min(lats), min(lons), max(lats), max(lons))


# ---------------------------------------------------------------------------
# MapLibre GL component (B2) — used only when HUMANPATH_MAP=maplibre. A build-less
# static Streamlit component (app/components/maplibre_map/frontend/) served from a
# REAL origin via declare_component, so external tiles load and lines render —
# unlike the earlier components.html `srcdoc` spike, whose null origin CORS-blocked
# tiles and broke line rendering. Python passes a route GeoJSON + a camera target;
# main.js keeps a PERSISTENT map and updates layers/camera per rerun (no remount).
# Basemap: OpenFreeMap for now (HUMANPATH_STYLE to switch); production = self-hosted
# Protomaps PMTiles (B2.1b). MapLibre is CDN-loaded for now; vendor for production.
_MAPLIBRE_BASEMAP = {
    "demotiles": "https://demotiles.maplibre.org/style.json",
    "openfreemap": "https://tiles.openfreemap.org/styles/positron",
}.get(os.environ.get("HUMANPATH_STYLE", "openfreemap").strip().lower(),
      "https://tiles.openfreemap.org/styles/positron")

_MAPLIBRE_COMPONENT = components.declare_component(
    "humanpath_maplibre",
    path=str(Path(__file__).parent / "components" / "maplibre_map" / "frontend"),
)


def _route_geojson(G, routes, focus):
    """(FeatureCollection, focus_bounds) of route LineStrings in GeoJSON [lon,lat].

    Alternatives get ``role="alt"``, the focused route ``role="focused"``; colour
    from score_hex. bounds is MapLibre's [[w,s],[e,n]] for the focused route (or
    None when there are no routes).
    """
    if not routes:
        return {"type": "FeatureCollection", "features": []}, None
    feats = []
    order = [i for i in range(len(routes)) if i != focus] + [focus]
    for i in order:
        r = routes[i]
        coords = []
        for u, v, key in r.edges:
            coords += [[lon, lat] for lat, lon in _edge_coords(G, u, v, key)]
        feats.append({
            "type": "Feature",
            "properties": {"color": score_hex(r.walk_score),
                           "role": "focused" if i == focus else "alt"},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    fc = []
    for u, v, key in routes[focus].edges:
        fc += [[lon, lat] for lat, lon in _edge_coords(G, u, v, key)]
    bounds = None
    if fc:
        lons = [c[0] for c in fc]
        lats = [c[1] for c in fc]
        bounds = [[min(lons), min(lats)], [max(lons), max(lats)]]
    return {"type": "FeatureCollection", "features": feats}, bounds


_legend_html = (
    '<div style="display:flex;gap:18px;align-items:center;margin:0 0 8px;'
    'font-family:IBM Plex Mono,monospace;font-size:11px;color:#5c564a;">'
    '<span style="text-transform:uppercase;letter-spacing:0.12em;color:#a8a08c;">Walk score by block</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#3f8f5f;vertical-align:middle;"></span> 80+</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#789b3e;vertical-align:middle;"></span> 65–79</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#c8922f;vertical-align:middle;"></span> 50–64</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#c0512f;vertical-align:middle;"></span> under 50</span>'
    '</div>'
)
if routes:
    _leg_col, _btn_col = st.columns([5, 1], vertical_alignment="center")
    _leg_col.markdown(_legend_html, unsafe_allow_html=True)
    _btn_col.button("Fit route", key="fit_route_btn", on_click=_recenter,
                    use_container_width=True, help="Recenter the map on the selected route.")
else:
    st.markdown(_legend_html, unsafe_allow_html=True)

_focus = st.session_state.focus
_segmented = st.session_state.get(f"seg_{_focus}", False)
_weights = st.session_state.active_weights

if _MAP_BACKEND == "maplibre":
    # B2 GPU vector map (persistent component, no remount). The camera token changes
    # only on a new search, a focus switch, or Fit route, so the JS animates on
    # intent only and a plain rerun / manual pan leaves the view alone.
    _gj, _bounds = _route_geojson(G, routes, _focus)
    # Camera reframes only on a NEW TRIP (committed origin/destination changes) or
    # the Fit route button (recenter_nonce) — NOT on a focus switch or a weight-only
    # re-search — so a manual pan/zoom is preserved while comparing alternatives or
    # tweaking sliders. Fit route frames whichever route is currently focused.
    _committed = st.session_state.committed or {}
    _cam_token = f"{_committed.get('o')}|{_committed.get('d')}|{st.session_state.recenter_nonce}"
    _MAPLIBRE_COMPONENT(
        geojson=_gj,
        camera={"bounds": _bounds, "token": _cam_token, "animate": True},
        style=_MAPLIBRE_BASEMAP,
        height=660,
        key="maplibre_map",
        default=None,
    )
else:
    # Persistent base map (stable key → never remounts), routes as a swappable
    # FeatureGroup, and the camera moved via center/zoom. st_folium only setViews
    # when center/zoom change vs the last pass, so the camera eases to a route on a
    # search or focus switch but stays put on a segment toggle or a manual pan.
    base_map = build_base_map(G)
    route_layer = build_route_layer(G, routes, _focus, _weights, _segmented)
    cam_center, cam_zoom = camera_view(G, routes, _focus)
    # Fold the "Fit route" nonce into BOTH center and zoom as an imperceptible,
    # non-accumulating jitter (alternates 0 / ~0.2 m / 0.001 zoom). Clicking the
    # button flips it, so both values differ from the last pass and st_folium
    # re-fires setView with the *route's* center AND zoom — without the zoom jitter
    # the zoom branch sees `zoom === last_zoom`, keeps the user's current (panned-in)
    # zoom, and only recenters. On every other rerun the nonce is unchanged, so the
    # camera (and any manual pan/zoom) holds.
    _jit = st.session_state.recenter_nonce % 2
    cam_center = (cam_center[0] + _jit * 2e-6, cam_center[1])
    cam_zoom = cam_zoom + _jit * 1e-3
    st_folium(base_map, key="route_map", height=660, use_container_width=True,
              returned_objects=[], center=cam_center, zoom=cam_zoom,
              feature_group_to_add=route_layer)
