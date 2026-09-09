# ADR 0005 — Alternate/ERA planning minima (OM-A §8.1.7.5.2 Table 3)

Date: 2026-08-26
Status: Accepted — implemented in `met_engine.py`, `app.py`, `index.html` (verified against
a real pipeline run; see Verification)

## Context

OM-A §8.1.3.2.4 requires a destination alternate, fuel ERA, or isolated destination
aerodrome to be selectable only when forecast weather, for the period ETA−1h to ETA+1h,
is at or above a **planning minima** — the aerodrome's published landing minima plus a
margin from §8.1.7.5.2 Table 3 (THAI's standing fuel scheme; §8.1.7.4.2's Table 1 is
confirmed dead code for B777 ops, gated on preconditions the fleet always meets). The ask:
show the crew *how* that comparison is made for each alternate/ERA aerodrome, not just a
verdict.

Three things shape every decision below:

- **The base minima are unreachable by construction, not by omission.** §8.1.3.3 states it
  directly: "THAI utilizes the LIDO mPilot application… to establish the aerodrome
  operating minima in terms of DH/MDH and the minimum required RVR/VIS." This tool has no
  path to Lido. The gap is permanent, not a TODO — every downstream decision routes around
  it rather than trying to close it.
- **`_classify_wx_tier` and its fixtures (RKSI, LSZH, VTBS, VHHH, OPKC, LTCC, EDDF, OPLA)
  are load-bearing and off-limits.** The new feature reads the same condensed TAF outputs
  but must never change what `condense_taf`/`_fold_conditions`/`_classify_wx_tier` return
  for existing inputs.
- **§8.1.6's TAF/TREND applicability table is a different rule from `wx_tier`'s severity
  floor, not a stricter version of it.** `wx_tier` scores every deterioration at full
  severity; §8.1.6 disregards some of them entirely for planning purposes. An alternate
  correctly showing `[YELLOW]` next to a minima `PASS` is the expected output of two
  different rules answering two different questions, not a contradiction to reconcile.

## Decision

### 1. Table 3 only; applies uniformly to `dest_altn`, `era`, `rcf_altn`

§8.1.7.5.2: "This criterion is the standard for THAI operations," reverting to Table 1
only if the fleet lacks a computerised flight-planning system, LVO approval, or an
operational control system with flight monitoring — B777 ops meet all three, so Table 1
is not built. Both table captions read "Destination alternate aerodrome, fuel ERA
aerodrome, isolated destination aerodrome" — one table, no per-role split.

`rcf_dest` is out of scope (it is a *destination* for the RCF route, governed by
§8.1.3.2.3's destination rule, not this table). Takeoff alternate is out of scope
(§8.1.3.2.2 uses actual landing minima directly with no planning-minima increment — a
structurally different rule the user did not ask for).

`app.py`'s `era` field is confirmed to mean **Fuel ERA**, not an EDTO diversion alternate:
`_extract_alternates` only matches `ERA/XXXX` and `FUEL ERA (XXXX)`, and the TG934
fixture's own OFP text reads `"CF 3% ERA/LTFM"` / `"FUEL ERA (LTFM) FUEL TIME DISPATCH
LOAD"` — the exact §8.1.7.5.5 fuel-scheme mechanism, not EDTO's §8.5.6.7/§8.5.6.8. So the
§8.1.6 row for "Destination, Take-Off Alternate, Dest. Alternate, Fuel ERA" governs `era`
aerodromes, not the table's separate EDTO ERA row (materially more permissive on transient
TEMPO deterioration). If a future OFP format ever conflates fuel ERA with an EDTO
diversion alternate in this same field, the wrong row would silently apply — recorded as
KNOWN_ISSUES #12.

### 2. Manual entry, not derivation — one row plus one base-minima pair per ICAO

