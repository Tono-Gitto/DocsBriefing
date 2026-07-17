"""
MET Source Pane support: locate each airport's MET Block in the original MET PDF
(for the click-to-highlight split view) and provide word-level geometry for the
ETA-window highlights inside each block (see CONTEXT.md — "MET Block",
"ETA-Window Highlight").

Position-aware companion to met_engine.parse_met_pdf() — reuses that module's
header/group regexes so anchors agree with the parsed airports, but never
modifies or imports from parse_met_pdf()'s call path.

Never raises on malformed content: an airport whose FT text can't be
reconstructed with word-level fidelity still gets a block anchor, just no
"groups" entry — a missing ETA-window fill is a graceful miss, a misplaced
one would be a trust failure (see docs/adr/0001, docs/adr/0002).
"""

import pdfplumber

from met_engine import _GROUP_RE, _HEADER_RE, _PAGE_HDR_RE
from notam_anchors import _lines_to_rects

_Y_PAD_FRAC = 0.005  # matches notam_anchors._Y_PAD_FRAC — same visual padding


def _words_to_group_rects(word_hits, page_sizes):
    """word_hits: [(page_idx, x0, x1, top, bottom, line_key), ...] for the words
    overlapping one group's span. Returns one rect per (page, physical line) touched
    — text-selection style, unlike the whole-block rect which merges a page's lines."""
    by_line = {}
    order = []
    for page_idx, x0, x1, top, bottom, line_key in word_hits:
        key = (page_idx, line_key)
        if key not in by_line:
            by_line[key] = []
            order.append(key)
        by_line[key].append((x0, x1, top, bottom))

    rects = []
    for page_idx, line_key in order:
        pw, ph = page_sizes[page_idx]
        entries = by_line[(page_idx, line_key)]
        xs = [e[0] for e in entries]
        xe = [e[1] for e in entries]
        tops = [e[2] for e in entries]
        bots = [e[3] for e in entries]
        pad = _Y_PAD_FRAC * ph
        y0 = max(0.0, min(tops) - pad)
        y1 = min(ph, max(bots) + pad)
        rects.append({
            "page": page_idx + 1,
            "x0": round(min(xs) / pw, 4),
            "y0": round(y0 / ph, 4),
            "x1": round(max(xe) / pw, 4),
            "y1": round(y1 / ph, 4),
        })
    return rects


def _word_rect(page_idx, x0, x1, top, bottom, page_sizes):
    """Normalized rect for a single word — same vertical padding as block/group
    rects, but never merged across words (a word never crosses lines)."""
    pw, ph = page_sizes[page_idx]
    pad = _Y_PAD_FRAC * ph
    y0 = max(0.0, top - pad)
    y1 = min(ph, bottom + pad)
    return {
        "page": page_idx + 1,
        "x0": round(x0 / pw, 4),
        "y0": round(y0 / ph, 4),
        "x1": round(x1 / pw, 4),
        "y1": round(y1 / ph, 4),
    }


