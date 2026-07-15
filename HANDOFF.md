# HANDOFF — Split-View Source-Document Pane ("Source Pane")

**Branch:** `redesign/split-view-source-pane` (never merge to `main` without review).
**Design mockup:** `request/IMG_0990.jpeg` — iPad landscape, original NOTAM document on the
left half, existing briefing app on the right half, blue rounded rectangle highlighting one
NOTAM block in the document.

Read `CLAUDE.md` first. This document assumes its vocabulary (pipeline steps, `runs/<uuid>/<g>/`
layout, `notamRow`, fixtures). New domain terms are defined in `CONTEXT.md`; the rendering
decision is recorded in `docs/adr/0001-page-images-and-parse-time-anchors.md`.

---

## 1. Feature summary

Pilots want to cross-check the app's AI-summarised NOTAMs against the original dispatch
document — the raw text is the legal source of truth. Add a collapsible **Source Pane** to
`index.html` that displays the original NOTAM PDF as page images. Tapping any NOTAM row in
the app opens the pane, scrolls to the right page, and draws a blue rectangle (the **Source
Highlight**) around that NOTAM's block — ID line + validity line + body.

Decisions already made with the product owner (do not relitigate):

| # | Decision |
|---|----------|
| D1 | Source Pane is an **optional mode**, not a replacement UI. Persistent header toggle button; auto-opens the first time a NOTAM row is tapped; open/closed state remembered in-page (no cross-reload persistence). |
| D2 | Pane shows the **NOTAM PDF only**. Build the viewer generically (a document + a set of anchors) so MET/OFP could be added later, but do not add them now. |
| D3 | Rendering = **server-side page PNGs + parse-time anchors** (no PDF.js, no HTML re-render). See ADR 0001. |
| D4 | **All four NOTAM surfaces link to source:** airport panels, FIR panels, FLIGHT panel sections, and the filtered-out audit rows. |
| D5 | **The whole NOTAM row is the tap target.** A small passive blue dot marks rows that have an anchor. Exception: clicks on the `.notam-clickable` body keep the existing summary↔body toggle and do not anchor. |
| D6 | Narrow viewports (phone / iPad portrait): **vertical split** — document top half, app bottom half. |

---

## 2. Verified constraints (why the design is shaped this way)

- `app.py:719` — `uploads/<uuid>/` (including `notam.pdf`) is deleted in the pipeline's
  `finally` block. Everything the Source Pane needs must be written into
  `runs/<run_id>/<g>/` **during** the pipeline.
- `_utils.py:23` `clean_pdf_lines()` flattens the PDF to stripped text lines — page numbers
  and coordinates are discarded before `parse_notam_pdf()` (`notam_engine.py:382`) ever sees
  them. Anchors therefore come from a **separate position-aware pass** over the PDF.
  **Do not modify `parse_notam_pdf()`** — it is battle-tested and fixture-validated.
- `notamRow()` (`index.html:613`) is the single shared row renderer for all four surfaces —
  one integration point covers D4.
- `/data/<int:group>/<filename>` (`app.py:758`) serves **flat filenames only** from the
  group dir — name page images flat (`notam_page_001.png`), no subdirectories.
- Both groups of a 3–4-leg run share **one** NOTAM PDF. Render pages once, hard-link
  (`os.link`, fall back to `shutil.copy` on cross-device failure) into each group dir.
- pdfplumber coordinates are PDF points (72/inch); PNGs are rendered at a different scale.
  Store anchor rects as **normalized 0–1 fractions** of page width/height so the client is
  resolution-independent.
- Offline cockpit rule: no CDN/external resources — everything served from the app
  (this is why PDF.js was rejected; vendored-Leaflet precedent in `CLAUDE.md`).

---

## 3. Backend

### 3.1 New module: `notam_anchors.py`

No project imports except `notam_engine` (for its regexes) and `_utils` if needed. Two
public functions:

```python
def extract_anchors(pdf_path):
    """Position-aware pass over the NOTAM PDF.

    Returns:
      anchors:    {anchor_key: [ {page, x0, y0, x1, y1}, ... ]}   # 1+ rects (page-break split)
      page_sizes: [ (width_pt, height_pt), ... ]                   # per page, PDF points
    """

def render_pages(pdf_path, out_dir, resolution=144):
    """Render every page to out_dir/notam_page_NNN.png (1-based, zero-padded 3).
    Returns page count. Uses pdfplumber page.to_image(resolution=144) → pypdfium2."""
```

**Anchor extraction algorithm:**

1. Walk pages with `pdfplumber`; use `page.extract_text_lines()` — each entry gives
   `text`, `top`, `bottom`, `x0`, `x1` in points.
