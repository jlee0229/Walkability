/* Humanpath PWA service worker.
 *
 * Conservative by design: precache the app shell + vendored map libs, serve
 * same-origin static GETs stale-while-revalidate, and NEVER touch /api/* or
 * cross-origin requests (PMTiles range requests, fonts, glyphs go straight to
 * the network — intercepting range requests corrupts tile reads).
 * Bump VERSION on any shell change to invalidate old caches.
 */
var VERSION = "humanpath-pwa-v4";
var SHELL = [
  "./",
  "./index.html",
  "./style.css",
  "./app.js",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
  "./icons/apple-touch-icon.png",
  "/vendor/maplibre-gl.js",
  "/vendor/maplibre-gl.css",
  "/vendor/pmtiles.js",
  "/vendor/basemaps.js",
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(VERSION).then(function (c) { return c.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        if (k !== VERSION) return caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;          // fonts, glyphs, tiles
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz") return;
  if (e.request.headers.has("range")) return;

  // Stale-while-revalidate for the shell and vendor assets.
  e.respondWith(
    caches.match(e.request).then(function (cached) {
      var fresh = fetch(e.request).then(function (resp) {
        if (resp && resp.ok) {
          var copy = resp.clone();
          caches.open(VERSION).then(function (c) { c.put(e.request, copy); });
        }
        return resp;
      }).catch(function () { return cached; });
      return cached || fresh;
    })
  );
});
