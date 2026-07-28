// RotaPulse service worker — static shell only, same reasoning as the
// sibling apps: this app writes live state (shift edits, clock-in/out
// timestamps, leave requests) that must always hit the real server. A
// stale cached rota or attendance page would be actively misleading, not
// just unhelpful. Only tenant-agnostic static assets (CSS, JS, icons) get
// cached, purely so the app installs and launches like a native app.
//
// Bump CACHE_VERSION any time a precached file's contents change.
const CACHE_VERSION = "rotapulse-static-v3";

const PRECACHE_URLS = [
  "/static/style.css",
  "/static/manifest.json",
  "/static/logo.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-192.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png",
  "/static/icons/favicon.ico",
  "/static/offline.html",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      Promise.all(PRECACHE_URLS.map((url) => cache.add(url).catch(() => {})))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((n) => n !== CACHE_VERSION).map((n) => caches.delete(n)))
      )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/static/offline.html")));
    return;
  }

  const url = new URL(req.url);
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(req).then((cached) => cached || fetch(req)));
  }
});
