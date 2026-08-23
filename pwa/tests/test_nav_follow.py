"""Stage-2 checkpoint: nav lifecycle + chase camera + HUD, driven by ?sim=1.

Asserts, over a simulated walk on a real Boston route:
  - Start walking enters nav mode (body.nav, HUD visible, chrome hidden)
  - progress strictly increases and the camera tracks the snapped fix
  - camera pitch/bearing engage (pitch 48, bearing ~= heading)
  - a user drag pauses follow + shows Re-center; tapping it resumes
  - End walk tears everything down (state cleared, pitch back to 0)
"""

import sys

from helpers import (BASE_URL, OUT_DIR, launch, stub_basemap, wait_for_app,
                     wait_for_map_load)

INJECT = (
    "qs => fetch('/api/route?' + qs).then(r => r.json())"
    ".then(d => { window.__hpTest.loadRoutes(d); return d.routes.length; })"
)
QS = "olat=42.3588&olon=-71.0638&dlat=42.3541&dlon=-71.0700&alpha=2.5&area=boston"

SAMPLE = """
() => {
  const m = window.__hpMap, s = window.__hpNav.state();
  if (!s || !s.snapped) return null;
  const p = m.project(s.snapped);
  const el = m.getContainer();
  return { progress: s.progressM, residual: s.residualM, heading: s.heading,
           follow: s.follow,
           // screen-space error vs the chase anchor: container centre pushed
           // down by the same 18%-of-viewport offset navCamera uses
           anchorErr: Math.hypot(p.x - el.clientWidth / 2,
                                 p.y - (el.clientHeight / 2 + Math.round(window.innerHeight * 0.18))),
           bearing: (m.getBearing() + 360) % 360, pitch: m.getPitch() };
}
"""


def ang_diff(a, b):
    return abs((a - b + 540) % 360 - 180)


def main():
    with launch() as (browser, page):
        stub_basemap(page)
        page.goto(BASE_URL + "/?area=boston&sim=1")
        wait_for_app(page)
        wait_for_map_load(page)
        n = page.evaluate(INJECT, QS)
        assert n > 0, "no routes"

        page.click(".route-card.focused .nav-start")
        assert page.evaluate("document.body.classList.contains('nav')"), "nav mode not entered"
        assert page.evaluate("!document.getElementById('navHud').hidden"), "HUD hidden"
        assert page.evaluate(
            "getComputedStyle(document.getElementById('topbar')).display"
        ) == "none", "topbar still visible in nav mode"
        page.evaluate("window.__hpSim.speed(15)")

        samples = []
        for _ in range(9):
            page.wait_for_timeout(1500)
            s = page.evaluate(SAMPLE)
            if s:
                samples.append(s)
        assert len(samples) >= 5, f"too few fixes arrived: {len(samples)}"

        # progress strictly increases; puck stays on-route
        progs = [s["progress"] for s in samples]
        assert all(b > a for a, b in zip(progs, progs[1:])), progs
        assert all(s["residual"] < 15 for s in samples), [s["residual"] for s in samples]

        # camera engaged and tracking: pitch up, puck projected near the chase
        # anchor (lower-third point), bearing near the smoothed heading
        # (easing lag allowed on both)
        last = samples[-1]
        assert last["pitch"] > 40, last
        cam_err = [s["anchorErr"] for s in samples[2:]]
        assert max(cam_err) < 120, cam_err
        brg_err = [ang_diff(s["bearing"], s["heading"]) for s in samples[3:]]
        assert min(brg_err) < 30, brg_err

        # the focused route line and the rotated puck are actually painted
        rendered = page.evaluate(
            "({line: window.__hpMap.queryRenderedFeatures({layers:['r-line']}).length,"
            "  puck: window.__hpMap.queryRenderedFeatures({layers:['r-navpuck']}).length})"
        )
        assert rendered["line"] > 0, f"route line not rendered: {rendered}"
        assert rendered["puck"] > 0, f"nav puck not rendered: {rendered}"

        page.screenshot(path=str(OUT_DIR / "nav_follow.png"))

        # drag pauses follow, Re-center resumes
        page.mouse.move(210, 500)
        page.mouse.down()
        page.mouse.move(210, 380, steps=6)
        page.mouse.up()
        assert page.evaluate("window.__hpNav.state().follow") is False, "drag didn't pause follow"
        assert page.evaluate("!document.getElementById('navRecenter').hidden"), "Re-center chip missing"
        page.click("#navRecenter")
        assert page.evaluate("window.__hpNav.state().follow") is True, "Re-center didn't resume"

        # exit tears down
        page.click("#navExit")
        assert page.evaluate("window.__hpNav.state()") is None, "nav state survived exit"
        assert not page.evaluate("document.body.classList.contains('nav')"), "body.nav survived"
        page.wait_for_timeout(1200)   # let the exit fitBounds settle
        assert page.evaluate("window.__hpMap.getPitch()") == 0, "pitch not reset"
        assert page.evaluate(
            "getComputedStyle(document.getElementById('sheet')).display"
        ) != "none", "route sheet not restored"

        print(f"OK: follow-me over {progs[-1]:.0f} m — puck within "
              f"{max(cam_err):.0f} px of the chase anchor, drag/recenter/exit "
              f"all behave (screenshot {OUT_DIR / 'nav_follow.png'})")


if __name__ == "__main__":
    sys.exit(main())
