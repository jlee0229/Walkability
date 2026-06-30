/* Humanpath MapLibre map — build-less Streamlit component.
 *
 * No bundler: this file talks to Streamlit directly over postMessage (the same
 * protocol streamlit-component-lib implements). It receives args from Python on
 * each rerun WITHOUT the iframe remounting (the map instance persists), so route
 * updates and camera moves are dynamic and smooth.
 *
 * Python args (event.data.args):
 *   geojson : FeatureCollection of route lines — role: alt | halo | focused |
 *             segment, plus {color, label} for paint/hover
 *   points  : FeatureCollection of O/D markers (role: origin | dest, {label})
 *   camera  : { bounds:[[w,s],[e,n]]|null, token:str, animate:bool }
 *   style   : MapLibre style URL/object, or a {_protomaps} marker (basemap)
 *   center, zoom : initial view (first creation only)
 *   height  : iframe height in px
 *
 * Camera moves only when `token` changes (search / focus switch / Fit route), so
 * a plain rerun (and any manual pan/zoom) leaves the view alone.
 */
(function () {
  // --- Streamlit handshake (bare-bones; mirrors streamlit-component-lib) ------
  function send(type, data) {
    window.parent.postMessage(
      Object.assign({ isStreamlitMessage: true, type: type }, data || {}), "*");
  }
  function setFrameHeight(h) { send("streamlit:setFrameHeight", { height: h }); }
  function setComponentReady() { send("streamlit:componentReady", { apiVersion: 1 }); }
  function setComponentValue(v) { send("streamlit:setComponentValue", { value: v, dataType: "json" }); }

  var errEl = document.getElementById("err");
  function showErr(msg) { errEl.style.display = "flex"; errEl.textContent = msg; }

  // Fatal failure (WebGL/lib/init): show the message AND report it to Python via
  // setComponentValue so streamlit_app can fall back to the st_folium map. Reported
  // once per page-load (the iframe persists across reruns) to avoid a rerun loop.
  var reported = false;
  function fail(reason) {
    showErr(reason || "Map failed to load");
    if (!reported) { reported = true; setComponentValue({ status: "error", reason: reason || "" }); }
  }

  // A `_protomaps` marker from Python -> a full MapLibre style built here from the
  // Protomaps basemap theme (build-less; `basemaps` is the @protomaps/basemaps UMD
  // global). Keeps the heavy ~100-layer theme out of the Python<->JS payload and
  // lets it version-track the CDN. Pass-through for plain style URLs/objects.
  // Humanpath brand palette — warm the neutral-grey "light" flavor toward the app's
  // parchment/terracotta editorial look. Tinting at the flavor (colour-token) level
  // propagates through all ~100 generated layers, far cleaner than recolouring each.
  var BRAND_FLAVOR = {
    background: "#f0ebe0", earth: "#f4efe4", water: "#c2d4d6",
    buildings: "#e7dcc6", pedestrian: "#efe9da",
    // Clearer greens so parks read against the pale parchment earth.
    park_a: "#cde0bd", wood_a: "#c6dcb2", scrub_a: "#d2e0cc",
  };

  function resolveStyle(style) {
    if (!style || !style._protomaps) return style;
    var pm = style._protomaps;
    if (typeof basemaps === "undefined") { showErr("@protomaps/basemaps failed to load"); return style; }
    var flavorName = pm.flavor || "light";
    var flavor = Object.assign({}, basemaps.namedFlavor(flavorName), BRAND_FLAVOR);
    return {
      version: 8,
      glyphs: "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf",
      sprite: "https://protomaps.github.io/basemaps-assets/sprites/v4/" + flavorName,
      sources: {
        protomaps: {
          type: "vector", url: pm.url,
          attribution: '<a href="https://protomaps.com">Protomaps</a> © <a href="https://openstreetmap.org">OpenStreetMap</a>',
        },
      },
      layers: basemaps.layers("protomaps", flavor, { lang: "en" }),
    };
  }

  var EMPTY_FC = { type: "FeatureCollection", features: [] };
  var ACCENT = "#b1592e", INK = "#211e18", HALO = "#faf8f2";

  var map = null;
  var styleLoaded = false;
  var lastToken = null;
  var lastHeight = null;
  var pendingRoutes = null;
  var pendingPoints = null;

  function ensureMap(style, center, zoom) {
    if (map) return;
    if (typeof maplibregl === "undefined") { fail("maplibregl failed to load"); return; }
    if (maplibregl.supported && !maplibregl.supported()) { fail("WebGL not supported"); return; }
    // PMTiles protocol (B2.1b) — registered only if the pmtiles lib is present.
    if (typeof pmtiles !== "undefined" && maplibregl.addProtocol) {
      try { maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile); } catch (e) {}
    }
    try {
      map = new maplibregl.Map({
        container: "map", style: resolveStyle(style),
        center: center || [-71.06, 42.35], zoom: zoom || 12, attributionControl: true,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      map.on("error", function (e) { console.error("[maplibre]", e && e.error); });
      // Some Protomaps theme icons (e.g. "townhall") aren't in the published v4
      // sprite; supply a transparent 1x1 placeholder so MapLibre doesn't warn and
      // the rest of the POI renders cleanly. Robust against any sprite/theme drift.
      map.on("styleimagemissing", function (e) {
        if (map.hasImage(e.id)) return;
        map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) });
      });
      map.on("load", function () {
        styleLoaded = true;
        addRouteLayers();
        if (pendingRoutes || pendingPoints) {
          updateData(pendingRoutes, pendingPoints);
          pendingRoutes = pendingPoints = null;
        }
        map.resize();
      });
    } catch (e) { fail("MapLibre init failed: " + e); console.error(e); }
  }

  // Route + marker layers, drawn above the basemap. Order (bottom->top): faint
  // alternatives, the white halo under the focused route, the focused route
  // (one line, or per-block `segment` features), then the O/D circle markers.
  // Mirrors the folium build_route_layer parity.
  function addRouteLayers() {
    if (map.getSource("routes")) return;
    map.addSource("routes", { type: "geojson", data: EMPTY_FC });
    map.addSource("points", { type: "geojson", data: EMPTY_FC });
    var round = { "line-cap": "round", "line-join": "round" };
    // Alternatives: dashed + faint, so the solid haloed focused route is unmistakable.
    map.addLayer({
      id: "r-alt", type: "line", source: "routes",
      filter: ["==", ["get", "role"], "alt"],
      layout: { "line-cap": "butt", "line-join": "round" },
      paint: {
        "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.55,
        "line-dasharray": [2, 2],
      },
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
    // Block-boundary dots (segmented mode): small white pips so adjacent blocks read
    // as distinct. Drawn under the larger O/D markers; no tooltip.
    map.addLayer({
      id: "r-joints", type: "circle", source: "points",
      filter: ["==", ["get", "role"], "joint"],
      paint: {
        "circle-radius": 3, "circle-color": HALO,
        "circle-stroke-width": 1.5, "circle-stroke-color": INK, "circle-stroke-opacity": 0.55,
      },
    });
    map.addLayer({
      id: "r-points", type: "circle", source: "points",
      filter: ["match", ["get", "role"], ["origin", "dest"], true, false],
      paint: {
        "circle-radius": 7,
        "circle-color": ["match", ["get", "role"], "origin", ACCENT, INK],
        "circle-stroke-width": 3, "circle-stroke-color": HALO,
      },
    });
    addTooltips();
  }

  // Hover tooltips (parity with folium PolyLine/CircleMarker tooltips): a single
  // reusable popup showing the feature's `label` (walk score / street, Start/Dest).
  function addTooltips() {
    var popup = new maplibregl.Popup({
      closeButton: false, closeOnClick: false, offset: 12, className: "hp-pop",
    });
    ["r-line", "r-alt", "r-points"].forEach(function (id) {
      map.on("mousemove", id, function (e) {
        var f = e.features && e.features[0];
        if (!f || !f.properties || !f.properties.label) return;
        map.getCanvas().style.cursor = "pointer";
        var at = (id === "r-points") ? f.geometry.coordinates : e.lngLat;
        popup.setLngLat(at).setText(f.properties.label).addTo(map);
      });
      map.on("mouseleave", id, function () {
        map.getCanvas().style.cursor = "";
        popup.remove();
      });
    });
  }

  function updateData(geojson, points) {
    if (!map) return;
    if (!styleLoaded) { pendingRoutes = geojson; pendingPoints = points; return; }
    var rs = map.getSource("routes"); if (rs) rs.setData(geojson || EMPTY_FC);
    var ps = map.getSource("points"); if (ps) ps.setData(points || EMPTY_FC);
  }

  function moveCamera(camera) {
    if (!map || !camera || !camera.bounds) return;
    if (camera.token === lastToken) return;  // only on intent (new trip / Fit route)
    lastToken = camera.token;
    var dur = camera.animate ? 800 : 0;
    var fit = function () {
      map.stop();  // cancel any in-flight animation to avoid stacking glitches
      map.fitBounds(camera.bounds, { padding: 48, duration: dur });
    };
    if (styleLoaded) fit(); else map.once("load", fit);
  }

  function onRender(args) {
    if (!args) return;
    var h = args.height || 660;
    if (h !== lastHeight) { lastHeight = h; setFrameHeight(h); }
    // Failure-injection hook to verify the Python-side folium fallback end-to-end
    // (HUMANPATH_MAP_FORCE_FAIL). Exercises the real fail()->setComponentValue path.
    if (args.forceFail && !reported) { fail("forced failure (HUMANPATH_MAP_FORCE_FAIL)"); return; }
    ensureMap(args.style, args.center, args.zoom);
    updateData(args.geojson || EMPTY_FC, args.points || EMPTY_FC);
    moveCamera(args.camera);
    if (map) map.resize();
  }

  var gotRender = false;
  window.addEventListener("message", function (e) {
    if (e.data && e.data.type === "streamlit:render") {
      gotRender = true;
      try { onRender(e.data.args); } catch (err) { console.error(err); }
    }
  });
  window.addEventListener("resize", function () { if (map) map.resize(); });

  setComponentReady();
  setFrameHeight(660);
  // Diagnostic: if Streamlit never sends a render, the handshake is wrong.
  setTimeout(function () {
    if (!gotRender) showErr("No render from Streamlit yet (handshake?). Check console.");
  }, 4000);
})();
