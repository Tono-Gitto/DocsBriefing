/* Offline briefing service worker — ADR 0003, slice 3.
 *
 * The requirement it exists for: a briefing opened before departure stays fully
 * usable, page images included, for at least 12 hours with no network, and
 * survives the tablet being backgrounded, evicted, or rebooted.
 *
 * Three things here are load-bearing and easy to break:
 *
 *  1. RULE ORDER in onfetch. hira.json lives under /data/<run>/<g>/ so it also
 *     matches the generic cache-first rule. It MUST be checked first, and its
 *     404s must never be cached — otherwise the client's load-time probe caches
 *     a miss and a brief generated ten minutes later never surfaces.
 *
 *  2. TWO BUCKETS. Briefing data is swapped wholesale per run; tiles are shared
 *     across flights and never swapped. Putting tiles in the run bucket would
 *     re-download the whole basemap on every swap and make the server-side
 *     sharing pointless.
 *
 *  3. SWAP ORDER. The old briefing bucket is deleted only after the new one is
 *     fully cached, so the device always holds at least one complete briefing.
 */

const SHELL_VERSION = 'shell-v1';
const TILE_CACHE    = 'tiles-shared';
const BRIEFING_NS   = 'briefing-';

const briefingCache = runId => BRIEFING_NS + runId;

// Everything needed to render the app with no network at all.
const SHELL_URLS = [
  '/map',
  '/static/leaflet.js',
  '/static/leaflet.css',
  '/static/app.webmanifest',
];

// ── Install / activate ───────────────────────────────────────────────────────

self.addEventListener('install', event => {
  // No skipWaiting: a new version waits until the crew taps the reload chip.
  // Auto-activating could swap the UI mid-brief, or mid-flight.
  event.waitUntil(
    caches.open(SHELL_VERSION).then(c => c.addAll(SHELL_URLS)).catch(() => {})
  );
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names
        .filter(n => n.startsWith('shell-') && n !== SHELL_VERSION)
        .map(n => caches.delete(n))
    );
    await self.clients.claim();
  })());
});

// ── Precache (owned here, not by the page) ───────────────────────────────────
//
// A 3-4 leg upload opens two map tabs. If each drove its own sweep they would
// fetch every tile twice and their readiness chips would disagree. Here, the
// first tab to ask starts it, later asks are no-ops, and progress is broadcast
// to every client — which also means a reload mid-precache picks up where it
// was instead of restarting.

let _sweep = { runId: null, state: 'idle', done: 0, total: 0, missing: 0, promise: null };

async function broadcast(msg) {
  const clients = await self.clients.matchAll({ includeUncontrolled: true });
  for (const c of clients) c.postMessage(msg);
}

function sweepStatus() {
  return {
    type:    'precache-status',
    runId:   _sweep.runId,
    state:   _sweep.state,
    done:    _sweep.done,
    total:   _sweep.total,
    missing: _sweep.missing || 0,
  };
}

function entryUrl(runId, entry) {
  // Tiles are shared and served off /tiles/; everything else is run-scoped.
  return entry.startsWith('tiles/') ? '/' + entry : `/data/${runId}/${entry}`;
}

