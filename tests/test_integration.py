"""
Integration tests against TG921 fixture PDFs (Input/TG921_*.pdf).

Run with:  python3 -m pytest tests/ -m integration -q
Skipped automatically when fixture PDFs are absent (e.g. CI without inputs).

No API key needed: we test parse_notam_pdf() and met_engine.main() directly —
not the AI summarisation step.
"""

import json
import os
import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))

FIXTURE_OFP   = os.path.join(ROOT, "Input", "TG921_OFP.pdf")
FIXTURE_MET   = os.path.join(ROOT, "Input", "TG921_MET.pdf")
FIXTURE_NOTAM = os.path.join(ROOT, "Input", "TG921_NOTAM.pdf")

_FIXTURES_PRESENT = all(os.path.exists(p) for p in (FIXTURE_OFP, FIXTURE_MET, FIXTURE_NOTAM))

pytestmark = pytest.mark.skipif(
    not _FIXTURES_PRESENT,
    reason="TG921 fixture PDFs not present in Input/",
)


@pytest.mark.integration
class TestTG921METValidation:
    """Validate the CLAUDE.md spot-check cases against the TG921 fixture."""

    @pytest.fixture(scope="class")
    def airports(self):
        from datetime import datetime, timezone
        import sys
        sys.path.insert(0, ROOT)
        import parse_ofp
        import met_engine

        parse_ofp.OFP_PDF  = FIXTURE_OFP
        parse_ofp.OUT_JSON = os.path.join(ROOT, "data", "route.json")
        # FLIGHT_TIME_MIN stays at the hardcoded TG921 value (621 min)
        parse_ofp.main()

        met_engine.MET_PDF    = FIXTURE_MET
        met_engine.ROUTE_JSON = os.path.join(ROOT, "data", "route.json")
        met_engine.OUT_JSON   = os.path.join(ROOT, "data", "airports.json")
        met_engine.TAKEOFF_UTC = datetime(2026, 6, 20, 13, 5, tzinfo=timezone.utc)
        met_engine.main()

        with open(met_engine.OUT_JSON) as f:
            return {a["icao"]: a for a in json.load(f)}

    def test_eddf_ref_and_base(self, airports):
        ap = airports["EDDF"]
        assert ap["ref_time"] == "1305Z"
        assert "23007KT" in (ap["taf_base"] or "")

    def test_opla_fm_folded(self, airports):
        ap = airports["OPLA"]
        assert "26005KT 4000 FU SCT100" in (ap["taf_base"] or "")

    def test_opkc_becmg_in_progress(self, airports):
        ap = airports["OPKC"]
        assert ap["becmg_in_progress"] is not None

    def test_vtbs_tempos_excluded(self, airports):
        ap = airports["VTBS"]
        assert ap["ref_time"] == "2326Z"
        assert "24008KT 9999 SCT020" in (ap["taf_base"] or "")
        assert ap["active_overlays"] == []


@pytest.fixture(scope="module")
def notam_db():
    """Shared across TestTG921NOTAMValidation and TestNotamAnchors — one PDF pass."""
    import sys
    sys.path.insert(0, ROOT)
    from notam_engine import parse_notam_pdf
    db, _, _ = parse_notam_pdf(FIXTURE_NOTAM)
    return db


@pytest.mark.integration
class TestTG921NOTAMValidation:
    """Validate NOTAM parse against TG921_NOTAM.pdf (no API key needed)."""

    def test_vtbs_notams_present(self, notam_db):
        assert "VTBS" in notam_db

    def test_all_notam_ids_have_slash(self, notam_db):
        for icao, notams in notam_db.items():
            for n in notams:
                assert "/" in n["id"], f"{icao}: malformed NOTAM id: {n['id']!r}"

    def test_tiers_are_valid(self, notam_db):
        for icao, notams in notam_db.items():
            for n in notams:
                assert n["tier"] in (1, 2, 3), f"{icao}: unexpected tier {n['tier']}"


