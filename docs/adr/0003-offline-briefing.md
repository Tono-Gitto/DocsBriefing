# ADR 0003 — Offline briefing: run-scoped URLs, service-worker precache, self-contained bundle

Date: 2026-08-17
Status: Accepted (design; not yet implemented)

## Context

The briefing is served from Railway and read in the cockpit, where there is no connectivity.
The requirement: a briefing opened before departure must remain fully usable — including the
Source Pane's original document images — for at least 12 hours with no network.

Four properties of the current system shape every decision:

- **Payload is small.** A run group is ~10 MB: 129 page PNGs (9.3 MB) plus 776 KB of JSON.
  Caching the whole thing is cheap; selective caching would buy nothing.
- **`/data/<group>/<file>` is run-agnostic.** `DATA(f) = /data/${GROUP}/${f}`
  (`index.html:640`) produces byte-identical URLs for every flight ever uploaded; the server
  resolves the run from the in-process `_current_run` dict (`app.py:832-839`).
- **Map tiles are third-party.** `L.tileLayer("https://{s}.tile.openstreetmap.org/...")`
  (`index.html:666`) — the only asset on the map that is not ours.
- **Server retention is short.** `runs/` is swept at 24 h on the next upload
  (`app.py:768-774`), and Railway's filesystem is wiped on every redeploy.

### The load-bearing problem

Run-agnostic URLs and caching are incompatible. A cache entry keyed `/data/1/airports.json`
from TG970 is indistinguishable from TG415's. Offline, the crew would be served the wrong
flight's weather under the right flight's number — a failure strictly worse than having no
offline mode at all. This had to be resolved before any caching design.

It is also a live bug today, independent of offline: a second upload silently repoints every
already-open `/map` tab at the new flight.

### The iOS constraint

The target device is an iPad. Chrome on iOS **cannot register a service worker** — all iOS
browsers are required to use WebKit, and Apple restricts the Service Worker API to Safari,
`SFSafariViewController`, and home-screen web apps. `navigator.serviceWorker.register()`
rejects outright in Chrome, Firefox, and Edge on iOS. This is not a degradation to design
around; it is a hard absence of the mechanism.

## Decision

### 1. Run-scoped URLs

Routes become `/data/<run_id>/<group>/<file>` and `/map?r=<run_id>&g=<n>`. Cache keys are
unique per flight, multiple briefings can coexist, and "which flight am I looking at" is
answerable offline from the URL alone.

The single-segment legacy route `/data/<filename>` (MVP demo, `app.py:878`) does not collide
with a three-segment path and is untouched.

**The run is identified by the request, never by server memory.** `serve_group_data` drops its
`_current_run` gate and serves any run whose `manifest.json` exists. `POST /api/hira`
(`app.py:852-854`) takes `run_id` in its body and is gated the same way — it carried the same
`_current_run` dependency, which would 404 on any multi-upload day when the crew returns to an
earlier tab. The run_id is an unguessable UUID; the URL is its own capability.

**`manifest.json` is written last and is the completion marker.** This replaces the
`status == "done"` half of the old gate with on-disk state, so no in-memory tracking is needed
to know a run is safe to serve.

### 2. Service worker, cold-start capable

Precaches the app shell (`/map`, `/static/leaflet.js`, `/static/leaflet.css`, the manifest)
plus every file in the run manifest. A rebooted device with no signal opens the briefing
complete. Rejected: caching only enough to survive a reload — a cockpit tablet backgrounds and
gets swiped away mid-sector, so the page will be evicted.

**Fetch strategies:**

| Path | Strategy | Why |
|---|---|---|
| `hira.json` | network-first, cache fallback, **never cache a negative response** | Can go 404 → 200 after generation |
| `/data/<run_id>/**` | cache-first | Immutable once written; run_id makes the URL content-addressed |
| `/tiles/**` | cache-first, separate never-swapped bucket | Immutable, and shared across flights |
| `/map`, `/static/**` | stale-while-revalidate | Instant start, but must pick up deploys |
| `/upload`, `/api/status`, `POST /api/hira` | never cached | Live endpoints |

