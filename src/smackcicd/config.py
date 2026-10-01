# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration: one ``smackcicd.toml`` in a home folder, plus ``secrets.env``.

Every path in the config may be relative; relative paths resolve against the
home folder, so a runner can be moved or copied without editing anything.
Secrets never live in the TOML: the config names environment variables, and
``secrets.env`` beside it is loaded into the environment at start-up.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

from . import platforms as platforms_mod
from .util import SCRUB

CONFIG_FILENAME = "smackcicd.toml"
SECRETS_FILENAME = "secrets.env"
HOME_ENV = "SMACKCICD_HOME"

DEFAULTS = {
    "project": {
        # Relative to the workspace root. Empty: the only .uproject at the root.
        "uproject": "",
        # Empty: the .uproject's file name (Unreal names targets after it).
        "name": "",
    },
    "repo": {
        "url": "",
        # auto | gitea | github | none
        "forge": "auto",
        # Empty: derived from repo.url (api.github.com, or <host>/api/v1 for Gitea).
        "api_url": "",
        # Empty: parsed from repo.url.
        "owner": "",
        "name": "",
        "token_env": "SMACKCICD_FORGE_TOKEN",
        # When set, git fetches over HTTPS with this user name and the forge
        # token, so a service account needs no stored git credentials.
        # GitHub: "x-access-token". Gitea: your Gitea user name.
        "git_user": "",
        "verify_tls": True,
    },
    "workspace": {
        "path": "workspace",
        # fast: keep DerivedDataCache, Intermediate, Binaries, Saved, plugin builds.
        # standard: keep DerivedDataCache and Saved only.  deep: keep nothing.
        "clean": "fast",
        "submodules": True,
        # auto: run `git lfs pull` when .gitattributes uses LFS.  true | false.
        "lfs": "auto",
    },
    "engine": {
        # A fixed engine root for every build. Empty: pick per build.
        "path": "",
        # {"5.4": "C:/Program Files/Epic Games/UE_5.4"} -- filled by `init`.
        "versions": {},
        # Engine version(s) to build when the tag names none. Empty: the
        # .uproject's EngineAssociation.
        "default": [],
        # Extra folders that contain UE_x.y installs.
        "search_dirs": [],
    },
    "triggers": {
        "tag_pattern": r"^v\d+\.\d+\.\d+",
        "poll": True,
        "poll_seconds": 60,
        # On the very first run, build tags that already exist? Usually not.
        "build_backlog_on_first_run": False,
    },
    "server": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 9099,
        # How people on your network reach the dashboard, for download links.
        # Empty: derived from the address this machine uses to reach the forge.
        "public_url": "",
        "webhook": True,
        "webhook_path": "/webhook",
        "webhook_secret_env": "SMACKCICD_WEBHOOK_SECRET",
        "dashboard": True,
        "allow_actions": True,
        "admin_token_env": "SMACKCICD_ADMIN_TOKEN",
    },
    "platforms": {
        # Platforms a tag builds when it names none. Empty: every enabled
        # platform this machine can build.
        "default": [],
        "Windows": {"enabled": True, "prereqs": True, "zip": True, "extra_args": [],
                    "env": {}},
        "Android": {"enabled": True, "cook_flavor": "ASTC", "distribution": "auto",
                    "zip": True, "extra_args": [], "env": {}},
        "Linux": {"enabled": False, "zip": True, "extra_args": [], "env": {}},
    },
    "channels": {
        "alpha": {"configuration": "Development", "prerelease": True, "rank": 1},
        "beta": {"configuration": "Development", "prerelease": True, "rank": 3},
        "rc": {"configuration": "Shipping", "prerelease": True, "rank": 6},
        "release": {"configuration": "Shipping", "prerelease": False, "rank": 9},
        "_unknown": {"configuration": "Development", "prerelease": True, "rank": 0},
    },
    "signing": {
        "android": {
            "keystore": "",
            "alias": "",
            "store_password_env": "SMACKCICD_ANDROID_STORE_PASSWORD",
            "key_password_env": "SMACKCICD_ANDROID_KEY_PASSWORD",
        },
    },
    "versioning": {
        "inject_project_version": True,
        "inject_android_version": True,
        "android_version_code_offset": 0,
    },
    "artifacts": {
        "path": "artifacts",
        "retain_builds": 20,
    },
    "publish": {
        "release": True,
        # Artifact types attached to a release. Multi-GB OBBs and zips are
        # linked from the dashboard instead (download_links).
        "upload": ["apk", "aab", "manifest", "checksums"],
        # 0: no limit of our own (the forge may still have one).
        "max_upload_mb": 0,
        "commit_status": True,
        "status_context": "smackcicd",
        "download_links": True,
    },
    "notify": {
        # A URL to POST to when a job finishes. Empty: off.
        "url": "",
        # generic | slack | discord | teams
        "kind": "generic",
    },
    "runner": {
        "build_timeout_minutes": 300,
        "logs": "logs",
        "state": "state",
    },
    "service": {
        "name": "smackcicd",
    },
}

# Free-form maps: their keys are user data, not settings to validate.
FREE_FORM = {"engine.versions", "channels", "platforms.Windows.env", "platforms.Android.env",
             "platforms.Linux.env"}

SECRET_KEYS = (
    "repo.token_env",
    "server.webhook_secret_env",
    "server.admin_token_env",
    "signing.android.store_password_env",
    "signing.android.key_password_env",
)


class ConfigError(RuntimeError):
    pass


# ------------------------------------------------------------------ home
def default_home():
    if os.name == "nt":
        base = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "smackcicd"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "smackcicd"


