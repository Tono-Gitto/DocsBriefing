"""Unit tests for notam_engine helpers — no PDFs, no API key."""
from datetime import datetime, timezone

import pytest

from notam_engine import (
    _classify_tier,
    _effective_tier,
    _is_active,
    _is_active_daily,
    _parse_daily_windows,
    _parse_until,
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
