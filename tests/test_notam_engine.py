"""Unit tests for notam_engine helpers — no PDFs, no API key."""
from datetime import datetime, timezone

import pytest

from notam_engine import (
    _classify_tier,
    _effective_tier,
    _is_active,
    _is_active_daily,
    _parse_daily_windows,
    _partition_at_dash_boundaries,
    _parse_until,
    _split_com_info_parts,
)


def _dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestClassifyTier:
    @pytest.mark.parametrize("body,expected", [
        (["RWY 28L CLSD DUE WIP"], 1),                    # space-separated designator
        (["RWY02R/20L CLSD"], 1),                          # concatenated designator
        (["ILS RWY 25L U/S"], 1),
        (["RESTRICTED AREA VTR41 ACTIVE"], 1),             # airport context → T1
        (["TWY B CLSD"], 2),
        (["ACFT STAND 105 CLSD"], 2),
        (["TRIGGER NOTAM - AIRAC AMDT 07/26"], 3),
    ])
    def test_airport_tiers(self, body, expected):
        assert _classify_tier(body) == expected

    @pytest.mark.parametrize("body,expected", [
        (["ROUTE M751 NOT AVBL"], 1),
        (["VOR PNH 116.3 U/S"], 2),
        (["RESTRICTED AREA VTR41 ACTIVE"], 3),             # FIR context → T3
        (["DANGER AREA VTD25 ACTIVE"], 3),
    ])
    def test_fir_tiers(self, body, expected):
        assert _classify_tier(body, is_fir=True) == expected


