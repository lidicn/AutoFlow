/*
 * AutoFlow PWA Service Worker
 * 策略：network-first + 离线回退缓存 shell。
 * - 绝不缓存 /api 与 /mcp（实时数据，必须走网络）。
 * - 在线时每次重新拉取，尊重服务端 no-store（开发期改了前端能立即生效）。
 * - 离线时回退到已缓存的 app shell，保证 PWA 可打开。
 */
const CACHE = 'autoflow-shell-v2';
const SHELL = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/brand/logo.svg',
  '/static/brand/logo-wordmark.svg',
  '/manifest.webmanifest'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.allSettled(SHELL.map((u) => cache.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // 实时接口不进缓存，直接放给网络
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/mcp')) return;

  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then((c) => c || caches.match('/')))
  );
});
