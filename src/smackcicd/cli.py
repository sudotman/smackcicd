# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from . import __version__, platforms as platforms_mod, tagspec
from .config import ConfigError, load
from .logging_setup import configure, get_logger
from .state import State
from .util import ensure_dir, free_space_gb, human_duration, which

OK, WARN, FAIL = "[ ok ]", "[warn]", "[FAIL]"


def _cfg(args):
    return load(home=args.home, config_path=args.config)


# ------------------------------------------------------------------ setup
def cmd_init(args):
    from .wizard import run_init
    return run_init(args)


def cmd_clone(args):
    from .workspace import Workspace
    cfg = _cfg(args)
    Workspace(cfg, get_logger("clone")).clone(partial=args.partial)
    return 0


def cmd_doctor(args):
    from .engines import EngineError, discover, find_engine
    from .forge import ForgeError, NullForge
    from .forge import from_config as forge_from_config
    from .publish import dashboard_url
    from .workspace import Workspace

    counts = {"problems": 0, "warnings": 0}

    def report(level, message):
        print("%s %s" % (level, message))
        if level == FAIL:
            counts["problems"] += 1
        elif level == WARN:
            counts["warnings"] += 1

    try:
        cfg = _cfg(args)
        report(OK, "config %s" % cfg.path)
    except ConfigError as exc:
        report(FAIL, str(exc))
        return 1
    for warning in cfg.warnings:
        report(WARN, "config: %s" % warning)

    # Forge
    try:
        forge = forge_from_config(cfg)
    except ForgeError as exc:
        report(FAIL, str(exc))
        forge = NullForge()
    if isinstance(forge, NullForge):
        report(WARN, "no forge API: tags come from git ls-remote; no releases or commit status")
    else:
        try:
            report(OK, forge.describe())
        except ForgeError as exc:
            report(FAIL, "cannot reach the forge: %s" % exc)
        if forge.token:
            try:
                forge.check_access()
                report(OK, "API token can read %s/%s" % (forge.owner, forge.repo))
            except ForgeError as exc:
                report(FAIL, "API token rejected: %s" % exc)
        else:
            report(WARN, "no API token (%s) -- releases and commit status are skipped"
                   % cfg.get("repo.token_env"))

    # Workspace and project
    workspace = Workspace(cfg, get_logger("doctor"))
    if workspace.exists():
        report(OK, "workspace clone at %s" % workspace.path)
        if workspace.uses_lfs() and not which("git-lfs"):
            report(FAIL, "the repository uses Git LFS but git-lfs is not installed")
    else:
        report(FAIL, "no clone at %s -- run `smackcicd clone`" % workspace.path)
    free = free_space_gb(workspace.path.parent)
    report(OK if free > 150 else (WARN if free > 60 else FAIL),
           "%.0f GiB free for the workspace (150+ recommended for Unreal)" % free)
    uproject = cfg.uproject
    if workspace.exists():
        report(OK if uproject.exists() else FAIL, "project %s" % uproject.name)

    # Engines
    found = discover(cfg)
    if found:
        report(OK, "Unreal installs: %s" % ", ".join("%s" % v for v in found))
    else:
        report(FAIL, "no Unreal Engine install found -- add one under [engine] versions")
    if workspace.exists() and uproject.exists():
        try:
            engine = find_engine(cfg, uproject)
            report(OK, "this project builds with %s at %s" % (engine.version_string, engine.root))
            if not engine.editor_cmd.exists():
                report(FAIL, "%s is missing" % engine.editor_cmd)
        except EngineError as exc:
            report(FAIL, str(exc))

    # Toolchain
    report(OK if which("git") else FAIL, "git %s" % ("found" if which("git") else "not on PATH"))
    if not (which("7z") or which("7za")):
        report(WARN, "7-Zip not on PATH (optional: zips large packages faster)")
    enabled = cfg.enabled_platforms()
    for name in platforms_mod.PLATFORMS:
        if cfg.platform(name).get("enabled") and not platforms_mod.buildable_here(name):
            report(WARN, "%s is enabled but this machine cannot build it" % name)
    report(OK if enabled else FAIL, "platforms: %s" % (", ".join(enabled) or "none enabled"))
    if "Android" in enabled:
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (cfg.platform("Android").get("env") or {}).items()})
        for var in ("JAVA_HOME", "ANDROID_HOME", "NDKROOT"):
            value = env.get(var)
            if value and os.path.isdir(value):
                report(OK, "%s=%s" % (var, value))
            else:
                report(FAIL, "%s is not set for Android builds -- see docs/android.md" % var)
        from .engines import missing_android_pieces
        for version, engine in found.items():
            gaps = missing_android_pieces(engine, env.get("ANDROID_HOME")
                                          or env.get("ANDROID_SDK_ROOT"), env.get("NDKROOT"))
            for gap in gaps:
                if gap.startswith("NDKROOT"):
                    report(WARN, gap)
                else:
                    report(WARN, "UE %s Android builds need SDK package %s -- install it with "
                           "sdkmanager \"%s\"" % (version, gap, gap))
        keystore = cfg.get("signing.android.keystore")
        if keystore:
            path = cfg.path_of("signing.android.keystore")
            if not path.exists():
                report(FAIL, "keystore %s not found" % path)
            elif not cfg.secret("signing.android.store_password_env"):
                report(FAIL, "keystore password %s is empty"
                       % cfg.get("signing.android.store_password_env"))
            else:
                report(OK, "release keystore %s" % path.name)
        else:
            report(WARN, "no release keystore: APKs are signed with Unreal's debug key")

    # Output folders
    for label, path in (("artifacts", cfg.drop_root), ("logs", cfg.log_root),
                        ("state", cfg.state_dir)):
        try:
            ensure_dir(path)
            report(OK, "%s folder %s" % (label, path))
        except OSError as exc:
            report(FAIL, "%s folder unusable: %s" % (label, exc))

    # Server
    if cfg.get("server.enabled", True):
        port = int(cfg.get("server.port", 9099))
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1)
        in_use = probe.connect_ex(("127.0.0.1", port)) == 0
        probe.close()
        report(OK, "port %d %s" % (port, "in use (the daemon is probably running)"
                                   if in_use else "free"))
        report(OK, "dashboard: %s/" % dashboard_url(cfg))
        if cfg.get("server.webhook", True) and not cfg.secret("server.webhook_secret_env"):
            report(WARN, "webhook secret is empty: anyone who can reach the port can queue builds")
        if cfg.get("server.allow_actions", True) and not cfg.secret("server.admin_token_env"):
            report(WARN, "admin token is empty: the dashboard is read-only")

    print("\n%d problem(s), %d warning(s)" % (counts["problems"], counts["warnings"]))
    return 1 if counts["problems"] else 0


