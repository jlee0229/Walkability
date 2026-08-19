# Humanpath PWA

An installable, mobile-first Progressive Web App for Humanpath: a static
MapLibre frontend (`pwa/static/`) served by a small FastAPI backend
(`pwa/server.py`) that runs the CSR routing substrate (~32 MB pickle, ~80 MB
RAM, <0.1 s load). It reuses the repo's routing/scoring verbatim (`find_routes`
dispatches to `routing/csr_router.py`) and the same vendored MapLibre + PMTiles
basemap as the Streamlit app, so scores and routes match the desktop app
route-for-route.

Why a separate frontend instead of wrapping the Streamlit app: Streamlit gives
no control over the served `<head>`, so a real installable PWA (manifest +
service worker) isn't possible there, and every interaction round-trips the
server. Here the UI is instant, the map is GPU vector tiles, and the only
network calls are geocoding + one `/api/route` per search.

## Run locally

```bash
pip install -r pwa/requirements.txt   # once (fastapi + uvicorn)
venv/bin/uvicorn pwa.server:app --port 8123
# then open http://localhost:8123
```

The server loads `data/osm/boston_walk_enriched.csr.pkl` (falls back to the
runtime pickle, then to downloading either from the data-v1 GitHub Release).

## Features

- **Address search** — same geocoder behaviour as the app (Photon primary with
  the hard-filtering metro bbox, timed Nominatim fallback, coverage check).
- **Use my location** — `navigator.geolocation` (the crosshair button in the
  From field); shows a blue device dot, reverse-geocodes a "near …" label.
  Needs HTTPS in production (any host provides it) — localhost is exempt.
- **Walk-style slider** — the same 0–100 → alpha 0–5 mapping.
- **Swipeable route cards** — the focused route follows the centred card;
  Details opens confidence, the weakest stretch, safety/comfort/path dimension
  bars, and per-block score colouring on the map.
- **Installable** — manifest + icons + service worker (shell cached
  stale-while-revalidate; `/api` and cross-origin tile/font requests are never
  intercepted). Android shows an Install chip; iOS installs via
  Share → Add to Home Screen.

## Deploy (Hugging Face Docker Space)

See `deploy-hf-pwa.sh` (one-time Space + remote setup in its header). The
image is small (no streamlit/osmnx/geopandas — see `pwa/requirements.txt`);
the graph pickle is fetched from the data-v1 release at first boot, so
`boston_walk_enriched.csr.pkl` must be uploaded there:

```bash
gh release upload data-v1 data/osm/boston_walk_enriched.csr.pkl
```
