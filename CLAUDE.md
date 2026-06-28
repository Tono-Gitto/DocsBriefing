# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Goal

Thai Airways B777 dispatch packages (OFP + MET + NOTAM) run 100+ pages of raw text per flight. This tool turns them into a pictorial, click-on-the-airport briefing: tap an airport on the map, see only the weather and NOTAM information relevant *at the time the flight will be near that airport*.

---

## Running the App

**Primary interface — Flask app at :5001:**
```bash
python3 app.py          # serves at http://localhost:5001
```
Requires `.env` file with `ANTHROPIC_API_KEY=<key>` in the project root. The key is loaded via `python-dotenv` on startup; it is never sent to the browser.

**Legacy MVP — static server at :8000 (frozen snapshot):**
```bash
cd mvp && python3 -m http.server 8000
```
`mvp/index.html` is a frozen copy of the map UI; `mvp/data` symlinks to `../data/`.

**Install dependencies:**
```bash
pip install -r requirements.txt   # all packages: flask, gunicorn, python-dotenv, pdfplumber, anthropic, requests
```
`requirements-app.txt` mirrors the same set and is kept for reference. `requirements.txt` is what Railway's build system (`railpack`) auto-detects and installs.

**CLI scripts (dev/debug only — not needed when using Flask):**
```bash
python3 parse_ofp.py        # Input/TG921_OFP.pdf → data/route.json
python3 met_engine.py       # Input/TG921_MET.pdf + route.json → data/airports.json
ANTHROPIC_API_KEY=<key> python3 notam_engine.py   # Input/TG921_NOTAM.pdf → augments airports.json + writes data/fir_notams.json
```
CLI scripts have **hardcoded TG921 paths and TAKEOFF_UTC** at module level. The Flask pipeline monkey-patches these per run — see "Architecture" below.

---

## Architecture

### Multi-leg / Multi-group model

The upload form accepts **1–4 OFP files** (legs). The pipeline groups them:
- **1–2 legs** → single group (group 1), single map tab
- **3–4 legs** → two groups (group 1 = legs 1–2, group 2 = legs 3–4), opens two map tabs

Each group gets its own output directory. The map URL uses `?g=1` or `?g=2` to select which group to display.

### Directory layout

```
uploads/<uuid>/          ← PDFs held here during pipeline only; deleted in finally block
  ofp_1.pdf, ofp_2.pdf, ..., met.pdf, notam.pdf

runs/<uuid>/             ← pipeline output (swept after 24 h on next upload)
  1/                     ← group 1
    flight_info.json     {legs: [{flight, dep, dest, acft, reg, date, etd, eta}, ...]}
    route_1.json         [{name, lat, lon, acct_min}, ...]  leg 1 waypoints
    route_2.json         leg 2 waypoints (if present)
    airports.json        see schema below
    fir_notams.json      see schema below
  2/                     ← group 2 (only when 3–4 legs uploaded)
    ...
```

### JSON schemas

**`airports.json`** — list of airports, each with per-leg weather and NOTAMs:
```json
[{
  "icao": "VTBS", "iata": "BKK", "name": "...",
  "lat": 13.68, "lon": 100.75,
  "runway_info": "01L/19R 4000 01R/19L 3700",
  "metar": "VTBS ...",
  "dist_nm": 45,
  "legs": [{
    "leg": 1,
    "ref_time": "2326Z",
    "taf_base": "24008KT 9999 SCT020",
    "becmg_in_progress": null,
    "active_overlays": [],
    "notams": [{"id": "...", "tier": 1, "body": "...", "summary": "...", "window": "..."}],
    "notam_covered": true
  }]
}]
```

**`fir_notams.json`** — list of FIRs that have active NOTAMs, each with per-leg entries:
```json
[{
  "fir": "VTBB", "name": "Bangkok FIR",
  "lat": 13.4, "lon": 100.6,
  "legs": [{"leg": 1, "ref_time": "2315Z", "notams": [...]}]
}]
```

**`flight_info.json`** — `{legs: [{flight, dep, dest, acft, reg, date, etd, eta}]}`

### Flask app (`app.py`) — the orchestrator

Pipeline runs in a background thread; progress streamed via `GET /api/status/<id>`.

**6-step pipeline per group:**
1. **OFP constants** — extract `(etd_utc, taxi_min, flt_min)` and `flight_info` per leg via `_extract_ofp_constants()` / `_extract_flight_info()`
2. **Route parsing** — monkey-patches `parse_ofp.OFP_PDF`, `parse_ofp.OUT_JSON`, `parse_ofp.FLIGHT_TIME_MIN`, calls `parse_ofp.main()` → writes `route_<n>.json`
3. **MET per leg** — monkey-patches `met_engine.MET_PDF`, `met_engine.ROUTE_JSON`, `met_engine.TAKEOFF_UTC`, `met_engine.OUT_JSON`, calls `met_engine.main()` → writes temporary `_airports_leg_<n>.json`
4. **Merge** — `_merge_airports_legs()` collapses per-leg airport lists into the multi-leg schema (one entry per ICAO with a `legs` array)
5. **NOTAM** — `_run_notam_step_multi()` attaches per-leg NOTAMs, AI-summarises all, writes `airports.json` and `fir_notams.json`
6. **flight_info.json** — written last

