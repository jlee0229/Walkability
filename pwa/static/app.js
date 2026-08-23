/* Humanpath PWA frontend.
 *
 * Map rendering is a port of the Streamlit MapLibre component (main.js): same
 * Protomaps brand flavor, same route layer stack (alt / halo / focused /
 * segment + O/D points), same geojson-vt guards (tolerance:0, buffer:512).
 * On top of that: the search flow (geocode -> route), swipeable route cards,
 * a per-route details drawer, device geolocation, and PWA install/SW wiring.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- consts
  var ACCENT = "#b1592e", INK = "#211e18", HALO = "#faf8f2", YOU = "#22648c";
  var WALK_SPEED_MPS = 1.33;
  var EMPTY_FC = { type: "FeatureCollection", features: [] };
  var MYLOC = "My location";

  var BRAND_FLAVOR = {
    background: "#f0ebe0", earth: "#f4efe4", water: "#c2d4d6",
    buildings: "#e7dcc6", pedestrian: "#efe9da",
    park_a: "#cde0bd", wood_a: "#c6dcb2", scrub_a: "#d2e0cc",
  };

  // ----------------------------------------------------------------- state
  var state = {
    config: null,
    routes: [],
    focus: 0,
    segmented: false,
    detailOpen: false,
    origin: null,          // {lat, lon, label, device}
    dest: null,
    youLoc: null,          // last device fix [lon, lat]
    nav: null,             // follow-me walking session (see startNav)
  };

  var $ = function (id) { return document.getElementById(id); };
  var topbar = $("topbar"), sheet = $("sheet"), cardsEl = $("cards"),
      detailEl = $("detail"), fromInput = $("fromInput"), toInput = $("toInput"),
      goBtn = $("goBtn"), locBtn = $("locBtn"), swapBtn = $("swapBtn"),
      alphaSlider = $("alphaSlider"), alphaWord = $("alphaWord"),
      hint = $("hint"), toastEl = $("toast"), tripPill = $("tripPill"),
      pillFrom = $("pillFrom"), pillTo = $("pillTo"),
      fabs = $("fabs"), fitChip = $("fitChip"), sampleBtn = $("sampleBtn"),
      installBtn = $("installBtn");

  // ------------------------------------------------------------ formatting
  function scoreHex(s01) {
    var s = s01 * 100;
    if (s >= 80) return "#3f8f5f";
    if (s >= 65) return "#789b3e";
    if (s >= 50) return "#c8922f";
    return "#c0512f";
  }
  function distStr(m) {
    var mi = m / 1609.34;
    if (mi < 0.1) return Math.round(m * 3.28084 / 10) * 10 + " ft";
    return mi.toFixed(2) + " mi";
  }
  function timeStr(m) {
    return Math.max(1, Math.round(m / WALK_SPEED_MPS / 60)) + " min";
  }
  function alphaWordFor(v) {
    return v < 15 ? "Shortest path" : v < 35 ? "Lean shorter"
         : v < 58 ? "Balanced" : v < 82 ? "Lean walkable" : "Best walk";
  }

  // toast(msg, opts): opts.kind "info" = muted notice dot (default is the
  // terracotta problem dot); opts.action {label, fn} shows a tappable trailing
  // label and makes the toast sticky until acted on; opts.ms overrides the
  // auto-hide delay.
  var toastTimer = null, toastAction = null;
  function toast(msg, opts) {
    opts = opts || {};
    $("toastText").textContent = msg;
    toastEl.className = "toast" + (opts.kind === "info" ? " toast-info" : "");
    toastAction = opts.action || null;
    var act = $("toastAction");
    act.hidden = !toastAction;
    if (toastAction) act.textContent = toastAction.label;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    if (!toastAction) {
      toastTimer = setTimeout(function () { toastEl.hidden = true; }, opts.ms || 4200);
    }
  }
  toastEl.addEventListener("click", function () {
    if (toastAction) toastAction.fn();
    else toastEl.hidden = true;
  });

  // ------------------------------------------------------------------- map
  var map = null, styleLoaded = false, pendingFC = null;
  // "Fit route" chip state: the analytically computed fit target, and a quiet
  // window covering the fit animation (an eased fitBounds can fire moveend
  // more than once, so "first moveend after fit" is not a reliable reference).
  var fitCam = null, fitQuietUntil = 0;

  function resolveStyle(pmtilesUrl) {
    var flavor = Object.assign({}, basemaps.namedFlavor("light"), BRAND_FLAVOR);
    return {
      version: 8,
      glyphs: "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf",
      sprite: "https://protomaps.github.io/basemaps-assets/sprites/v4/light",
      sources: {
        protomaps: {
          type: "vector", url: "pmtiles://" + pmtilesUrl,
          attribution: '<a href="https://protomaps.com">Protomaps</a> © <a href="https://openstreetmap.org">OpenStreetMap</a>',
        },
      },
      layers: basemaps.layers("protomaps", flavor, { lang: "en" }),
    };
  }

  // Per-area basemap: Boston ships the brand PMTiles cut; other cities can use
  // a plain style URL (e.g. OpenFreeMap positron) — both come from /api/config.
  function basemapStyle(cfg) {
    var s = cfg.style || { type: "pmtiles", url: cfg.pmtiles };
    return s.type === "pmtiles" ? resolveStyle(s.url) : s.url;
  }

  function initMap(cfg) {
    if (typeof maplibregl === "undefined") { toast("The map failed to load. Refresh to try again."); return; }
    if (maplibregl.addProtocol && typeof pmtiles !== "undefined") {
      try { maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile); } catch (e) {}
    }
    map = new maplibregl.Map({
      container: "map",
      style: basemapStyle(cfg),
      center: cfg.center, zoom: 12.6,
      attributionControl: { compact: true },
    });
    // "Fit route" chip: appears once the camera has wandered from the fitted
    // view (any route on screen), disappears on refit.
    map.on("moveend", function () {
      if (state.nav) return;   // chase camera moves constantly — no fit chip
      if (!state.routes.length || !fitCam || Date.now() < fitQuietUntil) return;
      var p0 = map.project(fitCam.c), p1 = map.project(map.getCenter());
      var dx = p0.x - p1.x, dy = p0.y - p1.y;
      if (Math.sqrt(dx * dx + dy * dy) > 48 || Math.abs(map.getZoom() - fitCam.z) > 0.3) {
        fitChip.hidden = false;
      }
    });
    map.on("styleimagemissing", function (e) {
      if (map.hasImage(e.id)) return;
      map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) });
    });
    map.on("error", function (e) { console.error("[maplibre]", e && e.error); });
    // A user pan during nav pauses the chase camera ("free look"); the
    // Re-center chip resumes it. dragstart fires only on user gestures, never
    // on easeTo animations, so the chase camera can't un-follow itself.
    map.on("dragstart", function () {
      if (!state.nav) return;
      state.nav.follow = false;
      navRecenter.hidden = false;
    });
    map.on("load", function () {
      styleLoaded = true;
      addRouteLayers();
      if (pendingFC) { setMapData(pendingFC[0], pendingFC[1]); pendingFC = null; }
    });
    window.__hpMap = map;      // debug handles
    window.__hpToast = toast;
  }

  function addRouteLayers() {
    if (map.getSource("routes")) return;
    // tolerance:0 + buffer:512 — same geojson-vt guards as the desktop map, so
    // long straight spans (bridges, Esplanade) don't vanish in a mid-zoom band.
    map.addSource("routes", { type: "geojson", data: EMPTY_FC, tolerance: 0, buffer: 512 });
    map.addSource("points", { type: "geojson", data: EMPTY_FC });
    var round = { "line-cap": "round", "line-join": "round" };
    map.addLayer({
      id: "r-alt", type: "line", source: "routes",
      filter: ["==", ["get", "role"], "alt"],
      layout: { "line-cap": "butt", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.55,
               "line-dasharray": [2, 2] },
    });
    map.addLayer({
      id: "r-halo", type: "line", source: "routes",
      filter: ["==", ["get", "role"], "halo"], layout: round,
      paint: { "line-color": HALO, "line-width": 10 },
    });
    map.addLayer({
      id: "r-line", type: "line", source: "routes",
      filter: ["match", ["get", "role"], ["focused", "segment"], true, false], layout: round,
      paint: { "line-color": ["get", "color"], "line-width": 6 },
    });
    map.addLayer({
      id: "r-joints", type: "circle", source: "points",
      filter: ["==", ["get", "role"], "joint"],
      paint: { "circle-radius": 3, "circle-color": HALO,
               "circle-stroke-width": 1.5, "circle-stroke-color": INK,
               "circle-stroke-opacity": 0.55 },
    });
    map.addLayer({
      id: "r-you", type: "circle", source: "points",
      filter: ["==", ["get", "role"], "you"],
      paint: { "circle-radius": 7, "circle-color": YOU,
               "circle-stroke-width": 3, "circle-stroke-color": "#fff" },
    });
    map.addLayer({
      id: "r-points", type: "circle", source: "points",
      filter: ["match", ["get", "role"], ["origin", "dest"], true, false],
      paint: { "circle-radius": 7,
               "circle-color": ["match", ["get", "role"], "origin", ACCENT, INK],
               "circle-stroke-width": 3, "circle-stroke-color": HALO },
    });
    // Nav puck: a map-aligned arrow rotated to the walking heading (canvas-
    // drawn — no sprite dependency). Sits above every other point layer.
    if (!map.hasImage("nav-puck")) map.addImage("nav-puck", navPuckImage());
    map.addLayer({
      id: "r-navpuck", type: "symbol", source: "points",
      filter: ["==", ["get", "role"], "navpuck"],
      layout: { "icon-image": "nav-puck", "icon-size": 0.6,
                "icon-rotate": ["get", "heading"],
                "icon-rotation-alignment": "map",
                "icon-allow-overlap": true, "icon-ignore-placement": true },
    });
    // Tap tooltips (mobile): block score / O-D labels.
    var popup = new maplibregl.Popup({ closeButton: false, offset: 12, className: "hp-pop" });
    ["r-line", "r-alt", "r-points"].forEach(function (id) {
      map.on("click", id, function (e) {
        var f = e.features && e.features[0];
        if (!f || !f.properties || !f.properties.label) return;
        var at = (id === "r-points") ? f.geometry.coordinates : e.lngLat;
        popup.setLngLat(at).setText(f.properties.label).addTo(map);
      });
    });
  }

  function setMapData(routesFC, pointsFC) {
    if (!map) return;
    if (!styleLoaded) { pendingFC = [routesFC, pointsFC]; return; }
    map.getSource("routes").setData(routesFC || EMPTY_FC);
    map.getSource("points").setData(pointsFC || EMPTY_FC);
  }

  // ------------------------------------------------- route -> GeoJSON (port)
  function fullCoords(route) {
    if (route._full) return route._full;
    var out = [];
    route.segments.forEach(function (seg) {
      seg.coords.forEach(function (c) {
        var last = out[out.length - 1];
        if (!last || last[0] !== c[0] || last[1] !== c[1]) out.push(c);
      });
    });
    route._full = out;
    return out;
  }

  function lineFeat(coords, props) {
    return { type: "Feature", properties: props,
             geometry: { type: "LineString", coordinates: coords } };
  }

  function buildFC() {
    var routes = state.routes, focus = state.focus, segmented = state.segmented;
    var feats = [], points = [];
    if (state.nav && state.nav.snapped) {
      points.push({ type: "Feature",
                    properties: { role: "navpuck", heading: state.nav.heading || 0 },
                    geometry: { type: "Point", coordinates: state.nav.snapped } });
    } else if (state.youLoc) {
      points.push({ type: "Feature", properties: { role: "you", label: MYLOC },
                    geometry: { type: "Point", coordinates: state.youLoc } });
    }
    if (!routes.length) {
      return [{ type: "FeatureCollection", features: feats },
              { type: "FeatureCollection", features: points }];
    }
    routes.forEach(function (r, i) {
      if (i === focus) return;
      feats.push(lineFeat(fullCoords(r), {
        role: "alt", color: scoreHex(r.score),
        label: "Walk score " + Math.round(r.score * 100) + "/100" }));
    });
    var focal = routes[focus];
    var full = fullCoords(focal);
    feats.push(lineFeat(full, { role: "halo" }));
    if (segmented) {
      focal.segments.forEach(function (seg, j) {
        feats.push(lineFeat(seg.coords, {
          role: "segment", color: scoreHex(seg.score),
          label: "walk " + Math.round(seg.score * 100) + "/100 · " + seg.highway }));
        if (j > 0 && seg.coords.length) {
          points.push({ type: "Feature", properties: { role: "joint" },
                        geometry: { type: "Point", coordinates: seg.coords[0] } });
        }
      });
    } else {
      feats.push(lineFeat(full, {
        role: "focused", color: scoreHex(focal.score),
        label: "Walk score " + Math.round(focal.score * 100) + "/100" }));
    }
    points.push(
      { type: "Feature", properties: { role: "origin", label: "Start" },
        geometry: { type: "Point", coordinates: full[0] } },
      { type: "Feature", properties: { role: "dest", label: "Destination" },
        geometry: { type: "Point", coordinates: full[full.length - 1] } });
    return [{ type: "FeatureCollection", features: feats },
            { type: "FeatureCollection", features: points }];
  }

  function redrawMap() {
    var fc = buildFC();
    setMapData(fc[0], fc[1]);
  }

  function fitToFocused(animate) {
    var routes = state.routes;
    if (!routes.length || !map) return;
    var full = fullCoords(routes[state.focus]);
    var minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
    full.forEach(function (c) {
      if (c[0] < minLon) minLon = c[0];
      if (c[0] > maxLon) maxLon = c[0];
      if (c[1] < minLat) minLat = c[1];
      if (c[1] > maxLat) maxLat = c[1];
    });
    // Cap the pads so an open detail drawer can never push fitBounds past the
    // canvas (MapLibre throws when padding exceeds the viewport).
    var padTop = Math.min(
      document.body.classList.contains("collapsed")
        ? tripPill.offsetHeight + 34 : topbar.offsetHeight + 34,
      window.innerHeight * 0.3);
    var padBottom = Math.min(sheet.hidden ? 40 : sheet.offsetHeight + 24,
                             window.innerHeight * 0.45);
    var bounds = [[minLon, minLat], [maxLon, maxLat]];
    var padding = { top: padTop, bottom: padBottom, left: 30, right: 30 };
    try {
      var cam = map.cameraForBounds(bounds, { padding: padding });
      if (cam) fitCam = { c: cam.center, z: Math.min(cam.zoom, 17) };
    } catch (e) { fitCam = null; }
    fitQuietUntil = Date.now() + (animate ? 800 : 0) + 450;
    fitChip.hidden = true;
    map.fitBounds(bounds, { padding: padding, duration: animate ? 800 : 0, maxZoom: 17,
                            bearing: 0, pitch: 0 });
  }

  // ------------------------------------------------------------ route cards
  function renderCards() {
    var routes = state.routes;
    cardsEl.innerHTML = "";
    routes.forEach(function (r, i) {
      var col = scoreHex(r.score);
      var card = document.createElement("article");
      card.className = "route-card" + (i === state.focus ? " focused" : "");
      card.innerHTML =
        '<div class="card-top">' +
          '<span class="card-name">' + (i === 0 ? "Recommended" : "Alternative " + i) + "</span>" +
          (i === 0 ? '<span class="badge">Best fit</span>' : "") +
          '<button class="card-details-btn">Details</button>' +
        "</div>" +
        '<div class="score-row">' +
          '<span class="score" style="color:' + col + '">' + Math.round(r.score * 100) + "</span>" +
          '<span class="score-100">/ 100</span><span class="score-tag">Walk score</span>' +
        "</div>" +
        '<div class="bar"><div class="bar-fill" style="width:' +
          Math.max(4, Math.round(r.score * 100)) + "%;background:" + col + '"></div></div>' +
        '<div class="card-meta"><b>' + distStr(r.distance_m) + "</b> · <b>" +
          timeStr(r.distance_m) + "</b> walk</div>" +
        '<div class="card-via">' + (r.via ? "via " + r.via : r.segments.length + " blocks") + "</div>" +
        '<button class="nav-start">Start walking</button>';
      card.addEventListener("click", function (e) {
        if (e.target.classList.contains("nav-start")) { startNav(); return; }
        var wantDetail = e.target.classList.contains("card-details-btn");
        if (state.focus !== i) setFocus(i);
        if (wantDetail) toggleDetail(state.detailOpen && state.focus === i ? false : true);
        card.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
      });
      cardsEl.appendChild(card);
    });
    sheet.hidden = routes.length === 0;
    renderDetail();
    positionFabs();
  }

  function setFocus(i) {
    if (i === state.focus) return;
    state.focus = i;
    state.segmented = false;
    Array.prototype.forEach.call(cardsEl.children, function (el, j) {
      el.classList.toggle("focused", j === i);
    });
    renderDetail();
    positionFabs();
    redrawMap();
  }

  function toggleDetail(open) {
    state.detailOpen = open;
    renderDetail();
    positionFabs();
    fitToFocused(false);
  }

  var DIM_LABELS = { safety: "Safety", comfort: "Comfort", path: "Path" };

  function renderDetail() {
    var r = state.routes[state.focus];
    if (!r || !state.detailOpen) { detailEl.hidden = true; detailEl.innerHTML = ""; return; }
    var dims = "";
    ["safety", "comfort", "path"].forEach(function (k) {
      if (r.dimensions && r.dimensions[k] != null) {
        var v = r.dimensions[k];
        dims += '<div class="dim"><div class="dim-label"><span>' + DIM_LABELS[k] +
          "</span><span>" + Math.round(v * 100) + '</span></div>' +
          '<div class="dim-bar"><div class="dim-fill" style="width:' + Math.round(v * 100) +
          "%;background:" + scoreHex(v) + '"></div></div></div>';
      }
    });
    var segRows = "";
    if (state.segmented) {
      r.segments.forEach(function (s, j) {
        segRows += '<div class="seg-row"><span>' + (j + 1) + ". " + s.highway + "</span>" +
          '<span style="color:' + scoreHex(s.score) + '">' + Math.round(s.score * 100) +
          "/100 · " + distStr(s.length_m) + "</span></div>";
      });
    }
    detailEl.innerHTML =
      (dims ? '<div class="dims">' + dims + "</div>" : "") +
      '<div class="detail-row"><span>Confidence in this scoring</span><b>' +
        Math.round(r.confidence * 100) + " / 100</b></div>" +
      '<div class="detail-row"><span>Weakest stretch — ' + distStr(r.worst_dist_m) +
        ' in</span><b style="color:' + scoreHex(r.worst_score) + '">' +
        Math.round(r.worst_score * 100) + " / 100</b></div>" +
      '<div class="detail-row"><span>Street crossings</span><b>' + r.crossings + "</b></div>" +
      '<button class="seg-btn" id="segBtn">' +
        (state.segmented ? "Hide block colours" : "Colour " + r.segments.length + " blocks by score") +
      "</button>" +
      (segRows ? '<div class="seg-list">' + segRows + "</div>" : "");
    detailEl.hidden = false;
    $("segBtn").addEventListener("click", function () {
      state.segmented = !state.segmented;
      renderDetail();
      positionFabs();
      redrawMap();
    });
  }

  // Swipe-snap: focused route follows the centred card.
  var scrollTimer = null;
  cardsEl.addEventListener("scroll", function () {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function () {
      var mid = cardsEl.scrollLeft + cardsEl.clientWidth / 2;
      var best = 0, bestDist = Infinity;
      Array.prototype.forEach.call(cardsEl.children, function (el, i) {
        var c = el.offsetLeft + el.offsetWidth / 2;
        var d = Math.abs(c - mid);
        if (d < bestDist) { bestDist = d; best = i; }
      });
      setFocus(best);
    }, 90);
  });

  // ---------------------------------------------------------------- search
  function apiError(resp, fallback) {
    return resp.json().then(function (j) {
      return (j && j.detail) || fallback;
    }).catch(function () { return fallback; });
  }

  function areaParam() {
    return state.config ? "&area=" + encodeURIComponent(state.config.id) : "";
  }

  function resolveEndpoint(input, cached) {
    var q = input.value.trim();
    if (cached && cached.device && q === MYLOC) return Promise.resolve(cached);
    if (!q) return Promise.reject("Enter both a start and a destination.");
    return fetch("/api/geocode?q=" + encodeURIComponent(q) + areaParam()).then(function (resp) {
      if (!resp.ok) return apiError(resp, "Couldn't find “" + q + "”.").then(Promise.reject.bind(Promise));
      return resp.json();
    });
  }

  function search() {
    goBtn.disabled = true;
    goBtn.textContent = "Reading the streets…";
    hint.textContent = "";
    Promise.all([
      resolveEndpoint(fromInput, state.origin),
      resolveEndpoint(toInput, state.dest),
    ]).then(function (ends) {
      state.origin = ends[0]; state.dest = ends[1];
      var alpha = (alphaSlider.value / 100) * (state.config ? state.config.alpha_max : 5);
      var qs = "olat=" + ends[0].lat + "&olon=" + ends[0].lon +
               "&dlat=" + ends[1].lat + "&dlon=" + ends[1].lon +
               "&alpha=" + alpha.toFixed(2) + areaParam();
      return fetch("/api/route?" + qs).then(function (resp) {
        if (!resp.ok) return apiError(resp, "Routing failed.").then(Promise.reject.bind(Promise));
        return resp.json();
      });
    }).then(function (data) {
      if (!data.routes.length) {
        toast("No walkable route found between those points.");
        return;
      }
      state.routes = data.routes;
      state.focus = 0;
      state.segmented = false;
      state.detailOpen = false;
      // Adopt the geocoder's formal names ("MIT maseeh" -> "Maseeh Hall") so
      // the pill and the reopened panel both show the resolved place.
      if (!state.origin.device) fromInput.value = formalName(state.origin, fromInput.value.trim());
      if (!state.dest.device) toInput.value = formalName(state.dest, toInput.value.trim());
      renderCards();
      cardsEl.scrollLeft = 0;
      redrawMap();
      collapseTopbar(true);
      // Let the sheet lay out first so the camera padding sees its real height.
      requestAnimationFrame(function () { fitToFocused(true); });
    }).catch(function (err) {
      toast(typeof err === "string" ? err
            : (err && err.message) || "Something went wrong. Please try again.");
    }).finally(function () {
      goBtn.disabled = false;
      goBtn.textContent = "Find routes";
    });
  }

  // The formal display name of a resolved endpoint — the geocoder's matched
  // place name (e.g. "MIT maseeh" -> "Maseeh Hall") — falling back to what the
  // user typed when there is no better name.
  function formalName(ep, typed) {
    if (ep && ep.device) return MYLOC;
    if (ep && ep.name) return ep.name;
    if (ep && ep.label) return ep.label.split(" · ")[0].split(",")[0].trim() || typed;
    return typed;
  }

  function collapseTopbar(collapsed) {
    document.body.classList.toggle("collapsed", collapsed);
    tripPill.hidden = !collapsed;
    if (collapsed) {
      pillFrom.textContent = formalName(state.origin, fromInput.value.trim());
      pillTo.textContent = formalName(state.dest, toInput.value.trim());
    }
  }

  // Keep the floating buttons riding just above the sheet (whose height changes
  // with cards / an open detail drawer). The Fit chip's visibility is driven by
  // the camera (moveend handler) — here we only make sure it's gone when
  // there's nothing to fit.
  function positionFabs() {
    var base = sheet.hidden ? 20 : sheet.offsetHeight + 12;
    fabs.style.bottom = "calc(var(--sab) + " + base + "px)";
    if (!state.routes.length) fitChip.hidden = true;
  }
  fitChip.addEventListener("click", function () { fitToFocused(true); });
  window.addEventListener("resize", positionFabs);

  tripPill.addEventListener("click", function () { collapseTopbar(false); });
  goBtn.addEventListener("click", search);
  [fromInput, toInput].forEach(function (el) {
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { el.blur(); search(); }
    });
  });
  fromInput.addEventListener("input", function () {
    fromInput.classList.remove("device-loc");
    if (state.origin && state.origin.device) state.origin = null;
  });
  toInput.addEventListener("input", function () { state.dest = null; });

  // Sample walk: fill the area's default trip and run it — a one-tap demo for
  // a first-time viewer staring at two empty fields.
  sampleBtn.addEventListener("click", function () {
    if (!state.config) return;
    state.origin = null;
    state.dest = null;
    fromInput.classList.remove("device-loc");
    toInput.classList.remove("device-loc");
    fromInput.value = state.config.default_from;
    toInput.value = state.config.default_to;
    search();
  });

  swapBtn.addEventListener("click", function () {
    var f = fromInput.value; fromInput.value = toInput.value; toInput.value = f;
    var so = state.origin; state.origin = state.dest; state.dest = so;
    var wasDevice = fromInput.classList.contains("device-loc");
    fromInput.classList.toggle("device-loc", toInput.classList.contains("device-loc"));
    toInput.classList.toggle("device-loc", wasDevice);
  });

  alphaSlider.addEventListener("input", function () {
    alphaWord.textContent = alphaWordFor(+alphaSlider.value);
  });

  // ------------------------------------------------------------ geolocation
  // Shared by the From-field button and the floating map button. The field
  // button only recenters when no routes are shown; the map button always
  // flies to the fix (that's what a locate control on the map means).
  function requestLocation(btn, alwaysFly) {
    if (!navigator.geolocation) { toast("Location isn't available in this browser."); return; }
    btn.classList.add("busy");
    navigator.geolocation.getCurrentPosition(function (pos) {
      btn.classList.remove("busy");
      var lat = pos.coords.latitude, lon = pos.coords.longitude;
      var bbox = state.config && state.config.bbox;
      if (bbox && !(bbox[0] <= lon && lon <= bbox[2] && bbox[1] <= lat && lat <= bbox[3])) {
        toast("It looks like you're outside the covered area. Humanpath currently covers " +
              (state.config.covered || "metro Boston") + ".");
        return;
      }
      state.origin = { lat: lat, lon: lon, label: MYLOC, device: true };
      state.youLoc = [lon, lat];
      fromInput.value = MYLOC;
      fromInput.classList.add("device-loc");
      redrawMap();
      if ((alwaysFly || !state.routes.length) && map) {
        map.flyTo({ center: [lon, lat], zoom: 15, duration: 900 });
      }
      fetch("/api/reverse?lat=" + lat + "&lon=" + lon)
        .then(function (r) { return r.json(); })
        .then(function (j) { if (j.label) hint.textContent = "→ near " + j.label; })
        .catch(function () {});
    }, function (err) {
      btn.classList.remove("busy");
      toast(err.code === 1
        ? "Location access is off for this site. Type an address instead, or allow location in your browser settings."
        : "Couldn't get your location. Try again in a moment.");
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 });
  }
  locBtn.addEventListener("click", function () { requestLocation(locBtn, false); });
  $("fabLocate").addEventListener("click", function () { requestLocation($("fabLocate"), true); });

  // ------------------------------------------------------------- navigation
  // Follow-me walking mode geometry. A route's polyline is projected once into
  // a local equirectangular meters frame anchored at the route start, giving
  // per-vertex coords, cumulative distances, and per-segment bearings. Fixes
  // snap to the polyline with a windowed nearest-segment search so out-and-back
  // corridors can't grab the wrong leg.
  var M_PER_DEG_LAT = 110540, M_PER_DEG_LON = 111320;
  var SNAP_WINDOW_SEGS = 30;   // segments searched around the last match
  var SNAP_RESCAN_M = 50;      // windowed residual beyond this → full rescan
  var BEARING_SMOOTH = 0.35;   // per-fix blend toward the segment bearing

  function navGeom(route) {
    if (route._nav) return route._nav;
    var pts = fullCoords(route);
    var lon0 = pts[0][0], lat0 = pts[0][1];
    var kx = M_PER_DEG_LON * Math.cos(lat0 * Math.PI / 180), ky = M_PER_DEG_LAT;
    var xy = new Array(pts.length), cum = new Array(pts.length);
    var brg = new Array(Math.max(0, pts.length - 1));
    var d = 0;
    for (var i = 0; i < pts.length; i++) {
      xy[i] = [(pts[i][0] - lon0) * kx, (pts[i][1] - lat0) * ky];
      if (i > 0) {
        var dx = xy[i][0] - xy[i - 1][0], dy = xy[i][1] - xy[i - 1][1];
        d += Math.sqrt(dx * dx + dy * dy);
        brg[i - 1] = (Math.atan2(dx, dy) * 180 / Math.PI + 360) % 360;
      }
      cum[i] = d;
    }
    route._nav = { pts: pts, xy: xy, cum: cum, brg: brg, total: d,
                   lon0: lon0, lat0: lat0, kx: kx, ky: ky };
    return route._nav;
  }

  function snapToRoute(geom, lat, lon, lastSegIdx) {
    var px = (lon - geom.lon0) * geom.kx, py = (lat - geom.lat0) * geom.ky;
    function scan(from, to) {
      var best = null;
      for (var i = from; i < to; i++) {
        var a = geom.xy[i], b = geom.xy[i + 1];
        var vx = b[0] - a[0], vy = b[1] - a[1];
        var len2 = vx * vx + vy * vy;
        var t = len2 ? ((px - a[0]) * vx + (py - a[1]) * vy) / len2 : 0;
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        var cx = a[0] + t * vx, cy = a[1] + t * vy;
        var dx = px - cx, dy = py - cy;
        var d2 = dx * dx + dy * dy;
        if (!best || d2 < best.d2) best = { d2: d2, segIdx: i, t: t, cx: cx, cy: cy };
      }
      return best;
    }
    var n = geom.xy.length - 1;
    var best = null;
    if (lastSegIdx != null) {
      best = scan(Math.max(0, lastSegIdx - SNAP_WINDOW_SEGS),
                  Math.min(n, lastSegIdx + SNAP_WINDOW_SEGS + 1));
      if (best && Math.sqrt(best.d2) > SNAP_RESCAN_M) best = null;
    }
    if (!best) best = scan(0, n);
    var segLen = geom.cum[best.segIdx + 1] - geom.cum[best.segIdx];
    return {
      segIdx: best.segIdx, t: best.t,
      snapped: [geom.lon0 + best.cx / geom.kx, geom.lat0 + best.cy / geom.ky],
      progressM: geom.cum[best.segIdx] + best.t * segLen,
      residualM: Math.sqrt(best.d2),
    };
  }

  // The one pluggable heading source (a device-compass experiment would only
  // replace this): the current segment's bearing, blended from the previous
  // heading along the shortest angular path so the camera doesn't snap at
  // polyline vertices.
  function navBearing(geom, segIdx, prevHeading) {
    var target = geom.brg[segIdx] != null ? geom.brg[segIdx] : (prevHeading || 0);
    if (prevHeading == null) return target;
    var delta = ((target - prevHeading + 540) % 360) - 180;
    return (prevHeading + delta * BEARING_SMOOTH + 360) % 360;
  }

  // --- nav lifecycle ------------------------------------------------------
  var NAV_CAM_MS = 800;        // min gap between chase-camera moves
  var NAV_ZOOM = 17.5, NAV_PITCH = 48;
  var OFFROUTE_M = 40;         // residual beyond this counts as off-route
  var OFFROUTE_FIXES = 3;      // consecutive off-route fixes before rerouting
  var ARRIVE_M = 20, ARRIVE_RESIDUAL_M = 30;

  var navHud = $("navHud"), navRemain = $("navRemain"), navEta = $("navEta"),
      navStatus = $("navStatus"), navExit = $("navExit"), navRecenter = $("navRecenter");
  var SIM_ON = /[?&]sim=1/.test(location.search);

  function navPuckImage() {
    var c = document.createElement("canvas");
    c.width = c.height = 64;
    var g = c.getContext("2d");
    g.translate(32, 32);
    g.beginPath();
    g.moveTo(0, -20); g.lineTo(15, 14); g.lineTo(0, 6); g.lineTo(-15, 14);
    g.closePath();
    g.fillStyle = YOU; g.strokeStyle = "#fff"; g.lineWidth = 5;
    g.lineJoin = "round"; g.stroke(); g.fill();
    return g.getImageData(0, 0, 64, 64);
  }

  function setNavHud(remainM) {
    navRemain.textContent = distStr(Math.max(0, remainM));
    navEta.textContent = timeStr(Math.max(0, remainM)) + " left";
  }

  function navStatusMsg(msg) {
    navStatus.hidden = !msg;
    if (msg) navStatus.textContent = msg;
  }

  function startNav() {
    if (state.nav || !state.routes.length) return;
    var geom = navGeom(state.routes[state.focus]);
    state.nav = { watchId: null, simTimer: null, sim: null, wakeLock: null,
                  geom: geom, snapped: null, heading: null,
                  progressM: 0, residualM: 0, lastSegIdx: null,
                  offCount: 0, rerouting: false, arrived: false,
                  follow: true, lastCamAt: 0 };
    document.body.classList.add("nav");
    navHud.hidden = false; navExit.hidden = false; navRecenter.hidden = true;
    navStatusMsg(null);
    fitChip.hidden = true;
    setNavHud(geom.total);
    acquireWakeLock();
    if (SIM_ON) { simStart(); return; }
    if (!navigator.geolocation) {
      toast("Location isn't available in this browser.");
      exitNav();
      return;
    }
    state.nav.watchId = navigator.geolocation.watchPosition(function (pos) {
      onNavFix(pos.coords.latitude, pos.coords.longitude);
    }, function (err) {
      if (err.code === 1) {
        toast("Location access is off for this site. Allow location in your browser settings to be guided.");
        exitNav();
      }
      // transient errors (timeout / unavailable): keep the last fix and wait
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 2000 });
  }

  function exitNav() {
    var nav = state.nav;
    if (!nav) return;
    state.nav = null;
    if (nav.watchId != null && navigator.geolocation) navigator.geolocation.clearWatch(nav.watchId);
    if (nav.simTimer) clearInterval(nav.simTimer);
    if (nav.wakeLock) { try { nav.wakeLock.release(); } catch (e) {} }
    document.body.classList.remove("nav");
    navHud.hidden = true; navExit.hidden = true; navRecenter.hidden = true;
    redrawMap();
    positionFabs();
    fitToFocused(true);   // fitBounds resets bearing/pitch to 0
  }

  function onNavFix(lat, lon) {
    var nav = state.nav;
    if (!nav || nav.arrived) return;
    var bbox = state.config && state.config.bbox;
    if (bbox && !(bbox[0] <= lon && lon <= bbox[2] && bbox[1] <= lat && lat <= bbox[3])) {
      navStatusMsg("You've left the covered area.");
      return;
    }
    var s = snapToRoute(nav.geom, lat, lon, nav.lastSegIdx);
    nav.snapped = s.snapped;
    nav.lastSegIdx = s.segIdx;
    nav.progressM = s.progressM;
    nav.residualM = s.residualM;
    nav.heading = navBearing(nav.geom, s.segIdx, nav.heading);
    setNavHud(nav.geom.total - s.progressM);
    handleOffRoute(lat, lon, s);
    handleArrival(s);
    navCamera(false);
    redrawMap();
  }

  function navCamera(force) {
    var nav = state.nav;
    if (!nav || !nav.follow || !nav.snapped || !map) return;
    var now = Date.now();
    if (!force && now - nav.lastCamAt < NAV_CAM_MS) return;
    nav.lastCamAt = now;
    map.easeTo({
      center: nav.snapped, bearing: nav.heading || 0,
      zoom: NAV_ZOOM, pitch: NAV_PITCH, duration: 700,
      // puck rides the lower third so the road ahead fills the screen
      offset: [0, Math.round(window.innerHeight * 0.18)],
    });
  }

  // Off-route / arrival: indicator only for now (auto-reroute lands next).
  function handleOffRoute(lat, lon, s) {
    var nav = state.nav;
    if (s.residualM > OFFROUTE_M) {
      nav.offCount++;
      navStatusMsg("Off route");
    } else {
      nav.offCount = 0;
      if (!nav.rerouting) navStatusMsg(null);
    }
  }

  function handleArrival(s) {}

  function acquireWakeLock() {
    if (!("wakeLock" in navigator)) return;
    navigator.wakeLock.request("screen").then(function (wl) {
      if (state.nav) state.nav.wakeLock = wl;
      else { try { wl.release(); } catch (e) {} }
    }).catch(function () {});
  }
  document.addEventListener("visibilitychange", function () {
    if (state.nav && document.visibilityState === "visible") acquireWakeLock();
  });

  navExit.addEventListener("click", function () { exitNav(); });
  navRecenter.addEventListener("click", function () {
    if (!state.nav) return;
    state.nav.follow = true;
    navRecenter.hidden = true;
    navCamera(true);
  });

  // --- sim harness (?sim=1) -----------------------------------------------
  // Deterministic fake fixes along the focused route at walking speed with
  // mild GPS noise; drivable from the console / Playwright via __hpSim.
  // Inert without the flag — real navigation uses watchPosition.
  function simStart() {
    var nav = state.nav;
    var sim = nav.sim = { t: 0, speed: 1.4, drift: false, paused: false, seed: 42 };
    function rand() {   // LCG → [-1, 1)
      sim.seed = (sim.seed * 1664525 + 1013904223) >>> 0;
      return sim.seed / 2147483648 - 1;
    }
    nav.simTimer = setInterval(function () {
      if (!state.nav || state.nav.sim !== sim || sim.paused) return;
      sim.t = Math.min(sim.t + sim.speed, state.nav.geom.total);
      var p = simPointAt(state.nav.geom, sim.t, sim.drift ? 60 : 0, rand);
      onNavFix(p[1], p[0]);
    }, 1000);
  }

  function simPointAt(geom, distM, driftM, rand) {
    var i = 1;
    while (i < geom.cum.length - 1 && geom.cum[i] < distM) i++;
    var a = geom.xy[i - 1], b = geom.xy[i];
    var seg = geom.cum[i] - geom.cum[i - 1] || 1;
    var t = (distM - geom.cum[i - 1]) / seg;
    var vx = b[0] - a[0], vy = b[1] - a[1];
    var L = Math.sqrt(vx * vx + vy * vy) || 1;
    var x = a[0] + t * vx + (-vy / L) * driftM + rand() * 4;
    var y = a[1] + t * vy + (vx / L) * driftM + rand() * 4;
    return [geom.lon0 + x / geom.kx, geom.lat0 + y / geom.ky];
  }

  window.__hpSim = {
    speed: function (mps) { if (state.nav && state.nav.sim) state.nav.sim.speed = mps; },
    drift: function (on) { if (state.nav && state.nav.sim) state.nav.sim.drift = !!on; },
    jumpTo: function (frac) {
      if (state.nav && state.nav.sim) state.nav.sim.t = state.nav.geom.total * frac;
    },
    pause: function (p) { if (state.nav && state.nav.sim) state.nav.sim.paused = p !== false; },
  };

  window.__hpNav = {
    geom: navGeom, snap: snapToRoute, bearing: navBearing,
    start: startNav, exit: exitNav,
    state: function () {
      var nav = state.nav;
      return nav && { progressM: nav.progressM, residualM: nav.residualM,
                      heading: nav.heading, snapped: nav.snapped,
                      follow: nav.follow, arrived: nav.arrived,
                      rerouting: nav.rerouting, offCount: nav.offCount };
    },
  };

  // Test/dev hook: inject a routes payload as if a search had resolved — the
  // checkpoint scripts (and offline dev) can't reach the geocoders, so they
  // fetch /api/route themselves and hand the result over here.
  window.__hpTest = {
    loadRoutes: function (data, origin, dest) {
      state.origin = origin || null;
      state.dest = dest || null;
      state.routes = data.routes;
      state.focus = 0;
      state.segmented = false;
      state.detailOpen = false;
      renderCards();
      cardsEl.scrollLeft = 0;
      redrawMap();
      collapseTopbar(true);
      requestAnimationFrame(function () { fitToFocused(false); });
    },
  };

  // ------------------------------------------------------------------ boot
  // The active city comes from ?area= (a picker switch), else the last choice
  // (localStorage — survives standalone PWA launches, whose start_url has no
  // query), else the server default. A stale saved id falls back to default.
  var AREA_KEY = "hp-area";

  function loadConfig(areaId) {
    var url = "/api/config" + (areaId ? "?area=" + encodeURIComponent(areaId) : "");
    return fetch(url).then(function (r) {
      if (!r.ok) {
        if (areaId) return loadConfig("");
        throw new Error("config " + r.status);
      }
      return r.json();
    });
  }

  var _initialArea = null;
  try {
    _initialArea = new URLSearchParams(location.search).get("area") ||
                   localStorage.getItem(AREA_KEY);
  } catch (e) {}

  loadConfig(_initialArea).then(function (cfg) {
    state.config = cfg;
    try { localStorage.setItem(AREA_KEY, cfg.id); } catch (e) {}
    var sel = $("areaSelect");
    sel.innerHTML = "";
    (cfg.areas || [{ id: cfg.id, label: cfg.label }]).forEach(function (a) {
      var o = document.createElement("option");
      o.value = a.id;
      o.textContent = a.label;
      sel.appendChild(o);
    });
    sel.value = cfg.id;
    sel.addEventListener("change", function () {
      try { localStorage.setItem(AREA_KEY, sel.value); } catch (e) {}
      location.search = "?area=" + encodeURIComponent(sel.value);  // clean reboot into the city
    });
    fromInput.placeholder = "From — e.g. " + cfg.default_from;
    toInput.placeholder = "To — e.g. " + cfg.default_to;
    initMap(cfg);
  }).catch(function () {
    toast("Can't reach the Humanpath server. Check your connection and try again.");
  });

  // PWA: service worker + update flow. When a NEW worker finishes installing
  // while an old one controls the page, a fresh version of the app shell is
  // ready — offer it as a one-tap refresh instead of waiting for the next
  // launch to pick it up.
  function offerUpdate() {
    toast("A new version is ready.", {
      kind: "info",
      action: { label: "Refresh", fn: function () { location.reload(); } },
    });
  }
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("./sw.js").then(function (reg) {
        function track(sw) {
          if (!sw) return;
          sw.addEventListener("statechange", function () {
            if (sw.state === "installed" && navigator.serviceWorker.controller) offerUpdate();
          });
        }
        if (reg.waiting && navigator.serviceWorker.controller) offerUpdate();
        track(reg.installing);
        reg.addEventListener("updatefound", function () { track(reg.installing); });
      }).catch(function () {});
    });
  }
  var deferredPrompt = null;
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferredPrompt = e;
    installBtn.hidden = false;
  });
  installBtn.addEventListener("click", function () {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    deferredPrompt.userChoice.finally(function () {
      deferredPrompt = null;
      installBtn.hidden = true;
    });
  });
  window.addEventListener("appinstalled", function () { installBtn.hidden = true; });

  // iOS has no install prompt event — nudge Safari users toward Add to Home
  // Screen once (skipped when already installed/standalone).
  var isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
  var standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone;
  if (isIOS && !standalone && !localStorage.getItem("hp-ios-hint")) {
    localStorage.setItem("hp-ios-hint", "1");
    setTimeout(function () {
      toast("Install this app: tap Share, then “Add to Home Screen”.",
            { kind: "info", ms: 7000 });
    }, 2500);
  }
})();
