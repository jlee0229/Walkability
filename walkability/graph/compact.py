"""
Slim, fast-loading runtime graph (Phase 1 of the RAM-reduction work).

The enriched GraphML (``boston_walk_enriched.graphml``, ~178 MB) loads to a
~2.7 GB-peak NetworkX graph in ~17 s: osmnx re-parses every attribute from
strings, keeps ~32 attributes per edge (most of them diagnostic), and inflates
~81 k edge geometries into shapely ``LineString`` objects.

This module produces a **runtime** graph that keeps only what the deployed app
and the router actually read at query time, with:

  * scores stored as native ``float`` (no per-query ``_as_float`` string parse),
  * geometries packed into ``float32`` ``(n, 2)`` arrays (lon, lat — same order
    as ``shapely.coords``) instead of shapely objects, and
  * every diagnostic / raw-OSM attribute dropped.

It is still an ordinary ``networkx.MultiDiGraph`` with the identical
``(u, v, key)`` topology, so routing/clip/router code is **unchanged** — the
only consumer that must learn the packed-geometry type is the app's
``_edge_coords`` (it now accepts an ndarray as well as a shapely geometry).

Persisted with ``pickle`` (loads in ~2 s, no string re-parse). The full
compact-array / CSR representation that clears the 1 GB host ceiling is Phase 2;
this is the cheap, parity-preserving milestone.

Runtime keep-set (everything else is dropped)
----------------------------------------------
Nodes:  ``y``, ``x`` (coords), ``highway`` (only set on ``highway=crossing``
        nodes; router counts crossings from it).
Edges:  ``length``, ``foot_access`` (routing); ``highway`` (router service-edge
        rule + map tooltip); ``name`` (route details); the per-factor scores and
        confidences ``edge_walkability`` reads; the baked ``walk_score`` /
        ``walk_confidence`` fast-path fields; and packed ``geometry``.
"""

from __future__ import annotations

import argparse
import pickle
import time
from pathlib import Path

import networkx as nx
import numpy as np

from walkability.config import OSM_DIR
from walkability.scoring.factors import _as_float, _as_str

# Float-valued edge fields, coerced from GraphML strings to native float at
# convert time so the query path never re-parses. None stays None (a missing
# factor must not be read as 0.0 — see scoring/factors.py).
RUNTIME_EDGE_FLOAT_FIELDS: tuple[str, ...] = (
    "length",
    "highway_score", "highway_confidence",
    "surface_score", "surface_confidence", "surface_material_score",
    "width_score",
    "environment_score", "environment_confidence",
    "walk_score", "walk_confidence",
)

# String/categorical edge fields kept verbatim (``highway`` may be a list).
RUNTIME_EDGE_STR_FIELDS: tuple[str, ...] = ("foot_access", "highway")

# Node attributes worth keeping. ``highway`` marks crossing nodes.
RUNTIME_NODE_FLOAT_FIELDS: tuple[str, ...] = ("y", "x")

RUNTIME_SUFFIX = ".runtime.pkl"


def runtime_path(graphml_path: Path) -> Path:
    """Sibling runtime-pickle path for a given enriched GraphML path."""
    p = Path(graphml_path)
    return p.with_name(p.stem + RUNTIME_SUFFIX)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _pack_geometry(geom) -> np.ndarray | None:
    """Pack a shapely LineString (or existing ndarray) into ``float32`` (n, 2).

    Coordinates stay in ``(lon, lat)`` order to match ``shapely.coords``, so the
    app's ``_edge_coords`` flips them the same way for either representation.
    """
    if geom is None:
        return None
    if isinstance(geom, np.ndarray):
        return geom.astype(np.float32, copy=False)
    coords = getattr(geom, "coords", None)
    if coords is None:
        return None
    arr = np.asarray(coords, dtype=np.float32)
    return arr if arr.size else None


def _clean_name(value):
    """Normalise the OSM ``name`` (sometimes a list) to a clean str or None."""
    if isinstance(value, list):
        value = value[0] if value else None
    return _as_str(value)


