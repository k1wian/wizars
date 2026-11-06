// простой service worker: сайт работает как приложение, данные всегда свежие из сети
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open("wz1").then(x => x.put(e.request, c)); return r; })
    .catch(() => caches.match(e.request)));
});
