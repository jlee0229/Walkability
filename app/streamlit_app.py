"""
Humanpath — walkability-aware walking routes for metro Boston (Streamlit UI).

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
from walkability.graph.csr import RoutingGraph, csr_path, load_csr
from walkability.graph.inventory import CITY_PROFILES
from walkability.routing.router import find_routes
from walkability.scoring.factors import _as_float, _as_str, edge_walkability
from walkability.scoring.weights import FACTOR_WEIGHTS

_ICON_PATH = str(Path(__file__).parent / "humanpath_icon.png")
st.set_page_config(page_title="Humanpath", page_icon=_ICON_PATH, layout="wide")

# Palette (mirrors the Humanpath design direction).
ACCENT = "#b1592e"
INK = "#211e18"
WALK_SPEED_MPS = 1.33  # ~average pedestrian pace, for walk-time estimates

def _cfg(name, default=""):
    """Deployment config from the environment OR Streamlit secrets, then default.

    HF Spaces injects config as **environment variables**; Streamlit Community
    Cloud exposes dashboard config as **st.secrets** (NOT os.environ). Check both
    so the same code works on either host (env wins). st.secrets raises when no
    secrets file exists (plain local runs) — swallow that and fall back.
    """
    v = os.environ.get(name)
    if v is not None:
        return v
    try:
        return str(st.secrets[name])
    except Exception:
        return default


# Map backend (B2): "maplibre" (default — the GPU vector map) or "folium" (the
# st_folium raster map, also the automatic fallback if MapLibre/WebGL fails in the
# browser; see the maplibre_failed round-trip below). Override via HUMANPATH_MAP.
_MAP_BACKEND = _cfg("HUMANPATH_MAP", "maplibre").strip().lower()
# Verification hook: force the MapLibre component to report a fatal failure so the
# graceful st_folium fallback can be exercised end-to-end. (HUMANPATH_MAP_FORCE_FAIL=1)
_MAP_FORCE_FAIL = _cfg("HUMANPATH_MAP_FORCE_FAIL", "").strip() not in ("", "0", "false")


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

        /* Hide default Streamlit chrome for a cleaner app shell. The header stays
           transparent + click-through (it's an invisible bar over the top content
           row that would otherwise eat pointer events on e.g. the top half of the
           "Fit route" button). We KEEP the toolbar present (not display:none) so
           the collapsed-rail REOPEN chevron — stExpandSidebarButton, which lives
           in the toolbar — is available at every width; only the deploy button and
           menu are suppressed, and only the expand chevron re-arms pointer events
           (a child may set pointer-events:auto under a pointer-events:none
           ancestor). */
        header[data-testid="stHeader"] { background: transparent; pointer-events: none; }
        #MainMenu, footer { visibility: hidden; }
        [data-testid="stToolbar"] { pointer-events: none; }
        [data-testid="stAppDeployButton"], [data-testid="stMainMenu"], [data-testid="stStatusWidget"] { display: none !important; }
        [data-testid="stExpandSidebarButton"] { display: inline-flex !important; pointer-events: auto !important; }
        [data-testid="stMainBlockContainer"] { padding-top: 1.0rem; }

        /* Left rail — fixed 446px width and non-resizable, but COLLAPSIBLE at
           every viewport size (close/open toggle always available). The 446px
           pin is scoped to the OPEN rail (aria-expanded="true") only: forcing the
           width with !important on the collapsed state fought Streamlit's own
           collapse transform and left the rail half-shown at some widths. */
        section[data-testid="stSidebar"] { background: #f6f1e6; border-right: 1px solid var(--line); }
        section[data-testid="stSidebar"][aria-expanded="true"] { width: 446px !important; min-width: 446px !important; }
        [data-testid="stSidebarResizeHandle"], [data-testid="stSidebarResizer"] { display: none !important; }
        /* Keep a slim header strip so the collapse chevron is always reachable. */
        [data-testid="stSidebarHeader"] { padding: 0.2rem 0.5rem 0 !important; min-height: 0 !important; }
        [data-testid="stSidebarCollapseButton"] { display: inline-flex !important; }
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

        /* ── Mobile / small-viewport (esp. landscape phones) ──────────────────
           The collapse/reopen toggle is handled above at ALL widths, so this
           block only narrows the OPEN rail (a fixed 446px leaves almost no room
           for the map on a ~700–930px landscape phone) and tightens type/inputs.
           The width is scoped to aria-expanded="true" for the same reason as the
           desktop rule — never force a width on the collapsed rail. */
        @media (max-width: 932px) {
            section[data-testid="stSidebar"][aria-expanded="true"] {
                width: 300px !important; min-width: 260px !important; max-width: 70vw !important;
            }
            /* Tighten oversized display type so the narrower rail isn't cramped */
            .fp-title { font-size: 30px; }
            .fp-score { font-size: 30px; }
            /* iOS Safari auto-zooms the whole page when a focused input is under
               16px. The desktop inputs are 14.5px → tapping the address field
               zooms in jarringly. Force 16px on small screens to suppress it. */
            [data-testid="stTextInput"] input { font-size: 16px !important; }
        }
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


def _prewarm(G):
    """Build the snap/routing caches now (during the load spinner) so the user's
    *first* search isn't ~0.7–1.2 s slower than the rest.

    These caches — the largest-walkable-component mask and the per-node
    walk-quality array (plus, on the Nx path, the coordinate cache) — are memoised
    on the graph and otherwise built lazily on the first ``find_routes`` snap.
    Best-effort: warming must never block loading, so failures are swallowed."""
    try:
        if isinstance(G, RoutingGraph):
            from walkability.routing import csr_router
            csr_router._routable_mask(G)
            csr_router._node_quality(G)
        else:
            from walkability.routing import clip
            clip._node_coords(G)
            clip._routable_mask(G)
            clip._node_walk_quality(G)
    except Exception:
        pass


@st.cache_resource(max_entries=1, show_spinner="Loading the walk graph (one-time)…")
def get_graph(path_str: str):
    """Load the walk graph for an enriched GraphML path, smallest-first.

    Fallback ladder: the Phase-2 compact **CSR** pickle (``*.csr.pkl`` — ~32–60 MB,
    <0.1 s, ~80 MB RAM) → the Phase-1 runtime ``MultiDiGraph`` pickle
    (``*.runtime.pkl``) → the heavyweight GraphML. Each tier is tried locally, then
    downloaded from the GitHub Release. Routing/rendering handle either the CSR
    RoutingGraph or a MultiDiGraph transparently (see the ``_edge_*`` helpers and
    ``router.find_routes``'s isinstance dispatch).

    ``max_entries=1`` keeps **only one graph resident** — switching city/area
    evicts the previous graph rather than stacking both in RAM (a second city like
    Austin alone approaches the host cap; see the deploy-memory note). The load is
    pre-warmed (:func:`_prewarm`) so the first search is as snappy as the rest.
    """
    graphml = Path(path_str)
    cp = csr_path(graphml)
    rt = runtime_path(graphml)

    def _load():
        if cp.exists():
            return load_csr(cp)
        if rt.exists():
            return load_runtime(rt)
        # Not local → fetch the compact CSR from the release first (smallest).
        with st.spinner(f"Downloading map data ({cp.name}) — first run only…"):
            if _download_release_asset(cp):
                return load_csr(cp)
        with st.spinner(f"Downloading map data ({rt.name}) — first run only…"):
            if _download_release_asset(rt):
                return load_runtime(rt)
        # Last resort: the heavyweight GraphML (older release without a pickle).
        if not graphml.exists():
            with st.spinner(f"Downloading map data ({graphml.name}) — first run only…"):
                _download_release_asset(graphml)
        return load_graph(graphml)

    G = _load()
    _prewarm(G)
    return G


def _graph_center(G):
    if isinstance(G, RoutingGraph):
        c = getattr(G, "_center", None)
        if c is None:
            c = (float(G.node_y.mean()), float(G.node_x.mean()))
            G._center = c
        return c
    cached = G.graph.get("_center")
    if cached is None:
        ys = [d["y"] for _, d in G.nodes(data=True)]
        xs = [d["x"] for _, d in G.nodes(data=True)]
        cached = (sum(ys) / len(ys), sum(xs) / len(xs))
        G.graph["_center"] = cached
    return cached


# --- Graph-type-agnostic edge/node accessors -------------------------------
# Routes carry (u, v, key) tuples; on the CSR RoutingGraph they also carry the
# aligned edge index (RouteResult.edge_indices), which is how geometry/fields are
# fetched there. These helpers hide the MultiDiGraph-vs-RoutingGraph split so the
# rendering code reads the same on both.

def _route_edge_idx(r):
    """Per-hop CSR edge index aligned with ``r.edges`` (None-filled on the Nx path)."""
    return r.edge_indices if r.edge_indices else [None] * len(r.edges)


def _node_yx(G, n):
    if isinstance(G, RoutingGraph):
        i = G.id_to_idx[n]
        return (float(G.node_y[i]), float(G.node_x[i]))
    nd = G.nodes[n]
    return (nd["y"], nd["x"])


def _edge_data(G, u, v, key, eidx=None):
    """Edge-attribute view (``.get``/``.items``) for one hop, either substrate."""
    return G.edge_view(eidx) if isinstance(G, RoutingGraph) else G[u][v][key]


def _edge_coords(G, u, v, key, eidx=None):
    """Edge polyline as folium (lat, lon) points, either substrate.

    Geometry is a float32 (n, 2) (lon, lat) array on the CSR/runtime path and a
    shapely LineString on the enriched GraphML; both iterate as (lon, lat) and are
    flipped here. Falls back to the two node endpoints when geometry is absent."""
    if isinstance(G, RoutingGraph):
        geom = G.edge_geometry(eidx)
        if geom is not None:
            return [(float(lat), float(lon)) for lon, lat in geom]
        return [_node_yx(G, u), _node_yx(G, v)]
    geom = G[u][v][key].get("geometry")
    if geom is not None:
        coords = geom if isinstance(geom, np.ndarray) else geom.coords
        return [(float(lat), float(lon)) for lon, lat in coords]
    return [(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])]


# Geocoding, metro-biased; cached so repeats are instant. Primary is **Photon**
# (komoot) — OSM-based, no key, and tolerant of server/cloud use. Nominatim's public
# server rate-limits/blocks shared cloud IPs (Streamlit Community Cloud), which used
# to HANG the deployed app on "Reading the streets…" via a no-timeout osmnx fallback.
# Photon primary + a timed Nominatim fallback fixes that; every call has a hard
# timeout so geocoding can never spin forever (worst case → "couldn't find address").
_PHOTON_URL = "https://photon.komoot.io/api"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_GEO_HEADERS = {"User-Agent": "walkability-route-app/0.1 (educational project)"}

# ---------------------------------------------------------------------------
# Map areas — the city/area selector
# ---------------------------------------------------------------------------
# Each area = a walk graph + its geocoding box/bias + basemap style + a coverage
# blurb. The geocoding bbox HARD-FILTERS Photon results, which is what lets a bare
# name ("Harvard Square", "Barton Springs") resolve to the local one without
# appending a town. Boston ships a brand-tinted self-hosted PMTiles cut; other
# cities fall back to the global OpenFreeMap positron style (functional
# everywhere — a per-city PMTiles cut can be added later via the CLAUDE.md
# recipe). Keep each area's bbox in sync with its graph extent. Only ONE area's
# graph is resident at a time (get_graph `max_entries=1`).
_BOSTON_GEO = {
    "bbox": (-71.21, 42.21, -70.94, 42.44),   # lon_min, lat_min, lon_max, lat_max
    "bias": (42.36, -71.08),                  # lat, lon
    "covered": "Boston, Brookline, Cambridge, Somerville, Everett, or Chelsea",
    "style": "pmtiles-boston",
    "from": "Massachusetts State House",      # default trip endpoints for this area
    "to": "Boston Public Garden",
}
_AUSTIN_GEO = {
    "bbox": (-97.98, 30.08, -97.55, 30.55),
    "bias": (30.27, -97.74),
    "covered": "Austin, TX",
    "style": "openfreemap",
    "from": "Texas State Capitol",
    "to": "Zilker Park",
}
# `city=True` areas are the first-class options in the rail selector; the Boston
# `DEV_REGIONS` test beds stay resolvable (a set region_select value still loads
# them) but are hidden from the promoted selector.
_AREAS: dict[str, dict] = {
    "full":   {"label": "Boston metro", "graph": str(ENRICHED_PATH), "city": True, **_BOSTON_GEO},
    "austin": {"label": "Austin, TX",   "graph": str(CITY_PROFILES["austin"].enriched_path), "city": True, **_AUSTIN_GEO},
}
for _r in DEV_REGIONS:
    _AREAS[_r] = {"label": _r.replace("_", " ").title(), "graph": str(dev_region_path(_r)), **_BOSTON_GEO}
_CITY_AREAS = [k for k, a in _AREAS.items() if a.get("city")]
_DEFAULT_AREA = "full"


def in_coverage(latlon, bbox) -> bool:
    """True if a geocoded (lat, lon) falls inside the active area's extent."""
    lat, lon = latlon
    lon_min, lat_min, lon_max, lat_max = bbox
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


def _photon_label(props: dict) -> str | None:
    """A human "what got matched" string from a Photon feature — the POI/place
    name plus a street/city hint (so "Austin Capital" reads as the mortgage office
    it actually resolved to, not silently)."""
    name = props.get("name")
    detail = ", ".join(p for p in (props.get("street"), props.get("city")) if p)
    if name and detail:
        return f"{name} · {detail}"
    return name or detail or None


@st.cache_data(show_spinner=False)
def _geocode_query(q: str, bbox: tuple, bias: tuple):
    """(lat, lon, label) for a query within an area's box; raises on total failure
    (so failures aren't cached). ``label`` is the matched place, shown to the user.
    ``bbox``/``bias`` are part of the cache key so the same address resolves
    per-area (e.g. a "Main St" in Boston vs Austin)."""
    import requests

    bias_lat, bias_lon = bias
    photon_bias = {"lat": bias_lat, "lon": bias_lon,
                   "bbox": ",".join(str(v) for v in bbox)}
    # Primary: Photon (komoot). GeoJSON features; coords are [lon, lat].
    try:
        resp = requests.get(_PHOTON_URL, params={"q": q, "limit": 1, **photon_bias},
                            headers=_GEO_HEADERS, timeout=8)
        resp.raise_for_status()
        feats = resp.json().get("features") or []
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"][:2]
            return (float(lat), float(lon), _photon_label(feats[0].get("properties", {})))
    except Exception:
        pass

    # Fallback: Nominatim (bounded to the area box, then unbounded), each call
    # timed. An unbounded hit outside the box is caught by in_coverage at the
    # call site (clear "outside the covered area" error, not a bad snap).
    # Nominatim viewbox order is left,top,right,bottom.
    viewbox = f"{bbox[0]},{bbox[3]},{bbox[2]},{bbox[1]}"
    base = {"q": q, "format": "json", "limit": 1, "countrycodes": "us", "viewbox": viewbox}
    for bounded in (1, 0):
        try:
            resp = requests.get(_NOMINATIM_URL, params={**base, "bounded": bounded},
                                headers=_GEO_HEADERS, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            if data:
                label = ", ".join((data[0].get("display_name") or "").split(",")[:2]).strip()
                return (float(data[0]["lat"]), float(data[0]["lon"]), label or None)
        except Exception:
            continue
    raise ValueError(f"no geocoding result for {q!r}")


def geocode(query: str, geo: dict):
    """(lat, lon) for an address within the active area, or None. No town is
    appended to the query — the area's Photon bbox filter does the disambiguation
    (an old ", Boston" append made hull-town addresses ungeocodable)."""
    q = query.strip()
    if not q:
        return None
    try:
        r = _geocode_query(q, geo["bbox"], geo["bias"])
        return (r[0], r[1])
    except Exception:
        return None


def geocode_label(query: str, geo: dict):
    """The matched-place label for an address (for the "→ …" caption under each
    box), or None. Same cached call as :func:`geocode`, so no extra request."""
    q = query.strip()
    if not q:
        return None
    try:
        return _geocode_query(q, geo["bbox"], geo["bias"])[2]
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
    for (u, v, key), e in zip(route.edges, _route_edge_idx(route)):
        d = _edge_data(G, u, v, key, e)
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
# Trip endpoints default to the initially-selected area's landmarks; reset to the
# new area's on a city switch (see the rail selector below).
_init_area = _AREAS.get(st.session_state.get("region_select", _DEFAULT_AREA), _AREAS[_DEFAULT_AREA])
st.session_state.setdefault("from_addr", _init_area["from"])
st.session_state.setdefault("to_addr", _init_area["to"])

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

    # Promoted city selector (cities only — the DEV_REGIONS test beds are hidden).
    # Resolved here, above the Trip inputs, so a city switch can reset the trip
    # endpoints to the new city's landmarks BEFORE those widgets render.
    st.markdown('<div class="fp-mono">City</div>', unsafe_allow_html=True)
    # A stale value (e.g. a hidden dev region from an older session) isn't a valid
    # option in the cities-only selector — reset it so the widget can't error.
    if "region_select" in st.session_state and st.session_state.region_select not in _CITY_AREAS:
        st.session_state.region_select = _DEFAULT_AREA
    st.selectbox("City", _CITY_AREAS, format_func=lambda k: _AREAS[k]["label"],
                 label_visibility="collapsed", key="region_select")
    region = st.session_state.get("region_select", _DEFAULT_AREA)
    area = _AREAS.get(region, _AREAS[_DEFAULT_AREA])
    graph_path = area["graph"]
    if st.session_state.region != region:
        st.session_state.region = region
        st.session_state.from_addr = area["from"]   # reset endpoints to the new city
        st.session_state.to_addr = area["to"]
        st.session_state.routes = []
        st.session_state.committed = None
        st.session_state.error = None

    st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)
    st.markdown('<div class="fp-mono">Trip</div>', unsafe_allow_html=True)
    o_addr = st.text_input("From", key="from_addr", label_visibility="collapsed",
                           placeholder=f"From — e.g. {area['from']}")
    _o_label = geocode_label(o_addr, area)
    if _o_label:
        st.caption(f"→ {_o_label}")
    d_addr = st.text_input("To", key="to_addr", label_visibility="collapsed",
                           placeholder=f"To — e.g. {area['to']}")
    _d_label = geocode_label(d_addr, area)
    if _d_label:
        st.caption(f"→ {_d_label}")

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

# `region`/`area`/`graph_path` are resolved in the rail (with the promoted city
# selector, above the Trip inputs). Load the graph for the selected city.
G = get_graph(graph_path)


# ---------------------------------------------------------------------------
# Run a search
# ---------------------------------------------------------------------------

if find:
    st.session_state.error = None
    _covered = area["covered"]
    _bbox = area["bbox"]
    with st.spinner("Reading the streets…"):
        o = geocode(o_addr, area)
        d = geocode(d_addr, area)
        ok = (o is not None and d is not None
              and in_coverage(o, _bbox) and in_coverage(d, _bbox))
        routes_found = find_routes(G, o, d, alpha=alpha, weights=weights) if ok else None
    if o is None:
        st.session_state.error = f"Couldn't find “{o_addr}”. Try a more specific address."
    elif d is None:
        st.session_state.error = f"Couldn't find “{d_addr}”. Try a more specific address."
    elif not in_coverage(o, _bbox):
        st.session_state.error = (f"“{o_addr}” looks outside the covered area. "
                                  f"Humanpath currently covers {_covered}.")
    elif not in_coverage(d, _bbox):
        st.session_state.error = (f"“{d_addr}” looks outside the covered area. "
                                  f"Humanpath currently covers {_covered}.")
    else:
        st.session_state.routes = routes_found
        st.session_state.committed = params
        st.session_state.active_weights = weights  # freeze rendering to the committed weights
        st.session_state.focus = 0
        # On small screens, collapse the rail after a search so the map (the
        # payload) gets the screen. Consumed once at the end of the run; the JS
        # is itself viewport-gated, so desktop is never affected.
        st.session_state._collapse_rail_mobile = True

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
                    for j, ((u, v, ekey), e) in enumerate(zip(r.edges, _route_edge_idx(r))):
                        d = _edge_data(G, u, v, ekey, e)
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


# (The city selector was promoted to the top of the rail, above the Trip inputs.)


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
            coords = [_node_yx(G, n) for n in r.nodes]
            folium.PolyLine(coords, color=score_hex(r.walk_score), weight=4,
                            opacity=0.4, line_cap="round").add_to(fg)
        else:
            full = []
            for (u, v, key), e in zip(r.edges, _route_edge_idx(r)):
                full += _edge_coords(G, u, v, key, e)
            folium.PolyLine(full, color="#faf8f2", weight=10, opacity=1,
                            line_cap="round", line_join="round").add_to(fg)  # halo
            if segmented:
                for (u, v, key), e in zip(r.edges, _route_edge_idx(r)):
                    cs = _edge_coords(G, u, v, key, e)
                    d = _edge_data(G, u, v, key, e)
                    w, _ = edge_walkability(d, weights)
                    folium.PolyLine(
                        cs, color=score_hex(w), weight=6, opacity=1, line_cap="round",
                        tooltip=f"walk {round(w*100)}/100 · {_as_str(d.get('highway')) or 'path'}",
                    ).add_to(fg)
            else:
                folium.PolyLine(
                    full, color=score_hex(r.walk_score), weight=6, opacity=1,
                    line_cap="round", line_join="round",
                    tooltip=f"Walk score {round(r.walk_score*100)}/100",
                ).add_to(fg)

    focal = routes[focus]
    o = _node_yx(G, focal.nodes[0])
    d = _node_yx(G, focal.nodes[-1])
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
    fr = routes[focus]
    for (u, v, key), e in zip(fr.edges, _route_edge_idx(fr)):
        fpts += _edge_coords(G, u, v, key, e)
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
# Empty by default → each area picks its own basemap (Boston = self-hosted PMTiles,
# other cities = global OpenFreeMap). Set HUMANPATH_STYLE to force one style
# globally (e.g. "openfreemap", "pmtiles-demo") for testing.
_HUMANPATH_STYLE = _cfg("HUMANPATH_STYLE", "").strip().lower()

# B2.1b PMTiles validation: a public, CORS-open (access-control-allow-origin: *),
# range-request-enabled Protomaps demo file (Florence). Proves the pmtiles://
# protocol + HTTP range + CORS + GPU vector render work INSIDE our real-origin
# component iframe — the make-or-break unknown. (The Protomaps demo *planet* is
# origin-locked to maps.protomaps.com, so production needs a self-hosted Boston
# .pmtiles; this only validates the mechanism.) Schema = the old Protomaps v2
# layers in that file (landuse/roads/mask), per the MapLibre pmtiles example.
_PMTILES_DEMO_URL = "pmtiles://https://pmtiles.io/protomaps(vector)ODbL_firenze.pmtiles"
_PMTILES_DEMO_CENTER = [11.2558, 43.7696]  # Florence, [lon, lat]
_PMTILES_DEMO_STYLE = {
    "version": 8,
    "sources": {
        "demo": {
            "type": "vector", "url": _PMTILES_DEMO_URL,
            "attribution": '© <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
        },
    },
    "layers": [
        {"id": "bg", "type": "background", "paint": {"background-color": "#ece5d5"}},
        {"id": "mask", "type": "fill", "source": "demo", "source-layer": "mask",
         "paint": {"fill-color": "#faf8f2"}},
        {"id": "buildings", "type": "fill", "source": "demo", "source-layer": "landuse",
         "paint": {"fill-color": "#e3d8bd"}},
        {"id": "roads", "type": "line", "source": "demo", "source-layer": "roads",
         "paint": {"line-color": "#b1592e", "line-width": 1.0}},
    ],
}

# Production basemap: self-hosted Boston Protomaps PMTiles (v4 schema) rendered with
# the protomaps-themes-base theme (built in main.js via the `_protomaps` marker).
# Default = the Cloudflare R2 public URL (range + open CORS), so deploy needs no
# config; HUMANPATH_PMTILES_URL overrides it (e.g. a local CORS test server).
# boston_metro.pmtiles (2026-07-04) widens the cut to the metro-hull PLACES
# (adds Cambridge/Somerville/Everett/Chelsea + all of East Boston); the old
# boston.pmtiles object stays in R2 so pre-hull deploys keep rendering.
_PMTILES_BOSTON_URL = _cfg(
    "HUMANPATH_PMTILES_URL",
    "https://pub-0235cb1b1636455cbaee68cc6b610bdd.r2.dev/boston_metro.pmtiles").strip()
_PMTILES_BOSTON_STYLE = {"_protomaps": {"url": "pmtiles://" + _PMTILES_BOSTON_URL, "flavor": "light"}}

_OPENFREEMAP_POSITRON = "https://tiles.openfreemap.org/styles/positron"
_MAPLIBRE_STYLES = {
    "demotiles": "https://demotiles.maplibre.org/style.json",
    "openfreemap": _OPENFREEMAP_POSITRON,
    "pmtiles-demo": _PMTILES_DEMO_STYLE,
    "pmtiles-boston": _PMTILES_BOSTON_STYLE,
}


def _resolve_basemap(area: dict):
    """Basemap style for the active area — a global HUMANPATH_STYLE override wins,
    else the area's own style (Boston = brand PMTiles, others = OpenFreeMap)."""
    name = _HUMANPATH_STYLE or area.get("style", "openfreemap")
    return _MAPLIBRE_STYLES.get(name, _OPENFREEMAP_POSITRON)

_MAPLIBRE_COMPONENT = components.declare_component(
    "humanpath_maplibre",
    path=str(Path(__file__).parent / "components" / "maplibre_map" / "frontend"),
)


def _route_lonlat(G, r):
    """Full edge geometry of a route as GeoJSON [lon, lat] coords.

    Consecutive edges share a node, so each edge's geometry repeats the previous
    edge's last coord — dropping the duplicate keeps the LineString clean (coincident
    vertices confuse GL simplification/clipping and are pure bloat)."""
    coords = []
    for (u, v, key), e in zip(r.edges, _route_edge_idx(r)):
        for lat, lon in _edge_coords(G, u, v, key, e):
            pt = [lon, lat]
            if not coords or coords[-1] != pt:
                coords.append(pt)
    return coords


def _line_feature(coords, props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords}}