@pytest.mark.integration
class TestMetAnchors:
    """Validate met_anchors.py (Source Pane click-to-highlight + ETA-window fills)
    against TG921_MET.pdf.

    Position-aware companion pass to met_engine.parse_met_pdf() — see met_anchors.py
    and docs/adr/0002-two-document-source-pane.md.
    """

    @pytest.fixture(scope="class")
    def parsed(self):
        import sys
        sys.path.insert(0, ROOT)
        from met_anchors import extract_anchors
        anchors, page_sizes = extract_anchors(FIXTURE_MET)
        return anchors, page_sizes

    @pytest.fixture(scope="class")
    def met_airports(self):
        import sys
        sys.path.insert(0, ROOT)
        from met_engine import parse_met_pdf
        airports, order = parse_met_pdf(FIXTURE_MET)
        return airports, order

    def test_full_coverage(self, parsed, met_airports):
        anchors, _ = parsed
        _, order = met_airports
        assert set(anchors.keys()) == set(order)

    def test_known_single_page_block(self, parsed):
        anchors, _ = parsed
        assert [r["page"] for r in anchors["EDDF"]["block"]] == [1]
        assert [r["page"] for r in anchors["VTBS"]["block"]] == [1]

    def test_page_crossing_block(self, parsed):
        anchors, _ = parsed
        assert [r["page"] for r in anchors["VECC"]["block"]] == [1, 2]

    def test_group_rect_fidelity_for_all_taf_airports(self, parsed, met_airports):
        import re
        import sys
        sys.path.insert(0, ROOT)
        from met_engine import _GROUP_RE
        anchors, _ = parsed
        airports, order = met_airports
        for icao in order:
            taf = airports[icao]["taf_raw"]
            if taf is None:
                continue
            expected = {str(m.start()) for m in _GROUP_RE.finditer(taf)}
            got = anchors[icao].get("groups", {})
            assert set(got.keys()) == expected, f"{icao}: fidelity gate dropped groups"

    def test_known_group_offsets(self, parsed):
        anchors, _ = parsed
        assert set(anchors["VTBS"]["groups"].keys()) == {"41", "98"}

    def test_all_rects_normalized(self, parsed):
        anchors, _ = parsed
        for icao, entry in anchors.items():
            for r in entry["block"]:
                assert 0 <= r["x0"] < r["x1"] <= 1, f"{icao} block: bad x range {r}"
                assert 0 <= r["y0"] < r["y1"] <= 1, f"{icao} block: bad y range {r}"
            for src_start, rects in entry.get("groups", {}).items():
                for r in rects:
                    assert 0 <= r["x0"] < r["x1"] <= 1, f"{icao} group {src_start}: bad x range {r}"
                    assert 0 <= r["y0"] < r["y1"] <= 1, f"{icao} group {src_start}: bad y range {r}"

    def test_page_sizes_match_page_count(self, parsed):
        _, page_sizes = parsed
        assert len(page_sizes) == 7

    def test_words_present_for_all_taf_airports(self, parsed, met_airports):
        # Same fidelity-gate parity as "groups": every airport with a TAF that
        # reconstructs cleanly gets word-level geometry too (see met_anchors.py
        # "words", HANDOFF.md — Baseline Fills).
        anchors, _ = parsed
        airports, order = met_airports
        for icao in order:
            if airports[icao]["taf_raw"] is None:
                continue
            if "groups" in anchors[icao]:
                assert "words" in anchors[icao], f"{icao}: groups present but words missing"

    def test_vtbu_baseline_offsets_resolve_to_matching_words(self, parsed):
        # HANDOFF.md worked example: VTBU's folded baseline is
        # "36003KT 9999 FEW020" at taf_raw offsets 57, 29, 34.
        from datetime import datetime, timezone
        import sys
        sys.path.insert(0, ROOT)
        from met_engine import parse_met_pdf, condense_taf

        anchors, _ = parsed
        airports, _ = parse_met_pdf(FIXTURE_MET)
        taf = airports["VTBU"]["taf_raw"]
        ref_dt = datetime(2026, 6, 20, 23, 26, tzinfo=timezone.utc)
        base, _, _, toks = condense_taf(taf, ref_dt)
        assert base == "36003KT 9999 FEW020"

        words = anchors["VTBU"]["words"]
        by_start = {w[0]: w for w in words}
        for tok in toks:
            end = tok["s"] + len(tok["t"])
            assert tok["s"] in by_start, f"no word at offset {tok['s']} for {tok['t']!r}"
            w = by_start[tok["s"]]
            assert w[1] == end, f"word span mismatch for {tok['t']!r}: {w}"
            assert taf[tok["s"]:end] == tok["t"]


@pytest.mark.integration
class TestNotamAnchors:
    """Validate notam_anchors.py (Source Pane click-to-highlight) against TG921_NOTAM.pdf.

    Position-aware companion pass to parse_notam_pdf() — see notam_anchors.py
    and docs/adr/0001-page-images-and-parse-time-anchors.md.
    """

    @pytest.fixture(scope="class")
    def parsed(self):
        import sys
        sys.path.insert(0, ROOT)
        from notam_anchors import extract_anchors
        anchors, page_sizes = extract_anchors(FIXTURE_NOTAM)
        return anchors, page_sizes

    def test_known_airport_notam_page(self, parsed):
        anchors, _ = parsed
        rects = anchors["EDDF|EDDZA3149/26"]
        assert [r["page"] for r in rects] == [4]

    def test_known_fir_and_general_ids_present(self, parsed):
        anchors, _ = parsed
        assert "VTBS|VTBDC3295/26" in anchors
        assert anchors["VTBS|VTBDC3295/26"][0]["page"] == 7

    def test_page_break_notam_yields_multiple_rects(self, parsed):
        # THA 00002/13 (GENERAL section) is long enough to span three pages
        anchors, _ = parsed
        rects = anchors["GENERAL|THA 00002/13"]
        assert [r["page"] for r in rects] == [1, 2, 3]

    def test_all_rects_normalized(self, parsed):
        anchors, _ = parsed
        for key, rects in anchors.items():
            for r in rects:
                assert 0 <= r["x0"] < r["x1"] <= 1, f"{key}: bad x range {r}"
                assert 0 <= r["y0"] < r["y1"] <= 1, f"{key}: bad y range {r}"

    def test_page_sizes_match_page_count(self, parsed):
        anchors, page_sizes = parsed
        max_page = max(r["page"] for rects in anchors.values() for r in rects)
        assert len(page_sizes) >= max_page

    def test_missing_id_absent_not_raised(self, parsed):
        anchors, _ = parsed
        assert "EDDF|NOSUCHNOTAM/99" not in anchors

    def test_hit_rate_against_parsed_notams(self, notam_db, parsed):
        # Cross-check against the real parser's airport NOTAMs (already parsed by
        # TestTG921NOTAMValidation's notam_db fixture — no second PDF pass here).
        anchors, _ = parsed
        expected = {f"{icao}|{n['id']}" for icao, notams in notam_db.items() for n in notams}
        missing = expected - anchors.keys()
        hit_rate = (len(expected) - len(missing)) / len(expected)
        assert hit_rate >= 0.95, f"anchor hit rate {hit_rate:.1%} below 95% target; missing: {sorted(missing)[:10]}"
