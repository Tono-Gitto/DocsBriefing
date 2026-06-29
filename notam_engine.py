"""
NOTAM engine: parse TG921_NOTAM.pdf → augment data/airports.json with notams[].

Per airport (matched by ICAO):
  - Collect NOTAMs from all AERODROME/ADDITIONAL sections in the NOTAM PDF
  - Triage each NOTAM: Tier 1 (RWY/ILS/navaid) / Tier 2 (TWY/stand) / Tier 3 (admin)
  - Filter by validity window at reference time (outer window only per CLAUDE.md §3)
  - NOTAMs with no validity window are always shown (conservative)

Also parses ENROUTE INFORMATION section:
  - FIR/UIR NOTAMs collected per FIR header (ICAO / FIR-NAME format)
  - UNTIL-style windows (body-level) parsed alongside standard starred windows
  - Output: data/fir_notams.json with markers at nearest route waypoint

Attribution: airport is determined by section header (ICAO / IATA) not NOTAM ID prefix.

Run AFTER met_engine.py so airports.json already has weather + ref_time fields.
"""

import json, math, os, re
from datetime import datetime, timedelta, timezone
import anthropic
import pdfplumber

HERE = os.path.dirname(os.path.abspath(__file__))
NOTAM_PDF     = os.path.join(HERE, "Input", "TG921_NOTAM.pdf")
AIRPORTS_JSON = os.path.join(HERE, "data", "airports.json")
FIR_JSON      = os.path.join(HERE, "data", "fir_notams.json")
ROUTE_JSON    = os.path.join(HERE, "data", "route.json")

# ETD 1245Z + 20 min taxi = takeoff 1305Z on 20 JUN 2026
TAKEOFF_UTC = datetime(2026, 6, 20, 13, 5, tzinfo=timezone.utc)

# ── PDF line extraction ───────────────────────────────────────────────────────

_PAGE_HDR_RE = re.compile(
    r"^(Official Pilot Briefing|Trans ID:|Creation Time:|"
    r"THA\d{3}\s+\d{2}[A-Z]{3}\d{2}\s+[A-Z]+\s+[A-Z0-9]+)"
)

def _get_clean_lines(pdf_path):
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.extend(text.split("\n"))
    return [l.strip() for l in lines if l.strip() and not _PAGE_HDR_RE.match(l.strip())]


# ── Pattern constants ─────────────────────────────────────────────────────────

# 4-letter ICAO / 3-4-letter IATA — airport section header
_AP_HDR_RE = re.compile(r"^([A-Z]{4})\s*/\s*([A-Z]{3,4})\s*$")

# 4-letter ICAO / FIR or UIR name — enroute section header
# Requiring FIR/UIR in the name excludes "DVOR / DME STJ..." artifacts and "DEU / GERMANY" labels
_FIR_HDR_RE = re.compile(r"^([A-Z]{4})\s*/\s*(.+(?:FIR|UIR).*)\s*$")

# Main section headers — reset current airport / FIR context
_MAIN_SECT_RE = re.compile(
    r"^(GENERAL|FLIGHT LEG|AERODROME|ENROUTE|ADDITIONAL|AEROPLANE)\s+INFORMATION\s*$"
)

# NOTAM ID: optional leading *, then series/year (date issued in parens)
# Matches: EDDZA3149/26 (18 JUN 26)  *VTBDC3295/26 (18 JUN 26)
#          THA 00159/26 (18 JUN 26)   // (COM-INFO) // THA 00064/25 (18 JUN 26)
_NOTAM_ID_RE = re.compile(
    r"^\*?(?:/{2}\s*\([^)]+\)\s*/{2}\s*)?"  # optional // (COM-INFO) // prefix
    r"(.+?/\d{2,4})\s+\(\d{2}\s+[A-Z]{3}\s+\d{2}\)\s*$"
)

# Validity window line: *DD MMM YYYY HH:MM-DD MMM YYYY HH:MM*
_WINDOW_RE = re.compile(
    r"^\*(\d{2}\s+[A-Z]{3}\s+\d{4}\s+\d{2}:\d{2})"
    r"-(\d{2}\s+[A-Z]{3}\s+\d{4}\s+\d{2}:\d{2})\*\s*$"
)
_WINDOW_FMT = "%d %b %Y %H:%M"

# UNTIL-style validity window in FIR NOTAM body lines (appears AFTER body text):
# "16 JUN 26 05:43 UNTIL 16 SEP 26 23:59 ESTIMATED"
_UNTIL_RE = re.compile(
    r"^(\d{2}\s+[A-Z]{3}\s+\d{2}\s+\d{2}:\d{2})\s+UNTIL\s+"
    r"(\d{2}\s+[A-Z]{3}\s+\d{2}\s+\d{2}:\d{2})"
)
_UNTIL_FMT = "%d %b %y %H:%M"

