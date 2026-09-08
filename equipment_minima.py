"""
OM-A §8.1.3.3.6 — Effect on landing minima of temporarily failed or downgraded
ground equipment, for operations WITHOUT LVO approval.  (docs/adr/0006)

This module answers one question: given an airport's raw NOTAMs and the window
the aircraft will be near it, which §8.1.3.3.6 table facilities are failed, on
which runway, and what does the table say that costs?

It does NOT produce a required RVR.  That needs the aerodrome's DH/MDH, which
lives in the hand-entered store (`data/aerodrome_minima.json`, ADR 0005) and is
live-editable in the browser — so the final lookup and arithmetic happen
client-side, and this module emits findings plus the lookup table as data.  That
is ADR 0005 §4's seam ("Python resolves and filters, the client does the
arithmetic"), unchanged.

Three boundaries worth knowing before editing:

* **Primary-aid removal is not in scope.**  `ILS RWY 18 U/S` takes the approach
  away; it does not degrade an approach you are still flying, and it is not in
  the table.  It keeps its ordinary T1 tile and produces no finding — which
  falls out of the patterns rather than needing a guard, since no row matches
  ILS/LOC/VOR/NDB/GLS and net 2 is anchored on lighting and marker vocabulary.
  The test is membership in the table's 14 rows, *not* "is it a navaid":
  `Outer marker`, `Middle marker`, `DME` and `Navaid stand-by transmitter` are
  listed facilities and do produce findings (ADR 0006 §1).
* **A raised OCA/H NOTAM is not in scope** either (ADR 0006 §1), so the DH/MDH
  has exactly one source — the store — and never a NOTAM.
* **Nothing here is AI-driven.**  The output is a number a crew acts on, and an
  AI miss is silent.  Same reasoning as `notam_engine._classify_tier`'s regex
  tables and ADR 0005 §5's phenomena pair.
"""

import re
from datetime import timedelta


# ── The "RVR versus DH/MDH" table (OM-A §8.1.3.3.2) ───────────────────────────
#
# Served to the client as data rather than mirrored as a JS constant (ADR 0006
# §9): 25 bands × 4 classes whose boundaries fail *silently* when off by one,
# and the OM-A publishes two worked examples that pin them.  Table 3's six rows
# stay in index.html — six additions, already shipped, not worth moving.
#
# Each entry is (band_low_ft, band_high_ft, {class: rvr_m}).  The final band is
# open-ended ("661 and above") and carries band_high = None.

LIGHTING_CLASSES = ("FALS", "IALS", "BALS", "NALS")

RVR_VS_DH_MDH = [
    (200, 210, (550, 750, 1000, 1200)),
    (211, 240, (550, 800, 1000, 1200)),
    (241, 250, (550, 800, 1000, 1300)),
    (251, 260, (600, 800, 1100, 1300)),
    (261, 280, (600, 900, 1100, 1300)),
    (281, 300, (650, 900, 1200, 1400)),
    (301, 320, (700, 1000, 1200, 1400)),
    (321, 340, (800, 1100, 1300, 1500)),
    (341, 360, (900, 1200, 1400, 1600)),
    (361, 380, (1000, 1300, 1500, 1700)),
    (381, 400, (1100, 1400, 1600, 1800)),
    (401, 420, (1200, 1500, 1700, 1900)),
    (421, 440, (1300, 1600, 1800, 2000)),
    (441, 460, (1400, 1700, 1900, 2100)),
    (461, 480, (1500, 1800, 2000, 2200)),
    (481, 500, (1500, 1800, 2100, 2300)),
    (501, 520, (1600, 1900, 2100, 2400)),
    (521, 540, (1700, 2000, 2200, 2400)),
    (541, 560, (1800, 2100, 2300, 2400)),
    (561, 580, (1900, 2200, 2400, 2400)),
    (581, 600, (2000, 2300, 2400, 2400)),
    (601, 620, (2100, 2400, 2400, 2400)),
    (621, 640, (2200, 2400, 2400, 2400)),
    (641, 660, (2300, 2400, 2400, 2400)),
    (661, None, (2400, 2400, 2400, 2400)),
]


