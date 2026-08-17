"""Unit tests for the offline-briefing support (ADR 0003) — no PDFs, no API key.

Covers the run manifest: it is both the service worker's precache list and the
data routes' completion marker, so an incomplete or inaccurate manifest is the
highest-consequence bug in the offline design — a page PNG missing from it is
simply absent at FL350, with nothing to signal that it should have been there.
"""
import json
import os

import pytest

import app as app_module
import tile_store
from app import _build_manifest, _run_dir_if_complete


# Real route bboxes, matching the table in docs/adr/0003. These ceilings are the
# whole point of a budget over a fixed zoom: the bbox needing the most depth
# (a turnaround, where the crew actually zooms in) costs the least to provide.
BBOXES = {
    "EDDF-VTBS": ((13.5, 47.7, 8.3, 100.7),   6),
    "VTBS-EBBR": ((13.6, 51.0, 4.4, 100.8),   6),
    "VTBS-VHHH": ((13.6, 22.4, 100.5, 114.0), 8),
    "VTBS-WMKK": ((2.7, 13.7, 100.5, 101.8),  9),
}


class TestTileBudget:
    @pytest.mark.parametrize("name", sorted(BBOXES))
    def test_ceiling_matches_adr(self, name):
        bbox, expected = BBOXES[name]
        tiles = tile_store.tiles_for_bbox(bbox)
        assert tile_store.zoom_ceiling(tiles) == expected

    @pytest.mark.parametrize("name", sorted(BBOXES))
    def test_stays_within_budget(self, name):
        bbox, _ = BBOXES[name]
        assert len(tile_store.tiles_for_bbox(bbox)) <= tile_store.TILE_BUDGET

    @pytest.mark.parametrize("name", sorted(BBOXES))
    def test_one_more_zoom_would_bust_it(self, name):
        """The ceiling is the DEEPEST affordable zoom, not just an affordable one."""
        bbox, ceiling = BBOXES[name]
        tiles = tile_store.tiles_for_bbox(bbox)
        nxt = tile_store._tiles_at_zoom(bbox, ceiling + 1)
        assert len(tiles) + len(nxt) > tile_store.TILE_BUDGET

    def test_covers_every_zoom_from_zero(self, name="EDDF-VTBS"):
        bbox, ceiling = BBOXES[name]
        zooms = {z for z, _, _ in tile_store.tiles_for_bbox(bbox)}
        assert zooms == set(range(0, ceiling + 1))

    def test_no_bbox_fetches_nothing(self):
        """Never silently fall back to downloading the whole world."""
        assert tile_store.tiles_for_bbox(None) == []

    def test_tiles_are_unique(self):
        bbox, _ = BBOXES["VTBS-VHHH"]
        tiles = tile_store.tiles_for_bbox(bbox)
        assert len(tiles) == len(set(tiles))

    def test_indices_within_world_bounds(self):
        """Antimeridian/pole clamping — an out-of-range index 404s forever."""
        for bbox, _ in BBOXES.values():
            for z, x, y in tile_store.tiles_for_bbox(bbox):
                assert 0 <= x < (1 << z) and 0 <= y < (1 << z)


class TestBboxForRoutes:
    def _route(self, tmp_path, name, pts):
        p = tmp_path / name
        p.write_text(json.dumps([{"name": "X", "lat": la, "lon": lo} for la, lo in pts]))
        return str(p)

    def test_unions_every_leg(self, tmp_path):
        """A 3-4 leg upload's two groups share one tile set; a group-1-only
        bbox would leave group 2's map over blank tiles."""
        a = self._route(tmp_path, "route_1.json", [(13.6, 100.7), (22.4, 114.0)])
        b = self._route(tmp_path, "route_2.json", [(35.5, 139.8), (40.0, 141.0)])
        assert tile_store.bbox_for_routes([a, b]) == (13.6, 40.0, 100.7, 141.0)

    def test_missing_file_is_skipped(self, tmp_path):
        a = self._route(tmp_path, "route_1.json", [(13.6, 100.7), (22.4, 114.0)])
        assert tile_store.bbox_for_routes([a, str(tmp_path / "nope.json")]) is not None

    def test_all_routes_unreadable_returns_none(self, tmp_path):
        assert tile_store.bbox_for_routes([str(tmp_path / "nope.json")]) is None

    def test_malformed_waypoints_do_not_raise(self, tmp_path):
        p = tmp_path / "route_1.json"
        p.write_text('[{"name": "X"}]')       # no lat/lon
        assert tile_store.bbox_for_routes([str(p)]) is None


class TestTileFetch:
    def test_existing_tiles_are_never_refetched(self, tmp_path):
        """The whole point of a shared store: a second Bangkok flight is free."""
        tiles = [(6, 50, 28), (6, 50, 29)]
        for z, x, y in tiles:
            p = tile_store.tile_path(z, x, y, str(tmp_path))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(b"\x89PNG")
        fetched, skipped, failed = tile_store.fetch_tiles(tiles, root=str(tmp_path))
        assert (fetched, skipped, failed) == (0, 2, 0)

    def test_manifest_entries_are_origin_paths(self):
        assert tile_store.manifest_entries([(6, 50, 28)]) == ["tiles/6/50/28.png"]

    def test_tile_store_lives_outside_runs(self):
        """The 24 h sweep walks runs/ — a store inside it would be deleted and
        the cross-flight sharing would buy nothing."""
        assert "runs" not in os.path.relpath(tile_store.TILE_DIR, tile_store.HERE).split(os.sep)


