# Known Issues

Open problems, unproven claims, and accepted limitations. Close an entry by deleting it in
the same commit that fixes it.

Last reviewed: 2026-09-09, amending #13 (destination planning minima implemented, regulatory
fidelity unverified) and #16 (Planning Minima gate widened to weather-or-equipment-finding).

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
| 11 | `data/aerodrome_minima.json` is wiped on every Railway redeploy — unlike tiles/fir_coords, not re-derivable | accepted |
| 12 | `era` extraction can't distinguish Fuel ERA from a future EDTO ERA in the same field | low |
| 17 | A manual tier override can go silently inert if its NOTAM later splits into COM-INFO parts | accepted |

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

---

## #11 — The aerodrome-minima store is wiped on every Railway redeploy

**Status:** accepted, not engineered around · See `docs/adr/0005`.

`data/aerodrome_minima.json` (hand-entered Table 3 rows + base minima, docs/adr/0005) lives
in `data/`, the same directory `data/tiles/` and `data/fir_coords_learned.json` already sit
in — and that directory does not survive a Railway redeploy (the whole container filesystem
resets from git). Tiles and learned FIR centroids degrade gracefully because they are
re-derivable; hand-entered minima are not — a redeploy silently erases everything a
dispatcher typed in.

Accepted rather than fixed: a Railway persistent Volume is real infrastructure outside a
coding session's reach, and committing the store to git would fight the "edit inline, save
instantly" flow the feature is built around. Already-completed runs are unaffected — each
keeps its own `minima_snapshot.json` inside `runs/`, independent of the live store — only a
*new* upload made after a redeploy starts against an empty store and needs its alternates
re-entered.

---

## #12 — `era` extraction can't distinguish Fuel ERA from a future EDTO ERA

**Status:** open, low — no known trigger in any current fixture · See `docs/adr/0005` §1.

`app.py`'s `_extract_alternates` populates `era` from `ERA/XXXX` and `FUEL ERA (XXXX)`
patterns — both are the fuel-scheme concept (§8.1.7.5.5's 3%-contingency mechanism), not an
EDTO diversion alternate (§8.5.6.7/§8.5.6.8), confirmed against the TG934 fixture's own text
(`"CF 3% ERA/LTFM"`, `"FUEL ERA (LTFM) FUEL TIME DISPATCH LOAD"`). The alternate/ERA
Planning Minima feature (docs/adr/0005) relies on this: it applies §8.1.6's first table row
(Destination/Takeoff Alt/Dest Alt/Fuel ERA) to every `era` airport, not the table's separate,
more permissive EDTO ERA row.

If a future OFP format ever puts an EDTO diversion alternate into this same field, the wrong
row would silently apply — in the false-PASS direction, since the EDTO row treats a
transient-phenomenon TEMPO deterioration as applicable where the fuel-ERA row disregards it.
Nothing currently triggers this; recorded so it isn't rediscovered as a fresh bug the first
time it does.

## #13 — Destination planning minima (§8.1.3.2.3) — implemented, regulatory fidelity unverified

**Status:** open, downgraded from "not computed" to "computed, but not confirmed to match
OM-A's exact wording" — not closed outright, because the residual risk (a wrong number
reaching the aircraft) is the same class this ADR was written to avoid.

The destination Planning Minima block now computes a real verdict (`index.html`'s
`_minimaCompute`, `role === "destination"` branch, docs/adr/0006 §3 amendment) instead of
rendering `NOT COMPUTED`. `minima_snapshot.json` was extended to `dest`/`rcf_dest` ICAOs
alongside alternate/ERA/`rcf_altn` (ADR 0005 §7 amendment) so the check also works offline.

**What was implemented, precisely:** the entered DH/MDH and RVR/VIS are compared directly
against the applicable ETA±1h forecast, `>=` passes, with the Table 3 margin pinned to `0`
instead of a selected row's increment — the same `_minimaVerdict()` the alternate/ERA check
uses, not a second hand-written one. Layer 1's re-determined RVR (when a facility is failed)
feeds in identically to the alternate check.

**What this is not:** OM-A §8.1.3.2.3's exact wording (as understood when this was first
deferred) tests "ceiling above MDH" for an NPA/circling approach, a comparison against a
different quantity than Table 3's "MDH + increment". This implementation is the same-shape,
zero-margin comparison the crew asked for — not a verified implementation of that specific
NPA/circling nuance. If the exact regulatory text is later obtained and it turns out to
specify something other than a bare `>=` compare against the entered DH/MDH, this needs a
second pass, not just a config change.

## #14 — Two NOTAM schedule forms are unparsed, so their windows read as continuous

`notam_engine._parse_daily_windows()` and `_parse_date_schedules()` both miss the
quoted day-of-month form that TG638's VTBU `VTBDJ6288/26` uses:

```
"25 0800-1200, 26 0230-1000"
```

`_DATE_SCHED_RE` requires a month name (`JUN 29 1900-2330`), and the comma-separated
day-number form matches neither pattern. The NOTAM therefore keeps only its absolute
window (`*25 AUG 2026 08:00 – 26 AUG 2026 10:00*`) and reads as continuously active
across the whole 26 hours, including 26 AUG 0000–0230Z when it is not.

