# SPDX-License-Identifier: GPL-3.0-or-later
"""The dashboard: build history, downloads, logs and admin actions.

Read routes are open on the LAN; anything that changes state needs the bearer
token named by ``server.admin_token_env``.  With no token configured the write actions
are refused outright, so the default posture is read-only.

The page itself is a static shell -- every value arrives as JSON -- which keeps
all escaping on the client and out of Python string formatting.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import socket
import urllib.parse
from pathlib import Path

from importlib import resources

from . import __version__, tagspec
from .control import CONTROL
from .util import free_space_gb, human_size

LOG_CHUNK_LIMIT = 256 * 1024
DOWNLOAD_CHUNK = 1024 * 1024


# ---------------------------------------------------------------- helpers
def _safe_under(root, relative):
    """Resolve ``relative`` under ``root``, refusing anything that escapes it."""
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def _engine_summary(cfg):
    try:
        from .engines import discover, find_engine
        engine = find_engine(cfg, cfg.uproject)
        summary = {"version": engine.version_string, "path": str(engine.root)}
    except Exception as exc:
        summary = {"version": "not found", "path": "", "error": str(exc)}
    try:
        summary["available"] = list(discover(cfg))
    except Exception:
        summary["available"] = []
    return summary


def health(cfg, state):
    from .workspace import Workspace
    from .logging_setup import get_logger

    workspace = Workspace(cfg, get_logger("dashboard"))
    drop_root = cfg.drop_root
    builds_on_disk = len([p for p in drop_root.iterdir() if p.is_dir()]) if drop_root.is_dir() else 0
    recent = state.recent_builds(1)

    workspace_free = free_space_gb(workspace.path.parent)
    drop_free = free_space_gb(drop_root)
    warnings = []
    if workspace_free < 60:
        warnings.append("workspace drive below 60 GiB free (%.0f GiB)" % workspace_free)
    if drop_free < 40:
        warnings.append("drop drive below 40 GiB free (%.0f GiB)" % drop_free)
    if not workspace.exists():
        warnings.append("workspace clone is missing")
    snapshot = CONTROL.snapshot()
    if snapshot["paused"]:
        warnings.append("runner is paused -- queued tags will not build")
    if snapshot["lastPollError"]:
        warnings.append("last tag poll failed: %s" % snapshot["lastPollError"])

    try:
        project = cfg.project_name
    except Exception:
        project = cfg.get("project.name") or ""
    return {
        "host": socket.gethostname(),
        "project": project,
        "toolVersion": __version__,
        "repo": cfg.remote_url,
        "remote": cfg.remote_url,
        "engine": _engine_summary(cfg),
        "workspace": {"path": str(workspace.path), "exists": workspace.exists(),
                      "freeGiB": round(workspace_free, 1),
                      "cleanLevel": cfg.get("workspace.clean")},
        "drop": {"path": str(drop_root), "freeGiB": round(drop_free, 1),
                 "buildFolders": builds_on_disk,
                 "retain": cfg.get("artifacts.retain_builds")},
        "logs": str(cfg.log_root),
        "pollSeconds": cfg.get("triggers.poll_seconds"),
        "poll": bool(cfg.get("triggers.poll", True)),
        "tagPattern": cfg.get("triggers.tag_pattern"),
        "platformsDefault": cfg.default_platforms(),
        "platformsEnabled": cfg.enabled_platforms(),
        "signing": "release-keystore" if cfg.get("signing.android.keystore") else "debug",
        "androidEnabled": "Android" in cfg.enabled_platforms(),
        "lastBuild": recent[0] if recent else None,
        "warnings": warnings,
    }


def _artifact_url(cfg, path):
    """Dashboard download URL for a file in the drop folder, or None."""
    try:
        relative = Path(path).resolve().relative_to(Path(cfg.drop_root).resolve())
    except (ValueError, OSError):
        return None
    return "/artifacts/" + urllib.parse.quote(relative.as_posix())


def _build_manifest(row):
    """The per-platform manifest: the file on disk if present, else the DB copy."""
    path = row.get("manifest_path")
    if path and Path(path).is_file():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        return json.loads(row.get("manifest_json") or "{}")
    except ValueError:
        return {}


# What people actually download, most useful first.
KIND_ORDER = {"archive": 0, "apk": 1, "aab": 2, "obb": 3, "installer": 4,
              "executable": 5, "symbols": 8}


def deliverables(cfg, row, manifest=None):
    """The build's real outputs, from its manifest -- not every staged file."""
    manifest = manifest if manifest is not None else _build_manifest(row)
    drop = row.get("drop_path")
    out = []
    for artifact in manifest.get("artifacts") or []:
        full = Path(artifact.get("path") or (Path(drop) / artifact["relativePath"]
                                             if drop else artifact["relativePath"]))
        out.append({
            "name": artifact["name"],
            "type": artifact.get("type", "other"),
            "sizeBytes": artifact.get("sizeBytes", 0),
            "size": human_size(artifact.get("sizeBytes", 0)),
            "sha256": artifact.get("sha256", ""),
            "url": _artifact_url(cfg, full) if full.is_file() else None,
        })
    out.sort(key=lambda a: (KIND_ORDER.get(a["type"], 6), a["name"].lower()))
    return out


