"""
Source Pane support: locate each NOTAM's block in the original PDF (for the
click-to-highlight split view) and render the PDF's pages as images.

Position-aware companion to notam_engine.parse_notam_pdf() — reuses that
module's section/ID regexes so anchor boundaries agree with parsed NOTAMs,
but never modifies or imports from parse_notam_pdf()'s call path.

Never raises on malformed content: an ID the walker can't cleanly bound is
simply absent from the returned dict (see CONTEXT.md — "Anchor").
"""

import os

import pdfplumber

from notam_engine import (
    _AP_HDR_RE,
    _FIR_HDR_RE,
    _GENERAL_SECTIONS,
    _MAIN_SECT_RE,
    _NOTAM_ID_RE,
    _PAGE_HDR_RE,
)

_Y_PAD_FRAC = 0.005  # ~0.5% of page height, so the box doesn't kiss the glyphs


def _owner_for(section, current_ap, current_fir):
    if section in ("AERODROME", "ADDITIONAL"):
        return current_ap
    if section == "ENROUTE":
        return current_fir
    if section in _GENERAL_SECTIONS:
        return section
    return None


def _lines_to_rects(block_lines, page_sizes):
    """block_lines: [(page_idx0, x0, x1, top, bottom), ...] → one rect per page touched."""
    by_page = {}
    order = []
    for page_idx, x0, x1, top, bottom in block_lines:
        if page_idx not in by_page:
            by_page[page_idx] = []
            order.append(page_idx)
        by_page[page_idx].append((x0, x1, top, bottom))

    rects = []
    for page_idx in order:
        pw, ph = page_sizes[page_idx]
        xs = [x0 for x0, x1, top, bottom in by_page[page_idx]]
        xe = [x1 for x0, x1, top, bottom in by_page[page_idx]]
        tops = [top for x0, x1, top, bottom in by_page[page_idx]]
        bots = [bottom for x0, x1, top, bottom in by_page[page_idx]]
        pad = _Y_PAD_FRAC * ph
        y0 = max(0.0, min(tops) - pad)
        y1 = min(ph, max(bots) + pad)
        rects.append({
            "page": page_idx + 1,  # 1-based, matches notam_page_NNN.png
            "x0": round(min(xs) / pw, 4),
            "y0": round(y0 / ph, 4),
            "x1": round(max(xe) / pw, 4),
            "y1": round(y1 / ph, 4),
        })
    return rects


def extract_anchors(pdf_path):
    """Position-aware pass over the NOTAM PDF.

    Returns:
      anchors:    {anchor_key: [ {page, x0, y0, x1, y1}, ... ]}   1+ rects (page-break split)
      page_sizes: [ (width_pt, height_pt), ... ]                  per page, PDF points
    """
    anchors = {}
    page_sizes = []

    current_section = ""
    current_ap = None
    current_fir = None

    cur_key = None
    cur_lines = []

    def flush():
        nonlocal cur_key, cur_lines
        if cur_key and cur_lines and cur_key not in anchors:  # first occurrence wins
            anchors[cur_key] = _lines_to_rects(cur_lines, page_sizes)
        cur_key = None
        cur_lines = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            page_sizes.append((page.width, page.height))
            for line in page.extract_text_lines() or []:
                text = (line.get("text") or "").strip()
                if not text or _PAGE_HDR_RE.match(text):
                    continue

                m_sect = _MAIN_SECT_RE.match(text)
                if m_sect:
                    flush()
                    current_section = m_sect.group(1)
                    current_ap = None
                    current_fir = None
                    continue

                if current_section == "ENROUTE":
                    m = _FIR_HDR_RE.match(text)
                    if m:
                        flush()
                        current_fir = m.group(1)
                        continue
                elif current_section in ("AERODROME", "ADDITIONAL"):
                    m = _AP_HDR_RE.match(text)
                    if m:
                        flush()
                        current_ap = m.group(1)
                        continue

                m_id = _NOTAM_ID_RE.match(text)
                if m_id:
                    flush()
                    owner = _owner_for(current_section, current_ap, current_fir)
                    cur_key = f"{owner}|{m_id.group(1).strip()}" if owner else None

                if cur_key is not None:
                    cur_lines.append((page_idx, line["x0"], line["x1"], line["top"], line["bottom"]))

        flush()

    return anchors, page_sizes


def render_pages(pdf_path, out_dir, resolution=144):
    """Render every page of pdf_path to out_dir/notam_page_NNN.png (1-based, zero-padded 3).
    Returns the page count."""
    os.makedirs(out_dir, exist_ok=True)
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            img = page.to_image(resolution=resolution)
            img.save(os.path.join(out_dir, f"notam_page_{i:03d}.png"))
        return len(pdf.pages)
