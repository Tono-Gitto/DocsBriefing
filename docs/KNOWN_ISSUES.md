# Known Issues

Open problems, unproven claims, and accepted limitations. Close an entry by deleting it in
the same commit that fixes it.

Last reviewed: 2026-08-22, closing #6 (fixture tree and docs now agree).

| # | Issue | Severity |
|---|---|---|
| 1 | Deployment URL in `CLAUDE.md` 404s — nothing can be tested against the live app | **blocker** |
| 2 | iOS Add to Home Screen creates a bookmark, not a standalone web app | high |
| 3 | Manifest `start_url` lands on the upload form, not a briefing | high |
| 4 | Offline briefing never verified on the target iPad | high |
| 5 | Cached bundle goes stale after an `index.html` deploy | low |
| 7 | HIRA deliberately switched off | tracked, not a bug |
| 8 | Accepted limitations (service workers on iOS Chrome; single cached briefing) | by design |
| 9 | Manual tier overrides reach neither HIRA nor the bundle | by design |
| 10 | `_GROUP_RE` misses a space-split `FM DDHHMM`, running two TAF states together | low |

---

## #1 — The deployment URL 404s, blocking all live testing

**Status:** open · **Severity:** blocker

`https://web-production-2ec19.up.railway.app`, recorded in `CLAUDE.md`'s Deployment
section, returns Railway's own `404 Application not found` on **every** path. That is a
platform-level "no app at this hostname", not the application erroring — so either the
deployment URL has changed or the service is down.

The bundle download was observed working on an iPad, so a working instance clearly exists
at *some* address. **The current URL is needed before #2, #3 or #4 can be investigated at
all.** Correct `CLAUDE.md` once it is known.

---

## #2 — iOS Safari "Add to Home Screen" creates a bookmark, not a standalone web app

**Status:** open · **Severity:** high · **Area:** ADR 0003, slice 3

### Symptom

On an iPad, Safari → Share → Add to Home Screen produces what behaves like a plain URL
bookmark rather than an installed standalone web app.

This matters because ADR 0003 §9 makes **Safari + Add to Home Screen the daily offline
path**. The self-contained bundle was tested at the same time and **works**, so a
functioning offline route exists today — the convenient one is what is broken.

### Establish this first

**Standalone install and offline caching are different things, and only the first is
confirmed broken.** Service workers run in ordinary Safari *tabs* too, so the briefing may
already work offline without the Home Screen icon. This decides whether #2 is a packaging
annoyance or a functional failure:

1. Open the briefing in a normal Safari tab. Does the header chip reach `✓ Offline ready`?
2. With the chip green, enable airplane mode and reload. Does the full briefing render,
   including basemap and both DOC pane documents?

If both pass, the offline requirement is already met in Safari and this is cosmetic. If the
chip never turns green, the real fault is service worker registration and this entry is
mistitled.

### Candidate causes (none verified)

- The manifest never loaded or failed to parse on the device — most likely, easiest to
  check. Safari caches manifests aggressively, so a visit made *before* the manifest
  existed can stick.
- `apple-mobile-web-app-capable` is deprecated in favour of the manifest's `display`
  member. Both are present in `index.html`, so this should not be it — worth ruling out.
- The page was added from a URL carrying `?r=…&g=…`; iOS prefers the manifest's
  `start_url`, and behaviour when the manifest is unreachable is poorly specified.

### Diagnostics

- Open `<host>/static/app.webmanifest` directly on the iPad: expect HTTP 200,
  `Content-Type: application/manifest+json`, valid JSON.
- Attach macOS Safari's Web Inspector (Develop menu) → console for manifest parse errors,
  Application → Service Workers for a live registration.
- Confirm the origin is HTTPS. Service workers require a secure origin; `localhost` is
  exempt, a bare-IP or plain-HTTP host is not.

### Workaround

The `⬇ Bundle` button — one self-contained HTML file, no server, no service worker, no
install. Verified working on the device.

---

## #3 — Manifest `start_url` opens the upload form instead of a briefing

**Status:** open · **Severity:** high · **Confirmed** (not speculative)

`static/app.webmanifest` declares `"start_url": "/"`, but `/` redirects to `/upload`
(`app.py:840-842`). So even once #2 is fixed, tapping the Home Screen icon **while online**
lands on the upload form rather than a briefing.

It happens to work offline only because the service worker intercepts that navigation and
redirects to the cached run (`navigationFetch` in `static/sw.js`). The online path has no
equivalent.

There is no single correct value, because the briefing URL is run-scoped
(`/map?r=<run_id>&g=1`) and changes with every upload. Options:

- a new server route that 302s to the most recent completed run, used as `start_url`
- keep `/` but redirect to the latest run when one exists, upload form otherwise
- rewrite the manifest's `start_url` at runtime from the client via a blob URL

The second is probably the least machinery, but it changes what `/` means for everyone, so
it deserves a moment's thought rather than a reflex.

---

## #4 — The offline briefing has never been verified on the target device

**Status:** open · **Severity:** high

Everything in ADR 0003 was verified on **desktop Chrome**, by killing the server outright
and reloading — map, basemap tiles, markers, and both Source Pane documents all rendered.
That is a real test, but it is not the target.

The requirement is 12 hours offline on an iPad. Nothing has proven that. The procedure is
in `CLAUDE.md`: Safari → wait for the green chip → **airplane mode → reboot** → open.
Blocked by #1, and entangled with #2.