Table 3's own note (p326 of the OM-A) makes row selection an explicit operator
optimization: *"THAI may select the most convenient planning minima row. For example,
aerodrome with two type B approaches: one CAT3 (0 ft/75 m) another CAT1 (200 ft/550 m).
The operator may use Row 2 and use CAT3 (0 + 150 ft/75 + 450 m) instead of Row 1 CAT1."*
That is a human judgment call already made by whoever reads Lido, not something to
re-derive from a per-runway approach inventory this tool doesn't have (rejected as Option
A — see Consequences).

So the entry unit, per ICAO, is:

```
{ "row": 2, "base_height_ft": 0, "base_rvr_vis_m": 75, "updated": "2026-08-26" }
```

`base_height_ft` is the **DH/MDH — height above the aerodrome**, never DA/MDA (an
altitude). Table 3 writes "DA/H"/"MDA/H" because either form of the published minima may
apply, but the comparison target is a TAF ceiling, which is AGL by definition; entering a
DA in this field would inflate the required ceiling by roughly the field elevation. The
entry form labels the field accordingly and the same footnote is the field's help text,
not separate logic — under manual entry, Table 3's `*`/`**` footnotes ("higher of usable
DA/H or MDA/H", "higher of usable RVR or VIS") are exactly the selection judgment the human
already made in picking this row and these numbers; the tool renders `base + increment =
required` next to the forecast value and lets the numbers speak, rather than adjudicating
DA-vs-DH or RVR-vs-VIS itself.

### 3. Project-level store, ICAO-keyed, `data/aerodrome_minima.json`

