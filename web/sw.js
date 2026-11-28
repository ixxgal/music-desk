/* 音乐下载台 · 手机端 Service Worker
   只缓存静态外壳（css/js/图标），**绝不缓存 /m 这个 HTML 入口**，
   API 和媒体也永远走网络 —— 曲库文件动辄几十兆，缓存它没意义。
   1.2.9 之前这里把 '/m' 也塞进了 SHELL，结果一旦某次缓存到半截的 HTML
   （引擎刚起、或者并发解压写坏），之后每次打开都返回那份坏页面，
   连 body 都没有，用户看到的就是「界面没画出来」且怎么 reload 都没用。 */
const VERSION = 'md-1.3.1';
const SHELL = [
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
  // 页面本体一律走网络，不进缓存 —— 这是唯一能保证「壳永远不会被缓存卡死」的规则
  if (e.request.mode === 'navigate') return;
  if (url.pathname === '/m' || url.pathname === '/m/' || url.pathname.endsWith('.html')) return;
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