# ADR 0001 — Source Pane renders server-side page images with parse-time anchors

Date: 2026-07-15
Status: Accepted

## Context

The Source Pane must display the original NOTAM PDF next to the briefing and draw a precise
highlight rectangle around the NOTAM a pilot taps. Constraints:

- **Offline cockpit rule** — the app must work with no internet; all assets are served from
  the app itself (precedent: Leaflet is vendored, no CDN).
- **No build step** — `index.html` is a single hand-maintained file.
- **Precision** — the highlight must enclose exactly the tapped NOTAM's block; a wrong or
  approximate box is worse than none (trust feature).
- The uploaded PDF is deleted after the pipeline; anything the viewer needs must be
  produced during the pipeline.

Alternatives considered:

1. **PDF.js in the browser** — vendor ~2 MB of PDF.js, serve the retained PDF, locate each
   NOTAM at runtime by searching the text layer.
2. **Server-side page PNGs + parse-time anchors** — pipeline renders each page to an image
   and records each NOTAM's page + bounding box while it already walks the document;
   client shows images and positions highlight divs from normalized rects.
3. **HTML re-render of extracted text** — display the parsed text styled like a document.

## Decision

Option 2: render pages to PNG during the pipeline (pdfplumber `page.to_image` via
pypdfium2) and compute anchors in a position-aware parsing pass, stored as normalized 0–1
rectangles per page.

The parser is the only component that authoritatively knows which lines belong to which
NOTAM; computing anchors at parse time is strictly more reliable than re-discovering them
in the browser with text search (line wrapping, repeated ids, hyphenation). Page images are
pixel-faithful to the paper briefing — what a pilot cross-checking a summary expects — and
need no client library, keeping the no-build, offline constraints intact.

Option 3 was rejected outright: a re-render is no longer the *original* document, which
defeats the provenance purpose. Option 1 was rejected for runtime-matching fragility and a
heavy third-party dependency in a safety-adjacent trust feature.

## Consequences

- Each run stores one PNG per NOTAM-PDF page (~5–20 MB per run; hard-linked across groups);
  `runs/` sweep policy already bounds retention to ~24 h.
- Pipeline gains a rendering step (seconds for a 50-page document); it is best-effort — on
  failure the briefing completes without a Source Pane.
- Anchors are frozen at parse time: any future change to NOTAM section parsing must keep
  the anchor pass's section/ID regexes shared (imported), or highlights drift.
- Adding MET/OFP documents later reuses the same mechanism (document + anchor set); no
  rework expected.
