const releaseAssets = new Set(ASSETS);

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll([
    new Request('/', {cache:'reload', credentials:'omit'}), ...ASSETS,
  ])));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith('polka-shell-') && name !== SHELL_CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const {request} = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  const path = url.pathname;
  if (request.mode === 'navigate' && (path === '/' || /^\/(?:offline|read\/\d+)\/?$/.test(path))) {
    event.respondWith(fetch(request).catch(async () => (await (await caches.open(SHELL_CACHE)).match('/')) || Response.error()));
  } else if (releaseAssets.has(path) && !url.search && !request.headers.has('Range')) {
    event.respondWith((async () => (await (await caches.open(SHELL_CACHE)).match(path)) || fetch(request))());
  }
});
