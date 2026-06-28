"""Sparse-eyes route set — ground-truth coverage for the "Seaport problem".

Seaport (congress) is the survey's biggest miss: the user rated it 80 ("wide
sidewalk, buffered") but the model scored it 70, because its `eyes_score` is low
(~0.5) — new-development / waterfront blocks have few foot-traffic POIs and little
residential enclosure, so perceived-safety reads sparse even where the street is
pleasant. Fixing that on n=1 risks overfitting (and we're about to add less-walkable
cities). This set adds more routes with the SAME signature — new/waterfront/
converted-industrial, wide-but-quiet — so the eyes calibration has a real sample.

These were picked by confirming a low mean ``eyes_score`` (and were *not* the high-
eyes candidates — Navy Yard, Longwood campus, West End all came back ~0.8 and were
dropped). Survey them like the main set: is the model under-scoring a genuinely
fine new-development street, or is the sparse reading correct?

Run (reuses calibration_survey's renderer; does not modify it):
    python notebooks/sparse_eyes_routes.py
    → writes notebooks/sparse_eyes_survey.html
"""

from __future__ import annotations

from pathlib import Path

SPARSE_EYES_ROUTES: list[dict] = [
    {
        "name": "seaport_northern_ave",
        "area": "Seaport (Northern Ave core)",
        "origin": (42.3522, -71.0430), "dest": (42.3505, -71.0385),
        "look_for": "Core Seaport, like the original case. Wide new sidewalks, sparse street-level activity (eyes ~0.43). Does it under-score the way seaport_congress did (you: 80, model: 70)?",
    },
    {
        "name": "seaport_blvd_convention",
        "area": "Seaport (Convention / D St)",
        "origin": (42.3470, -71.0455), "dest": (42.3445, -71.0420),
        "look_for": "Convention-center side: big blocks, few shops, wide sidewalks. Eyes read low (~0.42). Is it actually fine to walk, or genuinely empty/unpleasant?",
    },
    {
        "name": "fort_point_channel",
        "area": "Fort Point (converted warehouses)",
        "origin": (42.3505, -71.0510), "dest": (42.3475, -71.0530),
        "look_for": "Converted-industrial loft district — characterful but quiet, channel-side. Sparse eyes (~0.48) plus arterial exposure pulls it lower (model 62). Too low?",
    },
    {
        "name": "eastie_jeffries_point",
        "area": "East Boston (new waterfront)",
        "origin": (42.3635, -71.0375), "dest": (42.3675, -71.0345),
        "look_for": "New residential waterfront (Clippership Wharf-style). Newer build, moderate eyes (~0.68). A 'new development that's actually nice' test — is the model fair here?",
    },
    {
        "name": "southie_marine_park",
        "area": "South Boston (Marine Industrial Park / Black Falcon)",
        "origin": (42.3445, -71.0335), "dest": (42.3415, -71.0300),
        "look_for": "The extreme of sparse — working marine-industrial waterfront, almost no eyes (~0.14), model 51. Is this one correctly low (truly unwalkable), unlike the buffered Seaport blocks? The contrast that keeps us from over-lifting all sparse routes.",
    },
]


def _main() -> None:
    import calibration_survey as csv_mod  # sibling module; run from repo root
    from walkability.graph.build import ENRICHED_PATH, load_graph

    print(f"Loading {ENRICHED_PATH} ...")
    G = load_graph(ENRICHED_PATH)
    results = []
    for case in SPARSE_EYES_ROUTES:
        try:
            r = csv_mod._survey(G, case)
        except Exception as exc:  # noqa: BLE001
            r = {**case, "found": False, "error": f"{type(exc).__name__}: {exc}"}
        tag = "NO ROUTE" if not r.get("found") else (
            f"walk={r['walk']*100:.0f} safety={r['categories'].get('safety', float('nan')):.2f} "
            f"len={r['length_m']:.0f}m")
        print(f"  {case['name']:<26} {tag}")
        results.append(r)

    out = Path(__file__).with_name("sparse_eyes_survey.html")
    out.write_text(csv_mod.build_html(results))
    print(f"\nWrote {sum(1 for r in results if r.get('found'))}/{len(results)} routes → {out}")


if __name__ == "__main__":
    _main()
