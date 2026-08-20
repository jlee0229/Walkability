"""
Vision calibration — street-level imagery for the calibration battery routes.

The scoring pipeline is fact-based (OSM tags + city inventories). This script
adds an independent PERCEPTION channel to the calibration process: for every
route in the auto-derived `verify_city` battery (the same routes that populate
`calibration_targets.<city>.csv`), it samples a few stops along the route
geometry, fetches the best available street-level photo at each stop from an
API-friendly imagery source, and emits:

  * `notebooks/vision/<city>/<route>/stopNN.<source>.<id>.jpg` — the photos;
  * `notebooks/vision/<city>/manifest.json` — stop coords/bearings + image
    provenance (source, id, capture date, distance, heading delta);
  * `notebooks/<city>_vision_stops.html` — a contact sheet, one card per route;
  * `notebooks/vision_scores.<city>.csv` — a merge-preserving rating stub, the
    vision sibling of `calibration_targets.<city>.csv`, keyed on `route_name`.

A vision-capable rater (a human, or a VLM agent reading the JPEGs) fills
`vision_score` (0–100) + reasoning per route — judging the *subjective* street
experience the fact model can't see: is the sidewalk actually there, buffer
quality, shade, blank walls vs storefronts. `--compare` then joins the three
channels (model / human `ideal_score` / vision) to show where they diverge.

Calibration-only by design: nothing here feeds routing or the enriched graph.
Imagery is historical — every image carries its capture date, and ratings
inherit that staleness (the disclaimer is printed on the contact sheet).

Known limitation: candidate selection is geometric (distance/bearing/age), not
map-matched, so a sequence driving a road *under* the stop (Big Dig tunnels)
can win — it is horizontally nearest. The rater must treat such frames as
off-route context; the real fix is matching sequences to walkable ways.

Sources (`--source`):
  * `mapillary` — primary; needs a (free) token from --token, $MAPILLARY_TOKEN,
    or the git-ignored file `notebooks/.mapillary_token` (checked in that order).
  * `kartaview` — fallback; no token required, sparser/older coverage.
  * `auto` (default) — mapillary if a token is available, else kartaview.

Usage (run from repo root):
  python notebooks/vision_calibration.py --city boston --fetch
  python notebooks/vision_calibration.py --city boston --fetch --limit 3   # smoke
  python notebooks/vision_calibration.py --city boston --plan             # no network
  python notebooks/vision_calibration.py --city boston --compare

The battery run (graph load + selectors) is the slow part, so the derived
cases are cached at `vision/<city>/cases.json`; `--refresh-cases` re-derives
after a rebuild (the cache stores the graph file's mtime and warns on drift).
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:  # optional: pano cropping + EXIF orientation (graceful no-op without it)
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None

# Siblings (repo convention: notebooks/ import each other when run from root).
from diagnostics import streetview_url, mapillary_url

NB_DIR = Path(__file__).parent
UA = {"User-Agent": "walkability-vision-calibration/0.1 (research prototype)"}
HTTP_TIMEOUT = 20
RADIUS_M = 60.0          # stop search radius; sidewalk-scale but GPS-tolerant
STOP_SPAN = (0.10, 0.90)  # sample inside the route, away from snap artefacts
CROSS_MAX_M = 25.0       # max perpendicular offset from the route polyline —
                         # ~one street width; kills parallel-street cameras
ALONG_MAX_M = 60.0       # max offset along the route from the stop point
TRACK_WINDOW_M = 90.0    # how far along the polyline to search for the projection

VISION_FIELDS = [
    "route_name", "area", "vision_score", "confidence", "tier",
    "sidewalk", "buffer", "notes", "n_images", "image_dates",
]
_RATER_FIELDS = ("vision_score", "confidence", "tier", "sidewalk", "buffer", "notes")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _ang_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _cumdist(coords: list[tuple[float, float]]) -> list[float]:
    cum = [0.0]
    for p, q in zip(coords, coords[1:]):
        cum.append(cum[-1] + _haversine_m(p, q))
    return cum


def annotate_track(coords: list[tuple[float, float]], cum: list[float],
                   stop_cum: float, cands: list[dict]) -> None:
    """Attach `cross_m` (perpendicular offset from the route polyline) and
    `along_m` (offset along the route from the stop) to each candidate.

    Straight-line distance can't tell "50 m ahead on the same street" (fine)
    from "50 m away on the parallel street" (wrong corridor). Projecting the
    camera onto the polyline separates the two so they can be filtered
    asymmetrically. Only segments within TRACK_WINDOW_M of the stop are
    considered, in a local equirectangular frame centred on the stop."""
    lo, hi = stop_cum - TRACK_WINDOW_M, stop_cum + TRACK_WINDOW_M
    lat0 = coords[0][0]
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 111320.0

    segs = []
    for i in range(len(coords) - 1):
        if cum[i + 1] < lo or cum[i] > hi:
            continue
        (alat, alon), (blat, blon) = coords[i], coords[i + 1]
        segs.append((alat * ky, alon * kx, blat * ky, blon * kx, cum[i]))

    for c in cands:
        py, px = c["lat"] * ky, c["lon"] * kx
        best = None
        for ay, ax, by, bx, c0 in segs:
            vy, vx = by - ay, bx - ax
            L2 = vy * vy + vx * vx
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((py - ay) * vy + (px - ax) * vx) / L2))
            qy, qx = ay + t * vy, ax + t * vx
            cross = math.hypot(py - qy, px - qx)
            along = (c0 + t * math.sqrt(L2)) - stop_cum
            if best is None or cross < best[0]:
                best = (cross, along)
        if best:
            c["cross_m"], c["along_m"] = round(best[0], 1), round(best[1], 1)


def sample_stops(coords: list[tuple[float, float]], n: int | None = None) -> list[dict]:
    """Evenly spaced stops along the polyline, each with the local walking bearing.

    Stop count scales gently with route length (3 for a short block, up to 6 for
    a multi-km route) — enough looks to judge, few enough to stay ratable.
    """
    cum = _cumdist(coords)
    total = cum[-1]
    if n is None:
        n = max(3, min(6, 1 + round(total / 1200)))
    lo, hi = STOP_SPAN
    fracs = [lo + (hi - lo) * i / (n - 1) for i in range(n)] if n > 1 else [0.5]

    stops = []
    for i, f in enumerate(fracs, start=1):
        target = f * total
        j = max(1, min(len(cum) - 1, next(
            (k for k, c in enumerate(cum) if c >= target), len(cum) - 1)))
        seg_len = cum[j] - cum[j - 1] or 1.0
        t = (target - cum[j - 1]) / seg_len
        p, q = coords[j - 1], coords[j]
        lat = p[0] + t * (q[0] - p[0])
        lon = p[1] + t * (q[1] - p[1])
        # Local bearing over a window a few vertices wide — steadier than one segment.
        b0 = coords[max(0, j - 3)]
        b1 = coords[min(len(coords) - 1, j + 2)]
        stops.append({
            "stop": i, "frac": round(f, 3), "lat": round(lat, 6),
            "lon": round(lon, 6), "cum_m": round(target),
            "bearing": round(_bearing_deg(b0, b1), 1),
        })
    return stops


# ---------------------------------------------------------------------------
# Imagery sources — each returns candidate dicts with a common shape:
# {source, id, lat, lon, heading|None, pano, captured 'YYYY-MM-DD', url, page, dist_m}
# ---------------------------------------------------------------------------

def fetch_mapillary(sess: requests.Session, lat: float, lon: float,
                    radius: float, token: str) -> list[dict]:
    dlat = radius / 111320.0
    dlon = radius / (111320.0 * math.cos(math.radians(lat)) or 1.0)
    bbox = f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}"
    r = sess.get(
        "https://graph.mapillary.com/images",
        params={
            # limit must comfortably exceed the in-circle density: the API
            # returns an arbitrary page, and the distance cap then filters —
            # too small a page can leave zero eligible candidates in dense areas.
            "access_token": token, "bbox": bbox, "limit": 400,
            "fields": "id,geometry,compass_angle,captured_at,thumb_1024_url,is_pano,sequence",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for d in r.json().get("data", []):
        try:
            ilon, ilat = d["geometry"]["coordinates"]
        except (KeyError, TypeError, ValueError):
            continue
        cap = d.get("captured_at")
        dt = datetime.fromtimestamp(cap / 1000, timezone.utc) if cap else None
        out.append({
            "source": "mapillary", "id": str(d["id"]),
            "lat": ilat, "lon": ilon,
            "heading": d.get("compass_angle"),
            "pano": bool(d.get("is_pano")),
            "captured": dt.date().isoformat() if dt else None,
            "hour_local": ((dt.hour + ilon / 15.0) % 24) if dt else None,
            "sequence": d.get("sequence"),
            "url": d.get("thumb_1024_url"),
            "page": f"https://www.mapillary.com/app/?pKey={d['id']}",
            "dist_m": _haversine_m((lat, lon), (ilat, ilon)),
        })
    return out


def fetch_kartaview(sess: requests.Session, lat: float, lon: float,
                    radius: float) -> list[dict]:
    r = sess.get(
        "https://api.openstreetcam.org/2.0/photo/",
        params={"lat": lat, "lng": lon, "radius": int(radius), "itemsPerPage": 80},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for d in r.json().get("result", {}).get("data", []):
        try:
            ilat, ilon = float(d["lat"]), float(d["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        heading = d.get("heading")
        stamp = d.get("shotDate") or d.get("dateAdded") or ""
        shot = stamp.split(" ")[0] or None
        try:  # "YYYY-MM-DD HH:MM:SS(.f)" assumed UTC
            hour = int(stamp.split(" ")[1].split(":")[0])
            hour_local = (hour + ilon / 15.0) % 24
        except (IndexError, ValueError):
            hour_local = None
        # lth = large thumbnail (~1024px) — plenty for rating, light to store;
        # proc (full-size, plates blurred) stays reachable via `page`.
        url = d.get("fileurlLTh") or d.get("fileurlProc")
        out.append({
            "source": "kartaview", "id": str(d["id"]),
            "lat": ilat, "lon": ilon,
            "heading": float(heading) if heading not in (None, "", "null") else None,
            "pano": d.get("isWrapped") == "1",
            "captured": shot,
            "hour_local": hour_local,
            "sequence": str(d.get("sequenceId")) if d.get("sequenceId") else None,
            "url": url,
            "page": f"https://kartaview.org/details/{d.get('sequenceId')}/{d.get('sequenceIndex')}",
            "dist_m": _haversine_m((lat, lon), (ilat, ilon)),
        })
    return out


def pick_image(cands: list[dict], bearing: float, radius: float,
               used_ids: set | None = None,
               seq_stops: dict | None = None) -> dict | None:
    """Best stop photo: ON the route corridor, facing along (or back along) the
    walk, recent, taken in daylight, not already used by another stop, and —
    all else near-equal — from a sequence that follows the whole route.

    Corridor gate (when track offsets are annotated): cross-track ≤ CROSS_MAX_M
    (~one street width — rejects cameras on parallel streets that plain distance
    can't tell apart) and |along-track| ≤ ALONG_MAX_M. Without annotations
    (`--plan`-less sources, tests) it falls back to straight distance ≤ radius.

    Score (metre-equivalents) =
        1.5·cross + 0.25·|along|   (lateral offset is the dangerous direction)
      + 0.4·radius·alignment       (unknown heading = 0.45)
      + 2.5·age_years              (a 2015 shot must beat a 2025 one by ~25 m)
      + 25 if night-time           (soft: last resort, not excluded)
      + 3 if pano                  (croppable, but flat frames are sharper)
      − 8/4 if its sequence spans ≥3 / 2 stops of this route — a contributor
        driving or walking the corridor is on-route evidence no single frame has.
    """
    now_year = datetime.now(timezone.utc).year
    scored = []
    for c in cands:
        if not c.get("url"):
            continue
        if used_ids and c["id"] in used_ids:
            continue
        if "cross_m" in c:
            if c["cross_m"] > CROSS_MAX_M or abs(c["along_m"]) > ALONG_MAX_M:
                continue
            base = 1.5 * c["cross_m"] + 0.25 * abs(c["along_m"])
        else:
            if c["dist_m"] > radius:
                continue
            base = c["dist_m"]
        if c["heading"] is None:
            align = 0.45
        else:
            d = min(_ang_diff(c["heading"], bearing),
                    _ang_diff(c["heading"], (bearing + 180.0) % 360.0))
            align = d / 90.0                       # 0 aligned … 1 perpendicular
        age = max(0, now_year - int(c["captured"][:4])) if c.get("captured") else 6
        night = (c.get("hour_local") is not None
                 and not (6.0 <= c["hour_local"] <= 19.0))
        span = len(seq_stops.get(c.get("sequence"), ())) if seq_stops else 0
        seq_bonus = 8.0 if span >= 3 else (4.0 if span == 2 else 0.0)
        score = (base + 0.4 * radius * align + 2.5 * age
                 + (25.0 if night else 0.0) + (3.0 if c["pano"] else 0.0)
                 - seq_bonus)
        scored.append((score, c))
    if not scored:
        return None
    return min(scored, key=lambda t: t[0])[1]


def _save_image(raw: bytes, path: Path, pano: bool,
                heading: float | None, bearing: float) -> bool:
    """Write the photo, normalising EXIF orientation; yaw-crop panos to a
    ~140° directed view centred on the walking bearing. Returns True if the
    pano was cropped. Falls back to raw bytes without Pillow or on any error."""
    if Image is None:
        path.write_bytes(raw)
        return False
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        cropped = False
        if pano and heading is not None and img.width >= 2 * img.height * 0.9:
            w, h = img.size
            yaw = (bearing - heading + 540.0) % 360.0 - 180.0   # [-180, 180)
            centre = (0.5 + yaw / 360.0) * w
            half = w * (140.0 / 360.0) / 2.0
            tiled = Image.new(img.mode, (2 * w, h))
            tiled.paste(img, (0, 0)); tiled.paste(img, (w, 0))
            x0 = centre - half + (w if centre - half < 0 else 0)
            box = (int(x0), int(0.15 * h), int(x0 + 2 * half), int(0.85 * h))
            img = tiled.crop(box)
            cropped = True
        img.convert("RGB").save(path, "JPEG", quality=88)
        return cropped
    except Exception:
        path.write_bytes(raw)
        return False


# ---------------------------------------------------------------------------
# Battery cases (cached — the graph+battery run is the slow part)
# ---------------------------------------------------------------------------

def vision_dir(city: str) -> Path:
    return NB_DIR / "vision" / city


def scores_path(city: str) -> Path:
    return NB_DIR / f"vision_scores.{city}.csv"


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def derive_cases(city: str, k: int, seed: int, refresh: bool = False) -> list[dict]:
    """The auto-picked battery cases for `city` — identical derivation to
    `calibration_survey --auto`, so `route_name` keys line up with
    `calibration_targets.<city>.csv`. Cached per city with the graph mtime."""
    from walkability.graph.inventory import CITY_PROFILES

    profile = CITY_PROFILES[city]
    gpath = Path(profile.enriched_path)
    cache = vision_dir(city) / "cases.json"
    if cache.exists() and not refresh:
        data = json.loads(cache.read_text())
        if data.get("graph_mtime") != gpath.stat().st_mtime:
            print("  [WARN] graph rebuilt since cases.json was derived — "
                  "route_names may drift; use --refresh-cases to re-derive.")
        if data.get("k") != k or data.get("seed") != seed:
            print(f"  [WARN] cases.json was derived with k={data.get('k')} "
                  f"seed={data.get('seed')} (asked for k={k} seed={seed}); "
                  "use --refresh-cases to re-derive.")
        return data["cases"]

    from walkability.graph.build import load_graph
    import route_types
    from calibration_survey import auto_pick_routes

    print(f"Deriving battery cases for {city} (slow path: graph + battery) ...")
    G = load_graph(gpath)
    ctx = route_types.Ctx(G, profile, seed=seed)
    candidates = route_types.run_battery(ctx, lambda *a, **kw: None)[0]
    cases = auto_pick_routes(candidates, k=k)

    # Routing here (with the graph already resident) keeps fetch runs graph-free.
    from walkability.routing.router import find_routes
    from walkability.routing.cost import ALPHA_DEFAULT
    from calibration_survey import _route_coords, _area_label

    enriched = []
    for case in cases:
        alpha = case.get("alpha", ALPHA_DEFAULT)
        routes = find_routes(G, tuple(case["origin"]), tuple(case["dest"]), alpha=alpha)
        if not routes:
            print(f"  [WARN] no route for {case['name']} — skipped")
            continue
        coords = _route_coords(G, routes[0])
        enriched.append({
            "name": case["name"],
            "area": _area_label(case["area"]),
            "alpha": alpha,
            "len_m": round(routes[0].total_length),
            "model_score": round(routes[0].walk_score * 100),
            "coords": [(round(la, 6), round(lo, 6)) for la, lo in coords],
        })

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "city": city, "k": k, "seed": seed,
        "graph_mtime": gpath.stat().st_mtime, "graph_file": gpath.name,
        "derived_at": datetime.now().isoformat(timespec="seconds"),
        "cases": enriched,
    }))
    print(f"  cached {len(enriched)} cases → {cache}")
    return enriched


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_city(city: str, cases: list[dict], source: str, token: str | None,
               radius: float, stops_n: int | None, plan_only: bool,
               limit: int | None, routes_filter: str | None) -> dict:
    out_dir = vision_dir(city)
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers.update(UA)

    if routes_filter:
        cases = [c for c in cases if routes_filter in c["name"]]
    if limit:
        cases = cases[:limit]

    manifest = {
        "city": city, "source": source, "radius_m": radius,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "disclaimer": ("Street-level imagery is historical; every image carries "
                       "its capture date and ratings inherit that staleness. "
                       "Calibration/audit input only — never a live routing signal."),
        "routes": [],
    }

    n_img = n_stop = 0
    for case in cases:
        coords = [tuple(c) for c in case["coords"]]
        cum = _cumdist(coords)
        stops = sample_stops(coords, n=stops_n)
        rdir = out_dir / _safe(case["name"])
        entry = {**{k: case[k] for k in ("name", "area", "alpha", "len_m", "model_score")},
                 "stops": []}
        used_ids: set = set()   # within-route dedupe across stops

        # Phase 1 — fetch + corridor-annotate candidates for EVERY stop, so the
        # sequence-span index (which sequences follow this route) exists before
        # any pick is made.
        all_cands: dict[int, list[dict]] = {}
        seq_stops: dict[str, set] = {}
        if not plan_only:
            for s in stops:
                try:
                    if source == "mapillary":
                        cands = fetch_mapillary(sess, s["lat"], s["lon"], radius, token)
                    else:
                        cands = fetch_kartaview(sess, s["lat"], s["lon"], radius)
                except requests.RequestException as exc:
                    print(f"    [WARN] {case['name']} stop {s['stop']}: {exc}")
                    cands = []
                annotate_track(coords, cum, float(s["cum_m"]), cands)
                all_cands[s["stop"]] = cands
                for c in cands:  # span counted over corridor-passing candidates only
                    if c.get("sequence") and "cross_m" in c and c["cross_m"] <= CROSS_MAX_M:
                        seq_stops.setdefault(c["sequence"], set()).add(s["stop"])
                time.sleep(0.15)

        # Phase 2 — pick + download per stop.
        for s in stops:
            n_stop += 1
            s = dict(s)
            s["streetview"] = streetview_url(s["lat"], s["lon"])
            s["mapillary_app"] = mapillary_url(s["lat"], s["lon"])
            s["image"] = None
            if not plan_only:
                best = pick_image(all_cands.get(s["stop"], []), s["bearing"],
                                  radius, used_ids, seq_stops)
                if best:
                    rdir.mkdir(parents=True, exist_ok=True)
                    fname = f"stop{s['stop']:02d}.{best['source']}.{best['id']}.jpg"
                    fpath = rdir / fname
                    try:
                        cropped = False
                        if not fpath.exists():
                            img = sess.get(best["url"], timeout=HTTP_TIMEOUT)
                            img.raise_for_status()
                            cropped = _save_image(img.content, fpath, best["pano"],
                                                  best["heading"], s["bearing"])
                        used_ids.add(best["id"])
                        seq = best.get("sequence")
                        s["image"] = {
                            **{k: best[k] for k in
                               ("source", "id", "heading", "pano", "captured", "page")},
                            "pano_cropped": cropped,
                            "dist_m": round(best["dist_m"], 1),
                            "cross_m": best.get("cross_m"),
                            "along_m": best.get("along_m"),
                            "sequence": seq,
                            "seq_span": len(seq_stops.get(seq, ())) if seq else 0,
                            "path": str(fpath.relative_to(NB_DIR)),
                        }
                        n_img += 1
                    except requests.RequestException as exc:
                        print(f"    [WARN] download failed {case['name']} "
                              f"stop {s['stop']}: {exc}")
            entry["stops"].append(s)
        got = sum(1 for s in entry["stops"] if s["image"])
        print(f"  {case['name']:<42} {got}/{len(entry['stops'])} stops imaged")
        manifest["routes"].append(entry)

    mpath = out_dir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=1))
    print(f"\n{n_img}/{n_stop} stops imaged → {mpath}")
    return manifest


# ---------------------------------------------------------------------------
# Rating stub CSV (merge-preserving, mirrors sync_targets_csv)
# ---------------------------------------------------------------------------

def sync_scores_csv(manifest: dict, city: str) -> tuple[Path, int, int]:
    out = scores_path(city)
    prior: dict[str, dict] = {}
    if out.exists():
        with out.open(newline="") as fh:
            for row in csv.DictReader(fh):
                prior[row.get("route_name", "")] = row

    rows, unrated = [], 0
    for r in manifest["routes"]:
        keep = prior.get(r["name"], {})
        dates = sorted({s["image"]["captured"] for s in r["stops"]
                        if s["image"] and s["image"]["captured"]})
        row = {
            "route_name": r["name"], "area": r["area"],
            **{f: keep.get(f, "") for f in _RATER_FIELDS},
            "n_images": sum(1 for s in r["stops"] if s["image"]),
            "image_dates": " ".join(dates),
        }
        if not str(row["vision_score"]).strip():
            unrated += 1
        rows.append(row)

    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=VISION_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return out, len(rows), unrated


# ---------------------------------------------------------------------------
# Contact sheet
# ---------------------------------------------------------------------------

_SHEET_CSS = """
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:1100px;margin:0 auto;padding:24px;color:#222;background:#faf8f4}
h1{font-size:24px;margin:0 0 4px}.sub{color:#666;margin:0 0 14px}
.disc{background:#2c2a26;color:#ede7da;border-radius:10px;padding:10px 14px;margin:0 0 18px;font-size:13.5px}
.card{background:#fff;border:1px solid #e6e0d6;border-radius:10px;padding:14px 16px;margin:0 0 20px}
.card h2{font-size:17px;margin:0 0 2px}.card h2 .rk{font:12px/1 ui-monospace,Menlo,monospace;color:#8a8578;background:#f2efe8;border:1px solid #e6e0d6;border-radius:5px;padding:2px 6px;margin-left:8px;vertical-align:middle}
.meta{font-size:13px;color:#666;margin:0 0 8px}
.strip{display:flex;gap:10px;flex-wrap:wrap}
.stopc{width:255px}.stopc img{width:100%;border-radius:6px;border:1px solid #e6e0d6;display:block}
.cap{font-size:12px;color:#666;margin:3px 0 0}.cap b{color:#333}.cap a{margin-right:8px}
.nostop{width:255px;height:150px;border:1px dashed #cfc8ba;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#999;font-size:12.5px;flex-direction:column;gap:4px}
.tmpl{background:#f2efe8;border-radius:8px;padding:8px 10px;margin:10px 0 0;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:#555}
"""


def build_contact_sheet(manifest: dict, city: str) -> Path:
    out = NB_DIR / f"{city}_vision_stops.html"
    cards = []
    for r in manifest["routes"]:
        cells = []
        for s in r["stops"]:
            links = (f'<a href="{s["streetview"]}" target="_blank">SV</a>'
                     f'<a href="{s["mapillary_app"]}" target="_blank">Mly</a>')
            if s["image"]:
                im = s["image"]
                cells.append(
                    f'<div class="stopc"><a href="{html.escape(im["page"])}" target="_blank">'
                    f'<img src="{html.escape(im["path"])}" loading="lazy"></a>'
                    f'<div class="cap"><b>#{s["stop"]}</b> {im["captured"] or "undated"} · '
                    f'{im["dist_m"]:.0f} m off · {im["source"]} · {links}</div></div>')
            else:
                cells.append(
                    f'<div class="nostop"><div>#{s["stop"]} — no image</div>'
                    f'<div class="cap">{links}</div></div>')
        tmpl = html.escape(
            f"[{r['name']}]\n  vision_score (0-100): \n  confidence: sure|rough\n"
            f"  tier: car_free|buffered|ped_priority|good|mixed|poor|hostile\n"
            f"  sidewalk: yes|partial|no|unseen\n"
            f"  buffer: none|parked_cars|planting|separated|car_free\n  notes: ")
        cards.append(
            f'<div class="card"><h2>{html.escape(r["area"])}'
            f'<span class="rk">{html.escape(r["name"])}</span></h2>'
            f'<p class="meta">{r["len_m"]} m · alpha={r["alpha"]}</p>'
            f'<div class="strip">{"".join(cells)}</div>'
            f'<div class="tmpl">{tmpl}</div></div>')
    n_img = sum(1 for r in manifest["routes"] for s in r["stops"] if s["image"])
    n_stop = sum(len(r["stops"]) for r in manifest["routes"])
    out.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Vision calibration stops — {html.escape(city)}</title><style>{_SHEET_CSS}</style></head><body>
<h1>Vision calibration stops — {html.escape(city)}</h1>
<p class="sub">{len(manifest['routes'])} battery routes · {n_img}/{n_stop} stops imaged ·
source: {html.escape(manifest['source'])} · fetched {html.escape(manifest['fetched_at'])} ·
ratings go in <code>vision_scores.{html.escape(city)}.csv</code></p>
<div class="disc"><b>Historical imagery.</b> {html.escape(manifest['disclaimer'])}</div>
{''.join(cards)}
</body></html>""")
    print(f"Contact sheet → {out}")
    return out


# ---------------------------------------------------------------------------
# Compare — model vs human ideal_score vs vision_score
# ---------------------------------------------------------------------------

def compare(city: str) -> None:
    from calibration_survey import targets_path

    tpath, vpath = targets_path(city), scores_path(city)
    if not vpath.exists():
        raise SystemExit(f"{vpath.name} missing — run --fetch and rate first.")
    with tpath.open(newline="") as fh:
        targets = {r["route_name"]: r for r in csv.DictReader(fh)}
    with vpath.open(newline="") as fh:
        visions = list(csv.DictReader(fh))

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    print(f"\n{'route_name':<38}{'model':>6}{'ideal':>7}{'vision':>7}"
          f"{'v-m':>6}{'v-i':>6}  tier(v)      notes")
    print("-" * 110)
    dm, di = [], []
    for v in visions:
        t = targets.get(v["route_name"], {})
        m, i, vs = _f(t.get("model_score")), _f(t.get("ideal_score")), _f(v.get("vision_score"))
        if vs is not None and m is not None:
            dm.append(vs - m)
        if vs is not None and i is not None:
            di.append(vs - i)
        print(f"{v['route_name']:<38}"
              f"{m if m is not None else '—':>6}"
              f"{i if i is not None else '—':>7}"
              f"{vs if vs is not None else '—':>7}"
              f"{f'{vs - m:+.0f}' if None not in (vs, m) else '—':>6}"
              f"{f'{vs - i:+.0f}' if None not in (vs, i) else '—':>6}"
              f"  {v.get('tier', ''):<12} {v.get('notes', '')[:44]}")
    if dm:
        print(f"\nvision - model:  mean {sum(dm)/len(dm):+.1f}  "
              f"mean|Δ| {sum(abs(d) for d in dm)/len(dm):.1f}  (n={len(dm)})")
    if di:
        print(f"vision - ideal:  mean {sum(di)/len(di):+.1f}  "
              f"mean|Δ| {sum(abs(d) for d in di)/len(di):.1f}  (n={len(di)})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _token_file() -> str | None:
    """Mapillary token from the git-ignored sibling file, if present."""
    p = NB_DIR / ".mapillary_token"
    if p.exists():
        tok = p.read_text().strip()
        return tok or None
    return None


def _token_file() -> str | None:
    """Mapillary token from the git-ignored sibling file, if present."""
    p = NB_DIR / ".mapillary_token"
    if p.exists():
        tok = p.read_text().strip()
        return tok or None
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Fetch street-level imagery for the calibration battery routes.")
    ap.add_argument("--city", default="boston")
    ap.add_argument("--fetch", action="store_true", help="Fetch images + write outputs.")
    ap.add_argument("--plan", action="store_true",
                    help="Stops + manifest only, no network (dry run).")
    ap.add_argument("--compare", action="store_true",
                    help="Join model / ideal_score / vision_score and print deltas.")
    ap.add_argument("--source", default="auto",
                    choices=("auto", "mapillary", "kartaview"))
    ap.add_argument("--token", default=None,
                    help="Mapillary access token (default: $MAPILLARY_TOKEN).")
    ap.add_argument("--radius", type=float, default=RADIUS_M)
    ap.add_argument("--stops", type=int, default=None,
                    help="Force stops per route (default: 3-6 by length).")
    ap.add_argument("--k", type=int, default=15, help="Battery auto-pick size.")
    ap.add_argument("--seed", type=int, default=7, help="Battery sampling seed.")
    ap.add_argument("--limit", type=int, default=None, help="First N routes (smoke).")
    ap.add_argument("--routes", default=None, help="Substring filter on route_name.")
    ap.add_argument("--refresh-cases", action="store_true",
                    help="Re-derive battery cases (after a graph rebuild).")
    args = ap.parse_args()

    if args.compare:
        compare(args.city)
        return
    if not (args.fetch or args.plan):
        raise SystemExit("Pick a mode: --fetch, --plan, or --compare.")

    token = args.token or os.environ.get("MAPILLARY_TOKEN") or _token_file()
    source = args.source
    if source == "auto":
        source = "mapillary" if token else "kartaview"
        if not token:
            print("[INFO] no $MAPILLARY_TOKEN — falling back to kartaview "
                  "(tokenless, but sparser/older coverage).")
    if source == "mapillary" and not token:
        raise SystemExit("Mapillary needs a token: set $MAPILLARY_TOKEN or pass "
                         "--token (free at mapillary.com/dashboard/developers).")

    cases = derive_cases(args.city, k=args.k, seed=args.seed,
                         refresh=args.refresh_cases)
    manifest = fetch_city(args.city, cases, source, token, args.radius,
                          args.stops, args.plan, args.limit, args.routes)
    if not args.plan:
        path, n, unrated = sync_scores_csv(manifest, args.city)
        print(f"Synced {n} rows → {path.name} ({unrated} awaiting a vision_score)")
        build_contact_sheet(manifest, args.city)


if __name__ == "__main__":
    main()
