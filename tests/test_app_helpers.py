"""Unit tests for app.py pipeline helpers — no PDFs, no API key, no server."""
from datetime import datetime, timezone

from app import (
    _fir_marker_position,
    _is_active_for_flight,
    _leg_ref_dt,
    _merge_airports_legs,
)


def _dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestLegRefDt:
    """C1 regression: ref times must land on the correct calendar day."""

    def test_ref_iso_preferred(self):
        leg = {"ref_iso": "2026-06-28T04:25:00+00:00", "ref_time": "0425Z"}
        assert _leg_ref_dt(leg, _dt(2026, 6, 27, 17, 25)) == _dt(2026, 6, 28, 4, 25)

    def test_same_day_fallback(self):
        # TG921: takeoff 1305Z, VTBS ref 2326Z the same day
        leg = {"ref_time": "2326Z"}
        assert _leg_ref_dt(leg, _dt(2026, 6, 20, 13, 5)) == _dt(2026, 6, 20, 23, 26)

    def test_midnight_rollover_fallback(self):
        # TG934-style: takeoff 27 Jun 1725Z, destination ref 0425Z = 28 Jun
        leg = {"ref_time": "0425Z"}
        assert _leg_ref_dt(leg, _dt(2026, 6, 27, 17, 25)) == _dt(2026, 6, 28, 4, 25)


class TestIsActiveForFlight:
    FLIGHT_START = _dt(2026, 6, 27, 17, 25)
    FLIGHT_END = _dt(2026, 6, 28, 17, 25)

    def test_overlapping_window(self):
        assert _is_active_for_flight(
            _dt(2026, 6, 28, 6, 0), _dt(2026, 6, 28, 10, 0),
            self.FLIGHT_START, self.FLIGHT_END) is True

    def test_window_entirely_before(self):
        assert _is_active_for_flight(
            _dt(2026, 6, 20, 0, 0), _dt(2026, 6, 25, 0, 0),
            self.FLIGHT_START, self.FLIGHT_END) is False

    def test_window_entirely_after(self):
        assert _is_active_for_flight(
            _dt(2026, 7, 5, 0, 0), _dt(2026, 7, 10, 0, 0),
            self.FLIGHT_START, self.FLIGHT_END) is False

    def test_no_window_always_active(self):
        assert _is_active_for_flight(None, None, self.FLIGHT_START, self.FLIGHT_END) is True

    def test_open_ended_window(self):
        assert _is_active_for_flight(
            _dt(2026, 6, 1, 0, 0), None, self.FLIGHT_START, self.FLIGHT_END) is True


class TestMergeAirportsLegs:
    def test_shared_airport_gets_two_legs(self):
        leg1 = [{"icao": "VTBS", "iata": "BKK", "name": "Suvarnabhumi", "lat": 13.68,
                 "lon": 100.75, "ref_time": "0230Z", "ref_iso": "2026-06-28T02:30:00+00:00",
                 "taf_base": "A", "becmg_in_progress": None, "active_overlays": []}]
        leg2 = [{"icao": "VTBS", "iata": "BKK", "name": "Suvarnabhumi", "lat": 13.68,
                 "lon": 100.75, "ref_time": "0830Z", "ref_iso": "2026-06-28T08:30:00+00:00",
                 "taf_base": "B", "becmg_in_progress": None, "active_overlays": []}]
        merged = _merge_airports_legs([leg1, leg2])
        assert len(merged) == 1
        legs = merged[0]["legs"]
        assert [l["leg"] for l in legs] == [1, 2]
        assert legs[0]["ref_iso"] == "2026-06-28T02:30:00+00:00"
        assert legs[1]["taf_base"] == "B"

    def test_airport_only_in_leg_two(self):
        leg1 = [{"icao": "VTBS", "lat": 1, "lon": 1}]
        leg2 = [{"icao": "WMKK", "lat": 2, "lon": 2}]
        merged = _merge_airports_legs([leg1, leg2])
        wmkk = next(a for a in merged if a["icao"] == "WMKK")
        assert [l["leg"] for l in wmkk["legs"]] == [2]

    def test_taf_base_src_survives_merge(self):
        # Regression for the per-leg field whitelist in _merge_airports_legs —
        # a field left out here silently vanishes in every Flask run even
        # though the CLI/met_engine.py output has it (see HANDOFF.md gotcha 1).
        leg1 = [{"icao": "VTBS", "lat": 1, "lon": 1,
                 "taf_base_src": [{"t": "24008KT", "s": 21}]}]
        merged = _merge_airports_legs([leg1])
        assert merged[0]["legs"][0]["taf_base_src"] == [{"t": "24008KT", "s": 21}]


class TestFirMarkerPosition:
    def test_skips_waypoint_near_airport(self):
        centroid = (13.4, 100.6)
        route = [
            {"lat": 13.68, "lon": 100.75},   # on top of VTBS
            {"lat": 14.50, "lon": 101.50},   # clear of airports
        ]
        airports = [{"lat": 13.68, "lon": 100.75}]
        assert _fir_marker_position(centroid, route, airports) == (14.50, 101.50)

    def test_falls_back_to_centroid_when_all_near(self):
        centroid = (13.4, 100.6)
        route = [{"lat": 13.68, "lon": 100.75}]
        airports = [{"lat": 13.68, "lon": 100.75}]
        assert _fir_marker_position(centroid, route, airports) == centroid
