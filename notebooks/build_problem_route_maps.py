"""
Generate one inspector HTML per problematic route, for manual testing.

    python notebooks/build_problem_route_maps.py

Writes notebooks/problem_route_maps/<name>.html (per-edge red→green map with
hover tooltips + crossing-node markers, via diagnostics.inspect_route_map) plus
an index.html linking them all with each route's audit flags. Open index.html
and click through to assess each route.

The 10 routes span Boston neighbourhoods and were chosen because they resolve on
the full graph AND trip the audit (arterials, steps, crossing-heavy, restricted
access) — i.e. they are worth a human look. Coordinates are (lat, lon).
"""

from __future__ import annotations

from pathlib import Path

from walkability.graph.build import ENRICHED_PATH, load_graph
from walkability.routing.router import find_routes

from diagnostics import audit_route, inspect_route_map   # sibling modules

OUT_DIR = Path(__file__).with_name("problem_route_maps")

# name -> (origin, dest, alpha, what to look for)
ROUTES: dict[str, tuple] = {
    "dudley_to_franklin_park": (
        (42.3290, -71.0830), (42.3060, -71.0890), 2.0,
        "Roxbury → Franklin Park. Worst case: arterials + restricted access + many crossings.",
    ),
    "beacon_hill_to_downtown": (
        (42.3588, -71.0707), (42.3554, -71.0605), 2.0,
        "Beacon Hill → Downtown Crossing. Has a STEPS edge — does the path use stairs?",
    ),
    "southend_to_backbay": (
        (42.3401, -71.0750), (42.3486, -71.0780), 2.0,
        "South End → Back Bay across Mass Ave / the Pike. Crossing-heavy.",
    ),
    "nubian_to_massave": (
        (42.3290, -71.0830), (42.3410, -71.0830), 2.0,
        "Nubian Sq → Mass Ave along Washington St. Arterial corridor.",
    ),
    "newmarket_to_andrew": (
        (42.3330, -71.0660), (42.3300, -71.0570), 2.0,
        "Newmarket industrial → Andrew Sq. Sparse sidewalks, many crossings.",
    ),
    "charlestown_citysq_to_sullivan": (
        (42.3782, -71.0602), (42.3840, -71.0700), 2.0,
        "City Sq → Sullivan Sq along Rutherford Ave. Car-dominated.",
    ),
    "northend_to_mgh": (
        (42.3647, -71.0542), (42.3618, -71.0686), 2.0,
        "North End → MGH/West End across the Greenway. Crossing the highway cap.",
    ),
    "copley_to_fenway": (
        (42.3496, -71.0785), (42.3442, -71.0995), 2.0,
        "Copley → Fenway along Boylston. Long, busy arterial.",
    ),
    "southboston_to_seaport": (
        (42.3370, -71.0490), (42.3470, -71.0440), 2.0,
        "South Boston → Seaport. Wide roads, big blocks, many crossings.",
    ),
    "jp_centre_st": (
        (42.3170, -71.1090), (42.3100, -71.1140), 2.0,
        "Jamaica Plain along Centre St. SW neighbourhood spot-check.",
    ),
}


def _index_html(rows: list[dict]) -> str:
    items = []
    for r in rows:
        flags = ", ".join(r["flags"]) if r["flags"] else "(no flags)"
        items.append(
            f"<li><a href='{r['name']}.html'>{r['name']}</a> — "
            f"{r['length']:.0f} m, walk={r['walk']:.3f}"
            f"<br><small>{r['note']}</small>"
            f"<br><small><b>flags:</b> {flags}</small></li>"
        )
    return (
        "<html><head><meta charset='utf-8'><title>Problem route maps</title>"
        "<style>body{font-family:sans-serif;max-width:760px;margin:2rem auto;}"
        "li{margin:0.8rem 0;} small{color:#555;}</style></head><body>"
        "<h2>Problematic routes — manual inspection</h2>"
        "<p>Open each map; hover red edges for scores, red dots are crossing nodes.</p>"
        f"<ol>{''.join(items)}</ol></body></html>"
    )


if __name__ == "__main__":
    G = load_graph(ENRICHED_PATH)
    OUT_DIR.mkdir(exist_ok=True)
    print(f"\nWriting {len(ROUTES)} route maps to {OUT_DIR}\n")

    rows = []
    for name, (orig, dest, alpha, note) in ROUTES.items():
        routes = find_routes(G, orig, dest, alpha=alpha)
        if not routes:
            print(f"  [SKIP] {name}: no route")
            continue
        best = routes[0]
        a = audit_route(G, best, alpha=alpha)
        inspect_route_map(G, best, out=OUT_DIR / f"{name}.html", label=name)
        rows.append({"name": name, "length": best.total_length,
                     "walk": best.walk_score, "flags": a["flags"], "note": note})
        print(f"         {name}: {best.total_length:.0f} m, walk={best.walk_score:.3f}, "
              f"flags={[f.split(' ')[0] for f in a['flags']] or '(none)'}")

    (OUT_DIR / "index.html").write_text(_index_html(rows))
    print(f"\nIndex: {OUT_DIR / 'index.html'}  ({len(rows)} maps)")