def primary_downloads(cfg, row):
    """The one-click downloads for a builds-table row: the zip, and the APK."""
    if row.get("status") != "success":
        return []
    return [{"name": d["name"], "kind": "zip" if d["type"] == "archive" else d["type"].upper(),
             "size": d["size"], "url": d["url"]}
            for d in deliverables(cfg, row) if d["type"] in ("archive", "apk") and d["url"]]


def drop_tree(cfg, row, limit_per_group=400):
    """The build's drop folder grouped by top-level entry, for the "all files" view."""
    drop = row.get("drop_path")
    target = _safe_under(cfg.drop_root, Path(drop).resolve().relative_to(
        Path(cfg.drop_root).resolve())) if drop else None
    if target is None or not target.is_dir():
        return {"groups": [], "fileCount": 0, "sizeBytes": 0}
    groups = {}
    total_files = total_bytes = 0
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(target)
        key = relative.parts[0] if len(relative.parts) > 1 else ""
        size = path.stat().st_size
        group = groups.setdefault(key, {"name": key or "(top level)", "files": [],
                                        "fileCount": 0, "sizeBytes": 0})
        group["fileCount"] += 1
        group["sizeBytes"] += size
        total_files += 1
        total_bytes += size
        if len(group["files"]) < limit_per_group:
            group["files"].append({"path": relative.as_posix(), "size": human_size(size),
                                   "url": _artifact_url(cfg, path)})
    ordered = sorted(groups.values(), key=lambda g: (g["name"] != "(top level)", g["name"].lower()))
    for group in ordered:
        group["size"] = human_size(group["sizeBytes"])
        group["truncated"] = group["fileCount"] > len(group["files"])
    return {"groups": ordered, "fileCount": total_files, "sizeBytes": total_bytes,
            "size": human_size(total_bytes)}


def build_detail(cfg, state, build_id):
    row = state.build(build_id)
    if not row:
        return None
    manifest = _build_manifest(row)
    row.pop("manifest_json", None)
    newer = [b for b in state.recent_builds(500)
             if b["id"] > row["id"] and b.get("drop_path") and b["drop_path"] == row.get("drop_path")]
    superseded_by = min(b["build_number"] for b in newer) if newer else None
    uat = manifest.get("uat") or {}
    source = manifest.get("source") or {}
    engine = manifest.get("engine") or {}
    target = manifest.get("target") or {}
    return {
        "build": row,
        "deliverables": [dict(d, url=None) for d in deliverables(cfg, row, manifest)]
                        if superseded_by else deliverables(cfg, row, manifest),
        "tree": {"groups": [], "fileCount": 0, "sizeBytes": 0} if superseded_by
                else drop_tree(cfg, row),
        "supersededBy": superseded_by,
        "facts": {
            "engine": engine.get("version") or "",
            "distribution": target.get("distribution"),
            "signing": target.get("signing"),
            "architectures": target.get("architectures") or [],
            "subject": source.get("subject") or "",
            "author": source.get("authorName") or "",
            "branches": source.get("branchesContaining") or [],
            "errorLines": uat.get("errorLines"),
            "warningLines": uat.get("warningLines"),
        },
        "logUrl": "/logs/%d" % row["id"] if row.get("log_path") else None,
    }


