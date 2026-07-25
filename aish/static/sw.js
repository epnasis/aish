/* aish service worker.
 *
 * Two jobs:
 *
 * 1. Notifications — installed PWAs (iOS home-screen apps especially) can only
 *    raise a notification through a registration, and tapping one must focus
 *    the app rather than open a second window.
 *
 * 2. The offline app shell (#165) — an installed app must OPEN with no network
 *    at all. Conversations themselves live in IndexedDB (see app.js); this
 *    worker only guarantees the code that renders them is always there.
 *
 * The caching strategy is picked per resource class, and the split matters:
 *
 *   navigation (index.html)  network-first, 3 s timeout, cache fallback
 *   ?v=<rev> assets          cache-first, forever (the URL names the revision)
 *   other static assets      stale-while-revalidate
 *   /file images             cache-first, LRU-capped (transcript screenshots)
 *   /ws /offline /upload …   never touched
 *
 * Why index.html is network-first and everything else is not: the page reads
 * its own `?v=<rev>` back out of the <script> tag and reloads when the server
 * reports a different rev (see PAGE_REV in app.js). Serving a STALE index.html
 * to an online client therefore means: load old rev → server says new rev →
 * reload → SW serves the same old index → forever. Network-first makes the
 * online path always authoritative, so that loop cannot start; app.js keeps an
 * independent reload throttle as the second line of defence, and asks us to
 * PURGE_SHELL before reloading so even a poisoned cache heals in one round.
 *
 * Rev'd assets are safe to cache immutably precisely BECAUSE the rev is in the
 * URL: app.js?v=abc and app.js?v=def are different cache entries, so a new
 * index.html can never be paired with stale JS.
 */

const SHELL_CACHE = "aish-shell-v1";
const IMG_CACHE = "aish-img-v1";
const CACHES = [SHELL_CACHE, IMG_CACHE];

// Cached transcripts reference screenshots and diagrams through /file; without
// them an offline transcript renders "🖼 (unavailable)" placeholders. Bounded
// so a session full of images can't grow the cache without limit.
const IMG_MAX_ENTRIES = 300;

const NAV_TIMEOUT_MS = 3000;

// Everything the app needs to boot with no network. The rev'd app.js/style.css
// aren't listed — their URLs aren't known until index.html is read, so install
// fetches the page and precaches whatever revision it names (cacheRevvedAssets).
const SHELL_ASSETS = [
  "./",
  "manifest.json",
  "icon.svg",
  "favicon-32.png",
  "icon-180.png",
  "icon-192.png",
  "vendor/xterm.css",
  "vendor/xterm.js",
  "vendor/xterm-addon-fit.js",
  "vendor/highlight.min.js",
];

// Live data. These must never be served from a cache: a stale session list or a
// replayed upload would be worse than an honest failure, and the app already
// handles their absence (that is what the offline mirror is for).
const NEVER_CACHE = ["/ws", "/offline/", "/upload", "/trigger", "/export/", "/dirs"];

// [SW-ROUTE-START]
// Which strategy a request gets. Kept as a pure function returning a string so
// the routing table can be unit-tested with no ServiceWorker environment.
function routeFor(request, url, scope) {
  if (request.method !== "GET") return "pass";
  if (url.origin !== scope.origin) return "pass";
  if (!url.pathname.startsWith(scope.pathname)) return "pass";
  // Re-root the path at the scope so a subpath-mounted deploy (/preview/…)
  // matches the same prefixes as one served at "/".
  const rest = url.pathname.slice(scope.pathname.length - 1);
  if (NEVER_CACHE.some((p) => rest === p || rest.startsWith(p))) return "pass";
  if (request.mode === "navigate") return "navigate";
  if (rest.startsWith("/file")) return "image";
  // The revision is in the query string, so the URL identifies exact bytes.
  if (url.searchParams.has("v")) return "immutable";
  return "revalidate";
}
// [SW-ROUTE-END]

async function cacheRevvedAssets(cache) {
  // index.html names the exact app.js/style.css revision it runs. Read them out
  // and precache those URLs, so the first offline launch after an install (or
  // after an update) has the matching code, not just the HTML that asks for it.
  try {
    const response = await cache.match("./");
    if (!response) return;
    const html = await response.clone().text();
    const refs = [...html.matchAll(/(?:src|href)="((?:app\.js|style\.css)\?v=[^"]+)"/g)];
    await Promise.all(
      refs.map(([, ref]) => cache.add(new Request(ref, { cache: "reload" })).catch(() => {}))
    );
  } catch { /* a shell without its JS still beats no shell */ }
}

