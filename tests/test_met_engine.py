"""Unit tests for met_engine.condense_taf — no PDFs, no API key.

Mirrors the CLAUDE.md §2 semantics (fold completed BECMG/FM, flag in-progress,
overlay ETA±1h window) plus the month-boundary cases that day-of-month
arithmetic used to get wrong.
"""
from datetime import datetime, timezone

from met_engine import condense_taf


def _dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


BASE_TAF = "FT 200500Z 2006/2112 20010KT 9999 FEW020"


class TestFolding:
    def test_completed_becmg_folds_into_baseline(self):
        taf = BASE_TAF + " BECMG 2008/2010 25015KT"
        base, becmg, overlays = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "25015KT"
        assert becmg is None
        assert overlays == []

    def test_becmg_in_progress_not_folded(self):
        taf = BASE_TAF + " BECMG 2008/2010 25015KT"
        base, becmg, overlays = condense_taf(taf, _dt(2026, 6, 20, 9, 0))
        assert base == "20010KT 9999 FEW020"
        assert becmg is not None and becmg["text"] == "25015KT"

    def test_upcoming_becmg_within_one_hour_is_overlay(self):
        taf = BASE_TAF + " BECMG 2008/2010 25015KT"
        base, becmg, overlays = condense_taf(taf, _dt(2026, 6, 20, 7, 30))
        assert base == "20010KT 9999 FEW020"
        assert becmg is None
        assert len(overlays) == 1 and overlays[0]["text"] == "25015KT"

    def test_fm_folds_once_start_passed(self):
        taf = BASE_TAF + " FM201800 26005KT 4000 FU SCT100 FM210400 30010KT"
        base, becmg, overlays = condense_taf(taf, _dt(2026, 6, 20, 19, 17))
        assert base == "26005KT 4000 FU SCT100"
        assert becmg is None
        assert overlays == []  # FM210400 far in the future

    def test_two_sequential_becmg_first_folds_second_in_progress(self):
        taf = BASE_TAF + " BECMG 2008/2010 25015KT BECMG 2014/2016 30008KT"
        base, becmg, overlays = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "25015KT"
        assert becmg is not None and becmg["text"] == "30008KT"


class TestOverlayWindow:
    def test_tempo_overlapping_eta_window_shown(self):
        taf = BASE_TAF + " TEMPO 2014/2018 5000 RA"
        _, _, overlays = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert len(overlays) == 1 and overlays[0]["type"] == "TEMPO"

    def test_tempo_ending_before_window_hidden(self):
        taf = BASE_TAF + " TEMPO 2010/2013 5000 RA"
        _, _, overlays = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert overlays == []

    def test_prob30_tempo_parsed_as_one_group(self):
        taf = BASE_TAF + " PROB30 TEMPO 2014/2018 TSRA"
        _, _, overlays = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert len(overlays) == 1
        assert overlays[0]["type"] == "PROB30 TEMPO"
        assert overlays[0]["text"] == "TSRA"


class TestMonthBoundary:
    """Day-of-month arithmetic regressions: flights within ±1 day of month end."""

    def test_future_becmg_next_month_not_folded(self):
        # Ref 30 Jun 23:00 — BECMG 0104/0106 is 1 Jul, five hours ahead.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 BECMG 0104/0106 25015KT"
        base, becmg, overlays = condense_taf(taf, _dt(2026, 6, 30, 23, 0))
        assert base == "20010KT 9999 FEW020"
        assert becmg is None
        assert overlays == []

    def test_tempo_spanning_month_end_shown_after_midnight(self):
        # Ref 1 Jul 00:30 — TEMPO 3023/0101 is still active.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 TEMPO 3023/0101 5000 RA"
        _, _, overlays = condense_taf(taf, _dt(2026, 7, 1, 0, 30))
        assert len(overlays) == 1 and overlays[0]["text"] == "5000 RA"

    def test_becmg_completed_before_month_rollover_folds(self):
        # Ref 1 Jul 00:30 — BECMG ended 30 Jun 22:00, transition complete.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 BECMG 3020/3022 25015KT"
        base, becmg, _ = condense_taf(taf, _dt(2026, 7, 1, 0, 30))
        assert base == "25015KT"
        assert becmg is None

    def test_hour_24_window_token(self):
        # 3018/3024 ends at 1 Jul 00:00; by 00:30 it has folded.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 BECMG 3022/3024 25015KT"
        base, becmg, _ = condense_taf(taf, _dt(2026, 7, 1, 0, 30))
        assert base == "25015KT"
        assert becmg is None

    def test_year_boundary(self):
        # Ref 1 Jan 00:30 — TEMPO 3123/0101 (31 Dec → 1 Jan) still active.
        taf = "FT 311700Z 3118/0118 20010KT 9999 FEW020 TEMPO 3123/0101 5000 RA"
        _, _, overlays = condense_taf(taf, _dt(2027, 1, 1, 0, 30))
        assert len(overlays) == 1 and overlays[0]["text"] == "5000 RA"


class TestNoGroups:
    def test_taf_without_groups_returns_whole_base(self):
        base, becmg, overlays = condense_taf(BASE_TAF, _dt(2026, 6, 20, 15, 0))
        assert base == "20010KT 9999 FEW020"
        assert becmg is None and overlays == []
