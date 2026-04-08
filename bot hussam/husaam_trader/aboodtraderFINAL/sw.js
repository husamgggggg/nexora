const CACHE = 'nexora-pwa-v10';
const ASSETS = ['/', '/manifest.json', '/pwa/icon-192.png', '/pwa/icon-512.png',
  '/pwa/screenshot-narrow.png', '/pwa/screenshot-wide.png'];

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
