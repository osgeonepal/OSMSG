// Service worker powered by Workbox (Google). https://developer.chrome.com/docs/workbox/
importScripts("https://storage.googleapis.com/workbox-cdn/releases/7.1.0/workbox-sw.js");
const { registerRoute } = workbox.routing;
const { CacheFirst, NetworkFirst } = workbox.strategies;
const { ExpirationPlugin } = workbox.expiration;
const { CacheableResponsePlugin } = workbox.cacheableResponse;

workbox.core.skipWaiting();
workbox.core.clientsClaim();

// App shell (HTML + JS): always take the current version when online, fall back to cache offline.
registerRoute(
    ({ request }) => request.mode === "navigate" || ["script", "style", "worker"].includes(request.destination),
    new NetworkFirst({ cacheName: "osmsg-shell-v5", networkTimeoutSeconds: 5 })
);

// OSMSG API: network-first with no premature timeout, so a ~75s mega-hashtag query can finish; let the
// request finish; only fall back to cache when the network genuinely fails (offline).
registerRoute(
    ({ url }) => url.pathname.startsWith("/api/"),
    new NetworkFirst({
        cacheName: "osmsg-api-v2",
        plugins: [new CacheableResponsePlugin({ statuses: [0, 200] })],
    })
);

// CDNs (fonts, lucide, tailwind, avatars): long-lived cache.
registerRoute(
    ({ url }) => ["fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net", "cdn.tailwindcss.com",
        "storage.googleapis.com", "github.com", "avatars.githubusercontent.com"].includes(url.hostname),
    new CacheFirst({
        cacheName: "osmsg-cdn",
        plugins: [
            new CacheableResponsePlugin({ statuses: [0, 200] }),
            new ExpirationPlugin({ maxEntries: 60, maxAgeSeconds: 60 * 60 * 24 * 30 }),
        ],
    })
);

// Evict every cache except the current ones, so a stale app.js can never be served after a version bump.
const CURRENT_CACHES = new Set(["osmsg-shell-v5", "osmsg-api-v2", "osmsg-cdn"]);
self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((k) => !CURRENT_CACHES.has(k)).map((k) => caches.delete(k)))
        )
    );
});