# Lines to skip that look like NOTAM IDs but aren't
_SKIP_BODY_RE = re.compile(r"^//\s*\(SEE ATTCH\)\s*//$")


# ── Tier classification ───────────────────────────────────────────────────────

_T1_LINE_RE = re.compile(
    r"(\bRWY\b\s+\S+\s+CLSD\b"
    r"|\bRUNWAY\b.+\bCLSD\b"
    r"|\bILS\b.+\b(U/S|OUT OF SERVICE|DOWNGRADED|SUSPENDED|UNSERVICEABLE)\b"
    r"|\bDME\b.+\b(U/S|SUSPENDED|MAINT|DO NOT USE|UNSERVICEABLE)\b"
    r"|\bVOR\b.+\b(U/S|SUSPENDED|MAINT|UNSERVICEABLE)\b"
    r"|\bNDB\b.+\b(U/S|SUSPENDED|UNSERVICEABLE)\b"
    r"|\bLPV\b.+\b(SUSPENDED|U/S)\b"
    r"|\bAPCH\b.+\b(U/S|SUSPENDED)\b"
    r"|\bLOC\b.+\b(U/S|SUSPENDED|UNSERVICEABLE)\b"
    r"|\bFALSE INDICATION\b"
    r"|\bOUT OF SERVICE\b"
    r"|\bRESTRICTED AREA\b.+\bACTIVE\b"
    r"|\bDANGER AREA\b.+\bACTIVE\b"
    r"|\bROUTE\b.+\bNOT AVBL\b"
    r")",
    re.IGNORECASE,
)

_T2_LINE_RE = re.compile(
    r"(\bTWY\b\s+\S+\s+CLSD\b"
    r"|\bTWY\b\s+CLSD\b"
    r"|\bTAXIWAY\b.+\bCLSD\b"
    r"|\bACFT STAND\b.+\bCLSD\b"
    r"|\bSTAND\b.+\bCLSD\b"
    r"|\bVDGS\b.+\b(U/S|UNSERVICEABLE|MAINT)\b"
    r"|\bRETIL\b.+\b(U/S|UNSERVICEABLE)\b"
    r"|\bPAPI\b.+\b(U/S|UNSERVICEABLE)\b"
    r"|\bPALS\b.+\b(U/S|UNSERVICEABLE|MAINT)\b"
    r"|\bSALS\b.+\b(U/S|UNSERVICEABLE|MAINT)\b"
    r"|\bTHR IDENTIFICATION LIGHTS\b.+\bU/S\b"
    r"|\bA-VDGS\b.+\b(U/S|UNSERVICEABLE)\b"
    r"|\bDVOR/DME\b.+\b(SUSPENDED|U/S)\b"
    r"|\bMSSR\b.+\b(U/S|UNSERVICEABLE)\b"
    r"|\bRADAR\b.+\b(U/S|UNRELIABLE)\b"
    r"|\bGROUP\s+[BC]\b.+\bAERODROME\b"
    r"|\bAERODROME\b.+\bGROUP\s+[BC]\b"
    r")",
    re.IGNORECASE,
)

_T1_FIR_RE = re.compile(
    r"(\bROUTE\b.+\bNOT AVBL\b"
    r")",
    re.IGNORECASE,
)

_T2_FIR_RE = re.compile(
    r"(\bVOR\b.+\b(U/S|SUSPENDED|MAINT|UNSERVICEABLE)\b"
    r"|\bDME\b.+\b(U/S|SUSPENDED|MAINT|DO NOT USE|UNSERVICEABLE)\b"
    r"|\bDVOR/DME\b.+\b(SUSPENDED|U/S)\b"
    r"|\bNDB\b.+\b(U/S|SUSPENDED|UNSERVICEABLE)\b"
    r"|\bROUTE\b.+\b(AVBL|AVAILABLE)\b"
    r")",
    re.IGNORECASE,
)

def _classify_tier(body_lines, is_fir=False):
    """Return 1, 2, or 3. Checks joined body so multi-line NOTAMs aren't penalised.
    FIR NOTAMs use stricter T1 criteria (airspace-critical only); T2 for navaid outages."""
    full = " ".join(body_lines)
    if is_fir:
        if _T1_FIR_RE.search(full): return 1
        if _T2_FIR_RE.search(full): return 2
        return 3
    if _T1_LINE_RE.search(full):
        return 1
    if _T2_LINE_RE.search(full):
        return 2
    return 3


# ── Validity filter ───────────────────────────────────────────────────────────

def _parse_window(line):
    m = _WINDOW_RE.match(line)
    if not m:
        return None, None
    try:
        start = datetime.strptime(m.group(1).strip(), _WINDOW_FMT).replace(tzinfo=timezone.utc)
        end   = datetime.strptime(m.group(2).strip(), _WINDOW_FMT).replace(tzinfo=timezone.utc)
        return start, end
    except ValueError:
        return None, None


