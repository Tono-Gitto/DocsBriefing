"""Self-contained offline briefing bundle (ADR 0003, slice 4).

Emits ONE .html holding the entire briefing — every JSON, page image, basemap
tile, and Leaflet itself — inlined as base64 data: URIs. Open it from the Files
app with no server, no service worker, and no network.

Its justification is durability, not the iOS service-worker gap: on an iPad a
downloaded .html opens in Safari anyway, where the service worker path already
works. What the bundle actually buys is independence from the server — it
outlives the 24 h sweep and a Railway redeploy, AirDrops to the other pilot, and
keeps as a record of what was briefed.

It deliberately does NOT fork index.html. The generator injects one
`window.__BUNDLE__` object ahead of the existing inline script; `DATA()` gains a
single conditional and the tile layer a `getTileUrl` override. Two copies of a
70 KB briefing UI drifting apart is the outcome to avoid — the wx_tier
thresholds and Source Pane fill logic living in that file are exactly what
CLAUDE.md documents as regression-prone.
"""
import base64
import json
import mimetypes
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# Anchor for the injection. Must sit BEFORE the inline script that defines
# DATA(), or the constant would not exist at definition time.
_SCRIPT_ANCHOR = "<script>\n// ── Run + group URL params"

_CSS_TAG = '<link rel="stylesheet" href="/static/leaflet.css" />'
_JS_TAG  = '<script src="/static/leaflet.js"></script>'

# Server paths that mean nothing to a file:// copy — stripped so the bundle
# makes no network requests at all, rather than a couple of harmless 404s.
_STRIP_TAGS = (
    '<link rel="manifest" href="/static/app.webmanifest" />',
    '<link rel="apple-touch-icon" href="/static/icon-180.png" />',
)


def _data_uri(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def build(run_dir, tile_root=None, index_path=None):
    """Render a run into one self-contained HTML string.

    Reads the run's manifest — the same list the service worker precaches, so
    the bundle and the online briefing can never disagree about what belongs
    to a flight.
    """
    manifest_path = os.path.join(run_dir, "manifest.json")
    manifest = json.loads(_read(manifest_path))

    tile_root  = tile_root or os.path.join(HERE, "data", "tiles")
    index_path = index_path or os.path.join(HERE, "index.html")

    # Keyed by manifest-relative path ("1/airports.json", "tiles/6/50/28.png"),
    # which is what DATA() and getTileUrl reconstruct on the client.
    assets = {}
    for rel in manifest.get("files", []):
        src = (os.path.join(tile_root, *rel.split("/")[1:]) if rel.startswith("tiles/")
               else os.path.join(run_dir, *rel.split("/")))
        if os.path.isfile(src):
            assets[rel] = _data_uri(src)

    payload = {
        "run_id":        manifest.get("run_id"),
        "generated_iso": manifest.get("generated_iso"),
        "groups":        manifest.get("groups", [1]),
        "assets":        assets,
    }
    # "</" cannot appear literally inside a <script> block or it terminates it.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    html = _read(index_path)
    for tag in _STRIP_TAGS:
        html = html.replace(tag, "")
    html = html.replace(_CSS_TAG, "<style>\n" + _read(os.path.join(HERE, "static", "leaflet.css")) + "\n</style>")
    html = html.replace(_JS_TAG,  "<script>\n" + _read(os.path.join(HERE, "static", "leaflet.js"))  + "\n</script>")
    if _SCRIPT_ANCHOR not in html:
        raise ValueError("index.html injection anchor not found — bundle_builder is out of sync")
    html = html.replace(_SCRIPT_ANCHOR,
                        f"<script>window.__BUNDLE__={blob};</script>\n" + _SCRIPT_ANCHOR, 1)
    return html


def build_to_file(run_dir, out_path=None, **kw):
    """Write the bundle beside the run's other output.

    Note: the route serves this cache-first, so a bundle built before an
    index.html change keeps the old UI. Acceptable because runs are swept at
    24 h anyway — delete bundle.html to force a rebuild.
    """
    out_path = out_path or os.path.join(run_dir, "bundle.html")
    html = build(run_dir, **kw)
    tmp = out_path + ".part"          # never serve a half-written bundle
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out_path)
    return out_path
