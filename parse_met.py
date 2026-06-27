"""
Parse TG921_MET.pdf → data/airports.json

Each airport in airports.json:
  { "icao": str, "iata": str, "name": str, "lat": float, "lon": float }
"""

import json
import os
import re
import pdfplumber
from airport_coords import load_coords

MET_PDF = os.path.join(os.path.dirname(__file__), "Input", "TG921_MET.pdf")
OUT_JSON = os.path.join(os.path.dirname(__file__), "data", "airports.json")

# MET airport header: "EDDF -FRA - FRANKFURT"
_HEADER_RE = re.compile(r"^([A-Z]{4})\s+-\s*([A-Z]{3,4})\s+-\s+(.+)")


def parse_met(coords: dict) -> list:
    airports = []
    seen = set()

    with pdfplumber.open(MET_PDF) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for line in text.split("\n"):
                m = _HEADER_RE.match(line.strip())
                if not m:
                    continue
                icao, iata, name = m.group(1), m.group(2), m.group(3).strip()
                if icao in seen:
                    continue
                seen.add(icao)
                if icao not in coords:
                    print(f"  WARN: no coords for {icao} ({name})")
                    continue
                lat, lon = coords[icao]
                airports.append({"icao": icao, "iata": iata, "name": name, "lat": lat, "lon": lon})

    return airports


def main():
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    coords = load_coords()
    airports = parse_met(coords)
    print(f"Airports extracted: {len(airports)}")
    for ap in airports:
        print(f"  {ap['icao']} / {ap['iata']}  {ap['lat']:.3f}, {ap['lon']:.3f}  {ap['name']}")
    with open(OUT_JSON, "w") as f:
        json.dump(airports, f, indent=2)
    print(f"Written {OUT_JSON}")


if __name__ == "__main__":
    main()
