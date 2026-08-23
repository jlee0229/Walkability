"""Stage-1 checkpoint: nav geometry core (navGeom / snapToRoute / navBearing).

Injects a real Boston route via the __hpTest hook, then exercises the pure
geometry functions in-page and asserts:
  - on-vertex snap has ~zero residual and exact cumulative progress
  - a 25 m perpendicular offset snaps back with ~25 m residual
  - progress is monotonic walking the polyline and ends at the route total
  - polyline total length agrees with the API's distance_m
  - bearing targeting, smoothing, and 350°→10° wraparound behave
  - the windowed search agrees with a full scan while walking in order
"""
import sys

from helpers import BASE_URL, launch, wait_for_app

CHECKS_JS = """
() => {
  const nav = window.__hpNav;
  const r = window.__hpRoutes[0];
  const g = nav.geom(r);
  const out = { total: g.total, n: g.pts.length };

  // 1. on-vertex snap
  const vi = Math.min(5, g.pts.length - 1);
  const v = g.pts[vi];
  const s1 = nav.snap(g, v[1], v[0], null);
  out.vertexResidual = s1.residualM;
  out.vertexProgressErr = Math.abs(s1.progressM - g.cum[vi]);

  // 2. 25 m perpendicular offset from the midpoint of a mid-route segment
  const si = Math.floor((g.xy.length - 1) / 2);
  const a = g.xy[si], b = g.xy[si + 1];
  const vx = b[0] - a[0], vy = b[1] - a[1];
  const L = Math.hypot(vx, vy) || 1;
  const mx = (a[0] + b[0]) / 2 + (-vy / L) * 25;
  const my = (a[1] + b[1]) / 2 + (vx / L) * 25;
  const lon = g.lon0 + mx / g.kx, lat = g.lat0 + my / g.ky;
  const s2 = nav.snap(g, lat, lon, null);
  out.offsetResidual = s2.residualM;

  // 3. monotonic progress along all vertices + windowed vs global agreement
  let prev = -1, monotonic = true, windowAgrees = true, lastIdx = null;
  for (let i = 0; i < g.pts.length; i++) {
    const p = g.pts[i];
    const w = nav.snap(g, p[1], p[0], lastIdx);
    const f = nav.snap(g, p[1], p[0], null);
    if (Math.abs(w.progressM - f.progressM) > 1) windowAgrees = false;
    if (w.progressM < prev - 0.5) monotonic = false;
    prev = w.progressM;
    lastIdx = w.segIdx;
  }
  out.monotonic = monotonic;
  out.windowAgrees = windowAgrees;
  out.finalProgress = prev;

  // 4. bearing behaviour
  out.bearingTarget = nav.bearing(g, si, null) === g.brg[si];
  out.bearingSmooth = Math.abs(nav.bearing({brg: {0: 90}}, 0, 0) - 31.5) < 1e-9;
  const wrap = nav.bearing({brg: {0: 10}}, 0, 350);
  out.bearingWrap = Math.abs(wrap - 357) < 1e-9;
  return out;
}
"""


def main():
    with launch() as (browser, page):
        page.goto(BASE_URL + "/?area=boston")
        wait_for_app(page)
        data = page.evaluate(
            "qs => fetch('/api/route?' + qs).then(r => r.json()).then(d => {"
            "  window.__hpRoutes = d.routes;"
            "  window.__hpTest.loadRoutes(d);"
            "  return {n: d.routes.length, dist: d.routes[0].distance_m};"
            "})",
            "olat=42.3588&olon=-71.0638&dlat=42.3541&dlon=-71.0700&alpha=2.5&area=boston",
        )
        assert data["n"] > 0, "no routes returned"
        c = page.evaluate(CHECKS_JS)

        assert c["vertexResidual"] < 0.5, c
        assert c["vertexProgressErr"] < 0.5, c
        assert abs(c["offsetResidual"] - 25) < 2, c
        assert c["monotonic"], c
        assert c["windowAgrees"], c
        assert abs(c["finalProgress"] - c["total"]) < 1, c
        # polyline length vs API distance (edge lengths): allow 5% drift
        assert abs(c["total"] - data["dist"]) / data["dist"] < 0.05, (c["total"], data["dist"])
        assert c["bearingTarget"] and c["bearingSmooth"] and c["bearingWrap"], c
        print(f"OK: geometry checks passed on a {data['dist']:.0f} m route "
              f"({c['n']} vertices, polyline total {c['total']:.1f} m)")


if __name__ == "__main__":
    sys.exit(main())
