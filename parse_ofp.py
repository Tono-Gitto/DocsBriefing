"""
Parse TG921_OFP.pdf → data/route.json

Each waypoint in route.json:
  { "name": str, "lat": float, "lon": float, "acct_min": int }

acct_min = accumulated time since takeoff in minutes.
First waypoint = 0, last waypoint = OFP stated flight time.
"""

import json
import os
import re
import pdfplumber

OFP_PDF = os.path.join(os.path.dirname(__file__), "Input", "TG921_OFP.pdf")
OUT_JSON = os.path.join(os.path.dirname(__file__), "data", "route.json")

# OFP stated flight time (FLT): 10:21 → 621 minutes
# Read from page 1: "FLT: 1021"
FLIGHT_TIME_MIN = 10 * 60 + 21  # 621

# Lines to discard when collecting route data
_SKIP_RE = re.compile(
    r"^("
    r"OPERATIONAL FLIGHT PLAN PAGE"
    r"|TIME ALT ALT"
    r"|DEPARTURE TERMINAL"
    r"|DESTINATION TERMINAT"
    r"|ENROUTE CHECK"
    r"|RAIM PREDICTION"
    r"|CS:"
    r"|TG \d"
    r"|PLN ID"
    r"|FLX CI"
    r"|\.*\./"         # dot rows like "..../...."
    r")"
)

# Column header lines — signal start of route data but are not data themselves
_COL_HEADER_RE = re.compile(r"^(AWY WPT|RNP NAME FIR|MGA LAT LONG)")

_STOP_RE = re.compile(
    r"^("
    r"DEST INFO"
    r"|ALTERNATE ROUTE SECTION"
    r"|ALTN ATIS"
    r"|ROUTE TO SECONDARY"
    r")"
)

# Lat/lon patterns (CLAUDE.md §1)
_LAT_RE = re.compile(r"^[NS]\d{5}$")
_LON_RE = re.compile(r"^[EW]\d{6}$")
_CONCAT_LATLON_RE = re.compile(r"^([NS]\d{5})([EW]\d{6})$")

# Pure-digit token (for ACCT extraction)
_DIGITS_RE = re.compile(r"^\d+$")


def _decode_lat(s: str) -> float:
    sign = 1 if s[0] == "N" else -1
    deg = int(s[1:3])
    min_x10 = int(s[3:])
    return sign * (deg + min_x10 / 600)


def _decode_lon(s: str) -> float:
    sign = 1 if s[0] == "E" else -1
    deg = int(s[1:4])
    min_x10 = int(s[4:])
    return sign * (deg + min_x10 / 600)


def _get_latlon(row3: str):
    """Return (lat_str, lon_str) or (None, None)."""
    toks = row3.split()
    for j, t in enumerate(toks):
        m = _CONCAT_LATLON_RE.match(t)
        if m:
            return m.group(1), m.group(2)
        if _LAT_RE.match(t) and j + 1 < len(toks) and _LON_RE.match(toks[j + 1]):
            return t, toks[j + 1]
    return None, None


def _get_acct(row2: str):
    """Return accumulated time in minutes, or None."""
    toks = row2.split()
    numeric = [t for t in toks if _DIGITS_RE.match(t)]
    if len(numeric) >= 3:
        acct_tok = numeric[2].zfill(4)
        return int(acct_tok[:2]) * 60 + int(acct_tok[2:])
    return None


def _get_name(row1: str) -> str:
    toks = row1.split()
    return toks[1] if len(toks) >= 2 else toks[0]


def _collect_route_lines() -> list:
    """Extract and filter raw text lines from the OFP primary route section."""
    lines = []
    in_route = False  # True once we've passed the first column header

    with pdfplumber.open(OFP_PDF) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for raw_line in text.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                if _STOP_RE.match(line):
                    return lines
                # Column headers mark start of route data; skip the header lines themselves
                if _COL_HEADER_RE.match(line):
                    in_route = True
                    continue
                if _SKIP_RE.match(line):
                    continue
                if not in_route:
                    continue
                lines.append(line)
    return lines


def parse_ofp() -> list:
    raw = _collect_route_lines()
    # Group into 3-line blocks
    blocks = [raw[i : i + 3] for i in range(0, len(raw) - 2, 3)]

    waypoints = []
    for idx, block in enumerate(blocks):
        if len(block) < 3:
            continue
        row1, row2, row3 = block

        lat_s, lon_s = _get_latlon(row3)
        if lat_s is None:
            continue  # malformed block, skip

        try:
            lat = _decode_lat(lat_s)
            lon = _decode_lon(lon_s)
        except (ValueError, IndexError):
            continue

        # ACCT override: first block = 0, last = flight time
        if idx == 0:
            acct = 0
        elif idx == len(blocks) - 1:
            acct = FLIGHT_TIME_MIN
        else:
            acct = _get_acct(row2)

        name = _get_name(row1)
        waypoints.append({"name": name, "lat": lat, "lon": lon, "acct_min": acct})

    return waypoints


def main():
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    waypoints = parse_ofp()

    if not waypoints:
        print("ERROR: no waypoints extracted")
        return

    # Sanity check: last waypoint ACCT should equal FLIGHT_TIME_MIN
    last = waypoints[-1]
    print(f"Waypoints extracted: {len(waypoints)}")
    print(f"First: {waypoints[0]}")
    print(f"Last:  {last}")
    print(f"Last acct_min={last['acct_min']} (expected {FLIGHT_TIME_MIN}) — {'OK' if last['acct_min'] == FLIGHT_TIME_MIN else 'MISMATCH'}")

    with open(OUT_JSON, "w") as f:
        json.dump(waypoints, f, indent=2)
    print(f"Written {OUT_JSON}")


if __name__ == "__main__":
    main()
