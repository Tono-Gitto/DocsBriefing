"""OM-A §8.1.3.3.6 failed/downgraded ground equipment — docs/adr/0006.

The OM-A publishes two worked examples with answers (§8.1.3.3.6 Examples 1 and
2). They are the reason the RVR-vs-DH/MDH table lives in Python rather than as a
JS constant like MINIMA_TABLE3: they pin the band boundaries, which fail
silently when off by one.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import equipment_minima as em


UTC = timezone.utc


def _band(hh, mm=0, day=26, month=8, year=2026):
    """The ETA±1h planning band around a UTC time."""
    eta = datetime(year, month, day, hh, mm, tzinfo=UTC)
    return eta - timedelta(hours=1), eta + timedelta(hours=1)


def _notam(body, **kw):
    n = {"id": "TEST0001/26", "body": body, "win_start": None, "win_end": None,
         "daily_windows": [], "date_schedules": []}
    n.update(kw)
    return n


def _findings(body, band=None, **kw):
    b0, b1 = band or _band(8)
    return em.extract_findings(_notam(body, **kw), b0, b1)


# ── The published worked examples ────────────────────────────────────────────

class TestOmaWorkedExamples:
    def test_example_1_vor_dme_rwy36_apl_us(self):
        """Cat C, VOR DME RWY 36, APL U/S. MDH 440 falls in band 421-440; the
        approach-lights row substitutes NALS. OM-A answer: 440 ft - 2.0 km."""
        assert em.lookup_rvr(440, "NALS") == 2000

    def test_example_2_loc_dme_rwy18_raised_oca(self):
        """Cat D, MDH 766 with IALS approach lights -> the "661 and above"
        band. OM-A answer: RVR 2400 m. (The raised-OCA path itself is out of
        scope, docs/adr/0006 §1 — this pins the table lookup only.)"""
        assert em.lookup_rvr(766, "IALS") == 2400


class TestRvrTableBands:
    @pytest.mark.parametrize("dh,cls,expected", [
        (440, "NALS", 2000),   # last ft of band 421-440
        (441, "NALS", 2100),   # first ft of band 441-460 — the off-by-one
        (660, "FALS", 2300),   # last ft before the open-ended band
        (661, "FALS", 2400),   # first ft of "661 and above"
        (200, "FALS", 550),    # table floor
        (250, "BALS", 1000),
    ])
    def test_boundaries(self, dh, cls, expected):
        assert em.lookup_rvr(dh, cls) == expected

    def test_below_table_floor_is_none_not_a_pass(self):
        # 199 ft is below the table's 200 ft floor. None must render as
        # "cannot determine" — never as an unrestricted pass.
        assert em.lookup_rvr(199, "FALS") is None

    def test_unknown_class_is_none(self):
        assert em.lookup_rvr(440, "ZZZZ") is None

    def test_payload_shape_matches_table(self):
        p = em.rvr_table_payload()
        assert p["classes"] == list(em.LIGHTING_CLASSES)
        assert len(p["bands"]) == len(em.RVR_VS_DH_MDH)
        assert p["bands"][-1]["high"] is None
        assert p["bands"][0]["rvr"]["FALS"] == 550


class TestApproachType:
    @pytest.mark.parametrize("dh,expected", [(249, "B"), (250, "A"), (200, "B"), (440, "A")])
    def test_250ft_boundary(self, dh, expected):
        """OM-A §1.x: Type A is MDH/DH at or ABOVE 250 ft; Type B is DH BELOW
        250 ft. The boundary itself is Type A."""
        assert em.approach_type_for(dh) == expected

    def test_no_height_no_type(self):
        assert em.approach_type_for(None) is None


# ── Extraction ───────────────────────────────────────────────────────────────

class TestVtbuFixtureShape:
    """TG638's VTBDC3302/26 — two facilities on two runways in one NOTAM, with
    a single trailing U/S governing both."""

    BODY = "PALS CAT 1 RWY 18 AND SALS RWY 36 U/S DUE TO MAINT"

    def test_decomposes_into_two_findings(self):
        fs = _findings(self.BODY)
        assert len(fs) == 2
        assert {f["runway"] for f in fs} == {"18", "36"}
        assert all(f["facility"] == "apch_lights" for f in fs)

    def test_both_columns_downgrade_to_nals(self):
        for f in _findings(self.BODY):
            assert f["outcome_a"] == {"kind": "class", "class": "NALS"}
            assert f["outcome_b"] == {"kind": "class", "class": "NALS"}

    def test_shared_trailing_verb_reaches_the_first_facility(self):
        """The lone U/S sits after SALS. Scoping the failure verb to each
        facility's own segment would silently drop the PALS half."""
        assert any(f["runway"] == "18" for f in _findings(self.BODY))


