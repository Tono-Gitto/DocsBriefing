# Backlog — low-priority findings from the July 2026 code review

These are the deferred LOW/POLISH items from a principal-SWE review (commit
`0c9044b` fixed all Critical/High/Medium findings). Each item is
self-contained: file, problem, fix. None are urgent; none change dispatch
correctness.

**Ground rules for whoever picks these up:**
- Run `python3 -m pytest tests/ -q` before and after — all 50 tests must stay green.
- CLI fixture regression: `python3 parse_ofp.py && python3 met_engine.py` must
  reproduce every ✓ in the CLAUDE.md validated table exactly.
- Do NOT "fix" the hardcoded TG921 constants in the CLI scripts — they are the
  regression fixture (see CLAUDE.md).

---

## B1 — Scroll-to-NOTAM never works (runway chip filter)
`index.html` — in `filterNotams()`, the line `panel.scrollTop = first.offsetTop - 80`
sets scrollTop on `#panel`, but `#panel` is a fixed flex container — the actual
scroll container is `#panel-content` (`overflow-y: auto`). The scroll is a silent
no-op. **Fix:** `document.getElementById('panel-content').scrollTop = ...`.
Verify by opening an airport with many NOTAMs and tapping a runway chip — the
list should jump to the first match.

## B2 — Stale prev/next nav bar under the FLIGHT panel
`index.html` — `openFlightPanel()` replaces the panel content but does not reset
navigation state, so the bottom nav bar still shows the previous airport's
prev/next buttons. **Fix:** inside `openFlightPanel()`, set `_navIndex = -1;`
and call `_updateNavBar();`.

## B3 — `%-d` strftime is POSIX-only
`app.py` (`_fmt_win`) and `notam_engine.py` (`main()`, two inline window
formatters) use `%-d`, which raises on Windows. Harmless on macOS/Railway but
breaks any Windows dev machine. **Fix:** format day with an f-string
(`f"{dt.day} {dt:%b %Y %H:%M}Z"`) or `lstrip("0")`.

## B4 — upload.html missing viewport meta
`upload.html` has no `<meta name="viewport" content="width=device-width, initial-scale=1.0">`
in `<head>` — the upload page renders desktop-sized on iPad (the primary
device). `index.html` already has it; copy that line.

## B5 — Dead code: `cur_is_ci`
`notam_engine.py` — `cur_is_ci` is assigned in three places inside
`parse_notam_pdf()` but never read. Delete the variable and its assignments
(including the `nonlocal` mention).

## B6 — Duplicated helpers
Consolidate (small shared module or pick one home; keep public behavior identical):
- `_haversine_nm` exists in both `met_engine.py` and `notam_engine.py`.
- PDF line-cleaning: `met_engine._clean_lines` vs `notam_engine._get_clean_lines`
  (same shape, different skip regex — factor the loop, parameterise the regex).
- Window display formatting: `app._fmt_win` vs the inline strftime expressions
  in `notam_engine.main()`.
- Anthropic model ID `"claude-haiku-4-5-20251001"` is hardcoded in both
  `notam_engine.py` and `app.py` — promote to one constant.
- `index.html`: `notamRow()` and `notamRowUnified()` are near-duplicates —
  merge into one function taking optional leg chips.

## B7 — `load_coords()` re-parses the OurAirports CSV per leg
`airport_coords.py` — `load_coords()` reads the ~80k-row CSV on every call, and
the Flask pipeline calls it once per leg. **Fix:** memoize with a module-level
cache (see `fir_coords._cache` for the existing pattern in this repo).

## B8 — Magic numbers → named constants
- `app.py` `_extract_ofp_constants`: taxi default `20` (min) when TAXI line missing.
  Also: emit a pipeline warning when the default is used — it shifts every ref_time.
- `app.py` `_run_notam_step_multi`: FIR flight window `timedelta(hours=24)`.
- `app.py` `_fir_marker_position`: `threshold_nm=10.0` is already a kwarg — fine,
  but name the taxi/FIR constants at module top with a one-line comment each.

## B9 — Frontend fetch robustness
`index.html` — none of the `fetch(...)` chains check `r.ok`; a 404 surfaces as a
confusing JSON parse error ("Load failed: Unexpected token..."). Add `r.ok`
checks with a clear message. Also `app.py` `_PROGRESS_HTML`: the poll loop stops
on 404 (handled) but keeps polling forever on network errors, appending a
"Poll error" line every 2 s — stop after ~5 consecutive failures.

## B10 — Server paths leak into the browser
`app.py` `_run_pipeline` except-block does `_progress(f"ERROR: {exc}")`;
exception text can contain absolute server paths (e.g. from pdfplumber).
**Fix:** send a generic message + exception class to `_progress`, keep the full
traceback in server logs only (it's already printed).

## B11 — Leaflet from unpkg CDN, no SRI, no offline
`index.html` loads leaflet 1.9.4 JS/CSS from unpkg with no `integrity` hashes,
and the map is unusable offline (cockpit use). **Fix:** vendor the two files
into a `static/` dir served by Flask (add a route or set `static_folder`), or at
minimum add SRI hashes. Remember `mvp/index.html` is a frozen snapshot — leave it.

## B12 — Same OFP PDF opened up to 3× per leg
`app.py` — `_extract_ofp_constants()`, `_extract_flight_info()`, and
`_extract_alternates()` each do `pdfplumber.open(ofp_path)` on the same file.
**Fix:** extract page-1 text (and full-doc lines for alternates) once and pass
strings into the three functions. Keep signatures backward-compatible or update
the two call sites in `_run_pipeline` — and the tests import none of these, so
no test churn expected.

## B13 — Fixture integration tests (deferred half of the test plan)
`tests/` currently covers pure functions only. Add `@pytest.mark.integration`
tests that run against `Input/TG921_*.pdf` and assert the CLAUDE.md validated
table (EDDF 1305Z/23007KT, OPLA, OPKC BECMG-in-progress, LTCC, VTBS) plus the
NOTAM parse expectations in `notam_engine.main()`'s validation block. Gate on
file existence (`pytest.mark.skipif`) so CI without fixtures still passes.
No API key needed if you test `parse_notam_pdf` + `_is_active` directly rather
than `main()`.