def _is_active(win_start, win_end, ref_dt):
    """True if ref_dt falls within [win_start, win_end], or if no window given."""
    if win_start is None:
        return True
    return win_start <= ref_dt <= win_end


# ── FIR reference-time engine ─────────────────────────────────────────────────

# Approximate FIR centroids (lat, lon) — used as haversine search targets only;
# markers are placed on the nearest route waypoint, not at these coordinates.
# Covers worldwide corridors relevant to Thai Airways operations.
FIR_COORDS = {
    # ── Europe ──────────────────────────────────────────────────────────────
    "EBBU": (50.5,   4.5),  # Brussels UIR (Belgium)
    "EDMM": (48.0,  12.5),  # Munich FIR (Germany S)
    "EDGG": (50.5,   9.0),  # Langen FIR (Germany C)
    "EDWW": (53.5,   9.5),  # Bremen FIR (Germany N)
    "EDUU": (51.0,  10.0),  # Rhein UIR (Germany)
    "EGPX": (57.0,  -4.0),  # Scottish FIR
    "EGTT": (52.0,  -1.0),  # London FIR
    "EHAA": (52.5,   5.5),  # Amsterdam FIR (Netherlands)
    "EKDK": (55.5,  12.0),  # Copenhagen FIR (Denmark)
    "ENOR": (65.0,  14.0),  # Oslo FIR (Norway)
    "EPWW": (52.0,  19.0),  # Warsaw FIR (Poland)
    "ESAA": (59.0,  16.0),  # Sweden FIR
    "EYVL": (55.5,  24.0),  # Vilnius FIR (Lithuania)
    "EERR": (57.0,  25.0),  # Riga FIR (Latvia)
    "EETT": (59.5,  25.0),  # Tallinn FIR (Estonia)
    "EFIN": (64.0,  27.0),  # Helsinki FIR (Finland)
    "LAAA": (41.0,  20.0),  # Tirana FIR (Albania)
    "LDZO": (45.5,  16.0),  # Zagreb FIR (Croatia)
    "LECB": (41.0,   1.0),  # Barcelona FIR (Spain NE)
    "LECM": (40.0,  -4.0),  # Madrid FIR (Spain)
    "LFFF": (47.0,   2.0),  # Paris FIR (France)
    "LFRR": (47.5,  -4.0),  # Brest Oceanic FIR
    "LGGG": (37.5,  22.5),  # Athens FIR (Greece)
    "LHCC": (47.0,  19.5),  # Budapest FIR (Hungary)
    "LIMM": (45.0,  10.0),  # Milan FIR (Italy N)
    "LIRR": (41.5,  12.5),  # Rome FIR (Italy S)
    "LJLA": (46.0,  14.5),  # Ljubljana FIR (Slovenia)
    "LKAA": (50.0,  15.0),  # Prague FIR (Czech Republic)
    "LOVV": (47.5,  15.5),  # Vienna FIR (Austria)
    "LPPC": (38.5,  -9.0),  # Lisbon FIR (Portugal)
    "LRBB": (45.0,  24.5),  # Bucharest FIR (Romania)
    "LSAS": (47.0,   8.5),  # Switzerland FIR
    "LBSR": (42.5,  25.5),  # Sofia FIR (Bulgaria)
    "LYBA": (44.0,  20.0),  # Belgrade FIR (Serbia)
    "LWSS": (41.5,  21.5),  # Skopje FIR (North Macedonia)
    "LCCC": (35.0,  33.0),  # Nicosia FIR (Cyprus)
    "LZBB": (48.5,  19.0),  # Bratislava FIR (Slovakia)
    "LTAA": (39.0,  37.0),  # Ankara FIR (Turkey E)
    "LTBB": (41.0,  33.0),  # Istanbul FIR (Turkey W)
    # ── North Atlantic ───────────────────────────────────────────────────────
    "BIRD": (65.0, -18.0),  # Reykjavik Oceanic FIR
    "EGGX": (54.0, -20.0),  # Shanwick Oceanic FIR
    "LPPO": (39.0, -27.0),  # Santa Maria Oceanic FIR (Azores)
    # ── Caucasus / Central Asia ─────────────────────────────────────────────
    "UGGG": (42.0,  43.5),  # Tbilisi FIR (Georgia)
    "UDDD": (40.0,  44.5),  # Yerevan FIR (Armenia)
    "UBBA": (40.5,  49.0),  # Baku FIR (Azerbaijan)
    "UTSS": (41.5,  64.0),  # Tashkent UIR (Uzbekistan)
    "UTDD": (38.5,  68.5),  # Dushanbe FIR (Tajikistan)
    "UTAK": (40.0,  53.5),  # Turkmenbashi FIR (Turkmenistan W)
    "UTAA": (38.0,  58.0),  # Ashgabat FIR (Turkmenistan)
    "UTAV": (38.5,  63.0),  # Turkmenabat FIR (Turkmenistan E)
    "UCFM": (42.5,  74.5),  # Bishkek FIR (Kyrgyzstan)
    "UAAA": (43.5,  77.0),  # Almaty FIR (Kazakhstan SE)
    "UATT": (50.0,  57.0),  # Aktobe FIR (Kazakhstan W)
    # ── Russia ──────────────────────────────────────────────────────────────
    "ULLL": (60.0,  30.0),  # St Petersburg FIR
    "UUWV": (55.8,  37.5),  # Moscow FIR
    "URRR": (47.0,  40.0),  # Rostov FIR
    "UWWW": (53.0,  50.0),  # Samara FIR
    "USSS": (57.0,  60.5),  # Yekaterinburg FIR
    "UNNT": (55.0,  82.5),  # Novosibirsk FIR
    "UNKL": (56.0,  93.0),  # Krasnoyarsk FIR
    "UIIX": (52.5, 104.0),  # Irkutsk FIR
    "UHHH": (48.5, 135.0),  # Khabarovsk FIR
    "UHWW": (43.0, 132.0),  # Vladivostok FIR
    "UHPP": (53.0, 158.5),  # Petropavlovsk-Kamchatsky FIR
    # ── Middle East ─────────────────────────────────────────────────────────
    "OAKX": (34.5,  67.5),  # Kabul FIR (Afghanistan)
    "OBBB": (26.0,  50.5),  # Bahrain FIR
    "OEDF": (24.0,  45.0),  # Riyadh FIR (Saudi Arabia)
    "OIIX": (35.0,  52.0),  # Tehran FIR (Iran)
    "OJAC": (31.5,  36.5),  # Amman FIR (Jordan)
    "OKAC": (29.0,  47.5),  # Kuwait FIR
    "OLBB": (33.5,  35.5),  # Beirut FIR (Lebanon)
    "OMAE": (24.5,  55.0),  # Emirates FIR (UAE)
    "OOMM": (23.0,  58.0),  # Muscat FIR (Oman)
    "ORBB": (33.0,  44.0),  # Baghdad FIR (Iraq)
    "OSTT": (35.0,  38.0),  # Damascus FIR (Syria)
    "OYSN": (15.5,  44.5),  # Sana'a FIR (Yemen)
    "LLLL": (31.5,  34.5),  # Tel Aviv FIR (Israel)
    # ── Africa ──────────────────────────────────────────────────────────────
    "HECA": (26.5,  30.0),  # Cairo FIR (Egypt)
    "HLLL": (27.0,  14.0),  # Tripoli FIR (Libya)
    "DTTC": (33.5,   9.0),  # Tunis FIR (Tunisia)
    "GMMM": (32.0,  -5.0),  # Casablanca FIR (Morocco)
    "GOOO": (14.7, -17.5),  # Dakar FIR (Senegal)
    "DIAP": ( 5.5,  -4.0),  # Abidjan FIR (Côte d'Ivoire)
    "DNKK": (10.0,   8.0),  # Kano FIR (Nigeria)
    "HSSS": (15.0,  32.0),  # Khartoum FIR (Sudan)
    "HAAA": ( 9.0,  40.0),  # Addis Ababa FIR (Ethiopia)
    "HKNA": ( 1.0,  38.0),  # Nairobi FIR (Kenya)
    "FAJA": (-26.0, 28.0),  # Johannesburg FIR (South Africa)
    # ── South Asia ──────────────────────────────────────────────────────────
    "OPLR": (30.0,  72.0),  # Lahore FIR (Pakistan N)
    "OPKR": (25.0,  67.0),  # Karachi FIR (Pakistan S)
    "VIDF": (27.5,  79.0),  # Delhi FIR (India N)
    "VABF": (19.0,  72.8),  # Mumbai FIR (India W)
    "VANF": (21.0,  78.0),  # Nagpur FIR (India C)
    "VECF": (23.0,  88.0),  # Kolkata FIR (India E)
    "VOMF": (13.0,  80.0),  # Chennai FIR (India SE)
    "VCCC": ( 7.5,  80.5),  # Colombo FIR (Sri Lanka)
    "VGZR": (23.8,  90.4),  # Dhaka FIR (Bangladesh)
    "VNKT": (28.0,  84.0),  # Kathmandu FIR (Nepal)
    # ── Southeast Asia ──────────────────────────────────────────────────────
    "VYYF": (19.5,  96.5),  # Yangon FIR (Myanmar)
    "VTBB": (15.0, 100.5),  # Bangkok FIR (Thailand)
    "VLVT": (17.5, 103.0),  # Vientiane FIR (Laos)
    "VDPF": (12.5, 105.0),  # Phnom Penh FIR (Cambodia)
    "VVHM": (11.5, 107.5),  # Ho Chi Minh FIR (Vietnam S)
    "VVTS": (21.0, 105.8),  # Hanoi FIR (Vietnam N)
    "WMFC": ( 4.0, 109.0),  # Kuala Lumpur FIR (Malaysia W)
    "WBFC": ( 5.0, 115.5),  # Kota Kinabalu FIR (Malaysia E / Brunei)
    "WSJC": ( 1.4, 104.0),  # Singapore FIR
    "WAAF": (-6.5, 108.0),  # Jakarta FIR (Indonesia W)
    "WIIF": (-2.5, 117.0),  # Ujung Pandang FIR (Indonesia E)
    "RPHI": (13.0, 122.0),  # Manila FIR (Philippines)
    "RPMM": (13.0, 132.0),  # Manila Oceanic FIR
    # ── China ───────────────────────────────────────────────────────────────
    "ZWUQ": (43.5,  87.5),  # Urumqi FIR (Xinjiang)
    "ZLHW": (36.0, 103.5),  # Lanzhou FIR (Gansu / NW China)
    "ZPKM": (25.0, 102.7),  # Kunming FIR (Yunnan)
    "ZUUU": (30.0, 104.0),  # Chengdu FIR (Sichuan)
    "ZHWH": (30.5, 114.0),  # Wuhan FIR (Hubei / central China)
    "ZGZU": (23.5, 113.5),  # Guangzhou FIR (Guangdong / S China)
    "ZSHA": (31.0, 118.0),  # Shanghai FIR (E China)
    "ZBPE": (40.0, 116.5),  # Beijing FIR (N China)
    "ZYSH": (41.5, 123.0),  # Shenyang FIR (NE China)
    "VHHK": (22.3, 114.2),  # Hong Kong FIR
    "RCAA": (25.0, 121.5),  # Taipei FIR (Taiwan)
    # ── Northeast Asia ──────────────────────────────────────────────────────
    "RKRR": (37.5, 127.5),  # Incheon FIR (South Korea)
    "RJTG": (33.0, 131.0),  # Fukuoka FIR (Japan SW)
    "RJJJ": (43.0, 141.0),  # Sapporo FIR (Japan N)
    # ── Pacific Oceanic ─────────────────────────────────────────────────────
    "KZAK": (35.0,-150.0),  # Oakland Oceanic FIR (N Pacific)
    "NZZO": (-30.0,175.0),  # Auckland Oceanic FIR (NZ)
    "NFFF": (-18.0,178.0),  # Nadi FIR (Fiji / SW Pacific)
    "NTTT": (-22.0,-149.0), # Tahiti FIR (French Polynesia)
    # ── Australia ───────────────────────────────────────────────────────────
    "YBBB": (-23.0, 149.0), # Brisbane FIR (Australia E)
    "YMMM": (-25.0, 134.0), # Melbourne FIR (Australia)
    # ── Americas ────────────────────────────────────────────────────────────
    "CZVR": (49.0,-123.0),  # Vancouver FIR (Canada BC)
    "CZEG": (51.0,-114.0),  # Edmonton FIR (Canada Alberta)
    "CZYZ": (44.0, -79.0),  # Toronto FIR (Canada E)
    "CZUL": (45.5, -73.8),  # Montreal FIR (Canada QC)
    "KZSE": (47.5,-122.3),  # Seattle ARTCC (USA NW)
    "KZLA": (34.0,-118.0),  # Los Angeles ARTCC (USA W)
    "KZOA": (37.5,-121.5),  # Oakland ARTCC (USA W coast)
    "KZLC": (41.0,-112.0),  # Salt Lake City ARTCC (USA W)
    "KZNY": (41.0, -74.0),  # New York ARTCC (USA NE)
    "KZDC": (38.5, -77.0),  # Washington ARTCC (USA E)
}