**Rule order in that table is load-bearing.** `hira.json` is served *at*
`/data/<run_id>/<group>/hira.json`, so it matches the generic `/data/**` rule too and must be
checked first. If cache-first won, the client's load-time probe (`index.html:1694`) would 404,
the SW would cache that 404, and a HIRA generated ten minutes later would never surface — the
button staying dotless with a valid brief sitting on the server.

**Shell updates never act on their own.** A new deploy is detected in the background and
surfaced as a dismissible `New version — reload` chip. Auto-reloading the page mid-brief, or
worse mid-flight, is the failure this avoids. Rejected: network-first for the shell (stalls
every start on a slow link) and a manual SW version constant (silently does nothing when
someone forgets to bump it).

**The service worker owns the precache sweep.** A 3–4 leg upload opens two map tabs; whichever
loads first messages the SW to start, subsequent messages are no-ops, and both tabs subscribe
for progress. Tab-driven sweeping would fetch every tile twice, show two chips disagreeing with
each other, and restart from zero on a reload.

**Offline navigations resolve to the cached briefing.** `/`, `/upload`, or a run-less `/map`
opened with no network serve the cached run's map. Paired with a web app manifest
(`display: standalone`, `start_url: /`), the app installs to the Home Screen and opens straight
into the current briefing with no URL bar and no bookmark discipline.

### 3. Automatic precache with a readiness chip

The sweep starts on map load, unprompted, and a header chip shows `Caching 34/136` →
`Offline ready ✓`.

Rejected: an explicit "Save for offline" button. It is the conventional pattern and it respects
metered connections, but it adds a step a crew can forget, and forgetting it means discovering
at FL350 that there is no briefing. 10 MB on dispatch wifi is not worth a safety-relevant step.
The chip is not decoration — it is the answer to "am I safe to go offline?" and must be
glanceable before pushback.

**The chip must be honest when the mechanism is absent.** Where no service worker can register
(Chrome on iOS), it must say so and point at the bundle, never sit silently amber.

### 4. Tiles: server-fetched, own origin, budget-derived depth

The pipeline fetches tiles into a shared server-side store keyed `z/x/y`; Leaflet points at
`/tiles/{z}/{x}/{y}.png`. Tiles are listed in the run manifest and precached like page PNGs —
no cross-origin special-casing, no `{s}` subdomain rotation. It is also better OSM
citizenship: one fetch per tile ever, with a proper `User-Agent`, shared across flights and
devices, rather than every device pulling the full set. (OSM's usage policy prohibits bulk
downloading; this keeps the volume to one-per-tile-ever rather than one-per-device-per-flight.)

**Zoom depth is derived from a ~400-tile budget, not fixed.** Cache z0 down to the deepest zoom
whose cumulative count stays under ~400 tiles (≈8 MB). A fixed ceiling is badly calibrated
because the bbox needing the most depth costs the least to provide it:

| Route | z6 cumulative | Ceiling under a 400-tile budget |
|---|---|---|
| EDDF→VTBS (long-haul) | 341 / 6.7 MB | **z6** (z7 = 989) |
| VTBS→EBBR (long-haul) | 382 / 7.5 MB | **z6** (z7 = 1122) |
| VTBS→VHHH (medium) | 102 / 2.0 MB | **z8** (288 / 5.6 MB) |
| VTBS↔WMKK (turnaround) | 81 / 1.6 MB | **z9** (248 / 4.8 MB) |

A fixed z6 would leave a VTBS↔WMKK turnaround using 1.6 MB of a 7 MB budget while opening onto
upscaled tiles at its own `fitBounds` view — and the header buttons' `flyTo(..., max(zoom, 7))`
(`index.html:695`) goes straight past the ceiling.

**Beyond the cached ceiling, Leaflet upscales rather than requesting tiles that were never
fetched.** Blurry beats blank, at zero storage cost. At z7+ the crew is identifying *which*
airport they are looking at, not reading taxiways.

