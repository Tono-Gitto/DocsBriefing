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
