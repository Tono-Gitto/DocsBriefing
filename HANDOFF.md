# HANDOFF — iPad split-view UX: enlarge detail panel, speed up map fly animation

## Context

Pilots use this app on an iPad in **split-screen**: the briefing map gets the left
half (~680 CSS px wide), GoodReader (official documents) the right half. See
`request/IMG_0988.PNG` for the real-world layout. Two problems in that mode:

1. **Detail panel too small.** `#panel` is a fixed 380 px column, and its typography
   runs 0.65–0.85 rem (≈10–13 px) — below comfortable iPad reading size. In split
   view the panel is the primary reading surface; the map is secondary.
2. **Map animation too slow.** The Next/Previous airport buttons and the header
   airport shortcut buttons call `map.flyTo()` with **no duration option**, so
   Leaflet computes duration from distance — skipping between airports hundreds of
   nm apart takes several seconds per tap.

Design decisions already made with the user (do not re-litigate):
- Split view: panel takes **~75 % of the viewport with a live map sliver** remaining
  (not a full-width overlay).
- Animation: **quick fly, fixed ~0.75 s** (not an instant jump).

## Scope

- **Only file to modify: `index.html`** (served by Flask at `GET /map`).
- **Do NOT touch `mvp/index.html`** — it is a frozen snapshot.
- No backend / pipeline / JSON schema changes.

## Change 1 — Responsive panel width via one CSS custom property

The 380 px width is hard-coupled in three places. Replace all of them with a
custom property so they can never drift apart:

```css
:root { --panel-w: 440px; }                      /* full-screen / desktop: 380 → 440 */
@media (max-width: 900px) {
  :root { --panel-w: min(75vw, 560px); }         /* split view & narrow: ~510px at 680px viewport */
}
```

Update the three coupled rules (current line numbers as of commit `b8c5be4`):

| Rule | Now (index.html) | Change to |
|---|---|---|
| `body.panel-open #map` (line 33) | `right: 380px` | `right: var(--panel-w)` |
| `#panel` (lines 38–41) | `right: -380px; width: 380px` | `right: calc(-1 * var(--panel-w)); width: var(--panel-w)` |
| `body.panel-open #status` (line 273) | `right: 396px` | `right: calc(var(--panel-w) + 16px)` |

Notes:
- Keep the existing `transition: right 0.25s ease` on `#map` and `#panel` — the
  slide-in feel is fine.
- No JS needed for resizes: Leaflet's default `trackResize` handles window/split
  resizing, and `openPanel()`/`closePanel()` already call `map.invalidateSize()`.
- At 680 px viewport: panel ≈ 510 px, map sliver ≈ 170 px (still tappable for
  nearby markers). Breakpoint 900 px also covers iPad portrait halves.

## Change 2 — Typography & spacing scale-up inside the panel

Bump the panel type scale globally (not only in the media query — the sizes are
small even full-screen). Target: no body text below ~12.5 px effective. All in the
`<style>` block of `index.html`:

| Class | Now | New |
|---|---|---|
| `.panel-icao` | 1.4rem | 1.55rem |
| `.panel-name` | 0.9rem | 1rem |
| `.panel-ref` | 0.82rem | 0.9rem |
| `.panel-label` | 0.7rem | 0.8rem |
| `.panel-value` | 0.85rem | 1rem |
| `.notam-body` | 0.82rem | 0.95rem |
| `.notam-id` | 0.7rem | 0.8rem |
| `.notam-window` | 0.68rem | 0.78rem |
| `.badge` (incl. tier badges) | 0.65rem | 0.75rem |
| `.badge-window` | 0.72rem | 0.8rem |
| `.rwy-chip` | 0.78rem | 0.88rem |
| `.rwy-len` | 0.72rem | 0.8rem |
| `.taf-expand-block`, `.notam-expand-block` | 0.72rem | 0.82rem |
| `.notam-filter-clear` | 0.68rem | 0.78rem |
| `.no-data` | 0.82rem | 0.9rem |
| `.wx-tag` | 0.72rem | 0.8rem |
| `.leg-chip` | 0.6rem | 0.7rem |
| `.leg-divider-label` | 0.7rem | 0.8rem |
| `.t3-expand` | 0.78rem | 0.88rem |
| `.filtered-expand` | 0.75rem | 0.85rem |
| `.nav-btn` | 0.82rem | 0.95rem |

Spacing/fit adjustments that go with the wider panel:
- `.panel-section` padding `14px 18px` → `16px 20px`; `.panel-head` padding
  `20px 18px 14px` → `22px 20px 16px`; `.notam-row` padding `9px 0` → `11px 0`.
- `.nav-btn` padding `7px 13px` → `10px 16px`; `max-width: 155px` → `max-width: 45%`
  (two buttons + gap must still fit side-by-side in `#panel-nav`).
- Leave `#header`, `.ap-btn`, `#flightBtn`, `#wxLegend`, tooltips and all **map**
  element sizes unchanged — the request is about the details panel. (Optional
  polish, only if trivial: `.ap-btn`/`#flightBtn` font 0.75 → 0.82rem.)

## Change 3 — Fixed-duration map fly

Add one helper near the other map utilities in the `<script>`:

```js
function focusAirport(lat, lon) {
  map.flyTo([lat, lon], Math.max(map.getZoom(), 7),
            { duration: 0.75, easeLinearity: 0.4 });
}
```

Replace both existing call sites with it:
- `navigateTo(idx)` — line 423: `map.flyTo([data.lat, data.lon], Math.max(map.getZoom(), 7));`
- header airport buttons in `renderHeaderAirportBtns` — line 970:
  `btn.onclick = () => { openPanel(ap); map.flyTo(...); };`

Marker click handlers (lines 1036, 1073) intentionally do **not** fly — leave them.

## Verification (end-to-end)

1. `python3 app.py` (needs `.env` with `ANTHROPIC_API_KEY`; without a key the run
   still completes — NOTAM summaries degrade to first-line after retries, which is
   fine for a UI check).
2. Upload the TG921 fixture (`Input/TG921_OFP.pdf`, `_MET`, `_NOTAM`) at
   `http://localhost:5001/upload`, wait for the pipeline, open `/map?g=1`.
   (`/data/1/…` 404s unless a completed run exists in-process — the legacy `data/`
   fallback is not used by the map, so an upload run is required.)
3. **Split-view emulation:** set the browser window/devtools viewport to
   ~**680 × 930**. Check: panel ≈ 75 % width with map sliver visible and tappable;
   all panel text comfortably readable (nothing under ~12 px); runway chips, badges,
   L1/L2 chips, and prev/next buttons don't overflow or wrap badly; `#status` chip
   sits left of the panel, not under it.
4. **Animation:** tap a header airport button, then use Next/Previous across several
   airports including a long hop (e.g. EDDF-area alternates → VTBS side) — each move
   must complete in ~0.75 s, smooth, no distance-dependent slow arcs.
5. **Desktop width** (≥ 1000 px): panel is 440 px, layout intact, close (✕) button
   and TAF expand/collapse still work.
6. Multi-leg spot check with TG415+TG416 fixture (`?g=1`): leg dividers, unified
   NOTAM block, and FIR panels render correctly at the new sizes.

No pytest changes needed — this is HTML/CSS/JS-only; the test suite doesn't cover
`index.html`.
