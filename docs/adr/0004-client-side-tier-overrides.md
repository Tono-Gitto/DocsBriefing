# ADR 0004 — Manual NOTAM tier overrides, client-side and run-scoped

Date: 2026-08-17
Status: Accepted — implemented in `index.html` (no server change)

## Context

`notam_engine._classify_tier()` rates every NOTAM T1/T2/T3 from regex tables, and
`_effective_tier()` then downgrades anything outside its daily operating window. The rating is
good but it is not a pilot. A crew member reading the briefing routinely knows that a given T1
does not matter for *this* flight, or that something rated T3 does.

The ask: let the crew re-rate a NOTAM from the panel, and have that rating survive reopening the
briefing and using it offline.

Three properties of the app shape every decision below:

- **The tier is not just a badge.** It is read at eight places: the badge itself, panel sort
  (`index.html:1385`, `1432`), the FIR diamond color (`1871`), runway-chip glow (`1202`), the
  FLIGHT badge counts (`1543`), the T3 "show N more" collapse (`1440`, `1471`, `1506`), and —
  least obviously — **Selection Sync's** NOTAM target, which is defined as "the first
  `.notam-row[data-anchor-key]` in DOM order" and is only correct *because* panels render
  tier-sorted (ADR 0002, Decision 2).
- **The briefing is used with no network.** `static/sw.js` is network-only for `/api/*`, so any
  design that writes an override to the server does not work in the one place it is most wanted.
- **Three origins, one app.** The briefing is read served (`https://…/map?r=…`), from the
  service-worker cache at the same origin, and as `bundle.html` on `file://` — a **different
  origin**, whose storage is empty or unavailable.

## Decision

### 1. Store client-side, in `localStorage`, scoped to the run

Key `preflight.tiers.<run_id>`, value `{saved_iso, overrides: {"<owner>|<id>": tier}}`.

Writes are instant, need no server, and work in airplane mode — the only place the crew is
actually holding the iPad and disagreeing with a rating. The cost is that overrides do not
travel: not to the other pilot, not into the bundle (§8), not into HIRA (§7).

**Run-scoped, and deliberately not seeded from the previous run.** A NOTAM's operational
meaning depends on the flight — a runway closure that is irrelevant at 0600Z matters at 1800Z —
so carrying a downgrade into tomorrow's pack would silently hide a live hazard. Each dispatch
pack is judged fresh.

**Every write is read-modify-write.** A 3–4 leg upload opens **two map tabs on one `run_id`**
(different `?g=`), and both share the key; `general_notams.json` carries the same NOTAM ids in
both groups, so this is the documented flow, not a corner case. Writing an in-memory object
wholesale would let tab 2's next tap erase tab 1's work.

**Bounded, and never fatal.** On load, `preflight.tiers.*` keys are pruned to the 20 most recent
by `saved_iso` (the current run always kept). Every read and write is wrapped — Safari private
browsing throws on `setItem` — and on failure the overrides degrade to an in-memory map that
lasts the session. A storage failure must never take the briefing down.

### 2. Apply once, at data-load time, by mutating `tier`

After each of `airports.json` / `fir_notams.json` / `general_notams.json` is fetched, every
NOTAM gets `tier_auto = tier` stashed, then `tier` overwritten from the override map.

All eight consumers above stay coherent for free, with no edit at any of those call sites. The
alternative — an `effectiveTier(n, owner)` accessor threaded through all eight — makes every
future tier consumer a drift risk, which is precisely the failure mode CLAUDE.md's gotchas exist
to catch. The same pass builds `_tierIndex`, a `key → [notam objects]` map, so a later cycle can
update every copy of a NOTAM without re-walking the data.

### 3. Identity is `"<owner>|<id>"` — the existing anchor key, flattened across legs

The Source Pane already has exactly this key shape (ADR 0001). Reusing it means the app has one
NOTAM identity, not two.

One override applies to **every leg's copy** of that id under that owner. The nuance being given
up is real — `_effective_tier` is per-leg, so a daily-window closure can be T1 at leg 1's ETA and
T3 at leg 2's — but `buildPanel`'s dedup is `if (!notamMap.has(n.id))` (`index.html:1381`), i.e.
**first leg wins**, so the unified panel renders one row per id and there is no affordance in
which a per-leg override could be expressed.

**The gesture reads `data-tier-key`, not `data-anchor-key`.** The latter is only emitted when the
key resolves in the loaded `notam_anchors.json`, which is best-effort and absent wholesale on
legacy and demo runs — hanging the gesture off it would make rows non-cyclable exactly where
anchors failed.

### 4. Tap the badge to cycle; skip states that would not visibly change

