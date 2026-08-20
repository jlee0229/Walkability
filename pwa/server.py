"""
Humanpath PWA backend — a lean FastAPI twin of the Streamlit app's search flow.

Serves the installable mobile frontend (``pwa/static/``) plus a small JSON API:

    GET /api/config              area metadata the frontend boots from
    GET /api/geocode?q=...       address -> (lat, lon, label), metro-biased
    GET /api/reverse?lat&lon     (lat, lon) -> nearby place label ("use my location")
    GET /api/route?olat&olon&dlat&dlon&alpha=...   routes, serialized per block

Routing, scoring, and geocoding semantics are the app's own: ``find_routes``
dispatches to the CSR router on the compact ``*.csr.pkl`` graph (~80 MB RAM,
<0.1 s load), and the geocoder mirrors ``streamlit_app._geocode_query`` (Photon
primary with a hard-filtering metro bbox, timed Nominatim fallback, hard
timeouts everywhere). Run locally with:

    venv/bin/uvicorn pwa.server:app --port 8123
"""

from __future__ import annotations

import sys
import threading
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import requests as _requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from walkability.graph.build import ENRICHED_PATH
from walkability.graph.compact import load_runtime, runtime_path
from walkability.graph.csr import RoutingGraph, csr_path, load_csr
from walkability.routing.router import find_routes
from walkability.scoring.factors import _as_float, _as_str, edge_walkability
from walkability.scoring.weights import FACTOR_WEIGHTS

# ---------------------------------------------------------------------------
# Areas (cities). One entry per city — bbox/bias mirror streamlit_app's _AREAS
# and must stay in sync with the graph extent (see CLAUDE.md). Adding a city =
# adding one entry here: e.g. Austin would use
# CITY_PROFILES["austin"].enriched_path for "graph", its bbox/bias/defaults
# from streamlit_app._AUSTIN_GEO, and {"type": "url", "url": <OpenFreeMap
# positron>} for "style" (no per-city PMTiles cut needed). The frontend gets
# everything it needs from /api/config, so no client changes are required.
# ---------------------------------------------------------------------------

DEFAULT_AREA = "boston"
AREAS: dict[str, dict] = {
    "boston": {
        "label": "Boston metro",
        "bbox": (-71.21, 42.21, -70.94, 42.44),   # lon_min, lat_min, lon_max, lat_max
        "bias": (42.36, -71.08),                  # lat, lon
        "covered": "Boston, Brookline, Cambridge, Somerville, Everett, or Chelsea",
        "from": "Massachusetts State House",
        "to": "Boston Public Garden",
        "style": {"type": "pmtiles",
                  "url": "https://pub-0235cb1b1636455cbaee68cc6b610bdd.r2.dev/boston_metro.pmtiles"},
        "graph": ENRICHED_PATH,
    },
}


def _area(area_id: str) -> dict:
    a = AREAS.get(area_id)
    if a is None:
        raise HTTPException(404, f"Unknown area {area_id!r}.")
    return a

_GRAPH_RELEASE = "https://github.com/jlee0229/Walkability/releases/download/data-v1"
_PHOTON_URL = "https://photon.komoot.io/api"
_PHOTON_REVERSE_URL = "https://photon.komoot.io/reverse"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_GEO_HEADERS = {"User-Agent": "walkability-route-app/0.1 (educational project)"}

ALPHA_MAX = 5.0  # slider 0-100 maps to alpha 0-5, same as the Streamlit rail


# ---------------------------------------------------------------------------
# Graph load (per area, cached) — smallest-first ladder like get_graph.
# ---------------------------------------------------------------------------

_GRAPHS: dict[str, object] = {}
_GRAPH_LOCK = threading.Lock()


