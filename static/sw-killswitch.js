/* Kill-switch service worker — the back-out path for the offline briefing.
 *
 * Reverting the offline feature in git does NOT remove a service worker that a
 * device has already registered. Deleting the /sw.js route just makes it 404,
 * and "unregister on 404" was only ever *proposed* for the spec
 * (w3c/ServiceWorker#204) — it is debated behaviour, not something to rely on
 * across browsers. A worker left installed would keep answering from its own
 * caches, which on a briefing tool means potentially serving a stale flight.
 *
 * So instead of removing /sw.js on a revert, the route falls back to serving
 * THIS file (see service_worker() in app.py). The browser's routine update
 * check fetches it, sees different bytes, installs it, and it tears itself
 * down: every cache deleted, the registration removed, open tabs reloaded
 * straight from the network.
 *
 * Two properties make that reliable:
 *   - skipWaiting() in install, so it activates immediately rather than
 *     waiting for the old worker to release its clients. The real worker
 *     deliberately does NOT skip waiting (it prompts the crew instead); this
 *     one must, because it is a teardown, not a feature update.
 *   - NO fetch handler at all. From activation onward every request goes
 *     straight to the network, so even before the unregister completes
 *     nothing is being served from cache.
 *
 * It is safe to serve this at any time, including right now with no offline
 * feature deployed: a browser with no registration simply never fetches it.
 */

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    // Caches first: once these are gone nothing stale can be served even if
    // the unregister below fails for any reason.
    await Promise.all((await caches.keys()).map(name => caches.delete(name)));

    await self.registration.unregister();

    // Reload any open tab so it picks up server content rather than sitting on
    // whatever this worker was controlling. navigate() can reject once the
    // registration is gone — harmless, the next manual reload is clean anyway.
    for (const client of await self.clients.matchAll({ type: 'window' })) {
      client.navigate(client.url).catch(() => {});
    }
  })());
});
