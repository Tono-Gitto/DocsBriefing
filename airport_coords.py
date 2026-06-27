import csv
import os
import requests

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RAW_CSV = os.path.join(os.path.dirname(__file__), "data", "airports_raw.csv")


def _download():
    os.makedirs(os.path.dirname(RAW_CSV), exist_ok=True)
    print("Downloading OurAirports CSV…", flush=True)
    r = requests.get(OURAIRPORTS_URL, timeout=30)
    r.raise_for_status()
    with open(RAW_CSV, "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"Saved to {RAW_CSV}")


def load_coords(csv_path=RAW_CSV) -> dict:
    """Return {icao: (lat_float, lon_float)} for every airport in the CSV."""
    if not os.path.exists(csv_path):
        _download()
    coords = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            icao = row.get("ident", "").strip()
            try:
                lat = float(row["latitude_deg"])
                lon = float(row["longitude_deg"])
            except (KeyError, ValueError):
                continue
            if icao:
                coords[icao] = (lat, lon)
    return coords
