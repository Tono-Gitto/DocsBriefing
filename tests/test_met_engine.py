"""Unit tests for met_engine.condense_taf — no PDFs, no API key.

Mirrors the CLAUDE.md §2 semantics (fold completed BECMG/FM, flag in-progress,
overlay ETA±1h window) plus the month-boundary cases that day-of-month
arithmetic used to get wrong.
"""
from datetime import datetime, timezone

from met_engine import (
    condense_taf,
    _fold_conditions,
    _is_pure_wind_change,
    _classify_wx_tier,
    _tier_for_text,
)


def _joined(toks):
    return " ".join(t["t"] for t in toks)


def _dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


BASE_TAF = "FT 200500Z 2006/2112 20010KT 9999 FEW020"


class TestFolding:
    def test_completed_becmg_folds_into_baseline(self):
        # Wind-only BECMG carries the base visibility/cloud forward.
        taf = BASE_TAF + " BECMG 2008/2010 25015KT"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "25015KT 9999 FEW020"
        assert becmg is None
        assert overlays == []

    def test_becmg_in_progress_not_folded(self):
        # In-progress wind-only BECMG shows the full target conditions.
        taf = BASE_TAF + " BECMG 2008/2010 25015KT"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 9, 0))
        assert base == "20010KT 9999 FEW020"
        assert becmg is not None and becmg["text"] == "25015KT 9999 FEW020"

    def test_upcoming_becmg_within_one_hour_is_overlay(self):
        taf = BASE_TAF + " BECMG 2008/2010 25015KT"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 7, 30))
        assert base == "20010KT 9999 FEW020"
        assert becmg is None
        assert len(overlays) == 1 and overlays[0]["text"] == "25015KT"

    def test_fm_folds_once_start_passed(self):
        taf = BASE_TAF + " FM201800 26005KT 4000 FU SCT100 FM210400 30010KT"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 19, 17))
        assert base == "26005KT 4000 FU SCT100"
        assert becmg is None
        assert overlays == []  # FM210400 far in the future

    def test_two_sequential_becmg_first_folds_second_in_progress(self):
        # Both BECMGs are wind-only: base carries 9999 FEW020, and the
        # in-progress second BECMG folds onto the already-folded baseline.
        taf = BASE_TAF + " BECMG 2008/2010 25015KT BECMG 2014/2016 30008KT"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "25015KT 9999 FEW020"
        assert becmg is not None and becmg["text"] == "30008KT 9999 FEW020"


class TestSourceSpans:
    """src_start on becmg_in_progress / active_overlays — the character offset into
    taf_raw the group started at, threaded through for the Source Pane's ETA-window
    highlight (see met_anchors.py, docs/adr/0002-two-document-source-pane.md)."""

    def test_tempo_overlay_src_start_points_at_tempo_token(self):
        taf = BASE_TAF + " TEMPO 2006/2008 3000 TSRA"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 6, 30))
        assert len(overlays) == 1
        s = overlays[0]["src_start"]
        assert taf[s:].startswith("TEMPO")

    def test_becmg_in_progress_src_start_points_at_becmg_token(self):
        taf = BASE_TAF + " BECMG 2008/2010 25015KT"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 9, 0))
        assert becmg is not None
        s = becmg["src_start"]
        assert taf[s:].startswith("BECMG")

    def test_fm_overlay_src_start_points_at_fm_token_despite_normalized_type(self):
        # condense_taf normalizes the output "type" to "FM" (dropping the DDHHMM
        # digits), but src_start must still point at the raw "FM201800..." token.
        taf = BASE_TAF + " FM201800 26005KT 4000 FU SCT100"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 17, 30))
        assert len(overlays) == 1
        assert overlays[0]["type"] == "FM"
        s = overlays[0]["src_start"]
        assert taf[s:].startswith("FM201800")


