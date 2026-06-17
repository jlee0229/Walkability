"""
Humanpath — walkability-aware walking routes for Boston (Streamlit UI).

Run with:
    streamlit run app/streamlit_app.py

Enter an origin and destination by address, choose how far you'll go for a
better walk (the `alpha` slider), and optionally fine-tune the per-factor
weights. Routes are scored block by block and ranked; each route card can be
expanded for the specifics (confidence, weakest stretch).

Design: a warm editorial look (parchment + terracotta) with a left control rail
and a full-height map. The graph loads once per session (`@st.cache_resource`).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import streamlit as st

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
from walkability.routing.router import find_routes
from walkability.scoring.factors import _as_float, _as_str, edge_walkability
from walkability.scoring.weights import FACTOR_WEIGHTS

_ICON_PATH = str(Path(__file__).parent / "humanpath_icon.png")
st.set_page_config(page_title="Humanpath", page_icon=_ICON_PATH, layout="wide")

# Palette (mirrors the Humanpath design direction).
ACCENT = "#b1592e"
INK = "#211e18"
WALK_SPEED_MPS = 1.33  # ~average pedestrian pace, for walk-time estimates


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
        header[data-testid="stHeader"] { background: transparent; }
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

        /* Find button (only st.button in the app) */
        .stButton > button {
            width:100%; border:none; border-radius:13px; background:var(--accent); color:#fdfbf6;
            font-family:'Public Sans',sans-serif; font-weight:600; font-size:15px; padding:13px 15px;
            box-shadow:0 4px 14px rgba(177,89,46,.32); transition:filter .15s;
        }
        .stButton > button:hover { filter:brightness(1.05); color:#fff; }
        .stButton > button:focus { color:#fff; }

        /* Expander as a quiet "fine-tune" panel */
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


@st.cache_resource(show_spinner="Loading the walk graph (one-time, ~10s)…")
def get_graph(path_str: str):
    p = Path(path_str)
    if not p.exists():  # not present locally (e.g. on a fresh deploy) → download once
        import requests
        p.parent.mkdir(parents=True, exist_ok=True)
        with st.spinner(f"Downloading map data ({p.name}) — first run only…"):
            with requests.get(f"{_GRAPH_RELEASE}/{p.name}", stream=True, timeout=120) as r:
                r.raise_for_status()
                tmp = p.with_suffix(p.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                tmp.replace(p)  # atomic: only a complete download becomes the real file
    return load_graph(p)


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
        return [(lat, lon) for lon, lat in geom.coords]
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


_FACTOR_LABELS = {
    "road_type": "street type", "surface_quality": "surface condition",
    "surface_material": "surface material", "foot_access": "foot access",
}


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


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

st.session_state.setdefault("routes", [])
st.session_state.setdefault("focus", 0)            # index of route emphasised on the map
st.session_state.setdefault("committed", None)     # params behind the shown routes
st.session_state.setdefault("active_weights", FACTOR_WEIGHTS)  # weights the shown routes/colours use
st.session_state.setdefault("view_token", 0)       # bump to remount the map on a new search
st.session_state.setdefault("region", None)
st.session_state.setdefault("error", None)

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

    with st.expander("Fine-tune what matters"):
        w_road = st.slider("Street type", 0.0, 10.0, FACTOR_WEIGHTS["road_type"], 0.5)
        w_surf = st.slider("Surface condition", 0.0, 10.0, FACTOR_WEIGHTS["surface_quality"], 0.5)
        w_mat = st.slider("Surface material", 0.0, 10.0, FACTOR_WEIGHTS["surface_material"], 0.5)
        w_foot = st.slider("Foot access", 0.0, 10.0, FACTOR_WEIGHTS["foot_access"], 0.5)

    _custom = {"road_type": w_road, "surface_quality": w_surf,
               "surface_material": w_mat, "foot_access": w_foot}
    weights = FACTOR_WEIGHTS if _custom == FACTOR_WEIGHTS else _custom

    params = {"o": o_addr.strip(), "d": d_addr.strip(), "alpha": alpha, "w": dict(_custom)}
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
            '<span style="font-size:12.5px;color:#5c564a;">Priorities changed — update to recompute.</span></div>',
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
        st.session_state.view_token += 1  # re-frame the map to the new result

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

def _focused_center(G, route):
    ys = [G.nodes[n]["y"] for n in route.nodes]
    xs = [G.nodes[n]["x"] for n in route.nodes]
    return (sum(ys) / len(ys), sum(xs) / len(xs))


def build_map(G, routes, focus, weights, segmented):
    # Centre on the focused route (never the graph default) so any re-render lands
    # on the trip, not on the city centroid. Native wheel zoom (the SmoothWheelZoom
    # plugin doesn't execute inside st_folium's iframe): zoom_snap=0 for fractional
    # zoom, wheel_px_per_zoom_level=40 for a brisk-but-smooth trackpad/mouse zoom.
    center = _focused_center(G, routes[focus]) if routes else _graph_center(G)
    fmap = folium.Map(location=center, zoom_start=14, tiles=None, zoom_control=True,
                      zoom_snap=0, wheel_px_per_zoom_level=40)
    folium.TileLayer(
        "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png",
        attr="© OpenStreetMap, © CARTO", subdomains="abcd", max_zoom=20, control=False,
    ).add_to(fmap)
    folium.TileLayer(
        "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png",
        attr="© CARTO", subdomains="abcd", max_zoom=20, control=False,
    ).add_to(fmap)

    if not routes:
        return fmap

    # Alternatives first (faint), focused route last + on top. The focused route
    # is a single smooth line by default; only "Show segments" (segmented=True)
    # breaks it into per-block coloured pieces.
    order = [i for i in range(len(routes)) if i != focus] + [focus]
    for i in order:
        r = routes[i]
        if i != focus:
            coords = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in r.nodes]
            folium.PolyLine(coords, color=score_hex(r.walk_score), weight=4,
                            opacity=0.4, line_cap="round").add_to(fmap)
        else:
            full = []
            for u, v, key in r.edges:
                full += _edge_coords(G, u, v, key)
            folium.PolyLine(full, color="#faf8f2", weight=10, opacity=1,
                            line_cap="round", line_join="round").add_to(fmap)  # halo
            if segmented:
                for u, v, key in r.edges:
                    cs = _edge_coords(G, u, v, key)
                    w, _ = edge_walkability(G[u][v][key], weights)
                    folium.PolyLine(
                        cs, color=score_hex(w), weight=6, opacity=1, line_cap="round",
                        tooltip=f"walk {round(w*100)}/100 · {_as_str(G[u][v][key].get('highway')) or 'path'}",
                    ).add_to(fmap)
            else:
                folium.PolyLine(
                    full, color=score_hex(r.walk_score), weight=6, opacity=1,
                    line_cap="round", line_join="round",
                    tooltip=f"Walk score {round(r.walk_score*100)}/100",
                ).add_to(fmap)

    focal = routes[focus]
    o = (G.nodes[focal.nodes[0]]["y"], G.nodes[focal.nodes[0]]["x"])
    d = (G.nodes[focal.nodes[-1]]["y"], G.nodes[focal.nodes[-1]]["x"])
    folium.CircleMarker(o, radius=7, color="#faf8f2", weight=3, fill_color=ACCENT,
                        fill_opacity=1, tooltip="Start").add_to(fmap)
    folium.CircleMarker(d, radius=7, color="#faf8f2", weight=3, fill_color=INK,
                        fill_opacity=1, tooltip="Destination").add_to(fmap)
    # Fit the camera to the FOCUSED route. st_folium only re-renders (and thus
    # re-fits) when the figure actually changes — i.e. on a search, on "Show on
    # map", or on a segment toggle — so editing sliders/addresses leaves the view
    # alone, and the camera never snaps to the city default.
    fpts = []
    for u, v, key in routes[focus].edges:
        fpts += _edge_coords(G, u, v, key)
    if fpts:
        lats = [p[0] for p in fpts]
        lons = [p[1] for p in fpts]
        fmap.fit_bounds([(min(lats), min(lons)), (max(lats), max(lons))], padding=(60, 60))
    return fmap


st.markdown(
    '<div style="display:flex;gap:18px;align-items:center;margin:0 0 8px;'
    'font-family:IBM Plex Mono,monospace;font-size:11px;color:#5c564a;">'
    '<span style="text-transform:uppercase;letter-spacing:0.12em;color:#a8a08c;">Walk score by block</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#3f8f5f;vertical-align:middle;"></span> 80+</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#789b3e;vertical-align:middle;"></span> 65–79</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#c8922f;vertical-align:middle;"></span> 50–64</span>'
    '<span><span style="display:inline-block;width:18px;height:4px;border-radius:2px;background:#c0512f;vertical-align:middle;"></span> under 50</span>'
    '</div>',
    unsafe_allow_html=True,
)

_segmented = st.session_state.get(f"seg_{st.session_state.focus}", False)
fmap = build_map(G, routes, st.session_state.focus, st.session_state.active_weights, _segmented)
st_folium(fmap, key=f"map_{st.session_state.view_token}", height=660,
          use_container_width=True, returned_objects=[])