def find_home(explicit=None):
    """--home, then $SMACKCICD_HOME, then the current folder if it has a
    smackcicd.toml, then the per-OS default."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(HOME_ENV)
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / CONFIG_FILENAME).exists():
        return cwd.resolve()
    return default_home()


# ---------------------------------------------------------------- merging
def deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _type_name(value):
    return {bool: "true/false", int: "a whole number", float: "a number", str: "text",
            list: "a list", dict: "a table"}.get(type(value), type(value).__name__)


def validate(user, defaults=DEFAULTS, prefix=""):
    """Type-check ``user`` against the defaults. Returns warnings; raises on errors."""
    warnings = []
    for key, value in user.items():
        dotted = prefix + key
        if prefix.rstrip(".") in FREE_FORM:
            continue
        if key not in defaults:
            if prefix == "platforms." and isinstance(value, dict):
                warnings.append("unknown platform [platforms.%s] (known: %s)"
                                % (key, ", ".join(platforms_mod.PLATFORMS)))
            else:
                warnings.append("unknown setting %s (ignored)" % dotted)
            continue
        expected = defaults[key]
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ConfigError("%s must be a table, e.g. [%s]" % (dotted, dotted))
            warnings += validate(value, expected, dotted + ".")
        elif isinstance(expected, bool):
            if not isinstance(value, bool):
                raise ConfigError("%s must be true or false, not %r" % (dotted, value))
        elif isinstance(expected, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError("%s must be a whole number, not %r" % (dotted, value))
        elif isinstance(expected, list):
            if not isinstance(value, list):
                raise ConfigError("%s must be a list, e.g. [\"%s\"]" % (dotted, value))
        elif isinstance(expected, str):
            # distribution accepts a bool or "auto"; everything else is text.
            if not isinstance(value, str) and not (dotted.endswith(".distribution")
                                                   and isinstance(value, bool)):
                raise ConfigError("%s must be %s, not %r" % (dotted, _type_name(expected), value))
    return warnings


# ---------------------------------------------------------------- config
class Config:
    def __init__(self, data, home, path=None, warnings=None):
        self.data = data
        self.home = Path(home)
        self.path = Path(path) if path else self.home / CONFIG_FILENAME
        self.warnings = list(warnings or [])

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted):
        value = self.get(dotted)
        if value in (None, "", [], {}):
            raise ConfigError("%s is not set in %s" % (dotted, self.path))
        return value

    def secret(self, env_key_path):
        """The value of the environment variable that ``env_key_path`` names."""
        env_name = self.get(env_key_path)
        return os.environ.get(env_name, "") if env_name else ""

    # -- paths ---------------------------------------------------------
    def path_of(self, dotted, fallback=""):
        value = self.get(dotted) or fallback
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else self.home / path

    @property
    def workspace(self):
        return self.path_of("workspace.path", "workspace")

    @property
    def drop_root(self):
        return self.path_of("artifacts.path", "artifacts")

    @property
    def log_root(self):
        return self.path_of("runner.logs", "logs")

    @property
    def state_dir(self):
        return self.path_of("runner.state", "state")

    @property
    def state_db(self):
        return self.state_dir / "smackcicd.sqlite"

    @property
    def lock_path(self):
        return self.state_dir / "build.lock"

    @property
    def uproject(self):
        """The configured .uproject, or failing that the only one in the checkout.

        Projects get renamed, and one workspace builds tags from both sides of
        a rename, so a missing configured file is not an error by itself.
        """
        configured = self.get("project.uproject")
        if configured:
            candidate = Path(configured)
            candidate = candidate if candidate.is_absolute() else self.workspace / candidate
            if candidate.exists():
                return candidate
        found = sorted(self.workspace.glob("*.uproject")) if self.workspace.is_dir() else []
        if len(found) == 1:
            return found[0]
        if configured:
            return self.workspace / configured
        return found[0] if found else self.workspace / "Project.uproject"

    @property
    def project_name(self):
        return self.get("project.name") or self.uproject.stem

    @property
    def remote_url(self):
        return self.get("repo.url") or ""

    # -- platforms -------------------------------------------------------
    def platform(self, name):
        return self.get("platforms.%s" % name) or {}

    def enabled_platforms(self):
        """Platforms switched on in config that this machine can build."""
        return [name for name in platforms_mod.PLATFORMS
                if self.platform(name).get("enabled", False)
                and platforms_mod.buildable_here(name)]

    def default_platforms(self):
        return list(self.get("platforms.default") or self.enabled_platforms())


def load_secrets(path):
    """Load KEY=VALUE pairs from secrets.env into the environment.

    Values already in the environment win, so a service manager or CI system
    can override the file.
    """
    path = Path(path)
    if not path.exists():
        return {}
    loaded = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def load(home=None, config_path=None):
    """Load the config from ``config_path``, or ``<home>/smackcicd.toml``."""
    if config_path:
        path = Path(config_path).expanduser().resolve()
        home = path.parent
    else:
        home = find_home(home)
        path = home / CONFIG_FILENAME
    if not path.exists():
        raise ConfigError(
            "no %s in %s -- run `smackcicd init` to create one (or pass --home)"
            % (CONFIG_FILENAME, home))
    try:
        user = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("%s is not valid TOML: %s" % (path, exc)) from exc
    warnings = validate(user)
    load_secrets(home / SECRETS_FILENAME)
    cfg = Config(deep_merge(DEFAULTS, user), home, path, warnings)
    for key in SECRET_KEYS:
        SCRUB.add(cfg.secret(key))
    return cfg