class TestBoundedNavaidLimitation:
    """A navaid U/S over a stated radial range or distance band is a limitation on
    an aid still in service — not an outage — and must not badge T1/T2 alongside a
    genuine withdrawal. The bound has to be stated geometry; see the "PART" cases in
    test_unbounded_outage_keeps_tier for the construction that looks similar and
    isn't. Bodies are the fixture NOTAMs verbatim."""

    @pytest.mark.parametrize("body", [
        # ZBAA ZBBBE2197/26 (TG664) — the reference case: a distance band.
        ["DME 36L 'IDK' CH54X LIMITATION:",
         "DME U/S BTN 11NM-12.5NM,BTN 19NM-22NM FOR ILS/DME APPROACH",
         "PROCEDURE."],
        # ZBAA ZBBBE2199/26 (TG664) — same, on an RNAV CAT-I/II procedure.
        ["DME 01 'INJ' CH22X LIMITED TO USE:",
         "DME U/S BTN 9NM-10NM ON RNAV CAT-I/II ILS/DME Z RWY01"],
        # ZSAM ZBBBM1536/26 (TG628) — radial range + a beyond-NM limit. Fires the
        # \bVOR\b clause as well as \bDME\b, which is why the strip is body-level.
        ["XINGLIN VOR/DME 'XLN' 114.7MHZ/CH94X LIMITED TO USE:",
         "1. U/S BTN RADIAL 090DEG-188DEG CLOCKWISE.",
         "2. U/S BEYOND 43NM ON RADIAL 359DEG FOR ARRIVAL/DEPARTURE",
         "PROCEDURE."],
        # ZSPD ZBBBF2442/26 (TG664) — bare radial range, no "LIMITED TO USE" header.
        ["LIUZAO VOR/DME 'PDL' 109.4MHZ/CH31X U/S BTN RADIAL",
         "209DEG-213DEG CLOCKWISE."],
        # ZBAA ZBBBE2190/26 (TG664) — a localizer bounded by distance.
        ["LOC 19 ILS U/S BEYOND 21.5NM OF FRONT COURSE."],
        # ZBAA ZBBBE2191/26 (TG664) — bounded by ANGLE, not distance. The bound
        # reads "BEYOND 010DEG"; an NM-only pattern leaves this one at T1.
        ["LOC 36R ILS LIMITATION:",
         "1.U/S BEYOND 010DEG LEFTSIDE OF FRONT COURSE.",
         "2.U/S BTN 17-28NM BEYOND LEFTSIDE 3.8DEG AND RIGHTSIDE 3.8DEG OF",
         "FRONT COURSE."],
        # VLVT VLVTA0091/26 (TG628) — DVOR/DME, two radial ranges.
        ["XIENGKHOUANG VOR/DME 'THX' 114.00MHZ/CH87X LIMITED TO USE:",
         "1. DVOR/DME U/S ON RADIAL 042DEG-222DEG CLOCKWISE.",
         "2. DOVR/DME U/S ON RADIAL 143DEG-323DEG CLOCKWISE."],
    ])
    def test_bounded_limitation_is_t3(self, body):
        assert _classify_tier(body) == 3
        assert _classify_tier(body, is_fir=True) == 3

    @pytest.mark.parametrize("body,expected", [
        # WMKK WMKKA2470/26 (TG415) — a real withdrawal. Must stay T1.
        (["TANJUNG SEPAT DME (DTS) CH 84X WITHDRAWN", "FOR MAINT"], 1),
        # VTBS THA 00064/25 [3] — DVOR/DME suspended outright, dated. Must stay T1.
        (["DVOR/DME (SVB) (133932.5N 1004353.2E) (111.4 MHZ, CH51X) temporary",
          "suspended from 28 November 2024 at 0001 UTC to 02 October 2026"], 1),
        (["VOR PNH 116.3 U/S"], 1),
        # "PART" names a COMPONENT of a combined aid, not a fraction of its
        # coverage — one half failing completely is a real outage. WIDD
        # WRRRA2129/26 (TG415), EDDB EDDZA6387/25 and LOWW LOWWA2108/26 (TG950).
        (["ILS/DME CH38X, DME PART U/S DUE TO TECH REASON"], 1),
        (["LOEWENBERG DVOR/DME LWB 114.55MHZ / CH92Y, DVOR-PART U/S."], 1),
        (["FUERSTENWALDE VOR/DME FWE 113.3MHZ / CH80X, VOR-PART U/S."], 1),
        (["DVOR/DME FMD 110.40MHZ/CH41X, VOR PART U/S.",
          "IF UNABLE TO PERFORM PUBLISHED MISSED APPROACH PROCEDURES BY",
          "SUPERSEDING FMD DVOR/DME BY RNAV, ADVISE ATC ON INITIAL CONTACT"], 1),
        (["ILS RWY 25L U/S DUE TO MAINT"], 1),
    ])
    def test_unbounded_outage_keeps_tier(self, body, expected):
        assert _classify_tier(body) == expected

    def test_real_closure_beside_a_limitation_survives(self):
        """The strip is per statement, so an unrelated genuine closure in the same
        body still scores. This is what makes a body-level strip safe."""
        body = ["RWY 28L CLSD DUE WIP.",
                "DME U/S BTN 9NM-10NM ON ILS/DME APPROACH PROCEDURE."]
        assert _classify_tier(body) == 1

    @pytest.mark.parametrize("body", [
        # RJFF RJAAF0920/26 (TG664) — the docs/adr/0006 approach-light fixture.
        # "PARTLY U/S" on a PALS is NOT a non-event: §8.1.3.3.6 re-determines
        # landing minima from it and equipment_minima.py reads the 427 m to pick
        # IALS. Dropping this to T3 would hide a NOTAM with a real minima cost.
        ["PALS FOR RWY 16L PARTLY U/S DUE TO CONST",
         "RMK: AVBL APCH LGT LEN 427M"],
        # RJTT RJAAJ1656/26 (TG677) — six failures in one body, several unbounded.
        # Also the "NR.4" case: splitting statements on a bare '.' tore the
        # APCH-GUIDANCE-LGT-to-U/S adjacency apart and silently dropped it to T3.
        ["RWY-THR-ID-LGT FOR RWY 16L U/S",
         "SEQUENCED-FLG-LGT FOR RWY 34L U/S",
         "REDL FOR RWY 16L/34R PARTLY U/S",
         "APCH-GUIDANCE-LGT FOR RWY 16R/16L(NR.4) U/S",
         "APCH-GUIDANCE-LGT FOR RWY 16R/16L(NR.6,NR.8) PARTLY U/S",
         "LIGHTING SYSTEM CAT-2,3 FOR RWY 34R DOWNGRADED TO CAT-1"],
    ])
    def test_partial_lighting_failure_is_out_of_scope(self, body):
        assert _classify_tier(body) == 1

    @pytest.mark.parametrize("body", [
        # ZGHA ZBBBG2520/26 (TG664) — the limitation is the CAUSE and the
        # consequence is a published STAR withdrawn. Matching the bare
        # "LIMITED TO USE" header swallowed the consequence with it.
        ["\"DUE TO LAOLIANGCANG VOR/DME 'LLC' 116.2MHZ/CH109X LIMITED TO",
         "USE,",
         "FLW STAR PROCEDURE U/S:",
         "STAR RWY36L/R(ZGHA-9B):RUK-01A.\""],
        # ZGHA ZBBBG2527/26 (TG664) — same shape, two SIDs off an NDB.
        ["\"DUE TO GUTANG NDB 'W' 388KHZ LIMITED TO USE,FLW SID PROCEDURE",
         "U/S:",
         "1.SID RWY36L(ZGHA-7C): OLT-O3D(BY ATC).",
         "2.SID RWY36R(ZGHA-7D): OLT-04D(BY ATC).\""],
    ])
    def test_procedure_withdrawn_by_a_limitation_keeps_tier(self, body):
        assert _classify_tier(body) == 1

    def test_fir_navaid_limitation_drops_from_t2(self):
        """Enroute navaid outages are T2 (never T1); a *bounded* one is still T3."""
        assert _classify_tier(["VOR ABC 114.7 U/S BTN RADIAL 090DEG-188DEG"],
                              is_fir=True) == 3
        assert _classify_tier(["VOR ABC 114.7 U/S"], is_fir=True) == 2


