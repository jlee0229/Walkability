"""Shared Playwright plumbing for the PWA checkpoint scripts.

These are dev-only scripts (not packaged, not shipped in the Docker image's CMD
path). They expect a locally running server:

    uvicorn pwa.server:app --port 7860

Chromium: uses Playwright's managed browser when the pinned build is present
(PLAYWRIGHT_BROWSERS_PATH honoured), otherwise falls back to the preinstalled
/opt/pw-browsers/chromium executable.
"""
import contextlib
import os
import pathlib

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("HP_TEST_URL", "http://127.0.0.1:7860")
OUT_DIR = pathlib.Path(__file__).parent / "out"


@contextlib.contextmanager
def launch(**context_kwargs):
    OUT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            exe = "/opt/pw-browsers/chromium"
            browser = p.chromium.launch(executable_path=exe)
        try:
            ctx = browser.new_context(
                viewport={"width": 420, "height": 850},  # phone-ish portrait
                **context_kwargs,
            )
            page = ctx.new_page()
            page.set_default_timeout(20000)
            yield browser, page
        finally:
            browser.close()


def wait_for_app(page):
    """Wait until app.js booted: config loaded and the map object exists."""
    page.wait_for_function("!!window.__hpMap")


# The sandbox proxy can't reach the remote PMTiles/glyph hosts, and a failed
# vector source keeps MapLibre's "load" from ever firing — which blocks the
# app's route layers. Swapping the config's basemap for a local minimal style
# lets "load" fire so the GeoJSON layer stack renders and can be asserted on.
MINIMAL_STYLE = {
    "version": 8,
    "sources": {},
    "layers": [{"id": "bg", "type": "background",
                "paint": {"background-color": "#e8e2d4"}}],
}


def stub_basemap(page):
    def _cfg(route):
        cfg = route.fetch().json()
        cfg["style"] = {"type": "url", "url": "/teststyle.json"}
        route.fulfill(json=cfg)
    page.route("**/api/config*", _cfg)
    page.route("**/teststyle.json", lambda r: r.fulfill(json=MINIMAL_STYLE))


def wait_for_map_load(page):
    """Wait for the app's route layers (added on the map 'load' event)."""
    page.wait_for_function("window.__hpMap && !!window.__hpMap.getSource('routes')")


def fetch_routes(page, olat, olon, dlat, dlon, alpha=2.5, area="boston"):
    """Fetch routes from the local API inside the page (same-origin)."""
    qs = f"olat={olat}&olon={olon}&dlat={dlat}&dlon={dlon}&alpha={alpha}&area={area}"
    return page.evaluate(
        "qs => fetch('/api/route?' + qs).then(r => r.json())", qs
    )
