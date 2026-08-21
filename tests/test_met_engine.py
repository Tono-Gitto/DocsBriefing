"""Unit tests for met_engine.condense_taf — no PDFs, no API key.

Mirrors the CLAUDE.md §2 semantics (fold completed BECMG/FM, flag in-progress,
overlay ETA±1h window) plus the month-boundary cases that day-of-month
arithmetic used to get wrong.
"""
from datetime import datetime, timezone

from met_engine import (
    condense_taf,
    _fold_conditions,
    _tok_category,
    _classify_wx_tier,
    _tier_for_text,
    _strip_temp,
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


class TestElementWiseFold:
    """A BECMG states only the elements that change; every element it is silent
    on persists from the preceding conditions (ICAO Annex 3). An FM is a
    complete restatement and replaces the baseline wholesale.

    This used to be implemented as a wind-only special case, which silently
    dropped whatever a partial BECMG didn't restate — ~20% of the BECMG groups
    across the fixture MET PDFs. See TestPartialBecmgRegression below."""

    def test_wind_only_becmg_carries_vis_and_cloud(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 34005KT"
        base, becmg, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "34005KT 9999 SCT020"
        assert becmg is None

    def test_wind_only_becmg_carries_cavok(self):
        taf = "FT 200500Z 2006/2112 20005KT CAVOK BECMG 2008/2010 04004KT"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "04004KT CAVOK"

    def test_wind_only_fm_replaces_rather_than_carrying(self):
        # An FM supersedes everything before it, so even a wind-only one does
        # not carry vis/cloud forward — carrying would assert a visibility the
        # group never stated. The bare result trips _tier_for_text's
        # ambiguity default (YELLOW), which is the right failure mode for a
        # malformed group: flag it, don't fabricate confidence. No FM in any
        # fixture MET PDF is partial, so this only governs bad input.
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 FM201200 34005KT"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "34005KT"
        assert _tier_for_text(base) == "YELLOW"

    def test_gust_token_is_wind_only(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 34005G20KT"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "34005G20KT 9999 SCT020"

    def test_becmg_stating_wind_vis_wx_still_carries_cloud(self):
        # The group restates wind/vis/weather but says nothing about cloud, so
        # SCT020 persists. (Under the old wind-only rule this replaced the
        # baseline wholesale and SCT020 was lost.)
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 30010KT 4000 BR"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "30010KT 4000 BR SCT020"

    def test_full_restatement_replaces_every_element(self):
        taf = ("FT 200500Z 2006/2112 24008KT 9999 SCT020 "
               "BECMG 2008/2010 30010KT 4000 BR BKN008")
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "30010KT 4000 BR BKN008"

    def test_fm_replaces_wholesale_even_when_partial(self):
        # FM is a complete restatement by definition — unlike BECMG, nothing
        # carries forward. All 5 FM groups in the fixture MET PDFs state wind
        # and visibility, consistent with that reading.
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 FM201200 30010KT 4000 BR"
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "30010KT 4000 BR"

    def test_in_progress_wind_only_becmg_merged(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2014/2016 34005KT"
        _, becmg, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert becmg is not None and becmg["text"] == "34005KT 9999 SCT020"

    def test_tok_category(self):
        assert _tok_category("34005KT") == "WIND"
        assert _tok_category("34005G20KT") == "WIND"
        assert _tok_category("VRB03KT") == "WIND"
        assert _tok_category("240V300") == "WIND"
        assert _tok_category("9999") == "VIS"
        assert _tok_category("CAVOK") == "CAVOK"
        assert _tok_category("SCT020") == "CLOUD"
        assert _tok_category("FEW018CB") == "CLOUD"
        assert _tok_category("SCT040TCU") == "CLOUD"
        assert _tok_category("NSC") == "CLOUD"
        assert _tok_category("VV003") == "CLOUD"
        assert _tok_category("-TSRA") == "WX"
        assert _tok_category("BR") == "WX"
        assert _tok_category("HZ") == "WX"
        assert _tok_category("NSW") == "WX"
        # not conditions — classified OTHER, though _parse_groups strips these
        # specific tokens (max/min temp) before they ever reach this function
        # in the real pipeline (see TestTempStripping below)
        assert _tok_category("TX23/1715Z") == "OTHER"
        assert _tok_category("TN16/1804Z") == "OTHER"

    def test_fold_conditions_direct(self):
        assert _fold_conditions("24008KT 9999 SCT020", "34005KT") == "34005KT 9999 SCT020"
        # silent on cloud → SCT020 persists
        assert _fold_conditions("24008KT 9999 SCT020", "30010KT 4000 BR") == \
            "30010KT 4000 BR SCT020"
        # a baseline with no wind is fine — the new wind simply fills that slot
        assert _fold_conditions("CAVOK", "34005KT") == "34005KT CAVOK"
        # FM ignores the old baseline entirely
        assert _fold_conditions("24008KT 9999 SCT020", "34005KT", becmg=False) == "34005KT"

    def test_temps_survive_the_merge_primitive_itself(self):
        # _fold_conditions is the generic element-wise merge; on its own it
        # still carries an OTHER-category token through untouched. In the real
        # pipeline TX/TN never reach it — _parse_groups strips them first (see
        # TestTempStripping) — so this only documents the primitive's own
        # behavior, not what the crew sees.
        assert _fold_conditions("24006KT 9999 FEW015 TX23/1715Z TN16/1804Z", "FEW030") == \
            "24006KT 9999 FEW030 TX23/1715Z TN16/1804Z"


class TestCavokFold:
    """CAVOK is the one token spanning three elements (vis + weather + cloud),
    so folding it needs its own rules in both directions."""

    def test_new_cavok_subsumes_vis_wx_cloud_but_not_wind(self):
        assert _fold_conditions("20015KT 3000 BR FEW040", "CAVOK") == "20015KT CAVOK"

    def test_old_cavok_survives_a_wind_only_change(self):
        # RKSI/TG677: CAVOK on both sides of a wind-only BECMG.
        assert _fold_conditions("20010KT CAVOK", "14010KT") == "14010KT CAVOK"

    def test_old_cavok_degrades_to_9999_when_new_states_cloud(self):
        # LTAC (TG910/TG934/TG970): "BECMG CAVOK" then "BECMG SCT040".
        # Emitting "CAVOK SCT040" would be self-contradictory; CAVOK's cloud
        # claim is superseded, its ≥10 km visibility claim still holds.
        assert _fold_conditions("24006KT CAVOK", "SCT040") == "24006KT 9999 SCT040"

    def test_old_cavok_fully_superseded_when_new_states_vis(self):
        assert _fold_conditions("24006KT CAVOK", "4000 HZ") == "24006KT 4000 HZ"


class TestPartialBecmgRegression:
    """Real partial-BECMG groups from the fixture MET PDFs. Each one used to
    discard every element the group didn't restate; the tier consequence
    depended on what happened to be left over."""

    def test_lszh_cloud_only_becmg_keeps_wind_and_visibility(self):
        # TG970 UPDATE, LSZH (destination) at ETA 18AUG 0527Z. Full raw TAF,
        # TX/TN temp groups included as they appear in the real fixture PDF.
        # "BECMG 1719/1721 FEW030" restates cloud only. Dropping 24006KT and
        # 9999 left a bare "FEW030": no vis token and no CAVOK, which
        # _tier_for_text treats as ambiguous → YELLOW on a GREEN airport.
        taf = ("FT 171327Z 1713/1818 24006KT 9999 FEW015 SCT040TCU "
               "TX23/1715Z TN16/1804Z TX27/1814Z "
               "TEMPO 1713/1716 SCT020 PROB40 TEMPO 1713/1719 SHRA "
               "PROB30 TEMPO 1714/1719 26010KT TSRA SCT040CB "
               "BECMG 1719/1721 FEW030 TEMPO 1800/1809 CAVOK "
               "BECMG 1809/1811 28012KT")
        base, becmg, overlays, toks = condense_taf(taf, _dt(2026, 8, 18, 5, 27))
        # Baseline holds at ETA with no leftover temp-forecast tokens.
        assert base == "24006KT 9999 FEW030"
        assert becmg is None
        assert overlays == [{"type": "TEMPO", "text": "CAVOK",
                              "window": "18/0000Z-18/0900Z", "src_start": 203}]
        assert _classify_wx_tier(base, becmg, overlays) == "GREEN"
        assert " ".join(t["t"] for t in toks) == base

    def test_vaah_vis_dropped_used_to_read_as_green(self):
        # TG910, VAAH. The mirror-image failure, and the dangerous one: when
        # the surviving tokens still contain a BKN/OVC layer, _tier_for_text's
        # ambiguity backstop doesn't fire and the missing visibility silently
        # defaults to 9999 — a 3000 m forecast reading as GREEN.
        taf = ("FT 100500Z 1006/1112 22010KT 3000 -RA BR FEW020 SCT025 BKN080 "
               "BECMG 1008/1010 23012KT FEW020 SCT025 BKN080")
        base, _, _, _ = condense_taf(taf, _dt(2026, 8, 10, 15, 0))
        assert base == "23012KT 3000 -RA BR FEW020 SCT025 BKN080"
        assert _classify_wx_tier(base, None, []) == "YELLOW"

    def test_ltcc_wind_and_cloud_becmg_keeps_visibility(self):
        # TG921 LTCC @ 1608Z. "BECMG 20015KT FEW040" is silent on visibility,
        # so 9999 persists and the baseline is GREEN. This assertion used to
        # read YELLOW, rationalised through the same ambiguity default rather
        # than questioning the fold.
        assert _fold_conditions("18010KT 9999 SCT030", "20015KT FEW040") == \
            "20015KT 9999 FEW040"
        assert _tier_for_text("20015KT 9999 FEW040") == "GREEN"

    def test_group_stating_visibility_twice_replaces_wholesale(self):
        # TG970 UPDATE OPKC. The PDF's "FM 180500" is space-split, so
        # _GROUP_RE (which wants FM\d{6}) misses it and two states end up
        # inside the preceding BECMG's text. Element-merging that would
        # interleave them into "4000 4000 HZ HZ SCT020 BKN030 SCT020 BKN030";
        # a group this untrustworthy falls back to wholesale replacement,
        # keeping the run-together text in its original readable order.
        run_together = "26008G18KT 4000 HZ SCT020 BKN030 FM 180500 25010G25KT 4000 HZ SCT020 BKN030"
        assert _fold_conditions("24012G22KT 5000 HZ SCT020 BKN030", run_together) == run_together

    def test_low_ceiling_still_yellow_after_the_fold_is_fixed(self):
        # Carrying visibility forward must not launder a genuinely low ceiling:
        # TG934 LTFM "BECMG BKN012" is still YELLOW on its 1200 ft ceiling.
        assert _fold_conditions("05018G28KT 9999 SCT020", "BKN012") == \
            "05018G28KT 9999 BKN012"
        assert _tier_for_text("05018G28KT 9999 BKN012") == "YELLOW"


class TestTempStripping:
    """Max/min-temperature forecast groups (TX/TN) aren't a TAF condition and
    aren't shown to the crew — _parse_groups drops them before the baseline or
    any BECMG/TEMPO/PROB group text or token list is built, so neither the
    string fold nor the token fold in condense_taf ever carries one forward."""

    def test_strip_temp_drops_tx_and_tn(self):
        toks = [{"t": "24006KT", "s": 0}, {"t": "TX23/1715Z", "s": 8},
                 {"t": "TN16/1804Z", "s": 19}, {"t": "9999", "s": 30}]
        assert [t["t"] for t in _strip_temp(toks)] == ["24006KT", "9999"]

    def test_strip_temp_handles_negative_temperature(self):
        # M prefix = below zero (e.g. TNM04/0412Z = min -4C at 04/1200Z).
        toks = [{"t": "TNM04/0412Z", "s": 0}, {"t": "9999", "s": 12}]
        assert [t["t"] for t in _strip_temp(toks)] == ["9999"]

    def test_baseline_drops_temp_tokens(self):
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 TX23/1715Z TN16/1804Z"
        base, _, _, toks = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "24008KT 9999 SCT020"
        assert " ".join(t["t"] for t in toks) == base

    def test_temp_token_inside_a_becmg_group_is_also_dropped(self):
        # Malformed/unusual input — a temp group restated inside a BECMG's own
        # text — but the strip applies uniformly to every group, not just the
        # baseline, since it happens at tokenize time in _parse_groups.
        taf = ("FT 200500Z 2006/2112 24008KT 9999 SCT020 "
               "BECMG 2008/2010 25015KT TX23/1715Z")
        base, _, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "25015KT 9999 SCT020"

    def test_temp_token_inside_an_overlay_is_dropped(self):
        taf = "FT 200500Z 2006/2112 20010KT 9999 FEW020 TEMPO 2014/2018 5000 RA TX23/1715Z"
        _, _, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert len(overlays) == 1 and overlays[0]["text"] == "5000 RA"


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


class TestWindowFormat:
    """Group windows render as DD/HHMMZ. The day is explicit so a window can't
    be misread as the HHMM 'CONDITIONS AT' time sitting next to it in the panel,
    and the minutes are explicit so an FM group's minutes aren't dropped."""

    def test_becmg_window_carries_the_day(self):
        taf = BASE_TAF + " BECMG 2014/2016 25015KT"
        _, becmg, _, _ = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert becmg["window"] == "20/1400Z-20/1600Z"

    def test_fm_window_keeps_minutes(self):
        taf = BASE_TAF + " FM201830 25015KT 3000 BR"
        _, _, overlays, _ = condense_taf(taf, _dt(2026, 6, 20, 18, 0))
        assert overlays[0]["window"] == "from 20/1830Z"


class TestWxTierFixtureAnchors:
    """Regression anchors against the validated TG921 fixture rows (CLAUDE.md §2)."""

    def test_eddf_green(self):
        # 23007KT 9999 SCT040 — vis 9999, SCT never counts as a ceiling.
        assert _classify_wx_tier("23007KT 9999 SCT040", None, []) == "GREEN"

    def test_opla_yellow(self):
        # 26005KT 4000 FU SCT100 — vis 4000 falls in the 2400-4999 band.
        assert _classify_wx_tier("26005KT 4000 FU SCT100", None, []) == "YELLOW"

    def test_opkc_yellow_becmg_in_progress(self):
        # Base alone is GREEN-boundary (vis 5000, ceiling 2000); the YELLOW
        # comes from the BECMG target's own vis 4000, not from the fact that
        # a transition is under way.
        base = "24012G22KT 5000 HZ BKN020"
        becmg = {"text": "25006G16KT 4000 HZ BKN020", "window": "20/1800Z-20/2000Z"}
        assert _classify_wx_tier(base, becmg, []) == "YELLOW"

    def test_ltcc_green_second_becmg_in_progress(self):
        # Real TG921 LTCC @ 1608Z. This asserted YELLOW until the element-wise
        # BECMG fold landed. The first BECMG ("20015KT FEW040") restates wind
        # and cloud only, so the baseline's 9999 persists instead of being
        # discarded — and once the vis token is there, _tier_for_text's
        # ambiguity default no longer fires. Both end-states are GREEN: an
        # airport with 10 km visibility transitioning to CAVOK.
        base = "20015KT 9999 FEW040"
        becmg = {"text": "VRB02KT CAVOK", "window": "20/1600Z-20/1800Z"}
        assert _tier_for_text(becmg["text"]) == "GREEN"
        assert _classify_wx_tier(base, becmg, []) == "GREEN"

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

    def test_becmg_in_progress_has_no_floor_of_its_own(self):
        # RKSI/TG677 @ 1007Z, the regression this rule was changed for: a
        # wind-only BECMG between two CAVOK states. Both end-states GREEN on
        # vis/ceiling => GREEN. An in-progress BECMG contributes only its
        # target's severity, never a floor.
        base = "20010KT CAVOK TN24/1020Z TX33/1106Z"
        becmg = {"text": "14010KT CAVOK TN24/1020Z TX33/1106Z",
                 "window": "10/1000Z-10/1200Z"}
        assert _classify_wx_tier(base, becmg, []) == "GREEN"

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

    def test_restated_elements_point_at_the_group_carried_ones_at_the_base(self):
        # Mixed provenance is the normal case for an element-wise fold: the
        # wind/vis/weather tokens the BECMG restates point into the BECMG's own
        # region (s≥57), while the cloud it is silent on keeps the original
        # base line's offset (s=34) so the Source Pane highlights the token the
        # crew is actually reading.
        taf = "FT 200500Z 2006/2112 24008KT 9999 SCT020 BECMG 2008/2010 30010KT 4000 BR"
        base, becmg, overlays, toks = condense_taf(taf, _dt(2026, 6, 20, 15, 0))
        assert base == "30010KT 4000 BR SCT020"
        assert toks == [
            {"t": "30010KT", "s": 57},
            {"t": "4000", "s": 65},
            {"t": "BR", "s": 70},
            {"t": "SCT020", "s": 34},
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