def _route_geojson(G, routes, focus, weights, segmented):
    """(routes_fc, points_fc, focus_bounds) for the MapLibre component.

    Mirrors the folium ``build_route_layer`` parity exactly. ``routes_fc`` holds,
    by ``role``: faint ``alt`` lines (one per alternative), a single white ``halo``
    under the focused route, and the focused line as either one ``focused`` feature
    or many per-block ``segment`` features (when ``segmented``). Each carries a
    ``color`` and a hover ``label``. ``points_fc`` holds the O/D markers
    (``role`` origin/dest). ``bounds`` is MapLibre's [[w,s],[e,n]] for the focused
    route. Coords are GeoJSON [lon, lat]. Empty when there are no routes.
    """
    empty = {"type": "FeatureCollection", "features": []}
    if not routes:
        return dict(empty), dict(empty), None

    feats = []
    # Alternatives first (drawn underneath), focused last (on top).
    for i in range(len(routes)):
        if i == focus:
            continue
        r = routes[i]
        feats.append(_line_feature(_route_lonlat(G, r), {
            "role": "alt", "color": score_hex(r.walk_score),
            "label": f"Walk score {round(r.walk_score * 100)}/100"}))

    focal = routes[focus]
    full = _route_lonlat(G, focal)
    feats.append(_line_feature(full, {"role": "halo"}))  # continuous white casing
    joints = []  # block-boundary dots (segmented mode) so adjacent blocks read apart
    if segmented:
        seg_coords = []
        for (u, v, key), e in zip(focal.edges, _route_edge_idx(focal)):
            d = _edge_data(G, u, v, key, e)
            w, _ = edge_walkability(d, weights)
            hwy = _as_str(d.get("highway")) or "path"
            cs = [[lon, lat] for lat, lon in _edge_coords(G, u, v, key, e)]
            seg_coords.append(cs)
            feats.append(_line_feature(cs, {
                "role": "segment", "color": score_hex(w),
                "label": f"walk {round(w * 100)}/100 · {hwy}"}))
        # A small dot at each interior block boundary (start of every block but the
        # first) — crisp separation without the old (ugly) white gaps.
        for cs in seg_coords[1:]:
            if cs:
                joints.append({
                    "type": "Feature", "properties": {"role": "joint"},
                    "geometry": {"type": "Point", "coordinates": cs[0]}})
    else:
        feats.append(_line_feature(full, {
            "role": "focused", "color": score_hex(focal.walk_score),
            "label": f"Walk score {round(focal.walk_score * 100)}/100"}))

    points, bounds = [], None
    if full:
        points = joints + [
            {"type": "Feature", "properties": {"role": "origin", "label": "Start"},
             "geometry": {"type": "Point", "coordinates": full[0]}},
            {"type": "Feature", "properties": {"role": "dest", "label": "Destination"},
             "geometry": {"type": "Point", "coordinates": full[-1]}},
        ]
        lons = [c[0] for c in full]
        lats = [c[1] for c in full]
        bounds = [[min(lons), min(lats)], [max(lons), max(lats)]]
    return ({"type": "FeatureCollection", "features": feats},
            {"type": "FeatureCollection", "features": points}, bounds)


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