> **Correction, 2026-08-17 (during slice 3).** This was originally specified as *"on a miss at
> deeper zoom, the service worker returns the containing parent tile"*. That is wrong and was
> not implemented. A parent tile covers 4× the child's area, so dropping it into the child's
> 256 px slot renders geography that is not merely blurry but *misplaced* — features sit at
> the wrong coordinates. Correcting it inside the SW would mean decoding, cropping and
> re-encoding via `OffscreenCanvas` for every miss.
>
> Leaflet's own `maxNativeZoom` already solves this correctly and natively: it requests the
> tile at the deepest cached zoom and stretches it across the right area. The pipeline writes
> the run's ceiling to `basemap.json` and the client sets `maxNativeZoom` from it, so tiles
> past the ceiling are never requested at all and there is no miss to fall back from. The SW
> needs no tile-specific logic beyond plain cache-first.
>
> A tile that *is* within the ceiling but genuinely missing from cache renders blank. That is
> deliberate: blank-with-correct-geography beats wrong-geography on a map a crew is reading.

**The bbox unions every leg across both groups**, and the fetch is a run-level pipeline step
(it needs `route_<n>.json`, so it runs after step 2) alongside `_run_source_pane_step`, not a
per-group one — otherwise group 2's map opens over blank tiles.

**The server-side tile store lives outside `runs/`** — `data/tiles/<z>/<x>/<y>.png` — and is
exempt from the 24 h sweep. The sweep walks `runs/` (`app.py:768-774`), so a store placed
inside a run dir would be deleted with it and the cross-flight sharing that justifies
server-side fetching would buy nothing. Growth is bounded in practice by the airline's route
network, and the store is rebuilt from scratch after any Railway redeploy regardless.

### 5. Single cached briefing, swapped atomically

One run-scoped cache bucket. "One briefing" means **one run, not one group** — a 4-leg upload's
two groups share the bucket, or the second tab would break the first.

**Tiles go in a separate, never-swapped bucket.** The single-briefing rule exists to guarantee
the crew is never shown another flight's data; tiles carry no flight-specific information and
are immutable, so that rationale does not apply to them. Keeping them out of the run bucket is
what makes the server-side sharing worth anything on the device — otherwise every swap
re-downloads ~8 MB of basemap the device already had. Bounded by the ~400-tile budget per
distinct corridor; a "clear map cache" control is the escape hatch if it ever matters.

**The old bucket is deleted only after the new precache completes.** Transient peak ~34 MB of
briefing data (plus the shared tile bucket, which is not duplicated), which guarantees at least
one complete briefing is on the device at all times. Evicting on new map load would open a
multi-minute window with neither briefing intact.

`navigator.storage.persist()` is requested unconditionally to resist disk-pressure eviction.

### 6. Never expire; show age

The briefing always renders. The header carries its generation timestamp and turns amber past
12 h with an explicit age (`⚠ 14 h old — verify current wx`).

Stale weather clearly labelled stale is a tool working correctly; stale weather that *looks*
current is the failure mode. A hard expiry was rejected because leaving the crew with nothing on
a long sector is strictly worse than labelled-stale data.

### 7. HIRA stays online-only, but cached opportunistically

`POST /api/hira` is a live Sonnet call and is not part of the precache — preserving both
original reasons for on-demand generation (uploads stay fast; unopened runs cost no tokens).

But once a brief has been generated and fetched, the SW keeps it. A hazard brief the crew read
at the gate vanishing at FL350 — taking the header risk dot dark with it — is a surprising
regression from their point of view.

### 8. Self-contained bundle as a second, different capability

`GET /bundle/<run_id>` builds a single ~25 MB `.html` with all JSON, page PNGs, tiles and
Leaflet inlined as base64, writes it into the run dir, and serves it. Cache-first, mirroring the
existing `POST /api/hira` idiom (`app.py:842-873`) — a crew that never needs it never pays for
it.

**Its justification is durability, not the iOS gap.** On iPad a downloaded `.html` opened from
Files realistically lands in Safari anyway, so it is not really the Chrome workaround it first
appears to be. What it actually provides is a briefing that is independent of the server: it
survives the 24 h sweep, survives a Railway redeploy, can be AirDropped to the other pilot, and
can be retained as a record of what was briefed.

