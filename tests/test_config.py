# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tomllib

import pytest
from conftest import write_config

from smackcicd import config, template, tomlw
from smackcicd.util import SCRUB


def test_relative_paths_resolve_against_home(cfg, home):
    assert cfg.workspace == home / "workspace"
    assert cfg.drop_root == home / "artifacts"
    assert cfg.state_db == home / "state" / "smackcicd.sqlite"


def test_absolute_paths_are_kept(home, tmp_path):
    elsewhere = (tmp_path / "big-disk" / "ws").as_posix()
    write_config(home, '[workspace]\npath = "%s"\n' % elsewhere)
    assert config.load(home).workspace.as_posix() == elsewhere


def test_uproject_falls_back_to_the_only_one(cfg):
    cfg.workspace.mkdir(parents=True)
    (cfg.workspace / "Renamed.uproject").write_text("{}")
    cfg.data["project"]["uproject"] = "OldName.uproject"
    assert cfg.uproject.name == "Renamed.uproject"
    assert cfg.project_name == "Renamed"


def test_type_errors_are_explained(home):
    write_config(home, "[server]\nport = \"9099\"\n")
    with pytest.raises(config.ConfigError, match="server.port must be a whole number"):
        config.load(home)
    write_config(home, "[workspace]\nsubmodules = 1\n")
    with pytest.raises(config.ConfigError, match="true or false"):
        config.load(home)


def test_unknown_keys_warn_but_load(home):
    write_config(home, "[server]\nprot = 1\n[platforms.Switch]\nenabled = true\n")
    cfg = config.load(home)
    assert any("server.prot" in w for w in cfg.warnings)
    assert any("platforms.Switch" in w for w in cfg.warnings)


def test_free_form_tables_are_not_validated(home):
    write_config(home, '[engine.versions]\n"5.4" = "C:/UE_5.4"\n'
                       '[channels.nightly]\nconfiguration = "DebugGame"\n'
                       '[platforms.Android.env]\nJAVA_HOME = "/opt/jdk"\n')
    cfg = config.load(home)
    assert cfg.warnings == []
    assert cfg.get("channels.nightly.configuration") == "DebugGame"


def test_secrets_load_and_are_scrubbed(home, monkeypatch):
    (home / "secrets.env").write_text(
        "# comment\nSMACKCICD_FORGE_TOKEN='tok-123456'\nexport SMACKCICD_ADMIN_TOKEN=adm-654321\n")
    cfg = config.load(home)
    assert cfg.secret("repo.token_env") == "tok-123456"
    assert cfg.secret("server.admin_token_env") == "adm-654321"
    assert SCRUB("token tok-123456 here") == "token **** here"


def test_environment_beats_secrets_file(home, monkeypatch):
    monkeypatch.setenv("SMACKCICD_FORGE_TOKEN", "from-env")
    (home / "secrets.env").write_text("SMACKCICD_FORGE_TOKEN=from-file\n")
    assert config.load(home).secret("repo.token_env") == "from-env"


def test_missing_config_says_what_to_do(tmp_path):
    with pytest.raises(config.ConfigError, match="smackcicd init"):
        config.load(tmp_path / "nothing-here")


def test_find_home_order(tmp_path, monkeypatch):
    monkeypatch.delenv("SMACKCICD_HOME", raising=False)
    assert config.find_home(tmp_path / "x") == (tmp_path / "x").resolve()
    monkeypatch.setenv("SMACKCICD_HOME", str(tmp_path / "env"))
    assert config.find_home() == (tmp_path / "env").resolve()


@pytest.mark.parametrize("value", [
    "plain", r"C:\Program Files\Epic Games", "it's", 'say "hi"', r"^v\d+$", 42, True, [],
    ["a", "b"], {}, {"5.4": "C:/UE_5.4", "bare_key": 1},
])
def test_toml_values_round_trip(value):
    assert tomllib.loads("v = %s" % tomlw.value(value))["v"] == value


def test_template_renders_valid_toml_with_the_values():
    text = template.render({
        "created": "2026-01-01", "project_uproject": "Game.uproject",
        "repo_url": "https://github.com/acme/game.git", "repo_forge": "github",
        "repo_api_url": "", "repo_owner": "acme", "repo_name": "game",
        "repo_git_user": "x-access-token", "workspace_path": "workspace",
        "engine_versions": {"5.4": "C:/Program Files/Epic Games/UE_5.4"},
        "server_port": 9100, "server_public_url": "", "platforms_default": ["Windows"],
        "windows_enabled": True, "android_enabled": False, "linux_enabled": False,
        "android_env": {"JAVA_HOME": "C:/jdk"},
    })
    data = tomllib.loads(text)
    assert data["repo"]["forge"] == "github"
    assert data["engine"]["versions"]["5.4"].endswith("UE_5.4")
    assert data["server"]["port"] == 9100
    assert data["triggers"]["tag_pattern"] == r"^v\d+\.\d+\.\d+"
    assert config.validate(data) == []


def test_default_home_is_per_os():
    path = config.default_home()
    assert path.name == "smackcicd"
    if os.name == "nt":
        assert "ProgramData" in str(path) or "AppData" in str(path)