# Graceful fallback: the MapLibre component reports a fatal client-side failure
# (no WebGL, a lib failed to load, map init threw) back to Python via
# setComponentValue; we latch it for the session and render the st_folium map
# instead, so the app never goes blank. (Streamlit picks the backend server-side
# before the component runs, so this round-trip is the only real fallback path.)
_use_maplibre = _MAP_BACKEND == "maplibre" and not st.session_state.get("maplibre_failed")

if _use_maplibre:
    # B2 GPU vector map (persistent component, no remount). The camera token changes
    # only on a new search, a focus switch, or Fit route, so the JS animates on
    # intent only and a plain rerun / manual pan leaves the view alone.
    _gj, _points, _bounds = _route_geojson(G, routes, _focus, _weights, _segmented)
    # Camera reframes only on a NEW TRIP (committed origin/destination changes) or
    # the Fit route button (recenter_nonce) — NOT on a focus switch or a weight-only
    # re-search — so a manual pan/zoom is preserved while comparing alternatives or
    # tweaking sliders. Fit route frames whichever route is currently focused.
    _committed = st.session_state.committed or {}
    _cam_token = f"{_committed.get('o')}|{_committed.get('d')}|{st.session_state.recenter_nonce}"
    # Initial map view (used once, on first creation): the demo basemap is Florence,
    # otherwise the loaded graph's centre. [lon, lat] for MapLibre.
    if _HUMANPATH_STYLE == "pmtiles-demo":
        _init_center, _init_zoom = _PMTILES_DEMO_CENTER, 13
    else:
        _gc = _graph_center(G)
        _init_center, _init_zoom = [_gc[1], _gc[0]], _BASE_ZOOM
    # Switching city/area changes both the basemap and the centre; the component's
    # centre is only applied on creation, so key the component by area — a switch
    # remounts it fresh onto the new city (mirrors the folium region-switch remount),
    # while staying persistent within an area.
    _ml_val = _MAPLIBRE_COMPONENT(
        geojson=_gj,
        points=_points,
        camera={"bounds": _bounds, "token": _cam_token, "animate": True},
        style=_resolve_basemap(area),
        center=_init_center,
        zoom=_init_zoom,
        forceFail=_MAP_FORCE_FAIL,
        height=660,
        key=f"maplibre_map_{region}",
        default=None,
    )
    if isinstance(_ml_val, dict) and _ml_val.get("status") == "error":
        # Component failed in the browser — latch and re-render with folium.
        st.session_state.maplibre_failed = True
        st.rerun()
