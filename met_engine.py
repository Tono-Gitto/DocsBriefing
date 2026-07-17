"""
MET engine: parse TG921_MET.pdf + route.json → enriched airports.json.

Per airport:
  - METAR (SA line)
  - TAF condensed at reference time (BECMG/FM folded per CLAUDE.md §2)
  - Reference time = TAKEOFF + ACCT of nearest route waypoint (haversine)
"""

import json, os, re
from datetime import datetime, timedelta, timezone
from airport_coords import load_coords
from _utils import haversine_nm, clean_pdf_lines

HERE = os.path.dirname(os.path.abspath(__file__))
MET_PDF   = os.path.join(HERE, "Input", "TG921_MET.pdf")
ROUTE_JSON = os.path.join(HERE, "data", "route.json")
OUT_JSON  = os.path.join(HERE, "data", "airports.json")

# ETD 1245Z + 20 min taxi = takeoff 1305Z on 20 JUN 2026
TAKEOFF_UTC = datetime(2026, 6, 20, 13, 5, tzinfo=timezone.utc)

# Per-run parse warnings (reset by main); the Flask pipeline surfaces these
# in the progress UI so silently skipped airports are visible to the crew.
WARNINGS = []

# ── MET PDF parsing ──────────────────────────────────────────────────────────

_HEADER_RE = re.compile(r"^([A-Z]{4})\s+-\s*([A-Z]{3,4})\s+-\s+(.+)")
_PAGE_HDR_RE = re.compile(
    r"^(\$B|Dispatch MET|_{5,}|\d{2}[A-Z]{3}\d{2}\s+THA\d+|TG\d+\s+\d{2}[A-Z]{3})"
)

def parse_met_pdf(pdf_path):
    """Return ({icao: {iata, name, metar, taf_raw}}, [ordered icao list])."""
    clean = clean_pdf_lines(pdf_path, _PAGE_HDR_RE)
    airports = {}
    order = []
    current = None
    mode = None   # 'header' | 'sa' | 'ft' | 'done'
    buf = []

    def finalize(kind):
        if not buf or not current:
            return
        text = " ".join(buf)
        if "=" in text:
            text = text[: text.index("=")]
        text = text.strip()
        if kind == "metar":
            airports[current]["metar"] = re.sub(r"^SA\s+", "", text)
        else:
            airports[current]["taf_raw"] = text

    is_dup = False
    for line in clean:
        m = _HEADER_RE.match(line)
        if m:
            # Flush whatever was being captured — an unterminated METAR is
            # better kept partial than silently dropped
            if mode == "sa":
                finalize("metar")
            elif mode == "ft":
                finalize("taf")
            buf = []
            current = m.group(1)
            is_dup = current in airports
            if not is_dup:
                order.append(current)
                airports[current] = {
                    "iata": m.group(2),
                    "name": m.group(3).strip(),
                    "runway_info": None,
                    "metar": None,
                    "taf_raw": None,
                }
            mode = "header"
            continue

        if current is None:
            continue

        # Start new capture block when not already collecting
        if mode not in ("sa", "ft"):
            if line.startswith("SA "):
                mode = "sa"
                buf = []
            elif line.startswith("FT "):
                mode = "ft"
                buf = []
            else:
                if mode == "header" and not is_dup:
                    # Runway info may wrap to multiple lines (e.g. airports with 5+ runways)
                    if airports[current]["runway_info"] is None:
                        airports[current]["runway_info"] = line
                    else:
                        airports[current]["runway_info"] += " " + line
                continue

        buf.append(line)
        if "=" in " ".join(buf):
            finalize("metar" if mode == "sa" else "taf")
            buf = []
            mode = "done"

    if mode == "sa":
        finalize("metar")
    elif mode == "ft":
        finalize("taf")

    return airports, order


# ── Reference-time engine ────────────────────────────────────────────────────

def compute_ref_time(lat, lon, route_pts):
    """Return (ref_datetime_utc, dist_nm) via nearest-waypoint haversine."""
    best_dist, best_acct = float("inf"), 0
    for pt in route_pts:
        d = haversine_nm(lat, lon, pt["lat"], pt["lon"])
        if d < best_dist:
            best_dist, best_acct = d, pt["acct_min"]
    return TAKEOFF_UTC + timedelta(minutes=best_acct), best_dist


# ── TAF condensing (CLAUDE.md §2) ────────────────────────────────────────────