def rvr_table_payload():
    """The lookup table in the shape the client consumes.  Written into each
    group dir as `rvr_table.json` (not served from /api/*, which sw.js treats as
    network-only) so ADR 0003's existing manifest precache covers it offline
    with no service-worker change."""
    return {
        "classes": list(LIGHTING_CLASSES),
        "bands": [
            {"low": lo, "high": hi, "rvr": dict(zip(LIGHTING_CLASSES, vals))}
            for lo, hi, vals in RVR_VS_DH_MDH
        ],
    }


def lookup_rvr(dh_mdh_ft, lighting_class):
    """RVR (m) for a DH/MDH and class of lighting facility, or None when the
    height is below the table's floor (200 ft — Type B CAT I's own minimum DH,
    §8.1.3.3.1's System Minima).  None must render as "cannot determine", never
    as an unrestricted pass."""
    if dh_mdh_ft is None or lighting_class not in LIGHTING_CLASSES:
        return None
    idx = LIGHTING_CLASSES.index(lighting_class)
    for lo, hi, vals in RVR_VS_DH_MDH:
        if dh_mdh_ft >= lo and (hi is None or dh_mdh_ft <= hi):
            return vals[idx]
    return None


def approach_type_for(dh_mdh_ft):
    """OM-A §1.x: Type A is an instrument approach operation with an MDH or DH
    *at or above* 250 ft; Type B is an operation with a DH *below* 250 ft.  So
    the table's column is definitional, not inferred, and needs no stored field
    (ADR 0006 §5).  Returns "A" / "B", or None when no height is known."""
    if dh_mdh_ft is None:
        return None
    return "A" if dh_mdh_ft >= 250 else "B"


# ── Outcome vocabulary ────────────────────────────────────────────────────────
#
# The table's cells are not all numbers.  Four shapes, plus a conditional
# wrapper for the cells that cannot be resolved from NOTAM text alone.
# The client combines these per runway (ADR 0006 §7):
#
#     NOT ALLOWED ⊐ max(floors, class lookup, charted) ⊐ NO EFFECT

def _no_effect(note=None):
    return {"kind": "no_effect", "note": note}


def _downgrade(cls, note=None):
    return {"kind": "class", "class": cls, "note": note} if note else {"kind": "class", "class": cls}


def _floor(rvr_m, note=None):
    return {"kind": "floor", "rvr_m": rvr_m, "note": note}


def _not_allowed(note=None):
    return {"kind": "not_allowed", "note": note}


def _conditional(*branches):
    """branches: (label, outcome) pairs, BOTH rendered, never resolved.

    Used for the cells that turn on something this tool has no source for:
    day/night (the OM-A delegates it — "See LIDO RM, LAT – Sunrise and Sunset
    Table"; computing civil twilight ourselves would be the same Lido
    substitution ADR 0005 declared out of reach, at the exact boundary where the
    answer flips between "no effect" and "not allowed"), and the marker/DME
    escape clauses, which turn on how a specific procedure is constructed."""
    return {"kind": "conditional",
            "branches": [{"label": lbl, "outcome": out} for lbl, out in branches]}


# Flight director / Autoland IS resolved rather than left conditional: this is a
# B777 tool briefing THAI B777 packages and every airframe in flight_info.acft
# has both.  The reason is carried in the note so the MEL exception stays
# catchable (ADR 0006 §7).
_FD_NOTE = "flight director / Autoland available on type"


# ── The table itself — OM-A §8.1.3.3.6, 14 rows ───────────────────────────────
#
# `pattern` is the NOTAM-vocabulary alias set for that row.  `b` / `a` are the
# Type B and Type A columns; both are emitted, and the client selects with
# approach_type_for(base_height_ft) — because the height lives in the
# live-editable store, not here.
#
# Order matters: the first row whose pattern matches claims the text, so the
# more specific approach-light rows ("except the last 210 m") precede the
# general one.

