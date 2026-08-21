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
