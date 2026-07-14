import csv
import os
import time

import requests

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RAW_CSV = os.path.join(os.path.dirname(__file__), "data", "airports_raw.csv")
MAX_AGE_DAYS = 30


def _download():
    os.makedirs(os.path.dirname(RAW_CSV), exist_ok=True)
    print("Downloading OurAirports CSV…", flush=True)
    r = requests.get(OURAIRPORTS_URL, timeout=30)
    r.raise_for_status()
    with open(RAW_CSV, "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"Saved to {RAW_CSV}")


def _is_stale(csv_path, max_age_days=MAX_AGE_DAYS):
    age_seconds = time.time() - os.path.getmtime(csv_path)
    return age_seconds > max_age_days * 86400


_coords_cache = None


def load_coords(csv_path=RAW_CSV) -> dict:
    """Return {icao: (lat_float, lon_float)} for every airport in the CSV.

    OurAirports' `ident` column is a stable internal key that can lag behind
    real-world ICAO re-designations (e.g. Uzbekistan UT→UZ in 2024) — the
    current code only appears in `icao_code`/`gps_code`. Keys are merged with
    priority icao_code > gps_code > ident so both old and new codes resolve.
    """
    global _coords_cache
    if _coords_cache is not None:
        return _coords_cache
    if not os.path.exists(csv_path):
        _download()
    elif _is_stale(csv_path):
        try:
            _download()
        except Exception as exc:
            print(f"WARN: could not refresh airport coords CSV ({exc}); using cached copy", flush=True)

    by_ident, by_gps, by_icao = {}, {}, {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                lat = float(row["latitude_deg"])
                lon = float(row["longitude_deg"])
            except (KeyError, ValueError):
                continue
            ident = row.get("ident", "").strip()
            gps = row.get("gps_code", "").strip()
            icao = row.get("icao_code", "").strip()
            if ident:
                by_ident[ident] = (lat, lon)
            if gps:
                by_gps[gps] = (lat, lon)
            if icao:
                by_icao[icao] = (lat, lon)

    coords = {}
    coords.update(by_ident)
    coords.update(by_gps)
    coords.update(by_icao)
    _coords_cache = coords
    return _coords_cache