`T1 → T2 → T3 → Auto`, implemented as `[1,2,3].filter(t => t !== tier_auto).concat([auto])`. So a
NOTAM the engine rated T3 cycles `T3 → T1* → T2* → T3`, and every tap always changes something
visible — necessary for a control with no confirmation step. An explicit tier equal to `tier_auto`
is therefore unreachable; "reviewed and confirmed" is a different concept and has no UI here.

The badge listener is the **first** branch of the existing delegated click handler
(`index.html:1139`) and calls `stopPropagation()`. Two constraints force this: "a tap anywhere on
an anchored row triggers the anchor — there is no competing per-row click behavior", and
PDF-derived NOTAM ids must never be interpolated into inline `onclick` (quote injection), so the
key travels in a `data-` attribute exactly like the runway chips.

### 5. A 4 s toast with UNDO

`VTBS A1234/26 → T2 · UNDO`. A cycling badge changes a hazard rating on a single tap with no
confirmation, on a touchscreen, in turbulence; the toast is what makes a mis-tap recoverable in
the moment. It also doubles as the only discoverability hint that the badge is interactive.

### 6. Repaint in place; re-sort only on request or on reopen

The tapped badge repaints where it is — the row does **not** move. Cycling `T1 → Auto` would
otherwise mean chasing the row across the panel, possibly into a collapsed "show N more T3" block.
Indicators *not* under the finger update immediately: FIR diamond colors, FLIGHT badge counts,
runway-chip glow.

Ordering is recovered by a `⇅ Re-sort (N changed)` chip inserted above the first NOTAM row,
present only while the panel is stale and gone once tapped; reopening any panel re-sorts anyway.
Re-sorting re-renders, which detaches the node held in `_srcPane.docs.notam.selectedRowEl`, so the
Source Pane's row selection is re-applied by anchor key afterwards.

**An overridden badge is never mistakable for the engine's verdict.** It keeps its tier color — so
the severity semaphore still reads at a glance and no new color enters a screen that already
carries four — renders `T2*`, and shows a muted `auto T1` next to the id, so the engine's original
rating is always legible without mutating anything to discover it.

### 7. HIRA keeps the engine's tiers, and says so

`hira_engine.build_digest()` reads tiers from `airports.json` **server-side**, and `hira.json` is
cached on disk and in the service worker. Overrides live in the browser, so the risk dot and the
prose brief reflect the engine only.

