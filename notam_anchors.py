"""
Source Pane support: locate each NOTAM's block in the original PDF (for the
click-to-highlight split view) and render the PDF's pages as images.

Position-aware companion to notam_engine.parse_notam_pdf() — reuses that
module's section/ID regexes so anchor boundaries agree with parsed NOTAMs,
but never modifies or imports from parse_notam_pdf()'s call path.

Never raises on malformed content: an ID the walker can't cleanly bound is
simply absent from the returned dict (see CONTEXT.md — "Anchor").

A COM-INFO bulletin (see notam_engine._split_com_info_parts) gets its own
precise per-sub-notice anchors ("<owner>|<id> [N]") in addition to the whole
block's anchor, using the shared _partition_at_dash_boundaries boundary rule
so its split lines up with general_notams.json's. If the two independent
PDF-line extraction paths this module and notam_engine each use ever
disagree for some NOTAM, the frontend's key-resolution fallback (index.html
_resolveAnchorKey) degrades that mismatched part back to the whole-block box
rather than leaving it unclickable.
"""

import os
import re

import pdfplumber

from notam_engine import (
    _AP_HDR_RE,
    _COM_INFO_TAG_RE,
    _FIR_HDR_RE,
    _GENERAL_SECTIONS,
    _MAIN_SECT_RE,
    _NOTAM_ID_RE,
    _PAGE_HDR_RE,
    _partition_at_dash_boundaries,
)

_Y_PAD_FRAC = 0.005  # ~0.5% of page height, so the box doesn't kiss the glyphs

# ── Special Security Arrangement (SSA) sub-parsing ────────────────────────────
# One COM-INFO GENERAL part occasionally bundles a "SUBJ: SPECIAL SECURITY
# ARRANGEMENT" bulletin, itself structured as several station-code groups, each
# stating a security LEVEL for those stations. This is flight-independent (it
# doesn't know or care which airport a given upload's dep/dest is) — it just
# extracts every group's code words and level-phrase geometry; index.html
# resolves the flight's own dep/dest IATA codes against this data at render
# time, the same division of labor as met_anchors.py's word-level `words` list.
_SSA_SUBJ_RE = re.compile(r"SPECIAL\s+SECURITY\s+ARRANGEMENT", re.IGNORECASE)
# A station-list line: "...STATION[ EUROPE]: ARN BRU CDG..." — the colon is
# load-bearing (it's what separates the station-list codes from a line like
# "STATION ENGINEER", which names no colon and so never matches).
_SSA_STATION_RE = re.compile(r"\bSTATIONS?\b[A-Z\s]*:\s*(.*)$")
# A continuation line that is nothing but wrapped station codes (e.g. the
# second half of a station list that wrapped to the next physical line).
_SSA_CODES_ONLY_RE = re.compile(r"^(?:[A-Z]{3}\s+)*[A-Z]{3}$")
_SSA_DIVIDER_RE = re.compile(r"^-{3,}$")
_SSA_LEVEL_RE = re.compile(r"^LEVEL:\s*([A-Z]+)")
# The observed catch-all shape ("...STN NOT ON THE LIST ABOVE ARE LOW LEVEL...")
# for stations the notice doesn't name explicitly — level-only, since no
# specific airport code is printed for the client to box.
_SSA_CATCHALL_RE = re.compile(r"STNS?\s+NOT\s+ON\s+THE\s+LIST\s+ABOVE\s+ARE\s+([A-Z]+)\s+LEVEL")


def _is_ssa_notice(texts):
    return any(_SSA_SUBJ_RE.search(t) for t in texts)


def _parse_ssa_structure(items, text_of):
    """Strictly local state machine over a NOTAM part's line-items (opaque
    payload; text_of(item) -> str). Matches ONLY the shape:

        <station line: "...STATION...: CODE CODE ...">
        [<codes-only continuation line(s)>]
        <divider: "---...">
        <LEVEL: WORD ...>

    — never a looser "does this paragraph mention a station" guess. Anything
    that doesn't fit this exact shape (admin preamble, an "applies to all
    routes" paragraph, a mid-body line that happens to start with two
    capital letters) is simply skipped, not force-matched.

    Returns (groups, catch_all):
      groups    = [{"code_items": [item, ...], "level": str, "level_item": item}, ...]
      catch_all = {"level": str, "item": item} | None
    """
    groups = []
    catch_all = None
    i, n = 0, len(items)
    while i < n:
        text = text_of(items[i]).strip()

        if catch_all is None:
            m_catch = _SSA_CATCHALL_RE.search(text)
            if m_catch:
                catch_all = {"level": m_catch.group(1), "item": items[i]}
                i += 1
                continue

        m_station = _SSA_STATION_RE.search(text)
        if not m_station:
            i += 1
            continue

        code_items = [items[i]]
        i += 1
        while i < n and _SSA_CODES_ONLY_RE.match(text_of(items[i]).strip()):
            code_items.append(items[i])
            i += 1

        if i >= n or not _SSA_DIVIDER_RE.match(text_of(items[i]).strip()):
            continue  # not the expected shape — don't guess, just move on
        i += 1
        if i >= n:
            continue

        m_level = _SSA_LEVEL_RE.match(text_of(items[i]).strip())
        if not m_level:
            continue

        groups.append({"code_items": code_items, "level": m_level.group(1), "level_item": items[i]})
        i += 1

    return groups, catch_all