def overview(cfg, state, limit=25):
    allowed = bool(cfg.get("server.allow_actions", True))
    token_env = cfg.get("server.admin_token_env") or "the admin token variable"
    has_token = bool(cfg.secret("server.admin_token_env"))
    if not allowed:
        reason = "server.allow_actions is false in %s" % cfg.path.name
    elif not has_token:
        # The browser's token box cannot help here: the runner has no token to
        # compare against, so say so rather than silently greying the buttons.
        reason = ("%s is empty in secrets.env on the runner -- set it there and "
                  "restart the runner" % token_env)
    else:
        reason = ""
    builds = state.recent_builds(limit)
    newest_in = {}
    for row in builds:              # newest first
        drop = row.get("drop_path")
        if drop and drop in newest_in:
            # A rebuild of the same tag reuses the drop folder, so this build's
            # files were overwritten by the newer one.
            row["downloads"] = []
            row["supersededBy"] = newest_in[drop]
            continue
        if drop:
            newest_in[drop] = row["build_number"]
        try:
            row["downloads"] = primary_downloads(cfg, row)
        except Exception:          # a broken manifest must not break the page
            row["downloads"] = []
    return {
        "control": CONTROL.snapshot(),
        "health": health(cfg, state),
        "queue": state.queued_jobs(),
        "builds": builds,
        "actionsEnabled": has_token and allowed,
        "actionsDisabledReason": reason,
    }


def read_log(path, offset=0, limit=LOG_CHUNK_LIMIT):
    """Return a slice of a log file plus the offset to ask for next time."""
    path = Path(path)
    if not path.exists():
        return {"text": "", "offset": 0, "size": 0, "missing": True}
    size = path.stat().st_size
    if offset > size:          # rotated or replaced between polls
        offset = 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        raw = handle.read(limit)
    return {
        "text": raw.decode("utf-8", "replace"),
        "offset": offset + len(raw),
        "size": size,
        "truncated": offset + len(raw) < size,
        "missing": False,
    }


def list_artifacts(cfg, folder):
    """Every file in one build's drop folder, newest build first."""
    target = _safe_under(cfg.drop_root, folder)
    if target is None or not target.is_dir():
        return None
    entries = []
    for path in sorted(target.rglob("*")):
        if path.is_file():
            relative = path.relative_to(cfg.drop_root).as_posix()
            entries.append({
                "name": path.name,
                "relativePath": relative,
                "sizeBytes": path.stat().st_size,
                "size": human_size(path.stat().st_size),
                "url": "/artifacts/" + urllib.parse.quote(relative),
            })
    return entries


