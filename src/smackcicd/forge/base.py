# SPDX-License-Identifier: GPL-3.0-or-later
"""The interface every forge client implements, plus webhook handling.

Releases are normalised to ``{"id", "url", "uploadUrl", "assets": [{"id",
"name", "size"}]}`` so publishing code never needs to know which forge it
talks to.
"""

from __future__ import annotations

import hashlib
import hmac

from .http import ForgeError

ZERO_SHA = "0" * 40


class Forge:
    kind = "none"
    label = "no forge"
    # Largest single release asset the forge accepts, or None.
    max_asset_bytes = None

    def __init__(self, api_url="", owner="", repo="", token="", timeout=120, verify_tls=True):
        self.api_url = (api_url or "").rstrip("/")
        self.owner = owner
        self.repo = repo
        self.token = token
        self.timeout = timeout
        self.verify_tls = verify_tls

    # -- identity -----------------------------------------------------------
    def describe(self):
        """A short human description for `doctor`. Raises ForgeError if unreachable."""
        return self.label

    def check_access(self):
        """Raise ForgeError unless the token can read the repository."""
        self.list_tags(limit=1)

    # -- reads --------------------------------------------------------------
    def list_tags(self, limit=None):
        """[(name, sha)]. Raises ForgeError when the API is not available."""
        raise ForgeError(0, "no forge API configured")

    # -- writes -------------------------------------------------------------
    def set_commit_status(self, sha, state, context, description="", target_url=""):
        return None

    def get_release(self, tag):
        return None

    def create_release(self, tag, title, body, prerelease):
        raise ForgeError(0, "this forge does not support releases")

    def update_release(self, release, title, body, prerelease):
        raise ForgeError(0, "this forge does not support releases")

    def delete_asset(self, release, asset_id):
        return None

    def upload_asset(self, release, path, name):
        raise ForgeError(0, "this forge does not support release assets")


class NullForge(Forge):
    """No forge API: tags come from `git ls-remote`, nothing is published."""


# ----------------------------------------------------------------- webhooks
SIGNATURE_HEADERS = ("X-Hub-Signature-256", "X-Gitea-Signature", "X-Forgejo-Signature",
                     "X-Gogs-Signature")
EVENT_HEADERS = ("X-GitHub-Event", "X-Gitea-Event", "X-Forgejo-Event", "X-Gogs-Event")


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def verify_signature(headers, body, secret):
    """Check an HMAC-SHA256 webhook signature from GitHub, Gitea or Forgejo.

    GitHub sends ``sha256=<hex>`` in X-Hub-Signature-256; Gitea and Forgejo
    send the bare hex in their own header (newer Gitea sends both).
    """
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    for name in SIGNATURE_HEADERS:
        supplied = _header(headers, name).strip()
        if not supplied:
            continue
        if supplied.lower().startswith("sha256="):
            supplied = supplied[7:]
        if hmac.compare_digest(supplied.lower(), expected):
            return True
    return False


def event_name(headers):
    for name in EVENT_HEADERS:
        value = _header(headers, name)
        if value:
            return value.lower()
    return ""


def webhook_tags(event, payload):
    """[(tag, sha)] that a webhook payload announces. Deletions are ignored.

    The three events that can carry a new tag look the same on every forge:
    ``create`` (ref_type tag), ``push`` to refs/tags/..., and ``release``.
    """
    tags = []
    ref = payload.get("ref") or ""
    if event == "create" and payload.get("ref_type") == "tag":
        tags.append((ref.split("/")[-1], payload.get("sha", "")))
    elif event == "push" or (not event and ref.startswith("refs/tags/")):
        if ref.startswith("refs/tags/") and not payload.get("deleted") \
                and payload.get("after") != ZERO_SHA:
            tags.append((ref[len("refs/tags/"):], payload.get("after", "")))
    elif event == "release":
        action = (payload.get("action") or "").lower()
        tag = (payload.get("release") or {}).get("tag_name")
        if tag and action in ("", "published", "created"):
            tags.append((tag, ""))
    return [(name, sha) for name, sha in tags if name]
