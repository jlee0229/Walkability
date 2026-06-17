---
title: Humanpath
emoji: 🚶
colorFrom: red
colorTo: yellow
sdk: docker
app_port: 8501
pinned: false
short_description: Walkability-aware walking routes for Boston
thumbnail: https://raw.githubusercontent.com/jlee0229/Walkability/main/app/humanpath_icon.png
---

# Humanpath — walkability-aware pedestrian routing for Boston

Enriches the Boston OpenStreetMap walk network with per-edge **walkability scores**
(road type, sidewalk condition & material, foot access) and routes between two
points with a tunable distance-vs-walkability tradeoff. Includes a Streamlit web app.

## What it does

- **Scoring pipeline** (`walkability/graph/build.py`) attaches independent per-factor
  scores to every edge, layering Boston DPW sidewalk-inventory data over OSM tags.
- **Routing** (`walkability/routing/`) finds walkability-ranked routes with **A\***
  + penalty-method alternatives. An `alpha` knob trades distance for walkability;
  a route's overall score is a worst-segment-aware power mean; forced
  customers-only endpoints (e.g. a zoo entrance) aren't penalised.
- **Web app** (`app/streamlit_app.py`) — address input,
  `alpha` + per-factor weight sliders, and a folium map coloured by per-edge score.

See [CLAUDE.md](CLAUDE.md) for the full architecture and design rationale.

## Setup

```bash
pip install -e .
```

### Data (not in the repo — too large for git)

The enriched graph and source datasets are excluded via `.gitignore`. To rebuild:

```bash
python walkability/graph/download.py        # download the base Boston OSM walk graph
python -m walkability.graph.build            # build the enriched graph
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