def extract_anchors(pdf_path):
    """Position-aware pass over the MET PDF.

    Returns:
      anchors:    {icao: {"block": [rect, ...],
                           "groups": {"<src_start>": [rect, ...]},
                           "words": [[start, end, rect], ...]}}
      page_sizes: [ (width_pt, height_pt), ... ]                  per page, PDF points
    """
    anchors = {}
    page_sizes = []

    cur_icao = None
    cur_block_lines = []      # (page_idx, x0, x1, top, bottom) — whole block
    ft_lines = []              # (page_idx, line_dict) — FT physical lines only
    ft_capturing = False

    word_cache = {}

    def words_for_page(pages, page_idx):
        if page_idx not in word_cache:
            word_cache[page_idx] = pages[page_idx].extract_words(
                use_text_flow=False, keep_blank_chars=False
            )
        return word_cache[page_idx]

    def flush(pages):
        nonlocal cur_icao, cur_block_lines, ft_lines, ft_capturing
        if cur_icao and cur_block_lines and cur_icao not in anchors:  # first occurrence wins
            entry = {"block": _lines_to_rects(cur_block_lines, page_sizes)}
            result = _extract_group_rects(ft_lines, page_sizes, words_for_page, pages)
            if result is not None:
                groups, words = result
                entry["groups"] = groups
                entry["words"] = words
            anchors[cur_icao] = entry
        cur_icao = None
        cur_block_lines = []
        ft_lines = []
        ft_capturing = False

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        for page_idx, page in enumerate(pages):
            page_sizes.append((page.width, page.height))
            for line in page.extract_text_lines() or []:
                text = (line.get("text") or "").strip()
                if not text or _PAGE_HDR_RE.match(text):
                    continue

                m = _HEADER_RE.match(text)
                if m:
                    flush(pages)
                    cur_icao = m.group(1)
                    continue

                if cur_icao is None:
                    continue

                cur_block_lines.append(
                    (page_idx, line["x0"], line["x1"], line["top"], line["bottom"])
                )

                if not ft_capturing and text.startswith("FT "):
                    ft_capturing = True
                if ft_capturing:
                    ft_lines.append((page_idx, line))
                    if "=" in text:
                        ft_capturing = False

        flush(pages)

    return anchors, page_sizes


def _extract_group_rects(ft_lines, page_sizes, words_for_page, pages):
    """Reconstruct FT text two ways — from line text (ground truth, matches
    parse_met_pdf's own reconstruction) and from words (carries geometry) — and
    only trust word geometry if both reconstructions agree character-for-character.
    Returns ({src_start: [rect, ...]}, [[start, end, rect], ...]) or None if the
    fidelity gate failed. The word list gives the Source Pane per-word geometry
    for taf_base_src (met_engine.py) — same offsets as taf_raw by construction,
    since word_recon is validated character-identical to it."""
    if not ft_lines:
        return None

    # Ground truth: join each physical line's text with a single space, trim at "=".
    text_recon = " ".join(ln["text"].strip() for _, ln in ft_lines).strip()
    if "=" in text_recon:
        text_recon = text_recon[: text_recon.index("=")].rstrip()

    # Word-level reconstruction, tracking each word's offset + geometry.
    parts = []
    offset_map = []  # (start, end, x0, x1, top, bottom, page_idx, line_key)
    pos = 0
    for page_idx, ln in ft_lines:
        words = [
            w for w in words_for_page(pages, page_idx)
            if ln["top"] - 1 <= w["top"] <= ln["bottom"] + 1
        ]
        words.sort(key=lambda w: w["x0"])
        line_key = (ln["top"], ln["bottom"])
        for w in words:
            wt = w["text"]
            parts.append(wt)
            offset_map.append((pos, pos + len(wt), w["x0"], w["x1"], w["top"], w["bottom"],
                                page_idx, line_key))
            pos += len(wt)
            parts.append(" ")
            pos += 1

    word_recon = "".join(parts).rstrip()
    if "=" in word_recon:
        word_recon = word_recon[: word_recon.index("=")].rstrip()

    if word_recon != text_recon:
        return None  # fidelity gate failed — block anchor only, no group fills

    groups = {}
    matches = list(_GROUP_RE.finditer(word_recon))
    for i, gm in enumerate(matches):
        start = gm.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(word_recon)
        hits = [
            (page_idx, x0, x1, top, bottom, line_key)
            for (ws, we, x0, x1, top, bottom, page_idx, line_key) in offset_map
            if we > start and ws < end
        ]
        if hits:
            groups[str(start)] = _words_to_group_rects(hits, page_sizes)

    # Per-word geometry, clipped to word_recon's valid (post "=" trim) length so
    # offsets line up 1:1 with met_engine's own tokenization of taf_raw.
    valid_len = len(word_recon)
    words = []
    for (ws, we, x0, x1, top, bottom, page_idx, line_key) in offset_map:
        if ws >= valid_len:
            continue
        words.append([ws, min(we, valid_len), _word_rect(page_idx, x0, x1, top, bottom, page_sizes)])
    words.sort(key=lambda w: w[0])

    return groups, words