# Must try longer alternatives first so PROB30 TEMPO beats bare PROB30
_GROUP_RE = re.compile(
    r"\b(PROB30 TEMPO|PROB40 TEMPO|PROB30|PROB40|BECMG|TEMPO|FM\d{6})\b\s*(\d{4}/\d{4}|)"
)

_WIND_RE = re.compile(r"^(VRB|\d{3})\d{2,3}(G\d{2,3})?(KT|MPS|KMH)$")
_WIND_VAR_RE = re.compile(r"^\d{3}V\d{3}$")


def _leading_wind(s):
    toks = s.split()
    return toks[0] if toks and _WIND_RE.match(toks[0]) else None


def _is_pure_wind_change(text):
    """True if the group states only wind (plus an optional 250V310 variation)."""
    toks = text.split()
    if not toks or not _WIND_RE.match(toks[0]):
        return False
    return all(_WIND_VAR_RE.match(t) for t in toks[1:])


def _fold_conditions(old, new):
    """Fold a completed/in-progress BECMG or FM group onto the running baseline.

    A wind-only change keeps old's non-wind elements (visibility/weather/cloud,
    including CAVOK) and swaps in the new wind; any group that also states
    visibility/weather/cloud fully replaces the baseline (TAF convention).
    """
    if not _is_pure_wind_change(new):
        return new
    old_wind = _leading_wind(old)
    if old_wind is None:
        return new
    old_rest = old[len(old_wind):].strip()
    return f"{new} {old_rest}".strip() if old_rest else new


def _fold_conditions_toks(old_toks, old_text, new_toks, new_text):
    """Token-level mirror of _fold_conditions — same three branches, driven by
    the same _is_pure_wind_change/_leading_wind checks on the text form, so the
    two stay in lockstep by construction (see CLAUDE.md gotcha on
    taf_base_src). Tests assert " ".join(t) over the result equals
    _fold_conditions(old_text, new_text) rather than trusting this in isolation.
    """
    if not _is_pure_wind_change(new_text):
        return list(new_toks)
    if _leading_wind(old_text) is None:
        return list(new_toks)
    return list(new_toks) + list(old_toks[1:])


def _resolve_ddhh(dd, hh, mm, anchor_dt):
    """Resolve a TAF day/hour(/minute) token to the UTC datetime nearest anchor_dt.

    TAF tokens carry no month: a token whose day is far from the anchor's day
    belongs to the adjacent month (e.g. window 3018/0118 read at ref 30 Jun).
    Hour 24 means midnight at the end of that day.
    """
    extra = timedelta(0)
    if hh == 24:
        hh = 0
        extra = timedelta(days=1)
    candidates = []
    for moff in (-1, 0, 1):
        y, m = anchor_dt.year, anchor_dt.month + moff
        if m == 0:
            y, m = y - 1, 12
        elif m == 13:
            y, m = y + 1, 1
        try:
            candidates.append(datetime(y, m, dd, hh, mm, tzinfo=timezone.utc) + extra)
        except ValueError:
            pass  # day doesn't exist in that month (e.g. 31 Jun)
    return min(candidates, key=lambda c: abs(c - anchor_dt))


_FT_HEADER_RE = re.compile(r"^FT\s+\S+\s+\S+\s*")


def _tokenize(segment, base_offset):
    """[{"t": token, "s": absolute offset in taf_raw}, ...] for each whitespace-
    delimited token in segment, where base_offset is segment's own start offset
    in taf_raw. Used to give the Source Pane word-level provenance for
    taf_base_src (see CLAUDE.md "Source Pane" / met_anchors.py "words")."""
    return [{"t": m.group(0), "s": base_offset + m.start()} for m in re.finditer(r"\S+", segment)]


