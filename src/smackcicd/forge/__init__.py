# SPDX-License-Identifier: GPL-3.0-or-later
"""Forge clients: Gitea/Forgejo and GitHub, behind one interface."""

from __future__ import annotations

import re
import urllib.parse

from .base import Forge, NullForge, event_name, verify_signature, webhook_tags
from .gitea import Gitea
from .github import GitHub
from .http import ForgeError, Http

__all__ = ["Forge", "ForgeError", "Gitea", "GitHub", "NullForge", "detect_kind",
           "event_name", "from_config", "parse_remote", "verify_signature", "webhook_tags"]

KINDS = {"gitea": Gitea, "forgejo": Gitea, "github": GitHub, "none": NullForge}

SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


def parse_remote(url):
    """{"scheme", "host", "port", "owner", "name", "web"} from any git remote form.

    Handles https://host/owner/repo(.git), ssh://git@host:2222/owner/repo.git
    and git@host:owner/repo.git. ``web`` is the forge's web root, assuming the
    usual https on the default port when the remote is SSH.
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        parsed = urllib.parse.urlparse(url)
        scheme, host, port = parsed.scheme, parsed.hostname or "", parsed.port
        path = parsed.path
    else:
        match = SCP_LIKE.match(url)
        if not match:
            return None
        scheme, host, port, path = "ssh", match.group("host"), None, match.group("path")
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    name = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
    owner = "/".join(parts[:-1])
    prefix = "/".join(parts[:-2])
    if scheme in ("http", "https"):
        web = "%s://%s%s" % (scheme, host, ":%d" % port if port else "")
    else:
        web = "https://%s" % host
    if prefix:
        web += "/" + prefix
        owner = parts[-2]
    return {"scheme": scheme, "host": host, "port": port, "owner": owner, "name": name,
            "web": web}


def default_api_url(kind, remote):
    if not remote:
        return ""
    if kind == "github":
        if remote["host"].lower() in ("github.com", "www.github.com"):
            return "https://api.github.com"
        return remote["web"] + "/api/v3"            # GitHub Enterprise Server
    if kind == "gitea":
        return remote["web"] + "/api/v1"
    return ""


def detect_kind(url, timeout=10, verify_tls=True):
    """Best guess at the forge behind a remote: "github", "gitea" or "none".

    github.com is recognised by name; anything else is asked over HTTP, since
    a self-hosted Gitea, Forgejo or GitHub Enterprise can live at any address.
    """
    remote = parse_remote(url)
    if not remote:
        return "none"
    if remote["host"].lower() in ("github.com", "www.github.com"):
        return "github"
    http = Http(timeout=timeout, verify_tls=verify_tls)
    try:
        data = http.request("GET", remote["web"] + "/api/v1/version")
        if isinstance(data, dict) and data.get("version"):
            return "gitea"
    except ForgeError:
        pass
    try:
        http.request("GET", remote["web"] + "/api/v3/meta")
        return "github"
    except ForgeError:
        pass
    return "none"


def from_config(cfg):
    """The forge client the config describes (a NullForge when there is none)."""
    url = cfg.get("repo.url")
    remote = parse_remote(url)
    kind = (cfg.get("repo.forge") or "auto").lower()
    if kind == "auto":
        kind = detect_kind(url, verify_tls=cfg.get("repo.verify_tls", True))
    if kind not in KINDS:
        raise ForgeError(0, "repo.forge must be gitea, github or none, not %r" % kind)
    if kind == "forgejo":
        kind = "gitea"
    cls = KINDS[kind]
    if cls is NullForge:
        return NullForge()
    return cls(
        api_url=cfg.get("repo.api_url") or default_api_url(kind, remote),
        owner=cfg.get("repo.owner") or (remote or {}).get("owner", ""),
        repo=cfg.get("repo.name") or (remote or {}).get("name", ""),
        token=cfg.secret("repo.token_env"),
        verify_tls=cfg.get("repo.verify_tls", True),
    )