_FACILITIES = [
    {
        "key": "apch_lights_except_210",
        "label": "Approach lights except the last 210 m",
        "pattern": r"(?:APCH|APPROACH)\s+(?:LGT|LIGHT)\w*[^.\n]{0,40}?EXCEPT[^.\n]{0,30}?210\s*M",
        "b": _downgrade("BALS"), "a": _downgrade("BALS"),
    },
    {
        "key": "apch_lights_except_420",
        "label": "Approach lights except the last 420 m",
        "pattern": r"(?:APCH|APPROACH)\s+(?:LGT|LIGHT)\w*[^.\n]{0,40}?EXCEPT[^.\n]{0,30}?420\s*M",
        "b": _downgrade("IALS"), "a": _downgrade("IALS"),
    },
    {
        "key": "apch_lights_standby_power",
        "label": "Standby power for approach lights",
        "pattern": r"(?:STANDBY|STBY)\s+(?:PWR|POWER)[^.\n]{0,30}?(?:APCH|APPROACH|ALS|PALS|SALS)",
        "b": _no_effect(), "a": _no_effect(),
    },
    {
        # PALS = precision approach lighting system, SALS = simple approach
        # lighting system — the two forms THAI NOTAMs actually print (the OM-A
        # table says only "Approach lights").  ALSF/MALSR/HIALS/MIALS/ODALS are
        # the ICAO/FAA spellings that show up in foreign NOTAMs.
        "key": "apch_lights",
        "label": "Approach lights",
        "pattern": (r"\b(?:PALS|SALS|ALSF\d?|MALSR|MALSF|HIALS|MIALS|ODALS|APL"
                    r"|(?:APCH|APPROACH)\s+(?:LGT|LIGHT)\w*"
                    r"|(?:LGT|LIGHT)\w*\s+(?:APCH|APPROACH)\s+SYS\w*"
                    r"|\bALS\b)"),
        # Default: a bare "U/S"/"PARTLY U/S" with no stated remaining length is
        # the manual's own Example 1 (APL unserviceable -> NALS). Overridden
        # below when the body also states an available length (RJFF's
        # RJAAF0920/26: "PALS ... PARTLY U/S ... RMK: AVBL APCH LGT LEN 427M"
        # is not a total failure and must not be classed as one).
        "b": _downgrade("NALS"), "a": _downgrade("NALS"),
    },
    {
        # MUST precede edge_thr_end_lights: `TWY EDGE LGT U/S` is taxiway
        # lighting ("no effect"), not runway edge lights ("night: not allowed").
        # First-match-wins on the character span is what keeps them apart, so
        # the order of these two rows is load-bearing — TG638's VTBDC4088/26
        # is the fixture that catches it.
        "key": "taxiway_lights",
        "label": "Taxiway lighting system",
        # Both word orders occur: `TWY EDGE LGT U/S` (TG638) and
        # `LGT FOR TWY B1 THRU B9 U/S` (TG677, a dozen times). Without the
        # second, those all fell through to net 2 and reported as "minima
        # cannot be re-determined" when the table's answer is plainly "no
        # effect".
        # Three word orders occur, and the third matters for correctness rather
        # than tidiness: `CENTRE LINE LIGHTS TWY A4W ... U/S` is TAXIWAY centre
        # line lighting ("no effect"), not the table's runway Centre line lights
        # row (a 750 m floor in the Type A column). Claiming it here — this row
        # precedes centre_line_lights, and first-match-wins on the span — is
        # what keeps a taxiway outage from inflating a required RVR.
        "pattern": (r"\b(?:(?:TWY|TAXIWAY)\s+(?:EDGE\s+|CL\s+|CENTRE\s*LINE\s+)?(?:LGT|LIGHT)\w*"
                    r"|(?:LGT|LIGHT)\w*\s+(?:FOR\s+)?(?:TWY|TAXIWAY)\b"
                    r"|(?:CL|(?:CENTRE|CENTER)\s*LINE)\s+(?:LGT|LIGHT)\w*\s+(?:FOR\s+)?(?:TWY|TAXIWAY)\b)"),
        "b": _no_effect(), "a": _no_effect(),
    },
    {
        "key": "centre_line_lights_30m",
        "label": "Centre line lights spacing increased to 30 m",
        "pattern": r"(?:RCLL|(?:RWY\s+)?(?:CENTRE|CENTER)\s*LINE\s+(?:LGT|LIGHT)\w*)[^.\n]{0,40}?30\s*M\s+SPACING",
        "b": _no_effect(), "a": _no_effect(),
    },
    {
        "key": "centre_line_lights",
        "label": "Centre line lights",
        "pattern": r"\b(?:RCLL|CL\s+(?:LGT|LIGHT)\w*|(?:RWY\s+)?(?:CENTRE|CENTER)\s*LINE\s+(?:LGT|LIGHT)\w*)",
        # Type B: "No effect if flight director or Autoland otherwise RVR 750 m"
        # Type A: "No effect but the minimum RVR should be 750 m."
        "b": _no_effect(_FD_NOTE),
        "a": _floor(750),
    },
    {
        "key": "tdz_lights",
        "label": "TDZ lights",
        "pattern": r"\b(?:RTZL|TDZ\s+(?:LGT|LIGHT)\w*|TOUCHDOWN\s+ZONE\s+(?:LGT|LIGHT)\w*)",
        "b": _no_effect(_FD_NOTE), "a": _no_effect(),
    },
    {
        "key": "edge_thr_end_lights",
        "label": "Edge lights, threshold lights and runway end lights",
        "pattern": (r"\b(?:REDL|RENL|RTHL"
                    r"|(?:RWY\s+)?EDGE\s+(?:LGT|LIGHT)\w*"
                    r"|THR\s+(?:LGT|LIGHT)\w*|THRESHOLD\s+(?:LGT|LIGHT)\w*"
                    r"|(?:RWY|RUNWAY)\s+END\s+(?:LGT|LIGHT)\w*)"),
        "b": _conditional(("Day", _no_effect()), ("Night", _not_allowed())),
        "a": _conditional(("Day", _no_effect()), ("Night", _not_allowed())),
    },
    {
        "key": "outer_marker",
        "label": "Outer marker",
        "pattern": r"\b(?:OUTER\s+MARKER|\bOM\b(?!\w))",
        "b": _conditional(
            ("height vs glide path checkable by other means (e.g. DME fix)", _no_effect()),
            ("otherwise (CAT I)", _not_allowed("APV — not applicable")),
        ),
        "a": _conditional(
            ("not used as FAF", _no_effect()),
            ("used as FAF and FAF cannot be identified", _not_allowed("NPA operations cannot be conducted")),
        ),
    },
    {
        "key": "middle_marker",
        "label": "Middle marker (ILS only)",
        "pattern": r"\b(?:MIDDLE\s+MARKER|\bMM\b(?!\w))",
        "b": _no_effect(),
        "a": _conditional(("not used as MAPt", _no_effect()),
                          ("used as MAPt", _not_allowed())),
    },
    {
        "key": "dme",
        "label": "DME",
        # A bare DME.  `VOR/DME U/S` or `NDB/DME U/S` names a primary aid and is
        # caught by _PRIMARY_AID_RE before ever reaching this table.
        "pattern": r"\bDME\b",
        "b": _conditional(("replaced by RNAV (GNSS) information or the outer marker", _no_effect()),
                          ("otherwise", _not_allowed())),
        "a": _conditional(("replaced by RNAV (GNSS) information or the outer marker", _no_effect()),
                          ("otherwise", _not_allowed())),
    },
    {
        "key": "navaid_standby_tx",
        "label": "Navaid stand-by transmitter",
        "pattern": r"\b(?:STANDBY|STBY)\s+(?:TRANSMITTER|TX)",
        "b": _no_effect(), "a": _no_effect(),
    },
    {
        "key": "rvr_assessment",
        "label": "RVR assessment systems",
        # A qualifier is REQUIRED. Bare `\bRVR\b` matched lines merely stating a
        # value ("RVR 350M OR ...") on a line that carried a failure verb for
        # something else entirely.
        "pattern": r"\bRVR\b\s+(?:ASSESSMENT|SYS\w*|EQPT|EQUIPMENT|SENSOR\w*|RWY|AT)\b",
        "b": _no_effect(), "a": _no_effect(),
    },
]