def _haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _nearest_waypoint(lat, lon, route_pts):
    """Return (ref_dt, dist_nm, wp_lat, wp_lon) via haversine nearest-waypoint search."""
    best_dist = float("inf")
    best_acct, best_lat, best_lon = 0, lat, lon
    for pt in route_pts:
        d = _haversine_nm(lat, lon, pt["lat"], pt["lon"])
        if d < best_dist:
            best_dist = d
            best_acct = pt["acct_min"]
            best_lat, best_lon = pt["lat"], pt["lon"]
    return TAKEOFF_UTC + timedelta(minutes=best_acct), best_dist, best_lat, best_lon


# ── NOTAM PDF parser ──────────────────────────────────────────────────────────

_GENERAL_SECTIONS = {"GENERAL", "FLIGHT LEG", "AEROPLANE"}


def parse_notam_pdf(pdf_path):
    """
    Return:
      result:       {icao: [notam_dict, ...]}                           — airport sections
      fir_result:   {fir_icao: {"name": str, "notams": [notam_dict]}}  — ENROUTE section
      general_result: {"GENERAL": [...], "FLIGHT LEG": [...], "AEROPLANE": [...]}

    Each notam_dict: {id, tier, body, win_start, win_end}
    """
    clean = _get_clean_lines(pdf_path)
    result         = {}
    fir_result     = {}
    general_result = {"GENERAL": [], "FLIGHT LEG": [], "AEROPLANE": []}
    current_ap      = None
    current_fir     = None
    current_section = ""

    cur_id        = None
    cur_body      = []
    cur_win_s     = None
    cur_win_e     = None
    cur_is_ci     = False
    cur_see_attch = False

    def flush():
        nonlocal cur_id, cur_body, cur_win_s, cur_win_e, cur_is_ci, cur_see_attch
        if cur_id and not cur_see_attch:
            body_lines = [l for l in cur_body if l]
            is_fir = current_section == "ENROUTE"
            tier = _classify_tier(body_lines, is_fir=is_fir)
            notam = {
                "id":        cur_id,
                "tier":      tier,
                "body":      "\n".join(body_lines),
                "win_start": cur_win_s,
                "win_end":   cur_win_e,
            }
            if current_section == "ENROUTE" and current_fir:
                fir_result[current_fir]["notams"].append(notam)
            elif current_section in ("AERODROME", "ADDITIONAL") and current_ap:
                result.setdefault(current_ap, []).append(notam)
            elif current_section in _GENERAL_SECTIONS:
                general_result[current_section].append(notam)
        cur_id        = None
        cur_body      = []
        cur_win_s     = None
        cur_win_e     = None
        cur_is_ci     = False
        cur_see_attch = False

    expecting_window = False

    for line in clean:
        # ── Main section header → reset context
        m_sect = _MAIN_SECT_RE.match(line)
        if m_sect:
            flush()
            current_section = m_sect.group(1)
            current_ap  = None
            current_fir = None
            expecting_window = False
            continue

        # ── Sub-section header: FIR in ENROUTE, airport in AERODROME/ADDITIONAL
        if current_section == "ENROUTE":
            m = _FIR_HDR_RE.match(line)
            if m:
                flush()
                current_fir = m.group(1)
                name = m.group(2).strip()
                fir_result.setdefault(current_fir, {"name": name, "notams": []})
                fir_result[current_fir]["name"] = name
                expecting_window = False
                continue
        elif current_section in ("AERODROME", "ADDITIONAL"):
            m = _AP_HDR_RE.match(line)
            if m:
                flush()
                current_ap = m.group(1)
                expecting_window = False
                continue

        # ── Starred validity window (immediately after NOTAM ID line)
        if expecting_window:
            ws, we = _parse_window(line)
            if ws is not None:
                cur_win_s, cur_win_e = ws, we
                expecting_window = False
                continue
            else:
                expecting_window = False

        # ── NOTAM ID line
        m_id = _NOTAM_ID_RE.match(line)
        if m_id:
            flush()
            cur_id    = m_id.group(1).strip()
            cur_is_ci = bool(re.search(r"COM.INFO", line, re.IGNORECASE))
            expecting_window = True
            continue

        # ── Skip if no active context
        if cur_id is None:
            continue
        if current_section == "ENROUTE" and current_fir is None:
            continue
        if current_section in ("AERODROME", "ADDITIONAL") and current_ap is None:
            continue
        # GENERAL / FLIGHT LEG / AEROPLANE: no sub-header required — accumulate directly

        # ── See-attachment marker → discard NOTAM body
        if _SKIP_BODY_RE.match(line):
            cur_see_attch = True
            continue

        # ── UNTIL-style window in body line (FIR NOTAMs; strip from displayed body)
        if current_section == "ENROUTE" and cur_win_s is None:
            m_until = _UNTIL_RE.match(line)
            if m_until:
                try:
                    cur_win_s = datetime.strptime(m_until.group(1).strip(), _UNTIL_FMT).replace(tzinfo=timezone.utc)
                    cur_win_e = datetime.strptime(m_until.group(2).strip(), _UNTIL_FMT).replace(tzinfo=timezone.utc)
                except ValueError:
                    cur_body.append(line)
                continue  # don't add UNTIL line to body regardless

        cur_body.append(line)

    flush()
    return result, fir_result, general_result


