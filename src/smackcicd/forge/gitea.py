# SPDX-License-Identifier: GPL-3.0-or-later
"""Gitea and Forgejo (API v1)."""

from __future__ import annotations

import io
import mimetypes
import urllib.parse
import uuid
from pathlib import Path

from .base import Forge
from .http import ChainedReader, ForgeError, Http, upload_timeout


class Gitea(Forge):
    kind = "gitea"
    label = "Gitea"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http = Http(self.token, "token", self.timeout, self.verify_tls)

    def _url(self, path):
        return "%s%s" % (self.api_url, path)

    def _repo(self, suffix):
        return self._url("/repos/%s/%s%s" % (urllib.parse.quote(self.owner),
                                             urllib.parse.quote(self.repo), suffix))

    @staticmethod
    def _release(raw):
        if not raw:
            return None
        return {
            "id": raw.get("id"),
            "url": raw.get("html_url", ""),
            "uploadUrl": "",
            "assets": [{"id": a.get("id"), "name": a.get("name"), "size": a.get("size")}
                       for a in raw.get("assets") or []],
        }

    # -- identity -----------------------------------------------------------
    def describe(self):
        version = (self.http.request("GET", self._url("/version")) or {}).get("version", "?")
        return "Gitea/Forgejo %s at %s" % (version, self.api_url)

    # -- reads --------------------------------------------------------------
    def list_tags(self, limit=None):
        tags, page, per_page = [], 1, 50
        while True:
            batch = self.http.request("GET", self._repo("/tags"),
                                      query={"limit": per_page, "page": page}) or []
            tags += [(t.get("name", ""), (t.get("commit") or {}).get("sha", ""))
                     for t in batch if t.get("name")]
            if len(batch) < per_page or (limit and len(tags) >= limit) or page >= 100:
                break
            page += 1
        return tags[:limit] if limit else tags

    # -- writes -------------------------------------------------------------
    def set_commit_status(self, sha, state, context, description="", target_url=""):
        return self.http.request("POST", self._repo("/statuses/%s" % urllib.parse.quote(sha)),
                                 body={"state": state, "context": context,
                                       "description": description[:255],
                                       "target_url": target_url})

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
        self.http.request("DELETE", self._repo("/releases/%d/assets/%d"
                                               % (release["id"], asset_id)), parse=False)

    def upload_asset(self, release, path, name):
        """Gitea takes a multipart form; the file is streamed, not loaded."""
        path = Path(path)
        size = path.stat().st_size
        boundary = "----smackcicd%s" % uuid.uuid4().hex
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        head = ('--%s\r\nContent-Disposition: form-data; name="attachment"; filename="%s"\r\n'
                'Content-Type: %s\r\n\r\n' % (boundary, name.replace('"', "_"), ctype)
                ).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        body = ChainedReader([io.BytesIO(head), open(path, "rb"), io.BytesIO(tail)])
        try:
            return self.http.request(
                "POST", self._repo("/releases/%d/assets" % release["id"]),
                query={"name": name}, raw_body=body,
                content_length=len(head) + size + len(tail),
                headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
                timeout=upload_timeout(self.timeout, size))
        finally:
            body.close()
