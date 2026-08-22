---
title: Humanpath
emoji: 🚶
colorFrom: red
colorTo: yellow
sdk: docker
app_port: 8501
pinned: false
short_description: Walkability-aware walking routes — Boston & Austin
thumbnail: https://raw.githubusercontent.com/jlee0229/Walkability/main/app/humanpath_icon.png
---

# Humanpath — walkability-aware pedestrian routing for Boston

Enriches the Boston OpenStreetMap walk network with per-edge **walkability scores**
(road type, sidewalk condition & material, foot access) and routes between two
points with a tunable distance-vs-walkability tradeoff. Includes a Streamlit web app.

## What it does

- **Scoring pipeline** (`walkability/graph/build.py`) attaches independent per-factor
  scores to every edge, layering Boston DPW sidewalk-inventory data over OSM tags,
  plus a **safety factor** (`walkability/graph/environment.py`) built from car-safety
  (speed-based, road-separated, industrial-corridor-aware) and perceived safety
  (activity/enclosure/openness). Scores combine as a two-level, HDI-style aggregate —
  weighted mean within each of three dimensions (safety, comfort, path), then a
  floored geometric mean across dimensions — so a bad dimension can't be bought back
  by a good one.
- **Routing** (`walkability/routing/`) finds walkability-ranked routes with **A\***
  + penalty-method alternatives, then refines each candidate's exact street sides
  in a two-phase pass to cut gratuitous road crossings. An `alpha` knob trades
  distance for walkability; forced customers-only endpoints (e.g. a zoo entrance)
  aren't penalised.
- **Web app** (`app/streamlit_app.py`, branded "Humanpath") — address input,
  `alpha` + per-factor weight sliders, route cards with a weakest-stretch/per-segment
  breakdown, and a **MapLibre GL vector map** (self-hosted Protomaps basemap) as the
  default, falling back to a folium map if WebGL is unavailable.

See [CLAUDE.md](CLAUDE.md) for the full architecture and design rationale.

## Project layout

- `walkability/` — the installable package: graph build/enrichment, scoring, routing.
- `app/` — the Streamlit "Humanpath" UI and its MapLibre map component.
- `notebooks/` — documented diagnostics/QA tooling (regression harness, verification,
  calibration survey) — see CLAUDE.md's "Diagnostics & verification tooling".
- `Research/` — active design specs and the running decision log.
- `archive/` — superseded one-off exploration scripts and closed session logs,
  kept for history only; not part of the live pipeline.

## Setup

```bash
pip install -e .
```

### Data (not in the repo — too large for git)

The enriched graph and source datasets are excluded via `.gitignore`. To rebuild:

```bash
python walkability/graph/download.py            # download the base Boston OSM walk graph
python walkability/graph/download_environment.py # download OSM layers for the safety factor
python -m walkability.graph.build                # build the enriched graph
python -m walkability.graph.compact              # convert to the slim runtime pickle the app loads
```

The Boston DPW **sidewalk inventory** shapefile (condition/material/width) must be
obtained separately from the City of Boston open-data portal and placed under
`data/boston/sidewalk_inventory/`. Without it the pipeline falls back to OSM tags.

## Run

```bash
streamlit run app/streamlit_app.py           # web app
python -m walkability.routing.router         # routing smoke test (dev subset)
python notebooks/verify_system.py            # automated invariant checks
```

A deployed instance (Hugging Face Space, `deploy-hf.sh`) downloads the graph data
from a GitHub Release on first run rather than requiring it in the repo.
