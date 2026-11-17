/* Offline shell for the Honey Badger launcher (only active over HTTPS).
   Caches the launcher and the self-contained tools so they work out of
   range of the Badger. Library content is never cached here — it lives on the Badger. */
var SHELL = "badger-shell-v10";
var FILES = ["/", "/index.html", "/style.css", "/app.js", "/manifest.webmanifest", "/icon.svg", "/icon-192.png",
  "/tools/tools.css", "/tools/morse.html", "/tools/phrases.html", "/tools/water.html",
  "/tools/radio.html", "/tools/convert.html", "/tools/sun.html", "/tools/nutime.html", "/tools/nutime.js"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(SHELL).then(function (c) { return c.addAll(FILES); }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k !== SHELL; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.indexOf("/api/") === 0) {
    // Live data: network only, with a friendly offline reply.
    e.respondWith(fetch(e.request).catch(function () {
      return new Response(JSON.stringify({ error: "offline", offline: true }), { status: 503, headers: { "Content-Type": "application/json" } });
    }));
    return;
  }
  if (FILES.indexOf(url.pathname) >= 0) {
    // Shell: network first (picks up updates), cache when out of range.
    // Only a good answer replaces the cached copy: a 502 while the portal restarts
    // must not overwrite the working shell. An error answer falls back to the cache.
    e.respondWith(fetch(e.request).then(function (r) {
      if (r.ok && r.type === "basic") { var copy = r.clone(); caches.open(SHELL).then(function (c) { c.put(e.request, copy); }); return r; }
      return caches.match(e.request).then(function (hit) { return hit || r; });
    }).catch(function () { return caches.match(e.request); }));
  }
});
