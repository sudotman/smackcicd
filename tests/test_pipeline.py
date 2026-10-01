# SPDX-License-Identifier: GPL-3.0-or-later
"""End to end: a real git repo, a fake engine, the real pipeline."""

import hashlib
import hmac
import json
import os
import subprocess
import sys

import pytest
from conftest import write_config

from smackcicd import config
from smackcicd.runner import Pipeline
from smackcicd.state import State
from smackcicd.watch import handle_webhook

FAKE_UAT = r'''
import sys
from pathlib import Path
args = sys.argv[1:]
out = next(a.split("=", 1)[1] for a in args if a.startswith("-archivedirectory="))
platform = next(a.split("=", 1)[1] for a in args if a.startswith("-platform="))
project = Path(next(a.split("=", 1)[1] for a in args if a.startswith("-project=")))
print("Parsing command line: " + " ".join(args))
version = (project.parent / "Config" / "DefaultGame.ini").read_text()
assert "ProjectVersion=0.2.0-beta.1+" in version, version
Path(out).mkdir(parents=True, exist_ok=True)
if platform == "Win64":
    (Path(out) / "Game.exe").write_text("exe")
    (Path(out) / "Game" / "Content").mkdir(parents=True, exist_ok=True)
    (Path(out) / "Game" / "Content" / "pak.pak").write_text("pak")
print("AutomationTool exiting with ExitCode=0 (Success)")
'''


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


@pytest.fixture
def project(tmp_path):
    origin = tmp_path / "origin"
    (origin / "Config").mkdir(parents=True)
    (origin / "Game.uproject").write_text(json.dumps({"EngineAssociation": "5.4"}))
    (origin / "Config" / "DefaultGame.ini").write_text("[/Script/EngineSettings.GeneralProjectSettings]\n")
    (origin / "Config" / "DefaultEngine.ini").write_text("[/Script/Engine]\n")
    git(origin, "init", "-q", "-b", "main")
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "first")
    git(origin, "tag", "-a", "v0.2.0-beta.1", "-m", "beta")
    return origin


@pytest.fixture
def fake_engine(tmp_path):
    root = tmp_path / "UE_5.4"
    batch = root / "Engine" / "Build" / "BatchFiles"
    batch.mkdir(parents=True)
    (root / "Engine" / "Build" / "Build.version").write_text(json.dumps(
        {"MajorVersion": 5, "MinorVersion": 4, "PatchVersion": 4}))
    (batch / "fake_uat.py").write_text(FAKE_UAT)
    if os.name == "nt":
        (batch / "RunUAT.bat").write_text('@"%s" "%%~dp0fake_uat.py" %%*\r\n' % sys.executable)
    else:
        script = batch / "RunUAT.sh"
        script.write_text('#!/bin/sh\nexec "%s" "$(dirname "$0")/fake_uat.py" "$@"\n'
                          % sys.executable)
        script.chmod(0o755)
    return root


def test_a_tag_becomes_a_packaged_build(home, project, fake_engine, log):
    write_config(home, '[workspace]\npath = "ws"\n'
                       '[engine.versions]\n"5.4" = "%s"\n'
                       '[platforms]\ndefault = ["Windows"]\n'
                       '[platforms.Windows]\nenabled = true\n' % fake_engine.as_posix())
    cfg = config.load(home)
    cfg.data["repo"]["url"] = str(project)
    cfg.enabled_platforms = lambda: ["Windows"]
    subprocess.run(["git", "clone", "-q", str(project), str(cfg.workspace)], check=True)

    state = State(cfg.state_db)
    combined = Pipeline(cfg, state, log).run_tag("v0.2.0-beta.1")
    assert combined["result"] == "success", combined
    windows = combined["platforms"]["Windows"]
    assert [a["name"] for a in windows["artifacts"]] == ["Game-0.2.0-beta.1-Windows.zip"]
    drop = cfg.drop_root / "v0.2.0-beta.1"
    assert (drop / "manifest.json").exists() and (drop / "SHA256SUMS.txt").exists()
    assert json.loads((cfg.drop_root / "index.json").read_text())["builds"][0]["result"] == "success"
    # The version stamp is undone after the build: the workspace stays clean.
    assert "ProjectVersion" not in (cfg.workspace / "Config" / "DefaultGame.ini").read_text()
    row = state.recent_builds(1)[0]
    assert row["status"] == "success" and row["platform"] == "Windows"
    state.close()


def test_webhook_queues_a_signed_tag_push(cfg, log):
    cfg.data["platforms"]["default"] = ["Windows"]
    cfg.enabled_platforms = lambda: ["Windows"]
    os.environ["SMACKCICD_WEBHOOK_SECRET"] = "hook-secret"
    try:
        state = State(cfg.state_db)
        body = json.dumps({"ref": "refs/tags/v1.0.0", "after": "a" * 40}).encode()
        good = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        status, reply = handle_webhook(cfg, state, log, {"X-Gitea-Signature": "f" * 64}, body)
        assert status == 401
        status, reply = handle_webhook(cfg, state, log, {"X-GitHub-Event": "push",
                                                         "X-Hub-Signature-256": "sha256=" + good},
                                       body)
        assert status == 200 and reply == {"queued": ["v1.0.0"]}
        assert state.queued_jobs()[0]["tag"] == "v1.0.0"
        state.close()
    finally:
        del os.environ["SMACKCICD_WEBHOOK_SECRET"]