for _f in _FACILITIES:
    _f["re"] = re.compile(_f["pattern"], re.I)


# ── Net 2: the unmapped catch ─────────────────────────────────────────────────
#
# §8.1.3.3.6: "Only those facilities mentioned in Table below should be
# acceptable to be used to determine the effect of temporarily failed of
# downgraded equipment", and "Multiple failures of runway lights other than
# those indicated in the table should not be acceptable."
#
# So an equipment failure this module cannot map is a *finding*, not silence —
# it renders as "not among the facilities §8.1.3.3.6 permits for this
# determination".  It doubles as the module's own bug report: anything landing
# here across the fixture NOTAM PDFs is a missing alias above.
#
# Anchored on LIGHTING and MARKER vocabulary only — deliberately NOT on generic
# "SYSTEM"/"EQUIPMENT".  §8.1.3.3.6's closed-list sentence is about *runway
# lights*, and a broader net drags in equipment that has no bearing on landing
# minima at all (TG638 alone offers a stand-docking VDGS and a Doppler weather
# radar, both U/S, neither remotely a §8.1.3.3.6 facility).  Reporting those as
# "minima cannot be re-determined" would be a false alarm, not honesty.
_UNMAPPED_RE = re.compile(
    r"\b(?:[A-Z][A-Z0-9/\-']{1,14}\s+){0,3}"
    r"(?:LGT|LGTS|LIGHT\w*|BCN|BEACON|MARKER)\b",
    re.I,
)