# ── NOTAM summarisation ───────────────────────────────────────────────────────

_SUMMARIZE_SYSTEM = (
    "You are summarizing aviation NOTAMs for flight crew. "
    "For each numbered NOTAM body, write ONE sentence (max 20 words) "
    "describing the operational impact. "
    "Keep all aviation abbreviations as-is (RWY, TWY, ILS, LOC, GP, DME, NDB, THR, "
    "APCH, CLSD, U/S, WIP, AVBL, MAINT, BTN, AGL, etc.) — do not expand them. "
    "For drone/UAS NOTAMs with coordinate polygons, state the area in general terms. "
    "Reply with only the numbered list — no headers, no explanations."
)

def _call_summarize_batch(client, items):
    """items: [(icao, id, body), ...]. Returns {(icao,id): summary}."""
    lines = [f"{i+1}. [{icao} {nid}]\n{body}" for i, (icao, nid, body) in enumerate(items)]
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=_SUMMARIZE_SYSTEM,
        messages=[{"role": "user", "content": "\n\n".join(lines)}],
    )
    result = {}
    for line in msg.content[0].text.strip().splitlines():
        m = re.match(r"^(\d+)\.\s+(.+)$", line.strip())
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(items):
                icao, nid, _ = items[idx]
                result[(icao, nid)] = m.group(2).strip()
    return result