def cmd_webhook(args):
    from .publish import dashboard_url
    cfg = _cfg(args)
    print("URL:          %s%s" % (dashboard_url(cfg), cfg.get("server.webhook_path")))
    print("Content type: application/json")
    print("Events:       push and create (tags), releases")
    secret = cfg.secret("server.webhook_secret_env")
    if args.show_secret:
        print("Secret:       %s" % (secret or "(empty)"))
    else:
        print("Secret:       %s in %s (--show-secret prints it)"
              % (cfg.get("server.webhook_secret_env"), cfg.home / "secrets.env"))
    return 0


# ------------------------------------------------------------------ daily use
def cmd_explain(args):
    cfg = _cfg(args)
    try:
        spec = tagspec.parse(args.tag, cfg, engine_override=args.engine or None)
    except tagspec.TagRejected as exc:
        print("%s would NOT build: %s" % (args.tag, exc))
        return 1
    print(json.dumps({
        "tag": spec.tag,
        "semver": spec.semver,
        "channel": spec.channel,
        "configuration": spec.configuration,
        "prerelease": spec.is_prerelease,
        "platforms": spec.platforms,
        "engines": spec.engines or ["(the .uproject's EngineAssociation)"],
        "androidVersionCode": spec.android_version_code,
        "dropFolder": str(cfg.drop_root / spec.slug()),
    }, indent=2))
    return 0


def cmd_tags(args):
    from .watch import TagSource
    cfg = _cfg(args)
    try:
        remote_tags = sorted(TagSource(cfg, get_logger("tags")).list_tags())
    except Exception as exc:
        print("could not list remote tags: %s" % exc)
        return 1
    for name, sha in remote_tags:
        try:
            spec = tagspec.parse(name, cfg)
            detail = "%s %s -> %s" % (spec.semver, spec.configuration, ", ".join(spec.platforms))
        except tagspec.TagRejected as exc:
            detail = "ignored (%s)" % exc
        print("%-30s %-9s %s" % (name, (sha or "")[:8], detail))
    return 0


def cmd_build(args):
    from .runner import BuildLock, Pipeline
    cfg = _cfg(args)
    if args.no_publish:
        cfg.data["publish"]["release"] = False
        cfg.data["publish"]["commit_status"] = False
        cfg.data["notify"]["url"] = ""
    log = get_logger("build")
    state = State(cfg.state_db)
    lock = BuildLock(cfg.lock_path, log)
    if not lock.acquire(wait_seconds=args.wait * 60):
        log.error("another build is running (lock held); --wait N queues behind it")
        return 2
    try:
        combined = Pipeline(cfg, state, log).run_tag(
            args.tag, platform_override=args.platform or None, engine_override=args.engine or None)
        print("\nresult: %s" % combined["result"])
        for name, data in sorted(combined["platforms"].items()):
            print("  %-12s %-9s %s" % (name, data["result"],
                                       human_duration(data["durationSeconds"] or 0)))
        return 0 if combined["result"] == "success" else 1
    except tagspec.TagRejected as exc:
        log.error("%s", exc)
        return 2
    finally:
        lock.release()
        state.close()


