# SPDX-License-Identifier: GPL-3.0-or-later
import zipfile

import pytest

from smackcicd import artifacts, manifest, tagspec


@pytest.fixture
def spec(cfg):
    cfg.data["platforms"]["default"] = ["Windows", "Android"]
    return tagspec.parse("v0.3.0-alpha.2", cfg)


def lay_out(cfg, spec, label, files):
    root = artifacts.archive_dir(cfg, spec, label)
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 64)
    return root


def names(found):
    return [a["name"] for a in found]


def test_ue58_layout_windows(cfg, spec, log):
    """UE 5.8 archives straight into the folder: a 4-hour build was once
    reported as "no artifacts" because of this."""
    lay_out(cfg, spec, "Windows", ["Game.exe", "Engine/e.dll", "Game/Binaries/Win64/g.exe"])
    found = artifacts.collect(cfg, spec, "Windows", "Windows", "Game", log)
    assert names(found) == ["Game-0.3.0-alpha.2-Windows.zip"]
    entries = zipfile.ZipFile(found[0]["path"]).namelist()
    assert "Windows/Game.exe" in entries
    assert not any(e.endswith(".zip") for e in entries), "the zip must not contain itself"


def test_ue56_layout_windows(cfg, spec, log):
    lay_out(cfg, spec, "Windows", ["Windows/Game.exe", "Windows/Engine/e.dll"])
    found = artifacts.collect(cfg, spec, "Windows", "Windows", "Game", log)
    assert "Windows/Game.exe" in zipfile.ZipFile(found[0]["path"]).namelist()


def test_android_lists_loose_files_and_zips(cfg, spec, log):
    lay_out(cfg, spec, "Android", ["Game-arm64.apk", "main.3002.com.acme.game.obb",
                                   "Install_Game-arm64.bat"])
    found = artifacts.collect(cfg, spec, "Android", "Android", "Game", log)
    kinds = {a["name"]: a["type"] for a in found}
    assert kinds["Game-arm64.apk"] == "apk"
    assert kinds["main.3002.com.acme.game.obb"] == "obb"
    assert kinds["Game-0.3.0-alpha.2-Android.zip"] == "archive"


def test_android_cook_flavor_subfolder(cfg, spec, log):
    lay_out(cfg, spec, "Android", ["Android_ASTC/Game-arm64.apk"])
    found = artifacts.collect(cfg, spec, "Android", "Android", "Game", log)
    assert "Game-arm64.apk" in names(found)


def test_nothing_packaged_is_empty(cfg, spec, log):
    lay_out(cfg, spec, "Windows", [])
    assert artifacts.collect(cfg, spec, "Windows", "Windows", "Game", log) == []


def test_zip_can_be_switched_off(cfg, spec, log):
    cfg.data["platforms"]["Android"]["zip"] = False
    lay_out(cfg, spec, "Android", ["Game-arm64.apk"])
    assert names(artifacts.collect(cfg, spec, "Android", "Android", "Game", log)) == [
        "Game-arm64.apk"]


def test_checksums_file(cfg, spec, log, tmp_path):
    lay_out(cfg, spec, "Windows", ["Game.exe"])
    found = artifacts.collect(cfg, spec, "Windows", "Windows", "Game", log)
    path = artifacts.write_checksums(tmp_path, [{"target": {"label": "Windows"},
                                                 "artifacts": found}])
    line = path.read_text().strip()
    assert line.endswith("  Windows/Game-0.3.0-alpha.2-Windows.zip")
    assert len(line.split()[0]) == 64


def test_release_notes_link_downloads():
    combined = {
        "version": {"semver": "0.3.0-alpha.2", "channel": "alpha", "tag": "v0.3.0-alpha.2",
                    "androidVersionCode": 3102},
        "source": {"shortCommit": "abc1234", "commit": "abc1234def", "subject": "a | b"},
        "engines": [{"version": "5.4.4"}],
        "runner": {"host": "buildpc"},
        "generatedAt": "2026-01-01T00:00:00Z",
        "platforms": {
            "Android": {"result": "success", "configuration": "Development", "artifacts": [
                {"name": "G.zip", "type": "archive", "sizeBytes": 7 * 2**30, "sha256": "a" * 64,
                 "relativePath": "G.zip"},
                {"name": "G.apk", "type": "apk", "sizeBytes": 150 * 2**20, "sha256": "b" * 64,
                 "relativePath": "G.apk"}]},
            "Windows": {"result": "failure", "configuration": "Development", "artifacts": []},
        },
    }
    body = manifest.release_notes(combined, "/drop/v0.3.0-alpha.2", "http://ci:9099/")
    assert "### Quick download" in body
    assert "http://ci:9099/artifacts/v0.3.0-alpha.2/Android/G.zip" in body
    assert body.index("G.zip](") < body.index("G.apk](")       # zip first
    assert "Windows" not in body.split("### Quick download")[1].split("| Field")[0]
    assert "a \\| b" in body                                   # table stays intact
    assert "### Quick download" not in manifest.release_notes(combined, "/drop/x", "")
