"""
Flight HIRA (Hazard Identification & Risk Assessment) brief.

Run-level pipeline step (once per upload, spanning ALL groups/legs — a dispatcher
signs the whole day, not half of it). Builds a deterministic hazard digest from the
per-group JSON already written by the MET/NOTAM steps, computes a deterministic
overall risk in the app's existing 4-tier weather vocabulary (so the header dot is
coherent with the map markers), then asks Claude to synthesise a freeform-prose
dispatcher brief grounded on that digest.

Never fails the run: if the API call fails (or no key), `hira.json` is still written
with `generated=false` and `brief` set to the human-readable digest verbatim — so the
HIRA button always works, degrading to the raw facts exactly like the NOTAM step
degrades to first-body-line summaries.

Writes an identical `hira.json` into every group dir (the brief is whole-flight, so
both map tabs show the same document — same "compute once, copy into each group dir"
pattern as the Source Pane anchors).

Schema (`hira.json`):
  {
    "risk": "GREEN|YELLOW|ORANGE|RED",   # deterministic, whole-flight, AI-independent
    "generated": true,                    # false when brief == digest fallback
    "brief": "<freeform prose or digest text>",
    "digest_text": "<human-readable hazard digest — the AI input and the fallback UI>"
  }
"""

import json
import os

MODEL = "claude-sonnet-5"   # once per flight — cross-referencing judgment worth it
_API_RETRIES = 3

# 4-tier weather vocabulary, shared with index.html's WX_COLORS / WX_RANK so the
# header risk dot reads coherently next to the map markers.
_RANK = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}
_UNRANK = {v: k for k, v in _RANK.items()}

_SYSTEM = """You are an experienced airline flight dispatcher writing the HIRA \
(Hazard Identification & Risk Assessment) brief that goes to the operating crew of a \
Thai Airways B777/B787 long-haul flight.

You are given a DETERMINISTIC HAZARD DIGEST assembled from the flight's OFP, MET (TAF) \
and NOTAM package. Write a concise freeform-prose briefing — no rigid headings, no \
bullet-list dump — in the voice of a dispatcher handing the flight over.

Hard rules:
- Ground every statement in the digest. Never invent hazards, weather, NOTAMs, fuel \
figures, or numbers that are not in the digest. You have NO fuel data — never cite \
fuel quantities or endurance.
- Lead with a one-line bottom-line-up-front verdict, then expand.
- Prioritise the crew's decision-making: what is marginal, what to watch, and — the \
highest-value dispatcher insight — call out any leg where the destination AND its \
alternate are both marginal (a thin diversion margin).
- Cover, only where the digest shows something worth saying: destination/departure \
weather, runway/navaid/approach NOTAMs, alternate adequacy, and enroute/airspace \
hazards.
- Be honest and calm. If the flight is benign, say so briefly rather than \
manufacturing concern. Aim for 150–350 words.
- Plain text only (no markdown headings or tables). Short paragraphs."""