class TestSplitComInfoParts:
    def test_splits_on_double_dash_with_inline_first_part(self):
        body = [
            "COM INFO:--All A350 and BOEING 787:",
            "are now eligible for CPDLC.",
            "(ISSUED 23MAR26/BKKPC2/UFN)",
            "--IN CASE CANNOT BE CONTACTED BKKOC VIA TELEPHONE,",
            "PILOTS MAY CONTACT VIA MS TEAMS.",
            "(ISSUED 30JUL24/BKKOC/UFN)",
        ]
        parts = _split_com_info_parts(body)
        assert len(parts) == 2
        assert parts[0][0] == "All A350 and BOEING 787:"
        assert parts[1][0] == "IN CASE CANNOT BE CONTACTED BKKOC VIA TELEPHONE,"

    def test_triple_dash_rule_line_is_not_a_separator(self):
        body = [
            "COM INFO:--WEF 01JUN26,",
            "SUBJ: SPECIAL SECURITY ARRANGEMENT",
            "----------------------",
            "LEVEL: LOW CMA PERFORM SSA",
            "--------------------------",
            "MORE RULES",
        ]
        parts = _split_com_info_parts(body)
        assert len(parts) == 1
        assert "----------------------" in parts[0]

    def test_no_com_info_prefix_returns_single_part(self):
        body = ["COM INFO: Application of planning minima", "line two"]
        assert _split_com_info_parts(body) == [body]

    def test_zero_for_letter_o_prefix_variant(self):
        body = ["COM INF0:--WEF 19JUN26,", "next line"]
        parts = _split_com_info_parts(body)
        assert len(parts) == 1
        assert parts[0][0] == "WEF 19JUN26,"

    def test_empty_body(self):
        assert _split_com_info_parts([]) == [[]]


