# ADR 0007 — OM-A Issue 02 Rev 02: what changed for this tool

Date: 2026-09-18
Status: Accepted — implemented in `met_engine.py`, `app.py`, `index.html`. §3 (crosswind
computation) added the same day at the user's request; it supersedes §1's "advisory only"
wording.

## Context

OM-A moved from **Issue 02 Rev 01** (09 MAY 25, eff 16 APR 26) to **Issue 02 Rev 02**
(04 SEP 26, eff 16 SEP 26). ADRs 0005 and 0006 were written against Rev 01. This record
covers only the parts of the delta that touch rules this tool implements. It was built from
the revision diff (`../Docs_Diff/`: the Revision Highlights plus a word diff of every
bookmarked section), and every changed table was checked against both page renders.

### Verified unchanged: no code change

| Rule | Where it is used | Evidence |
|---|---|---|
| §8.1.7.5.2 **Table 3** (planning-minima increments) | `MINIMA_TABLE3`, `index.html` | Page 8-45 is still stamped Rev 01. The only change in the §8.1.7.5.2 text is a sentence break (`"…aerodrome only when"` → `"…aerodrome. Only when"`) |
| §8.1.3.3.2 **RVR vs DH/MDH** | `equipment_minima.py` → `rvr_table.json` | Pages 8-15 to 8-18 are Rev 01, and the section text is identical |
| §8.1.3.3.6 **failed/downgraded equipment** table | `equipment_minima.py` (ADR 0006) | Pages 8-19 to 8-22 are Rev 01, and the section text is identical |
| §8.1.3.2.4, §8.1.3.3, §8.1.3.3.1, §8.1.5.1, §8.1.7.4.1/.2, §8.1.7.5.5, §8.4.7, §8.5.6.7/.8 | cited across ADRs 0005 and 0006 | Section text is identical |
| §8.1.6 **ceiling and visibility** applicability | `_resolve_planning_minima()` | Every changed word in the §8.1.6 table is about wind or gusts, or rewords "from the time of start" as "from the start" with the same meaning. The TEMPO transient column ("Not applicable") and the PROB TEMPO column ("may be disregarded") are unchanged |

Also checked and out of scope, because the tool does not implement them: the §8.1.3.2.5
note now points to §8.1.7.3.3 instead of §8.1.7.5.1; §8.1.2.5 moves the PDP/secondary-destination
paragraph; §8.4.6.2 relabels the CAT III table with LIDO names but keeps the values.

## Decision

### 1. §8.1.6: gusts are now applied (the check that uses them is §3)

Rev 01's row for *Destination, Take-Off Alternate, Dest. Alternate, Fuel ERA (ETA ±1 HR)*
said `Gusts: may be disregarded` in four columns: FM/BECMG AT, BECMG deterioration, BECMG
improvement, and persistent TEMPO. Rev 02 replaces all four with **`Gusts: exceeding
crosswind limits should be fully applied`**, which is what the EDTO ERA row already said. The
transient/shower TEMPO column still allows mean wind *and* gusts to be disregarded, and
PROB TEMPO still allows its deterioration, including mean wind and gusts, to be disregarded.

`_resolve_planning_minima()` now also returns `applicable_gust_kt` / `gust_wind` /
`gust_source`: the highest gust (converted to knots from MPS/KMH) found in **the same pool the
ceiling/vis search reads**. That pool is the baseline, the in-progress BECMG target, and every
overlay that isn't disregarded. The gust therefore follows §8.1.6's exclusions with no
second filter. A `max()` over the pool gives the right direction for the same reason ADR
0005 §5's `min()` does: an improving BECMG can never beat a gustier baseline. The three
fields are added to `_merge_airports_legs()`'s whitelist, which is the known way new
per-leg fields get lost.

The gust fields stay in `airports.json`, but the crew now sees the computed **Crosswind
check** from §3. The gust-only advisory is shown only for legacy runs built before §3.

*Superseded:* the first version of this section rejected a crosswind PASS/FAIL for lack of
runway headings, a crosswind limit and runway condition. The user then supplied two of the
three: headings from the designator, and a 30 kt maximum. §3 builds the check on those and
keeps runway condition as a stated assumption.

**Rejected: adding gusts to `wx_tier`.** The fixtures in ADR 0005's Context (RKSI, LSZH,
VTBS, VHHH, OPKC, LTCC, EDDF, OPLA) must not change, and `wx_tier` is a severity indicator,
not a selection rule. `_classify_wx_tier` is untouched.

