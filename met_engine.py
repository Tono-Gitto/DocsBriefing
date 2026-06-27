"""
MET engine: parse TG921_MET.pdf + route.json → enriched airports.json.

Per airport:
  - METAR (SA line)
  - TAF condensed at reference time (BECMG/FM folded per CLAUDE.md §2)
  - Reference time = TAKEOFF + ACCT of nearest route waypoint (haversine)
"""

import json, math, os, re
from datetime import datetime, timedelta, timezone
import pdfplumber
from airport_coords import load_coords

HERE = os.path.dirname(os.path.abspath(__file__))
MET_PDF   = os.path.join(HERE, "Input", "TG921_MET.pdf")
ROUTE_JSON = os.path.join(HERE, "data", "route.json")
OUT_JSON  = os.path.join(HERE, "data", "airports.json")

# ETD 1245Z + 20 min taxi = takeoff 1305Z on 20 JUN 2026
TAKEOFF_UTC = datetime(2026, 6, 20, 13, 5, tzinfo=timezone.utc)

# ── MET PDF parsing ──────────────────────────────────────────────────────────

_HEADER_RE = re.compile(r"^([A-Z]{4})\s+-\s*([A-Z]{3,4})\s+-\s+(.+)")
_PAGE_HDR_RE = re.compile(
    r"^(\$B|Dispatch MET|_{5,}|\d{2}[A-Z]{3}\d{2}\s+THA\d+|TG\d+\s+\d{2}[A-Z]{3})"
)

def _clean_lines(pdf_path):
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.extend(text.split("\n"))
    return [l.strip() for l in lines if l.strip() and not _PAGE_HDR_RE.match(l.strip())]


def parse_met_pdf(pdf_path):
    """Return ({icao: {iata, name, metar, taf_raw}}, [ordered icao list])."""
    clean = _clean_lines(pdf_path)
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

    for line in clean:
        m = _HEADER_RE.match(line)
        if m:
            if mode == "ft":
                finalize("taf")
            buf = []
            current = m.group(1)
            if current not in airports:
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
                if mode == "header":
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

    if mode == "ft":
        finalize("taf")

    return airports, order


# ── Reference-time engine ────────────────────────────────────────────────────

def _haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(φ1) * math.cos(φ2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def compute_ref_time(lat, lon, route_pts):
    """Return (ref_datetime_utc, dist_nm) via nearest-waypoint haversine."""
    best_dist, best_acct = float("inf"), 0
    for pt in route_pts:
        d = _haversine_nm(lat, lon, pt["lat"], pt["lon"])
        if d < best_dist:
            best_dist, best_acct = d, pt["acct_min"]
    return TAKEOFF_UTC + timedelta(minutes=best_acct), best_dist


# ── TAF condensing (CLAUDE.md §2) ────────────────────────────────────────────

# Must try longer alternatives first so PROB30 TEMPO beats bare PROB30
_GROUP_RE = re.compile(
    r"\b(PROB30 TEMPO|PROB40 TEMPO|PROB30|PROB40|BECMG|TEMPO|FM\d{6})\b\s*(\d{4}/\d{4}|)"
)


def _ddhh(s):
    return int(s[:2]) * 1440 + int(s[2:4]) * 60


def _parse_groups(taf_raw):
    matches = list(_GROUP_RE.finditer(taf_raw))
    if not matches:
        # Strip header, return whole thing as base
        base = re.sub(r"^FT\s+\S+\s+\S+\s*", "", taf_raw).strip()
        return base, []

    base_raw = taf_raw[: matches[0].start()]
    base_text = re.sub(r"^FT\s+\S+\s+\S+\s*", "", base_raw).strip()

    groups = []
    for i, gm in enumerate(matches):
        gtype  = gm.group(1)
        window = gm.group(2)
        text_end = matches[i + 1].start() if i + 1 < len(matches) else len(taf_raw)
        gtext = taf_raw[gm.end(): text_end].strip()

        if gtype.startswith("FM"):
            # FM DDHHMM — time encoded in type token, no end
            start_min = int(gtype[2:4]) * 1440 + int(gtype[4:6]) * 60 + int(gtype[6:8])
            end_min = None
        elif window:
            parts = window.split("/")
            start_min, end_min = _ddhh(parts[0]), _ddhh(parts[1])
        else:
            continue  # malformed

        groups.append({"type": gtype, "start": start_min, "end": end_min, "text": gtext})

    return base_text, groups


def _fmt_min(total_min):
    dd, rem = divmod(total_min, 1440)
    return f"{dd:02d}{rem // 60:02d}Z"


def _fmt_window(start, end):
    return f"from {_fmt_min(start)}" if end is None else f"{_fmt_min(start)}-{_fmt_min(end)}"


def condense_taf(taf_raw, ref_dt):
    """
    Returns (base_str, becmg_in_progress|None, [active_overlays]).
    BECMG/FM completed before ref_dt fold into the baseline.
    """
    base_text, groups = _parse_groups(taf_raw)
    ref_min = ref_dt.day * 1440 + ref_dt.hour * 60 + ref_dt.minute
    baseline = base_text
    becmg_prog = None
    overlays = []

    for g in sorted(groups, key=lambda x: x["start"]):
        t = g["type"]
        if t == "BECMG" or t.startswith("FM"):
            if g["end"] is None:          # FM: complete once past start
                if ref_min >= g["start"]:
                    baseline = g["text"]
            else:
                if ref_min >= g["end"]:
                    baseline = g["text"]  # transition complete → fold
                elif g["start"] <= ref_min < g["end"]:
                    becmg_prog = g        # in progress right now
        else:  # TEMPO / PROB30 TEMPO / PROB40 TEMPO / bare PROB
            if g["end"] is not None and g["start"] <= ref_min < g["end"]:
                overlays.append(g)

    becmg_out = (
        {"text": becmg_prog["text"],
         "window": _fmt_window(becmg_prog["start"], becmg_prog["end"])}
        if becmg_prog else None
    )
    overlay_out = [
        {"type": g["type"], "text": g["text"],
         "window": _fmt_window(g["start"], g["end"])}
        for g in overlays
    ]
    return baseline, becmg_out, overlay_out


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    with open(ROUTE_JSON) as f:
        route_pts = json.load(f)

    coords = load_coords()
    met_data, order = parse_met_pdf(MET_PDF)

    out = []
    for icao in order:
        d = met_data[icao]
        if icao not in coords:
            print(f"  WARN: no coords for {icao} ({d['name']})")
            continue

        lat, lon = coords[icao]
        ref_dt, dist_nm = compute_ref_time(lat, lon, route_pts)

        taf_base, becmg_prog, active_overlays = None, None, []
        if d["taf_raw"]:
            taf_base, becmg_prog, active_overlays = condense_taf(d["taf_raw"], ref_dt)

        out.append({
            "icao": icao,
            "iata": d["iata"],
            "name": d["name"],
            "runway_info": d.get("runway_info"),
            "lat": lat,
            "lon": lon,
            "ref_time": ref_dt.strftime("%H%MZ"),
            "dist_nm": round(dist_nm),
            "metar": d["metar"],
            "taf_base": taf_base,
            "becmg_in_progress": becmg_prog,
            "active_overlays": active_overlays,
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