class TestWindOnlyFold:
    """Wind-only BECMG/FM carries the previous conditions forward (only the
    wind changes); any group that also states vis/weather/cloud fully replaces."""

    def test_wind_only_becmg_carries_vis_and_cloud(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 34005KT"
        base, becmg, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "34005KT 9999 SCT020"
        assert becmg is None

    def test_wind_only_becmg_carries_cavok(self):
        taf = "FT 200500Z 2006/2112 20005KT CAVOK BECMG 2008/2010 04004KT"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "04004KT CAVOK"

    def test_wind_only_fm_carries_forward(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 FM201200 34005KT"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "34005KT 9999 SCT020"

    def test_gust_token_is_wind_only(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 34005G20KT"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "34005G20KT 9999 SCT020"

    def test_non_wind_only_becmg_replaces(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 30010KT 4000 BR"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "30010KT 4000 BR"

    def test_in_progress_wind_only_becmg_merged(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2014/2016 34005KT"
        _, becmg, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert becmg is not None and becmg["text"] == "34005KT 9999 SCT020"

    def test_is_pure_wind_change(self):
        assert _is_pure_wind_change("34005KT")
        assert _is_pure_wind_change("VRB03KT")
        assert _is_pure_wind_change("24008KT 240V300")  # wind + direction variation
        assert not _is_pure_wind_change("34005KT 9999")
        assert not _is_pure_wind_change("CAVOK")
        assert not _is_pure_wind_change("")

    def test_fold_conditions_direct(self):
        assert _fold_conditions("24008KT 9999 SCT020", "34005KT") == "34005KT 9999 SCT020"
        assert _fold_conditions("24008KT 9999", "30010KT 4000 BR") == "30010KT 4000 BR"
        # base without a leading wind token → replace, don't fabricate
        assert _fold_conditions("CAVOK", "34005KT") == "34005KT"


class TestOverlayWindow:
    def test_tempo_overlapping_eta_window_shown(self):
        taf = BASE_TAF + " TEMPO 2014/2018 5000 RA"
        _, _, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert len(overlays) == 1 and overlays[0]["type"] == "TEMPO"

    def test_tempo_ending_before_window_hidden(self):
        taf = BASE_TAF + " TEMPO 2010/2013 5000 RA"
        _, _, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert overlays == []

    def test_prob30_tempo_parsed_as_one_group(self):
        taf = BASE_TAF + " PROB30 TEMPO 2014/2018 TSRA"
        _, _, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert len(overlays) == 1
        assert overlays[0]["type"] == "PROB30 TEMPO"
        assert overlays[0]["text"] == "TSRA"


class TestMonthBoundary:
    """Day-of-month arithmetic regressions: flights within ±1 day of month end."""

    def test_future_becmg_next_month_not_folded(self):
        # Ref 30 Jun 23:00 — BECMG 0104/0106 is 1 Jul, five hours ahead.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 BECMG 0104/0106 25015KT"
        base, becmg, overlays, _ = condense_taf(taf, _dt(2026, 6, 30, 23, 0))
        assert base == "20010KT 9999 FEW020"
        assert becmg is None
        assert overlays == []

    def test_tempo_spanning_month_end_shown_after_midnight(self):
        # Ref 1 Jul 00:30 — TEMPO 3023/0101 is still active.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 TEMPO 3023/0101 5000 RA"
        _, _, overlays, _ = condense_taf(taf, _dt(2026, 7, 1, 0, 30))
        assert len(overlays) == 1 and overlays[0]["text"] == "5000 RA"

    def test_becmg_completed_before_month_rollover_folds(self):
        # Ref 1 Jul 00:30 — BECMG ended 30 Jun 22:00, transition complete.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 BECMG 3020/3022 25015KT"
        base, becmg, _, _ = condense_taf(taf, _dt(2026, 7, 1, 0, 30))
        assert base == "25015KT 9999 FEW020"
        assert becmg is None

    def test_hour_24_window_token(self):
        # 3018/3024 ends at 1 Jul 00:00; by 00:30 it has folded.
        taf = "FT 301700Z 3018/0118 20010KT 9999 FEW020 BECMG 3022/3024 25015KT"
        base, becmg, _, _ = condense_taf(taf, _dt(2026, 7, 1, 0, 30))
        assert base == "25015KT 9999 FEW020"
        assert becmg is None

    def test_year_boundary(self):
        # Ref 1 Jan 00:30 — TEMPO 3123/0101 (31 Dec → 1 Jan) still active.
        taf = "FT 311700Z 3118/0118 20010KT 9999 FEW020 TEMPO 3123/0101 5000 RA"
        _, _, overlays, _ = condense_taf(taf, _dt(2027, 1, 1, 0, 30))
        assert len(overlays) == 1 and overlays[0]["text"] == "5000 RA"


class TestNoGroups:
    def test_taf_without_groups_returns_whole_base(self):
        base, becmg, overlays, _ = condense_taf(BASE_TAF, _dt(2026, 6, 20, 15, 0))
        assert base == "20010KT 9999 FEW020"
        assert becmg is None and overlays == []


class TestWxTierFixtureAnchors:
    """Regression anchors against the validated TG921 fixture rows (CLAUDE.md §2)."""

    def test_eddf_green(self):
        # 23007KT 9999 SCT040 — vis 9999, SCT never counts as a ceiling.
        assert _classify_wx_tier("23007KT 9999 SCT040", None, []) == "GREEN"

    def test_opla_yellow(self):
        # 26005KT 4000 FU SCT100 — vis 4000 falls in the 1600-4999 band.
        assert _classify_wx_tier("26005KT 4000 FU SCT100", None, []) == "YELLOW"

    def test_opkc_yellow_becmg_in_progress(self):
        # Base alone is GREEN-boundary (vis 5000, ceiling 2000) but the
        # in-progress BECMG forces a YELLOW floor regardless of end-state.
        base = "24012G22KT 5000 HZ BKN020"
        becmg = {"text": "25006G16KT 4000 HZ BKN020", "window": "2018Z-2020Z"}
        assert _classify_wx_tier(base, becmg, []) == "YELLOW"

    def test_ltcc_yellow_second_becmg_in_progress(self):
        base = "22006KT CAVOK"
        becmg = {"text": "30006KT CAVOK", "window": "2011Z-2014Z"}
        assert _classify_wx_tier(base, becmg, []) == "YELLOW"

    def test_vtbs_green_no_overlays(self):
        # 24008KT 9999 SCT020, both TEMPOs fall outside the +-1h window.
        assert _classify_wx_tier("24008KT 9999 SCT020", None, []) == "GREEN"


class TestWxTierSynthetic:
    def test_baseline_phenomena_without_severe_vis_ceiling_is_yellow(self):
        # VTCC/TG910 regression: a phenomena keyword (any intensity) never
        # elevates severity past what vis/ceiling numbers themselves say —
        # it only guarantees a YELLOW floor. vis 3000 / ceiling 800ft are
        # both YELLOW-band, not RED-band, so TSRA here stays YELLOW.
        assert _tier_for_text("24010KT 3000 TSRA BKN008") == "YELLOW"
        assert _classify_wx_tier("24010KT 3000 TSRA BKN008", None, []) == "YELLOW"

    def test_phenomena_alone_in_baseline_is_yellow_not_green(self):
        # Clean vis/ceiling (9999 / SCT — SCT never counts as a ceiling)
        # would be GREEN on numbers alone, but TSRA still floors to YELLOW.
        assert _tier_for_text("24010KT 9999 SCT040 TSRA") == "YELLOW"

    def test_phenomena_alone_in_tempo_overlay_is_yellow_not_red(self):
        # VTCC/TG910 regression, exact fixture shape: baseline is clean
        # (9999/SCT), and the only overlay is a light thunderstorm with no
        # vis/ceiling restatement of its own (unchanged from baseline per
        # TAF convention) — this must read YELLOW ("some awareness"), never
        # RED, since RED requires vis/ceiling numbers to actually back it.
        base = "35005KT 9999 SCT035"
        overlays = [{"type": "TEMPO", "text": "-TSRA FEW025CB BKN035", "window": "1718Z-1802Z"}]
        assert _classify_wx_tier(base, None, overlays) == "YELLOW"

    def test_severe_vis_in_prob_overlay_is_capped_to_orange(self):
        # PROB is an explicit probability estimate, not a forecast
        # commitment — even when its own vis/ceiling numbers are RED-level
        # (1200m here), it's capped short of RED, elevated above YELLOW.
        base = "VRB04KT CAVOK"
        overlays = [{"type": "PROB30 TEMPO", "text": "27015G35KT 1200 TSRA BKN008", "window": "2014Z-2018Z"}]
        assert _classify_wx_tier(base, None, overlays) == "ORANGE"

    def test_severe_vis_in_bare_prob_is_capped_to_orange(self):
        base = "VRB04KT CAVOK"
        overlays = [{"type": "PROB40", "text": "27015G35KT 1200 TSRA BKN008", "window": "2014Z-2018Z"}]
        assert _classify_wx_tier(base, None, overlays) == "ORANGE"

    def test_severe_vis_in_plain_tempo_overlay_is_red(self):
        # A plain TEMPO is a deterministic forecast change (just not yet
        # started/in-progress), not a probability estimate — unlike an
        # equivalent PROB overlay, it isn't capped: genuinely RED-level
        # vis/ceiling numbers (1200m here) show as RED.
        base = "VRB04KT CAVOK"
        overlays = [{"type": "TEMPO", "text": "27015G35KT 1200 TSRA BKN008", "window": "2014Z-2018Z"}]
        assert _classify_wx_tier(base, None, overlays) == "RED"

    def test_low_vis_fog_in_tempo_overlay_is_red(self):
        # UTAK/TG910 regression: TEMPO VV001 + 500m vis is RED-severity by
        # vis/ceiling numbers alone (no phenomena keyword match on FG).
        base = "33016G26KT 6000 SCT026 BKN100"
        overlays = [{"type": "TEMPO", "text": "VRB02KT 0500 FG VV001", "window": "1722Z-1803Z"}]
        assert _classify_wx_tier(base, None, overlays) == "RED"

    def test_becmg_in_progress_into_severe_conditions_is_red(self):
        # An in-progress BECMG is mid-transition right now — at least as
        # certain as a TEMPO — so it scores at full severity, not capped.
        # RED comes from vis 800 / ceiling 300ft, not from TSRA itself.
        base = "24010KT 9999 SCT040"
        becmg = {"text": "24020G35KT 800 TSRA BKN003", "window": "2014Z-2018Z"}
        assert _classify_wx_tier(base, becmg, []) == "RED"

    def test_becmg_in_progress_floor_stays_yellow(self):
        # Mid-transition never reads as clean GREEN even when both end
        # states are GREEN-level.
        base = "24010KT CAVOK"
        becmg = {"text": "25008KT CAVOK", "window": "2014Z-2018Z"}
        assert _classify_wx_tier(base, becmg, []) == "YELLOW"

    def test_low_visibility_is_red(self):
        # LVO-class visibility.
        assert _tier_for_text("22005KT 0800 FG") == "RED"

    def test_cavok_is_green(self):
        assert _tier_for_text("15013KT CAVOK") == "GREEN"

    def test_low_ceiling_is_red(self):
        assert _tier_for_text("18010KT 9999 BKN003") == "RED"

    def test_ambiguous_baseline_defaults_yellow(self):
        # Full-state text with no vis token, no CAVOK/NSC — flag for review.
        assert _tier_for_text("TX33/2013Z TN21/2103Z") == "YELLOW"

    def test_wind_only_overlay_is_not_ambiguous(self):
        # A TEMPO that only restates wind (e.g. VHHH "TEMPO 27010KT") means
        # vis/cloud are unchanged from baseline per TAF convention — it must
        # not drag a GREEN baseline up to YELLOW just for lacking a vis token.
        base = "12010KT 9999 FEW015 SCT025"
        overlays = [{"type": "TEMPO", "text": "27010KT", "window": "3004Z-3009Z"}]
        assert _classify_wx_tier(base, None, overlays) == "GREEN"


class TestTokenProvenance:
    """taf_base_src — token-level provenance for taf_base, threaded through
    condense_taf alongside the (unchanged) string fold so the Source Pane can
    highlight the exact source tokens behind "conditions at ETA" (see
    CLAUDE.md "Source Pane", HANDOFF.md worked example)."""

    def test_vtbu_worked_example(self):
        # First BECMG (wind-only) has completed by ref; its wind folds onto
        # the original base's vis/cloud. Second BECMG is still in the future.
        taf = ("FT 200500Z 2006/2106 24004KT 9999 FEW020 "
               "BECMG 2014/2016 36003KT BECMG 2101/2103 20006KT")
        base, becmg, overlays, toks = condense_taf(taf, _dt(2026, 6, 20, 23, 26))
        assert base == "36003KT 9999 FEW020"
        assert toks == [
            {"t": "36003KT", "s": 57},
            {"t": "9999", "s": 29},
            {"t": "FEW020", "s": 34},
        ]

    def test_full_restatement_offsets_all_from_group(self):
        # A BECMG that also restates vis/weather replaces the baseline wholesale
        # — every taf_base_src token must point into the BECMG's own region,
        # none carried forward from the original base line.
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 30010KT 4000 BR"
        base, becmg, overlays, toks = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "30010KT 4000 BR"
        assert toks == [
            {"t": "30010KT", "s": 57},
            {"t": "4000", "s": 65},
            {"t": "BR", "s": 70},
        ]

    def test_no_groups_yields_base_region_offsets_only(self):
        taf = "FT 200500Z 2006/2112 20010KT 9999 FEW020"
        base, becmg, overlays, toks = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "20010KT 9999 FEW020"
        assert toks == [
            {"t": "20010KT", "s": 21},
            {"t": "9999", "s": 29},
            {"t": "FEW020", "s": 34},
        ]

    def test_wind_only_fold_with_variation_token_keeps_both_new_tokens(self):
        # Pure wind change with a 250V310-style variation token: both new
        # tokens are kept, followed by the old base's carried-forward rest.
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 36003KT 330V030"
        base, becmg, overlays, toks = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "36003KT 330V030 9999 SCT020"
        assert toks == [
            {"t": "36003KT", "s": 57},
            {"t": "330V030", "s": 65},
            {"t": "9999", "s": 29},
            {"t": "SCT020", "s": 34},
        ]

    def test_joined_taf_base_src_always_equals_taf_base(self):
        """Property check across a spread of existing fixture TAFs/ref times:
        joining taf_base_src's tokens must always reproduce taf_base exactly."""
        cases = [
            (BASE_TAF + " BECMG 2008/2010 25015KT", _dt(2026, 6, 20, 15, 0)),
            (BASE_TAF + " BECMG 2008/2010 25015KT", _dt(2026, 6, 20, 9, 0)),
            (BASE_TAF + " BECMG 2008/2010 25015KT BECMG 2014/2016 30008KT", _dt(2026, 6, 20, 15, 0)),
            (BASE_TAF + " FM201800 26005KT 4000 FU SCT100 FM210400 30010KT", _dt(2026, 6, 20, 19, 17)),
            ("FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 34005KT", _dt(2026, 6, 20, 15, 0)),
            ("FT 200500Z 2006/2112 20005KT CAVOK BECMG 2008/2010 04004KT", _dt(2026, 6, 20, 15, 0)),
            ("FT 301700Z 3018/0118 20010KT 9999 FEW020 BECMG 0104/0106 25015KT", _dt(2026, 6, 30, 23, 0)),
            (BASE_TAF, _dt(2026, 6, 20, 15, 0)),
        ]
        for taf, ref in cases:
            base, _, _, toks = condense_taf(taf, ref)
            assert _joined(toks) == base, f"mismatch for {taf!r} @ {ref}"
