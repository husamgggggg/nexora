const CACHE = 'nexora-pwa-v12';
const ASSETS = ['/', '/manifest.json', '/pwa/icon-192.png', '/pwa/icon-512.png',
  '/pwa/screenshot-narrow.png', '/pwa/screenshot-wide.png'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(ASSETS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const u = String(req.url || '');

  // API: دائماً الشبكة فقط — respondWith يجب أن يمرّر Response صالحة
  if (u.includes('/api/')) {
    e.respondWith(fetch(req));
    return;
  }

  e.respondWith(
    fetch(req).catch(() =>
      caches.match(req).then((hit) => {
        if (hit) return hit;
        return new Response('', { status: 503, statusText: 'Offline' });
      })
    )
  );
});