PIPELINE_FILES = [
    "airports.json", "fir_notams.json", "flight_info.json", "general_notams.json",
    "warnings.json", "route_1.json",
    "met_anchors.json", "met_page_001.png", "met_page_002.png",
    "notam_anchors.json", "notam_page_001.png", "notam_page_002.png",
]


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A realistic single-group run directory, with RUNS_DIR pointed at it."""
    monkeypatch.setattr(app_module, "RUNS_DIR", str(tmp_path))
    run_id  = "692e5848-92a1-495b-bfd8-b02f9e437467"
    group_1 = tmp_path / run_id / "1"
    group_1.mkdir(parents=True)
    for name in PIPELINE_FILES:
        (group_1 / name).write_text("{}")
    return run_id, [str(group_1)]


class TestManifestContents:
    def test_lists_every_pipeline_file(self, run_dir):
        run_id, group_dirs = run_dir
        files = _build_manifest(run_id, group_dirs)["files"]
        for name in PIPELINE_FILES:
            assert f"1/{name}" in files, f"{name} missing — it would never be precached"

    def test_every_entry_exists_on_disk(self, run_dir):
        run_id, group_dirs = run_dir
        manifest = _build_manifest(run_id, group_dirs)
        base = os.path.join(app_module.RUNS_DIR, run_id)
        for rel in manifest["files"]:
            assert os.path.isfile(os.path.join(base, rel)), f"phantom entry {rel}"

    def test_page_images_all_present(self, run_dir):
        """The precache cannot enumerate notam_page_NNN.png on its own."""
        run_id, group_dirs = run_dir
        files = _build_manifest(run_id, group_dirs)["files"]
        pages = [f for f in files if "_page_" in f]
        assert sorted(pages) == [
            "1/met_page_001.png", "1/met_page_002.png",
            "1/notam_page_001.png", "1/notam_page_002.png",
        ]

    def test_excludes_on_demand_artifacts(self, run_dir):
        """hira.json and bundle.html are written AFTER the manifest.

        A naive "manifest == files on disk" rule would go red the first time
        anyone taps HIRA, so both are excluded by construction.
        """
        run_id, group_dirs = run_dir
        for name in ("hira.json", "bundle.html"):
            (open(os.path.join(group_dirs[0], name), "w")).write("{}")
        files = _build_manifest(run_id, group_dirs)["files"]
        assert not [f for f in files if "hira.json" in f or "bundle.html" in f]

    def test_excludes_pipeline_temp_files(self, run_dir):
        """_airports_leg_N.json is an intermediate, deleted before the manifest."""
        run_id, group_dirs = run_dir
        open(os.path.join(group_dirs[0], "_airports_leg_1.json"), "w").write("{}")
        files = _build_manifest(run_id, group_dirs)["files"]
        assert not [f for f in files if f.endswith("_airports_leg_1.json")]

    def test_carries_run_identity(self, run_dir):
        run_id, group_dirs = run_dir
        manifest = _build_manifest(run_id, group_dirs)
        assert manifest["run_id"] == run_id
        assert manifest["groups"] == [1]
        assert manifest["generated_iso"].startswith("20")

    def test_extra_files_are_appended(self, run_dir):
        """Tiles join the manifest this way (slice 2)."""
        run_id, group_dirs = run_dir
        files = _build_manifest(run_id, group_dirs, ["tiles/6/50/28.png"])["files"]
        assert "tiles/6/50/28.png" in files


class TestMultiGroupManifest:
    def test_covers_every_group(self, tmp_path, monkeypatch):
        """A 3-4 leg upload has two groups; both map tabs share one cache bucket,
        so one manifest must cover both or the second tab breaks the first."""
        monkeypatch.setattr(app_module, "RUNS_DIR", str(tmp_path))
        run_id = "692e5848-92a1-495b-bfd8-b02f9e437467"
        group_dirs = []
        for g in ("1", "2"):
            d = tmp_path / run_id / g
            d.mkdir(parents=True)
            (d / "airports.json").write_text("{}")
            group_dirs.append(str(d))
        manifest = _build_manifest(run_id, group_dirs)
        assert manifest["groups"] == [1, 2]
        assert manifest["files"] == ["1/airports.json", "2/airports.json"]


class TestCompletionGate:
    """The manifest is the completion marker — it replaces the old
    `_current_run["status"] == "done"` check, so an older briefing keeps
    working after a newer upload."""

    def test_absent_manifest_is_incomplete(self, run_dir):
        run_id, _ = run_dir
        assert _run_dir_if_complete(run_id) is None

    def test_present_manifest_resolves(self, run_dir):
        run_id, group_dirs = run_dir
        path = os.path.join(app_module.RUNS_DIR, run_id, "manifest.json")
        with open(path, "w") as f:
            json.dump(_build_manifest(run_id, group_dirs), f)
        assert _run_dir_if_complete(run_id) == os.path.join(app_module.RUNS_DIR, run_id)

    def test_unknown_run_is_none(self, run_dir):
        assert _run_dir_if_complete("11111111-2222-3333-4444-555555555555") is None

    @pytest.mark.parametrize("bad", [
        None, "", "../../etc", "not-a-uuid", "/absolute", "692e5848/../..",
    ])
    def test_rejects_non_uuid(self, run_dir, bad):
        """run_id reaches the filesystem, so the shape gate is a traversal guard."""
        assert _run_dir_if_complete(bad) is None
