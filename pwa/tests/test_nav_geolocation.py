"""Stage-4 checkpoint: the REAL geolocation path (no sim) + wake lock.

Drives navigator.geolocation via Playwright's mocked device location:
  - watchPosition fixes advance progress and engage the chase camera
  - the screen wake lock is requested on start and released on exit
  - with location permission denied, starting nav toasts and exits cleanly
"""
import sys

from helpers import (BASE_URL, launch, stub_basemap, wait_for_app,
                     wait_for_map_load)

QS = "olat=42.3588&olon=-71.0638&dlat=42.3541&dlon=-71.0700&alpha=2.5&area=boston"
INJECT = (
    "qs => fetch('/api/route?' + qs).then(r => r.json())"
    ".then(d => { window.__hpRoutes = d.routes; window.__hpTest.loadRoutes(d, null,"
    "  {lat: 42.3541, lon: -71.0700, label: 'dest'}); return d.routes.length; })"
)

WAKE_SPY = """
window.__wakeSpy = { requests: 0, releases: 0 };
Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: {
  request: function () {
    window.__wakeSpy.requests++;
    return Promise.resolve({ release: function () {
      window.__wakeSpy.releases++; return Promise.resolve();
    }});
  },
}});
"""


def main():
    start = {"latitude": 42.3588, "longitude": -71.0638, "accuracy": 5}

    # --- happy path: granted permission, fixes walked along the route
    with launch(permissions=["geolocation"], geolocation=start) as (browser, page):
        stub_basemap(page)
        page.add_init_script(WAKE_SPY)
        page.goto(BASE_URL + "/?area=boston")          # no sim flag: real watch
        wait_for_app(page)
        wait_for_map_load(page)
        assert page.evaluate(INJECT, QS) > 0

        page.click(".route-card.focused .nav-start")
        assert page.evaluate("document.body.classList.contains('nav')")
        assert page.evaluate("window.__wakeSpy.requests") >= 1, "wake lock not requested"

        # walk the mocked device along the route polyline
        pts = page.evaluate(
            "window.__hpNav.geom(window.__hpRoutes[0]).pts.filter((p, i) => i % 8 === 0)")
        prog = []
        for lon, lat in pts[:10]:
            page.context.set_geolocation(
                {"latitude": lat, "longitude": lon, "accuracy": 5})
            page.wait_for_timeout(700)
            s = page.evaluate("window.__hpNav.state()")
            if s and s["snapped"]:
                prog.append(s["progressM"])
        assert len(prog) >= 5, f"too few watchPosition fixes: {len(prog)}"
        assert prog[-1] > prog[0] + 50, f"progress didn't advance: {prog}"
        assert all(b >= a - 0.5 for a, b in zip(prog, prog[1:])), prog
        assert page.evaluate("window.__hpMap.getPitch()") > 40, "chase camera not engaged"

        page.click("#navExit")
        assert page.evaluate("window.__hpNav.state()") is None
        page.wait_for_timeout(300)
        assert page.evaluate("window.__wakeSpy.releases") >= 1, "wake lock not released"

    # --- denied permission: nav refuses politely and tears down. Headless
    # Chromium never actually fires PERMISSION_DENIED (it just withholds
    # fixes), so stub watchPosition to deliver the code-1 error the platform
    # would — the branch under test is the app's handling of it.
    with launch() as (browser, page):
        stub_basemap(page)
        page.add_init_script(
            "navigator.geolocation.watchPosition = function (ok, err) {"
            "  setTimeout(function () { err({ code: 1, message: 'denied' }); }, 50);"
            "  return 99; };")
        page.goto(BASE_URL + "/?area=boston")
        wait_for_app(page)
        wait_for_map_load(page)
        assert page.evaluate(INJECT, QS) > 0
        page.click(".route-card.focused .nav-start")
        page.wait_for_timeout(2500)                    # denial is async
        assert page.evaluate("window.__hpNav.state()") is None, "nav survived denied permission"
        assert not page.evaluate("document.body.classList.contains('nav')")
        assert page.evaluate("!document.getElementById('toast').hidden"), "no toast on denial"

    print(f"OK: real watchPosition advanced {prog[-1] - prog[0]:.0f} m over "
          f"{len(prog)} fixes with wake lock held; denied permission exits cleanly")


if __name__ == "__main__":
    sys.exit(main())