2. Track parsing context with the **same regexes as the parser — import them** from
   `notam_engine`, do not duplicate: `_MAIN_SECT_RE` (section context: GENERAL / FLIGHT LEG /
   AERODROME / ENROUTE / ADDITIONAL / AEROPLANE), `_AP_HDR_RE` (airport sub-header →
   owner = ICAO), `_FIR_HDR_RE` (FIR sub-header → owner = FIR code), `_NOTAM_ID_RE`
   (NOTAM ID line — capture group 1 is the ID string, **identical** to the `id` field the
   parser emits, e.g. `RJAAB1685/26`, `THA 00159/26`). Skip lines matching `_PAGE_HDR_RE`
   (page-header noise) when detecting block boundaries, but they still bound nothing —
   ignore them entirely.
3. A NOTAM **block** starts at its ID line and ends at the line above the next ID line,
   sub-header, or main section header. Convert the block to rects:
   - one rect per page the block touches (a block crossing a page break emits two rects);
   - rect = `x0 = min(line.x0)`, `x1 = max(line.x1)`, `y0 = first line top`,
     `y1 = last line bottom`, each **normalized** by that page's width/height;
   - add small padding (~0.5% of page height) so the box doesn't kiss the glyphs.
4. **`anchor_key = f"{owner}|{id}"`** where owner is:
   - airport ICAO for AERODROME and ADDITIONAL sections (both key by ICAO — matches how
     `parse_notam_pdf` merges them into `notam_db`);
   - FIR code for ENROUTE;
   - the section key (`GENERAL`, `FLIGHT LEG`, `AEROPLANE`) for the general sections.
   If the same key occurs twice (e.g. same NOTAM under AERODROME and ADDITIONAL for one
   airport), **first occurrence wins** — do not merge rects from different occurrences.
5. Never raise on malformed content: an ID the walker can't bound cleanly is simply absent
   from the dict. Missing anchor ⇒ the row gets no dot and no tap action (graceful miss).

**Output artifact — `notam_anchors.json`** (written per group):

```json
{
  "pages": 52,
  "page_sizes": [[595.3, 841.9], ...],
  "anchors": {
    "RJBB|RJAAB1685/26": [{"page": 4, "x0": 0.08, "y0": 0.61, "x1": 0.72, "y1": 0.66}],
    "VTBB|THA 00097/26": [ ... ]
  }
}
```

`page` is 1-based (matches `notam_page_NNN.png`). `page_sizes` lets the client set CSS
`aspect-ratio` on page placeholders before images load (no scroll jumping).

### 3.2 Pipeline hook (`app.py`)

Add a step next to `_run_notam_step_multi()` (`app.py:421` call site `app.py:615`):

- Once per **run** (not per group — the PDF is shared): `render_pages()` into the first
  group dir, `extract_anchors()`, then hard-link every PNG and write `notam_anchors.json`
  into each additional group dir.
- **Best-effort:** wrap the whole step in try/except; on failure append a progress warning
  (e.g. `⚠ source-document rendering failed — source pane unavailable`) and continue.
  A briefing must never fail because provenance rendering broke.
- Emit a progress log line for the step (match existing step-log style).
- Do **not** touch the CLI fixture scripts or the monkey-patched module globals.

### 3.3 Dependencies

Add `pypdfium2` to `requirements.txt` (pdfplumber 0.11.x uses it for `page.to_image`;
already importable in the dev venv, but Railway builds from `requirements.txt`). Mirror in
`requirements-app.txt`.

---

## 4. Frontend (`index.html` — single file, no build step)

### 4.1 Layout

- Wrap the existing `#map` + `#panel` region in a flex container with a new sibling
  `#srcPane` (pane first in DOM = left/top). 50/50 split per the mockup.
- Wide viewports: `flex-direction: row` (pane left). Narrow (`@media (max-width: 900px)`,
  and/or portrait orientation): `flex-direction: column` (pane top half, app bottom half —
  D6).
- Pane contents: a slim header bar (`NOTAM — ORIGINAL`, close ✕) above a scrollable page
  stack. Each page = a wrapper `div` with `position:relative`, CSS `aspect-ratio` from
  `page_sizes`, containing `<img src="/data/<GROUP>/notam_page_NNN.png" loading="lazy">`
  at `width:100%`. Highlight divs are absolutely positioned children of the wrapper using
  the normalized rect × 100%: `left: x0*100%; top: y0*100%; width: (x1-x0)*100%; ...`.
- When the pane opens/closes/resizes, call `map.invalidateSize()` (existing pattern in
  `openPanel`/`closePanel`).
- Zoom: v1 = fit-width + browser-native pinch zoom. +/− buttons are a stretch goal only.