class TestAvblApchLightLength:
    """RJFF's RJAAF0920/26 (TG664/TG677 fixtures): a partial approach-light
    failure that states a remaining serviceable length must be classified by
    that length (OM-A "Approach lighting systems" table), not defaulted to
    the total-failure NALS row — 427 m falls in IALS's 420-719 m bracket."""

    BODY = "PALS FOR RWY 16L PARTLY U/S DUE TO CONST\nRMK: AVBL APCH LGT LEN 427M"

    def test_classifies_by_available_length(self):
        fs = _findings(self.BODY)
        assert len(fs) == 1
        assert fs[0]["runway"] == "16L"
        assert fs[0]["outcome_a"] == {"kind": "class", "class": "IALS",
                                       "note": "427 m of approach lights available (NOTAM remark)"}
        assert fs[0]["outcome_b"] == fs[0]["outcome_a"]

    @pytest.mark.parametrize("length_m,expected", [
        (209, "NALS"), (210, "BALS"),   # BALS floor
        (419, "BALS"), (420, "IALS"),   # IALS floor
        (719, "IALS"), (720, "FALS"),   # FALS floor
    ])
    def test_length_class_boundaries(self, length_m, expected):
        assert em._apch_class_for_length(length_m) == expected

    def test_no_remark_still_defaults_to_nals(self):
        """No stated remaining length -> OM-A Example 1's own answer: total
        failure, minima as for NALS. Must not regress when a remark exists on
        an unrelated NOTAM elsewhere in the same airport's list."""
        fs = _findings("PALS FOR RWY 16L U/S DUE TO MAINT")
        assert fs[0]["outcome_a"] == {"kind": "class", "class": "NALS"}


class TestScopeExclusions:
    def test_primary_aid_removal_yields_nothing(self):
        """ILS RWY 18 U/S takes the approach away rather than degrading one
        still being flown — not in the table (docs/adr/0006 §1)."""
        assert _findings("ILS RWY 18 U/S DUE TO MAINT") == []

    def test_raised_oca_yields_nothing(self):
        body = ("IAC-ICAO-NDB RWY 36 CHANGED AS FLW: -OCA/H : STRAIGHT-IN APPROACH "
                ": READ 790(766)FT INSTEAD OF 750(726)FT")
        assert _findings(body) == []

    def test_no_failure_verb_yields_nothing(self):
        assert _findings("PALS CAT 1 RWY 18 AVBL") == []

    def test_apch_lgt_is_a_finding_not_a_primary_aid(self):
        """`APCH LGT` is the commonest NOTAM spelling of approach lights. A
        broad "line mentions a navaid" guard matched its bare APCH and silently
        dropped it — a false negative, the direction docs/adr/0006 §6 designs
        against. Regression pin."""
        fs = _findings("APCH LGT RWY 36 U/S")
        assert [f["facility"] for f in fs] == ["apch_lights"]
        assert fs[0]["runway"] == "36"

    def test_line_naming_both_a_primary_aid_and_a_light_still_yields_the_light(self):
        """docs/adr/0006 §1/D11: the test is membership in the table's 14 rows,
        not whether a navaid is mentioned nearby."""
        fs = _findings("ILS GP AND APCH LGT RWY 18 U/S")
        assert [f["facility"] for f in fs] == ["apch_lights"]

    @pytest.mark.parametrize("body", [
        "VOR/DME 'SVB' U/S", "ILS DME RWY 18 U/S", "NDB/DME U/S DUE MAINT",
    ])
    def test_compound_dme_is_not_the_standalone_dme_facility(self, body):
        assert _findings(body) == []

    def test_standalone_dme_is_a_finding(self):
        fs = _findings("DME FOR RWY 35L U/S.")
        assert [f["facility"] for f in fs] == ["dme"]