def cmd_watch(args):
    from .watch import watch
    cfg = _cfg(args)
    state = State(cfg.state_db)
    try:
        return watch(cfg, state, get_logger("watch"))
    finally:
        state.close()


def cmd_status(args):
    cfg = _cfg(args)
    state = State(cfg.state_db)
    try:
        builds, queued = state.recent_builds(args.limit), state.queued_jobs()
        if args.json:
            print(json.dumps({"queue": queued, "builds": builds}, indent=2))
            return 0
        if queued:
            print("queued/running: %s" % ", ".join("%s (%s)" % (j["tag"], j["status"])
                                                   for j in queued))
        if not builds:
            print("no builds yet")
            return 0
        row = "%-5s %-28s %-10s %-12s %-11s %-10s %s"
        print(row % ("#", "tag", "platform", "config", "status", "duration", "started"))
        for b in builds:
            print(row % (b["build_number"], b["tag"], b["platform"], b["configuration"] or "",
                         b["status"], human_duration(b["duration_s"]) if b["duration_s"] else "-",
                         b["started_at"] or ""))
        return 0
    finally:
        state.close()


def cmd_service(args):
    from . import service
    cfg = _cfg(args)
    try:
        if args.action == "install":
            print(service.install(cfg, interactive=args.interactive, user=args.user))
        elif args.action == "uninstall":
            print(service.uninstall(cfg))
        elif args.action == "restart":
            print(service.restart(cfg))
        elif args.action == "show":
            if os.name == "nt":
                print(service.windows_script(cfg, interactive=args.interactive, user=args.user))
            else:
                print(service.systemd_unit(cfg))
    except service.ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------ parser
def build_parser():
    parser = argparse.ArgumentParser(
        prog="smackcicd",
        description="Tag-triggered build, package and release runner for Unreal Engine.")
    parser.add_argument("--home", help="the runner's home folder (default: $SMACKCICD_HOME, "
                        "the current folder if it has a smackcicd.toml, else a per-OS default)")
    parser.add_argument("--config", help="an explicit smackcicd.toml (its folder is the home)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--version", action="version", version="smackcicd %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="set up a runner: detect, ask, write the config")
    p.add_argument("--repo", help="git URL of the Unreal project")
    p.add_argument("--workspace", help="the build clone: an existing one to adopt, or where to clone")
    p.add_argument("--forge", choices=["gitea", "github", "none"], help="skip forge detection")
    p.add_argument("--git-user", help="HTTPS user name for token-based fetches")
    p.add_argument("--platform", action="append", help="platforms to enable (repeatable)")
    p.add_argument("--port", type=int, default=9099)
    p.add_argument("--public-url", help="how your team reaches this machine's dashboard")
    clone = p.add_mutually_exclusive_group()
    clone.add_argument("--clone", dest="clone", action="store_true", default=None,
                       help="clone the repository now")
    clone.add_argument("--no-clone", dest="clone", action="store_false")
    p.add_argument("--partial", action="store_true",
                   help="blobless clone: faster for huge repositories")
    p.add_argument("-y", "--yes", action="store_true", help="no questions: use detected defaults")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("clone", help="create the build workspace clone")
    p.add_argument("--partial", action="store_true", help="blobless clone")
    p.set_defaults(func=cmd_clone)

    p = sub.add_parser("doctor", help="check this machine can build")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("webhook", help="what to enter on your forge's webhook page")
    p.add_argument("--show-secret", action="store_true")
    p.set_defaults(func=cmd_webhook)

    p = sub.add_parser("watch", help="run the daemon (webhook, poller, dashboard, builds)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("build", help="build one tag now, in the foreground")
    p.add_argument("tag")
    p.add_argument("--platform", action="append", help="override the tag's platforms (repeatable)")
    p.add_argument("--engine", action="append", metavar="VERSION",
                   help="build with this Unreal version, e.g. 5.4 (repeatable)")
    p.add_argument("--no-publish", action="store_true",
                   help="no release, commit status or notification")
    p.add_argument("--wait", type=int, default=0, help="minutes to wait for the build lock")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("status", help="recent builds and the queue")
    p.add_argument("-n", "--limit", type=int, default=15)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("tags", help="remote tags and how each would build")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("explain", help="what a tag would build, without building it")
    p.add_argument("tag")
    p.add_argument("--engine", action="append", metavar="VERSION")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("service", help="run in the background (Windows task / systemd)")
    p.add_argument("action", choices=["install", "uninstall", "restart", "show"])
    p.add_argument("--interactive", action="store_true",
                   help="Windows: run only while the user is logged on (no admin needed)")
    p.add_argument("--user", help="Windows: the account to run as (default: you)")
    p.set_defaults(func=cmd_service)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    log_root = None
    if args.command not in ("init",):
        try:
            log_root = _cfg(args).log_root
        except Exception:
            log_root = None
    configure(log_root, verbose=args.verbose, quiet=args.quiet)
    try:
        return args.func(args)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