**Monkey-patching** is how per-flight isolation works — do not "fix" the hardcoded constants in the CLI scripts; they are the regression fixture.

**Data routes:**
- `GET /data/<group>/<filename>` — serves from `runs/<run_id>/<group>/`
- `GET /data/<filename>` — legacy fallback, serves from `data/` (MVP demo only)

**Concurrency:** Single worker required — `_current_run` dict is in-process. A second upload while a pipeline is running returns 429.

### Map (`index.html`) — no build step

- URL param `?g=1` or `?g=2` selects the group; all data fetches go to `/data/<GROUP>/<file>`.
- **Leg route colors:** leg 1 = blue (`#5b9ef4`), leg 2 = orange (`#f4a15b`).
- **Marker colors:** green = departure, red = destination, amber = turnaround (dep and dest same airport), light blue = enroute MET.
- **Multi-leg panel:** when an airport appears in multiple legs, the side panel shows a `LEG N · HHMMZ` divider between leg sections.
- **FIR diamond color:** red (`#ff8080`) when the FIR has at least one T1 NOTAM; near-black (`#1a1a2e`) otherwise. Logic: `f.legs.flatMap(l => l.notams).some(n => n.tier === 1)`. T2 navaid outages appear as T2 badges only.
- **T3 NOTAM collapse:** FIR panel shows first 5 T3 NOTAMs then a "show N more" toggle.
- **Runway chips** (`buildChips(runway_info, allNotams)`): glow red/orange when a T1/T2 NOTAM references that runway. Matching is suffix-aware — `04L` matches only chips with that exact end; bare `RWY 04` matches any chip sharing that numeric designator.
- **Runway filter** (`filterNotams(ids, rwy)` / `clearNotamFilter()`): tapping a highlighted chip filters NOTAMs across all leg sections. State kept in `_activeRwyFilter`.
- Both `legs`-schema and legacy flat schema are handled in `buildPanel()` / `buildFirPanel()`.

**`upload.html`** is the Flask-served upload page (`GET /`). Supports dynamically adding/removing legs 2–4 via JS.

**`airport_coords.py`** — downloads and caches the OurAirports CSV (`data/airports_raw.csv`) and exposes `load_coords() → {icao: (lat, lon)}`. Used by `met_engine.py` to look up coordinates for each MET airport. Auto-downloads on first run.

**`parse_met.py`** is a legacy script (pre-TAF-condensing, no runway_info). Superseded by `met_engine.py`; kept for historical reference only.

---

## Reference-Time Engine

**Core concept:** `ref_time` for any airport or FIR = time the aircraft is nearest to it.

```python
takeoff_utc = ETD + timedelta(minutes=taxi_min)   # NOT ETD directly — ACCT is from takeoff
ref_time = takeoff_utc + timedelta(minutes=nearest_waypoint_acct)
```

**Gotcha — ACCT vs ETD:** The OFP waypoint table `ACCT` column counts from takeoff (first SID fix, ACCT=0000), not from ETD. A 20-min taxi gap means anchoring at ETD gives every ref_time 20 min early.

**Gotcha — column offsets shift across PDF pages:** Parse by token position, not character offset. Row 1/2/3 field order within a line is stable; absolute column position is not.

**Gotcha — first/last row layout differs:** Override manually: first waypoint ACCT=0 (takeoff), last waypoint ACCT = OFP's stated flight time. Don't generalize the parser for these edge cases.

**Lat/lon coded form:** `N50020` → 50°02.0'N; `E008315` → 8°31.5'E (2-digit lat degrees, 3-digit lon degrees, remaining = minutes×10). Use row 3's coded form only — row 2 sometimes has a human-readable spelled form that's harder to parse.

---

## MET (TAF) Engine

**Key invariant — fold completed BECMG/FM into baseline:** A naive "show groups that overlap ref_time" approach is wrong. A BECMG that finished transitioning before ref_time must replace the baseline, not appear as an overlay.

```python
for g in sorted(groups, key=lambda x: x['start']):
    if g['type'] in ('BECMG', 'FM'):
        if g['end'] is None or ref_min >= g['end']:
            baseline = g['text']          # complete — folds in
        elif g['start'] <= ref_min < g['end']:
            becmg_in_progress = g         # in transition — show separately
    else:  # TEMPO / PROB
        if g['start'] <= ref_min < g['end']:
            active_overlays.append(g)
```

`FM` is an abrupt change — no in-progress state; treat as complete once its start passes.

**Regex order matters:** `PROB30 TEMPO` must be tried before bare `PROB30` or it gets mis-split.

**Validated test cases (TG921 fixture — regression checks):**

