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

```
uploads/<uuid>/          ← PDFs saved here per upload (not browseable)
  ofp.pdf, met.pdf, notam.pdf

runs/<uuid>/             ← pipeline output per flight
  flight_info.json       {flight, dep, dest, acft, reg, date, etd, eta}
  route.json             [{name, lat, lon, acct_min}, ...]  (acct_min = minutes since takeoff)
  airports.json          [{icao, iata, name, runway_info, lat, lon, ref_time, dist_nm,
                           metar, taf_base, becmg_in_progress, active_overlays,
                           notams[], notam_covered}, ...]
  fir_notams.json        [{fir, name, lat, lon, ref_time, notams[]}, ...]
```

`runway_info` is a raw string from the MET PDF's second line (after the airport header, before `SA`), e.g. `"01/19 4000 02L/20R 4000 02R/20L 3700"`. Values are runway designator pairs + length in metres. Airports with 5+ runways may wrap across two lines; the parser accumulates them with a space join.

**Flask app (`app.py`) — the orchestrator:**
- Extracts `(etd_utc, taxi_min, flight_time_min)` from OFP on upload via `_extract_ofp_constants()`
- Computes `takeoff_utc = etd_utc + timedelta(minutes=taxi_min)`
- Runs the 4-step pipeline in a background thread, streaming progress via `GET /api/status/<id>`
- **Monkey-patches module-level constants** before calling each engine's `main()`:
  ```python
  parse_ofp.OFP_PDF  = ofp_path
  met_engine.TAKEOFF_UTC = takeoff_utc
  notam_engine.TAKEOFF_UTC = takeoff_utc
  ```
  This is how per-flight isolation works — the CLI scripts have hardcoded TG921 values, the Flask pipeline overrides them. Do not "fix" the hardcoded constants in the CLI scripts; they serve as the regression fixture.

**`_run_notam_step()` in `app.py`** replaces `notam_engine.main()` entirely — it calls the library functions directly so it can inject `takeoff_utc`, write to the run-specific directory, and AI-summarise both airport and FIR NOTAMs in batch.

**Map (`index.html`) — no build step:**
- Fetches `data/route.json`, `data/airports.json`, `data/fir_notams.json` relative to where it's served
- Flask serves these from the active run dir via `GET /data/<f>` (falls back to `data/` for the legacy MVP)
- Leaflet map layers: blue polyline (route) → circle markers (MET airports) → diamond markers (FIR NOTAMs)
- FIR diamond color: **red** only when the FIR has at least one T1 NOTAM (RESTRICTED AREA ACTIVE / DANGER AREA ACTIVE / ROUTE NOT AVBL — the route-blocking conditions); **near-black** (`#1a1a2e`) for everything else. T2 navaid outages do NOT color the diamond — they appear as T2 badges in the panel list only. Logic: `f.notams.some(n => n.tier === 1) ? "#ff8080" : "#1a1a2e"`. The white SVG border on `firIcon` keeps black diamonds visible on dark map tiles.
- **Runway chips** (`buildChips(runway_info, notams)`): renders runway/length pairs as styled chips in the panel header. Chips with T1/T2 NOTAMs referencing that runway glow red/orange. Matching uses suffix-aware logic against `n.summary + n.body` via regex `\bRWY\s+(\d{2}[LCR]?(?:\/\d{2}[LCR]?)?)`: if the captured token carries an L/C/R suffix it must match the chip's exact ends (prevents `04L` from matching the `04R/22L` chip); if the token is bare (e.g. `"RWY 04"`) it matches any chip sharing that numeric designator.
- **Runway filter** (`filterNotams(ids, rwy)` / `clearNotamFilter()`): tapping a highlighted chip filters the NOTAM list to matched rows only. State is kept in `_activeRwyFilter`. The onclick attribute uses single-quoted outer quotes so `JSON.stringify` double-quotes inside are safe.

**`upload.html`** is the Flask-served upload page (`GET /`). It posts the three PDFs to `POST /upload` which kicks off the background pipeline.

**`airport_coords.py`** — downloads and caches the OurAirports CSV (`data/airports_raw.csv`) and exposes `load_coords() → {icao: (lat, lon)}`. Used by `met_engine.py` to look up coordinates for each MET airport before computing ref_time. Auto-downloads on first run; subsequent runs use the cached file.

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

`notam_engine._summarize_notams()` batches all NOTAMs (airport + FIR) into Anthropic API calls. The Flask pipeline calls this once for airport NOTAMs and once for FIR NOTAMs after time-filtering. Running this step without an API key will raise an auth error.

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