class TestFacilityMapping:
    def test_taxiway_lighting_wins_over_runway_edge_lights(self):
        """TWY EDGE LGT is taxiway lighting ("no effect"), NOT runway edge
        lights ("night: not allowed"). Row order in _FACILITIES is what keeps
        them apart — TG638's VTBDC4088/26 is the fixture."""
        fs = _findings("TWY EDGE LGT U/S DUE TO MAINT")
        assert [f["facility"] for f in fs] == ["taxiway_lights"]
        assert fs[0]["outcome_a"]["kind"] == "no_effect"

    def test_runway_edge_lights_are_conditional_on_day_night(self):
        fs = _findings("RWY EDGE LGT RWY 18 U/S")
        assert fs[0]["facility"] == "edge_thr_end_lights"
        o = fs[0]["outcome_a"]
        assert o["kind"] == "conditional"
        assert [b["label"] for b in o["branches"]] == ["Day", "Night"]
        assert o["branches"][1]["outcome"]["kind"] == "not_allowed"

    def test_centre_line_lights_differ_by_column(self):
        """Type B resolves to no effect (flight director available on type);
        Type A carries a hard 750 m floor."""
        f = _findings("RCLL RWY 18 U/S")[0]
        assert f["outcome_b"]["kind"] == "no_effect"
        assert f["outcome_a"] == {"kind": "floor", "rvr_m": 750, "note": None}

    def test_paired_runway_designator_yields_both_ends(self):
        fs = _findings("ALS ON RWY 17R/35L U/S.")
        assert {f["runway"] for f in fs} == {"17R", "35L"}

    def test_runway_before_facility_still_resolves(self):
        fs = _findings("RWY 18 PALS U/S")
        assert fs and fs[0]["runway"] == "18"


class TestNetTwo:
    def test_unlisted_lighting_is_a_finding_not_silence(self):
        """§8.1.3.3.6: "Only those facilities mentioned in Table below should be
        acceptable". An unmappable lighting failure must say so."""
        fs = _findings("FOO BARRETTE LGT RWY 18 U/S")
        assert fs and fs[0]["facility"] == "unmapped"
        assert fs[0]["mapped"] is False

    def test_stop_bar_lights_are_unmapped(self):
        fs = _findings("STOP BAR LGT FOR RWY 14/32 U/S.")
        assert all(not f["mapped"] for f in fs)

    def test_non_lighting_equipment_is_not_dragged_in(self):
        """VDGS and weather radar are equipment and are U/S, but neither is a
        §8.1.3.3.6 facility — reporting them would be a false alarm."""
        assert _findings("VISUAL DOCKING GUIDANCE SYSTEM (VDGS) ACFT STAND NR 3 U/S") == []
        assert _findings("DOPPLER WX RADAR SYSTEM (METEOR 60DX10-S) U/S FOR REPAIR") == []


# ── The ETA±1h band gate (docs/adr/0006 §8) ──────────────────────────────────

