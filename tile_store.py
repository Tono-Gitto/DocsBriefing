"""Server-side OSM tile store for the offline basemap (ADR 0003, slice 2).

Tiles are fetched here, once, and served from our own origin at
/tiles/<z>/<x>/<y>.png. Three reasons this is not left to the browser:

  * the service worker then treats tiles as ordinary same-origin manifest
    entries — no cross-origin cache bucket, no {s} subdomain rotation;
  * OSM's tile usage policy prohibits bulk downloading. One fetch per tile
    ever, shared across every flight and device, with a real User-Agent, is
    a far smaller footprint than every device pulling a full route corridor;
  * a second Bangkok flight costs zero new downloads.

The store lives OUTSIDE runs/ (see TILE_DIR) because the 24 h sweep walks
runs/ — a store inside a run dir would be deleted with it and the sharing
would buy nothing.
"""
import math
import os
import time
import urllib.error
import urllib.request

HERE     = os.path.dirname(os.path.abspath(__file__))
TILE_DIR = os.path.join(HERE, "data", "tiles")

# Identifies this app to OSM's servers, as their usage policy requires.
USER_AGENT = "PreflightBriefingDecoder/1.0 (B777 dispatch briefing tool)"
TILE_URL   = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Cache z0 down to the deepest zoom whose CUMULATIVE tile count stays under
# this. A fixed ceiling is badly calibrated because the bbox needing the most
# zoom depth costs the least to provide it: EDDF-VTBS reaches only z6 within
# budget, but a VTBS-WMKK turnaround — where the crew actually zooms in, and
# whose own fitBounds already sits near z7 — reaches z9 for less storage.
TILE_BUDGET = 400
MAX_ZOOM    = 12   # hard stop; no sector needs more, and it bounds the loop
BBOX_PAD    = 1    # tiles of margin, so panning at the edge isn't instantly blank


def _lon_to_x(lon, z):
    return int((lon + 180.0) / 360.0 * (1 << z))


def _lat_to_y(lat, z):
    # Web Mercator; clamped to the projection's valid latitude range
    lat = max(-85.05112878, min(85.05112878, lat))
    r = math.radians(lat)
    return int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * (1 << z))


def bbox_for_routes(route_files):
    """Union of every waypoint across every leg, as (latmin, latmax, lonmin, lonmax).

    Unions ALL groups' routes: a 3-4 leg upload's two map tabs share one tile
    set, so a group-1-only bbox would leave group 2's map over blank tiles.
    Returns None when no waypoints are found — the caller degrades to no
    basemap rather than fetching the whole world.
    """
    import json

    lats, lons = [], []
    for path in route_files:
        try:
            with open(path) as f:
                for wp in json.load(f):
                    lats.append(wp["lat"])
                    lons.append(wp["lon"])
        except (OSError, ValueError, KeyError, TypeError):
            continue   # a malformed route costs its own leg's tiles, not the run's
    if not lats:
        return None
    return (min(lats), max(lats), min(lons), max(lons))


def _tiles_at_zoom(bbox, z, pad=BBOX_PAD):
    latmin, latmax, lonmin, lonmax = bbox
    n = 1 << z
    x0 = max(0, _lon_to_x(lonmin, z) - pad)
    x1 = min(n - 1, _lon_to_x(lonmax, z) + pad)
    y0 = max(0, _lat_to_y(latmax, z) - pad)   # north edge = smaller y
    y1 = min(n - 1, _lat_to_y(latmin, z) + pad)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def tiles_for_bbox(bbox, budget=TILE_BUDGET, max_zoom=MAX_ZOOM):
    """Every (z, x, y) from z0 down to the deepest zoom that fits the budget.

    Stops at the last zoom whose cumulative count is still within budget, so
    total storage is roughly constant per flight regardless of route length.
    """
    if bbox is None:
        return []
    out = []
    for z in range(0, max_zoom + 1):
        level = _tiles_at_zoom(bbox, z)
        if out and len(out) + len(level) > budget:
            break
        out.extend(level)
    return out


def zoom_ceiling(tiles):
    """Deepest zoom present, for Leaflet's maxNativeZoom (upscale beyond it)."""
    return max((z for z, _, _ in tiles), default=0)


def tile_path(z, x, y, root=None):
    return os.path.join(root or TILE_DIR, str(z), str(x), f"{y}.png")


def fetch_tiles(tiles, root=None, progress=None, pause=0.05):
    """Download any tile not already on disk. Returns (fetched, skipped, failed).

    Never raises: a basemap is a nicety, and a tile failure must never fail the
    briefing. The caller wraps this in its own try/except regardless.
    """
    root = root or TILE_DIR
    fetched = skipped = failed = 0
    for z, x, y in tiles:
        path = tile_path(z, x, y, root)
        if os.path.exists(path):
            skipped += 1
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        req = urllib.request.Request(
            TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            tmp = path + ".part"          # never leave a half-written PNG behind
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            fetched += 1
            time.sleep(pause)             # be a good citizen; not a scraper
        except (urllib.error.URLError, OSError, ValueError):
            failed += 1
        if progress and (fetched + skipped + failed) % 100 == 0:
            progress(f"  tiles: {fetched} fetched, {skipped} cached, {failed} failed")
    return fetched, skipped, failed


def manifest_entries(tiles):
    """Run-manifest paths for these tiles (served at /tiles/..., not /data/...)."""
    return [f"tiles/{z}/{x}/{y}.png" for z, x, y in tiles]
