# ADR 0002 — Two-document Source Pane: fixed stacked split, per-document highlights

Date: 2026-07-16
Status: Accepted

## Context

ADR 0001 established the Source Pane mechanism (server-side page images + parse-time anchors)
for the NOTAM document and anticipated extending it to MET. The MET document is now added: a
pilot tapping any MET-derived element in an airport panel (conditions baseline, BECMG in
progress, TEMPO/PROB overlays) should see the original MET block, while NOTAM
click-to-highlight keeps working. The pane is one fixed-width region (50vw wide / 50vh tall on
narrow screens); two documents must share it.

Layout alternatives considered:

1. **Tabs, auto-switching** — one full-height document at a time; tapping a MET item flips to
   the MET tab. Best per-document readability, but only one document visible at once.
2. **Focus-weighted stacked split** — both visible; the last-tapped document grows to ~70%.
   Best of both, but adds resize-on-tap motion and state logic.
3. **Fixed stacked split** — MET strip on top at a constant 25% of pane height, NOTAM below at
   75%; proportions never change.

Highlight-state alternatives: a single global highlight (today's model extended) vs. one
highlight per document.

MET anchor granularity: whole airport block vs. separate METAR/TAF sub-anchors.

## Decision

**Fixed stacked split (option 3), 25% MET / 75% NOTAM,** with two mitigations instead of
dynamic sizing: each Document Section collapses to its header bar on tap, and a tap on a
briefing item auto-expands its section. On narrow viewports the sections become an accordion
(exactly one open). Chosen for layout predictability — in a cockpit-adjacent trust feature, a
pane that never moves by itself beats one that reflows on every tap; MET blocks are short
(~10 lines), so a fixed strip that auto-scrolls to the highlight is sufficient. Tabs were
rejected because simultaneous visibility is the requirement: cross-checking weather and NOTAM
sources side by side is the reason the MET document joins the pane at all.

**One highlight per document.** A MET highlight and a NOTAM highlight coexist; a new tap
replaces only its own document's highlight. A global last-tap-wins rule would leave the other
document visible but blank, defeating the simultaneous-visibility rationale.

**MET anchors are whole airport blocks** (header line through end of TAF), one per ICAO, all
MET elements tapping to the same block — the anchor key stays a bare ICAO (the MET document
has a flat namespace, unlike NOTAM's owner|id). **Layered inside the block, ETA-window
highlights** additionally fill the raw TAF groups relevant in each leg's ETA±1h window (the
groups the panel shows as BECMG-in-progress / active overlays; the synthesized baseline was
originally never filled — see the Baseline Fills addendum below for how token-level
provenance closed that gap). All legs' fills render simultaneously in
the established leg colors with L1/L2 tags (single-leg airports: untagged, no legend) — the
border-vs-fill distinction separates "the block you tapped" from "what matters at ETA".
Group geometry is computed leg-agnostically at parse time for every TAF group, keyed by the
group's character offset in `taf_raw`; per-leg relevance travels in `airports.json`
(`src_start`), so the anchor file needs no knowledge of legs or reference times. This is
trustworthy only because the anchor pass's reconstructed FT text is char-identical to
`taf_raw` (validated 49/49 on the fixture); where that identity fails, group fills are
omitted for that airport — a missing fill is a graceful miss, a misplaced fill is a trust
failure (ADR 0001's precision rule).

**The inline TAF tap-to-expand (`toggleTaf`) is removed unconditionally.** The conditions
line's tap becomes the MET anchor tap; two behaviors cannot share one target. Where
`met_anchors.json` is absent (legacy runs, demo data, render failure), MET rows are inert —
the same graceful invisibility ADR 0001 chose for missing NOTAM anchors, rather than keeping
a second, rarely-exercised code path as a fallback.

The mechanism itself is unchanged from ADR 0001: a position-aware companion pass
(`met_anchors.py`) imports `met_engine`'s header regexes, pages render once per run, rects are
normalized 0–1, and each document's rendering is independently best-effort so a MET failure
never costs the NOTAM pane (or the briefing).

## Consequences

- Each run additionally stores one PNG per MET page (~7 pages for the fixture — negligible
  next to the ~100-page NOTAM document).
- The Source Pane gains per-section collapse state and a narrow-viewport accordion rule; the
  fixed ratio applies only when both sections are present and expanded (a one-document run
  degrades to a full-height single section).
- `raw TAF` text is no longer readable in the briefing panel itself; provenance moves wholly
  to the source image. `taf_raw` remains in `airports.json` for the map's data contract.
- Any future change to `met_engine._HEADER_RE` / `_PAGE_HDR_RE` / `_GROUP_RE` must keep the
  anchor pass's imports shared, or MET highlights drift (same invariant ADR 0001 states for
  NOTAM regexes).
- `condense_taf()` output additively gains `src_start` on BECMG-in-progress and overlay
  entries; the character offset becomes part of the contract between `met_engine`,
  `met_anchors`, and the map (both sides derive spans from `_GROUP_RE` over identical text).

## Addendum: Selection Sync (2026-07-17)

Manual per-row tapping (above) means browsing several airports in a row costs one tap per
document per airport just to keep the pane current. **Selection Sync** removes that cost:
while the pane is open, any map/header selection (marker, header button, FIR diamond, FLIGHT
button, prev/next nav) drives both documents automatically, on top of the existing tap
mechanism rather than replacing it.

Three decisions kept this additive instead of a redesign:

- **Sync only while the pane is open** — an opt-in mode stays opt-in. The pane never
  auto-opens from a map/header selection; closed-pane behavior is unchanged. This preserved
  the pane's original opt-in rationale (the briefing is complete without it) while still
  removing the per-tap cost once a pilot has chosen to cross-check sources.
- **Severity-first NOTAM target via displayed order, not a recomputation** — the target is
  simply the first `.notam-row[data-anchor-key]` in the freshly-rendered panel's DOM order.
  Panels already render tier-sorted (T1 first), so this is the first anchored row by
  displayed severity with no new sorting/lookup logic, and it works unmodified for FIR panels
  and the FLIGHT panel (GENERAL section renders first) — one rule, zero per-panel-type code.
- **Layout stability rule** — sync never expands, collapses, or accordion-flips a section.
  ADR 0002's original stacked-split rationale ("a pane that never moves by itself beats one
  that reflows on every tap") extends naturally to Selection Sync: a collapsed section's
  highlight is updated silently underneath and is already correct the moment the pilot
  reopens it, rather than force-expanding to show it.

Mechanically, `showSourceAnchor` (manual tap) was split into a layout half
(`openSrcPane()` + `_expandSection()`) and a content half (`_drawDocAnchor`: clear, draw
border + ETA fills, mark selected, scroll). Selection Sync calls only the content half,
directly, per document — no new highlight-drawing code path, so the two entry points stay
guaranteed identical in rendering.

Rationale: click reduction without surrendering half the screen to every marker tap — the
pane keeps ADR 0002's promise of layout predictability even as it becomes more automatic.

## Addendum: Baseline Fills (2026-07-17)

The original decision left `taf_base` — "conditions at ETA" — with no fill at all: it's a
*derived* string (folding may pull the wind from a completed BECMG and vis/cloud from the
original base line), so no single source region corresponds to it. This was a real gap: the
one condition set a pilot cares about most (what's actually happening on arrival) was the one
thing the Source Pane couldn't point at in the source document.

**Token-level provenance, mirroring the existing `src_start` → `groups` mechanism exactly, one
level down.** `met_engine._parse_groups()` now tokenizes the base region and each group's text
(`{"t": token, "s": offset in taf_raw}`), and `condense_taf()`'s fold runs a token-level twin
of `_fold_conditions()` (`_fold_conditions_toks()`) alongside the untouched string fold — same
three branches (full restatement / wind-only with old wind / wind-only with no old wind),
driven by the same `_is_pure_wind_change`/`_leading_wind` checks, so the two can never
silently drift apart. The result, `taf_base_src`, is a token list in display order threaded
onto each leg. `met_anchors.py` emits the geometric counterpart, `words` — one rect per PDF
word, built from the same `offset_map` that already backs `groups`, behind the identical
fidelity gate. The client resolves each token's `[s, s+len(t))` span against `words` by exact
match; a miss draws nothing (the same graceful-miss rule as everywhere else in the Source
Pane), and contiguous-in-source tokens sharing a PDF line merge into one rect so e.g.
`9999 FEW020` reads as a single box.

**Why not make `taf_base` itself a token list end-to-end** (replacing the string) — rejected:
`condense_taf()`'s string output is the CLAUDE.md-validated regression fixture and several
other call sites (`_classify_wx_tier`, the panel display) consume the string directly; keeping
both representations, derived in lockstep rather than one replacing the other, meant the
byte-identical output guarantee needed zero rework and the token version only had to prove
itself equal to it in tests.

**Why the baseline gets a distinct visual treatment** (`.src-fill-base`, solid 2px border vs.
the ETA-window fills' 1px) — the two answer different questions ("what holds at ETA" vs. "what
else is relevant within ETA±1h") and sharing one style would blur that distinction the same
way sharing one highlight color blurs MET vs. NOTAM.

## Addendum: Merged fills for shared timing (2026-07-17)

"All legs' fills render simultaneously in the established leg colors with L1/L2 tags" (above)
had a bug on quick turnarounds: when two legs' ETA windows resolved to the *exact same* raw TAF
group or baseline token (common when nothing changes between the two legs' ETAs — same
`src_start`/token span, same anchors-derived rect), both legs drew a separate, fully opaque,
identically-positioned `<div>`. The later leg (always L2, since legs render in ascending order)
painted directly over the earlier one, so a rect both legs cared about silently looked like it
belonged to L2 alone — the opposite of what the fill is for.

**Merge by exact rect equality, draw one fill with a two-tone frame.** `_drawMergedFills`
groups a category's fill entries by `page + x0 + y0 + x1 + y1` — the literal condition that
caused the bug, and the only one guaranteed exact by construction (a shared `src_start`/token
always resolves to the same shared anchors geometry, never a merely-similar one). A rect used
by more than one leg draws as a single fill: the lowest leg number's color as the solid border,
the next leg's color as an outer `box-shadow` ring, tagged with every contributing leg joined
(`"L1+L2"`) instead of just one. A rect used by only one leg is unaffected. Rejected: merging
by the *semantic* key (`src_start` value / token span) instead of the resolved rect — it would
have required two separate matching implementations (one for `groups` offsets, one for merged
baseline spans) where rect equality needs exactly one, and it doesn't fix anything rect
equality doesn't already fix, since the two keys never disagree given the shared-anchors
invariant above.

**Scoped per fill category, never across them.** Window fills (thin border, ETA±1h) and
baseline fills (thick border, at-ETA) are deliberately styled to mean different things; a
window-fill rect merging with a baseline-fill rect that happens to share coordinates would
erase that distinction. `_drawMetEtaFills` calls `_drawMergedFills` once per category.

**Tag density stays asymmetric across categories, unchanged by the merge.** Window fills tag
every rect in a merged group; baseline fills tag only the group's first rect per leg that was
already first before merging (`taggable`) — this predates the merge and wasn't the bug, so it
was left alone rather than folded into one convention while touching the code anyway.

**Follow-on: tag clipping on left-margin rects.** The wider combined tag (`"L1+L2"`, ~2.5× a
single-leg tag) made a latent issue much more visible: tags anchor to the *left* of their box
(`translate(-100%, …)`), and the MET Document Section's scroll container is `overflow-x: auto`
— content can't scroll to negative x, so a tag pushed left of a rect near the page's left
margin (`x0` near 0) was permanently clipped, not just offscreen-but-reachable. Fixed by
flipping the tag to the box's right edge (`.src-fill-tag-right`) whenever `x0 < 0.05`; the
right side can overflow the visible pane under `overflow-x: auto` but stays *reachable* by
scrolling, which is why only the left side needed the flip.
