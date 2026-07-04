"""
Render the PROBLEM_ROUTES registry to a single browsable HTML page — the concise
sibling of calibration_survey.py.

    python notebooks/view_problem_routes.py           # full graph (default)
    python notebooks/view_problem_routes.py --dev      # fast Beacon Hill subset

Reuses calibration_survey's map/segment/bar/table machinery, but strips the
ground-truth QUESTIONS/template: this is a diagnostic view, not a survey. One
card per registry route (grouped by region) with a numbered-segment map, the
overall walk_score, the three dimension bars (safety/comfort/path), distance +
audit flags, the observed_problem / hypothesis notes, and a collapsible segment
table. Because it reads the live PROBLEM_ROUTES list, anything you add there
(e.g. allston_to_jamaica_plain across the Brookline seam) shows up automatically.

Writes notebooks/problem_routes_view.html (self-contained; embeds folium maps).
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path

from walkability.graph.build import DEV_ENRICHED_PATH, ENRICHED_PATH, load_graph

# calibration_survey.py sits next to this file — reuse its helpers rather than
# duplicate the geometry/rendering (all heavy work lives behind its __main__).
from calibration_survey import CSS, _bar, _gmaps_route, _route_map_html, _seg_table, _survey
from diagnostics import streetview_url
from problem_routes import PROBLEM_ROUTES

OUT_PATH = Path(__file__).with_name("problem_routes_view.html")
CATS = ("safety", "comfort", "path")

# A little extra CSS on top of calibration_survey's (region headers + notes).
EXTRA_CSS = """
h2.reg{font-size:16px;margin:26px 0 10px;color:#c75b39;text-transform:capitalize;
  border-bottom:2px solid #eee;padding-bottom:4px}
.card h3{font-size:17px;margin:0} .card h3 .n{display:inline-block;width:24px;height:24px;
  line-height:24px;text-align:center;background:#c75b39;color:#fff;border-radius:50%;
  font-size:13px;margin-right:8px}
.prob{color:#555;margin:8px 0 2px;font-size:13.5px} .prob b{color:#333}
"""


def _card(i: int, r: dict) -> str:
    if not r.get("found"):
        return (f'<div class="card"><h3><span class="n">{i}</span>{html.escape(r["name"])}</h3>'
                f'<p class="miss">No route resolved between these points '
                f'(origin {r["origin"]}, dest {r["dest"]}).</p></div>')
    cats = r["categories"]
    bars = "".join(_bar(c, cats[c]) for c in CATS if c in cats)
    flags = r["audit"].get("flags", [])
    sv_links = " ".join(
        f'<a href="{streetview_url(*w["mid"])}" target="_blank">worst #{w["i"]} ({w["walk"]:.2f})</a>'
        for w in r["worst"]
    )
    observed = html.escape(r.get("observed_problem", "") or "—")
    hypothesis = html.escape(r.get("hypothesis", "") or "—")
    return f"""<div class="card">
  <h3><span class="n">{i}</span>{html.escape(r['name'])}</h3>
  <div class="mapwrap">{_route_map_html(r)}</div>
  <div class="panel">
    <div><div class="big">{r['walk']*100:.0f}<small>/100</small></div></div>
    <div class="bars">{bars}</div>
  </div>
  <p class="meta"><b>{r['length_m']:.0f} m</b> · ~{r['minutes']:.0f} min ·
     confidence {r['confidence']:.2f} · alpha moves path: <b>{'yes' if r['alpha_moves'] else 'no'}</b>
     {'· <span class="flags">flags: '+html.escape(', '.join(flags))+'</span>' if flags else ''}</p>
  <p class="prob"><b>observed:</b> {observed}<br><b>hypothesis:</b> {hypothesis}</p>
  <p class="links"><b>Look:</b>
     <a href="{_gmaps_route(r['origin'], r['dest'])}" target="_blank">Google walking route (start → end)</a>
     <a href="{streetview_url(*r['coords'][0])}" target="_blank">Street View (start)</a>
     {sv_links}</p>
  <details class="segs"><summary>{len(r['segments'])} segments — table (numbers match the map pins)</summary>
     {_seg_table(r)}</details>
</div>"""


def build_html(groups: dict[str, list[dict]]) -> str:
    n = sum(len(v) for v in groups.values())
    i = 0
    sections = []
    for region in sorted(groups):
        sections.append(f'<h2 class="reg">{html.escape(region)}</h2>')
        for r in groups[region]:
            i += 1
            sections.append(_card(i, r))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Problem routes</title><style>{CSS}{EXTRA_CSS}</style></head><body>
<h1>Problem routes</h1>
<p class="sub">{n} routes from the PROBLEM_ROUTES registry, grouped by region · model =
the HDI-style two-level walk_score. Hover a segment for detail; red pins on the map
are numbered to match the segment table.</p>
{''.join(sections)}
</body></html>"""


def main(dev: bool = False) -> None:
    path = DEV_ENRICHED_PATH if dev else ENRICHED_PATH
    print(f"Loading {'dev' if dev else 'full'} graph ...")
    G = load_graph(path)

    groups: dict[str, list[dict]] = {}
    for case in PROBLEM_ROUTES:
        region = case.get("region", "unassigned")
        try:
            r = _survey(G, case)
        except Exception as exc:               # keep one bad route from killing the page
            r = {**case, "found": False, "error": f"{type(exc).__name__}: {exc}"}
        tag = "NO ROUTE" if not r.get("found") else (
            f"walk={r['walk']*100:.0f} len={r['length_m']:.0f}m "
            f"flags={len(r['audit']['flags'])}")
        print(f"  {case['name']:<32} [{region}] {tag}")
        groups.setdefault(region, []).append(r)

    OUT_PATH.write_text(build_html(groups))
    found = sum(1 for v in groups.values() for r in v if r.get("found"))
    print(f"\nWrote {found}/{sum(len(v) for v in groups.values())} routes → {OUT_PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Render PROBLEM_ROUTES to a concise HTML view.")
    ap.add_argument("--dev", action="store_true", help="Use the fast Beacon Hill dev subset.")
    args = ap.parse_args()
    main(dev=args.dev)