| Airport | Ref time | Expected result | What it proves |
|---|---|---|---|
| EDDF | 1305Z | `23007KT` | completed BECMG folds into baseline |
| OPLA | 1917Z | `26005KT 4000 FU SCT100` | `FM201800` folds in; `FM210400` excluded (future) |
| OPKC | 1925Z | base unchanged + BECMG IN PROGRESS | in-progress BECMG flagged, not folded |
| LTCC | 1608Z | base folded once, second BECMG in-progress | two sequential BECMGs handled correctly |
| VTBS | 2326Z | `24008KT 9999 SCT020`, no TEMPOs | both TEMPOs (different windows) correctly excluded |

**Known limitation:** BECMG/FM folding replaces the whole conditions string — if a BECMG restates only wind, the baseline becomes just that wind (visibility/cloud from original base is dropped). Revisit if partial-field merging is needed.

---

## NOTAM Engine

### Tier classification

`_classify_tier(body_lines, is_fir=False)` in `notam_engine.py`:

- **Airport NOTAMs** (`is_fir=False`): T1 = RWY CLSD / ILS/VOR/DME/NDB U/S / LOC U/S / APCH U/S / RESTRICTED AREA ACTIVE / DANGER AREA ACTIVE / ROUTE NOT AVBL. T2 = TWY CLSD / STAND CLSD / VDGS U/S / PAPI U/S / RADAR U/S / DVOR/DME SUSPENDED. T3 = everything else.
- **FIR NOTAMs** (`is_fir=True`): T1 = only RESTRICTED AREA ACTIVE / DANGER AREA ACTIVE / ROUTE NOT AVBL. T2 = VOR U/S / DME U/S / DVOR/DME SUSPENDED / NDB U/S (navaid outages along the route). T3 = everything else.

The `is_fir` distinction matters because navaid outages in a FIR NOTAM are not operationally equivalent to a navaid U/S at a destination airport — they do not warrant T1 (red) but are notable enough for T2 (orange) on the FIR diamond.

### Attribution

NOTAMs are attributed to airports by the **section header** (` VTBS / BKK`) they appear under, never by the NOTAM ID prefix (`VTBDC...` = issuing AIS office, not necessarily the affected airport).

### FIR centroid table

`notam_engine.FIR_COORDS` (141 entries) covers worldwide corridors. Centroids are approximate — they're haversine search targets only; the FIR marker is placed at the nearest route waypoint, not at the centroid. Any FIR code found in the NOTAM PDF but missing from `FIR_COORDS` prints a warning and is skipped. Add missing FIRs directly to `FIR_COORDS` in `notam_engine.py`.

`app.py` no longer has a separate `_EXTRA_FIR_COORDS` — everything is in `notam_engine.FIR_COORDS`.

### AI summarisation

`notam_engine._summarize_notams()` batches all NOTAMs (airport + FIR) into Anthropic API calls. `_run_notam_step_multi()` calls this once for airport NOTAMs and once for FIR NOTAMs, deduplicating across legs before sending. Running this step without an API key will raise an auth error.

---

## Input Document Format

Three PDFs per flight, Thai Airways dispatch format:

- **OFP** — Per-waypoint table (lat/lon + ACCT elapsed time), ETD, taxi time, flight time, aircraft/reg.
- **MET** — One block per airport: `ICAO -IATA - NAME` header, runway line (`RWY/RWY length_m ...`), `SA ...=` METAR, `FT ...=` TAF (terminated by `=`, may wrap lines). Covers departure, destination, alternates, ~40 enroute contingency airports. The runway line may span two physical PDF lines for airports with 5+ runways.
- **NOTAM** — NOTAMs already decoded to plain English (no raw Q-codes). Sections: `GENERAL INFORMATION`, `FLIGHT LEG INFORMATION`, `AERODROME INFORMATION` (sub-headed by airport), `ENROUTE INFORMATION` (FIR NOTAMs), `ADDITIONAL INFORMATION`. Each NOTAM has an ID, optional `*validity-window*` line, and free-text body.

---

## Deployment

Hosted on Railway at **https://web-production-2ec19.up.railway.app** (project: DocsBriefing).

`Procfile` defines the start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`. Single worker is required — the background pipeline state (`_runs` dict) is in-process and would break across multiple workers.

`app.py` binds to `0.0.0.0` and reads `$PORT` from the environment (falls back to 5001 locally). `ANTHROPIC_API_KEY` is set as a Railway environment variable — not in the repo.

**Deploy workflow:** push to `main` → Railway auto-detects, rebuilds, and does a rolling redeploy. No manual steps needed. `nixpacks.toml` is present but currently unused (Railway uses railpack instead).

---

## Test Fixture

**TG921, 20 JUN 2026, EDDF→VTBS** (B773E, HSTKZ) — `Input/TG921_OFP.pdf`, `Input/TG921_MET.pdf`, `Input/TG921_NOTAM.pdf`. These files are never modified. All validated results in the MET table above reproduce exactly against them. If a parser change shifts any of those results without a clear reason, that is a regression.

The CLI scripts (`parse_ofp.py`, `met_engine.py`, `notam_engine.py`) are hardcoded to this fixture — they are the regression test harness. The Flask app's monkey-patching approach is what enables multi-flight use.