class TestPartitionAtDashBoundaries:
    """notam_anchors.py's geometry-aware counterpart to _split_com_info_parts —
    same boundary regex, applied to arbitrary (payload, text) items instead of
    bare strings, via a text_of accessor."""

    def test_first_item_always_seeds_part_one(self):
        items = ["All A350 and BOEING 787:", "are now eligible for CPDLC."]
        parts = _partition_at_dash_boundaries(items, text_of=lambda x: x)
        assert parts == [items]

    def test_splits_at_leading_double_dash(self):
        items = [
            "All A350 and BOEING 787:",
            "are now eligible for CPDLC.",
            "--IN CASE CANNOT BE CONTACTED BKKOC VIA TELEPHONE,",
            "PILOTS MAY CONTACT VIA MS TEAMS.",
        ]
        parts = _partition_at_dash_boundaries(items, text_of=lambda x: x)
        assert parts == [
            ["All A350 and BOEING 787:", "are now eligible for CPDLC."],
            ["--IN CASE CANNOT BE CONTACTED BKKOC VIA TELEPHONE,", "PILOTS MAY CONTACT VIA MS TEAMS."],
        ]

    def test_triple_dash_rule_line_is_not_a_boundary(self):
        items = ["SUBJ: SPECIAL SECURITY ARRANGEMENT", "----------------------", "LEVEL: LOW"]
        parts = _partition_at_dash_boundaries(items, text_of=lambda x: x)
        assert parts == [items]

    def test_works_on_non_string_payloads_via_text_of(self):
        # Mirrors notam_anchors.py's real usage: items are geometry tuples,
        # boundary detection reads the text field via text_of.
        items = [
            (1, 0.1, 0.9, 0.10, 0.12, "All A350 and BOEING 787:"),
            (1, 0.1, 0.9, 0.13, 0.15, "--IN CASE CANNOT BE CONTACTED"),
        ]
        parts = _partition_at_dash_boundaries(items, text_of=lambda item: item[5])
        assert len(parts) == 2
        assert parts[0] == [items[0]]
        assert parts[1] == [items[1]]

    def test_single_item(self):
        assert _partition_at_dash_boundaries(["only line"], text_of=lambda x: x) == [["only line"]]


class TestDailyWindows:
    def test_pure_time_first_line(self):
        assert _parse_daily_windows(["1800-2200", "RWY 01L/19R CLSD"]) == [(1080, 1320)]

    def test_daily_keyword(self):
        slots = _parse_daily_windows(["RWY CLSD DAILY 0430-0930, 1230-1530"])
        assert slots == [(270, 570), (750, 930)]

    def test_closure_period(self):
        assert _parse_daily_windows(["Closure Period (UTC) 1700-2100"]) == [(1020, 1260)]

    def test_no_windows(self):
        assert _parse_daily_windows(["RWY 01L/19R CLSD"]) == []

    def test_midnight_crossing_slot_active(self):
        slots = [(1320, 240)]  # 2200–0400
        assert _is_active_daily(slots, _dt(2026, 6, 20, 23, 0)) is True
        assert _is_active_daily(slots, _dt(2026, 6, 20, 3, 0)) is True
        assert _is_active_daily(slots, _dt(2026, 6, 20, 5, 0)) is False


class TestEffectiveTier:
    def test_t1_downgraded_outside_daily_window(self):
        n = {"tier": 1, "daily_windows": [(1080, 1320)], "date_schedules": []}
        assert _effective_tier(n, _dt(2026, 6, 20, 5, 58)) == 3
        assert _effective_tier(n, _dt(2026, 6, 20, 19, 0)) == 1


class TestIsActive:
    def test_inside_window(self):
        assert _is_active(_dt(2026, 6, 1, 0), _dt(2026, 6, 30, 0), _dt(2026, 6, 20, 13, 5)) is True

    def test_outside_window(self):
        assert _is_active(_dt(2026, 6, 1, 0), _dt(2026, 6, 10, 0), _dt(2026, 6, 20, 13, 5)) is False

    def test_no_window_always_active(self):
        assert _is_active(None, None, _dt(2026, 6, 20, 13, 5)) is True

    def test_open_ended_window_does_not_crash(self):
        # win_start set, win_end None (defensive: treat as open-ended)
        assert _is_active(_dt(2026, 6, 1, 0), None, _dt(2026, 6, 20, 13, 5)) is True
        assert _is_active(_dt(2026, 6, 25, 0), None, _dt(2026, 6, 20, 13, 5)) is False


class TestParseUntil:
    def test_valid_line(self):
        ws, we = _parse_until("16 JUN 26 05:43 UNTIL 16 SEP 26 23:59 ESTIMATED")
        assert ws == _dt(2026, 6, 16, 5, 43)
        assert we == _dt(2026, 9, 16, 23, 59)

    def test_non_until_line(self):
        assert _parse_until("RWY 01L/19R CLSD") is None

    def test_partial_failure_returns_none(self):
        # Second timestamp invalid — must NOT leave a dangling win_start
        assert _parse_until("16 JUN 26 05:43 UNTIL 99 XXX 26 23:59") is None