# Failure verbs.  "MAINT" alone is a *cause* ("U/S DUE TO MAINT"), never the
# verb, so it is deliberately absent.
_FAILURE_RE = re.compile(
    r"\b(?:U/S|UNSERVICEABLE|OUT\s+OF\s+SERVICE|DOWNGRADED|INOP(?:ERATIVE)?"
    r"|NOT\s+AVBL|NOT\s+AVAILABLE|WITHDRAWN|SUSPENDED)\b",
    re.I,
)

# Primary approach aids — removing one takes the approach away rather than
# degrading it, so it is out of scope (ADR 0006 §1).
#
# There is deliberately NO broad "does this line mention a navaid" guard.  It
# was tried and it silently ate the commonest NOTAM spelling of approach
# lights: `APCH LGT RWY 36 U/S` matched a bare \bAPCH\b and vanished — a false
# negative in a feature whose whole design premise is that false negatives are
# the dangerous direction.  The guard is unnecessary anyway: no row's pattern
# matches ILS/LOC/VOR/NDB/GLS, and net 2 is anchored on lighting and marker
# vocabulary only, so `ILS RWY 18 U/S` yields nothing all by itself.
#
# What IS still needed is narrower: a DME named as part of a compound navaid
# (`ILS DME`, `VOR/DME`, `NDB/DME`) is that aid's own ranging channel, not the
# table's standalone DME facility.  Only that case is excluded.  D11 governs
# the rest — a line naming both an ILS and an approach light still yields the
# light, because the test is membership in the table's 14 rows, not "is a
# navaid mentioned nearby".
_COMPOUND_DME_RE = re.compile(
    r"\b(?:ILS|LOC|LLZ|VOR|DVOR|NDB|TACAN|VORTAC|MLS|GLS)\s*[/\- ]?\s*DME\b",
    re.I,
)

_RWY_RE = re.compile(r"\bRWY\s*(\d{2}[LRC]?(?:/\d{2}[LRC]?)*)", re.I)

# A NOTAM'd approach-light failure that also states a remaining serviceable
# length is not the manual's Example 1 (total failure -> NALS) — it is the
# same "except the last N m" idea the table's two named rows already encode
# (`except the last 210 m` -> BALS, `except the last 420 m` -> IALS), just
# phrased as the length still AVAILABLE rather than the length lost. Both
# phrasings describe the same fact, so both must resolve through the
# "Approach lighting systems" table (OM-A §8.1.3.3.2), not through the
# full-failure row. Anchored narrowly on the one wording seen in the fixture
# NOTAMs (`RMK: AVBL APCH LGT LEN 427M`) — this module's classification only
# ever gets more permissive here, never more restrictive, so a missed variant
# fails safe (falls through to NALS) while a false-positive match would not.
_AVBL_APCH_LEN_RE = re.compile(
    r"AVBL\s+(?:APCH|APPROACH)\s+(?:LGT|LIGHT)\w*\s+LEN\s*(\d+)\s*M",
    re.I,
)


def _apch_class_for_length(length_m):
    """OM-A "Approach lighting systems" table (§8.1.3.3.2): FALS >= 720 m,
    IALS 420-719 m, BALS 210-419 m, NALS < 210 m or no lights at all — the
    same brackets the failure table's two named rows already pin (BALS at the
    210 m floor, IALS at the 420 m floor)."""
    if length_m >= 720:
        return "FALS"
    if length_m >= 420:
        return "IALS"
    if length_m >= 210:
        return "BALS"
    return "NALS"


def _runways_in(text):
    """[(char_pos, 'nn[L|R|C]'), ...] — `RWY02R/20L` yields both ends."""
    out = []
    for m in _RWY_RE.finditer(text):
        for part in m.group(1).split("/"):
            out.append((m.start(), part.upper()))
    return out