def _parse_groups(taf_raw, ref_dt):
    matches = list(_GROUP_RE.finditer(taf_raw))
    base_end = matches[0].start() if matches else len(taf_raw)
    base_raw = taf_raw[:base_end]
    hm = _FT_HEADER_RE.match(base_raw)
    header_end = hm.end() if hm else 0
    base_text = base_raw[header_end:].strip()
    base_toks = _tokenize(base_raw[header_end:], header_end)

    if not matches:
        return base_text, base_toks, []

    groups = []
    for i, gm in enumerate(matches):
        gtype  = gm.group(1)
        window = gm.group(2)
        text_end = matches[i + 1].start() if i + 1 < len(matches) else len(taf_raw)
        gtext = taf_raw[gm.end(): text_end].strip()
        gtoks = _tokenize(taf_raw[gm.end(): text_end], gm.end())

        if gtype.startswith("FM"):
            # FM DDHHMM — time encoded in type token, no end
            start = _resolve_ddhh(int(gtype[2:4]), int(gtype[4:6]), int(gtype[6:8]), ref_dt)
            end = None
        elif window:
            p_start, p_end = window.split("/")
            start = _resolve_ddhh(int(p_start[:2]), int(p_start[2:4]), 0, ref_dt)
            # end is anchored to start (validity ≤ 30 h) so 3018/0118 lands in the next month
            end   = _resolve_ddhh(int(p_end[:2]), int(p_end[2:4]), 0, start)
        else:
            continue  # malformed

        groups.append({"type": gtype, "start": start, "end": end, "text": gtext,
                        "toks": gtoks, "src_start": gm.start()})

    return base_text, base_toks, groups


def _fmt_dt(dt):
    return f"{dt.day:02d}{dt.hour:02d}Z"


def _fmt_window(start, end):
    return f"from {_fmt_dt(start)}" if end is None else f"{_fmt_dt(start)}-{_fmt_dt(end)}"


def condense_taf(taf_raw, ref_dt):
    """
    Returns (base_str, becmg_in_progress|None, [active_overlays], taf_base_src).
    BECMG/FM completed before ref_dt fold into the baseline.
    Overlays cover the OM-A §8.1.7.4.1(7) window: ETA ±1h.
    All group times are resolved to real datetimes so month/year boundaries
    compare correctly.

    taf_base_src is the token-level provenance of base_str — a list of
    {"t": token, "s": offset in taf_raw} in display order — threaded alongside
    the string fold via _fold_conditions_toks so the Source Pane can highlight
    exactly the source tokens that make up "conditions at ETA" (see CLAUDE.md
    "Source Pane", met_anchors.py "words"). becmg_in_progress's display text is
    not given token provenance — only the baseline is.
    """
    base_text, base_toks, groups = _parse_groups(taf_raw, ref_dt)
    win_start = ref_dt - timedelta(hours=1)
    win_end   = ref_dt + timedelta(hours=1)
    baseline  = base_text
    baseline_toks = base_toks
    becmg_prog = None
    becmg_prog_base = base_text
    overlays   = []

    for g in sorted(groups, key=lambda x: x["start"]):
        t = g["type"]
        s = g["start"]

        if t == "BECMG" or t.startswith("FM"):
            if g["end"] is None:              # FM: complete once past start
                if ref_dt >= s:
                    baseline_toks = _fold_conditions_toks(baseline_toks, baseline, g["toks"], g["text"])
                    baseline = _fold_conditions(baseline, g["text"])
                elif s < win_end:             # FM starts within +1h → overlay
                    overlays.append(g)
            else:
                if ref_dt >= g["end"]:
                    baseline_toks = _fold_conditions_toks(baseline_toks, baseline, g["toks"], g["text"])
                    baseline = _fold_conditions(baseline, g["text"])  # fold
                elif s <= ref_dt < g["end"]:
                    becmg_prog = g            # in progress right now
                    becmg_prog_base = baseline  # pre-BECMG conditions to fold onto
                elif s > ref_dt and s < win_end:
                    overlays.append(g)        # upcoming within +1h
        else:  # TEMPO / PROB30 TEMPO / PROB40 TEMPO / bare PROB
            # Show if group overlaps with [ETA−1h, ETA+1h]
            if s < win_end and g["end"] is not None and g["end"] > win_start:
                overlays.append(g)

    becmg_out = (
        {"text": _fold_conditions(becmg_prog_base, becmg_prog["text"]),
         "window": _fmt_window(becmg_prog["start"], becmg_prog["end"]),
         "src_start": becmg_prog["src_start"]}
        if becmg_prog else None
    )
    overlay_out = [
        {"type": "FM" if g["type"].startswith("FM") else g["type"],
         "text": g["text"],
         "window": _fmt_window(g["start"], g["end"]),
         "src_start": g["src_start"]}
        for g in overlays
    ]
    return baseline, becmg_out, overlay_out, baseline_toks


# ── Weather severity tier (RED/YELLOW/GREEN) ─────────────────────────────────
# Mirrors the _classify_tier pattern in notam_engine.py: keyword/regex tables
# feeding one classification function, unit-tested against fixed thresholds.