def _phrase_rect(page_idx, words_on_line, target_text, page_sizes):
    """Find a run of words on one PDF line whose concatenated (whitespace-
    stripped) text matches target_text, trying every possible starting word —
    the target phrase ("LEVEL: LOW", or "LOW LEVEL" for the catch-all) isn't
    always the first thing on the line. Returns a rect list (via
    _lines_to_rects, so page-consistent with every other anchor) or None."""
    target = re.sub(r"\s+", "", target_text).upper()
    if not target:
        return None
    words_sorted = sorted(words_on_line, key=lambda w: w["x0"])
    for start in range(len(words_sorted)):
        acc = ""
        picked = []
        for w in words_sorted[start:]:
            acc += re.sub(r"\s+", "", w["text"]).upper()
            picked.append(w)
            if acc == target:
                return _lines_to_rects(
                    [(page_idx, w2["x0"], w2["x1"], w2["top"], w2["bottom"]) for w2 in picked],
                    page_sizes,
                )
            if len(acc) > len(target):
                break
    return None


def _ssa_code_words(item, words_on_line, is_station_line):
    """Words on one code line that are plausible 3-letter station codes. On the
    station line itself, scoped to words right of the colon (the line's prefix
    — "FR FLT DEP FRM STATION EUROPE:" — contains real 3-letter words like
    "FLT" that must never be mistaken for a code); a pure continuation line has
    nothing but codes, so no scoping is needed there."""
    if is_station_line:
        colon_words = [w for w in words_on_line if ":" in w["text"]]
        if not colon_words:
            return []
        cutoff_x = max(w["x1"] for w in colon_words)
        candidates = [w for w in words_on_line if w["x0"] > cutoff_x]
    else:
        candidates = words_on_line
    return [w for w in candidates if re.fullmatch(r"[A-Z]{3}", w["text"].strip())]