# ── Time gate — ETA ± 1 h overlap (ADR 0006 §8) ───────────────────────────────

def _split_daily(start_min, end_min):
    """A possibly-midnight-crossing HHMM range as 1–2 [a, b] minute intervals."""
    if end_min < start_min:
        return [(start_min, 1439), (0, end_min)]
    return [(start_min, end_min)]


def _band_minute_spans(band_start, band_end):
    """The band as (date, start_min, end_min) pieces, one per calendar day it
    touches.  The band is 2 h wide, so this is 1 or 2 pieces."""
    pieces = []
    day = band_start.date()
    while day <= band_end.date():
        lo = band_start.hour * 60 + band_start.minute if day == band_start.date() else 0
        hi = band_end.hour * 60 + band_end.minute if day == band_end.date() else 1439
        pieces.append((day, lo, hi))
        day = day + timedelta(days=1)
    return pieces


def _overlaps_daily(daily_windows, band_start, band_end):
    if not daily_windows:
        return True
    spans = _band_minute_spans(band_start, band_end)
    for s, e in daily_windows:
        for a, b in _split_daily(s, e):
            for _day, lo, hi in spans:
                if a <= hi and b >= lo:
                    return True
    return False


def _overlaps_date_schedule(date_schedules, band_start, band_end):
    if not date_schedules:
        return True
    for day, lo, hi in _band_minute_spans(band_start, band_end):
        for month, dom, s, e in date_schedules:
            if month != day.month or dom != day.day:
                continue
            for a, b in _split_daily(s, e):
                if a <= hi and b >= lo:
                    return True
    return False


def is_active_in_band(n, band_start, band_end):
    """True if the NOTAM's validity overlaps [band_start, band_end].

    All three window kinds notam_engine parses are honoured — absolute,
    daily_windows and date_schedules — because _effective_tier gates on all
    three (notam_engine.py:270) and a gate reading fewer would over-apply
    findings on exactly the NOTAMs whose schedules are most restrictive.

    Overlap, not containment: the feature's output is compared against a
    forecast spanning ETA±1h, so assessing equipment at a single instant would
    leave a required figure valid for one minute of the window it is measured
    against.  Deliberately divergent from the tiles' point-in-time
    `_is_active` — see ADR 0006 §8.
    """
    ws, we = n.get("win_start"), n.get("win_end")
    if ws is not None:
        if ws > band_end:
            return False
        if we is not None and we < band_start:
            return False
    if not _overlaps_daily(n.get("daily_windows") or [], band_start, band_end):
        return False
    if not _overlaps_date_schedule(n.get("date_schedules") or [], band_start, band_end):
        return False
    return True


# ── Extraction ────────────────────────────────────────────────────────────────

def _facility_hits(text):
    """[(start, end, facility_or_None)] for the whole body, first-match-wins per
    character span, sorted by position.  facility None marks a net-2 hit."""
    hits = []
    claimed = []

    def _free(s, e):
        return not any(s < ce and e > cs for cs, ce in claimed)

    for fac in _FACILITIES:
        for m in fac["re"].finditer(text):
            if _free(m.start(), m.end()):
                claimed.append((m.start(), m.end()))
                hits.append((m.start(), m.end(), fac))

    for m in _UNMAPPED_RE.finditer(text):
        if _free(m.start(), m.end()):
            claimed.append((m.start(), m.end()))
            hits.append((m.start(), m.end(), None))

    hits.sort(key=lambda h: h[0])
    return hits


def _segment_bounds(hits, idx, text_len):
    """A hit owns the text from its own end to the next hit's start — that is
    what associates `RWY 18` with PALS and `RWY 36` with SALS in
    `PALS CAT 1 RWY 18 AND SALS RWY 36 U/S`."""
    _s, e, _f = hits[idx]
    nxt = hits[idx + 1][0] if idx + 1 < len(hits) else text_len
    return e, nxt


def _runways_for(hits, idx, text):
    """Runways for one facility hit: every runway inside its own segment; if the
    segment names none, the single nearest runway token by character distance
    (so `RWY 18 PALS U/S`, with the designator ahead of the facility, still
    resolves); otherwise None."""
    s, _e, _f = hits[idx]
    lo, hi = _segment_bounds(hits, idx, len(text))
    in_seg = [r for pos, r in _runways_in(text) if lo <= pos < hi]
    if in_seg:
        seen, out = set(), []
        for r in in_seg:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return out
    allr = _runways_in(text)
    if allr:
        return [min(allr, key=lambda pr: abs(pr[0] - s))[1]]
    return [None]


