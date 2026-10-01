# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
from pathlib import Path

import pytest

from smackcicd import engines, service
from smackcicd.inifile import get_key, set_keys


def fake_engine(root, major=5, minor=4, patch=2, setup=None):
    root = Path(root)
    batch = root / "Engine" / "Build" / "BatchFiles"
    batch.mkdir(parents=True, exist_ok=True)
    (batch / ("RunUAT.bat" if os.name == "nt" else "RunUAT.sh")).write_text("")
    (root / "Engine" / "Build" / "Build.version").write_text(json.dumps(
        {"MajorVersion": major, "MinorVersion": minor, "PatchVersion": patch}))
    if setup:
        extras = root / "Engine" / "Extras" / "Android"
        extras.mkdir(parents=True, exist_ok=True)
        (extras / "SetupAndroid.bat").write_text(setup)
    return root


def test_discovery_from_search_dirs(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "_registry_entries", lambda: [])
    monkeypatch.setattr(engines, "_launcher_entries", lambda: [])
    monkeypatch.setattr(engines, "_install_ini_entries", lambda: [])
    monkeypatch.setattr(engines, "default_search_dirs", lambda: [])
    fake_engine(tmp_path / "Epic" / "UE_5.4", 5, 4)
    fake_engine(tmp_path / "Epic" / "UE_5.3", 5, 3)
    cfg.data["engine"]["search_dirs"] = [str(tmp_path / "Epic")]
    found = engines.discover(cfg)
    assert list(found) == ["5.4", "5.3"]          # newest first


def test_pinned_versions_win(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "_registry_entries", lambda: [])
    monkeypatch.setattr(engines, "_launcher_entries", lambda: [])
    monkeypatch.setattr(engines, "_install_ini_entries", lambda: [])
    monkeypatch.setattr(engines, "default_search_dirs", lambda: [tmp_path / "Epic"])
    fake_engine(tmp_path / "Epic" / "UE_5.4", 5, 4)
    pinned = fake_engine(tmp_path / "Custom" / "UE54", 5, 4)
    cfg.data["engine"]["versions"] = {"5.4": str(pinned)}
    assert engines.discover(cfg)["5.4"].root == pinned


def test_unreadable_folders_do_not_crash_discovery(tmp_path, monkeypatch):
    """Path.is_dir() raises PermissionError on unreadable folders (Linux /opt)."""
    fake_engine(tmp_path / "Epic" / "UE_5.4", 5, 4)
    (tmp_path / "Epic" / "locked").mkdir()
    real_is_dir = Path.is_dir

    def is_dir(self):
        if "locked" in self.parts:
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    entries = engines._folder_entries([tmp_path / "Epic", tmp_path / "Epic" / "locked"])
    assert [Path(p).name for _, p in entries] == ["UE_5.4"]


def test_find_engine_by_association_and_guid(cfg, tmp_path, monkeypatch):
    root = fake_engine(tmp_path / "src-build", 5, 5)
    monkeypatch.setattr(engines, "_registry_entries",
                        lambda: [("{ABCD-1234}", str(root))])
    monkeypatch.setattr(engines, "_launcher_entries", lambda: [])
    monkeypatch.setattr(engines, "_install_ini_entries", lambda: [])
    monkeypatch.setattr(engines, "default_search_dirs", lambda: [])
    uproject = tmp_path / "Game.uproject"
    uproject.write_text(json.dumps({"EngineAssociation": "{abcd-1234}"}))
    assert engines.find_engine(cfg, uproject).root == root
    uproject.write_text(json.dumps({"EngineAssociation": "5.5"}))
    assert engines.find_engine(cfg, uproject).short_version == "5.5"
    uproject.write_text(json.dumps({"EngineAssociation": "4.27"}))
    with pytest.raises(engines.EngineError, match="4.27 is not installed"):
        engines.find_engine(cfg, uproject)


def test_android_requirements_from_setup_script(tmp_path):
    root = fake_engine(tmp_path / "UE", setup=(
        'if "%PLATFORMS_VERSION%" == "" SET PLATFORMS_VERSION=android-36\n'
        'if "%BUILDTOOLS_VERSION%" == "" SET BUILDTOOLS_VERSION=36.1.0\n'
        'if "%CMAKE_VERSION%" == "" SET CMAKE_VERSION=3.22.1\n'
        'if "%NDK_VERSION%" == "" SET NDK_VERSION=27.2.12479018\n'))
    engine = engines.Engine(root)
    assert engines.android_requirements(engine)["NDK_VERSION"] == "27.2.12479018"
    sdk = tmp_path / "sdk"
    (sdk / "ndk" / "27.2.12479018").mkdir(parents=True)
    (sdk / "build-tools" / "36.0.0").mkdir(parents=True)
    missing = engines.missing_android_pieces(engine, sdk, sdk / "ndk" / "25.1.8937393")
    assert "build-tools;36.1.0" in missing and "platforms;android-36" in missing
    assert any(m.startswith("NDKROOT points at 25.1") for m in missing)


def test_build_cook_run_args(cfg, tmp_path):
    engine = engines.Engine(fake_engine(tmp_path / "UE"))
    args = engines.build_cook_run_args(cfg, engine, "G.uproject", "Android", "Shipping",
                                       "out", distribution=True)
    for flag in ("-platform=Android", "-cookflavor=ASTC", "-package", "-distribution",
                 "-nodebuginfo", "-archivedirectory=out"):
        assert flag in args
    cfg.data["platforms"]["Windows"]["extra_args"] = ["-nocompileeditor"]
    args = engines.build_cook_run_args(cfg, engine, "G.uproject", "Windows", "Development", "o")
    assert "-platform=Win64" in args and "-prereqs" in args and args[-1] == "-nocompileeditor"


def test_windows_service_script(cfg):
    script = service.windows_script(cfg)
    assert "-LogonType S4U" in script and "-MultipleInstances IgnoreNew" in script
    assert "RepetitionInterval (New-TimeSpan -Minutes 5)" in script
    assert "WindowsIdentity]::GetCurrent().Name" in script
    assert "-m smackcicd --home" in script
    for forbidden in ("??", "?.", "USERDOMAIN"):      # PowerShell 5.1 and workgroup traps
        assert forbidden not in script
    assert "-LogonType Interactive" in service.windows_script(cfg, interactive=True)


def test_powershell_quoting(cfg):
    cfg.home = Path("C:/Users/O'Brien/ci")
    assert "O''Brien" in service.windows_script(cfg)


def test_systemd_unit(cfg):
    unit = service.systemd_unit(cfg)
    assert "Restart=always" in unit and "watch" in unit and "[Install]" in unit


def test_ini_edits_are_surgical(tmp_path):
    ini = tmp_path / "DefaultEngine.ini"
    ini.write_text("; keep me\r\n[/Script/A]\r\n+Arr=1\r\nKey=old\r\n\r\n[/Script/B]\r\nX=1\r\n")
    assert set_keys(ini, "/Script/A", {"Key": "new", "Added": "2"}) == ["Key", "Added"]
    text = ini.read_text()
    assert "; keep me" in text and "+Arr=1" in text and "\r\n" in ini.read_bytes().decode()
    assert get_key(ini, "/Script/A", "Key") == "new"
    assert get_key(ini, "/Script/A", "Added") == "2"
    set_keys(ini, "/Script/New", {"Z": "3"})
    assert get_key(ini, "/Script/New", "Z") == "3"
