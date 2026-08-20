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
import csv
import html
from pathlib import Path

from walkability.graph.build import ENRICHED_PATH, load_graph
from walkability.routing.cost import ALPHA_DEFAULT
from walkability.routing.router import find_routes
from walkability.scoring.factors import (
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
    # --- 10 new routes (2026-06-22 ground-truth expansion) ---
    {
        "name": "allston_harvard_ave",
        "area": "Allston (Harvard Ave commercial)",
        "origin": (42.3530, -71.1314), "dest": (42.3510, -71.1338),
        "look_for": "Student commercial strip: cheap eats, busy traffic, narrow worn sidewalks. Mediocre-but-lively. Does eyes-on-street over-inflate a gritty busy street?",
    },
    {
        "name": "fenway_kenmore_brookline_ave",
        "area": "Fenway / Kenmore (Brookline Ave)",
        "origin": (42.3460, -71.0975), "dest": (42.3437, -71.0948),
        "look_for": "Busy near-stadium streets next to the Riverway/Fens park. Check whether openness (park) lifts safety where car traffic is actually heavy.",
    },
    {
        "name": "dorchester_fields_corner",
        "area": "Dorchester (Dot Ave / Fields Corner)",
        "origin": (42.3001, -71.0592), "dest": (42.2978, -71.0615),
        "look_for": "Transit main street, arterial-ish, mixed commercial/residential. A non-core neighbourhood anchor — is the arterial penalty right here?",
    },
    {
        "name": "roslindale_square",
        "area": "Roslindale (Roslindale Village)",
        "origin": (42.2872, -71.1295), "dest": (42.2850, -71.1268),
        "look_for": "Walkable outer-neighbourhood square: shops, narrow streets, calm. A good outer-Boston anchor — does the model rate the village core high?",
    },
    {
        "name": "charlestown_main_thompson_sq",
        "area": "Charlestown (Main St / Thompson Sq)",
        "origin": (42.3772, -71.0623), "dest": (42.3795, -71.0638),
        "look_for": "Historic walkable Charlestown — the GOOD contrast to car-dominated Sullivan Sq. Narrow, brick, calm. Should score high.",
    },
    {
        "name": "west_roxbury_centre_st",
        "area": "West Roxbury (Centre St)",
        "origin": (42.2855, -71.1600), "dest": (42.2834, -71.1638),
        "look_for": "Suburban-feeling main street, wider roads, more parking, calmer. Tests the model at the leafy/low-density end of the spectrum.",
    },
    {
        "name": "mission_hill_tremont",
        "area": "Mission Hill (Tremont St)",
        "origin": (42.3325, -71.0985), "dest": (42.3301, -71.0959),
        "look_for": "Hilly, institutional edges, triple-deckers. Elevation isn't a factor yet — flag if a steep block scores higher than it walks.",
    },
    {
        "name": "longwood_medical_area",
        "area": "Longwood Medical Area (Longwood Ave)",
        "origin": (42.3375, -71.1030), "dest": (42.3398, -71.1056),
        "look_for": "Institutional canyon: wide sidewalks, heavy foot traffic, but big hospital-block crossings + buses. Does comfort+eyes over-inflate a busy medical corridor?",
    },
    {
        "name": "chinatown_beach_essex",
        "area": "Chinatown (Beach St / Essex St)",
        "origin": (42.3515, -71.0615), "dest": (42.3501, -71.0590),
        "look_for": "Extremely dense, narrow, lively but chaotic; tunnel/artery edges nearby. Eyes-on-street maxed — does anything pull it down where it should?",
    },
    {
        "name": "south_boston_broadway",
        "area": "South Boston (West Broadway)",
        "origin": (42.3370, -71.0500), "dest": (42.3356, -71.0540),
        "look_for": "Residential main street, shops, moderate traffic, decent grid. A 'good but not perfect' anchor like JP — check consistency across neighbourhoods.",
    },

    # ---- 2026-06-26 outlier batch: tail-spanning routes to calibrate the
    # distribution re-anchor. The current 20 are all mid-range urban walks
    # (84-89); these probe the TOP (pedestrian-designed / car-free — does the
    # model fail to reward them?), the BOTTOM (hostile parkways/arterials), and
    # LONG/COMPLEX (multi-km, many crossings). Key calibration question for the
    # top group: where SHOULD a car-free greenway land vs a normal sidewalk?
    {
        "name": "esplanade_charles_river",
        "area": "Charles River Esplanade (riverside path)",
        "origin": (42.3543, -71.0900), "dest": (42.3585, -71.0730),
        "look_for": "TOP-END: car-free riverside park path. Should this be near the TOP (>90)? It currently scores ~86, same as an ordinary sidewalk — where SHOULD it land?",
    },
    {
        "name": "sw_corridor_park",
        "area": "Southwest Corridor Park (linear park)",
        "origin": (42.3410, -71.0810), "dest": (42.3300, -71.0940),
        "look_for": "TOP-END: linear park, separated from traffic. Pedestrian-designed — should outscore a normal sidewalk. Does it?",
    },
    {
        "name": "jamaica_pond_loop",
        "area": "Jamaica Pond (pond loop path)",
        "origin": (42.3175, -71.1205), "dest": (42.3210, -71.1160),
        "look_for": "TOP-END: the most road-SEPARATED route in the set (sep 0.65). Genuinely car-free. This is the acid test — should be the highest-scoring route. Is it?",
    },
    {
        "name": "commonwealth_mall_full",
        "area": "Commonwealth Ave Mall (full length)",
        "origin": (42.3520, -71.0760), "dest": (42.3475, -71.0890),
        "look_for": "TOP-END: the central pedestrian mall (not the sidewalks). Tree-lined, car-free spine. Where should a pedestrian mall land?",
    },
    {
        "name": "morrissey_blvd_dorchester",
        "area": "Morrissey Blvd (Dorchester parkway)",
        "origin": (42.3170, -71.0530), "dest": (42.3110, -71.0500),
        "look_for": "BOTTOM-END: fast multi-lane parkway, car-dominated, sparse. Should be LOW (~60s). Is it low enough, or still propped up by surface/path?",
    },
    {
        "name": "vfw_parkway_wroxbury",
        "area": "VFW Parkway (West Roxbury)",
        "origin": (42.2870, -71.1560), "dest": (42.2810, -71.1610),
        "look_for": "BOTTOM-END: suburban high-speed parkway, narrow sidewalk beside fast traffic. Hostile — should score low.",
    },
    {
        "name": "jamaicaway_parkway",
        "area": "Jamaicaway (Olmsted parkway)",
        "origin": (42.3225, -71.1135), "dest": (42.3140, -71.1075),
        "look_for": "BOTTOM-END: a leafy-but-fast parkway — park-adjacent yet car-hostile. Tests whether greenery/openness wrongly props up a dangerous road.",
    },
    {
        "name": "downtown_to_fenway",
        "area": "Downtown → Fenway (long cross-city)",
        "origin": (42.3585, -71.0575), "dest": (42.3440, -71.0960),
        "look_for": "LONG/COMPLEX (~4 km): crosses many environments. Does the overall score sensibly average a mixed route? Any obvious bad detours?",
    },
    {
        "name": "dorchester_to_downtown",
        "area": "Dorchester → Downtown (very long)",
        "origin": (42.3130, -71.0570), "dest": (42.3560, -71.0570),
        "look_for": "LONG/COMPLEX (~6.7 km, 40+ crossings): the longest route. Stress-tests crossings + aggregation over a big mixed corridor.",
    },
    {
        "name": "nubian_to_longwood",
        "area": "Nubian → Longwood (cross-neighbourhood)",
        "origin": (42.3290, -71.0830), "dest": (42.3380, -71.1040),
        "look_for": "LONG/COMPLEX (~2.5 km): Roxbury → medical area, varied. Check the route choice and whether the mixed score feels right.",
    },
]