def _extract_ssa_entry(group_items, words_for_page, pages, page_sizes):
    """group_items: the line-items (page_idx, x0, x1, top, bottom, text) for
    one COM-INFO part already known to mention "SPECIAL SECURITY ARRANGEMENT".
    Returns {"groups": [{"codes": {code: [rect,...]}, "level": str,
    "level_rects": [rect,...]}, ...], "catch_all": {...} | None}, or None if
    nothing usable was found — same graceful-miss rule as the rest of this
    module; a shape this parser can't confidently read simply isn't briefed.
    """
    text_of = lambda item: item[5]
    raw_groups, catch_all_raw = _parse_ssa_structure(group_items, text_of)
    if not raw_groups and not catch_all_raw:
        return None

    def words_on_line(item):
        page_idx, x0, x1, top, bottom, text = item
        # NOTAM body text is single-spaced tightly enough (observed as little
        # as ~0.65pt between adjacent lines) that met_anchors.py's +-1pt
        # padding bleeds words from the next physical line in, scrambling
        # _phrase_rect's left-to-right accumulation. Word tops/bottoms here
        # are exact matches of their own line's, not merely close, so a much
        # tighter epsilon is safe.
        return [
            w for w in words_for_page(pages, page_idx)
            if top - 0.1 <= w["top"] <= bottom + 0.1
        ]

    out_groups = []
    for g in raw_groups:
        codes = {}
        for idx, item in enumerate(g["code_items"]):
            page_idx = item[0]
            for w in _ssa_code_words(item, words_on_line(item), is_station_line=(idx == 0)):
                code = w["text"].strip()
                if code not in codes:
                    codes[code] = _lines_to_rects(
                        [(page_idx, w["x0"], w["x1"], w["top"], w["bottom"])], page_sizes
                    )
        level_item = g["level_item"]
        level_rects = _phrase_rect(
            level_item[0], words_on_line(level_item), f"LEVEL:{g['level']}", page_sizes
        )
        if codes and level_rects:
            out_groups.append({"codes": codes, "level": g["level"], "level_rects": level_rects})

    out_catch_all = None
    if catch_all_raw:
        item = catch_all_raw["item"]
        level_rects = _phrase_rect(
            item[0], words_on_line(item), f"{catch_all_raw['level']} LEVEL", page_sizes
        )
        if level_rects:
            out_catch_all = {"level": catch_all_raw["level"], "level_rects": level_rects}

    if not out_groups and not out_catch_all:
        return None
    return {"groups": out_groups, "catch_all": out_catch_all}


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
      extra:      {"ssa": {anchor_key: {see _extract_ssa_entry}, ...}}
    """
    anchors = {}
    ssa = {}
    page_sizes = []

    current_section = ""
    current_ap = None
    current_fir = None

    cur_key = None
    cur_lines = []
    cur_is_ci = False

    word_cache = {}

    def words_for_page(pages, page_idx):
        if page_idx not in word_cache:
            word_cache[page_idx] = pages[page_idx].extract_words(
                use_text_flow=False, keep_blank_chars=False
            )
        return word_cache[page_idx]

    def flush(pages):
        nonlocal cur_key, cur_lines, cur_is_ci
        if cur_key and cur_lines and cur_key not in anchors:  # first occurrence wins
            anchors[cur_key] = _lines_to_rects([l[:5] for l in cur_lines], page_sizes)
            # COM-INFO bulletins (GENERAL/FLIGHT LEG/AEROPLANE only, matching
            # notam_engine._split_com_info_parts's own scope) bundle several
            # sub-notices in one block; give each its own precise anchor
            # ("<owner>|<id> [N]", matching general_notams.json's split id)
            # instead of leaving every part pointing at the whole block.
            # cur_lines[0] is the ID/header line itself — never part of a
            # sub-notice's own box, so only cur_lines[1:] is partitioned.
            if cur_is_ci and current_section in _GENERAL_SECTIONS and len(cur_lines) > 1:
                body = cur_lines[1:]
                groups = _partition_at_dash_boundaries(body, text_of=lambda item: item[5])
                if len(groups) > 1:
                    for idx, group in enumerate(groups):
                        part_key = f"{cur_key} [{idx + 1}]"
                        if part_key not in anchors:
                            anchors[part_key] = _lines_to_rects([l[:5] for l in group], page_sizes)
                        if part_key not in ssa and _is_ssa_notice(l[5] for l in group):
                            entry = _extract_ssa_entry(group, words_for_page, pages, page_sizes)
                            if entry:
                                ssa[part_key] = entry
                elif _is_ssa_notice(l[5] for l in body):  # a lone, unsplit SSA part
                    entry = _extract_ssa_entry(body, words_for_page, pages, page_sizes)
                    if entry:
                        ssa[cur_key] = entry
        cur_key = None
        cur_lines = []
        cur_is_ci = False

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        for page_idx, page in enumerate(pages):
            page_sizes.append((page.width, page.height))
            for line in page.extract_text_lines() or []:
                text = (line.get("text") or "").strip()
                if not text or _PAGE_HDR_RE.match(text):
                    continue

                m_sect = _MAIN_SECT_RE.match(text)
                if m_sect:
                    flush(pages)
                    current_section = m_sect.group(1)
                    current_ap = None
                    current_fir = None
                    continue

                if current_section == "ENROUTE":
                    m = _FIR_HDR_RE.match(text)
                    if m:
                        flush(pages)
                        current_fir = m.group(1)
                        continue
                elif current_section in ("AERODROME", "ADDITIONAL"):
                    m = _AP_HDR_RE.match(text)
                    if m:
                        flush(pages)
                        current_ap = m.group(1)
                        continue

                m_id = _NOTAM_ID_RE.match(text)
                if m_id:
                    flush(pages)
                    owner = _owner_for(current_section, current_ap, current_fir)
                    cur_key = f"{owner}|{m_id.group(1).strip()}" if owner else None
                    cur_is_ci = bool(_COM_INFO_TAG_RE.search(text))

                if cur_key is not None:
                    cur_lines.append((page_idx, line["x0"], line["x1"], line["top"], line["bottom"], text))

        flush(pages)

    return anchors, page_sizes, {"ssa": ssa}


def render_pages(pdf_path, out_dir, resolution=144, prefix="notam_page"):
    """Render every page of pdf_path to out_dir/<prefix>_NNN.png (1-based, zero-padded 3).
    Returns the page count."""
    os.makedirs(out_dir, exist_ok=True)
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            img = page.to_image(resolution=resolution)
            img.save(os.path.join(out_dir, f"{prefix}_{i:03d}.png"))
        return len(pdf.pages)