Until this passes, treat the offline feature as *plausible but unproven* on the aircraft.

---

## #5 — A cached bundle goes stale after an `index.html` deploy

**Status:** open · **Severity:** low

`GET /bundle/<run_id>` is cache-first on disk (`app.py`, mirroring the `POST /api/hira`
idiom), so a bundle built **before** a change to `index.html` keeps serving the old UI.

Mostly self-correcting, since runs are swept at 24 h and Railway wipes the filesystem on
every redeploy. Delete `runs/<run_id>/bundle.html` to force a rebuild. Worth a real fix
only if bundles ever outlive a deploy — e.g. stamp the builder with a version and rebuild
on mismatch.

---

## #7 — HIRA is deliberately switched off

**Status:** intentional · Tracked so it is not forgotten.

`HIRA_ENABLED = False` in both `app.py` and `index.html`: the header button is hidden,
`openHira()` no-ops, the load-time cache probe is skipped, and `POST /api/hira` returns
404 so no Sonnet call can be reached even directly.

Nothing was deleted — `hira_engine.py`, the modal, the risk dot and the retry logic are
intact. Flip both flags to re-enable. Note that ADR 0003 §7 assumes HIRA is reachable
online; re-enabling restores that assumption.

---

## #8 — Accepted limitations (by design)

Recorded so they are not rediscovered as bugs.

- **Service workers do not exist in Chrome/Firefox/Edge on iOS.** WebKit exposes them only
  to Safari and Home Screen web apps. The readiness chip says so explicitly rather than
  sitting amber; the bundle is the offline path there. See ADR 0003 §9.
- **One cached briefing plus 24 h server retention compound.** The device keeps a single
  briefing and the server sweeps runs at 24 h, so a cache eviction on a return sector can
  leave nothing to refill from. Both halves were chosen deliberately; the bundle is the
  mitigation for anyone wanting a durable copy. ADR 0003 → Consequences.
- **The basemap tile store is wiped on every Railway redeploy** (`data/tiles/`, ~6 MB
  locally). Tiles are refetched on the next upload. Harmless, just slower.
- **Deep zoom beyond the cached ceiling is upscaled and blurry.** Deliberate — a parent
  tile covers 4× the child's area, so substituting it would render *misplaced* geography
  rather than merely soft. See the correction recorded in ADR 0003 §4.

---

## #9 — Manual tier overrides reach neither HIRA nor the bundle

**Status:** intentional · Tracked so neither divergence is rediagnosed as a bug.

Overrides (ADR 0004) live in the browser's `localStorage`, scoped to the run. Two places
therefore keep showing the engine's own tiers:

- **The HIRA brief and its risk dot.** `hira_engine.build_digest()` reads tiers server-side
  from `airports.json`, and `hira.json` is cached on disk and in the service worker. Fixing
  it would mean a server round-trip plus a fresh 10–30 s Sonnet call that cannot happen
  offline — so the brief would go silently stale exactly when it matters. The modal states
  the divergence instead: *"Generated from automatic tiers — N manual tier change(s) not
  reflected."* Currently dormant anyway, see #7. ADR 0004 §7.
- **`bundle.html`.** It is a `file://` origin, so its storage is empty or unavailable and the
  bundle renders automatic tiers only. The loss is asymmetric: a missing *downgrade* shows
  something more severe (safe), but a missing *promotion* means a NOTAM the crew hand-marked
  T1 reaches the other pilot as T3. The Download-bundle button confirms when overrides exist.
  Shipping them into the bundle was rejected — it needs the server round-trip above plus
  invalidating the disk-cached bundle on every tap. ADR 0004 §8.

Also by design: overrides are **not** carried into the next run (a NOTAM's meaning depends on
the flight), and two tabs on one run converge on reload rather than live.

---

## #10 — `_GROUP_RE` misses a space-split `FM DDHHMM`, running two TAF states together

**Status:** open · Found while fixing the element-wise BECMG fold; deliberately not fixed
in the same pass.

`met_engine._GROUP_RE` matches an FM group as `FM\d{6}` with no separator. Two fixture TAFs
have a space in the extracted text — `FM 180500` (TG970 UPDATE, OPKC) and `FM 271600`
(TG934, OPLA) — so the group is never recognised and its conditions are absorbed into the
*preceding* group's text, leaving one "group" that states two full states:

```
BECMG 1720/1722 26008G18KT 4000 HZ SCT020 BKN030 FM 180500 25010G25KT 4000 HZ SCT020 BKN030
```

**Impact is display-only and currently contained.** Both affected airports are enroute
contingency airports — never a departure, destination or alternate — and neither changes
`wx_tier`, because the duplicated visibility is identical in both states. `_becmg_merge`
detects the double visibility and falls back to wholesale replacement, so the text renders
in its original readable order rather than interleaved by element (see the CLAUDE.md gotcha).

**Why it wasn't fixed here:** widening the regex to `FM\s*\d{6}` shifts every subsequent
group's `src_start`, which is what `met_anchors.py` keys its ETA-window rectangles on — a
Source Pane change that deserves its own verification pass against the fixture anchors, not
a ride-along on a tier fix.

**To close:** widen the regex, then re-run `tests/test_integration.py::TestMetAnchors` and
confirm the 49/49 fidelity-gate result and VECC's page-crossing block still hold.