### 2. §8.1.3.2.3: the destination ceiling test is `>=`, and only for Type A or circling

Rev 01: *"For a Non Precision Approach or circling operation, the ceiling is above MDH."*
Rev 02: ***"For Instrument Approach Operation Type A or Circling operation, the ceiling at
or above MDH."*** On the page this is the second of two bullets. The first,
*"RVR/Visibility: at least the prescribed RVR/Visibility…; and"*, always applies. The
ceiling bullet applies only under its own condition.

Two consequences:

- **`>=` is now the rule as written.** KNOWN_ISSUES #13 and ADR 0006 §3 held the
  destination check open until *"the exact regulatory text is later obtained"*. Now that the
  text is available, the existing `_minimaVerdict` `>=` matches it and needs no change.
- **A Type B destination has no ceiling criterion.** Before this change, `_minimaCompute`
  compared the ceiling for every destination, so a Type B destination (DH < 250 ft) could
  FAIL on a ceiling the OM-A does not test. `_minimaCompute` now takes `ceilingApplies`.
  The destination branch passes `_destCeilingApplies(entry)`, which is true when
  `_approachType(base_height_ft) === "A"` (the same ≥ 250 ft definition ADR 0006 §5 uses) or
  when the stored `approach` label mentions `CIRCL…`. When it is false, the ceiling line says
  *"not required — Type B"*, and the ceiling is left out of `overall` instead of counting as
  a pass. The line stays visible, so the crew can still see the forecast figure.

This change only ever makes the verdict more permissive. At a Type B destination it can turn
a ceiling-driven **FAIL into PASS**, and an indeterminate ceiling's **CANNOT DETERMINE into
PASS**, because once there is no ceiling criterion an unknown ceiling no longer matters. That
is why it rests on the quoted text and nothing weaker. **The gate applies only to the destination.** Table 3 (alternate/ERA/isolated
destination) keeps its ceiling increment on every row, including Type B rows 1–2.

### 3. Crosswind check: most headwind first, then crosswind vs 30 kt

Built at the user's direction: use the runway designators at the top of each MET block,
take the runway with the most headwind, compute its crosswind, and compare it with a
**30 kt maximum**. `met_engine._resolve_crosswind(runway_info, …)` writes a per-leg
`crosswind` object. It is whitelisted in `_merge_airports_legs()`.

- **Pool.** `_planning_sources()` is now the one §8.1.6 filter behind ceiling/vis, gust and
  crosswind: baseline, in-progress BECMG target, and every overlay not disregarded.
  Transient-TEMPO and PROB TEMPO winds never count, so the three checks can't disagree
  about what applies. `_resolve_planning_minima` was refactored onto it, and all of its
  existing tests pass unchanged.
