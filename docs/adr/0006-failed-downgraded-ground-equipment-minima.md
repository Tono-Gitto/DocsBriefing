# ADR 0006 — Effect on landing minima of temporarily failed or downgraded ground equipment (OM-A §8.1.3.3.6)

Date: 2026-08-27
Status: Accepted — implemented in `equipment_minima.py`, `app.py`, `index.html`
(see Verification)

## Context

OM-A §8.1.3.3 delegates aerodrome operating minima to Lido mPilot, then immediately carves
out the case this tool can help with:

> However, flight crew shall be able to determine the revised DH/MDH and/or RVR/VIS,
> particularly in situations where changes occur due to temporarily failed or downgraded
> ground equipment or an increased Obstacle Clearance Altitude (OCA) is promulgated through
> NOTAMs.

The dispatch package already puts both halves of that determination in front of the crew and
makes them do the join by hand: the NOTAM PDF says `PALS CAT 1 RWY 18 AND SALS RWY 36 U/S`,
and §8.1.3.3.6's table says what that costs. The ask: do the join, and show the working.

ADR 0005 is the direct parent. It built the alternate/ERA Planning Minima block on a
hand-entered per-ICAO store (`{row, base_height_ft, base_rvr_vis_m}`) because the base
minima are unreachable by construction — there is no path from this tool to Lido. **That
gap is not closed here either.** What changes is that §8.1.3.3.6's re-determination turns
out to be *computable from data ADR 0005 already stores*:

- `Approach lights → Minima as for NALS` is a **lighting-class substitution**, not an
  additive penalty. It feeds a second lookup — "RVR versus DH/MDH" — whose row index is the
  DH/MDH. ADR 0005 §2 already defines `base_height_ft` as exactly that ("the DH/MDH — height
  above the aerodrome, never DA/MDA").
- The three approach-lights rows output an **absolute** class (NALS / BALS / IALS), so the
  runway's prior lighting class is never an input. The one thing we could not source is the
  one thing the table does not need.

So the chain is closed, and it is three steps, not one addend:

```
failed equipment ──▶ class or floor (§8.1.3.3.6 table)
                          │
        base_height_ft ──▶ RVR vs DH/MDH lookup ──▶ re-determined RVR
                          │
                     max(that, charted RVR)   ← §8.1.3.3.6 Step 3, §8.1.5.1
                          │
                     + Table 3 increment      ← only where a planning role exists
```

The OM-A supplies two worked examples with published answers, and both reproduce exactly:

| | Example 1 | Example 2 |
|---|---|---|
| Given | Cat C, VOR DME RWY 36, APL U/S | Cat D, LOC DME RWY 18, OCA/H raised 790/(766) |
| Lookup | MDH 440 @ **NALS**, band 421–440 | MDH 766 @ **IALS**, band "661 and above" |
| OM-A answer | 440 ft − **2.0 km** (MDA 460) | MDA 790 (MDH 766) — **RVR 2400 m** |

Example 1's aerodrome shape (VOR/DME RWY 36, an APL NOTAM) and Example 2's exact
figures (`790/(766)`) both appear in the TG638 fixture's VTBU block. The published examples
are therefore free regression fixtures for the lookup table (§8 below).

Three constraints shape every decision:

- **ADR 0005's surface is load-bearing and must render byte-identically** on any airport
  this feature does not touch. Its verification pinned WMKP's `base + increment = required`
  arithmetic against a real Flask run.
- **The base minima stay manual.** This ADR adds fields to the same hand-entered store; it
  does not derive anything from an approach inventory. ADR 0005 rejected that as Option A
  and nothing here changes the reasoning.
- **A silent miss is the failure mode to design against.** The output is a number a crew
  acts on. Every gap in this feature renders as a visible statement of what is missing,
  never as an omitted line and never as a substituted guess.

## Decision

### 1. Scope: failed/downgraded equipment only — two adjacent cases explicitly excluded

The trigger is a facility from §8.1.3.3.6's table appearing as failed or downgraded in an
airport's NOTAMs. Two things that look adjacent are **out**:

- **An approach-removal NOTAM** — one taking out the *primary* aid an approach is flown on
  (`ILS RWY 18 U/S`, `LOC U/S`, `VOR U/S`, `NDB U/S`) — is not in the table at all; that table
  degrades an approach you are *still flying*. It keeps its existing T1 tile and produces no
  finding. In particular **the tool never re-derives, re-validates, or flags the Table 3
  row.** Table 3's rows are defined by what approaches are usable ("≥2 usable Type B",
  "1 usable Type A"), so an aid going U/S can in principle invalidate a stored row — but a
  typical alternate has several approaches, the tool has no approach inventory to reason
  with, and a "row may no longer hold" banner on every navaid NOTAM is noise a crew learns
  to ignore. **The crew picks the row.**

  **This exclusion is narrower than "any navaid NOTAM," and the boundary is the table itself.**
  `Navaid stand-by transmitter`, `Outer marker`, `Middle marker (ILS only)` and `DME` *are*
  listed facilities with their own outcomes — including the outer marker's
  `FOR CAT I: Not allowed…` — so they are in scope and produce findings. The test is
  membership in §8.1.3.3.6's 14 rows, not whether the failed item happens to be a navaid.

  **Implementation note, learned the hard way:** the exclusion needs *no* guard at all. No
  row's pattern matches ILS/LOC/VOR/NDB/GLS and net 2 is anchored on lighting and marker
  vocabulary, so `ILS RWY 18 U/S` yields nothing on its own. A broad "does this line mention
  a navaid" guard *was* written first, and it silently ate `APCH LGT RWY 36 U/S` — the
  commonest NOTAM spelling of approach lights — by matching its bare `APCH`. That is a false
  negative in a feature whose premise is that false negatives are the dangerous direction,
  and it was invisible until a live render produced an empty block. The only surviving
  narrow guard is `_COMPOUND_DME_RE`: a DME named as part of a compound navaid (`ILS DME`,
  `VOR/DME`) is that aid's ranging channel, not the table's standalone DME facility.
