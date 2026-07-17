# CONTEXT — Domain Glossary

Ubiquitous language for the Preflight Briefing app. Glossary only — no implementation
detail. See `CLAUDE.md` for architecture and `docs/adr/` for decisions.

## Briefing

- **Briefing** — the pictorial, per-flight view a pilot opens after uploading a dispatch
  package: map, airports, FIRs, and their weather/NOTAM information at the time the
  aircraft will be near each point.
- **Dispatch package** — the set of source documents for one flight: OFP (operational
  flight plan), MET (weather), NOTAM.
- **Reference time (ref_time)** — the moment the aircraft is nearest to a given airport or
  FIR; all weather/NOTAM relevance is judged at this moment.

## Source-document provenance

- **Source document** — an original dispatch PDF as issued (the NOTAM document and the MET
  document); the legal source of truth a pilot cross-checks summaries against.
- **Source Pane** — the collapsible viewer that shows the source documents alongside the
  briefing (split view), one Document Section per document. Optional mode: the briefing is
  complete without it.
- **Document Section** — the region of the Source Pane devoted to one source document (MET
  on top, NOTAM below), individually collapsible to its header bar. On narrow screens the
  sections behave as an accordion: exactly one open at a time.
- **Anchor** — the recorded location (page + rectangle) of one briefed item inside its
  source document, captured when the document is parsed: a NOTAM's block in the NOTAM
  document, or an airport's MET Block in the MET document. An item without an anchor is
  still briefed; it just cannot be located in the source.
- **MET Block** — the contiguous region of the MET document belonging to one airport
  (header line, runway line, METAR, TAF). The unit a MET anchor encloses; every MET element
  in the briefing panel points at the whole block, never a sub-part.
- **Source Highlight** — the blue border rectangle drawn in a Document Section around the
  anchored block when the pilot taps that item in the briefing. At most one per document;
  a MET and a NOTAM highlight may be visible at the same time.
- **ETA-Window Highlight** — a translucent leg-colored fill drawn inside a highlighted MET
  Block over each raw TAF group that is operationally relevant in a leg's ETA±1h window
  (the groups the panel shows as BECMG-in-progress or active overlays). All legs' fills show
  at once, tagged L1/L2 when the airport has more than one leg; out-of-window groups are
  never filled. A fill that cannot be located precisely is omitted, never approximated.
- **Baseline Fill** — a translucent leg-colored fill drawn over the exact source tokens that
  make up "conditions at ETA" (`taf_base`) — a solid border distinguishes it from the thinner
  ETA-Window Highlight. Unlike the ETA-Window Highlight (which fills a whole raw TAF group),
  `taf_base` is a *derived* string with no single source region, so this fill works token by
  token: each token's source offset is resolved to its own word rect and adjacent, same-line
  tokens are merged into one rect. A token that can't be resolved is simply not filled — same
  graceful-miss rule as everywhere else in the Source Pane.
- **Owner** — the entity a NOTAM is attributed to: an airport (ICAO), a FIR, or a
  flight-wide section (GENERAL / FLIGHT LEG / AEROPLANE). The same NOTAM id can appear
  under different owners; owner + id identifies one anchored occurrence.
- **Selection Sync** — the Source Pane following the pilot's map/header selection (airport
  marker, header button, FIR diamond, FLIGHT button, prev/next nav) while the pane is
  already open, so browsing the briefing keeps both documents current with zero extra taps.
  Only active while the pane is open; it never opens the pane itself, and it never changes
  layout (no expand, no collapse, no accordion flip) — a collapsed section's highlight
  updates silently and is already correct when reopened. Distinct from a manual row tap,
  which still opens/expands as before.

## NOTAM classification

- **Tier (T1/T2/T3)** — operational severity of a NOTAM at the reference time: T1 = affects
  ability to use the airport/route now; T2 = notable degradation; T3 = everything else.
- **Filtered-out NOTAM** — a flight-wide NOTAM the AI judged irrelevant to this flight;
  kept visible in a collapsed audit list so the pilot can verify the exclusion.

## Weather

- **Weather tier (GREEN/YELLOW/RED)** — severity of forecast conditions at an airport at
  its reference time; drives map marker color.