def _download_release_asset(p: Path) -> bool:
    """Stream a release asset into ``p`` (atomic). True on success, False on 404."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with _requests.get(f"{_GRAPH_RELEASE}/{p.name}", stream=True, timeout=120) as r:
        if r.status_code == 404:
            return False
        r.raise_for_status()
        tmp = p.with_suffix(p.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.replace(p)
    return True


def _load_graph(graphml: Path):
    """CSR pickle (local, then release) → runtime pickle (local, then release)."""
    cp = csr_path(graphml)
    rt = runtime_path(graphml)
    if cp.exists():
        return load_csr(cp)
    if rt.exists():
        return load_runtime(rt)
    if _download_release_asset(cp):
        return load_csr(cp)
    if _download_release_asset(rt):
        return load_runtime(rt)
    raise RuntimeError(
        f"No graph available: {cp.name} / {rt.name} not local and not in the "
        f"data-v1 release. Build with `python -m walkability.graph.compact` or "
        f"upload the pickle to the release."
    )


def get_graph(area_id: str):
    if area_id not in _GRAPHS:
        with _GRAPH_LOCK:
            if area_id not in _GRAPHS:
                G = _load_graph(Path(_area(area_id)["graph"]))
                # Prewarm the snap/routing caches so the first search is as
                # snappy as the rest (mirrors streamlit_app._prewarm).
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
                _GRAPHS[area_id] = G
    return _GRAPHS[area_id]


# ---------------------------------------------------------------------------
# Geocoding — ported from streamlit_app (Photon primary, timed Nominatim
# fallback, no town ever appended; the bbox does the disambiguation).
# ---------------------------------------------------------------------------

def in_coverage(lat: float, lon: float, area: dict) -> bool:
    lon_min, lat_min, lon_max, lat_max = area["bbox"]
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


def _photon_label(props: dict) -> str | None:
    name = props.get("name")
    detail = ", ".join(p for p in (props.get("street"), props.get("city")) if p)
    if name and detail:
        return f"{name} · {detail}"
    return name or detail or None


@lru_cache(maxsize=1024)
def _geocode_query(q: str, area_id: str):
    """(lat, lon, label) within the area, or None. Never raises; every call has
    a hard timeout so geocoding can never hang a request."""
    area = AREAS[area_id]
    bbox = area["bbox"]
    bias_lat, bias_lon = area["bias"]
    try:
        resp = _requests.get(
            _PHOTON_URL,
            params={"q": q, "limit": 1, "lat": bias_lat, "lon": bias_lon,
                    "bbox": ",".join(str(v) for v in bbox)},
            headers=_GEO_HEADERS, timeout=8)
        resp.raise_for_status()
        feats = resp.json().get("features") or []
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"][:2]
            return (float(lat), float(lon), _photon_label(feats[0].get("properties", {})))
    except Exception:
        pass

    viewbox = f"{bbox[0]},{bbox[3]},{bbox[2]},{bbox[1]}"
    base = {"q": q, "format": "json", "limit": 1, "countrycodes": "us", "viewbox": viewbox}
    for bounded in (1, 0):
        try:
            resp = _requests.get(_NOMINATIM_URL, params={**base, "bounded": bounded},
                                 headers=_GEO_HEADERS, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            if data:
                label = ", ".join((data[0].get("display_name") or "").split(",")[:2]).strip()
                return (float(data[0]["lat"]), float(data[0]["lon"]), label or None)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Route serialization — graph-type-agnostic edge accessors (ported from
# streamlit_app) + per-block payload the frontend renders from.
# ---------------------------------------------------------------------------

def _route_edge_idx(r):
    return r.edge_indices if r.edge_indices else [None] * len(r.edges)


def _edge_data(G, u, v, key, eidx=None):
    return G.edge_view(eidx) if isinstance(G, RoutingGraph) else G[u][v][key]


def _edge_coords_lonlat(G, u, v, key, eidx=None):
    """Edge polyline as [lon, lat] pairs (GeoJSON order), either substrate."""
    if isinstance(G, RoutingGraph):
        geom = G.edge_geometry(eidx)
        if geom is not None:
            return [[float(lon), float(lat)] for lon, lat in geom]
        iu, iv = G.id_to_idx[u], G.id_to_idx[v]
        return [[float(G.node_x[iu]), float(G.node_y[iu])],
                [float(G.node_x[iv]), float(G.node_y[iv])]]
    d = G[u][v][key]
    geom = d.get("geometry")
    if geom is not None:
        coords = geom if hasattr(geom, "__array__") else geom.coords
        return [[float(lon), float(lat)] for lon, lat in coords]
    return [[G.nodes[u]["x"], G.nodes[u]["y"]], [G.nodes[v]["x"], G.nodes[v]["y"]]]


def _serialize_route(G, r, weights) -> dict:
    """One route as per-block segments + card stats. The frontend concatenates
    segment coords (skipping the shared joint vertex) into the full line, so
    geometry is sent once. Mirrors route_details + _route_geojson semantics."""
    segments = []
    worst_walk, worst_dist, cum = 1.0, 0.0, 0.0
    street_len: dict[str, float] = defaultdict(float)
    for (u, v, key), e in zip(r.edges, _route_edge_idx(r)):
        d = _edge_data(G, u, v, key, e)
        w, _ = edge_walkability(d, weights)
        length = _as_float(d.get("length")) or 0.0
        if w < worst_walk:
            worst_walk, worst_dist = w, cum
        cum += length
        name = d.get("name")
        if isinstance(name, list):
            name = name[0] if name else None
        name = _as_str(name)
        if name:
            street_len[name] += length
        coords = [[round(lon, 6), round(lat, 6)]
                  for lon, lat in _edge_coords_lonlat(G, u, v, key, e)]
        segments.append({
            "coords": coords,
            "score": round(w, 4),
            "highway": _as_str(d.get("highway")) or "path",
            "length_m": round(length, 1),
        })
    dominant = max(street_len, key=street_len.get) if street_len else None
    return {
        "score": round(r.walk_score, 4),
        "confidence": round(r.confidence, 4),
        "distance_m": round(r.total_length, 1),
        "crossings": r.crossing_count,
        "dimensions": {k: round(v, 4) for k, v in (r.dimension_scores or {}).items()},
        "via": dominant,
        "worst_score": round(worst_walk, 4),
        "worst_dist_m": round(worst_dist, 1),
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# App + endpoints
# ---------------------------------------------------------------------------

app = FastAPI(title="Humanpath PWA")
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.on_event("startup")
def _startup():
    get_graph(DEFAULT_AREA)


@app.get("/healthz")
def healthz():
    return {"ok": True, "graph_loaded": bool(_GRAPHS)}


@app.get("/api/config")
def api_config(area: str = DEFAULT_AREA):
    a = _area(area)
    return {
        "id": area,
        "label": a["label"],
        "bbox": a["bbox"],
        "center": [a["bias"][1], a["bias"][0]],  # [lon, lat] for MapLibre
        "covered": a["covered"],
        "default_from": a["from"],
        "default_to": a["to"],
        "style": a["style"],
        "alpha_max": ALPHA_MAX,
        "areas": [{"id": k, "label": v["label"]} for k, v in AREAS.items()],
    }


@app.get("/api/geocode")
def api_geocode(q: str = Query(..., min_length=1, max_length=200), area: str = DEFAULT_AREA):
    a = _area(area)
    hit = _geocode_query(q.strip(), area)
    if hit is None:
        raise HTTPException(404, f"Couldn't find “{q.strip()}”. Try a more specific address.")
    lat, lon, label = hit
    if not in_coverage(lat, lon, a):
        raise HTTPException(
            422, f"“{q.strip()}” looks outside the covered area. "
                 f"Humanpath currently covers {a['covered']}.")
    # `name` is the formal display name of the matched place (label minus the
    # street/city hint) — what the UI shows once the trip is committed.
    name = (label or "").split(" · ")[0].split(",")[0].strip() or None
    return {"lat": lat, "lon": lon, "label": label, "name": name}


@app.get("/api/reverse")
def api_reverse(lat: float, lon: float):
    """Nearby place label for a device location (best-effort; label may be null)."""
    label = None
    try:
        resp = _requests.get(_PHOTON_REVERSE_URL, params={"lat": lat, "lon": lon},
                             headers=_GEO_HEADERS, timeout=6)
        resp.raise_for_status()
        feats = resp.json().get("features") or []
        if feats:
            label = _photon_label(feats[0].get("properties", {}))
    except Exception:
        pass
    return {"label": label}


@app.get("/api/route")
def api_route(
    olat: float, olon: float, dlat: float, dlon: float,
    alpha: float = Query(2.0, ge=0.0, le=ALPHA_MAX),
    area: str = DEFAULT_AREA,
):
    a = _area(area)
    for which, lat, lon in (("start", olat, olon), ("destination", dlat, dlon)):
        if not in_coverage(lat, lon, a):
            raise HTTPException(
                422, f"The {which} looks outside the covered area. "
                     f"Humanpath currently covers {a['covered']}.")
    G = get_graph(area)
    routes = find_routes(G, (olat, olon), (dlat, dlon), alpha=alpha, weights=FACTOR_WEIGHTS)
    return {"routes": [_serialize_route(G, r, FACTOR_WEIGHTS) for r in routes]}


# Static frontend. The vendored MapLibre/PMTiles/basemaps libs are shared with
# the Streamlit component (single copy in the repo); the PWA shell lives in
# pwa/static. Root mount goes LAST so /api, /vendor, and /healthz win.
_VENDOR_DIR = _PROJECT_ROOT / "app" / "components" / "maplibre_map" / "frontend" / "vendor"
app.mount("/vendor", StaticFiles(directory=str(_VENDOR_DIR)), name="vendor")
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")