# ---------------------------------------------------------------------------
# Representative AUSTIN routes — spanning the spectrum from the walkable core +
# car-free trails (top) through residential neighbourhoods (mid) to the
# car-dependent stroads/arterials Austin is built around (bottom). Coordinates
# are approximate; find_routes snaps to the nearest routable node. Austin is the
# less-walkable / real-condition-data calibration city — the KEY questions are at
# the LOW end (do the stroads score low enough?) and around the honest 1–5
# surface ratings (does genuinely-marginal condition read right?).
# ---------------------------------------------------------------------------
AUSTIN_ROUTES: list[dict] = [
    # ---- top end: walkable core + car-free trails ----
    {
        "name": "downtown_congress_2nd_to_6th",
        "area": "Downtown (Congress Ave, 2nd → 6th)",
        "origin": (30.2649, -97.7431), "dest": (30.2685, -97.7428),
        "look_for": "Austin's most walkable spine: wide sidewalks, shops, foot traffic. Should be near the top of Austin's range. Does it read as clearly better than the stroads?",
    },
    {
        "name": "rainey_street_district",
        "area": "Rainey Street (bar district)",
        "origin": (30.2585, -97.7395), "dest": (30.2602, -97.7386),
        "look_for": "Dense, lively converted-bungalow bar strip, narrow, high foot traffic. Eyes-on-street should be high. Does high activity score correctly?",
    },
    {
        "name": "south_congress_soco",
        "area": "South Congress (SoCo shops)",
        "origin": (30.2490, -97.7505), "dest": (30.2516, -97.7500),
        "look_for": "Iconic Austin shopping/dining strip — walkable but beside a moderately busy road. Does the retail liveliness vs the traffic balance out right?",
    },
    {
        "name": "ut_drag_guadalupe",
        "area": "UT / The Drag (Guadalupe St)",
        "origin": (30.2865, -97.7415), "dest": (30.2902, -97.7412),
        "look_for": "Campus commercial edge: constant students, transit, shops, but a busy arterial. High eyes + busy road — does the safety penalty land right?",
    },
    {
        "name": "hyde_park_residential",
        "area": "Hyde Park (Avenue B, residential)",
        "origin": (30.3020, -97.7285), "dest": (30.3048, -97.7283),
        "look_for": "Classic walkable historic neighbourhood: calm, tree-lined, gridded. A high-end residential anchor — should score well on safety+path.",
    },
    {
        "name": "clarksville_west_lynn",
        "area": "Clarksville (West Lynn St)",
        "origin": (30.2775, -97.7595), "dest": (30.2797, -97.7590),
        "look_for": "Small walkable historic pocket near downtown. Narrow, calm, some shops. Outer-core walkable anchor.",
    },
    {
        "name": "shoal_creek_trail",
        "area": "Shoal Creek Trail (greenway)",
        "origin": (30.2760, -97.7490), "dest": (30.2800, -97.7480),
        "look_for": "TOP-END: car-free creekside separated path — the greenway acid test. Where should a genuine road-separated trail land vs an ordinary sidewalk? Does openness/separation push it above street routes?",
    },
    {
        "name": "mueller_aldrich",
        "area": "Mueller (Aldrich St, new-urbanist)",
        "origin": (30.2985, -97.7062), "dest": (30.3000, -97.7042),
        "look_for": "Purpose-built walkable redevelopment: wide sidewalks, retail, calm streets by design. Should score high — a modern 'done right' anchor.",
    },
    # ---- middle: neighbourhood main streets / mixed ----
    {
        "name": "east_austin_e6th",
        "area": "East Austin (E 6th St)",
        "origin": (30.2595, -97.7220), "dest": (30.2599, -97.7172),
        "look_for": "Gentrifying nightlife/retail strip, mixed condition sidewalks, moderate traffic. A lively-but-gritty mid anchor — does eyes-on-street over-inflate it?",
    },
    {
        "name": "north_loop",
        "area": "North Loop Blvd (quirky strip)",
        "origin": (30.3175, -97.7225), "dest": (30.3196, -97.7212),
        "look_for": "Small walkable indie commercial strip in a residential sea. Calm, low-rise. A 'good but modest' mid anchor.",
    },
    {
        "name": "travis_heights_residential",
        "area": "Travis Heights (residential)",
        "origin": (30.2470, -97.7440), "dest": (30.2492, -97.7420),
        "look_for": "Leafy residential south of the river, some sidewalk gaps, calm streets. Tests the low-traffic residential middle where sidewalks are patchy.",
    },
    # ---- bottom end: stroads / car-dependent arterials (the calibration payload) ----
    {
        "name": "north_lamar_arterial",
        "area": "North Lamar Blvd (beside the arterial)",
        "origin": (30.3845, -97.6850), "dest": (30.3811, -97.6868), "alpha": 0.0,
        "look_for": "BOTTOM-END: classic Austin stroad — wide fast arterial, strip malls, hostile crossings. Coords put the walk BESIDE N.Lamar (the old @Rundberg coords mis-routed onto quiet residential side streets). Should score LOW. Is it low enough? (a lanes/width amplifier is the candidate lever.)",
    },
    {
        "name": "ben_white_s1st",
        "area": "Ben White Blvd (@ S 1st)",
        "origin": (30.2280, -97.7690), "dest": (30.2286, -97.7650), "alpha": 0.0,
        "look_for": "BOTTOM-END: highway-grade multi-lane arterial with frontage. Genuinely hostile to walk. Should be near the floor.",
    },
    {
        "name": "airport_blvd",
        "area": "Airport Blvd (@ 45th)",
        "origin": (30.3020, -97.7132), "dest": (30.3052, -97.7122), "alpha": 0.0,
        "look_for": "BOTTOM-END: wide car-oriented arterial, auto shops, sparse frontage. Low eyes + fast traffic — check the safety penalty.",
    },
    {
        "name": "east_riverside_arterial",
        "area": "East Riverside Dr (arterial)",
        "origin": (30.2345, -97.7230), "dest": (30.2360, -97.7198), "alpha": 0.0,
        "look_for": "BOTTOM-END: apartment-district arterial, wide and busy but heavily walked by students/transit riders. Tension: real foot traffic on a hostile road — where should it land?",
    },
    {
        "name": "burnet_rd_anderson",
        "area": "Burnet Rd (@ Anderson Ln)",
        "origin": (30.3625, -97.7392), "dest": (30.3652, -97.7386), "alpha": 0.0,
        "look_for": "BOTTOM/MID: commercial stroad, strip retail, moderate-to-fast traffic, patchy sidewalks. A car-dependent commercial anchor — is it distinguished from the true walkable strips?",
    },
    {
        "name": "research_183_frontage",
        "area": "Research Blvd / US-183 (frontage)",
        "origin": (30.3928, -97.7250), "dest": (30.3950, -97.7228), "alpha": 0.0,
        "look_for": "BOTTOM-END: highway frontage road — the most car-dependent case. Should be at or near the absolute floor of Austin's range.",
    },
    {
        # FREEWAY-VETO target: a genuine at-grade US-183 access/frontage road.
        "name": "us183_frontage_anderson",
        "area": "US-183 frontage road (@ Anderson Ln)",
        "origin": (30.3485, -97.7142), "dest": (30.3505, -97.7135), "alpha": 0.0,
        "look_for": "VETO TARGET / ABSOLUTE FLOOR: walking a US-183 freeway frontage — fast traffic, on-ramps, no shelter. The barrier-effect veto should crater this to ~10–30. Is it clearly the worst of Austin's range?",
    },
    {
        # Second, independent freeway frontage (I-35 access road) — consistency check.
        "name": "i35_frontage_south",
        "area": "I-35 frontage road (S, near St Elmo)",
        "origin": (30.2432, -97.7347), "dest": (30.2468, -97.7344), "alpha": 0.0,
        "look_for": "VETO TARGET: an I-35 access-road sidewalk — confirms the veto fires consistently on freeway frontages, not one lucky spot. Should also be near the floor.",
    },
]


