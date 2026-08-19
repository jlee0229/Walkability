"""
Phase 2 of the RAM-reduction work: a compact **CSR** runtime graph with *no
Python object per edge*.

Phase 1 (``compact.py``) slims the enriched GraphML into a runtime
``MultiDiGraph`` pickle (Boston ~0.45 GB peak / 0.5 s). The residual cost is
inherent to NetworkX: ~150–250 k per-edge dicts, ~90 k node dicts, a
dict-of-dict-of-dict adjacency, and ~80 k small shapely/ndarray geometry objects.
Metro-hull already peaks at ~0.63 GB and every added city shrinks the headroom.

This module replaces that substrate with flat numpy arrays in **CSR** layout
(``indptr`` + per-field columns), keyed by contiguous integer node indices:

  * one column array per edge field (no per-edge dict),
  * categoricals (``foot_access``, ``highway``) code-mapped to small int arrays,
  * ``name`` de-duped into a string table,
  * geometry concatenated into one ``float32 (P, 2)`` array + a ``geom_indptr``,
  * parallel edges kept as separate CSR rows in **source-adjacency order**, so
    the cheapest-parallel-edge projection and first-wins tie-break the router
    relies on are reproducible index-for-index.

Parity is the bar — routes must be **identical** to the Phase-1 runtime graph.
The de-risking move is that ``scoring/factors.py`` and ``routing/cost.py`` read an
edge purely through ``edge.get(field)`` and are already NetworkX-free; the
:class:`CsrEdge` flyweight below satisfies that duck type, so those modules stay
**unchanged** and scoring parity holds by construction. Only the graph-touching
routing layer (snap/clip/project/A*) is reimplemented CSR-native (see
``routing/`` accessors — a separate change).

Field precision (deliberate)
----------------------------
``length``, ``walk_score``, ``walk_confidence`` are kept **float64** — they feed
``edge_cost`` on the baked fast path (the common case), and a float32 round of
them vs the Phase-1 native floats could flip a near-tie A* argmin and break
*exact* path parity. The remaining factor columns are **float32**: they only
matter on the custom-weights (UI-slider) recompute path, where a float32-level
difference is immaterial to route choice, and any residual tie-flip is caught by
the parity harness's *identical node paths* assertion (not a silent risk).
Missing = ``NaN`` (a missing factor must never read as 0.0 — ``CsrEdge.get`` maps
NaN → None, matching an absent dict key).
"""

from __future__ import annotations

import hashlib
import math
import pickle
from pathlib import Path
from typing import Iterator

import networkx as nx
import numpy as np

from walkability.graph.compact import (
    RUNTIME_EDGE_FLOAT_FIELDS,
    _clean_name,
    _pack_geometry,
)
from walkability.scoring.factors import _as_float, _as_str

CSR_SUFFIX = ".csr.pkl"

# Cost-critical columns kept at full precision (see module docstring). Everything
# else in RUNTIME_EDGE_FLOAT_FIELDS is float32.
_F64_FIELDS: frozenset[str] = frozenset({"length", "walk_score", "walk_confidence"})

# Format version — bump on any layout change so a stale pickle is rejected loudly
# rather than mis-read.
CSR_FORMAT_VERSION: int = 1