This predates ADR 0006 and already affects NOTAM tiles via `_effective_tier`. It bounds how
precise the equipment-finding band gate (ADR 0006 §8) can be. Not fixed because that
particular NOTAM is an ILS flight check — not a §8.1.3.3.6 facility — so it produces no
finding either way; but any NOTAM using this form has the same imprecision.

## #15 — Failed-equipment findings: accepted imprecisions

All from ADR 0006, all deliberate:

- **Net 2 noise.** 71 findings across 28 distinct texts in the nine fixture NOTAM PDFs are
  reported as "not among the facilities §8.1.3.3.6 permits" — stopway lights, guard lights,
  RETIL, exit-taxiway indicators, apron/stand lighting, ambiguous "LGT FOR RWY 16L". One
  tuning pass has been done. Over-reporting is the deliberate trade against a silent miss;
  anything appearing here is also a candidate missing alias in net 1.
- **Circling has no column of its own.** §8.1.3.3.6's table has only Type B and Type A
  columns. A circling MDH is ≥ 250 ft and therefore lands in Type A.
- **A named enroute DME outage still produces a DME finding.** `DME 'LMN' U/S` is a listed
  facility with a failure verb; the tool cannot tell it is not the approach's DME. It
  renders as informational unless its runway matches the recorded approach.
- **Offline, layer 1's arithmetic only works where minima were snapshotted.** Findings and
  the RVR table precache everywhere, but the store entry does not: `minima_snapshot.json`
  covers alternate/ERA/`rcf_altn` ICAOs only. So a destination or enroute airport with a
  finding shows the finding and the class downgrade offline, but no re-determined number.
  Online it works — the client fetches the whole store. Extending the snapshot to every
  airport in `airports.json` would bake ~50 entries into every group dir to serve the
  handful that ever have one.

## #16 — Planning Minima only renders where equipment has failed OR weather is off-GREEN

**Status:** amended, deliberate (ADR 0006 §10 amendment, narrowing ADR 0005 less than the
original v1 gate did).

ADR 0005 rendered the Planning Minima block on every `dest_altn`/`era`/`rcf_altn` for every
leg — OM-A §8.1.3.2.4's selectability check applies to an alternate whether or not any
equipment is failed. ADR 0006 v1 gated it on the leg having at least one §8.1.3.3.6
equipment finding, so it appeared only alongside the Failed Ground Equipment block; that
gate is now widened to `wx_tier !== "GREEN" || equipment_findings.length > 0` — a weather
deterioration is treated as just as valid a reason to check planning minima as a failed
facility, and either alone is now sufficient.

**Why the original gate existed:** the base minima are hand-entered and most aerodromes have
none, so the always-on block printed `No entry for XXXX — Enter minima` on every alternate of
every leg regardless of anything happening. The regulatory check was invisible inside its own
noise.

**Why widening it doesn't reintroduce that noise:** the noise problem was never "the block
renders too often" — it was that a rendered block with nothing on file could only ever nag.
Under the new gate, the `No entry` prompt still only appears at a moment the tool judged
actually useful (weather has gone marginal, or a facility has failed), never unconditionally,
and it opens straight into the Equipment form that resolves it.

**What is still lost, narrower than before:** a *clean-weather* alternate with all equipment
serviceable still shows no PASS/FAIL — §8.1.3.2.4 technically applies there too. That is the
remaining accepted gap; the majority-case gap (any weather-marginal alternate/destination
with no equipment failure) is closed.

**Restoring the full always-on check:** delete the `weatherTriggered ||` half of the
condition at the top of `_minimaBlockHtml` in `index.html`, reducing it back to the
findings-only gate; deleting the whole condition restores ADR 0005's fully always-on
behaviour and its original noise.

---

## #17 — A manual tier override can go silently inert if its NOTAM later splits

**Status:** accepted, deliberate. No migration attempted.

COM-INFO bundle splitting (`notam_engine._split_com_info_parts`) now runs in every NOTAM
section, not just `GENERAL`/`FLIGHT LEG`/`AEROPLANE`. If a pilot sets a manual tier override
(ADR 0004) on an airport/FIR NOTAM that is a whole, unsplit COM-INFO bundle at the time
(`localStorage` key `<owner>|<id>`), and the flight is later re-uploaded after that bundle
picks up a `--` boundary it didn't have before, the rendered rows become
`<owner>|<id> [1]`..`[N]` — none of which match the old key. The override becomes an inert,
orphaned `localStorage` entry: not lost data, not a crash, just silently stops applying.

**Why not migrated:** the old override has no way to know which of the N new sub-notices it
was about — a RETIL-outage override and a DVOR/DME-suspension override look identical once
collapsed to one bundle-level key. Guessing wrong (e.g. defaulting it onto part `[1]`) would
be worse than doing nothing, since it would silently misapply a pilot's judgment to an
unrelated sub-notice.

**How likely in practice:** narrow. It requires an override to exist on a bundle that (a) was
whole at override time and (b) gains new sub-notices or a `--` boundary on a later
re-upload of the same recurring flight — a bundle's dash-boundary structure is set by the
NOTAM PDF's own formatting, not something that changes flight-to-flight for a fixed bundle.
