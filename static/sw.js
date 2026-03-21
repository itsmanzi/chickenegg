// chickenegg service worker — enables PWA install on Android
const CACHE = 'chickenegg-v1';

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(clients.claim());
});

// Network-first strategy — always get fresh results from server
self.addEventListener('fetch', e => {
  // Only cache GET requests for static assets
  if (e.request.method !== 'GET') return;
  if (e.request.url.includes('/analyze') || e.request.url.includes('/check-progress')) return;

  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
