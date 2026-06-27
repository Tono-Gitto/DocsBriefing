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
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pdfplumber
from dotenv import load_dotenv
from flask import Flask, Response, redirect, render_template_string, request, send_file
from werkzeug.utils import secure_filename

# Load .env before anything else so ANTHROPIC_API_KEY is in os.environ
load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # ensure "import parse_ofp" etc. resolve from this directory

UPLOAD_DIR = os.path.join(HERE, "uploads")
RUNS_DIR   = os.path.join(HERE, "runs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RUNS_DIR,   exist_ok=True)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap

_lock = threading.Lock()
_current_run: dict = {"run_id": None, "status": "idle", "messages": [], "error": None}


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


def _extract_ofp_constants(ofp_path):
    """
    Read OFP page 1. Returns (etd_utc: datetime, taxi_min: int, flight_time_min: int).
    Raises ValueError if essential fields (date, ETD, FLT) are missing.
    """
    with pdfplumber.open(ofp_path) as pdf:
        text = pdf.pages[0].extract_text() or ""

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
    taxi_min = int(tm.group(1)) * 60 + int(tm.group(2)) if tm else 20  # default 20 min

    fm = _FLT_RE.search(text)
    if not fm:
        raise ValueError("Cannot find FLT time in OFP page 1")
    flight_time_min = int(fm.group(1)) * 60 + int(fm.group(2))

    return etd_utc, taxi_min, flight_time_min


def _extract_flight_info(ofp_path, etd_utc, flight_time_min):
    """
    Read OFP page 1. Returns a dict for flight_info.json:
      {flight, dep, dest, date, acft, reg, etd, eta}
    """
    with pdfplumber.open(ofp_path) as pdf:
        text = pdf.pages[0].extract_text() or ""

    from datetime import timedelta
    eta_utc = etd_utc + timedelta(minutes=flight_time_min + 20)  # +taxi back

    flight = _FLIGHT_RE.search(text)
    dep    = _DEP_RE.search(text)
    cs     = _CS_RE.search(text)
    reg    = _REG_RE.search(text)
    dm     = _DATE_RE.search(text)

    return {
        "flight": "TG" + flight.group(1) if flight else "TG???",
        "dep":    dep.group(1)  if dep  else "",
        "dest":   cs.group(2)   if cs   else "",
        "acft":   cs.group(1)   if cs   else "",
        "reg":    reg.group(1)  if reg  else "",
        "date":   f"{dm.group(1)} {dm.group(2)} {2000 + int(dm.group(3))}" if dm else "",
        "etd":    etd_utc.strftime("%H%MZ"),
        "eta":    eta_utc.strftime("%H%MZ"),
    }


# ── NOTAM window formatter ────────────────────────────────────────────────────

def _fmt_win(n):
    if not n.get("win_start"):
        return None
    return (
        n["win_start"].strftime("%-d %b %Y %H:%MZ")
        + " – "
        + n["win_end"].strftime("%-d %b %Y %H:%MZ")
    )


# FIR centroids are now fully maintained in notam_engine.FIR_COORDS (worldwide coverage).

# ── NOTAM orchestrator (replaces notam_engine.main()) ────────────────────────

def _run_notam_step(notam_path, run_dir, takeoff_utc):
    """
    Calls notam_engine library functions directly so we can pass takeoff_utc.date()
    for ref_dt construction, avoiding the hardcoded 2026-06-20 literal in
    notam_engine.main() line 416.
    """
    import notam_engine

    # Monkeypatch TAKEOFF_UTC so _nearest_waypoint computes correct FIR ref times
    notam_engine.TAKEOFF_UTC = takeoff_utc

    airports_json = os.path.join(run_dir, "airports.json")
    route_json    = os.path.join(run_dir, "route.json")
    fir_json      = os.path.join(run_dir, "fir_notams.json")

    with open(airports_json) as f:
        airports = json.load(f)
    with open(route_json) as f:
        route_pts = json.load(f)

    notam_db, fir_db = notam_engine.parse_notam_pdf(notam_path)
    flight_date = takeoff_utc.date()

    # ── Airport NOTAMs ────────────────────────────────────────────────────────
    for ap in airports:
        ref_str = ap.get("ref_time", "0000Z").rstrip("Z")
        ref_dt  = datetime(
            flight_date.year, flight_date.month, flight_date.day,
            int(ref_str[:2]), int(ref_str[2:]),
            tzinfo=timezone.utc,
        )
        raw = notam_db.get(ap["icao"], [])
        active = [
            {"id": n["id"], "tier": n["tier"], "body": n["body"], "window": _fmt_win(n)}
            for n in raw
            if notam_engine._is_active(n["win_start"], n["win_end"], ref_dt)
        ]
        active.sort(key=lambda x: x["tier"])
        ap["notams"]        = active
        ap["notam_covered"] = ap["icao"] in notam_db

    summaries = notam_engine._summarize_notams(
        {ap["icao"]: ap["notams"] for ap in airports if ap.get("notams")}
    )
    for ap in airports:
        for n in ap.get("notams", []):
            n["summary"] = summaries.get((ap["icao"], n["id"]), n["body"].split("\n")[0])

    with open(airports_json, "w") as f:
        json.dump(airports, f, indent=2)

    # ── FIR NOTAMs ────────────────────────────────────────────────────────────
    fir_out = []
    for fir_icao, fir_data in fir_db.items():
        coords = notam_engine.FIR_COORDS.get(fir_icao)
        if not coords:
            _progress(f"  WARN: no centroid for FIR {fir_icao} ({fir_data['name']}) — skipped")
            continue
        ref_dt, _, wp_lat, wp_lon = notam_engine._nearest_waypoint(
            coords[0], coords[1], route_pts
        )
        active_fir = [
            {"id": n["id"], "tier": n["tier"], "body": n["body"], "window": _fmt_win(n)}
            for n in fir_data["notams"]
            if notam_engine._is_active(n["win_start"], n["win_end"], ref_dt)
        ]
        active_fir.sort(key=lambda x: x["tier"])
        fir_out.append({
            "fir":      fir_icao,
            "name":     fir_data["name"],
            "lat":      round(wp_lat, 4),
            "lon":      round(wp_lon, 4),
            "ref_time": ref_dt.strftime("%H%MZ"),
            "notams":   active_fir,
        })

    fir_summaries = notam_engine._summarize_notams(
        {e["fir"]: e["notams"] for e in fir_out if e["notams"]}
    )
    for entry in fir_out:
        for n in entry["notams"]:
            n["summary"] = fir_summaries.get(
                (entry["fir"], n["id"]), n["body"].split("\n")[0]
            )

    with open(fir_json, "w") as f:
        json.dump(fir_out, f, indent=2)


# ── Pipeline background thread ────────────────────────────────────────────────

def _progress(msg):
    with _lock:
        _current_run["messages"].append(msg)
    print(f"[pipeline] {msg}", flush=True)


def _run_pipeline(run_id, ofp_path, met_path, notam_path):
    run_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    try:
        # Step 1 — Extract flight-specific constants
        _progress("Reading OFP for flight constants…")
        etd_utc, taxi_min, flight_time_min = _extract_ofp_constants(ofp_path)
        takeoff_utc = etd_utc + timedelta(minutes=taxi_min)
        _progress(
            f"Date {takeoff_utc.strftime('%d %b %Y')}  "
            f"ETD {etd_utc.strftime('%H%MZ')}  "
            f"TAXI {taxi_min}min  "
            f"TAKEOFF {takeoff_utc.strftime('%H%MZ')}  "
            f"FLT {flight_time_min}min"
        )

        flight_info = _extract_flight_info(ofp_path, etd_utc, flight_time_min)
        _progress(f"Flight: {flight_info['flight']} {flight_info['dep']}→{flight_info['dest']}  {flight_info['acft']}  {flight_info['reg']}")
        with open(os.path.join(run_dir, "flight_info.json"), "w") as f:
            json.dump(flight_info, f, indent=2)

        # Step 2 — Parse route waypoints
        _progress("Parsing route waypoints from OFP…")
        import parse_ofp
        parse_ofp.OFP_PDF         = ofp_path
        parse_ofp.OUT_JSON        = os.path.join(run_dir, "route.json")
        parse_ofp.FLIGHT_TIME_MIN = flight_time_min
        parse_ofp.main()
        _progress("Route parsed.")

        # Step 3 — MET engine
        _progress("Processing MET / TAF data…")
        import met_engine
        met_engine.MET_PDF     = met_path
        met_engine.ROUTE_JSON  = os.path.join(run_dir, "route.json")
        met_engine.OUT_JSON    = os.path.join(run_dir, "airports.json")
        met_engine.TAKEOFF_UTC = takeoff_utc
        met_engine.main()
        _progress("MET data processed.")

        # Step 4 — NOTAM engine (via custom orchestrator)
        _progress("Processing NOTAMs — AI summaries take ~2 minutes…")
        _run_notam_step(notam_path, run_dir, takeoff_utc)
        _progress("NOTAMs processed.")

        with _lock:
            _current_run["status"] = "done"
            _current_run["run_id"] = run_id
        _progress("DONE")

    except Exception as exc:
        import traceback
        _progress(f"ERROR: {exc}")
        with _lock:
            _current_run["status"] = "error"
            _current_run["error"]  = str(exc)
        traceback.print_exc()


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def root():
    return redirect("/upload")


@app.route("/upload", methods=["GET"])
def upload_page():
    return send_file(os.path.join(HERE, "upload.html"))


@app.route("/upload", methods=["POST"])
def upload_files():
    with _lock:
        if _current_run["status"] == "running":
            return "A pipeline is already running. Please wait for it to finish.", 429

    for field in ("ofp", "met", "notam"):
        f = request.files.get(field)
        if not f or not f.filename:
            return f"Missing file for field '{field}'", 400
        ext = os.path.splitext(secure_filename(f.filename))[1].lower()
        if ext != ".pdf":
            return f"Field '{field}': only PDF files are accepted", 400

    run_id     = str(uuid.uuid4())
    upload_dir = os.path.join(UPLOAD_DIR, run_id)
    os.makedirs(upload_dir, exist_ok=True)

    ofp_path   = os.path.join(upload_dir, "ofp.pdf")
    met_path   = os.path.join(upload_dir, "met.pdf")
    notam_path = os.path.join(upload_dir, "notam.pdf")

    request.files["ofp"].save(ofp_path)
    request.files["met"].save(met_path)
    request.files["notam"].save(notam_path)

    with _lock:
        _current_run.update({
            "run_id":   run_id,
            "status":   "running",
            "messages": [],
            "error":    None,
        })

    t = threading.Thread(
        target=_run_pipeline,
        args=(run_id, ofp_path, met_path, notam_path),
        daemon=True,
    )
    t.start()

    return redirect(f"/progress/{run_id}")


@app.route("/progress/<run_id>")
def progress_page(run_id):
    return render_template_string(_PROGRESS_HTML, run_id=run_id)


@app.route("/api/status/<run_id>")
def api_status(run_id):
    with _lock:
        data = dict(_current_run)
    return Response(json.dumps(data), mimetype="application/json")


@app.route("/map")
def map_page():
    return send_file(os.path.join(HERE, "index.html"))


@app.route("/data/<filename>")
def serve_data(filename):
    with _lock:
        run_id = _current_run.get("run_id")
        status = _current_run.get("status")
    if run_id and status == "done":
        path = os.path.join(RUNS_DIR, run_id, filename)
        if os.path.exists(path):
            return send_file(path)
    # Fallback to static data/ folder (MVP pre-run output)
    fallback = os.path.join(HERE, "data", filename)
    if os.path.exists(fallback):
        return send_file(fallback)
    return Response("Not found", status=404)


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
    const runId = {{ run_id | tojson }};
    let seen = 0;
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
        .then(r => r.json())
        .then(data => {
          const msgs = data.messages || [];
          for (; seen < msgs.length; seen++) {
            const m = msgs[seen];
            const cls = m.startsWith("ERROR") ? "err" : m === "DONE" ? "done" : "";
            addLine(m, cls);
          }
          if (data.status === "done") {
            clearInterval(timer);
            setTimeout(() => { window.location.href = "/map"; }, 1200);
          } else if (data.status === "error") {
            clearInterval(timer);
            addLine("Pipeline failed — check server logs.", "err");
          }
        })
        .catch(e => addLine("Poll error: " + e, "err"));
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