def csr_path(graphml_path: Path) -> Path:
    """Sibling CSR-pickle path for an enriched GraphML / runtime path."""
    p = Path(graphml_path)
    # strip a trailing .runtime.pkl if handed the Phase-1 sibling
    name = p.name
    for suf in (".runtime.pkl", ".graphml"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    else:
        name = p.stem
    return p.with_name(name + CSR_SUFFIX)


# ---------------------------------------------------------------------------
# Highway display / service predicate
# ---------------------------------------------------------------------------

def _highway_display(value) -> str | None:
    """A stable display string for a possibly list-valued ``highway`` tag.

    Only feeds tooltips — routing uses the precomputed ``is_service`` mask, which
    is evaluated with the *identical* ``_as_str(value) == "service"`` predicate
    the router applies to the source graph, so a list-valued highway classifies
    the same way here as on the MultiDiGraph.
    """
    if isinstance(value, list):
        value = value[0] if value else None
    return _as_str(value)


# ---------------------------------------------------------------------------
# Source fingerprint (guards against comparing mismatched build snapshots)
# ---------------------------------------------------------------------------

def source_fingerprint(G: nx.MultiDiGraph) -> str:
    """Content hash of a MultiDiGraph over the fields that drive routing.

    Deterministic in the graph content (node/edge counts + per-edge
    ``walk_score``/``length`` in canonical ``(u, v, key)`` order), so a CSR graph
    built from one enriched snapshot and a runtime pickle built from another
    (e.g. after a ``--force`` rebuild) fingerprint differently. The parity harness
    refuses to compare across a mismatch — otherwise a stale pickle "passes" on
    the wrong data. Cheap enough at convert time; never on the query path.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(f"v{CSR_FORMAT_VERSION};n{G.number_of_nodes()};m{G.number_of_edges()};".encode())
    for u, v, k, d in sorted(
        G.edges(keys=True, data=True), key=lambda e: (str(e[0]), str(e[1]), e[2])
    ):
        ws = _as_float(d.get("walk_score"))
        length = _as_float(d.get("length"))
        h.update(f"{u}|{v}|{k}|{ws}|{length};".encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Edge flyweight — satisfies the edge.get(...) duck type of factors.py / cost.py
# ---------------------------------------------------------------------------

class CsrEdge:
    """A zero-copy view of one CSR edge, quacking like an edge-attribute dict.

    Implements ``get`` and ``items`` — the only edge-dict API ``scoring/factors``
    and ``routing/cost`` use — so those modules score a CSR edge unchanged. A
    single instance may be **reused** across A* relaxations by mutating ``idx``
    (no per-edge allocation on the hot path); callers that keep a reference past
    the next ``idx`` mutation must take a fresh view (``graph.edge_view``).
    """

    __slots__ = ("_g", "idx")

    def __init__(self, g: "RoutingGraph", idx: int = -1):
        self._g = g
        self.idx = idx

    def get(self, key, default=None):
        g = self._g
        e = self.idx
        arr = g.float_fields.get(key)
        if arr is not None:
            v = arr[e]
            return default if v != v else float(v)  # v != v is the NaN test
        if key == "foot_access":
            s = g.foot_access(e)
            return s if s is not None else default
        if key == "highway":
            s = g.highway_display(e)
            return s if s is not None else default
        if key == "name":
            s = g.name(e)
            return s if s is not None else default
        return default

    def items(self) -> Iterator[tuple[str, object]]:
        """Yield only the present (non-None) fields, mirroring the slim runtime
        MultiDiGraph edge dict (Phase 1 stores a key only when its value is not
        None). Consumed by ``_build_route``'s terminal-edge dict-comprehension."""
        g = self._g
        e = self.idx
        for name, arr in g.float_fields.items():
            v = arr[e]
            if v == v:  # not NaN
                yield name, float(v)
        s = g.foot_access(e)
        if s is not None:
            yield "foot_access", s
        h = g.highway_display(e)
        if h is not None:
            yield "highway", h
        nm = g.name(e)
        if nm is not None:
            yield "name", nm

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"CsrEdge(idx={self.idx}, {dict(self.items())})"


# ---------------------------------------------------------------------------
# The container
# ---------------------------------------------------------------------------

class RoutingGraph:
    """Compact CSR runtime graph (single instance; arrays hold all the memory).

    Node index space is ``0..N-1``; ``node_ids[i]`` is the original OSM id (kept
    so ``RouteResult.nodes``/``edges`` and the app/notebooks speak the same ids).
    Out-edges of node ``i`` are ``edges[indptr[i]:indptr[i+1]]``, stored in the
    source's adjacency order with parallel edges consecutive — the order
    ``G.out_edges(u, keys=True)`` yields, so the router's cheapest-parallel-edge
    projection + first-wins tie-break are reproducible.
    """

    def __init__(self):
        # nodes
        self.node_ids: np.ndarray = np.empty(0, dtype=np.int64)
        self.node_y: np.ndarray = np.empty(0, dtype=np.float64)
        self.node_x: np.ndarray = np.empty(0, dtype=np.float64)
        self.is_crossing: np.ndarray = np.empty(0, dtype=bool)
        self.id_to_idx: dict = {}
        # CSR edges
        self.indptr: np.ndarray = np.zeros(1, dtype=np.int64)
        self.dst: np.ndarray = np.empty(0, dtype=np.int32)
        self.edge_src: np.ndarray = np.empty(0, dtype=np.int32)
        self.edge_key: np.ndarray = np.empty(0, dtype=np.int32)
        # edge float columns (NaN = missing)
        self.float_fields: dict[str, np.ndarray] = {}
        # categoricals
        self.foot_codes: np.ndarray = np.empty(0, dtype=np.int8)
        self.foot_vocab: list[str] = []
        self.highway_codes: np.ndarray = np.empty(0, dtype=np.int16)
        self.highway_vocab: list[str] = []
        self.is_service: np.ndarray = np.empty(0, dtype=bool)
        self.name_codes: np.ndarray = np.empty(0, dtype=np.int32)
        self.name_vocab: list[str] = []
        # geometry (concatenated float32 (P,2) lon/lat + per-edge offsets)
        self.geom_xy: np.ndarray = np.empty((0, 2), dtype=np.float32)
        self.geom_indptr: np.ndarray = np.zeros(1, dtype=np.int64)
        # metadata
        self.graph_meta: dict = {}
        self.fingerprint: str = ""
        self.format_version: int = CSR_FORMAT_VERSION

    # -- sizes --------------------------------------------------------------
    def num_nodes(self) -> int:
        return int(self.node_ids.shape[0])

    def num_edges(self) -> int:
        return int(self.dst.shape[0])

    # -- edge access --------------------------------------------------------
    def out_edge_range(self, u_idx: int) -> tuple[int, int]:
        """(start, end) edge-index slice of node ``u_idx``'s out-edges."""
        return int(self.indptr[u_idx]), int(self.indptr[u_idx + 1])

    def edge_view(self, e: int) -> CsrEdge:
        """A fresh :class:`CsrEdge` for edge index ``e`` (safe to retain)."""
        return CsrEdge(self, e)

    def edge_endpoints(self, e: int) -> tuple:
        """Original ``(u_id, v_id, key)`` for edge index ``e``."""
        return (
            int(self.node_ids[self.edge_src[e]]),
            int(self.node_ids[self.dst[e]]),
            int(self.edge_key[e]),
        )

    def edge_geometry(self, e: int) -> np.ndarray | None:
        """Packed ``float32 (n, 2)`` (lon, lat) polyline, or None if absent."""
        a, b = int(self.geom_indptr[e]), int(self.geom_indptr[e + 1])
        if b <= a:
            return None
        return self.geom_xy[a:b]

    # -- categorical decoders ----------------------------------------------
    def foot_access(self, e: int) -> str | None:
        c = int(self.foot_codes[e])
        return self.foot_vocab[c - 1] if c > 0 else None

    def highway_display(self, e: int) -> str | None:
        c = int(self.highway_codes[e])
        return self.highway_vocab[c - 1] if c > 0 else None

    def name(self, e: int) -> str | None:
        c = int(self.name_codes[e])
        return self.name_vocab[c - 1] if c > 0 else None

    # -- construction -------------------------------------------------------
    @classmethod
    def from_networkx(cls, G: nx.MultiDiGraph) -> "RoutingGraph":
        """Build a CSR graph from a (slim runtime or enriched) MultiDiGraph.

        Reuses Phase-1's keep-set + coercions (``RUNTIME_EDGE_FLOAT_FIELDS``,
        ``_pack_geometry``, ``_clean_name``) so the two runtimes never diverge on
        what a field means.
        """
        self = cls()

        node_ids = list(G.nodes())
        n = len(node_ids)
        self.node_ids = np.asarray(node_ids, dtype=np.int64)
        self.id_to_idx = {nid: i for i, nid in enumerate(node_ids)}
        self.node_y = np.empty(n, dtype=np.float64)
        self.node_x = np.empty(n, dtype=np.float64)
        self.is_crossing = np.zeros(n, dtype=bool)
        for i, nid in enumerate(node_ids):
            d = G.nodes[nid]
            self.node_y[i] = _as_float(d.get("y"))
            self.node_x[i] = _as_float(d.get("x"))
            self.is_crossing[i] = _as_str(d.get("highway")) == "crossing"

        # Edges, grouped by source in adjacency order (CSR-ordered by construction
        # because we iterate nodes in index order and append).
        counts = np.zeros(n, dtype=np.int64)
        dst_list: list[int] = []
        src_list: list[int] = []
        key_list: list[int] = []
        float_cols: dict[str, list[float]] = {f: [] for f in RUNTIME_EDGE_FLOAT_FIELDS}
        foot_raw: list[str | None] = []
        hwy_raw: list[object] = []
        name_raw: list[str | None] = []
        geom_list: list[np.ndarray | None] = []

        for i, nid in enumerate(node_ids):
            for _u, v, key, data in G.out_edges(nid, keys=True, data=True):
                counts[i] += 1
                dst_list.append(self.id_to_idx[v])
                src_list.append(i)
                key_list.append(int(key))
                for f in RUNTIME_EDGE_FLOAT_FIELDS:
                    val = _as_float(data.get(f))
                    float_cols[f].append(np.nan if val is None else val)
                foot_raw.append(_as_str(data.get("foot_access")))
                hwy_raw.append(data.get("highway"))
                name_raw.append(_clean_name(data.get("name")))
                geom_list.append(_pack_geometry(data.get("geometry")))

        self.indptr = np.empty(n + 1, dtype=np.int64)
        self.indptr[0] = 0
        np.cumsum(counts, out=self.indptr[1:])
        self.dst = np.asarray(dst_list, dtype=np.int32)
        self.edge_src = np.asarray(src_list, dtype=np.int32)
        self.edge_key = np.asarray(key_list, dtype=np.int32)

        for f, col in float_cols.items():
            dtype = np.float64 if f in _F64_FIELDS else np.float32
            self.float_fields[f] = np.asarray(col, dtype=dtype)

        # foot_access categorical (code 0 = None/absent)
        self.foot_vocab = sorted({s for s in foot_raw if s is not None})
        foot_index = {s: i + 1 for i, s in enumerate(self.foot_vocab)}
        self.foot_codes = np.asarray(
            [foot_index.get(s, 0) for s in foot_raw], dtype=np.int8
        )

        # highway: service mask via the identical router predicate, + display code
        self.is_service = np.asarray(
            [_as_str(h) == "service" for h in hwy_raw], dtype=bool
        )
        hwy_disp = [_highway_display(h) for h in hwy_raw]
        self.highway_vocab = sorted({s for s in hwy_disp if s is not None})
        hwy_index = {s: i + 1 for i, s in enumerate(self.highway_vocab)}
        self.highway_codes = np.asarray(
            [hwy_index.get(s, 0) for s in hwy_disp], dtype=np.int16
        )

        # name string table
        self.name_vocab = sorted({s for s in name_raw if s is not None})
        name_index = {s: i + 1 for i, s in enumerate(self.name_vocab)}
        self.name_codes = np.asarray(
            [name_index.get(s, 0) for s in name_raw], dtype=np.int32
        )

        # geometry — concatenate, per-edge offsets
        geom_lens = np.asarray(
            [0 if g is None else len(g) for g in geom_list], dtype=np.int64
        )
        self.geom_indptr = np.empty(len(geom_list) + 1, dtype=np.int64)
        self.geom_indptr[0] = 0
        np.cumsum(geom_lens, out=self.geom_indptr[1:])
        total = int(self.geom_indptr[-1])
        self.geom_xy = np.empty((total, 2), dtype=np.float32)
        for e, g in enumerate(geom_list):
            if g is not None and len(g):
                self.geom_xy[self.geom_indptr[e]: self.geom_indptr[e + 1]] = g

        # preserve non-underscore graph metadata (crs, etc.)
        self.graph_meta = {
            k: v for k, v in G.graph.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
        self.fingerprint = source_fingerprint(G)
        return self


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def save_csr(g: RoutingGraph, path: Path) -> None:
    """Pickle a CSR graph (atomic: write to ``.part`` then rename)."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as f:
        pickle.dump(g, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def load_csr(path: Path) -> RoutingGraph:
    """Load a CSR graph; reject a stale/foreign format version loudly."""
    with open(path, "rb") as f:
        g = pickle.load(f)
    if getattr(g, "format_version", None) != CSR_FORMAT_VERSION:
        raise ValueError(
            f"{path}: CSR format v{getattr(g, 'format_version', '?')} != "
            f"expected v{CSR_FORMAT_VERSION}; re-run `compact --csr`."
        )
    return g


# ---------------------------------------------------------------------------
# Convert-time validation (a [verify] for the categorical/precision risks)
# ---------------------------------------------------------------------------

def validate_csr(g: RoutingGraph, G: nx.MultiDiGraph) -> dict:
    """Assert the CSR round-trips the source MultiDiGraph, edge for edge.

    Checks (raising ``AssertionError`` on the first failure):
      * node/edge counts + fingerprint,
      * every edge's ``(u, v, key)`` reconstruction indexes the source edge,
      * ``foot_access`` / ``highway`` decode to the same ``_as_str`` value, and
        ``is_service`` matches the router's exact predicate,
      * every float field equals the source (NaN ↔ None), within a tiny float32
        tolerance for the downcast columns.

    Returns a per-field NaN-rate dict (for the CLI to log — e.g. Austin's
    ``width_score`` should be ~100% NaN, a guard against a silent 0.0-fill).
    """
    assert g.num_nodes() == G.number_of_nodes(), "node count mismatch"
    assert g.num_edges() == G.number_of_edges(), "edge count mismatch"
    assert g.fingerprint == source_fingerprint(G), "fingerprint mismatch"

    m = g.num_edges()
    nan_counts = {f: 0 for f in g.float_fields}
    for e in range(m):
        u = int(g.node_ids[g.edge_src[e]])
        v = int(g.node_ids[g.dst[e]])
        k = int(g.edge_key[e])
        src = G[u][v][k]  # KeyError here => (u,v,key) reconstruction is wrong

        # categoricals
        assert g.foot_access(e) == _as_str(src.get("foot_access")), (
            f"foot_access decode mismatch at edge {e} ({u},{v},{k})"
        )
        assert bool(g.is_service[e]) == (_as_str(src.get("highway")) == "service"), (
            f"is_service mismatch at edge {e} ({u},{v},{k})"
        )
        assert g.highway_display(e) == _highway_display(src.get("highway")), (
            f"highway display mismatch at edge {e} ({u},{v},{k})"
        )

        # float columns
        for f, arr in g.float_fields.items():
            got = arr[e]
            want = _as_float(src.get(f))
            if want is None:
                assert got != got, f"{f} should be NaN at edge {e} ({u},{v},{k})"
                nan_counts[f] += 1
            else:
                assert got == got, f"{f} unexpectedly NaN at edge {e} ({u},{v},{k})"
                tol = 1e-9 if f in _F64_FIELDS else 1e-4 * (1.0 + abs(want))
                assert abs(float(got) - want) <= tol, (
                    f"{f} value drift at edge {e} ({u},{v},{k}): {got} vs {want}"
                )

    # geometry: totals + a fidelity spot check on the first geometried edge
    for e in range(m):
        u = int(g.node_ids[g.edge_src[e]])
        v = int(g.node_ids[g.dst[e]])
        k = int(g.edge_key[e])
        src_geom = _pack_geometry(G[u][v][k].get("geometry"))
        got_geom = g.edge_geometry(e)
        if src_geom is None:
            assert got_geom is None, f"geometry should be absent at edge {e}"
        else:
            assert got_geom is not None and got_geom.shape == src_geom.shape, (
                f"geometry shape mismatch at edge {e}"
            )
            assert np.allclose(got_geom, src_geom, atol=1e-3), (
                f"geometry drift at edge {e}"
            )
            break  # one positive spot check is enough (packing is deterministic)

    return {f: nan_counts[f] / m if m else 0.0 for f in nan_counts}


def build_csr(src: Path, dst: Path | None = None) -> tuple[Path, dict]:
    """Convert an enriched GraphML (or its runtime pickle) at ``src`` to a CSR
    pickle, validate the round-trip, and return ``(out_path, nan_rates)``.

    Prefers the Phase-1 ``*.runtime.pkl`` sibling as the source (loads in ~0.5 s
    vs ~17 s for GraphML) since the CSR keep-set is a subset of the runtime one.
    """
    from walkability.graph.compact import load_runtime, runtime_path

    src = Path(src)
    dst = Path(dst) if dst is not None else csr_path(src)

    rt = runtime_path(src)
    if rt.exists():
        G = load_runtime(rt)
    else:
        import osmnx as ox
        G = ox.load_graphml(src)

    g = RoutingGraph.from_networkx(G)
    nan_rates = validate_csr(g, G)
    save_csr(g, dst)
    return dst, nan_rates