# City → its curated route set. Add a city here when it gets a survey.
CITY_ROUTES: dict[str, list[dict]] = {
    "boston": SURVEY_ROUTES,
    "austin": AUSTIN_ROUTES,
}


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
    """The route-level per-dimension values the walk_score is actually built from.

    Reuses ``route.dimension_scores`` (router.py::_build_route) — the floored
    per-dimension power means that ``combine_categories`` turns into walk_score —
    so the displayed bars match the score exactly rather than a separate plain
    arithmetic mean.
    """
    return {c: route.dimension_scores[c] for c in CATS if c in route.dimension_scores}


def _alpha_moves(G, origin, dest) -> bool:
    fps = set()
    for a in ALPHAS:
        rs = find_routes(G, tuple(origin), tuple(dest), alpha=a)
        if rs:
            fps.add(tuple(rs[0].nodes))
    return len(fps) > 1


def _survey(G, case: dict) -> dict:
    # A route may pin its own alpha — e.g. a stroad calibration case uses alpha=0
    # (shortest path) so it stays ON the hostile arterial instead of the router
    # detouring onto a parallel calm street (which defeats low-end calibration).
    alpha = case.get("alpha", ALPHA_DEFAULT)
    routes = find_routes(G, tuple(case["origin"]), tuple(case["dest"]), alpha=alpha)
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
.blindbanner{background:#2c2a26;color:#ede7da;border-radius:10px;padding:12px 16px;margin:0 0 18px;font-size:14px}
.blindbanner code{background:#48453e;padding:1px 4px;border-radius:3px}
.intro ol{margin:8px 0 0;padding-left:20px} .intro li{margin:4px 0}
.card{background:#fff;border:1px solid #e6e0d6;border-radius:10px;padding:16px 18px;margin:0 0 22px}
.card h2 .n{display:inline-block;width:26px;height:26px;line-height:26px;text-align:center;background:#c75b39;color:#fff;border-radius:50%;font-size:14px;margin-right:8px}
.card h2 .rk{font:12px/1 ui-monospace,Menlo,monospace;color:#8a8578;background:#f2efe8;border:1px solid #e6e0d6;border-radius:5px;padding:3px 6px;margin-left:8px;vertical-align:middle}
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

# Q0 has a non-blind variant (shows model dims for context) and a blind variant
# (model verdict hidden so the estimate stays independent). Q1–Q3 leak nothing.
Q_IDEAL = "Your ideal walk_score for this route, <b>0–100</b>. Judge from the map / Street View — the model's number (safety {safety}, comfort {comfort}, path {path}) is shown for context, but deciding independently is the whole point. Sweat the <b>ordering &amp; tier gaps</b>, not 58-vs-62."
Q_IDEAL_BLIND = "Your ideal walk_score for this route, <b>0–100</b>, from the map / Street View alone — the model's scores are hidden on purpose so your estimate stays independent. Sweat the <b>ordering &amp; tier gaps</b>, not 58-vs-62."
QUESTIONS = [
    Q_IDEAL,
    "Confidence: <b>sure</b> or <b>rough</b> (rough is down-weighted in the fit, so don't agonise).",
    "Tier: car_free / buffered / ped_priority / good / mixed / poor / hostile — the coarse bucket that pins relative ordering.",
    "Notes / reasoning: which dimension feels off, a bad detour, \"should be higher because…\". Name a SEGMENT # (from the map/table) if something's factually wrong on the ground — a sidewalk that isn't there, a surface mis-rated, a bad crossing.",
]


def _card(i: int, r: dict, blind: bool = False) -> str:
    if not r.get("found"):
        return (f'<div class="card"><h2><span class="n">{i}</span>{html.escape(r["area"])}</h2>'
                f'<p class="miss">No route resolved between these points — I will adjust the endpoints. '
                f'(origin {r["origin"]}, dest {r["dest"]})</p></div>')
    cats = r["categories"]
    s = cats.get("safety", float("nan")); c = cats.get("comfort", float("nan")); p = cats.get("path", float("nan"))
    flags = r["audit"].get("flags", [])
    # In blind mode: "worst seg" labels + per-seg scores are model verdicts → drop them.
    sv_links = " ".join(
        (f'<a href="{streetview_url(*w["mid"])}" target="_blank">Street View seg #{w["i"]}</a>'
         if blind else
         f'<a href="{streetview_url(*w["mid"])}" target="_blank">worst seg #{w["i"]} ({w["walk"]:.2f})</a>')
        for w in r["worst"]
    )
    # QUESTIONS carry trusted inline HTML + {placeholders}; format (not escape) them.
    questions = [Q_IDEAL_BLIND if blind else QUESTIONS[0], *QUESTIONS[1:]]
    qs = "".join(
        "<li>" + q.format(safety=_fmt(s), comfort=_fmt(c), path=_fmt(p)) + "</li>"
        for q in questions
    )
    name = r.get("name", "")
    tmpl = html.escape(
        f"[{name}]  {_area_label(r['area'])}\n"
        f"  ideal_score (0-100): \n"
        f"  confidence: sure|rough\n"
        f"  tier: car_free|buffered|ped_priority|good|mixed|poor|hostile\n"
        f"  notes: \n"
    )
    # Blind hides the model's verdict signals (overall score, dimension bars, audit
    # flags, route confidence); the map's segment colours + Street View stay as a
    # navigation aid. See --blind.
    panel = "" if blind else f"""
  <div class="panel">
    <div><div class="big">{r['walk']*100:.0f}<small>/100</small></div></div>
    <div class="bars">{_bar('safety', s)}{_bar('comfort', c)}{_bar('path', p)}</div>
  </div>"""
    conf_txt = "" if blind else f"confidence {r['confidence']:.2f} · "
    flags_txt = "" if (blind or not flags) else \
        '· <span class="flags">flags: ' + html.escape(', '.join(flags)) + '</span>'
    return f"""<div class="card">
  <h2><span class="n">{i}</span>{html.escape(_area_label(r['area']))}
     <span class="rk" title="calibration_targets row key">{html.escape(name)}</span></h2>
  <p class="look">{html.escape(r['look_for'])}</p>
  <div class="mapwrap">{_route_map_html(r)}</div>{panel}
  <p class="meta"><b>{r['length_m']:.0f} m</b> · ~{r['minutes']:.0f} min ·
     {conf_txt}alpha moves path: <b>{'yes' if r['alpha_moves'] else 'no'}</b>
     {flags_txt}</p>
  <p class="links"><b>Look:</b>
     <a href="{streetview_url(*r['coords'][0])}" target="_blank">Street View (start)</a>
     <a href="{_gmaps_route(r['origin'], r['dest'])}" target="_blank">Google walking route</a>
     {sv_links}</p>
  <details class="segs"><summary>{len(r['segments'])} segments — table (numbers match the map pins)</summary>
     {_seg_table(r)}</details>
  <div class="q"><b>Questions</b><ol>{qs}</ol>
    <div class="tmpl">{tmpl}</div></div>
</div>"""


def build_html(results: list[dict], city: str = "Boston", blind: bool = False) -> str:
    cards = "\n".join(_card(i, r, blind=blind) for i, r in enumerate(results, start=1))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Walkability calibration survey — {html.escape(city)}</title><style>{CSS}</style></head><body>
<h1>Walkability calibration survey — {html.escape(city)}</h1>
<p class="sub">{len(results)} routes across {html.escape(city)} · model = the HDI-style two-level score.
For each card give an <b>ideal_score (0–100)</b> + reasoning in the matching
<code>route_name</code> row of <code>calibration_targets.{html.escape(city.split()[0].lower())}.csv</code>.</p>
{'<div class="blindbanner"><b>Blind pass.</b> The model&#39;s scores (overall number, dimension bars, flags) are hidden and the routes are shuffled — so your <code>ideal_score</code> is an independent judgment, not an echo of the model. Segment colours + Street View remain to help you read the ground.</div>' if blind else ''}
<div class="intro"><b>How to read each card, and what to record</b>
<ol>
<li><b>Map</b>: the route, each <b>segment</b> (one street) drawn + numbered and coloured by its walk_score (red→green). Hover or click a segment for detail + Street View; the numbers match the segment table.</li>
{'<li><b>Scores are hidden</b> in this blind pass — no overall number or dimension bars. Judge walkability from the map geometry + Street View, on the 0–100 scale, on your own.</li>' if blind else '<li><b>Big number</b> = the model&#39;s current walk_score (0–100); <b>bars</b> = its three length-weighted dimensions — <b>safety</b> (cars + eyes-on-street), <b>comfort</b> (surface/material/width), <b>path</b> (real walking right-of-way). Shown for context — your <code>ideal_score</code> is your own call.</li>'}
<li>The grey chip after each title (e.g. <code>high#0</code>) is the <b>route_name</b> — the row key in the CSV. Fill <code>ideal_score</code>, <code>confidence</code>, <code>tier</code>, <code>notes</code>; the copy-paste block at the bottom of each card mirrors those columns.</li>
</ol>
<b>What to sweat</b> (the fit needs relative structure, not absolute precision): get the <b>ordering</b> right (is the car-free path clearly above the busy stroad?) and the <b>tier gaps</b> (how <i>much</i> higher — 3 points or 12?). Two or three confident hard anchors (worst ≈ , best ≈ ) pin the scale; the middle interpolates.</div>
{cards}
</body></html>"""


# ---------------------------------------------------------------------------
# Auto-picked survey — driven by the verify_city route battery (no hand-picking)
# ---------------------------------------------------------------------------

def auto_pick_routes(candidates: list[dict], k: int = 15) -> list[dict]:
    """Pick ~``k`` routes spanning the observed walk-score spectrum plus every
    "pinned" case (anchor / seam / water) from the verify_city battery output.

    ``candidates`` are the dicts ``route_types.run_battery`` emits (keys:
    ``name, area, origin, dest, alpha, walk, look_for, pin``). Pinned cases are
    always kept (the extremes and the interesting data cases); the rest fill
    across even quantiles of ``walk`` so the human sees the full range. Returns
    ``case`` dicts ready for ``_survey`` (helper keys ``walk``/``pin`` stripped),
    ordered high→low walk for the deck.
    """
    seen: set = set()
    uniq: list[dict] = []
    for c in candidates:
        key = (round(c["origin"][0], 4), round(c["origin"][1], 4),
               round(c["dest"][0], 4), round(c["dest"][1], 4))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    pinned = [c for c in uniq if c.get("pin")]
    rest = sorted((c for c in uniq if not c.get("pin")), key=lambda c: c["walk"])
    picked = list(pinned)
    slots = max(0, k - len(picked))
    if slots and rest:
        if slots == 1:
            idxs = [len(rest) // 2]
        else:
            idxs = sorted({round(i * (len(rest) - 1) / (slots - 1)) for i in range(slots)})
        picked.extend(rest[i] for i in idxs)

    picked.sort(key=lambda c: c["walk"], reverse=True)
    return [{key: v for key, v in c.items() if key not in ("walk", "pin")}
            for c in picked]


# ---------------------------------------------------------------------------
# calibration_targets.<city>.csv — the standing "score + reasoning" ground truth
# ---------------------------------------------------------------------------
#
# This is the go-forward calibration format (it replaced the rigid 1-5
# ``subj_walkability`` in ground_truth.csv). The human fills FOUR columns —
# ``ideal_score`` (0-100), ``confidence``, ``tier``, ``notes`` — everything else
# is a pre-filled model-side reference, refreshed on every run so drift analysis
# reads against the current build. Column semantics: calibration_targets.README.md.

TARGET_FIELDS = [
    "route_name", "area", "ideal_score", "confidence", "tier", "notes",
    "model_score", "model_safety", "model_comfort", "model_path", "model_len_m",
]
_HUMAN_FIELDS = ("ideal_score", "confidence", "tier", "notes")


def targets_path(city: str) -> Path:
    """Per-city targets file — Boston is the base name, others get a sibling
    (mirrors the ground_truth.<city>.csv convention)."""
    name = "calibration_targets.csv" if city == "boston" \
        else f"calibration_targets.{city}.csv"
    return Path(__file__).with_name(name)


def _area_label(area: str) -> str:
    """The battery's ``area`` embeds a live ``· walk=0.NN`` suffix that churns
    every build; strip it so the label column stays stable."""
    return area.split(" · walk=")[0]


def sync_targets_csv(results: list[dict], city: str,
                     out: Path | None = None) -> tuple[Path, int, int]:
    """Merge the auto-survey routes into the per-city calibration_targets CSV.

    Model-side columns are (re)written from ``results`` so the reference snapshot
    tracks the current build; the four human columns are PRESERVED for any route
    already rated (keyed on ``route_name``) and left blank for new routes. Safe to
    re-run — it never clobbers a filled ``ideal_score``. Returns
    ``(path, n_rows, n_unrated)``.
    """
    out = out or targets_path(city)
    prior: dict[str, dict] = {}
    if out.exists():
        with out.open(newline="") as fh:
            for row in csv.DictReader(fh):
                prior[row.get("route_name", "")] = row

    rows, unrated = [], 0
    for r in results:
        if not r.get("found"):
            continue
        cats = r["categories"]
        name = r.get("name", "")
        keep = prior.get(name, {})
        row = {
            "route_name": name,
            "area": _area_label(r.get("area", "")),
            "ideal_score": keep.get("ideal_score", ""),
            "confidence": keep.get("confidence", ""),
            "tier": keep.get("tier", ""),
            "notes": keep.get("notes", ""),
            "model_score": round(r["walk"] * 100),
            "model_safety": round(cats.get("safety", float("nan")), 2),
            "model_comfort": round(cats.get("comfort", float("nan")), 2),
            "model_path": round(cats.get("path", float("nan")), 2),
            "model_len_m": round(r["length_m"]),
        }
        if not str(row["ideal_score"]).strip():
            unrated += 1
        rows.append(row)

    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TARGET_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return out, len(rows), unrated


def build_auto_survey(G, candidates: list[dict], city: str, k: int = 15,
                      out: Path | None = None, blind: bool = False) -> Path:
    """Render the auto-picked calibration deck from battery candidates → HTML,
    and sync the per-city calibration_targets CSV stub alongside it.

    Reuses ``_survey`` / ``build_html`` unchanged, so the auto deck has the same
    per-dimension bars, numbered segments and Street View links as the hand-picked
    one. This is the single genuinely-manual verification step: the human reads a
    card and fills ``ideal_score`` + ``notes`` (+ optional ``confidence``/``tier``)
    for the matching ``route_name`` row in ``calibration_targets.<city>.csv``;
    everything structural is automated.
    """
    cases = auto_pick_routes(candidates, k=k)
    default_name = (f"{city}_calibration_survey.auto"
                    f"{'.blind' if blind else ''}.html")
    out = out or Path(__file__).with_name(default_name)
    results = []
    for case in cases:
        try:
            r = _survey(G, case)
        except Exception as exc:
            r = {**case, "found": False, "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)
    # Deck order is shuffled for a blind pass (position leaks the model's ranking);
    # the CSV keeps the stable high→low order — it's keyed on route_name anyway.
    deck = list(results)
    if blind:
        import random
        random.Random(7).shuffle(deck)
    title = f"{city.capitalize()} (auto{', blind' if blind else ''})"
    out.write_text(build_html(deck, city=title, blind=blind))
    print(f"Wrote {sum(1 for r in results if r.get('found'))}/{len(results)} "
          f"auto-picked routes → {out}")
    tpath, nrows, unrated = sync_targets_csv(results, city)
    print(f"Synced {nrows} rows → {tpath.name} "
          f"({unrated} awaiting an ideal_score, {nrows - unrated} already rated)")
    return out


def main():
    from walkability.graph.inventory import CITY_PROFILES

    ap = argparse.ArgumentParser(description="Generate the calibration survey HTML.")
    ap.add_argument("--city", default="boston", choices=sorted(CITY_PROFILES),
                    help="City survey to generate (default: boston).")
    ap.add_argument("--graph", default=None,
                    help="Enriched graph path (default: the city's profile enriched_path).")
    ap.add_argument("--out", default=None,
                    help="Output HTML (default: <city>_calibration_survey[.auto].html).")
    ap.add_argument("--auto", action="store_true",
                    help="Auto-pick routes from the verify_city route battery instead "
                         "of the hand-picked CITY_ROUTES (works for any city).")
    ap.add_argument("--k", type=int, default=15, help="Auto: number of routes to pick.")
    ap.add_argument("--seed", type=int, default=7, help="Auto: battery sampling seed.")
    ap.add_argument("--blind", action="store_true",
                    help="Auto: hide the model's scores (overall + dimension bars + "
                         "flags) and shuffle route order, so the ideal_score pass is "
                         "an independent judgment. Writes *.auto.blind.html.")
    args = ap.parse_args()

    graph_path = args.graph or str(CITY_PROFILES[args.city].enriched_path)
    print(f"Loading {graph_path} ...")
    G = load_graph(Path(graph_path))

    if args.auto:
        import route_types
        ctx = route_types.Ctx(G, CITY_PROFILES[args.city], seed=args.seed)
        cands = route_types.run_battery(ctx, lambda *a, **k: None)[0]
        build_auto_survey(G, cands, args.city, k=args.k,
                          out=Path(args.out) if args.out else None, blind=args.blind)
        return

    if args.city not in CITY_ROUTES:
        raise SystemExit(f"No hand-picked routes for {args.city}; use --auto.")
    routes = CITY_ROUTES[args.city]
    if args.out:
        out = Path(args.out)
    elif args.city == "boston":
        out = Path(__file__).with_name("calibration_survey.html")
    else:
        out = Path(__file__).with_name(f"{args.city}_calibration_survey.html")

    results = []
    for case in routes:
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

    out.write_text(build_html(results, city=args.city.capitalize()))
    print(f"\nWrote {sum(1 for r in results if r.get('found'))}/{len(results)} routes → {out}")


if __name__ == "__main__":
    main()
