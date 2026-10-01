# SPDX-License-Identifier: GPL-3.0-or-later
"""Publishing: commit status, releases, asset upload and the finish notification."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .forge import ForgeError
from .manifest import release_notes
from .util import human_size

DEFAULT_UPLOAD = ["apk", "aab", "manifest", "checksums"]
EXTRA_TYPES = {"manifest.json": "manifest", "SHA256SUMS.txt": "checksums"}


def dashboard_url(cfg):
    """Where people reach this runner's dashboard, for download links."""
    configured = (cfg.get("server.public_url") or "").strip()
    if configured:
        return configured.rstrip("/")
    port = int(cfg.get("server.port", 9099))
    # The address this machine uses to reach the forge is one the forge's
    # users can very likely reach it on too.
    from .forge import parse_remote
    remote = parse_remote(cfg.get("repo.url")) or {}
    target = remote.get("host") or "192.0.2.1"
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((target, 9))
            address = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        address = socket.gethostname()
    return "http://%s:%d" % (address, port)


def set_commit_status(cfg, forge, sha, state, description, target_url="", logger=None):
    """Report build state on the tagged commit. Never fatal."""
    if not cfg.get("publish.commit_status", True) or not sha or not forge.token:
        return None
    try:
        return forge.set_commit_status(sha, state, cfg.get("publish.status_context", "smackcicd"),
                                       description=description, target_url=target_url)
    except ForgeError as exc:
        if logger:
            logger.warning("could not set commit status: %s", exc)
        return None


def upload_candidates(cfg, per_platform, extra_files):
    allowed = cfg.get("publish.upload")
    allowed = DEFAULT_UPLOAD if allowed is None else allowed
    seen, items = set(), []
    for entry in per_platform:
        label = entry["target"].get("label") or entry["target"]["platform"]
        for artifact in entry["artifacts"]:
            if artifact["type"] not in allowed:
                continue
            name = "%s-%s" % (label, artifact["name"])
            if name not in seen:
                seen.add(name)
                items.append((name, Path(artifact["path"]), artifact["sizeBytes"]))
    for path in extra_files:
        path = Path(path)
        if path.exists() and EXTRA_TYPES.get(path.name, "other") in allowed:
            items.append((path.name, path, path.stat().st_size))
    return items


def publish_release(cfg, forge, spec, combined, per_platform, extra_files, logger):
    """Create or refresh the forge release for this tag and attach artifacts."""
    result = {"released": False, "releaseUrl": "", "uploaded": [], "skipped": []}
    if not cfg.get("publish.release", True):
        return result
    if not forge.token:
        logger.warning("no forge token (%s); skipping the release", cfg.get("repo.token_env"))
        result["skipped"].append("no token")
        return result

    drop_folder = str(cfg.drop_root / spec.slug())
    base_url = dashboard_url(cfg) if cfg.get("publish.download_links", True) else ""
    body = release_notes(combined, drop_folder, base_url=base_url)
    title = "%s (%s)" % (spec.semver, spec.channel)
    try:
        release = forge.get_release(spec.tag)
        if release:
            logger.info("updating release %s", spec.tag)
            release = forge.update_release(release, title, body, spec.is_prerelease)
        else:
            logger.info("creating release %s", spec.tag)
            release = forge.create_release(spec.tag, title, body, spec.is_prerelease)
    except ForgeError as exc:
        logger.error("release create/update failed: %s", exc)
        result["skipped"].append(str(exc))
        return result
    result["released"] = True
    result["releaseUrl"] = release.get("url", "")

    existing = {a["name"]: a["id"] for a in release.get("assets") or []}
    limit = int(cfg.get("publish.max_upload_mb", 0) or 0) * 1024 * 1024
    cap = forge.max_asset_bytes
    for name, path, size in upload_candidates(cfg, per_platform, extra_files):
        too_big = (limit and size > limit) or (cap and size > cap)
        if too_big:
            logger.warning("not attaching %s (%s): over the %s limit; it is linked from the "
                           "release notes instead", name, human_size(size),
                           forge.label if cap and size > cap else "max_upload_mb")
            result["skipped"].append("%s (too large)" % name)
            continue
        if name in existing:
            try:
                forge.delete_asset(release, existing[name])
            except ForgeError as exc:
                logger.debug("could not remove old asset %s: %s", name, exc)
        try:
            logger.info("uploading %s (%s)", name, human_size(size))
            forge.upload_asset(release, path, name)
            result["uploaded"].append(name)
        except ForgeError as exc:
            hint = ""
            if exc.status in (413, 422) or (exc.status == 500 and size > 4 * 1024 * 1024):
                hint = " -- the forge may cap attachment size (Gitea: [attachment] MAX_SIZE)"
            logger.error("upload of %s failed: %s%s", name, exc, hint)
            result["skipped"].append("%s (%s)" % (name, exc.status or "network"))
    return result


def notify(cfg, payload, logger):
    """Optional POST when a job finishes. Never fatal."""
    url = (cfg.get("notify.url") or "").strip()
    if not url:
        return False
    kind = (cfg.get("notify.kind") or "generic").lower()
    text = payload.get("summary", "build finished")
    if payload.get("release"):
        text += " - " + payload["release"]
    if kind in ("slack", "teams"):
        body = {"text": text}
    elif kind == "discord":
        body = {"content": text}
    else:
        body = payload
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20):
            pass
        return True
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("finish notification to %s failed: %s",
                       urllib.parse.urlparse(url).hostname, exc)
        return False
