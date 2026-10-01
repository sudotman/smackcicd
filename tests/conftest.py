# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from smackcicd import config as config_mod  # noqa: E402


def write_config(home, text=""):
    """A minimal valid runner home with ``text`` as extra TOML."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    base = '[repo]\nurl = "https://github.com/acme/game.git"\nforge = "none"\n'
    (home / config_mod.CONFIG_FILENAME).write_text(base + text, encoding="utf-8")
    return home


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated runner home; nothing leaks in from the real environment."""
    for name in list(os.environ):
        if name.startswith("SMACKCICD_"):
            monkeypatch.delenv(name, raising=False)
    return write_config(tmp_path / "home")


@pytest.fixture
def cfg(home):
    return config_mod.load(home)


@pytest.fixture
def log():
    return logging.getLogger("smackcicd.test")


def _escape(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def pytest_runtest_logreport(report):
    """On GitHub Actions, surface each failure as an annotation.

    Job logs of public repositories need a signed-in viewer; annotations do
    not, so this is what makes a red run readable to everyone.
    """
    if not (report.failed and os.environ.get("GITHUB_ACTIONS") == "true"):
        return
    path, line, _ = report.location
    detail = str(report.longrepr).splitlines()[-25:]
    sys.__stdout__.write("\n::error file=%s,line=%d,title=%s::%s\n" % (
        path.replace("\\", "/"), (line or 0) + 1, _escape(report.nodeid),
        _escape("\n".join(detail))))
    sys.__stdout__.flush()