_CEILING_RE = re.compile(r"^(BKN|OVC|VV)(\d{3})$")
_VIS_TOKEN_RE = re.compile(r"^\d{4}$")
# Presence-only: matches regardless of intensity prefix (-/+) or count, and
# a leading VC (vicinity, e.g. "VCTS") never matches since \b requires a
# boundary immediately before "TS" — vicinity phenomena aren't at the field.
_WX_PHENOMENA_RE = re.compile(r"\b(TS\w*|FZRA|FZDZ|FZFG|FC|SS|DS)\b")

_TIER_RANK = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}


def _worse_tier(a, b):
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


def _cap_at(tier, ceiling):
    return tier if _TIER_RANK[tier] <= _TIER_RANK[ceiling] else ceiling


def _vis_and_ceiling(toks):
    vis_m = None
    ceiling_ft = None
    for tok in toks:
        if _VIS_TOKEN_RE.match(tok):
            vis_m = int(tok)
        m = _CEILING_RE.match(tok)
        if m:
            ft = int(m.group(2)) * 100
            ceiling_ft = ft if ceiling_ft is None else min(ceiling_ft, ft)
    return vis_m, ceiling_ft


def _tier_from_vis_ceiling(vis_m, ceiling_ft):
    if vis_m is None:
        vis_m = 9999
    if ceiling_ft is None:
        ceiling_ft = 999999
    if vis_m < 1600 or ceiling_ft < 500:
        return "RED"
    if vis_m < 5000 or ceiling_ft < 2000:
        return "YELLOW"
    return "GREEN"


def _tier_for_text(text):
    """Classify a full-state TAF condition string (taf_base, or a folded
    becmg_in_progress text) at full confidence.

    Ceiling = lowest BKN/OVC/VV height only (FEW/SCT never count as a
    ceiling). Visibility = a bare 4-digit token (unambiguous vs. wind's
    trailing KT/MPS/KMH and cloud's letters+3-digits shape). CAVOK implies
    unrestricted vis/ceiling. A full-state string with neither a vis token
    nor CAVOK/NSC is ambiguous (real TAFs always state one) and defaults to
    YELLOW rather than silently passing as GREEN.

    Severity is driven by vis/ceiling numbers alone — a phenomena keyword
    (thunderstorm, freezing precip, funnel cloud, sand/duststorm; any
    intensity) never elevates this past vis/ceiling's own verdict. It only
    guarantees a YELLOW floor: vis/ceiling numbers are what a pilot can act
    on operationally, but a phenomenon in play — even with clean vis/ceiling
    — still deserves at least a caution flag, not a silent GREEN.
    """
    toks = text.split()
    has_cavok = "CAVOK" in toks or "NSC" in toks
    vis_m, ceiling_ft = _vis_and_ceiling(toks)

    if vis_m is None and ceiling_ft is None and not has_cavok:
        tier = "YELLOW"
    else:
        tier = _tier_from_vis_ceiling(vis_m, ceiling_ft)

    if _WX_PHENOMENA_RE.search(text):
        tier = _worse_tier(tier, "YELLOW")

    return tier


def _tier_for_partial_text(text):
    """Classify a partial diff-group (TEMPO/PROB/an upcoming overlay) that
    only restates what's temporarily changing.

    Unlike _tier_for_text, a missing vis/ceiling token here is not ambiguous
    — TAF convention omits elements that aren't changing (e.g. a wind-only
    TEMPO carries no vis/cloud restriction of its own) — so it scores GREEN
    (neutral), not YELLOW, when neither is stated. Same phenomena floor as
    _tier_for_text — see there for rationale.
    """
    vis_m, ceiling_ft = _vis_and_ceiling(text.split())
    tier = _tier_from_vis_ceiling(vis_m, ceiling_ft)

    if _WX_PHENOMENA_RE.search(text):
        tier = _worse_tier(tier, "YELLOW")

    return tier