- **Runway choice, per wind.** Each applicable wind is placed on the runway whose heading
  gives the largest `V·cos Δ`, because crews pick the runway for the wind. Heading is
  **designator × 10, magnetic**. Parallel ends sharing a number (`19L/19R`) collapse into one
  entry, since their components are identical. **Runways under 2000 m are skipped when a
  longer one exists.** The MET line lists strips no B777 would use (ESMS 11/29 is 800 m), and
  choosing one for its headwind would under-report crosswind on the runway actually used.
  **2000 m is not set by any document.** Unlike the 30 kt limit (given by the user) or 250 ft
  (the OM-A's Type A/B definition), it is a plausibility screen chosen to exclude obviously
  unusable strips, not a landing-distance figure. It has been checked against all 11 fixture
  MET PDFs: every runway pair is followed by its length, including the joined 5+ runway
  lines (RKSI: `15R/33L 3750 15L/33R 3750 16L/34R 4000 16R/34L 3750`).
- **Speed.** Crosswind is `V·|sin Δ|` for the mean and for the gust. The **gust value decides
  the verdict** when present: §8.1.6 Rev 02 applies gusts, and mean ≤ gust, so the gust check
  covers the "mean wind within limits" cell too. MPS/KMH are converted to knots.
- **Worst case.** The pool's highest effective crosswind is reported with its wind token,
  source group, runway, headwind, mean and gust crosswind.
- **Verdict.** `> 30` → `fail` ("exceeding"). `25–30` → `marginal`. Below 25 → `pass`.
  Airport has a runway line but no applicable wind (no TAF) → `unknown`, never a silent pass.
  No runway line at all → `null`, and nothing is rendered.
- **`VRB`** has no direction, so no runway can be chosen. The full speed (and gust) is taken
  as crosswind, never treated as 000°.
- **Kept separate from Planning Minima.** The crosswind verdict has its own row
  (`_crosswindHtml`) and never feeds `_minimaVerdict`/`overall`. It is a different test from
  §8.1.3.2.3/Table 3, and it rests on a dry-runway assumption. As in §1, the row is gated on
  role alone.

**Accepted imprecision, and why the marginal band is 5 kt.** TAF wind is **true**, while the
designator is **magnetic** and rounded to 10°. Variation isn't corrected because the tool has
no source for it: `airports_raw.csv` carries none, and no geomag model is installed. The
combined heading error can reach ±5° from rounding plus the local variation. At a 30–40 kt
gust that shifts the crosswind by a few knots, which is why the 5 kt band just below the
limit reads MARGINAL instead of PASS. The row states the assumption on screen.

**Rejected alternatives:**
- Hand-entering variation per ICAO in the minima store. It silently defaults to 0° and is
  wiped on redeploy (KNOWN_ISSUES #11).
- Adding a WMM dependency. It adds a package and an expiring model epoch for one line.
- Recalling per-airport true headings or variation from memory. That produces authoritative-looking numbers
  nobody can check.

**Runway condition is not modelled:** 30 kt is the maximum, and the row tells the crew to
reduce it for wet or contaminated runways. **NOTAM runway closures are not applied:** a
closed runway can still be the one chosen. Both are recorded in KNOWN_ISSUES #18.

## Consequences

- The tool follows **OM-A Issue 02 Rev 02**, and `CLAUDE.md` records that. The next
  revision needs the same check: diff §8.1.3.x, §8.1.6 and §8.1.7.5.2 and confirm the
  revision stamps on the table pages.
- **Every dest/alternate/ERA with a runway line now shows a Crosswind check row**, whatever
  its weather colour. A crosswind row with no Planning Minima block is deliberate (§1, §3),
  not a gate bug. Across the 11 fixture flights every result is PASS. RKSI on TG664 is the
  worked example: `06010G20KT` on RWY 34L/34R gives 2 kt headwind, 10 kt crosswind and
  20 kt gust crosswind.
- **Isolated destination is still not detected.** §8.1.3.2.3 (unchanged) says an isolated
  destination uses the destination-alternate criteria, which means Table 3. The tool
  treats every `dest` as a plain destination with zero margin. KNOWN_ISSUES #13 is narrowed
  to this remaining gap.
- **Tests:** `tests/test_met_engine.py::TestPlanningGustAdvisory` covers each §8.1.6 column
  (applied: baseline, in-progress BECMG, persistent TEMPO, bare PROB; disregarded: transient
  TEMPO, PROB TEMPO), the unit conversion, and that the gust never changes a ceiling/vis
  field. `tests/test_app_helpers.py` covers the merge whitelist.
  `tests/test_integration.py::TestTG664GustAdvisory` runs it end to end through the real
  OFP → route → ETA path. **RKSI is TG664's destination alternate, and at its ETA it carries
  `06010G20KT`, wx_tier GREEN.** Running steps 1–4 of the pipeline over all eleven fixture
  flights, this is the only dest/alternate/ERA leg with an applicable gust, so it is the only
  fixture that renders the Crosswind check row. That row is shown by a role gate while the
  Planning Minima block for the same leg stays hidden. The client-side
  Type A/B gate has no JS harness, the same gap ADR 0006 §3 records. It was checked by
  hand in node against `_minimaCompute`: Type B at a 150 ft ceiling below a 200 ft DH →
  PASS; Type A at a 150 ft ceiling below a 440 ft MDH → FAIL; ceiling equal to MDH → PASS.

## References

- OM-A Issue 02 Rev 02 §8.1.6, page 8-26: *Application of Aerodrome Forecasts (TAF & TREND)
  to Pre-Flight Planning*
- OM-A Issue 02 Rev 02 §8.1.3.2.3, page 8-13: *Planning Minima for Destination*
- OM-A Issue 02 Rev 02 §8.1.7.5.2 / Table 3, pages 8-44 and 8-45 (checked, unchanged)
- `../Docs_Diff/Manual_Revision_Diff.html`, card `oma-gusts` (tier *must*) and card `oma-type-a`
- ADR 0005 §5 (the §8.1.6 pool), ADR 0006 §3 and §5 (destination role, Type A/B)
