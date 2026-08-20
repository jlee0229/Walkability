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

  var toastTimer = null;
  function toast(msg, ms) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, ms || 4200);
  }

  // ------------------------------------------------------------------- map
  var map = null, styleLoaded = false, pendingFC = null;
  var fitPending = false, fitCam = null;  // camera state for the "Fit route" chip

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
    if (typeof maplibregl === "undefined") { toast("Map failed to load."); return; }
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
    // view (any route on screen), disappears on refit. The moveend right after
    // a fit records the fitted camera as the reference.
    map.on("moveend", function () {
      if (fitPending) {
        fitPending = false;
        fitCam = { c: map.getCenter(), z: map.getZoom() };
        return;
      }
      if (!state.routes.length || !fitCam) return;
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
    map.on("load", function () {
      styleLoaded = true;
      addRouteLayers();
      if (pendingFC) { setMapData(pendingFC[0], pendingFC[1]); pendingFC = null; }
    });
    window.__hpMap = map;  // debug handle
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
    if (state.youLoc) {
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
    fitPending = true;
    fitChip.hidden = true;
    map.fitBounds([[minLon, minLat], [maxLon, maxLat]], {
      padding: { top: padTop, bottom: padBottom, left: 30, right: 30 },
      duration: animate ? 800 : 0, maxZoom: 17,
    });
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
        '<div class="card-via">' + (r.via ? "via " + r.via : r.segments.length + " blocks") + "</div>";
      card.addEventListener("click", function (e) {
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

  function resolveEndpoint(input, cached) {
    var q = input.value.trim();
    if (cached && cached.device && q === MYLOC) return Promise.resolve(cached);
    if (!q) return Promise.reject("Enter both a start and a destination.");
    return fetch("/api/geocode?q=" + encodeURIComponent(q)).then(function (resp) {
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
               "&alpha=" + alpha.toFixed(2);
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
      toast(typeof err === "string" ? err : (err && err.message) || "Something went wrong.");
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
        toast("You look outside the covered area — Humanpath currently covers " +
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
      toast(err.code === 1 ? "Location permission was denied — you can type an address instead."
                           : "Couldn't get your location.");
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 });
  }
  locBtn.addEventListener("click", function () { requestLocation(locBtn, false); });
  $("fabLocate").addEventListener("click", function () { requestLocation($("fabLocate"), true); });

  // ------------------------------------------------------------------ boot
  fetch("/api/config").then(function (r) { return r.json(); }).then(function (cfg) {
    state.config = cfg;
    $("areaLabel").textContent = cfg.label;
    fromInput.placeholder = "From — e.g. " + cfg.default_from;
    toInput.placeholder = "To — e.g. " + cfg.default_to;
    initMap(cfg);
  }).catch(function () {
    toast("Couldn't reach the Humanpath server.");
  });

  // PWA: service worker + install prompt.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("./sw.js").catch(function () {});
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
      toast("Tip: install Humanpath — tap Share, then “Add to Home Screen”.", 7000);
    }, 2500);
  }
})();