# ---------------------------------------------------------------- actions
class ActionError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def _tree_size(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0


def _strictly_under(root, path):
    """``path`` resolved, if it lies strictly inside ``root``; else None."""
    if not path:
        return None
    root = Path(root).resolve()
    candidate = Path(path).resolve()
    return candidate if root in candidate.parents else None


def plan_delete(cfg, state, build_ids):
    """What deleting these builds would remove. Nothing is touched here.

    Rebuilds of a tag share one drop folder and one log file, so files are
    only removed once no remaining build points at them.
    """
    ids = {int(i) for i in build_ids}
    everything = state.all_builds()
    targets = [b for b in everything if b["id"] in ids]
    if not targets:
        raise ActionError(404, "no such build")
    running = (CONTROL.snapshot().get("running") or {}).get("buildNumber")
    for b in targets:
        if b["status"] == "running" or b["build_number"] == running:
            raise ActionError(409, "build #%d is still running - cancel it first" % b["build_number"])
    remaining = [b for b in everything if b["id"] not in ids]
    kept_drops = {b["drop_path"] for b in remaining if b.get("drop_path")}
    kept_logs = {b["log_path"] for b in remaining if b.get("log_path")}
    kept_tags = {b["tag"] for b in remaining}

    paths = []
    def add(path, root):
        safe = _strictly_under(root, path)
        if safe and safe.exists() and safe not in paths:
            paths.append(safe)

    for b in targets:
        if b.get("drop_path") and b["drop_path"] not in kept_drops:
            add(b["drop_path"], cfg.drop_root)
        if b.get("log_path") and b["log_path"] not in kept_logs:
            uat = Path(b["log_path"])
            add(uat, cfg.log_root)
            add(uat.with_name(uat.name.replace("-uat.log", "-build.log")), cfg.log_root)
    # Last build of a tag gone: its combined manifest, checksums and log folder go too.
    for tag in {b["tag"] for b in targets} - kept_tags:
        slug = tag.replace("+", "_")
        add(Path(cfg.drop_root) / slug, cfg.drop_root)
        add(Path(cfg.log_root) / slug, cfg.log_root)
    # A folder that is going covers anything inside it.
    paths = [p for p in paths if not any(o in p.parents for o in paths)]
    size = sum(_tree_size(p) for p in paths)
    return {
        "builds": [{"id": b["id"], "buildNumber": b["build_number"], "tag": b["tag"],
                    "platform": b["platform"]} for b in targets],
        "paths": [str(p) for p in paths],
        "sizeBytes": size,
        "size": human_size(size),
        "tagsRemoved": sorted({b["tag"] for b in targets} - kept_tags),
    }


def _drop_index_entries(cfg, tags):
    index = Path(cfg.drop_root) / "index.json"
    if not tags or not index.exists():
        return
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        data["builds"] = [e for e in data.get("builds", []) if e.get("tag") not in tags]
        index.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass


def delete_builds(cfg, state, logger, build_ids):
    plan = plan_delete(cfg, state, build_ids)
    failed = []
    for path in plan["paths"]:
        target = Path(path)
        # Re-check at the last moment; the plan came from the same rules, but
        # this is the line that actually deletes.
        root = cfg.drop_root if _strictly_under(cfg.drop_root, target) else cfg.log_root
        if not _strictly_under(root, target):
            failed.append(path)
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError as exc:
            logger.warning("could not delete %s: %s", path, exc)
            failed.append(path)
    state.delete_builds([b["id"] for b in plan["builds"]])
    _drop_index_entries(cfg, plan["tagsRemoved"])
    logger.info("deleted builds %s from the dashboard (%s freed)",
                ", ".join("#%d" % b["buildNumber"] for b in plan["builds"]), plan["size"])
    plan["failed"] = failed
    return plan


def do_action(cfg, state, logger, name, payload):
    if name == "cancel":
        if CONTROL.cancel("dashboard"):
            logger.warning("cancel requested from the dashboard")
            return {"cancelled": True}
        raise ActionError(409, "nothing is building right now")

    if name == "pause":
        paused = CONTROL.set_paused(bool(payload.get("paused", True)))
        logger.info("runner %s from the dashboard", "paused" if paused else "resumed")
        return {"paused": paused}

    if name in ("build", "retry"):
        tag = (payload.get("tag") or "").strip()
        if name == "retry" and payload.get("buildId"):
            rows = [b for b in state.recent_builds(200)
                    if str(b["id"]) == str(payload["buildId"])]
            if not rows:
                raise ActionError(404, "no such build")
            tag = rows[0]["tag"]
        if not tag:
            raise ActionError(400, "a tag is required")
        platforms = payload.get("platforms") or None
        if platforms:
            platforms = [p for p in platforms if p in cfg.enabled_platforms()]
        try:
            spec = tagspec.parse(tag, cfg, platform_override=platforms)
        except tagspec.TagRejected as exc:
            raise ActionError(400, str(exc)) from exc
        job_id = state.enqueue(tag, None, source="dashboard", platforms=platforms)
        if not job_id:
            raise ActionError(409, "%s is already queued or running" % tag)
        logger.info("queued %s from the dashboard (%s)", tag, ", ".join(spec.platforms))
        return {"queued": tag, "jobId": job_id, "platforms": spec.platforms,
                "configuration": spec.configuration}

    if name == "delete":
        ids = payload.get("buildIds") or []
        if not isinstance(ids, list) or not ids:
            raise ActionError(400, "buildIds is required")
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError) as exc:
            raise ActionError(400, "buildIds must be numbers") from exc
        if payload.get("dryRun"):
            return plan_delete(cfg, state, ids)
        return delete_builds(cfg, state, logger, ids)

    raise ActionError(404, "unknown action")


