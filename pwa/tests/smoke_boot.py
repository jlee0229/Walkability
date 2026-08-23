"""Stage-0 harness smoke test: the PWA boots headlessly against a local server.

Asserts the app shell initializes (config fetched, MapLibre map constructed) and
saves a screenshot. Run from repo root with the server already up:

    uvicorn pwa.server:app --port 7860 &
    python pwa/tests/smoke_boot.py
"""
import sys

from helpers import BASE_URL, OUT_DIR, launch, wait_for_app


def main():
    with launch() as (browser, page):
        page.goto(BASE_URL + "/?area=boston")
        wait_for_app(page)
        cfg_id = page.evaluate("document.getElementById('areaSelect').value")
        assert cfg_id == "boston", f"area select not populated: {cfg_id!r}"
        has_map = page.evaluate("!!window.__hpMap")
        assert has_map, "MapLibre map was not constructed"
        page.wait_for_timeout(4000)  # give tiles a beat for the screenshot
        shot = OUT_DIR / "smoke_boot.png"
        page.screenshot(path=str(shot))
        print(f"OK: app booted, map constructed, screenshot at {shot}")


if __name__ == "__main__":
    sys.exit(main())