def _load(group_dir, name):
    path = os.path.join(group_dir, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _relevant_icaos(fleg):
    """Operationally-relevant airports for a leg: dep, dest, and every alternate.

    Deliberately EXCLUDES the ~40 enroute contingency airports in airports.json —
    the flight isn't landing there, so their weather must not drive flight risk or
    clutter the brief."""
    icaos = []
    for k in ("dep", "dest"):
        if fleg.get(k):
            icaos.append(fleg[k])
    for k in ("dest_altn", "era", "rcf_altn"):
        icaos.extend(fleg.get(k) or [])
    if fleg.get("rcf_dest"):
        icaos.append(fleg["rcf_dest"])
    # de-dupe, preserve order
    seen, out = set(), []
    for ic in icaos:
        if ic and ic not in seen:
            seen.add(ic)
            out.append(ic)
    return out


def _leg_entry(ap, leg_idx):
    """The airport's per-leg weather/NOTAM entry for local leg index `leg_idx`."""
    for lg in ap.get("legs", []):
        if lg.get("leg") == leg_idx:
            return lg
    return None


def _t12(notams):
    """T1/T2 NOTAMs only — the actionable infrastructure hazards."""
    return [n for n in (notams or []) if n.get("tier", 3) < 3]


def _wx_line(leg_entry):
    """Compact 'TIER — condensed TAF' for one airport-leg."""
    if not leg_entry:
        return "no MET data"
    tier = leg_entry.get("wx_tier", "—")
    parts = []
    base = leg_entry.get("taf_base")
    if base:
        parts.append(base)
    bip = leg_entry.get("becmg_in_progress")
    if bip and bip.get("text"):
        parts.append(f"BECMG(in progress) {bip['text']}")
    for ov in leg_entry.get("active_overlays", []):
        parts.append(f"{ov.get('type', '')} {ov.get('text', '')} [{ov.get('window', '')}]".strip())
    taf = "; ".join(parts) if parts else "no TAF"
    return f"{tier} — {taf}"


def _notam_lines(notams, indent="      "):
    out = []
    for n in _t12(notams):
        tier = n.get("tier", 3)
        txt = n.get("summary") or (n.get("body", "").splitlines()[0] if n.get("body") else n.get("id", ""))
        win = n.get("window")
        wtxt = f" [{win}]" if win else ""
        out.append(f"{indent}NOTAM T{tier}: {txt}{wtxt}")
    return out


def build_digest(group_dirs):
    """Assemble the whole-flight hazard digest across all groups.

    Returns (digest_text, risk) where risk is one of GREEN/YELLOW/ORANGE/RED,
    computed deterministically and independently of any AI call.

    Risk calibration (a real dispatcher's verdict, coherent with the map's
    weather semaphore, not a NOTAM-count trip-wire):
      - **Weather at dep/dest drives the dot at full weight** — a RED-weather
        destination is a RED flight.
      - **The alternate→RED path is the diversion-margin cross-check**: a leg
        whose destination is marginal (YELLOW+) *and* every listed destination
        alternate is also marginal has no clean out → RED. This is the
        highest-value dispatcher insight, and the only way alternates reach RED.
      - **NOTAMs and an individual alternate's own weather are a caution floor,
        never independently RED** — a lone NDB U/S or one alternate's ILS outage
        must not scream RED while the weather is clean. dep/dest T1 → ORANGE,
        T2 → YELLOW; any alternate/FIR/flight-wide T1/T2 → YELLOW. (NOTAM tiers in
        airports.json are already daily-window-effective — see app.py — so a
        closure outside its active hours is T3 here and contributes nothing.)"""
    lines = []
    risk_rank = 0
    _RED, _ORANGE, _YELLOW = _RANK["RED"], _RANK["ORANGE"], _RANK["YELLOW"]

    def bump(rank):
        nonlocal risk_rank
        risk_rank = max(risk_rank, rank)

    def bump_notam_floor(notams, t1_floor, t2_floor):
        for n in (notams or []):
            t = n.get("tier", 3)
            if t == 1:
                bump(t1_floor)
            elif t == 2:
                bump(t2_floor)

    header_done = False
    seen_general = set()   # de-dupe flight-wide NOTAMs across groups (shared NOTAM PDF)
    general_block = []

    for group_dir in group_dirs:
        flight_info = _load(group_dir, "flight_info.json") or {"legs": []}
        airports = _load(group_dir, "airports.json") or []
        firs = _load(group_dir, "fir_notams.json") or []
        general = _load(group_dir, "general_notams.json") or []
        ap_by_icao = {a["icao"]: a for a in airports}

        for leg_idx, fleg in enumerate(flight_info["legs"], start=1):
            if not header_done:
                acft = fleg.get("acft", "")
                reg = fleg.get("reg", "")
                date = fleg.get("date", "")
                lines.append(f"FLIGHT HIRA DIGEST — {date}   Aircraft {acft} {reg}".rstrip())
                lines.append("")
                header_done = True

            flt = fleg.get("flight", "TG???")
            dep, dest = fleg.get("dep", "?"), fleg.get("dest", "?")
            etd, eta = fleg.get("etd", "?"), fleg.get("eta", "?")
            lines.append(f"── {flt}  {dep}→{dest}   ETD {etd} / ETA {eta} ──")

            dest_altns = set(fleg.get("dest_altn") or [])
            leg_dest_wx = None       # dest wx rank, for the diversion-margin cross-check
            leg_altn_wx = []         # dest-alternate wx ranks (only those with MET data)
            for ic in _relevant_icaos(fleg):
                ap = ap_by_icao.get(ic)
                if not ap:
                    role = ("DEP" if ic == dep else "DEST" if ic == dest else "ALTN")
                    lines.append(f"  {role} {ic}: no data (not in MET package)")
                    continue
                le = _leg_entry(ap, leg_idx)
                is_depdest = ic in (dep, dest)
                if le:
                    wx_rank = _RANK.get(le.get("wx_tier"), 0)
                    if is_depdest:
                        bump(wx_rank)                                   # dep/dest weather: full weight
                        bump_notam_floor(le.get("notams"), _ORANGE, _YELLOW)
                    else:
                        bump(min(wx_rank, _ORANGE))                     # one alternate alone: cap at ORANGE
                        bump_notam_floor(le.get("notams"), _YELLOW, _YELLOW)
                    if ic == dest:
                        leg_dest_wx = wx_rank
                    elif ic in dest_altns:
                        leg_altn_wx.append(wx_rank)
                if ic == dep:
                    role = "DEP "
                elif ic == dest:
                    role = "DEST"
                elif ic in dest_altns:
                    role = "ALTN"
                else:
                    role = "ALTN(era/rcf)"
                lines.append(f"  {role} {ic}: {_wx_line(le)}")
                lines.extend(_notam_lines(le.get("notams") if le else None))

            # Diversion-margin cross-check: destination marginal AND every listed
            # destination alternate also marginal → no clean out → RED.
            if (leg_dest_wx is not None and leg_dest_wx >= _YELLOW
                    and leg_altn_wx and all(t >= _YELLOW for t in leg_altn_wx)):
                bump(_RED)

            # Enroute FIR hazards for this leg (enroute airspace — caution floor only)
            fir_lines = []
            for fir in firs:
                for flg in fir.get("legs", []):
                    if flg.get("leg") != leg_idx:
                        continue
                    t12 = _t12(flg.get("notams"))
                    bump_notam_floor(flg.get("notams"), _YELLOW, _YELLOW)
                    for n in t12:
                        txt = n.get("summary") or n.get("id", "")
                        fir_lines.append(f"      {fir.get('fir', '?')} T{n.get('tier')}: {txt}")
            if fir_lines:
                lines.append("  ENROUTE / FIR:")
                lines.extend(fir_lines)
            lines.append("")

        # Flight-wide NOTAMs (already AI-filtered) — collect once, de-duped by id
        for sec in general:
            for n in sec.get("notams", []):
                key = (sec.get("key"), n.get("id"))
                if key in seen_general:
                    continue
                seen_general.add(key)
                if n.get("tier", 3) < 3:
                    bump_notam_floor([n], _YELLOW, _YELLOW)   # flight-wide: caution floor only
                    txt = n.get("summary") or n.get("id", "")
                    general_block.append(f"  {sec.get('key', 'GEN')} T{n.get('tier')}: {txt}")

    if general_block:
        lines.append("FLIGHT-WIDE NOTAMs:")
        lines.extend(general_block)
        lines.append("")

    risk = _UNRANK[risk_rank]
    lines.append(f"DETERMINISTIC OVERALL RISK: {risk}")
    return "\n".join(lines), risk


def _call_claude(digest_text):
    """Ask Claude for the freeform brief. Returns prose, or None on failure."""
    import time as _time
    import anthropic

    client = anthropic.Anthropic()
    for attempt in range(_API_RETRIES):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=4000,  # room for adaptive thinking + the ~150-350 word brief
                thinking={"type": "adaptive"},  # aids the primary-vs-alternate cross-referencing
                system=_SYSTEM,
                messages=[{"role": "user", "content": digest_text}],
            )
            # Sonnet 5 returns thinking blocks first — take the text block, not content[0]
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            return text.strip() or None
        except Exception as e:
            if attempt == _API_RETRIES - 1:
                print(f"  WARN: HIRA synthesis failed after {_API_RETRIES} attempts ({e}) "
                      f"— falling back to raw digest")
                return None
            wait = 2 ** (attempt + 1)
            print(f"  WARN: HIRA API call failed ({e}) — retrying in {wait}s")
            _time.sleep(wait)
    return None


def generate(group_dirs):
    """On-demand HIRA generation (triggered by the map's HIRA button, not the pipeline).

    Builds the whole-flight digest + deterministic risk across every group, calls
    Sonnet, and — only on success — writes an identical hira.json into every group dir
    (the server-side cache) and returns the payload. On a Sonnet failure returns None
    and writes nothing, so the client's Retry re-attempts instead of caching a failure.
    """
    if not group_dirs:
        return None
    digest_text, risk = build_digest(group_dirs)

    brief = _call_claude(digest_text)
    if brief is None:
        return None   # do not cache a failure — the client offers Retry

    payload = {
        "risk": risk,
        "generated": True,
        "brief": brief,
        "digest_text": digest_text,
    }
    for group_dir in group_dirs:
        with open(os.path.join(group_dir, "hira.json"), "w") as f:
            json.dump(payload, f, indent=2)
    return payload
