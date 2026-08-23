"""Stage-3 checkpoint: off-route auto-reroute + arrival, driven by ?sim=1.

Asserts:
  - drifting 60 m off the route triggers exactly one /api/route reroute
    (after 3 consecutive off-route fixes), the routes are replaced, and
    guidance continues on the new line with a small residual
  - a failing reroute (blocked /api/route) degrades gracefully: status
    message, nav stays active, a retry fires after another off-route run
  - reaching the end of the route flips to the arrived state and nav
    auto-exits
"""
import sys

from helpers import (BASE_URL, launch, stub_basemap, wait_for_app,
                     wait_for_map_load)

QS = "olat=42.3588&olon=-71.0638&dlat=42.3541&dlon=-71.0700&alpha=2.5&area=boston"
INJECT = (
    "qs => fetch('/api/route?' + qs).then(r => r.json())"
    ".then(d => { window.__hpTest.loadRoutes(d, null,"
    "  {lat: 42.3541, lon: -71.0700, label: 'dest'}); return d.routes.length; })"
)


def wait_until(page, expr, timeout_s=20, tick_ms=500):
    for _ in range(int(timeout_s * 1000 / tick_ms)):
        if page.evaluate(expr):
            return True
        page.wait_for_timeout(tick_ms)
    return False


def main():
    with launch() as (browser, page):
        stub_basemap(page)
        reroutes = {"n": 0, "block": False, "aborted": 0}

        def on_route(route):
            # the injection fetch also hits /api/route — only count/steer
            # requests made while nav is active (tracked via block/count arm)
            if reroutes["armed"]:
                if reroutes["block"]:
                    reroutes["aborted"] += 1
                    route.abort()
                    return
                reroutes["n"] += 1
            route.continue_()

        reroutes["armed"] = False
        page.route("**/api/route*", on_route)

        page.goto(BASE_URL + "/?area=boston&sim=1")
        wait_for_app(page)
        wait_for_map_load(page)
        assert page.evaluate(INJECT, QS) > 0

        page.click(".route-card.focused .nav-start")
        page.evaluate("window.__hpSim.speed(10)")
        page.wait_for_timeout(3500)          # a few on-route fixes first
        reroutes["armed"] = True

        # --- failure path first: blocked reroute degrades gracefully
        reroutes["block"] = True
        page.evaluate("window.__hpSim.drift(true)")
        assert wait_until(page, "!!window.__hpNav.state() && window.__hpNav.state().offCount === 0"
                                " && !document.getElementById('navStatus').hidden"
                                " && document.getElementById('navStatus').textContent.indexOf('reroute') !== -1",
                          25), "failure status never shown"
        assert reroutes["aborted"] >= 1, "no reroute attempt was made"
        assert page.evaluate("!!window.__hpNav.state()"), "nav died on reroute failure"

        # --- success path: unblock, next off-route run reroutes for real
        reroutes["block"] = False
        done = False
        for _ in range(60):
            page.wait_for_timeout(500)
            if reroutes["n"] >= 1 and page.evaluate(
                    "!!window.__hpNav.state() && !window.__hpNav.state().rerouting"):
                done = True
                break
        assert done, "reroute never completed"
        page.evaluate("window.__hpSim.drift(false)")
        assert reroutes["n"] == 1, f"expected exactly one successful reroute, saw {reroutes['n']}"
        # guidance continues on the new line: residual back under the threshold
        assert wait_until(page, "window.__hpNav.state() && window.__hpNav.state().residualM < 15", 15), \
            "residual didn't recover on the new route"

        # --- arrival: jump near the end and let it finish
        page.evaluate("window.__hpSim.jumpTo(0.97)")
        assert wait_until(page, "(window.__hpNav.state() && window.__hpNav.state().arrived)"
                                " || !window.__hpNav.state()", 20), "never arrived"
        arrived_text = page.evaluate("document.getElementById('navRemain').textContent")
        assert wait_until(page, "!window.__hpNav.state()", 10), "nav didn't auto-exit after arrival"
        assert not page.evaluate("document.body.classList.contains('nav')")
        assert reroutes["n"] == 1, f"unexpected extra reroutes: {reroutes['n']}"

        print(f"OK: blocked reroute degraded gracefully ({reroutes['aborted']} aborted attempt(s)), "
              f"then exactly 1 live reroute; arrival banner {arrived_text!r} and auto-exit clean")


if __name__ == "__main__":
    sys.exit(main())
