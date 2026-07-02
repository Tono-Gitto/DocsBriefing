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


@pytest.mark.integration
class TestTG921NOTAMValidation:
    """Validate NOTAM parse against TG921_NOTAM.pdf (no API key needed)."""

    @pytest.fixture(scope="class")
    def notam_db(self):
        import sys
        sys.path.insert(0, ROOT)
        from notam_engine import parse_notam_pdf
        db, _, _ = parse_notam_pdf(FIXTURE_NOTAM)
        return db

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
