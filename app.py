"""
Flask web wrapper for the Preflight Briefing Decoder.

Routes:
  GET  /              → redirect /upload
  GET  /upload        → upload.html
  POST /upload        → validate + save 3 PDFs → start background pipeline → /progress/<id>
  GET  /progress/<id> → polling progress page
  GET  /api/status/<id> → JSON pipeline status
  GET  /map           → index.html (MVP Leaflet map, unmodified)
  GET  /data/<f>      → serve JSON from current run dir (fallback: data/)

Security:
  ANTHROPIC_API_KEY loaded from .env via python-dotenv — never sent to browser.
  Uploads validated: PDF extension only, 50 MB cap, secure_filename + UUID-namespaced dirs.
  uploads/ and runs/ have no browse route.
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pdfplumber
from dotenv import load_dotenv
from _utils import HAIKU_MODEL
from flask import (
    Flask, Response, redirect, render_template_string, request,
    send_file, send_from_directory,
)
from werkzeug.utils import secure_filename

# Load .env before anything else so ANTHROPIC_API_KEY is in os.environ
load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # ensure "import parse_ofp" etc. resolve from this directory

UPLOAD_DIR = os.path.join(HERE, "uploads")
RUNS_DIR   = os.path.join(HERE, "runs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RUNS_DIR,   exist_ok=True)

_DEFAULT_TAXI_MIN    = 20    # fallback when OFP TAXI line is absent; shifts all ref_times if wrong
_FIR_FLIGHT_WINDOW_H = 24   # FIR NOTAMs checked this many hours past the last leg takeoff
_FIR_EXCLUSION_NM    = 10.0 # FIR diamond placed outside this radius of any airport

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap

_lock = threading.Lock()
_current_run: dict = {
    "run_id": None, "status": "idle", "messages": [], "error": None,
    "leg_count": 1, "group_count": 1,
}


# ── OFP constant extraction ───────────────────────────────────────────────────

_DATE_RE   = re.compile(r"\b(\d{2})([A-Z]{3})(\d{2})\b")           # 20JUN26
_ETD_RE    = re.compile(r"\bETD:\S+/(\d{2})(\d{2})\b")            # ETD:20JUN/1245
_TAXI_RE   = re.compile(r"\bTAXI\s+\d+\s+(\d{2}):(\d{2})\b")     # TAXI 660 00:20
_FLT_RE    = re.compile(r"\bFLT:\s+(\d{2})(\d{2})\b")             # FLT: 1021
_FLIGHT_RE = re.compile(r"\bTG\s*(\d+)\b")                        # TG 921
_DEP_RE    = re.compile(r"\d{2}[A-Z]{3}\d{2}\s+([A-Z]{4})/")     # 20JUN26 EDDF/FRA
_CS_RE     = re.compile(r"CS:\s+\S+\s+(\S+)\s+([A-Z]{4})/")      # CS: THA921 B773E VTBS/
_REG_RE    = re.compile(r"\b([A-Z]{5})/[A-Z]+\s+FLT:")

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5,  "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


_ALTN_TABLE_HDR  = re.compile(r"\bALTN\s+DIST\s+TIME\s+FL\s+FUEL\b")
_ALTN_ROUTE_HDR  = re.compile(r"\bALTN\s+ROUTE\s+TEXT\s+DIST\s+TIME\s+FL\s+FUEL\b")
_ALTN_ICAO_ROW   = re.compile(r"\b([A-Z]{4})(?:\s+\(F\))?\s+\d{1,4}\s+\d{2}:\d{2}\b")
_ERA_SLASH_RE    = re.compile(r"\bERA/([A-Z]{4})\b")
_ERA_FUEL_RE     = re.compile(r"\bFUEL\s+ERA\s+\(([A-Z]{4})\)")
_RCF_ALTN_RE     = re.compile(r"ROUTE TO SECONDARY DESTINATION ALTERNATE\s+([A-Z]{4})/")
_RCF_DEST_RE     = re.compile(r"ROUTE TO SECONDARY DESTINATION\s+([A-Z]{4})/")


def _read_ofp(ofp_path):
    """Open OFP PDF once; return (page1_text: str, all_lines: list[str])."""
    with pdfplumber.open(ofp_path) as pdf:
        page1_text = pdf.pages[0].extract_text() or ""
        all_lines = []
        for page in pdf.pages:
            all_lines.extend((page.extract_text() or "").splitlines())
    return page1_text, all_lines


def _extract_alternates(all_lines):
    """
    Parse alternate airport lists from pre-extracted OFP lines.
    Returns dict: {dest_altn: [...], era: [...], rcf_dest: str|None, rcf_altn: [...]}.
    All lists contain 4-letter ICAO codes. Empty when the section is absent.
    """
    dest_altn = []
    era       = []
    rcf_dest  = None
    rcf_altn  = []
    seen_altn = set()

    in_altn_table = False
    for line in all_lines:
        stripped = line.strip()

        if _ALTN_ROUTE_HDR.search(stripped):
            in_altn_table = False
            continue

        if _ALTN_TABLE_HDR.search(stripped):
            in_altn_table = True
            continue

        if in_altn_table:
            m = _ALTN_ICAO_ROW.search(stripped)
            if m:
                icao = m.group(1)
                if icao not in seen_altn:
                    dest_altn.append(icao)
                    seen_altn.add(icao)
            else:
                in_altn_table = False

        # ERA — scan every line regardless
        for m in _ERA_SLASH_RE.finditer(stripped):
            ic = m.group(1)
            if ic not in era:
                era.append(ic)
        for m in _ERA_FUEL_RE.finditer(stripped):
            ic = m.group(1)
            if ic not in era:
                era.append(ic)

        # RCF — must check ALTERNATE pattern before DESTINATION pattern
        m = _RCF_ALTN_RE.search(stripped)
        if m:
            ic = m.group(1)
            if ic not in rcf_altn:
                rcf_altn.append(ic)
            continue
        m = _RCF_DEST_RE.search(stripped)
        if m and rcf_dest is None:
            rcf_dest = m.group(1)

    return {"dest_altn": dest_altn, "era": era, "rcf_dest": rcf_dest, "rcf_altn": rcf_altn}


def _extract_ofp_constants(page1_text):
    """
    Parse OFP page-1 text. Returns (etd_utc: datetime, taxi_min: int, flight_time_min: int).
    Raises ValueError if essential fields (date, ETD, FLT) are missing.
    """
    text = page1_text
    dm = _DATE_RE.search(text)
    if not dm:
        raise ValueError("Cannot find flight date (DDMMMYY) in OFP page 1")
    mon = _MONTHS.get(dm.group(2))
    if mon is None:
        raise ValueError(f"Unknown month abbreviation: {dm.group(2)}")
    year = 2000 + int(dm.group(3))
    day  = int(dm.group(1))

    em = _ETD_RE.search(text)
    if not em:
        raise ValueError("Cannot find ETD in OFP page 1")
    etd_utc = datetime(year, mon, day, int(em.group(1)), int(em.group(2)), tzinfo=timezone.utc)

    tm = _TAXI_RE.search(text)
    if tm:
        taxi_min = int(tm.group(1)) * 60 + int(tm.group(2))
    else:
        taxi_min = _DEFAULT_TAXI_MIN
        print(f"  WARN: TAXI line missing in OFP — using default {_DEFAULT_TAXI_MIN} min (shifts all ref_times)")

    fm = _FLT_RE.search(text)
    if not fm:
        raise ValueError("Cannot find FLT time in OFP page 1")
    flight_time_min = int(fm.group(1)) * 60 + int(fm.group(2))

    return etd_utc, taxi_min, flight_time_min


def _extract_flight_info(page1_text, all_lines, etd_utc, takeoff_utc, flight_time_min):
    """
    Parse pre-extracted OFP text. Returns a dict for flight_info.json:
      {flight, dep, dest, date, acft, reg, etd, eta}
    """
    text = page1_text

    # ETA = touchdown: takeoff (ETD + parsed taxi-out) + airborne time
    eta_utc = takeoff_utc + timedelta(minutes=flight_time_min)

    flight = _FLIGHT_RE.search(text)
    dep    = _DEP_RE.search(text)
    cs     = _CS_RE.search(text)
    reg    = _REG_RE.search(text)
    dm     = _DATE_RE.search(text)

    alts = _extract_alternates(all_lines)
    return {
        "flight":    "TG" + flight.group(1) if flight else "TG???",
        "dep":       dep.group(1)  if dep  else "",
        "dest":      cs.group(2)   if cs   else "",
        "acft":      cs.group(1)   if cs   else "",
        "reg":       reg.group(1)  if reg  else "",
        "date":      f"{dm.group(1)} {dm.group(2)} {2000 + int(dm.group(3))}" if dm else "",
        "etd":       etd_utc.strftime("%H%MZ"),
        "eta":       eta_utc.strftime("%H%MZ"),
        "dest_altn": alts["dest_altn"],
        "era":       alts["era"],
        "rcf_dest":  alts["rcf_dest"],
        "rcf_altn":  alts["rcf_altn"],
    }


# ── NOTAM window formatter ────────────────────────────────────────────────────

def _fmt_win(n):
    def _fdt(dt): return f"{dt.day} {dt:%b %Y %H:%M}Z"
    if n.get("win_start"):
        start = _fdt(n["win_start"])
        if n.get("win_end"):
            return start + " – " + _fdt(n["win_end"])
        return "from " + start  # open-ended window
    import notam_engine
    return notam_engine._fmt_daily_windows(n.get("daily_windows"))


# ── Multi-leg helpers ─────────────────────────────────────────────────────────

def _merge_airports_legs(leg_airports_list):
    """Merge per-leg airports.json lists into one list with a `legs` array per airport."""
    from collections import OrderedDict
    merged = OrderedDict()
    for leg_idx, airports in enumerate(leg_airports_list, start=1):
        for ap in airports:
            icao = ap["icao"]
            leg_entry = {
                "leg":               leg_idx,
                "ref_time":          ap.get("ref_time", "0000Z"),
                "ref_iso":           ap.get("ref_iso"),
                "taf_base":          ap.get("taf_base"),
                "becmg_in_progress": ap.get("becmg_in_progress"),
                "active_overlays":   ap.get("active_overlays", []),
                "wx_tier":           ap.get("wx_tier", "YELLOW"),
            }
            if icao not in merged:
                merged[icao] = {
                    "icao":        ap["icao"],
                    "iata":        ap.get("iata", ""),
                    "name":        ap.get("name", ""),
                    "lat":         ap.get("lat"),
                    "lon":         ap.get("lon"),
                    "runway_info": ap.get("runway_info"),
                    "metar":       ap.get("metar"),
                    "taf_raw":     ap.get("taf_raw"),
                    "dist_nm":     ap.get("dist_nm"),
                    "legs":        [leg_entry],
                }
            else:
                merged[icao]["legs"].append(leg_entry)
    return list(merged.values())


_GENERAL_SECTION_LABELS = {
    "GENERAL":    "GENERAL INFORMATION",
    "FLIGHT LEG": "FLIGHT LEG INFORMATION",
    "AEROPLANE":  "AEROPLANE INFORMATION",
}


def _filter_general_notams(general_db, fir_db, leg_flight_infos):
    """
    Two-pass AI filtering for GENERAL / FLIGHT LEG / AEROPLANE NOTAMs.

    Pass 1 — relevance filter: batches of 20, ask model for {id, relevant}.
              Any ID not returned by the model defaults to relevant=True.
    Pass 2 — summarise survivors via notam_engine._summarize_notams().

    Returns list of section dicts suitable for general_notams.json.
    """
    import anthropic
    import notam_engine

    legs_summary = " / ".join(f"{fi['dep']}→{fi['dest']}" for fi in leg_flight_infos)
    acft = leg_flight_infos[0].get("acft", "") if leg_flight_infos else ""
    fir_list = ", ".join(sorted(fir_db.keys())) or "none"

    filter_system = (
        "You are a flight dispatcher reviewing NOTAMs for operational relevance. "
        f"Flight: {legs_summary}. Aircraft type: {acft}. FIRs on route: {fir_list}. "
        "For each NOTAM below (separated by ---), decide if it could DIRECTLY affect the "
        "operation of this flight — e.g. requires crew action, affects an airport/route/procedure "
        "we use, or is a safety/security matter for this aircraft type. "
        "Exclude administrative, planning, or airspace redesign items with no operational impact. "
        'Reply with a JSON array only, no other text: [{"id": "...", "relevant": true}, ...]'
    )

    client = anthropic.Anthropic()
    output_sections = []

    for key, label in _GENERAL_SECTION_LABELS.items():
        all_notams = general_db.get(key, [])

        if not all_notams:
            output_sections.append({"key": key, "label": label, "notams": [], "filtered_out": []})
            continue

        survivors = []
        excluded  = []

        for start in range(0, len(all_notams), 20):
            batch = all_notams[start: start + 20]
            user_msg = "\n\n---\n\n".join(
                f'ID: {n["id"]}\n{n["body"]}' for n in batch
            )
            verdict_map = {}
            try:
                msg = client.messages.create(
                    model=HAIKU_MODEL,
                    max_tokens=1024,
                    system=filter_system,
                    messages=[{"role": "user", "content": user_msg}],
                )
                raw_text = msg.content[0].text.strip()
                if raw_text.startswith("```"):  # tolerate fenced JSON
                    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text)
                verdicts = json.loads(raw_text)
                verdict_map = {v["id"]: bool(v.get("relevant", True)) for v in verdicts if "id" in v}
            except Exception as e:
                print(f"  WARN: general NOTAM filter failed ({e}); defaulting all to included")

            for n in batch:
                if verdict_map.get(n["id"], True):  # default include on miss
                    survivors.append(n)
                else:
                    excluded.append(n)

        summaries = notam_engine._summarize_notams({key: survivors}) if survivors else {}

        notams_out = sorted(
            [
                {
                    "id":      n["id"],
                    "tier":    n["tier"],
                    "body":    n["body"],
                    "window":  _fmt_win(n),
                    "summary": summaries.get((key, n["id"]), n["body"].split("\n")[0]),
                }
                for n in survivors
            ],
            key=lambda x: x["tier"],
        )
        filtered_out = [
            {"id": n["id"], "tier": n["tier"], "body": n["body"], "window": _fmt_win(n)}
            for n in excluded
        ]

        output_sections.append({"key": key, "label": label, "notams": notams_out, "filtered_out": filtered_out})

    return output_sections


def _leg_ref_dt(leg_entry, takeoff_utc):
    """Full UTC datetime at which the aircraft is nearest this airport on this leg.

    Prefers the exact ref_iso written by met_engine. The HHMMZ fallback anchors
    to the takeoff date and rolls past midnight when needed — a ref time can
    never precede takeoff, so an "earlier" clock time means the next day.
    """
    iso = leg_entry.get("ref_iso")
    if iso:
        return datetime.fromisoformat(iso)
    ref_str = leg_entry.get("ref_time", "0000Z").rstrip("Z")
    d = takeoff_utc.date()
    ref_dt = datetime(d.year, d.month, d.day, int(ref_str[:2]), int(ref_str[2:]),
                      tzinfo=timezone.utc)
    if ref_dt < takeoff_utc:
        ref_dt += timedelta(days=1)
    return ref_dt


def _is_active_for_flight(win_start, win_end, flight_start, flight_end):
    """True if the NOTAM window overlaps the full flight duration [flight_start, flight_end]."""
    if win_start is None:
        return True
    return win_start <= flight_end and (win_end is None or win_end >= flight_start)


def _fir_marker_position(centroid, route_pts, airports, threshold_nm=_FIR_EXCLUSION_NM):
    """Return (lat, lon) for the FIR diamond marker.

    Uses the nearest route waypoint to the FIR centroid, skipping any waypoint
    within threshold_nm of an airport marker (to prevent diamond/circle overlap).
    Falls back to the centroid itself if every waypoint is too close to an airport.
    """
    from _utils import haversine_nm as _hav
    ap_positions = [(ap["lat"], ap["lon"]) for ap in airports]
    sorted_pts = sorted(
        route_pts,
        key=lambda p: _hav(centroid[0], centroid[1], p["lat"], p["lon"]),
    )
    for pt in sorted_pts:
        if not any(
            _hav(pt["lat"], pt["lon"], alat, alon) < threshold_nm
            for alat, alon in ap_positions
        ):
            return pt["lat"], pt["lon"]
    return centroid[0], centroid[1]  # all waypoints near airports — use centroid


def _run_notam_step_multi(notam_path, group_dir, airports, leg_routes, leg_takeoffs, leg_flight_infos):
    """Attach per-leg NOTAMs to airports; write airports.json, fir_notams.json, general_notams.json."""
    import notam_engine

    notam_db, fir_db, general_db = notam_engine.parse_notam_pdf(notam_path)

    # ── Airport NOTAMs per leg ────────────────────────────────────────────────
    for ap in airports:
        for leg_entry in ap["legs"]:
            takeoff_utc = leg_takeoffs[leg_entry["leg"] - 1]
            ref_dt = _leg_ref_dt(leg_entry, takeoff_utc)
            raw = notam_db.get(ap["icao"], [])
            active = [
                {"id": n["id"], "tier": notam_engine._effective_tier(n, ref_dt), "body": n["body"], "window": _fmt_win(n)}
                for n in raw
                if notam_engine._is_active(n["win_start"], n["win_end"], ref_dt)
            ]
            active.sort(key=lambda x: x["tier"])
            leg_entry["notams"]        = active
            leg_entry["notam_covered"] = ap["icao"] in notam_db

    # AI summaries — deduplicated across legs
    to_sum = {}
    for ap in airports:
        for leg_entry in ap["legs"]:
            for n in leg_entry.get("notams", []):
                to_sum.setdefault(ap["icao"], {})[n["id"]] = n
    summaries = notam_engine._summarize_notams(
        {icao: list(nm.values()) for icao, nm in to_sum.items()}
    )
    for ap in airports:
        for leg_entry in ap["legs"]:
            for n in leg_entry.get("notams", []):
                n["summary"] = summaries.get((ap["icao"], n["id"]), n["body"].split("\n")[0])

    with open(os.path.join(group_dir, "airports.json"), "w") as f:
        json.dump(airports, f, indent=2)

    # ── FIR NOTAMs per leg ────────────────────────────────────────────────────
    flight_start = min(leg_takeoffs)
    flight_end   = max(leg_takeoffs) + timedelta(hours=_FIR_FLIGHT_WINDOW_H)

    fir_merged = {}
    for leg_local, (route_json_path, takeoff_utc) in enumerate(
        zip(leg_routes, leg_takeoffs), start=1
    ):
        notam_engine.TAKEOFF_UTC = takeoff_utc
        with open(route_json_path) as f:
            route_pts = json.load(f)

        for fir_icao, fir_data in fir_db.items():
            coords, source = notam_engine.resolve_fir_centroid(
                fir_icao, fir_data["name"], route_pts
            )
            if source == "route_midpoint" and leg_local == 1:
                _progress(f"  WARN: no centroid known for FIR {fir_icao} ({fir_data['name']}) — using route midpoint")
            ref_dt, _, _, _ = notam_engine._nearest_waypoint(
                coords[0], coords[1], route_pts
            )
            active_fir = [
                {"id": n["id"], "tier": n["tier"], "body": n["body"], "window": _fmt_win(n)}
                for n in fir_data["notams"]
                if _is_active_for_flight(n["win_start"], n["win_end"], flight_start, flight_end)
            ]
            active_fir.sort(key=lambda x: x["tier"])
            leg_fir = {"leg": leg_local, "ref_time": ref_dt.strftime("%H%MZ"), "notams": active_fir}
            if fir_icao not in fir_merged:
                mk_lat, mk_lon = _fir_marker_position(coords, route_pts, airports)
                fir_merged[fir_icao] = {
                    "fir":  fir_icao, "name": fir_data["name"],
                    "lat":  round(mk_lat, 4), "lon": round(mk_lon, 4),
                    "legs": [leg_fir],
                }
            else:
                fir_merged[fir_icao]["legs"].append(leg_fir)

    fir_out = [e for e in fir_merged.values() if any(l["notams"] for l in e["legs"])]

    fir_to_sum = {}
    for entry in fir_out:
        for leg in entry["legs"]:
            for n in leg["notams"]:
                fir_to_sum.setdefault(entry["fir"], {})[n["id"]] = n
    fir_summaries = notam_engine._summarize_notams(
        {fir: list(nm.values()) for fir, nm in fir_to_sum.items()}
    )
    for entry in fir_out:
        for leg in entry["legs"]:
            for n in leg["notams"]:
                n["summary"] = fir_summaries.get(
                    (entry["fir"], n["id"]), n["body"].split("\n")[0]
                )

    with open(os.path.join(group_dir, "fir_notams.json"), "w") as f:
        json.dump(fir_out, f, indent=2)

    # ── Flight-wide NOTAMs (GENERAL / FLIGHT LEG / AEROPLANE) ─────────────────
    _progress("  Filtering flight-wide NOTAMs…")
    general_sections = _filter_general_notams(general_db, fir_db, leg_flight_infos)
    total_gen = sum(len(s["notams"]) for s in general_sections)
    total_excl = sum(len(s["filtered_out"]) for s in general_sections)
    _progress(f"  Flight-wide NOTAMs: {total_gen} relevant, {total_excl} filtered out")
    with open(os.path.join(group_dir, "general_notams.json"), "w") as f:
        json.dump(general_sections, f, indent=2)


# ── Source Pane: page images + click-to-highlight anchors ────────────────────

def _render_source_document(pdf_path, extract_fn, prefix, json_name, group_dirs):
    """Render one source document's pages and anchors once per run (shared across
    groups), then hard-link (falling back to a copy) the images and duplicate the
    JSON into every group dir beyond the primary. Shared by the NOTAM and MET
    halves of _run_source_pane_step — each document is best-effort independently.
    """
    import notam_anchors  # render_pages lives here regardless of which document

    primary_dir = group_dirs[0]
    n_pages = notam_anchors.render_pages(pdf_path, primary_dir, prefix=prefix)
    anchors, page_sizes = extract_fn(pdf_path)

    payload = {
        "pages": n_pages,
        "page_sizes": [list(s) for s in page_sizes],
        "anchors": anchors,
    }
    with open(os.path.join(primary_dir, json_name), "w") as f:
        json.dump(payload, f)

    for group_dir in group_dirs[1:]:
        with open(os.path.join(group_dir, json_name), "w") as f:
            json.dump(payload, f)
        for i in range(1, n_pages + 1):
            name = f"{prefix}_{i:03d}.png"
            src, dst = os.path.join(primary_dir, name), os.path.join(group_dir, name)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy(src, dst)


def _run_source_pane_step(notam_path, met_path, group_dirs):
    """Render both source documents' pages and compute click-to-highlight anchors,
    once per run (both PDFs are shared across groups). Each document is rendered
    independently, wrapped in its own try/except, so a MET rendering failure never
    costs the NOTAM pane (or vice versa) — the Source Pane degrades one document
    at a time, not all-or-nothing.
    """
    import notam_anchors

    try:
        _render_source_document(
            notam_path, notam_anchors.extract_anchors, "notam_page",
            "notam_anchors.json", group_dirs,
        )
    except Exception as exc:
        _progress(f"⚠ NOTAM source-document rendering failed — NOTAM pane unavailable ({type(exc).__name__})")

    try:
        import met_anchors
        _render_source_document(
            met_path, met_anchors.extract_anchors, "met_page",
            "met_anchors.json", group_dirs,
        )
    except Exception as exc:
        _progress(f"⚠ MET source-document rendering failed — MET pane unavailable ({type(exc).__name__})")


# ── Pipeline background thread ────────────────────────────────────────────────

def _progress(msg):
    with _lock:
        _current_run["messages"].append(msg)
    print(f"[pipeline] {msg}", flush=True)


def _run_pipeline(run_id, ofp_paths, met_path, notam_path):
    """Multi-leg pipeline. ofp_paths is an ordered list of 1–4 OFP file paths."""
    leg_count = len(ofp_paths)
    # 1–2 OFPs: single map (all legs in group 1)
    # 3–4 OFPs: two maps (legs 1-2 in group 1, rest in group 2)
    half       = (leg_count + 1) // 2 if leg_count > 2 else leg_count
    group_legs = {1: ofp_paths[:half]}
    if leg_count > 2:
        group_legs[2] = ofp_paths[half:]

    group_dirs = []
    try:
        for g_num, group_ofps in group_legs.items():
            group_dir = os.path.join(RUNS_DIR, run_id, str(g_num))
            os.makedirs(group_dir, exist_ok=True)
            group_dirs.append(group_dir)

            # ── Step 1: OFP constants + flight info per leg ──────────────────
            leg_data = []
            for local_idx, ofp_path in enumerate(group_ofps, start=1):
                _progress(f"[G{g_num}/L{local_idx}] Reading OFP…")
                page1_text, all_lines = _read_ofp(ofp_path)
                etd_utc, taxi_min, flt_min = _extract_ofp_constants(page1_text)
                takeoff_utc = etd_utc + timedelta(minutes=taxi_min)
                fi = _extract_flight_info(page1_text, all_lines, etd_utc, takeoff_utc, flt_min)
                _progress(
                    f"  {fi['flight']} {fi['dep']}→{fi['dest']}  "
                    f"ETD {fi['etd']}  TAKEOFF {takeoff_utc.strftime('%H%MZ')}"
                )
                leg_data.append({
                    "local_idx":   local_idx,
                    "ofp_path":    ofp_path,
                    "takeoff_utc": takeoff_utc,
                    "flt_min":     flt_min,
                    "flight_info": fi,
                })

            # Sort legs chronologically so leg 1 = earliest departure
            leg_data.sort(key=lambda ld: ld["takeoff_utc"])
            for new_idx, ld in enumerate(leg_data, start=1):
                ld["local_idx"] = new_idx

            # ── Step 2: Parse routes ──────────────────────────────────────────
            import parse_ofp
            for ld in leg_data:
                _progress(f"[G{g_num}/L{ld['local_idx']}] Parsing route…")
                parse_ofp.OFP_PDF         = ld["ofp_path"]
                parse_ofp.OUT_JSON        = os.path.join(group_dir, f"route_{ld['local_idx']}.json")
                parse_ofp.FLIGHT_TIME_MIN = ld["flt_min"]
                parse_ofp.main()

            # ── Step 3: MET engine per leg ────────────────────────────────────
            import met_engine
            leg_airports_list = []
            met_warnings = []
            for ld in leg_data:
                _progress(f"[G{g_num}/L{ld['local_idx']}] Processing MET…")
                tmp = os.path.join(group_dir, f"_airports_leg_{ld['local_idx']}.json")
                met_engine.MET_PDF     = met_path
                met_engine.ROUTE_JSON  = os.path.join(group_dir, f"route_{ld['local_idx']}.json")
                met_engine.OUT_JSON    = tmp
                met_engine.TAKEOFF_UTC = ld["takeoff_utc"]
                met_engine.main()
                if ld["local_idx"] == 1:  # same MET PDF every leg — warn once
                    met_warnings = list(met_engine.WARNINGS)
                    for w in met_warnings:
                        _progress(f"  WARN: {w}")
                with open(tmp) as f:
                    leg_airports_list.append(json.load(f))
                os.remove(tmp)

            with open(os.path.join(group_dir, "warnings.json"), "w") as f:
                json.dump(met_warnings, f, indent=2)

            # ── Step 4: Merge airports into multi-leg schema ──────────────────
            airports = _merge_airports_legs(leg_airports_list)

            # ── Step 5: NOTAM step ────────────────────────────────────────────
            _progress(f"[G{g_num}] Processing NOTAMs — AI summaries take ~2 minutes…")
            leg_routes        = [os.path.join(group_dir, f"route_{ld['local_idx']}.json") for ld in leg_data]
            leg_takeoffs      = [ld["takeoff_utc"] for ld in leg_data]
            leg_flight_infos  = [ld["flight_info"] for ld in leg_data]
            _run_notam_step_multi(notam_path, group_dir, airports, leg_routes, leg_takeoffs, leg_flight_infos)

            # ── Step 6: flight_info.json ──────────────────────────────────────
            fi_out = {"legs": [ld["flight_info"] for ld in leg_data]}
            with open(os.path.join(group_dir, "flight_info.json"), "w") as f:
                json.dump(fi_out, f, indent=2)

            _progress(f"[G{g_num}] Complete.")

        _progress("Rendering source documents for click-to-highlight…")
        try:
            _run_source_pane_step(notam_path, met_path, group_dirs)
            _progress("Source documents ready.")
        except Exception as exc:
            _progress(f"⚠ source-document rendering failed — source pane unavailable ({type(exc).__name__})")

        with _lock:
            _current_run["status"] = "done"
            _current_run["run_id"] = run_id
        _progress("DONE")

    except Exception as exc:
        import traceback
        _progress(f"ERROR: {type(exc).__name__}: {exc.args[0] if exc.args else '(no detail)'}")
        with _lock:
            _current_run["status"] = "error"
            _current_run["error"]  = str(exc)
        traceback.print_exc()
    finally:
        shutil.rmtree(os.path.dirname(ofp_paths[0]), ignore_errors=True)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def root():
    return redirect("/upload")


@app.route("/upload", methods=["GET"])
def upload_page():
    return send_file(os.path.join(HERE, "upload.html"))


@app.route("/upload", methods=["POST"])
def upload_files():
    # Validate all file fields before touching any shared state or disk
    ofp_file_objs = []
    for i in range(1, 5):
        f = request.files.get(f"ofp_{i}")
        if f and f.filename:
            ext = os.path.splitext(secure_filename(f.filename))[1].lower()
            if ext != ".pdf":
                return f"ofp_{i}: only PDF files are accepted", 400
            ofp_file_objs.append(f)
        elif i == 1:
            return "Missing file for field 'ofp_1'", 400

    for field in ("met", "notam"):
        f = request.files.get(field)
        if not f or not f.filename:
            return f"Missing file for field '{field}'", 400
        ext = os.path.splitext(secure_filename(f.filename))[1].lower()
        if ext != ".pdf":
            return f"Field '{field}': only PDF files are accepted", 400

    leg_count   = len(ofp_file_objs)
    group_count = 1 if leg_count <= 2 else 2
    run_id      = str(uuid.uuid4())

    # Atomic check-and-claim: the pipeline monkey-patches module globals, so two
    # concurrent runs would silently corrupt each other's output. Claim the slot
    # in the same lock acquisition as the "running" check.
    with _lock:
        if _current_run["status"] == "running":
            return "A pipeline is already running. Please wait for it to finish.", 429
        _current_run.update({
            "run_id":      run_id,
            "status":      "running",
            "messages":    [],
            "error":       None,
            "leg_count":   leg_count,
            "group_count": group_count,
        })

    upload_dir = os.path.join(UPLOAD_DIR, run_id)
    try:
        # Sweep run dirs older than 24 h
        cutoff = time.time() - 86400
        for name in os.listdir(RUNS_DIR):
            path = os.path.join(RUNS_DIR, name)
            try:
                if os.path.isdir(path) and os.stat(path).st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass

        os.makedirs(upload_dir, exist_ok=True)

        ofp_paths = []
        for i, f in enumerate(ofp_file_objs, start=1):
            path = os.path.join(upload_dir, f"ofp_{i}.pdf")
            f.save(path)
            ofp_paths.append(path)

        met_path   = os.path.join(upload_dir, "met.pdf")
        notam_path = os.path.join(upload_dir, "notam.pdf")
        request.files["met"].save(met_path)
        request.files["notam"].save(notam_path)
    except Exception:
        # Release the claimed slot so the next upload isn't locked out
        shutil.rmtree(upload_dir, ignore_errors=True)
        with _lock:
            _current_run["status"] = "error"
            _current_run["error"]  = "Failed to store uploaded files"
        raise

    t = threading.Thread(
        target=_run_pipeline,
        args=(run_id, ofp_paths, met_path, notam_path),
        daemon=True,
    )
    t.start()

    return redirect(f"/progress/{run_id}")


@app.route("/progress/<run_id>")
def progress_page(run_id):
    with _lock:
        group_count = _current_run.get("group_count", 1)
    return render_template_string(_PROGRESS_HTML, run_id=run_id, group_count=group_count)


@app.route("/api/status/<run_id>")
def api_status(run_id):
    with _lock:
        data = dict(_current_run)
        data["messages"] = list(_current_run["messages"])  # snapshot; list is mutated by pipeline thread
    if data.get("run_id") != run_id:
        return Response(json.dumps({"error": "unknown or superseded run"}),
                        status=404, mimetype="application/json")
    return Response(json.dumps(data), mimetype="application/json")


@app.route("/map")
def map_page():
    return send_file(os.path.join(HERE, "index.html"))


@app.route("/data/<int:group>/<filename>")
def serve_group_data(group, filename):
    with _lock:
        run_id = _current_run.get("run_id")
        status = _current_run.get("status")
    if run_id and status == "done":
        # send_from_directory rejects path traversal and 404s missing files
        return send_from_directory(os.path.join(RUNS_DIR, run_id, str(group)), filename)
    return Response("Not found", status=404)


@app.route("/data/<filename>")
def serve_data(filename):
    # Legacy endpoint: serves the static data/ folder (MVP demo fallback only)
    return send_from_directory(os.path.join(HERE, "data"), filename)


# ── Progress page ─────────────────────────────────────────────────────────────

_PROGRESS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Processing — Preflight Decoder</title>
  <style>
    body { background:#0d1117; color:#c9d1d9; font-family:monospace;
           margin:0; padding:2rem; }
    h1   { color:#58a6ff; font-size:1.2rem; margin-bottom:1rem; }
    #log { background:#161b22; border:1px solid #30363d; border-radius:6px;
           padding:1rem; height:60vh; overflow-y:auto; font-size:0.85rem; }
    .msg  { margin:0; padding:2px 0; white-space:pre-wrap; }
    .done { color:#3fb950; font-weight:bold; }
    .err  { color:#f85149; font-weight:bold; }
    #note { margin-top:1rem; color:#8b949e; font-size:0.8rem; }
  </style>
</head>
<body>
  <h1>Processing flight dispatch…</h1>
  <div id="log"></div>
  <p id="note">NOTAM AI summaries take approximately 2 minutes. Please wait.</p>
  <script>
    const runId     = {{ run_id | tojson }};
    const groupCount = {{ group_count | tojson }};
    let seen = 0;
    let pollErrors = 0;
    const log = document.getElementById("log");

    function addLine(text, cls) {
      const p = document.createElement("p");
      p.className = "msg" + (cls ? " " + cls : "");
      p.textContent = text;
      log.appendChild(p);
      log.scrollTop = log.scrollHeight;
    }

    function poll() {
      fetch("/api/status/" + runId)
        .then(r => {
          pollErrors = 0;
          if (r.status === 404) {
            clearInterval(timer);
            addLine("This run is no longer active (superseded or server restarted).", "err");
            return null;
          }
          return r.json();
        })
        .then(data => {
          if (!data) return;
          const msgs = data.messages || [];
          for (; seen < msgs.length; seen++) {
            const m = msgs[seen];
            const cls = m.startsWith("ERROR") ? "err" : m === "DONE" ? "done" : "";
            addLine(m, cls);
          }
          if (data.status === "done") {
            clearInterval(timer);
            setTimeout(() => {
              if (groupCount > 1) window.open("/map?g=2", "_blank");
              window.location.href = "/map?g=1";
            }, 1200);
          } else if (data.status === "error") {
            clearInterval(timer);
            addLine("Pipeline failed — check server logs.", "err");
          }
        })
        .catch(e => {
          pollErrors++;
          addLine("Poll error: " + e, "err");
          if (pollErrors >= 5) {
            clearInterval(timer);
            addLine("Polling stopped after repeated network errors.", "err");
          }
        });
    }

    const timer = setInterval(poll, 2000);
    poll();
  </script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
