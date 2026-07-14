"""Unit tests for airport_coords.load_coords — no network, synthetic CSV fixture.

Regression coverage for the "no coords for UZTT/UZSS — airport dropped" bug:
OurAirports keeps `ident` as a stable internal key that can lag behind a
real-world ICAO re-designation (Uzbekistan UT→UZ, 2024). The fix indexes
`icao_code`/`gps_code` too, with icao_code taking priority.
"""
import csv
import os
import time

import pytest

import airport_coords

CSV_HEADER = [
    "id", "ident", "type", "name", "latitude_deg", "longitude_deg",
    "elevation_ft", "continent", "iso_country", "iso_region", "municipality",
    "scheduled_service", "icao_code", "iata_code", "gps_code", "local_code",
    "home_link", "wikipedia_link", "keywords",
]


def _row(ident, lat, lon, icao_code="", gps_code="", name="Test Airport"):
    return {
        "id": "1", "ident": ident, "type": "large_airport", "name": name,
        "latitude_deg": str(lat), "longitude_deg": str(lon), "elevation_ft": "100",
        "continent": "AS", "iso_country": "UZ", "iso_region": "UZ-TO",
        "municipality": "Test", "scheduled_service": "yes",
        "icao_code": icao_code, "iata_code": "", "gps_code": gps_code,
        "local_code": "", "home_link": "", "wikipedia_link": "", "keywords": "",
    }


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        for row in rows:
            w.writerow(row)


@pytest.fixture(autouse=True)
def _reset_cache():
    airport_coords._coords_cache = None
    yield
    airport_coords._coords_cache = None


class TestRedesignation:
    def test_old_and_new_code_both_resolve(self, tmp_path):
        csv_path = tmp_path / "airports.csv"
        _write_csv(csv_path, [
            _row("UTTT", 41.2579, 69.281197, icao_code="UZTT", name="Tashkent"),
            _row("UTSS", 39.701842, 66.981467, icao_code="UZSS", name="Samarkand"),
        ])
        coords = airport_coords.load_coords(str(csv_path))
        assert coords["UZTT"] == (41.2579, 69.281197)
        assert coords["UTTT"] == (41.2579, 69.281197)
        assert coords["UZSS"] == (39.701842, 66.981467)
        assert coords["UTSS"] == (39.701842, 66.981467)


class TestPriority:
    def test_icao_code_wins_over_recycled_ident(self, tmp_path):
        # Row A's old ident is recycled as row B's current icao_code.
        csv_path = tmp_path / "airports.csv"
        _write_csv(csv_path, [
            _row("XXXX", 10.0, 20.0, name="Old Claimant"),
            _row("YYYY", 30.0, 40.0, icao_code="XXXX", name="Current Claimant"),
        ])
        coords = airport_coords.load_coords(str(csv_path))
        assert coords["XXXX"] == (30.0, 40.0)

    def test_icao_code_wins_over_gps_code(self, tmp_path):
        csv_path = tmp_path / "airports.csv"
        _write_csv(csv_path, [
            _row("ZZZZ", 1.0, 2.0, icao_code="AAAA", gps_code="BBBB"),
        ])
        coords = airport_coords.load_coords(str(csv_path))
        assert coords["AAAA"] == (1.0, 2.0)
        assert coords["BBBB"] == (1.0, 2.0)
        assert coords["ZZZZ"] == (1.0, 2.0)


class TestBlankColumns:
    def test_blank_icao_and_gps_do_not_clobber(self, tmp_path):
        csv_path = tmp_path / "airports.csv"
        _write_csv(csv_path, [
            _row("VTBS", 13.6811, 100.747002),
        ])
        coords = airport_coords.load_coords(str(csv_path))
        assert coords == {"VTBS": (13.6811, 100.747002)}
        assert "" not in coords


class TestStaleness:
    def test_stale_download_failure_falls_back_to_cache(self, tmp_path, monkeypatch):
        csv_path = tmp_path / "airports.csv"
        _write_csv(csv_path, [_row("VTBS", 13.6811, 100.747002)])
        old = time.time() - 40 * 86400
        os.utime(csv_path, (old, old))

        def _boom():
            raise RuntimeError("network unreachable")

        monkeypatch.setattr(airport_coords, "_download", _boom)
        coords = airport_coords.load_coords(str(csv_path))
        assert coords["VTBS"] == (13.6811, 100.747002)

    def test_fresh_file_skips_download(self, tmp_path, monkeypatch):
        csv_path = tmp_path / "airports.csv"
        _write_csv(csv_path, [_row("VTBS", 13.6811, 100.747002)])

        def _boom():
            raise AssertionError("should not be called for a fresh file")

        monkeypatch.setattr(airport_coords, "_download", _boom)
        coords = airport_coords.load_coords(str(csv_path))
        assert coords["VTBS"] == (13.6811, 100.747002)


class TestAgainstRealCache:
    def test_known_icaos_resolve(self):
        real_csv = airport_coords.RAW_CSV
        if not os.path.exists(real_csv):
            pytest.skip("data/airports_raw.csv not present")
        coords = airport_coords.load_coords(real_csv)
        for icao in ("UZTT", "UZSS", "UTTT", "UTSS", "VTBS", "EGLL"):
            assert icao in coords, f"{icao} missing from coords"