def extract_findings(notam, band_start, band_end):
    """Findings for ONE raw NOTAM dict (as parse_notam_pdf yields it), or [] .

    Returns one finding per (facility, runway).  Never raises on malformed
    content — an unparseable body simply yields nothing, the same graceful-miss
    philosophy as notam_anchors/met_anchors (ADR 0002).
    """
    try:
        body = "\n".join(notam.get("body", "").split("\n"))
        if not body.strip():
            return []
        if not _FAILURE_RE.search(body):
            return []
        if not is_active_in_band(notam, band_start, band_end):
            return []

        avbl_len_m = None
        m = _AVBL_APCH_LEN_RE.search(body)
        if m:
            avbl_len_m = int(m.group(1))

        out = []
        # Line-by-line, not one flattened blob.  A NOTAM line is the unit that
        # carries a failure verb: `PALS CAT 1 RWY 18 AND SALS RWY 36 U/S` has
        # ONE `U/S` governing two facilities, so requiring the verb inside each
        # facility's own segment would silently drop the PALS half (TG638's
        # VTBDC3302/26 is the fixture that catches it).  Scoping the verb to the
        # line keeps both, without letting a failure on line 1 contaminate an
        # unrelated facility named on line 7.
        for raw_line in body.split("\n"):
            line = " ".join(raw_line.split())
            if not line or not _FAILURE_RE.search(line):
                continue
            hits = _facility_hits(line)
            for i, (s, _e, fac) in enumerate(hits):
                _lo, hi = _segment_bounds(hits, i, len(line))
                segment = line[s:hi]
                # A DME that is part of a compound navaid is that aid's ranging
                # channel, not the table's standalone DME facility.  Checked on
                # the text leading up to the match, since the qualifier precedes
                # it (`VOR/DME`, `ILS DME`).
                if fac is not None and fac["key"] == "dme":
                    if _COMPOUND_DME_RE.search(line[max(0, s - 12):s + 4]):
                        continue
                outcome_b = fac["b"] if fac else {"kind": "unmapped"}
                outcome_a = fac["a"] if fac else {"kind": "unmapped"}
                if fac is not None and fac["key"] == "apch_lights" and avbl_len_m is not None:
                    note = f"{avbl_len_m} m of approach lights available (NOTAM remark)"
                    cls = _apch_class_for_length(avbl_len_m)
                    outcome_b = _downgrade(cls, note)
                    outcome_a = _downgrade(cls, note)
                for rwy in _runways_for(hits, i, line):
                    out.append({
                        "facility":     fac["key"] if fac else "unmapped",
                        "label":        fac["label"] if fac else "Unlisted equipment",
                        "mapped":       fac is not None,
                        "runway":       rwy,
                        "matched_text": segment[:120].strip(),
                        "outcome_b":    outcome_b,
                        "outcome_a":    outcome_a,
                    })
        return out
    except Exception:
        return []


def findings_for_airport(raw_notams, band_start, band_end, window_fmt=None):
    """All findings for one airport's raw NOTAM list, deduplicated on
    (notam id, facility, runway).

    `raw_notams` must come from `notam_engine.parse_notam_pdf()`, NOT from
    `airports.json` — app.py filters that list by point-in-time `ref_dt` before
    writing it and keeps only {id, tier, body, window}, so win_start/win_end/
    daily_windows are gone and the rows this band gate needs have already been
    discarded (ADR 0006 §8).
    """
    seen, out = set(), []
    for n in raw_notams or []:
        for f in extract_findings(n, band_start, band_end):
            key = (n.get("id"), f["facility"], f["runway"])
            if key in seen:
                continue
            seen.add(key)
            f = dict(f)
            f["notam_id"] = n.get("id")
            f["window"] = window_fmt(n) if window_fmt else None
            out.append(f)
    # Mapped findings first, then by facility label — so the block's Applicable
    # list leads with something actionable and net-2 noise sinks.
    out.sort(key=lambda f: (not f["mapped"], f["label"], f["runway"] or ""))
    return out
