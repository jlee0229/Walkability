"""
Calibration survey generator — expand the ground-truth set for weight tuning.

Runs a hand-picked set of routes spanning Boston's walkability spectrum (walkable
historic core → leafy boulevards → dense commercial → industrial arterials →
car-dominated squares) on the FULL enriched graph, and writes a single
self-contained HTML page with ONE CARD PER ROUTE. Each card has:

  * a zoomed-in map of just that route, with every SEGMENT (a run of one street)
    drawn and numbered, coloured by its model walk_score, hover/click for detail;
  * the model's per-DIMENSION verdict — safety / comfort / path (via
    edge_category_scores) — plus distance, walk-time, audit flags;
  * a segment table (street, length, walk, surface/SCI, source) so factual
    problems can be reported by segment number; and
  * the calibration QUESTIONS to answer, with Street View links.

The per-dimension breakdown lets a human say not just "this route is worse than
the score" but *which dimension* the model mis-weighted — exactly what tuning
CATEGORY_WEIGHTS / CATEGORY_FLOOR needs. The numbered segments make the
"anything factually wrong?" question answerable block by block.

    python notebooks/calibration_survey.py
    python notebooks/calibration_survey.py --out notebooks/calibration_survey.html
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path

from walkability.graph.build import ENRICHED_PATH, load_graph
from walkability.routing.cost import ALPHA_DEFAULT
from walkability.routing.router import find_routes
from walkability.scoring.factors import (
    edge_category_scores,
    edge_walkability,
    _as_float,
    _as_str,
)

# diagnostics.py sits next to this file (on sys.path[0] when run as a script).
from diagnostics import audit_route, streetview_url

WALK_SPEED_MPS = 1.4   # ~5 km/h, for walk-time
ALPHAS = (0.0, 2.0, 5.0)
CATS = ("safety", "comfort", "path")
SEG_MAX_M = 130.0      # cap a merged segment so long streets split into blocks

# ---------------------------------------------------------------------------
# Representative Boston routes — diverse neighbourhoods across the spectrum.
# Coordinates are approximate; find_routes snaps to the nearest routable node.
# ---------------------------------------------------------------------------
SURVEY_ROUTES: list[dict] = [
    {
        "name": "beacon_hill_charles_to_louisburg",
        "area": "Beacon Hill (Charles St → Louisburg Sq)",
        "origin": (42.3592, -71.0707), "dest": (42.3582, -71.0696),
        "look_for": "Walkable historic core: narrow brick/cobble lanes. Does the model rate it high? Is brick over/under-rated for comfort?",
    },
    {
        "name": "back_bay_comm_ave_mall",
        "area": "Back Bay (Commonwealth Ave Mall)",
        "origin": (42.3522, -71.0762), "dest": (42.3497, -71.0840),
        "look_for": "Leafy tree-lined boulevard with a pedestrian mall. Should be near the top — greenery isn't a factor yet, so check if it scores lower than it deserves.",
    },
    {
        "name": "north_end_hanover",
        "area": "North End (Hanover St)",
        "origin": (42.3630, -71.0561), "dest": (42.3656, -71.0533),
        "look_for": "Dense, lively, shops everywhere, narrow. Eyes-on-street should be high. Does high 'eyes' + narrow streets score correctly?",
    },
    {
        "name": "south_end_tremont_shops",
        "area": "South End (Tremont St)",
        "origin": (42.3414, -71.0755), "dest": (42.3447, -71.0703),
        "look_for": "Your stated ideal: residential brownstones with shops dotted throughout. Calm + watched. Should score high — does it?",
    },
    {
        "name": "newmarket_mass_ave_industrial",
        "area": "Newmarket / Mass Ave (industrial)",
        "origin": (42.3345, -71.0685), "dest": (42.3318, -71.0640),
        "look_for": "Industrial arterials, pristine surfaces but hostile. The bug case — should now score LOW (safety floors it). Is it low enough?",
    },
    {
        "name": "nubian_roxbury_washington",
        "area": "Nubian Sq / Roxbury (Washington St)",
        "origin": (42.3290, -71.0830), "dest": (42.3270, -71.0792),
        "look_for": "High arterial exposure. Mixed commercial but busy roads. Is the safety penalty right?",
    },
    {
        "name": "charlestown_sullivan_sq",
        "area": "Charlestown / Sullivan Sq",
        "origin": (42.3820, -71.0710), "dest": (42.3858, -71.0688),
        "look_for": "Car-dominated, wide roads, many crossings. Should score low. Are crossings (unscored!) making it feel worse than the number?",
    },
    {
        "name": "seaport_congress",
        "area": "Seaport (Congress St)",
        "origin": (42.3510, -71.0445), "dest": (42.3492, -71.0388),
        "look_for": "New, wide sidewalks (good comfort) but big hostile crossings and car traffic. Does good comfort over-inflate a car-hostile area?",
    },
    {
        "name": "downtown_crossing_financial",
        "area": "Downtown Crossing → Financial District",
        "origin": (42.3556, -71.0601), "dest": (42.3581, -71.0558),
        "look_for": "Dense urban, some pedestrianized, some busy. Mixed. Check the model handles the pedestrian streets vs the traffic streets.",
    },
    {
        "name": "jp_centre_st",
        "area": "Jamaica Plain (Centre St)",
        "origin": (42.3169, -71.1099), "dest": (42.3138, -71.1131),
        "look_for": "Neighbourhood main street: shops + residential, moderate traffic, far from highways. A 'good but not perfect' calibration anchor.",
    },
]


# ---------------------------------------------------------------------------
# Geometry / formatting helpers
# ---------------------------------------------------------------------------

def _edge_coords(G, u, v, key):
    d = G[u][v][key]
    geom = d.get("geometry")
    if geom is not None:
        return [(lat, lon) for lon, lat in geom.coords]
    return [(G.nodes[u]["y"], G.nodes[u]["x"]), (G.nodes[v]["y"], G.nodes[v]["x"])]


def _route_coords(G, route):
    coords: list[tuple[float, float]] = []
    for u, v, key in route.edges:
        ec = _edge_coords(G, u, v, key)
        coords.extend(ec if not coords else ec[1:])
    return coords


def _score_color(s: float) -> str:
    """Red (0) → amber (0.5) → green (1)."""
    s = max(0.0, min(1.0, s))
    if s < 0.5:
        g = min(220, int(120 + 200 * (s / 0.5)))
        return f"rgb(220,{g},60)"
    r = max(40, int(220 - 200 * ((s - 0.5) / 0.5)))
    return f"rgb({r},180,60)"


def _route_segments(G, route) -> list[dict]:
    """Merge the route's edges into human 'segments' = consecutive runs of one street.

    Each segment carries a length-weighted walk_score and the representative
    (longest edge's) surface/source attributes, so a block can be judged and
    reported by number.
    """
    raw = []
    for u, v, key in route.edges:
        d = G[u][v][key]
        raw.append({
            "coords": _edge_coords(G, u, v, key),
            "name": _as_str(d.get("name")),
            "length": _as_float(d.get("length")) or 0.0,
            "walk": edge_walkability(d)[0],
            "highway": _as_str(d.get("highway")),
            "surface_score": _as_float(d.get("surface_score")),
            "material": _as_float(d.get("surface_material_score")),
            "sci": _as_float(d.get("sidewalk_condition")),
            "env": _as_float(d.get("environment_score")),
            "foot": _as_str(d.get("foot_access")),
            "source": _as_str(d.get("data_source")),
        })

    # Group consecutive edges of the SAME street, but cap each block at SEG_MAX_M
    # so a long avenue splits into walkable-sized segments (and trivial OSM
    # micro-splits at intersections still collapse).
    groups: list[list[dict]] = []
    for e in raw:
        prev = groups[-1] if groups else None
        if (prev and prev[0]["name"] == e["name"]
                and sum(x["length"] for x in prev) < SEG_MAX_M):
            prev.append(e)
        else:
            groups.append([e])

    out = []
    for i, es in enumerate(groups, start=1):
        coords = list(es[0]["coords"])
        for e in es[1:]:
            tail = e["coords"]
            coords.extend(tail[1:] if coords and coords[-1] == tail[0] else tail)
        L = sum(e["length"] for e in es) or 1.0
        rep = max(es, key=lambda e: e["length"])
        out.append({
            "i": i,
            "name": es[0]["name"] or "(unnamed path)",
            "coords": coords,
            "mid": coords[len(coords) // 2],
            "length": sum(e["length"] for e in es),
            "walk": sum(e["walk"] * e["length"] for e in es) / L,
            "highway": rep["highway"],
            "surface_score": rep["surface_score"],
            "material": rep["material"],
            "sci": rep["sci"],
            "env": rep["env"],
            "foot": rep["foot"],
            "source": rep["source"],
        })
    return out


def _route_category_means(G, route) -> dict[str, float]:
    acc = {c: 0.0 for c in CATS}
    wsum = {c: 0.0 for c in CATS}
    for u, v, key in route.edges:
        d = G[u][v][key]
        L = _as_float(d.get("length")) or 0.0
        for c, val in edge_category_scores(d).items():
            if c in acc:
                acc[c] += val * L
                wsum[c] += L
    return {c: (acc[c] / wsum[c]) for c in CATS if wsum[c] > 0}


def _alpha_moves(G, origin, dest) -> bool:
    fps = set()
    for a in ALPHAS:
        rs = find_routes(G, tuple(origin), tuple(dest), alpha=a)
        if rs:
            fps.add(tuple(rs[0].nodes))
    return len(fps) > 1


def _survey(G, case: dict) -> dict:
    routes = find_routes(G, tuple(case["origin"]), tuple(case["dest"]), alpha=ALPHA_DEFAULT)
    if not routes:
        return {**case, "found": False}
    best = routes[0]
    segs = _route_segments(G, best)
    return {
        **case,
        "found": True,
        "coords": _route_coords(G, best),
        "segments": segs,
        "walk": best.walk_score,
        "confidence": best.confidence,
        "length_m": best.total_length,
        "minutes": best.total_length / WALK_SPEED_MPS / 60.0,
        "categories": _route_category_means(G, best),
        "audit": audit_route(G, best, alpha=ALPHA_DEFAULT),
        "alpha_moves": _alpha_moves(G, case["origin"], case["dest"]),
        "worst": sorted(segs, key=lambda s: s["walk"])[:2],
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _bar(label: str, val: float) -> str:
    pct = round(val * 100)
    return (f'<div class="bar"><span class="bl">{label}</span>'
            f'<span class="bt"><span class="bf" style="width:{pct}%;'
            f'background:{_score_color(val)}"></span></span>'
            f'<span class="bv">{val:.2f}</span></div>')


def _gmaps_route(origin, dest) -> str:
    return (f"https://www.google.com/maps/dir/?api=1&travelmode=walking"
            f"&origin={origin[0]},{origin[1]}&destination={dest[0]},{dest[1]}")


def _fmt(v, nd=2) -> str:
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


def _seg_popup(s: dict) -> str:
    return (f"<b>#{s['i']} {html.escape(s['name'])}</b><br>"
            f"walk <b>{s['walk']:.2f}</b> · {s['length']:.0f} m<br>"
            f"highway: {html.escape(str(s['highway']))}<br>"
            f"surface {_fmt(s['surface_score'])} · material {_fmt(s['material'])} · "
            f"SCI {_fmt(s['sci'],0)}<br>"
            f"environment {_fmt(s['env'])} · access {html.escape(str(s['foot']))}<br>"
            f"source: {html.escape(str(s['source']))}<br>"
            f"<a href='{streetview_url(*s['mid'])}' target='_blank'>Street View</a>")


def _route_map_html(r: dict) -> str:
    import folium
    fig = folium.Figure(height=400)
    fmap = folium.Map(tiles="cartodbpositron", control_scale=True)
    fig.add_child(fmap)

    lats = [c[0] for c in r["coords"]]
    lons = [c[1] for c in r["coords"]]
    fmap.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(25, 25))

    for s in r["segments"]:
        col = _score_color(s["walk"])
        folium.PolyLine(
            s["coords"], color=col, weight=7, opacity=0.85,
            tooltip=f"#{s['i']} {s['name']} — walk {s['walk']:.2f}",
            popup=folium.Popup(_seg_popup(s), max_width=260),
        ).add_to(fmap)
        folium.map.Marker(
            s["mid"],
            icon=folium.DivIcon(
                icon_size=(20, 20), icon_anchor=(10, 10),
                html=f'<div class="segpin">{s["i"]}</div>'),
        ).add_to(fmap)

    folium.CircleMarker(r["coords"][0], radius=6, color="#1a7", fill=True,
                        fill_opacity=1, tooltip="start").add_to(fmap)
    folium.CircleMarker(r["coords"][-1], radius=6, color="#b33", fill=True,
                        fill_opacity=1, tooltip="end").add_to(fmap)
    return fig._repr_html_()


def _seg_table(r: dict) -> str:
    rows = "".join(
        f"<tr><td class='c'>{s['i']}</td><td>{html.escape(s['name'])}</td>"
        f"<td class='r'>{s['length']:.0f}</td>"
        f"<td class='r' style='color:{_score_color(s['walk'])};font-weight:600'>{s['walk']:.2f}</td>"
        f"<td>{html.escape(str(s['highway']) if s['highway'] else '—')}</td>"
        f"<td class='r'>{_fmt(s['surface_score'])}</td>"
        f"<td class='r'>{_fmt(s['sci'],0)}</td>"
        f"<td class='r'>{_fmt(s['env'])}</td>"
        f"<td class='src'>{html.escape((s['source'] or '—').replace('city_inventory','city').replace('highway=','osm:'))}</td></tr>"
        for s in r["segments"]
    )
    return (
        "<table class='seg'><thead><tr>"
        "<th>#</th><th>street</th><th>m</th><th>walk</th><th>highway</th>"
        "<th>surf</th><th>SCI</th><th>env</th><th>source</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>")


CSS = """
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:980px;margin:0 auto;padding:24px;color:#222;background:#faf8f4}
h1{font-size:26px;margin:0 0 4px} h2{font-size:19px;margin:0}
.sub{color:#666;margin:0 0 18px}
.intro{background:#fff;border:1px solid #e6e0d6;border-radius:10px;padding:16px 18px;margin:0 0 22px}
.intro ol{margin:8px 0 0;padding-left:20px} .intro li{margin:4px 0}
.card{background:#fff;border:1px solid #e6e0d6;border-radius:10px;padding:16px 18px;margin:0 0 22px}
.card h2 .n{display:inline-block;width:26px;height:26px;line-height:26px;text-align:center;background:#c75b39;color:#fff;border-radius:50%;font-size:14px;margin-right:8px}
.look{color:#555;font-style:italic;margin:6px 0 12px}
.panel{display:flex;gap:22px;flex-wrap:wrap;align-items:center;background:#f7f4ee;border-radius:8px;padding:12px 14px;margin:0 0 12px}
.big{font-size:30px;font-weight:700} .big small{font-size:13px;font-weight:400;color:#777}
.bars{flex:1;min-width:240px}
.bar{display:flex;align-items:center;gap:8px;margin:3px 0}
.bl{width:58px;color:#555;font-size:13px} .bv{width:34px;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}
.bt{flex:1;height:11px;background:#e7e2d8;border-radius:6px;overflow:hidden} .bf{display:block;height:100%}
.mapwrap{margin:0 0 10px;border-radius:8px;overflow:hidden;border:1px solid #e6e0d6}
.meta{font-size:13px;color:#555;margin:8px 0 8px} .meta b{color:#222}
.flags{color:#b3501f}
.links a{margin-right:14px;font-size:13px}
table.seg{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0 4px}
table.seg th{text-align:left;color:#777;font-weight:600;border-bottom:1px solid #ddd;padding:3px 6px}
table.seg td{border-bottom:1px solid #f0ece3;padding:3px 6px} table.seg td.r{text-align:right;font-variant-numeric:tabular-nums} table.seg td.c{text-align:center;color:#777} td.src{color:#888}
details.segs{margin:4px 0 0} details.segs summary{cursor:pointer;color:#666;font-size:13px}
.q{margin:12px 0 0;padding-top:10px;border-top:1px dashed #ddd}
.q ol{margin:6px 0 0;padding-left:20px} .q li{margin:5px 0}
.tmpl{background:#2c2a26;color:#ede7da;border-radius:8px;padding:10px 12px;margin:12px 0 0;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap}
.segpin{background:#2c2a26;color:#fff;border-radius:50%;width:20px;height:20px;line-height:20px;text-align:center;font:bold 11px sans-serif;border:2px solid #fff;box-shadow:0 1px 2px rgba(0,0,0,.5)}
.miss{color:#b33}
"""

QUESTIONS = [
    "Overall, how walkable is this route, 1–5? (5 = great)",
    "The model scored it {score}/100. Is that too HIGH, about RIGHT, or too LOW?",
    "If it feels off, WHICH dimension is to blame? — cars/traffic (safety), feels unsafe/empty (safety-eyes), surface/width (comfort), \"not a real walking route / on a road\" (path), or none. (Model: safety {safety}, comfort {comfort}, path {path}.)",
    "Is this the route you'd actually walk between these points? If not, what's wrong (zig-zags / avoids a nicer street / takes a busy road)?",
    "Anything factually wrong on the ground? Name the SEGMENT # (from the map/table) and what's off — a sidewalk that isn't there, a surface mis-rated, a bad crossing.",
]


def _card(i: int, r: dict) -> str:
    if not r.get("found"):
        return (f'<div class="card"><h2><span class="n">{i}</span>{html.escape(r["area"])}</h2>'
                f'<p class="miss">No route resolved between these points — I will adjust the endpoints. '
                f'(origin {r["origin"]}, dest {r["dest"]})</p></div>')
    cats = r["categories"]
    s = cats.get("safety", float("nan")); c = cats.get("comfort", float("nan")); p = cats.get("path", float("nan"))
    flags = r["audit"].get("flags", [])
    sv_links = " ".join(
        f'<a href="{streetview_url(*w["mid"])}" target="_blank">worst seg #{w["i"]} ({w["walk"]:.2f})</a>'
        for w in r["worst"]
    )
    qs = "".join(
        "<li>" + html.escape(q).format(
            score=f"{r['walk']*100:.0f}", safety=_fmt(s), comfort=_fmt(c), path=_fmt(p)
        ) + "</li>"
        for q in QUESTIONS
    )
    tmpl = html.escape(
        f"[{i}] {r['area']}\n"
        f"  walkable_1to5: \n"
        f"  model_score_too: high|right|low\n"
        f"  worst_dimension: safety|safety-eyes|comfort|path|none\n"
        f"  would_you_walk_it: yes|no -> \n"
        f"  ground_truth_wrong (seg #): \n"
    )
    return f"""<div class="card">
  <h2><span class="n">{i}</span>{html.escape(r['area'])}</h2>
  <p class="look">{html.escape(r['look_for'])}</p>
  <div class="mapwrap">{_route_map_html(r)}</div>
  <div class="panel">
    <div><div class="big">{r['walk']*100:.0f}<small>/100</small></div></div>
    <div class="bars">{_bar('safety', s)}{_bar('comfort', c)}{_bar('path', p)}</div>
  </div>
  <p class="meta"><b>{r['length_m']:.0f} m</b> · ~{r['minutes']:.0f} min ·
     confidence {r['confidence']:.2f} · alpha moves path: <b>{'yes' if r['alpha_moves'] else 'no'}</b>
     {'· <span class="flags">flags: '+html.escape(', '.join(flags))+'</span>' if flags else ''}</p>
  <p class="links"><b>Look:</b>
     <a href="{streetview_url(*r['coords'][0])}" target="_blank">Street View (start)</a>
     <a href="{_gmaps_route(r['origin'], r['dest'])}" target="_blank">Google walking route</a>
     {sv_links}</p>
  <details class="segs"><summary>{len(r['segments'])} segments — table (numbers match the map pins)</summary>
     {_seg_table(r)}</details>
  <div class="q"><b>Questions</b><ol>{qs}</ol>
    <div class="tmpl">{tmpl}</div></div>
</div>"""


def build_html(results: list[dict]) -> str:
    cards = "\n".join(_card(i, r) for i, r in enumerate(results, start=1))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Walkability calibration survey</title><style>{CSS}</style></head><body>
<h1>Walkability calibration survey</h1>
<p class="sub">{len(results)} routes across Boston · model = the new HDI-style two-level score.
Answer per route; replies tune CATEGORY_WEIGHTS / CATEGORY_FLOOR and surface data problems.</p>
<div class="intro"><b>How to read each card</b>
<ol>
<li><b>Map</b>: the route, each <b>segment</b> (one street) drawn + numbered and coloured by its walk_score (red→green). Hover or click a segment for detail + Street View; the numbers match the segment table.</li>
<li><b>Big number</b> = the route's overall walk_score (0–100).</li>
<li><b>Bars</b> = the three dimensions (length-weighted): <b>safety</b> (cars + eyes-on-street), <b>comfort</b> (surface/material/width), <b>path</b> (is it a real walking right-of-way). The score is a weighted geometric mean of them (currently safety 1.4 : path 1.0 : comfort 0.6).</li>
</ol>
<b>Two global questions first:</b>
<ol>
<li>Rank the three dimensions by how much they matter to YOU: safety, comfort, path legitimacy.</li>
<li>Across all routes, is the model systematically too high, too low, or about right?</li>
</ol></div>
{cards}
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Generate the calibration survey HTML.")
    ap.add_argument("--graph", default=str(ENRICHED_PATH))
    ap.add_argument("--out", default=str(Path(__file__).with_name("calibration_survey.html")))
    args = ap.parse_args()

    print(f"Loading {args.graph} ...")
    G = load_graph(Path(args.graph))
    results = []
    for case in SURVEY_ROUTES:
        try:
            r = _survey(G, case)
        except Exception as exc:
            r = {**case, "found": False, "error": f"{type(exc).__name__}: {exc}"}
        tag = "NO ROUTE" if not r.get("found") else (
            f"walk={r['walk']*100:.0f} safety={r['categories'].get('safety',float('nan')):.2f} "
            f"comfort={r['categories'].get('comfort',float('nan')):.2f} path={r['categories'].get('path',float('nan')):.2f} "
            f"len={r['length_m']:.0f}m segs={len(r['segments'])} flags={len(r['audit']['flags'])}")
        print(f"  {case['name']:<36} {tag}")
        results.append(r)

    out = Path(args.out)
    out.write_text(build_html(results))
    print(f"\nWrote {sum(1 for r in results if r.get('found'))}/{len(results)} routes → {out}")


if __name__ == "__main__":
    main()