def to_runtime_graph(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """Build a slim ``MultiDiGraph`` holding only the runtime keep-set.

    Same nodes, same ``(u, v, key)`` edges and direction as ``G`` — so any
    routing result is identical — but every dropped/diagnostic attribute is
    gone, scores are native floats, and geometry is a packed ``float32`` array.
    """
    R = nx.MultiDiGraph()
    # Preserve graph-level metadata osmnx/clip may consult (e.g. crs); drop the
    # runtime caches clip.py memoises so a fresh load recomputes them cleanly.
    R.graph.update({
        k: v for k, v in G.graph.items()
        if not (isinstance(k, str) and k.startswith("_"))
    })

    for n, data in G.nodes(data=True):
        attrs = {}
        for f in RUNTIME_NODE_FLOAT_FIELDS:
            val = _as_float(data.get(f))
            if val is not None:
                attrs[f] = val
        hwy = _as_str(data.get("highway"))
        if hwy is not None:
            attrs["highway"] = hwy
        R.add_node(n, **attrs)

    for u, v, key, data in G.edges(keys=True, data=True):
        attrs: dict = {}
        for f in RUNTIME_EDGE_FLOAT_FIELDS:
            val = _as_float(data.get(f))
            if val is not None:
                attrs[f] = val
        for f in RUNTIME_EDGE_STR_FIELDS:
            if f in data and data[f] is not None:
                attrs[f] = data[f]            # highway may legitimately be a list
        name = _clean_name(data.get("name"))
        if name is not None:
            attrs["name"] = name
        geom = _pack_geometry(data.get("geometry"))
        if geom is not None:
            attrs["geometry"] = geom
        R.add_edge(u, v, key=key, **attrs)

    return R


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def save_runtime(G: nx.MultiDiGraph, path: Path) -> None:
    """Pickle a runtime graph (atomic: write to ``.part`` then rename)."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as f:
        pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def load_runtime(path: Path) -> nx.MultiDiGraph:
    """Load a runtime graph pickled by :func:`save_runtime`."""
    with open(path, "rb") as f:
        return pickle.load(f)


def build_runtime(src: Path, dst: Path | None = None) -> Path:
    """Convert an enriched GraphML at ``src`` to a runtime pickle.

    Returns the output path. Logs node/edge counts and on-disk sizes.
    """
    import osmnx as ox

    src = Path(src)
    dst = Path(dst) if dst is not None else runtime_path(src)

    t0 = time.time()
    G = ox.load_graphml(src)
    print(f"  loaded {src.name}: {G.number_of_nodes()} nodes, "
          f"{G.number_of_edges()} edges in {time.time() - t0:.1f}s")

    R = to_runtime_graph(G)
    save_runtime(R, dst)
    print(f"  wrote {dst.name}: {dst.stat().st_size / 1e6:.1f} MB "
          f"(source {src.stat().st_size / 1e6:.1f} MB)")
    return dst


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    from walkability.graph.build import (
        DEV_REGIONS,
        ENRICHED_PATH,
        dev_region_path,
    )

    ap = argparse.ArgumentParser(description="Build slim runtime graph pickles.")
    ap.add_argument("--dev", action="store_true", help="convert the dev subset(s)")
    ap.add_argument("--region", default=None, help="dev region name (implies --dev)")
    ap.add_argument("--all", action="store_true",
                    help="convert the full graph and every dev region")
    args = ap.parse_args()

    targets: list[Path] = []
    if args.all:
        targets.append(ENRICHED_PATH)
        targets += [dev_region_path(r) for r in DEV_REGIONS]
    elif args.region:
        targets.append(dev_region_path(args.region))
    elif args.dev:
        targets.append(dev_region_path("beacon_hill"))
    else:
        targets.append(ENRICHED_PATH)

    for src in targets:
        if not src.exists():
            print(f"  skip {src.name}: not found")
            continue
        build_runtime(src)


if __name__ == "__main__":
    _main()