def _summarize_notams(notams_by_key, batch_size=25):
    """Batch-call Claude to produce one-sentence summaries. Key can be ICAO or FIR code."""
    items = []
    for key, notams in notams_by_key.items():
        for n in notams:
            items.append((key, n["id"], n["body"]))
    if not items:
        return {}

    client = anthropic.Anthropic()
    result = {}
    for start in range(0, len(items), batch_size):
        batch = items[start: start + batch_size]
        print(f"  Summarising NOTAMs {start+1}–{start+len(batch)} of {len(items)}…")
        result.update(_call_summarize_batch(client, batch))
    return result


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    with open(AIRPORTS_JSON) as f:
        airports = json.load(f)
    with open(ROUTE_JSON) as f:
        route_pts = json.load(f)

    notam_db, fir_db, general_db = parse_notam_pdf(NOTAM_PDF)

    total_raw = sum(len(v) for v in notam_db.values())
    total_fir = sum(len(v["notams"]) for v in fir_db.values())
    total_gen = sum(len(v) for v in general_db.values())
    print(f"Raw NOTAMs parsed: {total_raw} airport ({len(notam_db)} airports),  "
          f"{total_fir} FIR ({len(fir_db)} FIRs),  "
          f"{total_gen} general ({', '.join(f'{k}:{len(v)}' for k,v in general_db.items() if v)})")

    # ── Airport NOTAMs ────────────────────────────────────────────────────────
    matched = 0
    for ap in airports:
        icao = ap["icao"]
        notams_raw = notam_db.get(icao, [])

        ref_str = ap.get("ref_time", "0000Z")
        hhmm = ref_str.rstrip("Z")
        d = TAKEOFF_UTC.date()
        ref_dt = datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=timezone.utc)

        active = [
            {
                "id":   n["id"],
                "tier": n["tier"],
                "body": n["body"],
                "window": (
                    n["win_start"].strftime("%-d %b %Y %H:%MZ")
                    + " – "
                    + n["win_end"].strftime("%-d %b %Y %H:%MZ")
                ) if n["win_start"] else None,
            }
            for n in notams_raw
            if _is_active(n["win_start"], n["win_end"], ref_dt)
        ]
        active.sort(key=lambda x: x["tier"])
        ap["notams"] = active
        ap["notam_covered"] = icao in notam_db
        if active:
            matched += 1

    print("Generating airport NOTAM summaries…")
    summaries = _summarize_notams({ap["icao"]: ap["notams"] for ap in airports if ap.get("notams")})
    for ap in airports:
        for n in ap.get("notams", []):
            n["summary"] = summaries.get((ap["icao"], n["id"]), n["body"].split("\n")[0])

    with open(AIRPORTS_JSON, "w") as f:
        json.dump(airports, f, indent=2)
    print(f"Airports with active NOTAMs: {matched}/{len(airports)}")

    # ── FIR NOTAMs ────────────────────────────────────────────────────────────
    fir_out = []
    fir_ref_times = []

    for fir_icao, fir_data in fir_db.items():
        coords = FIR_COORDS.get(fir_icao)
        if coords is None:
            print(f"  WARN: no centroid for FIR {fir_icao} ({fir_data['name']}) — skipping")
            continue

        ref_dt, dist_nm, wp_lat, wp_lon = _nearest_waypoint(coords[0], coords[1], route_pts)

        active_fir = [
            {
                "id":   n["id"],
                "tier": n["tier"],
                "body": n["body"],
                "window": (
                    n["win_start"].strftime("%-d %b %Y %H:%MZ")
                    + " – "
                    + n["win_end"].strftime("%-d %b %Y %H:%MZ")
                ) if n["win_start"] else None,
            }
            for n in fir_data["notams"]
            if _is_active(n["win_start"], n["win_end"], ref_dt)
        ]
        active_fir.sort(key=lambda x: x["tier"])

        fir_ref_times.append((fir_icao, ref_dt))
        fir_out.append({
            "fir":      fir_icao,
            "name":     fir_data["name"],
            "lat":      round(wp_lat, 4),
            "lon":      round(wp_lon, 4),
            "ref_time": ref_dt.strftime("%H%MZ"),
            "notams":   active_fir,
        })

    print("Generating FIR NOTAM summaries…")
    fir_summaries = _summarize_notams({e["fir"]: e["notams"] for e in fir_out if e["notams"]})
    for entry in fir_out:
        for n in entry["notams"]:
            n["summary"] = fir_summaries.get((entry["fir"], n["id"]), n["body"].split("\n")[0])

    os.makedirs(os.path.dirname(FIR_JSON), exist_ok=True)
    with open(FIR_JSON, "w") as f:
        json.dump(fir_out, f, indent=2)
    print(f"Written {len(fir_out)} FIRs to {FIR_JSON}")

    # Sanity check: FIR ref_times should emerge in route order
    fir_ref_times.sort(key=lambda x: x[1])
    print("\nFIR ref_times (should be roughly EDGG → VTBB order):")
    for fir_icao, ref_dt in fir_ref_times:
        n_active = next((len(e["notams"]) for e in fir_out if e["fir"] == fir_icao), 0)
        name = fir_db[fir_icao]["name"]
        print(f"  {fir_icao:4s}  {name:25s}  {ref_dt.strftime('%H%MZ')}  ({n_active} active)")

    # ── Validation ────────────────────────────────────────────────────────────
    print("\nValidation (EDDF @ 1305Z, VTBS @ 2326Z):")

    eddf_notams = notam_db.get("EDDF", [])
    vtbs_notams = notam_db.get("VTBS", [])
    ref_eddf = datetime(2026, 6, 20, 13, 5, tzinfo=timezone.utc)

    for nid in ["EDDZA3081/26", "EDDZA3082/26", "EDDZA3053/26"]:
        n = next((x for x in eddf_notams if nid in x["id"]), None)
        if n:
            active = _is_active(n["win_start"], n["win_end"], ref_eddf)
            print(f"  EDDF {nid}: active={active} ({'✓ NOT active' if not active else '✗ SHOULD be inactive'})")
        else:
            print(f"  EDDF {nid}: NOT FOUND ✗")

    for kw in ["EDDZA2715/26", "EDDZA2716/26", "EDDZA2717/26"]:
        n = next((x for x in eddf_notams if kw in x["id"]), None)
        if n:
            active = _is_active(n["win_start"], n["win_end"], ref_eddf)
            no_win = n["win_start"] is None
            print(f"  EDDF {kw}: tier={n['tier']} no_window={no_win} active={active} "
                  f"({'✓ correctly hidden (permanent)' if not active and no_win else '✗'})")
        else:
            print(f"  EDDF {kw}: NOT FOUND ✗")

    ref_vtbs = datetime(2026, 6, 20, 23, 26, tzinfo=timezone.utc)
    for nid, expect_active in [("VTBDC3295/26", True), ("VTBDC3296/26", False)]:
        n = next((x for x in vtbs_notams if nid in x["id"]), None)
        if n:
            active = _is_active(n["win_start"], n["win_end"], ref_vtbs)
            print(f"  VTBS {nid}: active={active} ({'✓' if active == expect_active else '✗ WRONG'})")
        else:
            print(f"  VTBS {nid}: NOT FOUND ✗")

    def _active_list(icao):
        ap = next((a for a in airports if a["icao"] == icao), None)
        return ap["notams"] if ap else []

    print(f"\n  EDDF active NOTAMs ({len(_active_list('EDDF'))}):")
    for n in _active_list("EDDF"):
        print(f"    T{n['tier']} {n['id']}: {n['body'][:60].replace(chr(10), ' ')}")

    print(f"\n  VTBS active NOTAMs ({len(_active_list('VTBS'))}):")
    for n in _active_list("VTBS"):
        print(f"    T{n['tier']} {n['id']}: {n['body'][:60].replace(chr(10), ' ')}")

    # FIR window correctness: EDDZB0483/26 (Muenster NDB U/S)
    print("\nFIR window check (EDDZB0483/26 — Muenster NDB):")
    muenster = next(
        (n for n in fir_db.get("EDGG", {}).get("notams", []) if "EDDZB0483" in n["id"]),
        None,
    )
    if muenster:
        has_win = muenster["win_start"] is not None
        body_clean = "UNTIL" not in muenster["body"].upper()
        print(f"  win_start={muenster['win_start']}  body_has_UNTIL={not body_clean}")
        print(f"  {'✓' if has_win and body_clean else '✗'} window parsed, UNTIL stripped from body")
    else:
        print("  EDDZB0483/26 not found in EDGG FIR NOTAMs ✗")


if __name__ == "__main__":
    main()