### 4.2 Header toggle

New `DOC` button beside `FLIGHT` in `#header-actions` (`index.html:379`). On load, fetch
`DATA("notam_anchors.json")`; on 404 (legacy `data/` demo, old runs, best-effort failure)
hide the button and disable all Source Pane behavior — the feature is invisible, nothing
breaks.

### 4.3 Row wiring

- `notamRow(n, legInfo, anchorKey)` — add third param, threaded from all four callers:
  - `buildPanel` (`index.html:649`): `anchorKey = ap.icao + "|" + n.id`
  - `buildFirPanel` (`index.html:757`): `f.fir + "|" + n.id`
  - `buildFlightPanel` (`index.html:836`): `sectionKey + "|" + n.id` — **including the
    filtered-out audit rows** (D4).
- If the key exists in the loaded anchors, render `data-anchor-key="<esc'd key>"` on the
  `.notam-row` and a small passive blue dot indicator (~8–10 px, `#4a7cf0`) at the row's
  right edge. No key / no match ⇒ no attribute, no dot, no action.
- **One delegated document-level click listener** on `.notam-row[data-anchor-key]` —
  follow the runway-chip pattern (`data-*` attributes + delegation). **Never interpolate
  PDF-derived NOTAM ids into inline `onclick` JS** (existing quote-injection rule; `esc()`
  escapes `& < > " '`). Clicks originating inside `.notam-clickable` must keep the existing
  summary↔body toggle and not trigger anchoring (check `event.target.closest()` order or
  stopPropagation in `toggleNotam`'s handler — D5).

### 4.4 Anchor action (tap flow)

1. Look up rects for the row's `data-anchor-key`.
2. Open the pane if closed (auto-open on first use — D1; afterwards respect the user's
   manual toggle state). Build the page stack lazily on first open.
3. Remove any previous highlight divs and `.src-selected` row style (one highlight at a
   time). Add `.src-selected` to the tapped row.
4. Insert highlight div(s): blue rounded rect per mockup — ~3px solid `#4a7cf0`,
   ~6px border-radius, subtle outer glow, transparent fill. Multi-rect anchors (page-break)
   get one div per rect.
5. Smooth-scroll the pane so the first rect is vertically centered
   (`scrollIntoView({behavior:"smooth", block:"center"})` on the highlight div).
6. Highlight persists until the next tap or pane close.

### 4.5 State

Module-level `_srcPane = { open: false, loaded: false, activeKey: null, anchors: null }` —
matches existing `_tafRaw` / `_notamBody` conventions. No `localStorage`.

---

## 5. Verification (do all of it)

**Unit tests — `tests/test_notam_anchors.py`** (pure functions, no API key):
- `extract_anchors(Input/TG415_NOTAM.pdf)`: known NOTAM ids resolve to expected pages
  (pick ≥3 ids by inspecting the PDF once and hard-coding expectations, like the MET
  regression table).
- Every rect satisfies `0 ≤ x0 < x1 ≤ 1` and `0 ≤ y0 < y1 ≤ 1`.
- A page-break-crossing NOTAM yields ≥2 rects (find one in TG934's 50+-page PDF).
- Anchor ids exactly match the `id` fields in `parse_notam_pdf()` output for the same PDF
  (set-intersection should cover ~all parsed NOTAMs; report the miss rate — expect ≥95%).
- Malformed/absent ids: no exception, key simply missing.

**Regression:** full `python3 -m pytest tests/ -q` — `parse_notam_pdf` output must be
byte-identical (anchors are additive); the CLI fixture check (`parse_ofp.py` +
`met_engine.py` table in `CLAUDE.md`) must still pass.

**End-to-end (Flask, per fixture):** run TG415/TG416 (multi-leg shared NOTAM), TG628/TG629
(FIR + VLVT airport/FIR collision), TG934 (long doc). For each: tap NOTAM rows on **all four
surfaces**; confirm the pane opens, scrolls to the right page, and the blue box encloses the
correct NOTAM block — spot-check ≥5 NOTAMs per fixture against the real PDF. Check group 2
of a 3–4-leg run serves images (hard-links). Check narrow viewport (phone width / iPad
portrait) gives the vertical split. Check legacy demo (`/data/<file>` fallback) hides the
DOC button. Check a run where rendering was forced to fail (e.g. temporarily rename
`pypdfium2`) still completes with the warning.

---

## 6. Out of scope — do not build

- MET/OFP documents in the pane (D2), PDF.js, text search inside the pane, cross-reload
  persistence of pane state, draggable split divider, zoom buttons (stretch only),
  highlight-to-app reverse navigation (PDF → row).