class TestBandGate:
    BODY = "PALS RWY 18 U/S"

    def test_absolute_window_overlapping_band_applies(self):
        n = {"win_start": datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
             "win_end":   datetime(2026, 8, 26, 12, 0, tzinfo=UTC)}
        assert _findings(self.BODY, band=_band(7, 45), **n)

    def test_absolute_window_clear_of_band_does_not(self):
        n = {"win_start": datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
             "win_end":   datetime(2026, 8, 26, 12, 0, tzinfo=UTC)}
        assert _findings(self.BODY, band=_band(2, 30), **n) == []

    def test_daily_window_overlapping_band_applies(self):
        # 0800-1200 daily vs an 0745Z ETA: the band reaches 0845Z.
        assert _findings(self.BODY, band=_band(7, 45), daily_windows=[(480, 720)])

    def test_daily_window_clear_of_band_does_not(self):
        assert _findings(self.BODY, band=_band(2, 30), daily_windows=[(480, 720)]) == []

    def test_date_schedule_overlapping_band_applies(self):
        assert _findings(self.BODY, band=_band(7, 45), date_schedules=[(8, 26, 480, 720)])

    def test_date_schedule_on_another_date_does_not(self):
        assert _findings(self.BODY, band=_band(7, 45), date_schedules=[(8, 27, 480, 720)]) == []

    def test_midnight_crossing_daily_window(self):
        # 2200-0400 is active at 0230Z.
        assert _findings(self.BODY, band=_band(2, 30), daily_windows=[(1320, 240)])

    def test_overlap_not_containment(self):
        """A failure ending 20 min after the ETA still degrades the approach a
        crew would fly arriving early — the band, not the instant."""
        n = {"win_start": datetime(2026, 8, 26, 6, 0, tzinfo=UTC),
             "win_end":   datetime(2026, 8, 26, 7, 20, tzinfo=UTC)}
        # ETA 0800Z: the NOTAM ends before it, but the band opens at 0700Z.
        assert _findings(self.BODY, band=_band(8), **n)


class TestRobustness:
    def test_empty_body_yields_nothing(self):
        assert em.extract_findings({"body": ""}, *_band(8)) == []

    def test_malformed_notam_never_raises(self):
        assert em.extract_findings({}, *_band(8)) == []
        assert em.extract_findings({"body": None}, *_band(8)) == []

    def test_findings_dedupe_on_id_facility_runway(self):
        n = _notam("PALS RWY 18 U/S\nPALS RWY 18 U/S")
        fs = em.findings_for_airport([n], *_band(8))
        assert len(fs) == 1

    def test_findings_carry_notam_id(self):
        n = _notam("PALS RWY 18 U/S", id="VTBDC3302/26")
        fs = em.findings_for_airport([n], *_band(8))
        assert fs[0]["notam_id"] == "VTBDC3302/26"

    def test_mapped_findings_sort_before_unmapped(self):
        n = _notam("PALS RWY 18 U/S\nSTOP BAR LGT RWY 18 U/S")
        fs = em.findings_for_airport([n], *_band(8))
        assert fs[0]["mapped"] is True
        assert fs[-1]["mapped"] is False


class TestFixtureDrivenMisclassifications:
    """Each of these was a real misclassification found by sweeping the nine
    fixture NOTAM PDFs — the tuning pass docs/adr/0006 §6 anticipated."""

    def test_taxiway_centre_line_lights_are_not_runway_centre_line_lights(self):
        """`CENTRE LINE LIGHTS TWY A4W ... U/S` is taxiway lighting ("no
        effect"). Read as the table's Centre line lights row it would apply a
        750 m floor in the Type A column and inflate a required RVR."""
        fs = _findings("CENTRE LINE LIGHTS TWY A4W BTN ACFT STAND B15 AND B27 U/S")
        assert [f["facility"] for f in fs] == ["taxiway_lights"]

    def test_runway_centre_line_lights_still_map(self):
        assert _findings("CENTRE LINE LGT U/S.")[0]["facility"] == "centre_line_lights"

    def test_lgt_for_twy_word_order(self):
        fs = _findings("LGT FOR TWY B1 THRU B9 U/S")
        assert [f["facility"] for f in fs] == ["taxiway_lights"]

    def test_rvr_value_is_not_an_rvr_assessment_system(self):
        """A line merely stating an RVR value, carrying a failure verb for
        something else, is not an RVR assessment system outage."""
        assert not any(f["facility"] == "rvr_assessment"
                       for f in _findings("APCH BAN RVR 350M OR LOWER, TWY C LGT U/S"))

    def test_rvr_assessment_system_still_maps(self):
        for body in ["RVR RWY 36 U/S", "RVR SYSTEM METPARK-27 TDZ U/S", "RVR AT METPARK-09 TDZ U/S."]:
            assert any(f["facility"] == "rvr_assessment" for f in _findings(body)), body

    def test_middle_marker_maps(self):
        fs = _findings("MM RWY 22 ID DOT/DASH 75 MHZ U/S.")
        assert fs[0]["facility"] == "middle_marker"
        assert fs[0]["runway"] == "22"