async function runSweep(runId) {
  _sweep = { runId, state: 'running', done: 0, total: 0, missing: 0, promise: _sweep.promise };
  await broadcast(sweepStatus());

  // A worker is terminated whenever it goes idle, so a reload re-enters this
  // function from scratch. Offline that means the network fetch below throws —
  // and reporting "caching failed" for a briefing that is fully cached would
  // be exactly the lie this chip exists to prevent. Fall back to the cached
  // manifest and re-verify against Cache Storage instead.
  const manifestUrl = `/data/${runId}/manifest.json`;
  let manifest;
  try {
    const r = await fetch(manifestUrl, { cache: 'no-store' });
    if (!r.ok) throw new Error('manifest ' + r.status);
    manifest = await r.json();
    // Cache the manifest itself — the age warning reads generated_iso offline.
    (await caches.open(briefingCache(runId))).put(
      manifestUrl, new Response(JSON.stringify(manifest), {
        headers: { 'Content-Type': 'application/json' },
      })
    );
  } catch (e) {
    const hit = await caches.match(manifestUrl);
    if (!hit) {
      _sweep.state = 'error';
      await broadcast(sweepStatus());
      return;
    }
    manifest = await hit.json();
  }

  // Shell first: without it a cold start renders nothing, however complete the
  // briefing data is.
  try {
    await (await caches.open(SHELL_VERSION)).addAll(SHELL_URLS);
  } catch (e) { /* best effort — individual misses surface below */ }

  const entries = manifest.files || [];
  _sweep.total = entries.length;
  await broadcast(sweepStatus());

  const briefing = await caches.open(briefingCache(runId));
  const tiles    = await caches.open(TILE_CACHE);

  // Modest concurrency: fast on dispatch wifi, not a thundering herd.
  let missing = 0;
  const queue = entries.slice();
  const worker = async () => {
    while (queue.length) {
      const entry = queue.shift();
      const url   = entryUrl(runId, entry);
      const cache = entry.startsWith('tiles/') ? tiles : briefing;
      try {
        if (!(await cache.match(url))) {
          const res = await fetch(url, { cache: 'no-store' });
          if (res.ok) await cache.put(url, res.clone());
          else missing += 1;
          // A non-ok response is tolerated, not retried: the chip must not
          // stall forever on one file the pipeline listed but never wrote.
        }
      } catch (e) {
        missing += 1;   // offline mid-sweep, and not already cached
      }
      _sweep.done += 1;
      if (_sweep.done % 5 === 0 || _sweep.done === _sweep.total) {
        await broadcast(sweepStatus());
      }
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);

  _sweep.missing = missing;
  if (missing) {
    // Say so rather than showing green over an incomplete briefing, and keep
    // the previous run's cache: a complete old briefing beats a partial new one.
    _sweep.state = 'partial';
    await broadcast(sweepStatus());
    return;
  }

  // Swap only now: until this line the previous briefing is still intact, so
  // the device is never between two incomplete ones.
  const names = await caches.keys();
  await Promise.all(
    names
      .filter(n => n.startsWith(BRIEFING_NS) && n !== briefingCache(runId))
      .map(n => caches.delete(n))
  );

  _sweep.state = 'ready';
  await broadcast(sweepStatus());
}

self.addEventListener('message', event => {
  const data = event.data || {};

  if (data.type === 'precache' && data.runId) {
    if (_sweep.runId === data.runId && _sweep.state !== 'error') {
      event.waitUntil(broadcast(sweepStatus()));   // already running or done
      return;
    }
    _sweep.promise = runSweep(data.runId);
    event.waitUntil(_sweep.promise);
    return;
  }

  if (data.type === 'status') {
    event.waitUntil(broadcast(sweepStatus()));
    return;
  }

  if (data.type === 'skip-waiting') {
    self.skipWaiting();
  }
});

// ── Fetch ────────────────────────────────────────────────────────────────────

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit   = await cache.match(request);
  if (hit) return hit;
  try {
    const res = await fetch(request);
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch (e) {
    return new Response('Offline and not cached', { status: 504 });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(SHELL_VERSION);
  const hit   = await cache.match(request, { ignoreSearch: true });
  const net   = fetch(request)
    .then(res => { if (res.ok) cache.put(request, res.clone()); return res; })
    .catch(() => null);
  return hit || (await net) || new Response('Offline', { status: 504 });
}

async function hiraFetch(request) {
  // Network-first, and NEVER cache a negative. hira.json legitimately goes
  // 404 -> 200 when the crew generates a brief; a cached 404 would leave the
  // button dotless forever with a valid brief sitting on the server.
  const cache = await caches.open(briefingCache(_sweep.runId || ''));
  try {
    const res = await fetch(request);
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch (e) {
    return (await cache.match(request)) ||
           new Response(JSON.stringify({ error: 'offline' }),
                        { status: 503, headers: { 'Content-Type': 'application/json' } });
  }
}

async function navigationFetch(request) {
  try {
    return await fetch(request);
  } catch (e) {
    // Offline. Land on a cached briefing rather than the browser's error page:
    // the crew's home-screen icon points at '/', which normally redirects to
    // the upload form — useless without a connection.
    const url = new URL(request.url);
    if (url.pathname === '/map' && url.searchParams.get('r')) {
      const hit = await caches.match(request, { ignoreSearch: true });
      if (hit) return hit;
    }
    const names  = await caches.keys();
    const cached = names.find(n => n.startsWith(BRIEFING_NS));
    if (cached) {
      const runId = cached.slice(BRIEFING_NS.length);
      const shell = await caches.match('/map', { ignoreSearch: true });
      if (url.pathname === '/map') return shell || Response.redirect(`/map?r=${runId}&g=1`, 302);
      return Response.redirect(`/map?r=${runId}&g=1`, 302);
    }
    return new Response(
      '<h1>Offline</h1><p>No briefing has been cached on this device yet.</p>',
      { status: 503, headers: { 'Content-Type': 'text/html' } }
    );
  }
}

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;              // uploads, /api/hira: live only

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;   // never intercept third parties

  if (request.mode === 'navigate') {
    event.respondWith(navigationFetch(request));
    return;
  }

  // ORDER MATTERS — hira.json also matches the /data/ rule below.
  if (url.pathname.endsWith('/hira.json')) {
    event.respondWith(hiraFetch(request));
    return;
  }
  if (url.pathname.startsWith('/tiles/')) {
    event.respondWith(cacheFirst(request, TILE_CACHE));
    return;
  }
  if (url.pathname.startsWith('/data/')) {
    const runId = url.pathname.split('/')[2];
    event.respondWith(cacheFirst(request, briefingCache(runId)));
    return;
  }
  if (url.pathname === '/map' || url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
  // /upload, /api/status, everything else: network only.
});
