/* Abood Trader — PWA offline: precache HTML + static assets; stale-while-revalidate for JS/CSS; /api network-only */
const CACHE = 'abood-static-v5';
const PRECACHE = [
  '/',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  '/screenshot-wide.png',
  '/screenshot-narrow.png',
];

function staleWhileRevalidate(req) {
  return caches.match(req).then((hit) => {
    const network = fetch(req).then((res) => {
      if (res && res.ok)
        caches.open(CACHE).then((c) => c.put(req, res.clone())).catch(() => {});
      return res;
    });
    if (hit) {
      network.catch(() => {});
      return hit;
    }
    return network;
  });
}

function isRuntimeStaticAsset(pathname) {
  return /\.(?:js|css|woff2?)$/i.test(pathname);
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) {
    event.respondWith(fetch(req));
    return;
  }
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(req));
    return;
  }
  if (req.method !== 'GET') {
    event.respondWith(fetch(req));
    return;
  }
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  const path = url.pathname;
  if (PRECACHE.includes(path) || isRuntimeStaticAsset(path)) {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }

  event.respondWith(fetch(req));
});
