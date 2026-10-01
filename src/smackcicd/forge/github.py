# SPDX-License-Identifier: GPL-3.0-or-later
"""GitHub and GitHub Enterprise Server (REST API v3)."""

from __future__ import annotations

import mimetypes
import re
import urllib.parse
from pathlib import Path

from .base import Forge
from .http import ForgeError, Http, upload_timeout

NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHub(Forge):
    kind = "github"
    label = "GitHub"
    # GitHub rejects release assets of 2 GiB or more.
    max_asset_bytes = 2 * 1024 ** 3 - 1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http = Http(self.token, "Bearer", self.timeout, self.verify_tls, extra_headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _repo(self, suffix):
        return "%s/repos/%s/%s%s" % (self.api_url, urllib.parse.quote(self.owner),
                                     urllib.parse.quote(self.repo), suffix)

    @staticmethod
    def _release(raw):
        if not raw:
            return None
        return {
            "id": raw.get("id"),
            "url": raw.get("html_url", ""),
            # "https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}"
            "uploadUrl": (raw.get("upload_url") or "").split("{", 1)[0],
            "assets": [{"id": a.get("id"), "name": a.get("name"), "size": a.get("size")}
                       for a in raw.get("assets") or []],
        }

    # -- identity -----------------------------------------------------------
    def describe(self):
        data = self.http.request("GET", self._repo("")) or {}
        visibility = "private" if data.get("private") else "public"
        return "GitHub (%s repository %s/%s)" % (visibility, self.owner, self.repo)

    # -- reads --------------------------------------------------------------
    def list_tags(self, limit=None):
        """Every tag, following GitHub's Link pagination."""
        tags = []
        url = self._repo("/tags?per_page=100")
        for _ in range(100):
            batch, headers = self.http.request("GET", url, want_headers=True)
            tags += [(t.get("name", ""), (t.get("commit") or {}).get("sha", ""))
                     for t in batch or [] if t.get("name")]
            if limit and len(tags) >= limit:
                break
            match = NEXT_LINK.search(headers.get("Link", "") or headers.get("link", ""))
            if not match:
                break
            url = match.group(1)
        return tags[:limit] if limit else tags

    # -- writes -------------------------------------------------------------
    def set_commit_status(self, sha, state, context, description="", target_url=""):
        body = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        return self.http.request("POST", self._repo("/statuses/%s" % urllib.parse.quote(sha)),
                                 body=body)

    def get_release(self, tag):
        try:
            return self._release(self.http.request(
                "GET", self._repo("/releases/tags/%s" % urllib.parse.quote(tag, safe=""))))
        except ForgeError as exc:
            if exc.status == 404:
                return None
            raise

    def create_release(self, tag, title, body, prerelease):
        return self._release(self.http.request("POST", self._repo("/releases"), body={
            "tag_name": tag, "name": title, "body": body,
            "draft": False, "prerelease": prerelease}))

    def update_release(self, release, title, body, prerelease):
        updated = self.http.request("PATCH", self._repo("/releases/%d" % release["id"]),
                                    body={"name": title, "body": body,
                                          "prerelease": prerelease})
        return self._release(updated) or release

    def delete_asset(self, release, asset_id):
        self.http.request("DELETE", self._repo("/releases/assets/%d" % asset_id), parse=False)

    def upload_asset(self, release, path, name):
        """GitHub takes the raw bytes at the release's upload URL."""
        path = Path(path)
        size = path.stat().st_size
        upload_url = release.get("uploadUrl")
        if not upload_url:
            raise ForgeError(0, "release has no upload URL")
        with open(path, "rb") as handle:
            return self.http.request(
                "POST", upload_url, query={"name": name}, raw_body=handle,
                content_length=size,
                headers={"Content-Type": mimetypes.guess_type(name)[0]
                         or "application/octet-stream"},
                timeout=upload_timeout(self.timeout, size))