- **A raised OCA/H NOTAM** (`VTBDC0333/23`) is not failed or downgraded equipment. Example 2's
  method never touches the equipment table — it enters "RVR vs DH/MDH" directly with the
  NOTAM's MDH. Excluding it has one clean consequence worth stating: **the DH/MDH has exactly
  one source, the store, never a NOTAM.**

§8.4.7 (CAT II/III, with LVO approval) is also out of scope — this feature implements
§8.1.3.3.6, the *without* LVO approval table, only.

### 2. The trigger is the NOTAM, not the role — so it applies to every airport

The finding renders for **any** airport whose NOTAMs contain a table facility: departure,
destination, alternate, fuel ERA, `rcf_altn`, or one of the ~40 enroute contingency airports
in `airports.json`. No triggering NOTAM, no block.

This is what makes "every airport" affordable. ADR 0005's block is role-gated because a
planning-minima entry must be hand-typed and 40 enroute airports of hand-entry is a burden
nobody carries. A NOTAM-triggered block has no such cost: the enroute airports produce
nothing until something is actually broken at one.

### 3. Two layers — role-independent re-determination, role-dependent comparison

§8.1.3.3.6 re-determines **landing** minima. That is role-independent: `APL U/S → NALS →
RVR vs DH/MDH → max(output, charted)` is the same computation whether the aerodrome is your
destination, your alternate, or a field you would only ever see on a diversion.

Only the comparison layered on top is role-dependent:

| Role on that leg | Rule | Required RVR/VIS | Built in v1 |
|---|---|---|---|
| `dest_altn` / `era` / `rcf_altn` | §8.1.7.5.2 Table 3 | re-determined base **+ row increment** | **yes** |
| destination (`dest`, `rcf_dest`) | §8.1.3.2.3 | re-determined base, **no increment**; NPA/circling ceiling above MDH | **deferred** |
| anything else | none | the re-determined landing minima *is* the answer | n/a — layer 1 only |

**Layer 2's destination variant is deferred out of v1.** Layer 1 renders at a destination like
anywhere else and is independently correct there — *"your destination's approach lights are
out, here is your re-determined RVR"* needs no planning rule to be true, and the destination
is the aerodrome the crew is actually landing at. What layer 2 would add is only the PASS/FAIL
comparison, and it is a genuinely third arithmetic: `ceiling above MDH` is a strict inequality
against a different quantity, not `MDH + 0`. Shipping it would also need
`minima_snapshot.json` extended to destination ICAOs (ADR 0005 §7 snapshots only
alternate/ERA/`rcf_altn`), which §11 deliberately does not do. A half-specified third rule
with no snapshot coverage and no test is how a wrong number reaches the aircraft.

At a destination, layer 2 therefore renders one line — *"destination planning minima
(§8.1.3.2.3) not computed — compare manually"* — the same graceful-miss shape used everywhere
else here. Recorded in `docs/KNOWN_ISSUES.md`.

The ordering (re-determine, *then* increment) is confirmed by Table 3's own footnotes —
`* The higher of the usable DA/H or MDA/H`, `** The higher of the usable RVR or VIS` — the
increment's input is the usable aerodrome operating minima, which is exactly what
§8.1.3.3.6 revises. §8.1.5.1 corroborates: *"the applicable minima should be the highest of:
state minima… Lido approach charts… NOTAMs temporary restrictions."*

`_minimaRole()` therefore changes from a boolean to a role discriminator. That is the one
signature change that ripples.

**§8.1.3.2.2's takeoff alternate is unreachable, not excluded:** `_extract_alternates()`
parses only `dest_altn`, `era`, `rcf_dest`, `rcf_altn` — there is no takeoff-alternate
extraction in the OFP parser at all.

### 4. Store schema: an approach label, and `row` demoted to optional

Two additions to ADR 0005 §2's per-ICAO entry:

```
{ "approach": "VOR DME RWY 36",   // new, optional
  "base_height_ft": 440,
  "base_rvr_vis_m": 1500,
  "row": 5,                       // now optional
  "updated": "2026-08-27" }
```

**`approach` exists because every finding is runway-specific and the store was not.**
`SALS RWY 36 U/S` may only degrade the stored base pair if that pair *is* the RWY 36
approach. If the crew entered VTBU as ILS RWY 18 (200 ft / 550 m) and the lights out are on
36, applying NALS produces a confident 2400 m that is simply wrong. Only the runway
designator is parsed out of the label, matched with the suffix-aware rule `buildChips()`
already uses for runway chips (`36` matches `RWY 36`; `18L` matches only that exact end).
The label also titles the block, so the arithmetic names the approach it is about instead
of being a bare number.

Free text rather than a structured approach picker, for the same reason ADR 0005 §2 chose
manual row selection: this is a human judgment already made by whoever read Lido, and a
structured field would need an approach inventory this tool has no source for.

**`row` becomes optional** because §2 makes the feature role-independent: an enroute
contingency airport has a DH/MDH and an approach but no Table 3 row, because it has no
planning role. `PUT /api/minima/<icao>` currently validates `row` as required, 1–6; it
becomes required only where the ICAO holds a planning role on some leg.

### 5. Type A / Type B is derived, not entered

§1.x defines **Type A = an instrument approach operation with an MDH or DH at or above
250 ft**; **Type B = an operation with a DH below 250 ft**. So the table's column is
`base_height_ft >= 250 ? "A" : "B"` — definitional, not inferred, and needing no new field.

The columns differ on only four rows (outer marker, middle marker, centre line lights, TDZ
lights). All three approach-lights rows are identical across both columns, so the headline
case never depends on this.

Circling (Table 3 row 6) has no column of its own; a circling MDH is ≥ 250 ft and lands in
Type A. Recorded as a known imprecision rather than special-cased.

### 6. Detection: a deterministic two-net extractor, never the AI

A new module yielding `(facility, runway | None, notam_id, window)` — **one finding per
facility per runway**, so `VTBDC3302/26`'s `PALS CAT 1 RWY 18 AND SALS RWY 36 U/S`
decomposes into two independent findings.

**Deterministic, because** the source is a regulatory table with 14 fixed rows, the output is
a number a crew acts on, and an AI miss is *silent* — the block would simply not render and
nobody would know. `_summarize_notams()`'s existing degrade-to-first-body-line tolerance is
right for prose and wrong for this. The codebase precedent is unambiguous: `_classify_tier`'s
regex tables, `_WX_PHENOMENA_RE`, and ADR 0005 §5's transient/persistent pair.

**A new extractor, not an extension of `_T1_LINE_RE`.** That regex already matches
`PALS.+U/S` and `SALS.+U/S`, but it is a boolean whole-NOTAM tier classifier carrying no
facility identity and no runway. Coupling minima semantics to tier semantics would drift
both — the same "a different partition, not a reuse" argument ADR 0005 §5 made.

Two nets, because a false negative is the dangerous direction:

- **Net 1 — mapped.** 14 patterns, one per table row, each with its NOTAM-vocabulary
  aliases: `PALS`/`SALS`/`ALS`/`APCH LGT`/`APL` → *Approach lights*; `RCLL`/`CL LGT` →
  *Centre line lights*; `RTZL`/`TDZ LGT` → *TDZ lights*; `REDL`/`RENL`/`THR LGT` → the
  combined *edge/threshold/runway end lights* row; and the no-effect rows (`DME`,
  navaid standby transmitter, RVR assessment systems, taxiway lighting) which are matched
  precisely so they can be reported as considered-and-harmless.
