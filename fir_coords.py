"""
Derive FIR centroids from OurAirports ICAO prefix averaging.

For any 4-letter FIR ICAO code (e.g. VTBB, ZGZU, WSJC), we average the
coordinates of all airports whose proper ICAO code shares a 3- or 2-letter
prefix with the FIR code.  This works globally with no external API — it reuses
the airports_raw.csv already downloaded by airport_coords.py.
"""

import csv, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
_CSV  = os.path.join(HERE, "data", "airports_raw.csv")

_cache = None  # [(icao, lat, lon), ...]


def _airports() -> list:
    """Return list of (icao, lat, lon) for every proper 4-letter ICAO airport."""
    global _cache
    if _cache is not None:
        return _cache
    from airport_coords import _download  # reuse download logic
    if not os.path.exists(_CSV):
        _download()
    rows = []
    with open(_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            icao = row.get("icao_code", "").strip()
            if not re.match(r"^[A-Z]{4}$", icao):
                continue
            try:
                rows.append((icao, float(row["latitude_deg"]), float(row["longitude_deg"])))
            except (KeyError, ValueError):
                pass
    _cache = rows
    return _cache


def derive_fir_centroid(fir_code: str, min_airports: int = 3):
    """
    Return (lat, lon) centroid estimate for fir_code, or None if too few airports.

    Strategy: try 3-char prefix first, fall back to 2-char.  Requires at least
    min_airports matching airports to avoid single-point bias.
    """
    airports = _airports()
    for prefix_len in (3, 2):
        prefix = fir_code[:prefix_len]
        matches = [(lat, lon) for icao, lat, lon in airports if icao.startswith(prefix)]
        if len(matches) >= min_airports:
            return (
                round(sum(m[0] for m in matches) / len(matches), 4),
                round(sum(m[1] for m in matches) / len(matches), 4),
            )
    return None
