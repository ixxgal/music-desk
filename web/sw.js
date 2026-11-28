/* 音乐下载台 · 手机端 Service Worker
   只缓存静态外壳（html/css/js/图标），API 和媒体永远走网络 —— 曲库文件动辄几十兆，
   缓存它没意义，而且用户删了文件缓存里还留着会显示还能播。 */
const VERSION = 'md-1.2.9';
const SHELL = [
  '/m',
  '/static/m.css',
  '/static/m.js',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION).then((c) => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  // 接口、媒体、封面代理一律不碰缓存
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/') ||
      url.pathname.startsWith('/api/thumb')) return;

  // 外壳：先用缓存秒开，后台再更新
  e.respondWith(
    caches.match(e.request).then((hit) => {
      const net = fetch(e.request).then((res) => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(e.request, copy)).catch(() => {});
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});