async function precacheShell(cache) {
  // Best-effort per asset: one 404 (an icon renamed between releases) must not
  // fail the whole install and leave the app with no offline shell at all.
  await Promise.all(
    SHELL_ASSETS.map((asset) =>
      cache.add(new Request(asset, { cache: "reload" })).catch(() => {})
    )
  );
  await cacheRevvedAssets(cache);
}

async function trimImageCache() {
  const cache = await caches.open(IMG_CACHE);
  const keys = await cache.keys();
  // Cache Storage preserves insertion order, so the front of the list is the
  // least recently ADDED. Good enough here: images are written once, read many.
  const excess = keys.length - IMG_MAX_ENTRIES;
  for (let i = 0; i < excess; i += 1) await cache.delete(keys[i]);
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(precacheShell).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((n) => !CACHES.includes(n)).map((n) => caches.delete(n)))
      )
      .then(() => self.clients.claim())
  );
});

async function handleNavigate(request) {
  const cache = await caches.open(SHELL_CACHE);
  // Race the network against a timeout rather than awaiting it: a phone on a
  // dying connection can leave a fetch pending for 30 s, which would look like
  // the app failing to launch. The fetch keeps running past the timeout and
  // still refreshes the cache, so the NEXT launch is current.
  const network = fetch(request)
    .then(async (response) => {
      if (response && response.ok) {
        await cache.put("./", response.clone());
        cacheRevvedAssets(cache); // not awaited — don't delay the page on it
      }
      return response;
    })
    .catch(() => null);
  const timeout = new Promise((resolve) => setTimeout(() => resolve(null), NAV_TIMEOUT_MS));
  const fresh = await Promise.race([network, timeout]);
  if (fresh && fresh.ok) return fresh;
  const cached = await cache.match("./");
  if (cached) return cached;
  const slow = await network; // nothing cached: the network is the only hope
  if (slow) return slow;
  return new Response(
    "<!doctype html><meta charset=utf-8><title>aish</title>" +
      "<body style='font:16px -apple-system,sans-serif;padding:2rem;background:#000;color:#eee'>" +
      "<h1>aish</h1><p>Offline, and no cached copy of the app yet. " +
      "Open aish once while connected and it will work offline from then on.</p>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

async function handleImmutable(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok) await cache.put(request, response.clone());
  return response;
}

async function handleRevalidate(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then(async (response) => {
      if (response && response.ok) await cache.put(request, response.clone());
      return response;
    })
    .catch(() => null);
  if (cached) return cached; // the network result lands in the cache for next time
  const response = await network;
  return response || new Response("", { status: 504 });
}

async function handleImage(request) {
  const cache = await caches.open(IMG_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      await cache.put(request, response.clone());
      trimImageCache();
    }
    return response;
  } catch {
    // Offline with no cached copy: app.js's img.onerror renders the "image
    // unavailable" placeholder, which is the honest thing to show.
    return new Response("", { status: 504 });
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const scope = new URL(self.registration.scope);
  switch (routeFor(event.request, url, scope)) {
    case "navigate": event.respondWith(handleNavigate(event.request)); break;
    case "immutable": event.respondWith(handleImmutable(event.request)); break;
    case "revalidate": event.respondWith(handleRevalidate(event.request)); break;
    case "image": event.respondWith(handleImage(event.request)); break;
    default: break; // "pass" — untouched, straight to the network
  }
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "PURGE_SHELL") {
    // app.js is about to reload because the server reports a different rev.
    // Drop the shell so the reload is guaranteed to hit the network — this is
    // what stops a poisoned cache from turning an update into a reload loop.
    event.waitUntil(
      caches.delete(SHELL_CACHE).then(() => {
        if (event.source) event.source.postMessage({ type: "SHELL_PURGED" });
      })
    );
  } else if (data.type === "CLEAR_CACHES") {
    event.waitUntil(Promise.all(CACHES.map((name) => caches.delete(name))));
  } else if (data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        for (const client of clients) {
          if ("focus" in client) return client.focus();
        }
        return self.clients.openWindow("./");
      })
  );
});