**It must not fork `index.html`.** The generator injects one `window.__BUNDLE__ = {...}` blob
into the existing file; `DATA()` gains a single conditional returning a `data:` URI (valid in
both `fetch()` and `<img src>`), and tiles need ~10 lines of
`L.TileLayer.extend({ getTileUrl })`. Two copies of a 70 KB briefing UI drifting apart is the
outcome to avoid — the `wx_tier` thresholds and Source Pane fill logic living in that file are
precisely what CLAUDE.md documents as regression-prone.

One bundle per run covering every group, with the group switch handled in-page; `?g=` on a
`file://` URL is fragile and two files for a 4-leg day is a filing problem.

Tiles are included (~10.7 MB base64 of the ~25 MB). A briefing whose map is blank is not the
pictorial click-on-the-airport tool the project exists to be.

### 9. Safari + Add to Home Screen is the daily path

The service worker path requires Safari on iPad, added to the Home Screen, which also reduces
exposure to Safari's site-data eviction (not verified in detail, and immaterial either way — 12
hours is far inside any eviction window). The bundle covers every other browser and device.

## Implementation surface

Not a task list, but the concrete edit points identified while designing this, recorded so they
are not rediscovered:

- `index.html:640` — `DATA()` and `GROUP` must carry `run_id` parsed from `?r=`
- `index.html:666` — tile layer URL moves to `/tiles/{z}/{x}/{y}.png`
- `index.html:695` — `flyTo(..., max(zoom, 7))` is what the tile budget must reach
- `app.py:832-839` — `serve_group_data` drops the `_current_run` gate, gains `run_id`
- `app.py:852-854` — `generate_hira` takes `run_id` from the POST body
- `app.py:768-774` — the sweep must not walk the tile store
- `_PROGRESS_HTML`'s completion link must emit `/map?r=<run_id>&g=<n>`
- `/` → `/upload` redirect (`app.py:715-722`) needs the offline-navigation interception

## Consequences

**Accepted risk — single client copy plus 24 h server retention compound.** The client keeps one
briefing and the server sweeps at 24 h (unchanged, deliberately). A briefing generated at 0500Z
on day 1 is swept by any upload after 0500Z on day 2, so a cache eviction on the return sector
leaves nothing to refill from. The self-contained bundle is the mitigation for anyone who wants
a durable copy.

**Also accepted:** Railway's ephemeral filesystem wipes both `runs/` and the shared tile store on
every redeploy; tiles are refetched on the next upload.

**A tile fetch failure must never fail the briefing.** It gets its own try/except and progress
warning, exactly as `_run_source_pane_step` does per document — the basemap degrades, the
briefing does not.

**The precache sweep must tolerate expected 404s** (`hira.json`, and `warnings.json` when empty)
without stalling the readiness chip.

## Verification

Unit-testable, and belongs in `tests/`:

- bbox → tile list, asserting the budget caps cumulative count across long-haul, medium and
  turnaround bboxes
- every entry the manifest lists exists on disk (catches a phantom entry)
- every `*_page_*.png` and pipeline-written `*.json` in the run dir appears in the manifest —
  this catches the highest-consequence bug in the design, a page PNG silently absent from the
  precache. **`hira.json` and `bundle.html` are explicitly excluded**: both are written *after*
  the manifest (on demand, decisions 7 and 8), so a naive "manifest ≡ files on disk" assertion
  would go red the first time anyone taps HIRA.
- `manifest.json` is the last file the pipeline writes

Not unit-testable, and documented as a manual procedure in CLAUDE.md: DevTools' "Offline"
checkbox does not cold-start the app, does not evict, and does not reproduce WebKit behaviour.
The only test that proves the requirement is **airplane mode on the real iPad after a reboot.**

## References

- Service workers are unavailable in third-party iOS browsers:
  <https://developer.apple.com/forums/thread/126923>,
  WebKit bug 206741 <https://bugs.webkit.org/show_bug.cgi?id=206741>
- OSM tile usage policy (bulk downloading prohibited):
  <https://operations.osmfoundation.org/policies/tiles/>
