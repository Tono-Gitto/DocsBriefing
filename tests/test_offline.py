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
from app import _build_manifest, _run_dir_if_complete


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