An aerodrome's approach minima are facts about the aerodrome, not the flight — enter VTBU
once, every future flight with VTBU as an alternate reuses it. Every write is
read-modify-write against the whole file (load, merge the one ICAO's entry, write) —
identical reasoning to ADR 0004 §2's read-modify-write rule for tier overrides: a 3–4 leg
upload opens two map tabs on one run, and a wholesale overwrite from one tab would erase
the other's concurrent edit.

**Accepted limitation:** `data/` is wiped on every Railway redeploy (KNOWN_ISSUES #8
already documents this for `data/tiles/` and `data/fir_coords_learned.json`). Those two
degrade gracefully because they are re-derivable; hand-entered minima are not. This is
recorded rather than engineered around — a Railway persistent Volume is real
infrastructure outside this session's reach, and a git-committed store would fight the
"edit inline, save instantly" flow decision 2 above requires. Already-completed runs are
unaffected, because a run keeps its own `minima_snapshot.json` (§7) independent of the
live store — only a *new* upload made after a redeploy starts against an empty store.
Recorded as KNOWN_ISSUES #11.

### 4. Computation seam: Python resolves and filters, the client does the arithmetic

`met_engine.py` emits, per airport-leg, the §8.1.6-filtered ceiling/vis numbers the
dispatcher may actually rely on. `index.html` looks up the ICAO in the minima store and
computes `base + increment` against those numbers at render time. Editing a stored minimum
therefore needs no pipeline re-run — the same division of labor ADR 0001/0002 already use
for `taf_base_src` / `met_anchors.json` ("Python resolves, client draws").

New per-leg fields, threaded through exactly the seam CLAUDE.md names for `taf_base_src` —
`_merge_airports_legs()`'s per-leg field whitelist (`app.py:260-269`) — since an unlisted
field "silently vanishes in every Flask run while the CLI output looks fine":

```
"applicable_ceiling_ft":   250,           // null if indeterminate or unrestricted
"applicable_vis_m":        4000,          // null if indeterminate or unrestricted
"ceiling_source":          "BECMG target (10/1000Z-10/1200Z)",
"vis_source":              "baseline",
"ceiling_indeterminate":   false,
"vis_indeterminate":       false,
"disregarded": [
  {"type": "TEMPO", "window": "10/1015Z-10/1030Z", "reason": "transient/shower phenomenon"}
]
```

`disregarded` is Python-emitted, not reconstructed client-side, because the client never
sees raw overlay text — only the resolved numbers. It exists so a dispatcher can see what
was excluded and why, not just a number that quietly ignores a TEMPO.

### 5. The §8.1.6 applicability filter — a new, purely additive function

A new `met_engine._resolve_planning_minima()` (name to be finalized during
implementation), called once per airport-leg right after `wx_tier` is computed, reading
`taf_base`, `becmg_in_progress`, `active_overlays` — the same inputs `_classify_wx_tier`
already takes — and **making zero edits to `condense_taf`, `_fold_conditions`, or
`_classify_wx_tier`.** It reuses `_vis_and_ceiling()` for extraction (already factored out,
already shared) rather than re-parsing ceiling/vis tokens a second way.

The core mechanism is a worst-case `min()` over a candidate pool, per element (ceiling,
visibility, judged independently; CAVOK/NSC and "no candidate" both act as +∞, i.e.
unrestricted):

- **Baseline (`taf_base`) always enters the pool.**
- **`becmg_in_progress`'s folded target text always enters the pool**, unconditionally —
  and this alone reproduces §8.1.6's BECMG rule with no separate deterioration/improvement
  test. If the target is worse (lower) than baseline, `min()` selects it — a deterioration
  applying from the start of change. If the target is better (higher), `min()` keeps the
  baseline — an improvement correctly not credited until `condense_taf` itself folds it
  into `taf_base` once the BECMG completes (which is precisely §8.1.6's "applicable from
  the time of *end* of the change"). No `prior_text`, no explicit comparison, and no edit
  to `condense_taf` — the existing fold already does the work.
- **An upcoming BECMG or FM overlay (`active_overlays`) always enters the pool the same
  way**, for the same reason: §8.1.6's "FM (alone)" column applies in both directions
  regardless, and an upcoming-BECMG improvement can never win a `min()` against a worse
  baseline anyway — so "always include, let `min()` decide" is behaviorally identical to
  explicitly testing direction, for every source in this feature, not only FM.
- **Bare TEMPO / PROB30 / PROB40 — excluded from the pool for transient/shower
  phenomena; included for persistent phenomena.** This is the one place explicit
  exclusion logic is required, because a transient/shower deterioration must never win the
  `min()` even when it is numerically the worst candidate. §8.1.6 names categories by
  example, not exhaustively: transient = "short-lived weather phenomena, e.g.
  thunderstorms, showers" (`TS*`, `SH*`); persistent = "e.g. haze, mist, fog,
  dust/sandstorm, continuous precipitation" (`HZ`, `BR`, `FG`, `DS`, `SS`, `FZRA`, `FZDZ`,
  `FZFG`, …). This is a **different partition from `_WX_PHENOMENA_RE`**, which lumps all
  of those into one YELLOW floor for `wx_tier` — a new regex pair, not a reuse. Two cases
  the table doesn't name are both defaulted to **persistent (included)**, the
  conservative, false-PASS-avoiding direction: a phenomenon token in neither list, and a
  TEMPO/PROB with no phenomenon token at all (e.g. bare `TEMPO 3000`). An *improving*
  TEMPO/PROB needs no separate exclusion — same `min()` argument as BECMG above.
- **Combined `PROB30 TEMPO` / `PROB40 TEMPO` — excluded from the pool entirely, by
  design, not by gap-fill default.** §8.1.6 states this construct's deterioration and
  improvement "may be disregarded" — explicit operator discretion, unlike the
  transient/shower exclusion above (which fills a genuine gap in the table's own
  categories). This is the one deliberately lenient reading in the feature: the OM-A
  permits it, so the tool exercises the permission — but it is never hidden. **Every
  excluded overlay lands in the `disregarded` list (§4) unconditionally** — transient TEMPO
  or combined `PROB30/40 TEMPO` alike — regardless of whether it would actually have won
  the `min()` against the rest of the pool. Recording only the overlays that would have
  been binding requires evaluating the counterfactual "what if it were included," which
  is extra machinery for no benefit: a dispatcher reviewing the list wants to see every
  exclusion the tool made, not only the ones that happened to matter this time.
- **Two states must never render as a pass.** `ceiling_indeterminate` is **not** "no
  BKN/OVC/VV token" — `_CLOUD_RE` already recognizes FEW/SCT/NSC/NCD/SKC/CLR as
  affirmative statements that no ceiling exists, and most GREEN baselines in the fixture
  set (e.g. VTBS's `24008KT 9999 SCT020`) have exactly this shape: no BKN/OVC/VV, but a
  cloud token is present, so the ceiling is unrestricted, not unknown. The correct
  predicate is **no token matching `_CLOUD_RE` at all, and no CAVOK/NSC** — genuine
  silence, not "no BKN/OVC/VV." This is *not* `_tier_for_text`'s ambiguity backstop at line
  505 (`vis_m is None and ceiling_ft is None and not has_cavok`, a whole-string "neither
  element stated" test) — that check is about visibility and ceiling *together*; this one
  needs cloud presence judged on its own. **The flag is a property of the baseline alone
  and nothing resolves it**: even when an applicable overlay states a concrete ceiling,
  the rest of the ETA±1h window is still uncharacterized, so once the baseline is
  genuinely silent, `applicable_ceiling_ft` is `None` and `ceiling_indeterminate` stays
  `true` regardless of the candidate pool — candidates are not evaluated further. `vis`
  has no equivalent affirmative-unrestricted token the way cloud does (a real TAF always
  states a vis figure or CAVOK), so `vis_indeterminate` is simply "no vis token and no
  CAVOK," independent of the ceiling check. Separately, an airport with **no TAF at
  all** — §8.1.3.2.4 already treats a missing forecast as the trigger for requiring *two*
  destination alternates; enforcing that trigger is out of scope, but the absence itself
  must render as an explicit finding, never a blank row that reads as "nothing to report."

`applicable_ceiling_ft`/`applicable_vis_m` is `min()` over the surviving candidate pool
(baseline plus every non-excluded BECMG/FM/TEMPO/PROB source), tracked with a `*_source`
label naming which source produced the winning value, so the panel can show its
provenance rather than a bare number.

### 6. Surface: an inline "Planning Minima" block in the existing per-leg MET section

For any airport where the ICAO is that leg's `dest_altn`/`era`/`rcf_altn` (role looked up
from `_legAirports`, the client's existing `flight_info.json` structure), `buildPanel`
gains a block after that leg's `active_overlays` loop (`index.html`, after line ~1706),
inside the same `legs.forEach`. It renders the selected Table 3 row, `base + increment =
required` for ceiling and RVR/VIS, the applicable forecast value with its source label,
and PASS / FAIL / CANNOT DETERMINE. An ICAO with no store entry renders "No entry — Enter
minima" instead.

`disregarded` fires on roughly a third of the TG921 fixture's airports (15/51, some with
2–3 entries), so it is not a rare edge state — it renders as a collapsed
`Disregarded (N) · tap to review` row, the same idiom the FLIGHT panel already uses for
filtered-out NOTAMs, not inline text that could push the NOTAM block off-screen.

**Role gate uses the full array, not just the primary.** `_legAirports` carries the
complete `dest_altn` list per leg; the map's pink alternate ring deliberately shows only
`dest_altn[0]` (CLAUDE.md, Map section) to avoid marker clutter, but every listed
alternate is a real regulatory option and must get the minima check — this is a
deliberate divergence from the ring's primary-only convention, not an inconsistency to
reconcile.

The entry form (and its trigger) sit inside this block but are **not** part of the MET
Source Highlight gesture. The block still carries `.met-row` + `data-met-anchor` (it is
MET-derived and Selection Sync should still be able to target it), but the entry control
follows ADR 0004 §4's precedent exactly: its own branch, first in the delegated click
handler, `stopPropagation()`, the ICAO carried in a `data-` attribute — never inline
`onclick` — so tapping "Enter minima" opens the form instead of firing the row's Source
Highlight underneath it.

### 7. Offline: a run-scoped snapshot, precached like any other briefing file

Unlike `hira.json` (flight-specific, must never cache a negative), aerodrome minima are
facts about the airport — safe to precache aggressively. At pipeline time, in the same
window `_run_source_pane_step()` occupies (after group dirs exist, before
`_write_manifest()`), the pipeline writes `minima_snapshot.json` into each group dir: the
current store's entries for that group's `dest_altn`/`era`/`rcf_altn` **and** `dest`/
`rcf_dest` ICAOs, written **unconditionally** — `{}` when none have entries — so the
manifest-listed file always exists on disk (the failure mode CLAUDE.md warns about for
`hira.json`, from the opposite direction: a conditionally-written file silently missing
under a manifest entry that claims it's there). It is listed in `manifest.json` and
precached normally.

**Amendment (docs/adr/0006 §3 extension):** originally scoped to alternate/ERA/`rcf_altn`
only, deliberately excluding the destination — layer 2 wasn't computed there yet, so a
destination snapshot entry would have had nothing to feed. Now that the destination gets a
real §8.1.3.2.3 PASS/FAIL (closing KNOWN_ISSUES #13), excluding it would mean that check
goes silently blank offline — exactly the failure this file exists to prevent for
alternates. `dest`/`rcf_dest` are folded into the same `role_icaos` set at the pipeline's
Step 6 call site.

The client prefers a live fetch of the store when online (so an edit made after upload
still helps that session) and falls back to the snapshot offline. `bundle.html` gets
whatever was baked in at build time, read-only — no special-casing, per ADR 0003's
no-fork rule, the same divergence class as tier overrides (ADR 0004 §8).

### 8. The `wx_tier` / minima divergence is intentional, and the panel says so

A transient-phenomenon TEMPO drives `wx_tier` to YELLOW at full severity (its rule) while
being properly disregarded for the minima check (§8.1.6's rule) — VTBS on TG628 leg 2
(`TEMPO VRB15KT 3000 TSRA FEW018CB SCT020 BKN080`, the documented driver of its YELLOW) is
the clearest fixture illustration: `wx_tier` YELLOW, minima PASS, both correct. The panel
block itself notes this (a short label, not a caveat paragraph) so a YELLOW-tagged
alternate showing PASS reads as two different questions answered, not a bug to chase.

## Implementation surface

- `met_engine.py` — new `_resolve_planning_minima()` (or final name chosen during
  implementation), called from the per-airport write loop right after `wx_tier` is
  computed; a new transient/persistent phenomena regex pair (distinct from
  `_WX_PHENOMENA_RE`). **No edit to `condense_taf`, `_fold_conditions`, or
  `_classify_wx_tier`** — the new function reads their existing return values only, so
  `TestPartialBecmgRegression`/`TestCavokFold`/the fixture table (EDDF, OPLA, OPKC, LTCC,
  VTBS, RKSI, LSZH, VHHH) are unaffected by construction, not by discipline.
- `app.py` — `_merge_airports_legs()`'s per-leg field whitelist gains the seven fields in
  §4 (`applicable_ceiling_ft`, `applicable_vis_m`, `ceiling_source`, `vis_source`,
  `ceiling_indeterminate`, `vis_indeterminate`, `disregarded`); new aerodrome-minima store
  helpers (read-modify-write per ICAO); new route(s) to
  read/write `data/aerodrome_minima.json`; `minima_snapshot.json` written per group dir
  per §7, in the same pipeline window as `_run_source_pane_step()`.
- `index.html` — `buildPanel`'s per-leg MET section gains the Planning Minima block; a new
  first-branch entry in the delegated click handler for the entry-form trigger (§6); the
  small inline form itself; a fetch against the new endpoint, with `minima_snapshot.json`
  as the offline fallback.
- `data/aerodrome_minima.json` — new project-level store (accepted redeploy loss, §3).
- `docs/KNOWN_ISSUES.md` — new #11 (redeploy wipes the store) and #12 (`era` extraction
  cannot distinguish Fuel ERA from a hypothetical future EDTO ERA in the same field).
- `CONTEXT.md` — new terms: Planning Minima, Table 3 Row, Base Minima, Applicable
  Forecast.

## Consequences

- The base minima remain permanently manual — this tool never closes the Lido gap, it
  routes around it. Data quality is exactly as good as what the crew/dispatcher enters.
- **Rejected alternative — Option A, deriving the Table 3 row from a per-runway approach
  inventory.** Rejected because row selection is an explicit operator optimization per the
  OM-A's own note (§2 above), not a derivation this tool has the data (or the mandate) to
  make; building it would mean replicating a judgment call on data (a full approach
  inventory per runway) this tool has no source for at all.
- A dispatcher who disagrees with the transient/shower default or the `PROB30/40 TEMPO`
  leniency has no override in this feature — both are visible in `disregarded`, but not
  editable. Acceptable for a first cut; revisit if it proves to matter in practice.
- The minima store's redeploy fragility (§3) is a real, accepted operational cost, not a
  latent bug — re-entering a handful of aerodromes after a Railway redeploy is the
  expected recovery path until a persistent Volume is provisioned (out of this session's
  reach).
- Two tabs on one run converge on the store's next fetch, not live — same limitation ADR
  0004 §Consequences accepts for tier overrides.
- **Known edge, unhit by every current fixture:** `_CLOUD_RE` accepts indefinite vertical
  visibility (`VV///`), but `_CEILING_RE` only parses the numeric form (`VV\d{3}`). An
  aerodrome reporting `VV///` would register `cloud_stated: true` (correctly not
  indeterminate) but contribute no numeric ceiling to the candidate pool — rendering as
  unrestricted rather than the ceiling-zero condition `VV///` actually means. Checked
  against all 8 fixture MET PDFs (TG921, TG415, TG628, TG677, TG910, TG934, TG970 and its
  UPDATE): only the numeric `VV001` form appears (TG910/UDYZ, TG934/OPLA), which
  `_CEILING_RE` handles correctly — `VV///` itself never occurs. Recorded here rather than
  guessed at; handle it (most likely as ceiling 0, alternatively folded into the
  indeterminate flag) if a future MET PDF ever produces it.

## Verification

No JS test harness exists for `index.html` (ADR 0004's own Consequences section states
this), so verification splits the same way ADR 0004's did:

**Unit tests, inline TAFs, no PDFs** (`tests/test_met_engine.py`, following
`TestPartialBecmgRegression`/`TestCavokFold`'s existing pattern) — one per branch:

1. Baseline only, no overlays.
2. BECMG in progress, deteriorating (target worse than baseline — `min()` selects the
   target).
3. BECMG in progress, improving — an inline TAF with a real numeric improvement (e.g.
   baseline `20015KT 3000 BKN008`, `BECMG` to `9999 BKN020`), so the test can actually
   distinguish the improvement branch from a no-op. (LTCC's second BECMG to `VRB02KT
   CAVOK` looked like this case but isn't one: its baseline's `FEW040` isn't a ceiling and
   `9999` is already unrestricted, so nothing numeric changes — keep it as the existing
   `wx_tier` fixture cross-check, not this test.)
4. Bare TEMPO, transient/shower phenomenon, constructed **strictly worse** than the
   baseline (e.g. baseline `9999 SCT020`, TEMPO `1500 TSRA BKN005`) — asserts the
   *baseline's* value survives in `applicable_ceiling_ft`/`applicable_vis_m`, not the
   TEMPO's. Under `min()`, a same-or-better excluded overlay would pass this test whether
   or not the exclusion is coded at all — the assertion is only meaningful when the
   excluded value would otherwise have won.
5. Bare TEMPO, persistent phenomenon (`FG`), also strictly worse than baseline — asserts
   the *TEMPO's* value wins (included, not excluded).
6. `PROB30 TEMPO` combined, constructed strictly worse than the baseline (same shape as
   #4) — asserts the baseline's value survives, for the same reason.
7. An improvement-only overlay — asserts the baseline's (better) value survives. This is
   an emergent property of `min()`, not a coded branch (§5) — the test is a guard against
   a future regression, not proof of a specific `if`.
8. Ceiling *not* indeterminate: baseline `24008KT 9999 SCT020` (the VTBS fixture shape —
   no BKN/OVC/VV, but `SCT020` is present) must resolve to unrestricted ceiling, not
   `ceiling_indeterminate: true` — the false-positive §5 flags explicitly against.
9. Ceiling genuinely indeterminate: a baseline with no cloud token of any kind and no
   CAVOK/NSC — renders "cannot determine," not a pass.
10. No TAF at all — explicit finding, not a blank/pass.

**Manual checklist, run against a real served briefing (TG415/TG416, VTBS↔WMKK) — all
passed:**

- `_merge_airports_legs()`'s whitelist edit verified against a real Flask run (not just
  the CLI): VTBS leg 2 carries `applicable_vis_m: 9999`, `disregarded: [{"type": "TEMPO",
  …, "reason": "transient/shower phenomenon"}]` alongside `wx_tier: "YELLOW"` — the
  documented divergence (§8), pinned in real merged data, not just synthetic tests.
- `/api/minima` GET/PUT exercised with two interleaved PUTs to different ICAOs (curl):
  both entries survive — the read-modify-write path holds. A PUT against a hand-corrupted
  store file (`echo 'not json' > …`) returns 200 and replaces it cleanly, no 500.
  Validation rejects a non-4-letter ICAO and a row outside 1–6 with 400.
  `minima_snapshot.json` confirmed written into the group dir and listed in
  `manifest.json`'s file list after a real pipeline run.
- In the browser: WMKP (a leg-1-only alternate) renders `PLANNING MINIMA — PASS`, `Row 5
  — 1 usable Type A approach`, the `base + increment = required` arithmetic, and
  `Disregarded (1) · tap to review` — tapping it expands the excluded TEMPO's reason
  inline. VTBU (a leg-2-only alternate) renders `No entry for VTBU — Enter minima` on leg
  2 and **no Planning Minima block at all on leg 1** — the per-leg role gate (§6) using
  the full `dest_altn` array is confirmed live, not just read from code.
- Tapping "Edit" opens the form pre-filled from the store; changing a value and tapping
  "Save" PUTs, updates the required figure in place, and does **not** open the Source
  Pane — the click-isolation branch (§6) holds. The DOC header button stayed un-highlighted
  throughout every minima-block interaction (toggle, edit, save) in this run.

**Not yet run:**

- The offline snapshot path end to end (kill-the-server test per CLAUDE.md, then the real
  iPad procedure) — blocked by KNOWN_ISSUES #4, same as every other offline claim in this
  app.
- Behavior across an actual Railway redeploy (the accepted loss in §3) — untestable
  outside the deployed environment, which KNOWN_ISSUES #1 already flags as unreachable.
- Two map tabs on one run both editing the same ICAO concurrently through the actual UI
  (the store's read-modify-write itself was verified directly via curl, per above, but not
  through two live browser tabs racing).

## References

- OM-A §8.1.3.3 — Aerodrome Operating Minima (the Lido mPilot delegation, quoted in
  Context)
- OM-A §8.1.3.2.4 — Planning Minima for Destination Alternates, Fuel ERA, and Isolated
  Destination Airports
- OM-A §8.1.7.4.2 / Table 1 — Basic Fuel Scheme planning minima (not built — see Decision
  1)
- OM-A §8.1.7.5.2 / Table 3 — Basic Fuel Scheme with Variations planning minima (the
  governing table)
- OM-A §8.1.6 — Application of Aerodrome Forecasts (TAF & TREND) to Pre-Flight Planning
  (the applicability matrix, Decision 5)
- ADR 0001 — page images + parse-time anchors
- ADR 0003 — offline briefing (manifest/precache pattern, the no-fork rule)
- ADR 0004 — client-side tier overrides (read-modify-write pattern, the click-isolation
  shape reused in Decision 6, the bundle divergence precedent reused in Decision 7)
- CLAUDE.md — "MET (TAF) Engine" section (`condense_taf`, `_fold_conditions`, the
  `wx_tier` fixture table)
- `docs/KNOWN_ISSUES.md` #8 (redeploy wipes `data/`), #11, #12 (new, this ADR)
