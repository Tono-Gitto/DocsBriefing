"""
Shared utilities: geographic math, PDF text extraction, model constant.

No project imports — safe to import from any engine module.
"""

import math
import pdfplumber

HAIKU_MODEL = "claude-haiku-4-5-20251001"


def haversine_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def clean_pdf_lines(pdf_path, skip_re):
    """Extract all text lines from a PDF, stripping page-header noise.

    skip_re: compiled regex; lines matching it are dropped.
    """
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.extend(text.split("\n"))
    return [l.strip() for l in lines if l.strip() and not skip_re.match(l.strip())]