else:
    # Persistent base map (stable key → never remounts), routes as a swappable
    # FeatureGroup, and the camera moved via center/zoom. st_folium only setViews
    # when center/zoom change vs the last pass, so the camera eases to a route on a
    # search or focus switch but stays put on a segment toggle or a manual pan.
    if _MAP_BACKEND == "maplibre" and st.session_state.get("maplibre_failed"):
        st.warning("Interactive vector map unavailable — using the standard map.", icon="🗺️")
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
    # Mobile: fill the viewport for the folium FALLBACK the same way the MapLibre
    # component fills itself (see main.js computeHeight). st_folium's height is a
    # fixed 660px Python arg we can't make viewport-aware server-side, so a small
    # mobile-only helper resizes its (same-origin) iframe from its top edge to the
    # bottom of the visible viewport and nudges Leaflet to reflow. Desktop (>932px)
    # is untouched; re-applied each run since st_folium re-asserts 660 on rerun.
    components.html(
        """
        <script>
        (function () {
          function fill() {
            try {
              var pw = window.parent;
              var f = pw.document.querySelector('iframe[title="st_folium"]');
              if (!f) return;
              var vh = (pw.visualViewport && pw.visualViewport.height) || pw.innerHeight;
              var top = Math.max(0, f.getBoundingClientRect().top);
              var h = Math.max(360, Math.floor(vh - top - 4));
              f.style.setProperty("height", h + "px", "important");
              // Same-origin (/component/) → reach in, stretch the map div, reflow.
              var idoc = f.contentDocument;
              if (idoc) {
                var m = idoc.querySelector(".folium-map, .leaflet-container");
                if (m) m.style.height = "100%";
              }
              if (f.contentWindow) f.contentWindow.dispatchEvent(new Event("resize"));
            } catch (e) { /* cross-origin/missing: give up quietly */ }
          }
          // Re-apply for ~2s so it survives st_folium re-asserting 660px on rerun.
          var n = 0;
          var t = setInterval(function () {
            n++;
            var pw = window.parent;
            if (n > 20 || (pw && pw.innerWidth > 932)) { clearInterval(t); return; }
            fill();
          }, 100);
        })();
        </script>
        """,
        height=0,
    )


# ---------------------------------------------------------------------------
# Mobile: collapse the rail once, right after a search (#2). Emitted only on the
# run where a search just succeeded, so it fires once per search and not on focus
# switches / slider edits. The script runs in a 0-height component iframe and
# reaches into the parent document to click the rail's collapse button — but only
# when the PARENT viewport is small, so desktop behaviour is untouched.
# ---------------------------------------------------------------------------
if st.session_state.pop("_collapse_rail_mobile", False):
    components.html(
        """
        <script>
        (function () {
          try {
            var pw = window.parent;
            if (!pw || pw.innerWidth > 932) return;   // desktop: do nothing
            // The parent DOM may still be settling after the rerun; retry briefly.
            var tries = 0;
            var tick = setInterval(function () {
              tries++;
              var doc = pw.document;
              var el = doc.querySelector('[data-testid="stSidebarCollapseButton"]');
              if (el) {
                var btn = el.tagName === 'BUTTON' ? el : el.querySelector('button');
                (btn || el).click();
                clearInterval(tick);
              } else if (tries > 20) {
                clearInterval(tick);
              }
            }, 100);
          } catch (e) { /* cross-origin or missing: leave the rail open */ }
        })();
        </script>
        """,
        height=0,
    )
