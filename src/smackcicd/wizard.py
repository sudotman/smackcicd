# SPDX-License-Identifier: GPL-3.0-or-later
"""`smackcicd init`: detect what can be detected, ask for the rest, write the config.

Works interactively, or headless with --yes and flags (for scripts and
provisioning). Everything it writes is plain text you can edit afterwards.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from datetime import date
from pathlib import Path

from . import detect, engines, forge, platforms as platforms_mod, template
from .config import CONFIG_FILENAME, SECRETS_FILENAME, find_home, load
from .logging_setup import get_logger
from .util import ensure_dir, which
from .workspace import Workspace


class Prompter:
    def __init__(self, interactive):
        self.interactive = interactive

    def ask(self, question, default=""):
        if not self.interactive:
            return default
        shown = " [%s]" % default if default else ""
        answer = input("%s%s: " % (question, shown)).strip()
        return answer or default

    def yes(self, question, default=True):
        if not self.interactive:
            return default
        hint = "Y/n" if default else "y/N"
        answer = input("%s [%s]: " % (question, hint)).strip().lower()
        return default if not answer else answer in ("y", "yes")

    def secret(self, question):
        if not self.interactive:
            return ""
        return getpass.getpass("%s (input hidden, Enter to skip): " % question).strip()


def _toml_path(path, home):
    """Relative to home when inside it, else absolute -- always forward slashes."""
    path = Path(path).resolve()
    try:
        return path.relative_to(home.resolve()).as_posix() or "."
    except ValueError:
        return path.as_posix()


def _say(text=""):
    print(text)


def run_init(args):
    home = find_home(args.home)
    config_path = home / CONFIG_FILENAME
    if config_path.exists() and not args.force:
        _say("%s already exists. Edit it, or re-run with --force to start over." % config_path)
        return 1
    interactive = sys.stdin.isatty() and not args.yes
    ask = Prompter(interactive)
    ensure_dir(home)
    _say("smackcicd home: %s" % home)

    if not which("git"):
        _say("git is not on PATH -- install it first.")
        return 1

    # -- workspace and repository --------------------------------------------
    workspace = Path(args.workspace or ask.ask(
        "Build workspace (an existing clone to adopt, or where to clone)",
        str(home / "workspace"))).expanduser()
    if not workspace.is_absolute():
        workspace = home / workspace
    is_clone = (workspace / ".git").exists()
    repo_url = args.repo or (detect.git_remote(workspace) if is_clone else "")
    if not repo_url:
        repo_url = ask.ask("Git URL of the Unreal project repository")
    if not repo_url:
        _say("A repository URL is needed (--repo).")
        return 1
    remote = forge.parse_remote(repo_url)

    kind = args.forge
    if not kind:
        _say("detecting the forge behind %s ..." % (remote or {}).get("host", repo_url))
        kind = forge.detect_kind(repo_url)
    _say("forge: %s" % {"github": "GitHub", "gitea": "Gitea / Forgejo",
                       "none": "none (tags via git; no releases)"}[kind])

    token_env = "SMACKCICD_FORGE_TOKEN"
    token = os.environ.get(token_env, "")
    if not token and kind != "none":
        hint = ("a fine-grained token with Contents: read/write and Commit statuses: "
                "read/write" if kind == "github"
                else "a token with write:repository scope (Settings > Applications)")
        token = ask.secret("API token for releases and commit status -- %s" % hint)
    git_user = args.git_user or ("x-access-token" if kind == "github" and token else "")

    # -- project, engines, platforms ------------------------------------------------
    uprojects = detect.find_uprojects(workspace)
    uproject = uprojects[0] if len(uprojects) == 1 else None
    association = engines.engine_association(uproject) if uproject else ""
    found = engines.discover()
    if found:
        _say("Unreal Engine installs found: %s" % ", ".join(
            "%s (%s)" % (v, e.root) for v, e in found.items()))
    else:
        _say("no Unreal Engine install found -- add it under [engine] versions later")
    if association and association not in found and len(association) <= 6:
        _say("warning: the project wants Unreal %s, which is not installed here" % association)

    toolchain = detect.android_toolchain()
    wants_android = detect.project_targets_android(workspace) if is_clone else bool(
        toolchain["sdk"])
    if args.platform:
        chosen = [platforms_mod.get(p).name for p in args.platform]
    else:
        chosen = []
        if os.name == "nt":
            chosen.append("Windows")
        else:
            chosen.append("Linux")
        if wants_android:
            if toolchain["missing"]:
                _say("Android: the project targets it, but this machine is missing %s -- "
                     "leaving it disabled (see docs/android.md)" % ", ".join(toolchain["missing"]))
            else:
                chosen.append("Android")
    _say("platforms: %s" % ", ".join(chosen))
    if "Android" in chosen and toolchain["env"]:
        _say("Android builds will get %s from the config" % ", ".join(sorted(toolchain["env"])))

    # -- secrets and config -----------------------------------------------------------
    webhook_secret = secrets.token_urlsafe(24)
    admin_token = secrets.token_urlsafe(24)
    values = {
        "created": date.today().isoformat(),
        "project_uproject": uproject.name if uproject else "",
        "repo_url": repo_url,
        "repo_forge": kind,
        "repo_api_url": "",
        "repo_owner": (remote or {}).get("owner", ""),
        "repo_name": (remote or {}).get("name", ""),
        "repo_git_user": git_user,
        "workspace_path": _toml_path(workspace, home),
        "engine_versions": {v: e.root.as_posix() for v, e in found.items()},
        "server_port": args.port,
        "server_public_url": args.public_url or "",
        "platforms_default": chosen,
        "windows_enabled": "Windows" in chosen,
        "android_enabled": "Android" in chosen,
        "linux_enabled": "Linux" in chosen,
        "android_env": toolchain["env"] if "Android" in chosen else {},
    }
    config_path.write_text(template.render(values), encoding="utf-8")
    secrets_path = home / SECRETS_FILENAME
    secrets_path.write_text(
        "# smackcicd secrets -- keep this file private and out of version control.\n"
        "SMACKCICD_FORGE_TOKEN=%s\n"
        "SMACKCICD_WEBHOOK_SECRET=%s\n"
        "SMACKCICD_ADMIN_TOKEN=%s\n"
        "SMACKCICD_ANDROID_STORE_PASSWORD=\n"
        "SMACKCICD_ANDROID_KEY_PASSWORD=\n" % (token, webhook_secret, admin_token),
        encoding="utf-8")
    if os.name != "nt":
        os.chmod(secrets_path, 0o600)
    _say("wrote %s and %s" % (config_path, secrets_path))

    # -- clone ------------------------------------------------------------------------------
    cfg = load(home)
    if not is_clone:
        if args.clone or (args.clone is None and ask.yes(
                "Clone %s into %s now? (large repositories take a while)" % (repo_url, workspace))):
            Workspace(cfg, get_logger("init")).clone(partial=args.partial)
            uprojects = detect.find_uprojects(workspace)
        else:
            _say("skipped the clone -- run `smackcicd clone` before the first build")

    # -- next steps -------------------------------------------------------------------------
    from .publish import dashboard_url
    base = dashboard_url(cfg)
    _say("")
    _say("Next:")
    _say("  1. smackcicd doctor                 check this machine can build")
    _say("  2. smackcicd service install        run it in the background, starting at boot")
    _say("  3. add a webhook on your forge for instant builds (optional; it also polls):")
    _say("       URL:     %s%s" % (base, cfg.get("server.webhook_path")))
    _say("       secret:  SMACKCICD_WEBHOOK_SECRET in %s" % secrets_path)
    _say("       events:  push / create (tags) and releases, content type JSON")
    _say("  4. push a tag such as v0.1.0-alpha.1 and watch %s/" % base)
    if not token and kind != "none":
        _say("")
        _say("No API token yet: builds will run, but releases and commit status are skipped "
             "until SMACKCICD_FORGE_TOKEN is set in %s." % secrets_path)
    return 0
