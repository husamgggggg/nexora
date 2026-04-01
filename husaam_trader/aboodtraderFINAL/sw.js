const CACHE = 'nexora-pwa-v8';
const ASSETS = ['/', '/manifest.json?v=8', '/icon-192.png?v=8', '/icon-512.png?v=8',
  '/pwa-screenshot-narrow.png?v=8', '/pwa-screenshot-wide.png?v=8'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => 
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) return; // API لا يُكاش
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
