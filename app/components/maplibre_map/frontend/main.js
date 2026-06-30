/* Humanpath MapLibre map — build-less Streamlit component.
 *
 * No bundler: this file talks to Streamlit directly over postMessage (the same
 * protocol streamlit-component-lib implements). It receives args from Python on
 * each rerun WITHOUT the iframe remounting (the map instance persists), so route
 * updates and camera moves are dynamic and smooth.
 *
 * Python args (event.data.args):
 *   geojson : FeatureCollection of route LineStrings ({color, role} props)
 *   camera  : { bounds:[[w,s],[e,n]]|null, token:str, animate:bool }
 *   style   : MapLibre style URL or object (basemap)
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

  var errEl = document.getElementById("err");
  function showErr(msg) { errEl.style.display = "flex"; errEl.textContent = msg; }

  var map = null;
  var styleLoaded = false;
  var lastToken = null;
  var lastHeight = null;
  var pendingGeojson = null;

  function ensureMap(style) {
    if (map) return;
    if (typeof maplibregl === "undefined") { showErr("maplibregl failed to load"); return; }
    if (maplibregl.supported && !maplibregl.supported()) { showErr("WebGL not supported"); return; }
    // PMTiles protocol (B2.1b) — registered only if the pmtiles lib is present.
    if (typeof pmtiles !== "undefined" && maplibregl.addProtocol) {
      try { maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile); } catch (e) {}
    }
    try {
      map = new maplibregl.Map({
        container: "map", style: style,
        center: [-71.06, 42.35], zoom: 12, attributionControl: true,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      map.on("error", function (e) { console.error("[maplibre]", e && e.error); });
      map.on("load", function () {
        styleLoaded = true;
        addRouteLayers();
        if (pendingGeojson) { updateRoute(pendingGeojson); pendingGeojson = null; }
        map.resize();
      });
    } catch (e) { showErr("MapLibre init failed: " + e); console.error(e); }
  }

  function addRouteLayers() {
    if (map.getSource("routes")) return;
    map.addSource("routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    // White casing (halo) under the coloured line; focused route thicker + opaque.
    map.addLayer({
      id: "casing", type: "line", source: "routes",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#faf8f2",
        "line-width": ["case", ["==", ["get", "role"], "focused"], 10, 7],
        "line-opacity": ["case", ["==", ["get", "role"], "focused"], 1, 0.5],
      },
    });
    map.addLayer({
      id: "line", type: "line", source: "routes",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["case", ["==", ["get", "role"], "focused"], 6, 4],
        "line-opacity": ["case", ["==", ["get", "role"], "focused"], 1, 0.5],
      },
    });
  }

  function updateRoute(geojson) {
    if (!map) return;
    if (!styleLoaded) { pendingGeojson = geojson; return; }
    var src = map.getSource("routes");
    if (src) src.setData(geojson);
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
    ensureMap(args.style);
    updateRoute(args.geojson || { type: "FeatureCollection", features: [] });
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