def _classify_wx_tier(taf_base, becmg_in_progress, active_overlays):
    """Worst-case severity tier for an airport at ref_dt. This is a planning
    tool, not a nowcast — a deteriorating overlay must be visible as severe,
    not smoothed down to a generic "caution" color, because dispatch plans
    for the credible worst case, not just the most-likely one.

    taf_base holds at ref_dt (already folded by condense_taf) and is scored
    at full severity (RED-capable). becmg_in_progress is also a folded
    full-state string — the airport is mid-transition, which is at least as
    certain as a TEMPO, so it's scored at full severity too, but still
    forces a YELLOW floor regardless of either end-state (mid-transition
    never reads as clean GREEN). active_overlays are raw TEMPO/PROB/FM/
    upcoming-BECMG diff-groups scored with _tier_for_partial_text: TEMPO,
    FM, and an upcoming BECMG are deterministic forecast changes (not yet
    started, but not probabilistic either) and score at full severity.
    PROB30/PROB40 (bare or combined with TEMPO) is an explicit probability
    estimate, not a forecast commitment, and is capped at ORANGE — worse
    than a plain "caution" YELLOW, but short of the certainty RED implies.

    This certainty cap is orthogonal to phenomena severity: RED is reserved
    for vis/ceiling numbers alone (see _tier_for_text / _tier_for_partial_text
    docstrings) — a phenomena keyword never drives a group above YELLOW on
    its own, so a PROB overlay's ORANGE cap in practice only ever bites when
    its own vis/ceiling numbers are RED-level.
    """
    tier = _tier_for_text(taf_base) if taf_base else "YELLOW"

    if becmg_in_progress:
        tier = _worse_tier(tier, "YELLOW")
        tier = _worse_tier(tier, _tier_for_text(becmg_in_progress["text"]))

    for ov in active_overlays:
        ov_tier = _tier_for_partial_text(ov["text"])
        if "PROB" in ov["type"]:
            ov_tier = _cap_at(ov_tier, "ORANGE")
        tier = _worse_tier(tier, ov_tier)

    return tier


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global WARNINGS
    WARNINGS = []

    with open(ROUTE_JSON) as f:
        route_pts = json.load(f)

    coords = load_coords()
    met_data, order = parse_met_pdf(MET_PDF)

    out = []
    for icao in order:
        d = met_data[icao]
        if icao not in coords:
            msg = f"no coords for {icao} ({d['name']}) — airport dropped from briefing"
            WARNINGS.append(msg)
            print(f"  WARN: {msg}")
            continue

        lat, lon = coords[icao]
        ref_dt, dist_nm = compute_ref_time(lat, lon, route_pts)

        taf_base, becmg_prog, active_overlays, taf_base_src = None, None, [], None
        if d["taf_raw"]:
            taf_base, becmg_prog, active_overlays, taf_base_src = condense_taf(d["taf_raw"], ref_dt)
        wx_tier = _classify_wx_tier(taf_base, becmg_prog, active_overlays)

        out.append({
            "icao": icao,
            "iata": d["iata"],
            "name": d["name"],
            "runway_info": d.get("runway_info"),
            "lat": lat,
            "lon": lon,
            "ref_time": ref_dt.strftime("%H%MZ"),
            "ref_iso": ref_dt.isoformat(),
            "dist_nm": round(dist_nm),
            "metar": d["metar"],
            "taf_raw": d["taf_raw"],
            "taf_base": taf_base,
            "taf_base_src": taf_base_src,
            "becmg_in_progress": becmg_prog,
            "active_overlays": active_overlays,
            "wx_tier": wx_tier,
        })

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Written {len(out)} airports to {OUT_JSON}")

    # Spot-check against CLAUDE.md §2 validated cases
    checks = {
        "EDDF": ("1305Z", "23007KT"),
        "OPLA": ("1917Z", "26005KT 4000 FU SCT100"),
        "VTBS": ("2326Z", "24008KT 9999 SCT020"),
    }
    print("\nValidation spot-checks:")
    for icao, (exp_ref, exp_base) in checks.items():
        ap = next((a for a in out if a["icao"] == icao), None)
        if not ap:
            print(f"  {icao}: NOT IN OUTPUT")
            continue
        ref_ok  = ap["ref_time"] == exp_ref
        base_ok = ap["taf_base"] and exp_base in ap["taf_base"]
        becmg   = ap["becmg_in_progress"]
        print(
            f"  {icao}  ref={ap['ref_time']} ({'✓' if ref_ok else '✗'})  "
            f"base='{ap['taf_base']}' ({'✓' if base_ok else '✗'})"
            + (f"  BECMG_PROG='{becmg['text']}' [{becmg['window']}]" if becmg else "")
        )
    # OPKC — check BECMG in progress
    opkc = next((a for a in out if a["icao"] == "OPKC"), None)
    if opkc:
        b = opkc["becmg_in_progress"]
        print(f"  OPKC  ref={opkc['ref_time']}  base='{opkc['taf_base']}'  "
              + (f"BECMG_PROG='{b['text']}' [{b['window']}]" if b else "no BECMG in progress"))


if __name__ == "__main__":
    main()