- **Net 2 — unmapped.** A deliberately broad "some facility is U/S" shape that net 1 did not
  claim. It does **not** drop silently. It renders as its own finding: *"equipment failure
  detected, not among the facilities §8.1.3.3.6 permits for this determination — minima
  cannot be re-determined here."* That is the OM-A's own closed-list rule stated back to the
  crew (*"Only those facilities mentioned in Table below should be acceptable…"*,
  *"Multiple failures of runway lights other than those indicated in the table should not be
  acceptable"*), not a gap being papered over. It doubles as the feature's own bug report:
  anything in net 2 across the fixture PDFs is a missing alias in net 1.

### 7. A four-outcome lattice, combined per runway

The table's cells are not all numbers. A finding evaluates to one of:

| Outcome | Example row |
|---|---|
| `NO EFFECT` | navaid standby transmitter, DME, RVR assessment systems, taxiway lighting |
| `CLASS DOWNGRADE` (NALS / BALS / IALS) | the three approach-lights rows |
| `RVR FLOOR` (a hard metre value) | centre line lights → `RVR 750 m` |
| `NOT ALLOWED` | edge/threshold/end lights at **night**; outer marker for CAT I |

Combined **per runway**, per *"Failures of approach and runway lights are acceptable at the
same time, and the most demanding consequence should be applied"*:

```
NOT ALLOWED  ⊐  max( RVR floors, class-downgrade lookup, charted )  ⊐  NO EFFECT
```

A class downgrade resolves through RVR-vs-DH/MDH into a number *first*, then joins the
`max`. `NOT ALLOWED` dominates and is never averaged into a number.

**Three** cells are conditional — they do not resolve from NOTAM text alone. Two are treated
asymmetrically on purpose: compute the one that depends on *our aircraft*, refuse the one
that depends on *the world*. The third is refused for the same reason as the second.

- **Day / Night** (`edge, threshold and runway end lights` → *Day: no effect / Night: not
  allowed*) — **never computed.** `ref_iso` and lat/lon make civil twilight computable to the
  minute, and it will be tempting. The OM-A delegates it (*"See LIDO RM, LAT – Sunrise and
  Sunset Table"*), it is the same Lido substitution ADR 0005 declared permanently out of
  reach, and a boundary misclassification flips `no effect` → `NOT ALLOWED`, the largest
  swing this feature can produce. Both branches render, with the leg ETA beside them.
- **Flight director / Autoland** (`centre line lights`, `TDZ lights` → *no effect if flight
  director or Autoland, otherwise RVR 750 m*) — **resolved to no effect, with the reason
  shown.** This is a B777 tool briefing THAI B777 packages; every airframe in
  `flight_info.acft` has both. Rendering `no effect — flight director available` states the
  condition (so the MEL exception is catchable) without asking the crew a question about
  their own aeroplane whose answer is fixed.
- **Escape clauses on the marker and DME rows** — `Outer marker → FOR CAT I: Not allowed
  except if the required height versus glide path can be checked using other means, e.g. DME
  fix`; `NPA with FAF: no effect unless used as FAF`; `Middle marker → no effect unless used
  as MAPt`; `DME → no effect if replaced by RNAV (GNSS) information or the outer marker` —
  **never resolved.** Each turns on how the *specific* procedure is constructed (what
  identifies its FAF, what its MAPt is, whether a DME fix is available), which is chart
  knowledge this tool has no source for — the same Lido gap, reached from a different
  direction. Both branches render, exactly like Day/Night: `no effect if the FAF can be
  identified by other means · otherwise NPA not available`. The conservative branch is never
  silently assumed in either direction.

Aircraft category (§8.1.3.3.10: CAT C = B777-200/A330/A350…, CAT D = B777-300ER/B787…) is
derivable from `flight_info.acft` but **does not enter this lookup** — it governs circling
minima and §8.1.3.3.2's item-3 floors, neither of which this feature computes. Not built.

### 8. Time gate: ETA ± 1 h overlap, not point-in-time — and it must run in Python

An equipment finding is gated on its NOTAM's validity window **overlapping ETA−1h → ETA+1h**.

**All three window kinds `notam_engine` parses must be covered**, not just the absolute one:
`win_start`/`win_end` (§`_is_active`), `daily_windows` (`_parse_daily_windows`), and
`date_schedules` (`_parse_date_schedules`) — `_effective_tier` gates on all three at line 270,
and a gate here that reads fewer would over-apply findings on precisely the NOTAMs whose
schedules are most restrictive. Each kind is an overlap test against the band, not a
containment test against `ref_dt`.

**Known pre-existing gap, inherited not introduced:** VTBU's `VTBDJ6288/26` carries
`"25 0800-1200, 26 0230-1000"`, which parses to **neither** a daily window nor a date
schedule — `_DATE_SCHED_RE` requires a month name (`JUN 29 1900-2330`) and the quoted,
comma-separated day-of-month form matches nothing. That NOTAM therefore keeps only its
absolute 26-hour span and reads as continuously active through 26 AUG 0000–0230Z when it is
not. This affects the tiles today and would affect findings identically. It is not fixed here
(that NOTAM is an ILS flight check, not a §8.1.3.3.6 facility, so it produces no finding
either way) but it bounds how precise this gate can be, and it belongs in
`docs/KNOWN_ISSUES.md` rather than being discovered later as a minima bug.

The band, not the point, because the comparison is otherwise incoherent: the feature's output
is *"forecast over ETA±1h vs. required minima"*, and computing the required minima from
equipment status at a single instant leaves a figure valid for one minute of the two-hour
window it is compared against. It is also the conservative direction, and
`_is_active_for_flight()` already implements this overlap shape for FIR NOTAMs.

**This diverges from the NOTAM tiles, deliberately, in two ways — and the second is stronger
than it first appears.** `app.py:525-529` filters airport NOTAMs by point-in-time
`_is_active(win_start, win_end, ref_dt)` *before* writing `airports.json`, and keeps only
`{id, tier, body, window}`:

1. A NOTAM present but outside its daily window is downgraded to T3 by `_effective_tier` yet
   still yields a finding.
2. A NOTAM whose *absolute* window misses `ref_dt` but overlaps ETA±1h is **absent from the
   panel entirely** — so a finding can cite a NOTAM that has no tile at all.

Same species as ADR 0005 §8's `wx_tier` YELLOW beside a minima PASS: two rules answering two
questions, one point-in-time for salience, one banded for planning. It fails safe (the tile
under-warns, the minima over-warns), and it gets the same treatment §8 got — the block states
the failure's own window and the leg ETA inline. Case 2 is why the Source Pane tap target
(§9) is load-bearing rather than a nicety: it may be the crew's only route to that NOTAM's
text.

Consequence for the implementation: **the extractor must read `notam_db` from
`parse_notam_pdf()`, not `leg_entry["notams"]`.** `win_start` / `win_end` / `daily_windows`
never reach the client, and the point-in-time filter has already discarded the rows case 2
depends on. The gate cannot be applied client-side or downstream of that filter.

### 9. Computation seam: Python extracts, the client joins — and the lookup table ships as data

ADR 0005 §4's seam holds, and its reason is specific rather than stylistic: **editing a stored
minimum must not require a pipeline re-run.**

- **Python emits the findings** into `airports.json` per airport-leg: facility, runway,
  notam id, outcome kind and value, conditional flag, and the failure's window. Deterministic,
  derived from NOTAM text, independent of the store.
- **The client joins and does the arithmetic**, because it consumes `base_height_ft`, which is
  live-editable.
- **The RVR-vs-DH/MDH table is a Python constant served as data** —
  written into each group dir as `rvr_table.json`. NOT an `/api/*` route —
  `static/sw.js` treats those as network-only, so a briefing file rides ADR 0003's
  existing manifest precache with no service-worker change (`_build_manifest`
  enumerates the group dir, so it is listed automatically).

That third point is a deliberate divergence from `_MINIMA_ROWS`, which ADR 0005 put in
`index.html` as a JS constant with no Python copy and no test. Table 3 is six additions;
this is a 26-band × 4-class lookup whose boundaries (`421–440` / `441–460`) fail silently
when off by one, and the OM-A hands us two published worked examples that would catch exactly
that (§Context). A regulatory lookup table is data, not logic, and the manifest/precache
mechanism for shipping data offline already exists.

A 404 on the table (legacy run, or a pipeline that predates it) renders the class downgrade
and *"RVR table unavailable"* — never a number. Same graceful-miss discipline as an
unresolved Source Pane anchor.

New per-airport-leg field `equipment_findings` in `airports.json`. It does **not** need the
`_merge_airports_legs()` whitelist that `taf_base_src` and ADR 0005's seven fields require:
that whitelist governs fields arriving from `met_engine`'s per-leg output, and the merge
runs at step 4, *before* the NOTAM step. `equipment_findings` is attached to `leg_entry`
inside `_run_notam_step_multi()` at step 5 — after the merge — exactly like the existing
`notams` / `notam_covered` fields, so it survives by construction. (An earlier draft of this
ADR asserted the opposite; the pipeline order is what settles it.)

### 10. Surface: two blocks, adjacent, per leg; findings split three ways

**Layer 2 is gated on layer 1.** Planning Minima renders only on a leg that has at least
one equipment finding — the same gate as the Failed Ground Equipment block, so the two
appear and disappear together. This **narrows ADR 0005**, which rendered its block
unconditionally on every `dest_altn`/`era`/`rcf_altn` for every leg as a standing
§8.1.3.2.4 selectability check. In practice most aerodromes have no hand-entered base
minima, so that block printed `No entry — Enter minima` on every alternate of every leg and
the signal drowned in it.

The cost is real and accepted rather than overlooked: a weather-marginal alternate with all
equipment serviceable now shows no PASS/FAIL at all, even though §8.1.3.2.4 applies to it.
Recorded as KNOWN_ISSUES #16. The gate is two lines at the top of `_minimaBlockHtml`;
deleting them restores ADR 0005's always-on behaviour. If the always-on check is wanted
back without the noise, the narrower fix is to suppress only the `No entry` state.

**Two blocks, not one**, mirroring §3's two layers:

- `FAILED GROUND EQUIPMENT · §8.1.3.3.6` — layer 1, renders wherever a finding exists, for
  any airport, with no role gate. It therefore **cannot live behind `_minimaBlockHtml`'s
  opening `if (!_minimaRole(...)) return ""`.**
- `PLANNING MINIMA` — layer 2, ADR 0005's existing block, unchanged in shape, rendering for
  `dest_altn`/`era`/`rcf_altn` only (§3 defers the destination variant). Its RVR line
  consumes layer 1's output with a visible back-reference:
  `RVR/VIS: 2000 + 1500 = 3500 m required · └ re-determined, SALS RWY 36 U/S ⟩`

Merging them would put an enroute airport's finding inside a block titled "Planning Minima"
that has no planning rule to apply. The back-reference is what stops two blocks reading as
unrelated.

Placement: per-leg, inside `legs.forEach`, after the MET rows and immediately before layer 2,
ahead of the unified NOTAM block — "here is what is broken and what it costs you" before the
NOTAM list it was derived from. Per-leg because §8's gate is ETA-banded, so the same NOTAM
can be applicable on leg 2 and not leg 1.

**Findings split three ways, all three visible, only one load-bearing:**

- **Applicable** — the finding's runway matches the stored approach's runway. Feeds the
  lattice and the arithmetic; rendered in full with its window, its class downgrade and its
  NOTAM id.
- **Other runways · not your approach** — a real finding on a runway that is not yours,
  one line each, explicitly outside the arithmetic. VTBU's RWY 18 pair lands here.
- **Considered (N)** — collapsed, the same idiom as ADR 0005's `Disregarded (N)`:
  `NO EFFECT` findings plus everything net 2 caught. *"The tool saw `DME U/S` and it changes
  nothing"* is worth one line, and it is how the crew audits net 2's misses.

**No stored approach label → nothing is applicable.** Every finding renders under *Other
runways*, relabelled *"applies if your planned approach is to RWY nn"*, with no arithmetic and
no guessed number — the fix is one tap on the same entry form.

Worked shape, VTBU on TG638 leg 2, stored as `VOR DME RWY 36 / 440 ft / 1500 m / Row 5`:

```
FAILED GROUND EQUIPMENT · §8.1.3.3.6
VOR DME RWY 36 · MDH 440 ft · Type A

  SALS RWY 36 U/S                 VTBDC3302/26 ⟩
  approach lights U/S → minima as for NALS
  daily 0800–1200Z · leg 2 ETA 0745Z

  RVR re-determined
    MDH 440 @ NALS                     2000 m
    charted                            1500 m
    higher of the two              ⇒   2000 m

  Ceiling unaffected — failures other than ILS/GLS
  affect RVR only (§8.1.3.3.6)

  Other runways · not your approach
    PALS CAT 1 RWY 18 U/S         VTBDC3302/26 ⟩
  Considered (1) · tap to review

PLANNING MINIMA — Row 5 · 1 usable Type A approach
  Ceiling:  440 + 400  = 840 ft required — forecast …
  RVR/VIS: 2000 + 1500 = 3500 m required — forecast 9999
           └ re-determined, SALS RWY 36 U/S ⟩          PASS
```

**Interaction mechanics, all forced by shipped code:**

- The NOTAM id is a Source Pane tap target. Anchor keys are already `"<owner>|<notam id>"` and
  `notam_anchors.json` already carries `VTBU|VTBDC3302/26`. **But the delegated listener is
  class-gated** — `e.target.closest('.notam-row[data-anchor-key]')` — so a bare
  `data-anchor-key` on a finding row will not fire. It needs that class, or its own branch.
- Any new tappable control inside the block (the `Considered (N)` toggle, `Enter DH/MDH`)
  carries `data-minima-action`, sits in the **first** branch of the delegated click listener,
  and `stopPropagation()`s — ADR 0004 §4 and ADR 0005 §6's rule — or it fires the enclosing
  `.met-row`'s Source Highlight instead.
- **`_minimaVerdict()` gains a fourth state.** Its three branches (indeterminate → null →
  numeric compare) would let a night edge-lights failure render as a numeric PASS.
  `NOT ALLOWED` ranks above `fail` in `_worseVerdict`'s `rank` map and renders as
  `APPROACH NOT AVAILABLE`.
- **`_minimaBlockHtml`'s `reqVis` line changes** from `entry.base_rvr_vis_m + row.rvr_vis_m`
  to `max(redetermined, charted) + row.rvr_vis_m`, and **must be a no-op when there are no
  applicable findings** — ADR 0005's verification pinned WMKP's arithmetic and it must still
  hold.

### 11. Offline

Two additions to the precache, both plain data, both safe to cache aggressively (they are
facts about aerodromes and about a regulatory table, not about this flight — the opposite of
`hira.json`'s never-cache-a-negative rule):

- the RVR-vs-DH/MDH table (§9), listed in `manifest.json`;
- the new `approach` field rides inside the existing `minima_snapshot.json` (ADR 0005 §7) with
  no schema change to the snapshot mechanism, and **no change to which ICAOs it covers** —
  still alternate/ERA/`rcf_altn` only, which is exactly why §3 defers the destination variant.

A consequence worth stating: layer 1 renders at any airport, but its *arithmetic* needs a
store entry, and offline that entry comes from the snapshot. So a destination or enroute
airport with a finding will show the finding and the class downgrade offline but no
re-determined number, because its minima were never snapshotted. Online it works (the client
fetches the whole store from `/api/minima`). That online/offline asymmetry is accepted rather
than engineered around — extending the snapshot to every airport in `airports.json` would bake
~50 entries into every group dir to serve the handful that ever have one.

Findings themselves live in `airports.json`, already precached. `bundle.html` gets whatever
was baked in at build time, read-only, per ADR 0003's no-fork rule.

## Implementation surface

- **`equipment_minima.py`** (new) — the two-net extractor (§6), the 14-row outcome table
  (§7), the band gate (§8), the RVR-vs-DH/MDH constant and its payload (§9). Zero project
  imports, following `_utils.py`'s zero-circular-dependency shape.
- `app.py` — call the extractor against `notam_db` (**not** `leg_entry["notams"]`, §8) inside
  `_run_notam_step_multi()`; `row` demoted to optional and `approach` accepted in
  `PUT /api/minima/<icao>`; `rvr_table.json` written per group dir alongside
  `minima_snapshot.json` (`_build_manifest` lists it automatically).
- `index.html` — the layer-1 block outside `_minimaRole`'s gate (§10); the three-way split;
  the back-reference line in `_minimaBlockHtml`; `_minimaVerdict`/`_worseVerdict` gain
  `NOT ALLOWED`; `_minimaRole` returns a role rather than a boolean; the entry form gains
  `approach` and makes `row` optional; a fetch for the RVR table with the 404 fallback.
- `docs/KNOWN_ISSUES.md` — the circling/Type-A imprecision (§5); net 2's expected first-release
  noise (§6); the unparsed `"25 0800-1200, 26 0230-1000"` schedule form (§8); the deferred
  destination layer 2 (§3); the offline-snapshot asymmetry (§11).
- `CONTEXT.md` — new terms: Equipment Finding, Applicable Finding, Class Downgrade,
  Re-determined Minima.

## Consequences

- **The Lido gap is still open, and this widens the manual-entry surface** — an `approach`
  label now matters as much as the numbers, and a stale one produces a confidently wrong
  runway match rather than a blank. ADR 0005 §3's redeploy fragility (KNOWN_ISSUES #11)
  applies to the new fields identically.
- **Rejected — re-deriving the Table 3 row from approach availability** (§1). It would need
  the per-runway approach inventory ADR 0005 already rejected as Option A, and the crew's row
  choice is an explicit operator optimization per Table 3's own note.
- **Rejected — computing civil twilight** (§7). Computable, tempting, and an unapproved
  substitution for a Lido table at the exact boundary where the answer flips between
  "no effect" and "not allowed".
- **Rejected — AI extraction** (§6). A miss is silent, and this output is a number a crew acts
  on.
- **Rejected — the JS-constant placement for the RVR table** (§9), despite `_MINIMA_ROWS`'
  precedent, because it discards two published worked examples as regression fixtures.
- **Net 2 will be noisy on first release.** Tuning it down against the nine fixture NOTAM PDFs
  is expected work, and the correct trade against a silent miss.
- **Layer 2 no longer renders on its own** (§10). ADR 0005's standing selectability check is
  now conditional on an equipment finding — a deliberate narrowing for signal, at the cost of
  losing the PASS/FAIL on a marginal alternate whose equipment is fine (KNOWN_ISSUES #16).
- **Deferred — layer 2 at the destination** (§3). The aerodrome the crew actually lands at gets
  the re-determination but not the PASS/FAIL comparison, which is the weakest point of the v1
  scope and the first thing to revisit. `KNOWN_ISSUES` entry, not a silent omission.
- **The tile/finding divergence (§8) is visible to the crew** and will look like a bug the
  first time a T3 — or absent — NOTAM moves a required RVR. The inline window/ETA line is the
  mitigation; if it proves confusing in practice, the fix is a better label, not a narrower
  gate.

## Verification

Same split as ADR 0004 and 0005, for the same reason: there is no JS test harness.

**Unit tests — `tests/test_equipment_minima.py`, 55 tests, no PDFs, all passing.** Full
suite 265 passed (210 before this feature + 55). Covering, beyond the numbered plan above:

- **The two published OM-A worked examples**, plus the band boundaries either side of each
  (440/441, 660/661) and the table floor — the off-by-one §9 exists to catch.
- **Below the 200 ft floor returns `None`**, which must render "cannot determine", never an
  unrestricted pass. Confirmed live: DH 199 with APL U/S renders `CANNOT DETERMINE` in both
  layers, not `PASS`.
- **`VTBDC3302/26` decomposes into two findings** on RWY 18 and RWY 36, driven by the single
  trailing `U/S` that governs both facilities.
- **Type A/B at the 250 ft boundary** (249 → B, 250 → A).
- **The band gate once per window kind** — absolute, `daily_windows` (including a
  midnight-crossing slot), `date_schedules` — plus an overlap-not-containment case.
- **A `TestFixtureDrivenMisclassifications` class**, one test per real error found by
  sweeping all nine fixture NOTAM PDFs (below).

**The fixture sweep was the highest-value verification and found four real defects**, none
of which any planned unit test would have caught, because each was a *pattern* error rather
than a logic error:

1. `TWY EDGE LGT U/S` classified as **runway** edge lights ("night: not allowed") instead of
   taxiway lighting ("no effect") — row order in `_FACILITIES` decides, and it was wrong.
2. `CENTRE LINE LIGHTS TWY A4W … U/S` classified as the table's runway Centre line lights
   row, applying a spurious 750 m Type-A floor to a taxiway outage.
3. Bare `\bRVR\b` matching lines that merely *state* an RVR value ("RVR 350M OR …") while
   carrying a failure verb for something else.
4. `LGT FOR TWY B1 THRU B9 U/S` (a dozen occurrences in TG677) falling through to net 2 and
   reporting "minima cannot be re-determined" when the table plainly says "no effect".

After tuning, net 2 holds 71 findings across 28 distinct texts in nine PDFs, and every one
inspected is genuinely outside the table — stopway lights, guard lights, RETIL, exit-taxiway
indicators, apron/stand lighting, and ambiguous "LGT FOR RWY 16L". That is the accepted
first-release noise §6 anticipated, after one tuning pass.

**A fifth defect was found by rendering, not by testing** — see §1's implementation note:
a broad primary-aid guard silently ate `APCH LGT RWY 36 U/S`. The unit test that should have
caught it was asserting the buggy behaviour; it has been corrected and two regression pins
added.

**Client-side, verified by driving the real functions out of `index.html` in Node** (the
functions were extracted by name and run against real extractor output — not a reimplementation):

- **OM-A Example 1's method reproduced end to end**: stored `VOR DME RWY 36 / MDH 440 /
  1500 m / Row 5` → `MDH 440 @ NALS = 2000` → higher than charted 1500 → `2000` →
  `+ Row 5's 1500` = **3500 m required**, PASS against a 9999 forecast, with the
  back-reference naming `VTBDC3302/26`.
- **The runway match does real work**: re-storing VTBU as `ILS RWY 18 / DH 200 / Row 2`
  swaps which finding is applicable — RWY 18's lights now drive the arithmetic (1200 + 450 =
  1650 m) and RWY 36's move to *Not your approach*.
- **No entry** → no arithmetic, findings listed under *Not matched to an approach*, with the
  entry form one tap away.
- **Both Type A/B columns**: RCLL U/S yields a 750 m floor on Type A and "no effect —
  flight director available" (collapsed into *Considered*) on Type B.
- **A conditional `NOT ALLOWED` no longer lets layer 2 print a bare PASS** — this was a real
  bug found in the same session: night edge-lights rendered `Night: NOT ALLOWED` in layer 1
  beside a naked `PASS` in layer 2. Layer 2 now carries an explicit
  *"Night: approach not available … the verdict above applies to the other branch only."*
- `node --check` on the extracted inline script.

**Not verified** — the same honest gaps ADR 0005 recorded:

- A full Flask upload run (needs `ANTHROPIC_API_KEY` for the NOTAM summarisation step); the
  extractor was exercised against `parse_notam_pdf()` output directly instead.
- Tapping a finding's NOTAM id into the Source Pane, and the `Considered (N)` toggle, in a
  real browser — the markup carries `.notam-row[data-anchor-key]` and `data-minima-action`
  per §10, but no click has been fired.
- ADR 0005's WMKP byte-identical re-render (§10's no-op requirement) — the code path is
  unchanged when `equipment_findings` is empty, but it has not been re-run in a browser.
- The offline path (KNOWN_ISSUES #4) and behaviour across a Railway redeploy
  (KNOWN_ISSUES #1).

## References

- OM-A §8.1.3.3 — Aerodrome Operating Minima (the Lido delegation and the failed-equipment
  carve-out quoted in Context)
- OM-A §8.1.3.3.1 — Determination of DH/MDH; System Minima and Runway Type Minima tables
- OM-A §8.1.3.3.2 — Determination of RVR/VIS; RVR-vs-DH/MDH and Approach Lighting Systems
  (FALS/IALS/BALS/NALS) tables
- OM-A §8.1.3.3.6 — **Effect on Landing Minima of Temporarily Failed or Downgraded Ground
  Equipment for Operation without LVO Approval** — the governing table, its combination rules,
  and Examples 1 and 2
- OM-A §8.1.3.3.10 — Aircraft Approach Categories (CAT C / CAT D by THAI type)
- OM-A §8.1.3.2.3 / §8.1.3.2.4 — destination and alternate/fuel-ERA planning minima
- OM-A §8.1.5.1 — applicable minima are the highest of AIP / Lido / CCI / NOTAM restrictions
- OM-A §8.1.7.5.2 / Table 3 — the planning-minima increments (ADR 0005's governing table)
- OM-A §8.4.7 — the CAT II/III counterpart table (explicitly out of scope, §1)
- ADR 0005 — alternate/ERA planning minima (the store, the seam, the block this extends)
- ADR 0004 — click-isolation shape reused in §10; the bundle divergence precedent
- ADR 0003 — offline manifest/precache pattern (§11); the no-fork rule
- `Input/TG638_NOTAM.pdf` — the VTBU fixture (`VTBDC3302/26`, `VTBDC4363/26`, `VTBDC0333/23`)
