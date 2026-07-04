"""
Brookline scoring-parity view — does the Boston-tuned model carry over to
Brookline, which has NO city sidewalk inventory (every edge is OSM-tier)?

    python notebooks/brookline_routes.py            # full graph (default)

Two groups of routes:
  * IN Brookline      — both endpoints in Brookline (100% OSM-tier). Do walkable
    Brookline streets (Coolidge Corner, Beacon St) score as high as their Boston
    equivalents? Does the environment/safety factor still work without city data?
  * THROUGH Brookline — cross the Boston↔Brookline seam. Watch for a visible
    discontinuity where the data source flips (city → OSM) mid-route.

Each card shows the usual map + score + dimension bars, PLUS a data-source mix
line (% OSM-tier vs % city inventory by length) — the crux of the parity check:
a Brookline route is ~100% OSM-tier, so comfort largely drops out and the score
leans on safety + path. Reuses calibration_survey's rendering machinery.

Writes notebooks/brookline_routes_view.html (self-contained; embeds folium maps).
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path

from walkability.graph.build import ENRICHED_PATH, load_graph

# Reuse calibration_survey helpers (all heavy work is behind its __main__).
from calibration_survey import CSS, _bar, _gmaps_route, _route_map_html, _seg_table, _survey
from diagnostics import streetview_url

OUT_PATH = Path(__file__).with_name("brookline_routes_view.html")
CATS = ("safety", "comfort", "path")

# ---------------------------------------------------------------------------
# Routes. Coordinates are approximate; find_routes snaps to the nearest routable
# node. `kind` groups them; `look_for` states the parity question to eyeball.
# ---------------------------------------------------------------------------
BROOKLINE_ROUTES: list[dict] = [
    # ---- IN Brookline (both ends in Brookline; ~100% OSM-tier) -------------
    {
        "name": "coolidge_corner_harvard_st",
        "kind": "in Brookline",
        "origin": (42.3425, -71.1216), "dest": (42.3390, -71.1235),
        "look_for": "Coolidge Corner commercial spine (Harvard St): dense shops, "
                    "transit, busy but walkable. Should score HIGH like a Boston "
                    "main street (North End / Tremont). Does eyes-on-street carry "
                    "the score without any city surface data?",
    },
    {
        "name": "beacon_st_coolidge_to_washington_sq",
        "kind": "in Brookline",
        "origin": (42.3421, -71.1212), "dest": (42.3400, -71.1348),
        "look_for": "Beacon St: wide leafy transit boulevard with the C line median. "
                    "Boston-equivalent = Comm Ave Mall. Check the arterial/off-path "
                    "safety penalty isn't over-firing on a calm boulevard.",
    },
    {
        "name": "brookline_village_to_coolidge",
        "kind": "in Brookline",
        "origin": (42.3326, -71.1163), "dest": (42.3421, -71.1212),
        "look_for": "Brookline Village → Coolidge Corner (Washington St): mixed "
                    "commercial/residential, moderate traffic. A 'good but not "
                    "perfect' anchor — compare to JP Centre St.",
    },
    {
        "name": "brookline_residential_corey_hill",
        "kind": "in Brookline",
        "origin": (42.3460, -71.1265), "dest": (42.3435, -71.1225),
        "look_for": "Quiet residential side streets (Corey Hill). Calm, low traffic, "
                    "leafy. Should score HIGH on safety+path. With comfort dropped "
                    "(no city surface), does the score still land sensibly?",
    },
    {
        "name": "south_brookline_route9",
        "kind": "in Brookline",
        "origin": (42.3268, -71.1360), "dest": (42.3235, -71.1445),
        "look_for": "South Brookline near Boylston St (Route 9): wider, faster, more "
                    "car-oriented. Should score LOWER — a rare non-Boston LOW-end "
                    "test. Is the arterial penalty right here?",
    },

    # ---- THROUGH Brookline (cross the Boston↔Brookline data seam) ----------
    {
        "name": "kenmore_to_coolidge_corner",
        "kind": "through Brookline",
        "origin": (42.3487, -71.0952), "dest": (42.3425, -71.1216),
        "look_for": "Boston Kenmore → Brookline Coolidge Corner along Beacon St. "
                    "Crosses the seam mid-route — look for a score/colour jump where "
                    "city data ends and OSM-tier begins.",
    },
    {
        "name": "longwood_to_brookline_village",
        "kind": "through Brookline",
        "origin": (42.3378, -71.1035), "dest": (42.3326, -71.1163),
        "look_for": "Boston Longwood Medical → Brookline Village. Institutional Boston "
                    "side (city data) into residential Brookline (OSM). Is the "
                    "transition smooth or does confidence/score step down?",
    },
    {
        "name": "allston_to_washington_sq",
        "kind": "through Brookline",
        "origin": (42.3512, -71.1300), "dest": (42.3400, -71.1348),
        "look_for": "Boston Allston → Brookline Washington Sq (via Comm Ave / Beacon). "
                    "Short cross-seam hop between two neighbourhoods separated only "
                    "by the town line.",
    },
    {
        "name": "fenway_to_brookline_village_riverway",
        "kind": "through Brookline",
        "origin": (42.3430, -71.1050), "dest": (42.3326, -71.1163),
        "look_for": "Boston Fenway → Brookline Village along the Riverway (Olmsted "
                    "park, road-separated). Tests whether openness/separation lifts "
                    "safety consistently on BOTH sides of the seam.",
    },
    {
        "name": "mission_hill_to_brookline_village",
        "kind": "through Brookline",
        "origin": (42.3300, -71.1015), "dest": (42.3326, -71.1163),
        "look_for": "Boston Mission Hill → Brookline Village. Hilly Boston side into "
                    "Brookline. A general cross-seam sanity route.",
    },

    # ---- LOW-END Brookline (suburban / car-oriented; the acid test) --------
    # These SHOULD score low. The question: does the Boston-tuned model still
    # floor a car-dependent suburb with ZERO city surface data, or does it flatten
    # toward the mid-range because comfort dropped out?
    {
        "name": "chestnut_hill_route9",
        "kind": "low-end Brookline",
        "origin": (42.3265, -71.1470), "dest": (42.3235, -71.1560),
        "look_for": "Boylston St / Route 9 near Chestnut Hill: fast multi-lane "
                    "state highway, big setbacks, hostile to walk. Should be LOW "
                    "(~60s). Is the arterial/off-path penalty firing hard enough?",
    },
    {
        "name": "south_brookline_heath_st",
        "kind": "low-end Brookline",
        "origin": (42.3260, -71.1490), "dest": (42.3235, -71.1470),
        "look_for": "Heath St / Reservoir Rd (Chestnut Hill): wide leafy but "
                    "car-oriented suburban roads, country-club edges, sparse foot "
                    "traffic. Leafy ≠ walkable — should land in the 50s.",
    },
    {
        "name": "putterham_grove_st_suburban",
        "kind": "low-end Brookline",
        "origin": (42.3060, -71.1480), "dest": (42.3035, -71.1445),
        "look_for": "Deep South Brookline (Grove St / Cypress Ave near Putterham / "
                    "Hancock Village): the most car-dependent, lowest-density corner. "
                    "Sparse sidewalks. The genuine LOW-end anchor — should be lowest.",
    },

    # ---- Boston reference (seam A/B) --------------------------------------
    # Boston-side parkway with CITY data, to A/B against the Brookline park-adjacent
    # paths: does park openness over-inflate a car-hostile Olmsted parkway, and does
    # having city surface data change the answer vs the OSM-only Brookline side?
    {
        "name": "jamaicaway_parkway_boston",
        "kind": "Boston reference (seam A/B)",
        "origin": (42.3225, -71.1135), "dest": (42.3140, -71.1075),
        "look_for": "Jamaicaway (Boston Olmsted parkway): leafy-but-fast, park-adjacent "
                    "yet car-hostile — the Boston-side, city-data counterpart to the "
                    "Brookline Riverway/Pond paths. A/B: does openness wrongly prop up "
                    "a dangerous parkway, and does city data shift it vs OSM-only?",
    },
]


def _source_mix(segs: list[dict]) -> tuple[float, float]:
    """(osm_share, city_share) of route length — the parity axis. Anything not
    'city_inventory' is OSM-tier (highway= / context: / no_tag)."""
    total = sum(s["length"] for s in segs) or 1.0
    city = sum(s["length"] for s in segs if (s["source"] or "").startswith("city"))
    return (total - city) / total, city / total


EXTRA_CSS = """
h2.kind{font-size:16px;margin:26px 0 10px;color:#c75b39;
  border-bottom:2px solid #eee;padding-bottom:4px}
.card h3{font-size:17px;margin:0} .card h3 .n{display:inline-block;width:24px;height:24px;
  line-height:24px;text-align:center;background:#c75b39;color:#fff;border-radius:50%;
  font-size:13px;margin-right:8px}
.mix{font-size:13px;margin:6px 0 0;color:#555}
.mix .track{display:inline-block;width:180px;height:11px;border-radius:6px;overflow:hidden;
  vertical-align:middle;background:#5b8def;margin:0 8px}
.mix .track i{display:block;height:100%;background:#c99a3a;float:right}
"""


def _card(i: int, r: dict) -> str:
    if not r.get("found"):
        return (f'<div class="card"><h3><span class="n">{i}</span>{html.escape(r["name"])}</h3>'
                f'<p class="miss">No route resolved (origin {r["origin"]}, dest {r["dest"]}).</p></div>')
    cats = r["categories"]
    bars = "".join(_bar(c, cats[c]) for c in CATS if c in cats)
    flags = r["audit"].get("flags", [])
    osm, city = _source_mix(r["segments"])
    mix = (f'<p class="mix">data source: <b>{osm:.0%}</b> OSM-tier · '
           f'<b>{city:.0%}</b> city inventory'
           f'<span class="track"><i style="width:{city*100:.0f}%"></i></span></p>')
    return f"""<div class="card">
  <h3><span class="n">{i}</span>{html.escape(r['name'])}</h3>
  <p class="look">{html.escape(r['look_for'])}</p>
  <div class="mapwrap">{_route_map_html(r)}</div>
  <div class="panel">
    <div><div class="big">{r['walk']*100:.0f}<small>/100</small></div></div>
    <div class="bars">{bars}</div>
  </div>
  <p class="meta"><b>{r['length_m']:.0f} m</b> · ~{r['minutes']:.0f} min ·
     confidence {r['confidence']:.2f}
     {'· <span class="flags">flags: '+html.escape(', '.join(flags))+'</span>' if flags else ''}</p>
  {mix}
  <p class="links"><b>Look:</b>
     <a href="{_gmaps_route(r['origin'], r['dest'])}" target="_blank">Google walking route (start → end)</a>
     <a href="{streetview_url(*r['coords'][0])}" target="_blank">Street View (start)</a></p>
  <details class="segs"><summary>{len(r['segments'])} segments — table (source column shows city vs osm per block)</summary>
     {_seg_table(r)}</details>
</div>"""


def build_html(results: list[dict]) -> str:
    order = ["in Brookline", "through Brookline", "low-end Brookline",
             "Boston reference (seam A/B)"]
    i = 0
    sections = []
    for kind in order:
        group = [r for r in results if r.get("kind") == kind]
        if not group:
            continue
        sections.append(f'<h2 class="kind">{html.escape(kind)} ({len(group)})</h2>')
        for r in group:
            i += 1
            sections.append(_card(i, r))
    found = sum(1 for r in results if r.get("found"))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Brookline scoring parity</title><style>{CSS}{EXTRA_CSS}</style></head><body>
<h1>Brookline scoring parity</h1>
<p class="sub">{found}/{len(results)} routes · Brookline has NO city sidewalk inventory,
so its edges are OSM-tier (comfort largely drops out — score leans on safety + path).
The question: does the Boston-tuned model still rank Brookline sensibly? Blue in the
data-source bar = OSM-tier, gold = city inventory.</p>
{''.join(sections)}
</body></html>"""


def main() -> None:
    print("Loading full graph ...")
    G = load_graph(ENRICHED_PATH)
    results = []
    for case in BROOKLINE_ROUTES:
        try:
            r = _survey(G, case)
            r["kind"] = case["kind"]
        except Exception as exc:
            r = {**case, "found": False, "error": f"{type(exc).__name__}: {exc}"}
        if r.get("found"):
            osm, _ = _source_mix(r["segments"])
            tag = f"walk={r['walk']*100:.0f} osm={osm:.0%} len={r['length_m']:.0f}m flags={len(r['audit']['flags'])}"
        else:
            tag = "NO ROUTE"
        print(f"  {case['name']:<38} [{case['kind']}] {tag}")
        results.append(r)

    OUT_PATH.write_text(build_html(results))
    print(f"\nWrote {sum(1 for r in results if r.get('found'))}/{len(results)} routes → {OUT_PATH}")


if __name__ == "__main__":
    argparse.ArgumentParser(description="Render the Brookline scoring-parity view.").parse_args()
    main()
