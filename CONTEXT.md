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

- **Source document** — the original dispatch PDF as issued (currently the NOTAM document);
  the legal source of truth a pilot cross-checks summaries against.
- **Source Pane** — the collapsible viewer that shows a source document alongside the
  briefing (split view). Optional mode: the briefing is complete without it.
- **Anchor** — the recorded location (page + rectangle) of one NOTAM inside its source
  document, captured when the document is parsed. A NOTAM without an anchor is still
  briefed; it just cannot be located in the source.
- **Source Highlight** — the blue rectangle drawn in the Source Pane around the anchored
  NOTAM block (ID line + validity line + body) when the pilot taps that NOTAM in the
  briefing.
- **Owner** — the entity a NOTAM is attributed to: an airport (ICAO), a FIR, or a
  flight-wide section (GENERAL / FLIGHT LEG / AEROPLANE). The same NOTAM id can appear
  under different owners; owner + id identifies one anchored occurrence.

## NOTAM classification

- **Tier (T1/T2/T3)** — operational severity of a NOTAM at the reference time: T1 = affects
  ability to use the airport/route now; T2 = notable degradation; T3 = everything else.
- **Filtered-out NOTAM** — a flight-wide NOTAM the AI judged irrelevant to this flight;
  kept visible in a collapsed audit list so the pilot can verify the exclusion.

## Weather

- **Weather tier (GREEN/YELLOW/RED)** — severity of forecast conditions at an airport at
  its reference time; drives map marker color.