# ---------------------------------------------------------------- routing
def handle_get(cfg, state, logger, path, query, send):
    """Return True if this GET was a dashboard route."""
    if path == "/":
        send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
    elif path == "/api/overview":
        limit = int(query.get("limit", ["25"])[0])
        send(200, json.dumps(overview(cfg, state, limit)).encode("utf-8"))
    elif path == "/api/log":
        try:
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            # The page's first poll can send "undefined" before it has an offset.
            offset = 0
        build_id = query.get("build", [""])[0]
        if build_id == "live":
            log_path = CONTROL.snapshot().get("running", {})
            log_path = (log_path or {}).get("logPath")
        else:
            rows = [b for b in state.recent_builds(200) if str(b["id"]) == build_id]
            log_path = rows[0]["log_path"] if rows else None
        if not log_path:
            send(404, json.dumps({"error": "no log for that build"}).encode("utf-8"))
        else:
            if query.get("tail", [""])[0] and Path(log_path).exists():
                # Failures are at the end of a UAT log, not the start.
                offset = max(0, Path(log_path).stat().st_size - LOG_CHUNK_LIMIT)
            send(200, json.dumps(read_log(log_path, offset)).encode("utf-8"))
    elif path == "/api/build":
        try:
            detail = build_detail(cfg, state, int(query.get("id", ["0"])[0]))
        except ValueError:
            detail = None
        if detail is None:
            send(404, json.dumps({"error": "no such build"}).encode("utf-8"))
        else:
            send(200, json.dumps(detail).encode("utf-8"))
    elif path.startswith("/logs/"):
        try:
            row = state.build(int(path[len("/logs/"):]))
        except ValueError:
            row = None
        log_path = Path(row["log_path"]) if row and row.get("log_path") else None
        if not log_path or not log_path.is_file():
            send(404, json.dumps({"error": "no log for that build"}).encode("utf-8"))
        else:
            send(200, log_path, "text/plain; charset=utf-8",
                 download_name="build-%d-%s" % (row["build_number"], log_path.name))
    elif path == "/api/daemon-log":
        send(200, json.dumps(read_log(
            cfg.log_root / "smackcicd.log",
            max(0, (cfg.log_root / "smackcicd.log").stat().st_size - 20000)
            if (cfg.log_root / "smackcicd.log").exists() else 0)).encode("utf-8"))
    elif path == "/api/artifacts":
        entries = list_artifacts(cfg, query.get("folder", [""])[0])
        if entries is None:
            send(404, json.dumps({"error": "no such build folder"}).encode("utf-8"))
        else:
            send(200, json.dumps({"files": entries}).encode("utf-8"))
    elif path.startswith("/artifacts/"):
        return _send_file(cfg, path[len("/artifacts/"):], send)
    elif path == "/status":                       # kept for scripts
        send(200, json.dumps({
            "queue": state.queued_jobs(),
            "running": (CONTROL.snapshot().get("running") or {}).get("tag"),
            "builds": state.recent_builds(20),
        }, indent=2).encode("utf-8"))
    elif path == "/healthz":
        snapshot = CONTROL.snapshot()
        send(200, json.dumps({"ok": True, "paused": snapshot["paused"],
                              "uptimeSeconds": snapshot["uptimeSeconds"]}).encode("utf-8"))
    else:
        return False
    return True


def _send_file(cfg, relative, send):
    target = _safe_under(cfg.drop_root, urllib.parse.unquote(relative))
    if target is None or not target.is_file():
        send(404, json.dumps({"error": "not found"}).encode("utf-8"))
        return True
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    send(200, target, ctype, download_name=target.name)
    return True


def authorised(cfg, headers):
    token = cfg.secret("server.admin_token_env")
    if not token:
        return False
    supplied = (headers.get("X-Smackcicd-Token")
                or (headers.get("Authorization") or "").replace("Bearer ", "", 1))
    return supplied.strip() == token


# ---------------------------------------------------------------- the page
# The page is a static shell: every value arrives as JSON, which keeps all
# escaping on the client. It lives in web/index.html so it can be edited as HTML.
PAGE = resources.files("smackcicd").joinpath("web/index.html").read_text(encoding="utf-8")