This divergence is accepted rather than fixed. Invalidating `hira.json` on override needs a server
round-trip and a fresh 10–30 s Sonnet call, which cannot happen offline — so the brief would go
silently stale exactly when it matters; recomputing the risk client-side would fork the
diversion-margin logic that CLAUDE.md names as the regression surface, and would still leave the
prose wrong. Instead the modal states it: `Generated from automatic tiers — N manual tier
change(s) not reflected.` (Currently dormant: HIRA is switched off, KNOWN_ISSUES #7.)

### 8. The bundle gets no special-casing, and warns at download

ADR 0003's rule is that `bundle.html` **never forks `index.html`** — `DATA()` and `getTileUrl` are
the only conditionals. Adding an `if (BUNDLE)` gate to disable cycling would be a third fork, for a
UI behavior, in the one place a pilot has time to sit and review. So badges cycle identically
inside the bundle; its `file://` storage either works or falls back to the in-memory map, and edits
last the session.

The loss is asymmetric and worth a warning: a lost *downgrade* renders something more severe
(safe), but a lost *promotion* means a NOTAM hand-marked T1 shows as T3 to the pilot who received
the AirDrop. The Download-bundle button therefore confirms when overrides exist.

### 9. Nothing a human touched may hide behind a collapse

Both directions, symmetrically:

- A **filtered-out** flight-wide NOTAM the crew made *more* severe (`tier < tier_auto`, strictly —
  not merely "an override that lands on T1/T2", which would also pull out a filtered T1 the crew
  had just downgraded to T2) renders in that section's visible list —
  partitioned at *render* time from `tier`/`tier_auto`, never by moving it between the JSON arrays,
  so cycling back to Auto returns it to the audit list with no reload.
- A NOTAM the engine rated T1/T2 that was **manually downgraded** to T3 is exempt from the FLIGHT
  panel's `t3.slice(0, 5)` truncation, so the `T3* / auto T1` pair stays on screen and the override
  stays auditable. (FIR panels already show all T3s in full.)

## Implementation surface

- `index.html` only. No server, schema, pipeline, or service-worker change.
- New: the override store (`_tierRead/_tierWrite/_tierPrune`), `_applyTierOverrides` + `_tierIndex`,
  `_tierNextState`/`_cycleTier`/`_tierApply`, the toast, the re-sort chip, `_refreshFirMarkers`,
  `_refreshChipGlow`.
- Modified: `notamRow` (asterisk, `auto Tn`, `data-tier-key`), the delegated click listener,
  `buildFlightPanel` + `_updateFlightBadge` (§9), the three fetch handlers (apply pass),
  `openPanel`/`openFlightPanel`/`closePanel` (track the open panel; reset staleness),
  `_renderHiraBrief` (§7 note), `_downloadBundle` (§8 confirm).

## Consequences

- The engine's rating is never lost — `tier_auto` is kept on every NOTAM and shown on any
  overridden row.
- Overrides are invisible to the server, so nothing downstream of the pipeline (HIRA, the digest,
  the bundle) can see them. §7 and §8 are the two places that shows.
- Two tabs on one run converge on reload, not live, unless a `storage` listener is added later.
- The feature is entirely client-side JS in a file with no build step and no JS test harness, so
  it is verified by checklist (below) rather than by CI.

## Verification

Manual, against a served briefing — matching how `wx_tier` and the Source Pane are already
verified. There is no JS test harness and this feature adds none (see Consequences).

**Run on the TG970 run (`runs/692e5848…`, single leg), server on :5002, Chrome:**

1. **Cycle** — an engine-T1 badge cycles `T1 → T2* → T3* → T1 (auto) → T2*`, storage tracking
   each step; an engine-T3 badge cycles `T3 → T1* → T2* → T3`, i.e. the redundant state is
   skipped. The badge tap does **not** open the Source Pane (`_srcPane.open` stays false) and the
   row does not move.
2. **Persistence** — override, reload: the value is re-applied from `localStorage` and the panel
   now renders it in the re-sorted position.
3. **UNDO** — the toast restores the previous state, including back to Auto, and decrements the
   re-sort chip.
4. **Re-sort chip** — appears only while stale, disappears on tap, and the Source Pane's row
   selection (`activeKey`, `selectedRowEl`) survives the re-render.
5. **§9 both directions** (synthetic section, since this run has no filtered-out NOTAMs) —
   promoting a filtered-out NOTAM moves it into the visible list; cycling back to Auto returns it
   to the audit list with no reload; a downgraded engine-T1 renders as `T3*` outside the
   "show N more" block.
6. **Derived indicators** — a FIR whose highlight rests on a single T2 goes `#c8a800 → #1a1a2e`
   on downgrade and back on undo; runway-chip glow follows the same way.
7. **Prune** — 25 seeded `preflight.tiers.*` keys reduce to 20, current run kept.
8. **Storage failure** — with `setItem` throwing, the override still applies in memory, the badge
   still repaints, and nothing throws.
9. **Bundle** — `bundle_builder.build()` still produces a ~21 MB bundle from the edited
   `index.html` (the `_SCRIPT_ANCHOR` is untouched).

**Run on a fresh TG628/TG629 upload (2 legs, one group), two tabs on the same run:**

10. **Multi-leg flatten** — `VTBS|THA 00064/25` exists in both legs; `_tierIndex` holds 2 copies
    and one tap moves both (`[1,1] → [3,3]`, and back on undo). Confirmed through the real UI
    click as well. *Not covered by this fixture:* a NOTAM whose `_effective_tier` actually
    **differs** between legs — the ~4 h turnaround puts both ETAs inside the same daily windows,
    so no such NOTAM exists here.
11. **Two tabs, no clobber** — tab 2 loaded *before* tab 1's write, so its in-memory map knew
    nothing of tab 1's entry; after tab 2 wrote a second key, storage held **both**, and both
    re-applied on reload in tab 1. This is the read-modify-write path end to end.
12. **Redundant write normalised** — writing an override equal to `tier_auto` stores Auto, so a
    no-op entry can never inflate the "N manual tier change(s)" warnings. `_tierCount()` reads
    storage rather than this tab's applied map, so it counts the *run's* overrides, not this
    tab's.

**Not yet run:**

- **Offline**: kill the server per CLAUDE.md, reload, override, reload again.
- **The download confirm** with overrides present, on the real Bundle button.
- **Two map tabs from a 3–4 leg upload** specifically (verified with two tabs on one group, which
  exercises the same shared key — group does not enter it).

## References

- ADR 0001 — page images + parse-time anchors (the `"<owner>|<id>"` key)
- ADR 0002 — two-document Source Pane (Selection Sync's tier-sorted assumption)
- ADR 0003 — offline briefing (run-scoped URLs, `/api/*` network-only, the no-fork rule)
- `docs/KNOWN_ISSUES.md` #7 (HIRA switched off), #8 (accepted limitations)
