# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import hmac
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from smackcicd import forge
from smackcicd.forge.http import ForgeError


class Recorder:
    def __init__(self):
        self.requests = []
        self.routes = {}

    def route(self, method, path, status=200, body=None, headers=None):
        self.routes[(method, path)] = (status, body, headers or {})


@pytest.fixture
def server():
    rec = Recorder()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _handle(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path
            rec.requests.append({"method": method, "path": path, "headers": dict(self.headers),
                                 "body": body})
            key = (method, path.split("?")[0])
            if (method, path) in rec.routes:
                key = (method, path)
            status, payload, headers = rec.routes.get(key, (404, {"message": "nope"}, {}))
            data = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v.replace("BASE", base))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    rec.base = base
    yield rec
    httpd.shutdown()


def test_gitea_tags_paginate(server):
    page1 = [{"name": "v%d" % i, "commit": {"sha": str(i)}} for i in range(50)]
    server.route("GET", "/api/v1/repos/o/r/tags?limit=50&page=1", body=page1)
    server.route("GET", "/api/v1/repos/o/r/tags?limit=50&page=2", body=[{"name": "v99",
                                                                          "commit": {"sha": "z"}}])
    g = forge.Gitea(server.base + "/api/v1", "o", "r", token="t")
    tags = g.list_tags()
    assert len(tags) == 51 and tags[-1] == ("v99", "z")
    assert server.requests[0]["headers"]["Authorization"] == "token t"


def test_gitea_release_and_multipart_upload(server, tmp_path):
    server.route("GET", "/api/v1/repos/o/r/releases/tags/v1.0.0", status=404)
    server.route("POST", "/api/v1/repos/o/r/releases",
                 body={"id": 7, "html_url": "http://x/rel", "assets": []})
    server.route("POST", "/api/v1/repos/o/r/releases/7/assets", body={"id": 1})
    g = forge.Gitea(server.base + "/api/v1", "o", "r", token="t")
    assert g.get_release("v1.0.0") is None
    release = g.create_release("v1.0.0", "1.0.0", "notes", False)
    assert release == {"id": 7, "url": "http://x/rel", "uploadUrl": "", "assets": []}
    artifact = tmp_path / "Game.apk"
    artifact.write_bytes(b"APKDATA" * 100)
    g.upload_asset(release, artifact, "Android-Game.apk")
    upload = server.requests[-1]
    assert upload["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'filename="Android-Game.apk"' in upload["body"] and b"APKDATA" in upload["body"]


def test_github_follows_link_pagination(server):
    server.route("GET", "/repos/o/r/tags", body=[{"name": "v1", "commit": {"sha": "a"}}],
                 headers={"Link": '<BASE/repos/o/r/tags?page=2>; rel="next"'})
    server.route("GET", "/repos/o/r/tags?page=2", body=[{"name": "v2", "commit": {"sha": "b"}}])
    gh = forge.GitHub(server.base, "o", "r", token="t")
    assert gh.list_tags() == [("v1", "a"), ("v2", "b")]
    first = server.requests[0]["headers"]
    assert first["Authorization"] == "Bearer t"
    assert first["X-Github-Api-Version"] == "2022-11-28"


def test_github_uploads_raw_bytes_to_the_upload_url(server, tmp_path):
    server.route("POST", "/repos/o/r/releases", body={
        "id": 3, "html_url": "h", "upload_url": server.base + "/up/3/assets{?name,label}",
        "assets": []})
    server.route("POST", "/up/3/assets", body={"id": 9})
    gh = forge.GitHub(server.base, "o", "r", token="t")
    release = gh.create_release("v1.0.0", "t", "b", True)
    assert release["uploadUrl"] == server.base + "/up/3/assets"
    artifact = tmp_path / "manifest.json"
    artifact.write_text('{"a": 1}')
    gh.upload_asset(release, artifact, "manifest.json")
    upload = server.requests[-1]
    assert upload["path"] == "/up/3/assets?name=manifest.json"
    assert upload["body"] == b'{"a": 1}' and upload["headers"]["Content-Type"] == "application/json"
    assert gh.max_asset_bytes < 2 * 1024 ** 3


def test_a_silent_server_is_a_forge_error_not_a_crash():
    """A socket timeout once escaped as a raw TimeoutError and aborted a job."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()
    g = forge.Gitea("http://127.0.0.1:%d/api/v1" % listener.getsockname()[1], "o", "r",
                    timeout=1)
    with pytest.raises(ForgeError):
        g.list_tags()
    listener.close()


# ------------------------------------------------------------------ webhooks
def sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signatures_from_every_forge():
    body = b'{"ref": "refs/tags/v1.0.0"}'
    good = sign("s3cret", body)
    assert forge.verify_signature({"X-Hub-Signature-256": "sha256=" + good}, body, "s3cret")
    assert forge.verify_signature({"X-Gitea-Signature": good}, body, "s3cret")
    assert forge.verify_signature({"x-forgejo-signature": good}, body, "s3cret")
    assert not forge.verify_signature({"X-Gitea-Signature": "0" * 64}, body, "s3cret")
    assert not forge.verify_signature({}, body, "s3cret")


def test_webhook_events():
    assert forge.webhook_tags("create", {"ref": "v1.0.0", "ref_type": "tag", "sha": "a"}) == [
        ("v1.0.0", "a")]
    assert forge.webhook_tags("create", {"ref": "main", "ref_type": "branch"}) == []
    assert forge.webhook_tags("push", {"ref": "refs/tags/v2", "after": "b"}) == [("v2", "b")]
    assert forge.webhook_tags("push", {"ref": "refs/heads/main", "after": "b"}) == []
    assert forge.webhook_tags("release", {"action": "published",
                                          "release": {"tag_name": "v3"}}) == [("v3", "")]
    assert forge.webhook_tags("release", {"action": "deleted",
                                          "release": {"tag_name": "v3"}}) == []


def test_tag_deletions_are_ignored():
    assert forge.webhook_tags("push", {"ref": "refs/tags/v1", "deleted": True, "after": "x"}) == []
    assert forge.webhook_tags("push", {"ref": "refs/tags/v1", "after": "0" * 40}) == []


def test_event_header_from_any_forge():
    assert forge.event_name({"X-GitHub-Event": "push"}) == "push"
    assert forge.event_name({"X-Gitea-Event": "create"}) == "create"
    assert forge.event_name({}) == ""


# ------------------------------------------------------------------ remotes
@pytest.mark.parametrize("url, owner, name, web", [
    ("https://github.com/acme/game.git", "acme", "game", "https://github.com"),
    ("https://github.com/acme/game", "acme", "game", "https://github.com"),
    ("git@github.com:acme/game.git", "acme", "game", "https://github.com"),
    ("ssh://git@git.studio.lan:2222/team/proj.git", "team", "proj", "https://git.studio.lan"),
    ("http://192.168.0.10:3000/team/proj.git", "team", "proj", "http://192.168.0.10:3000"),
    ("https://code.studio.com/gitea/team/proj.git", "team", "proj",
     "https://code.studio.com/gitea"),
])
def test_parse_remote(url, owner, name, web):
    remote = forge.parse_remote(url)
    assert (remote["owner"], remote["name"], remote["web"]) == (owner, name, web)


def test_api_urls():
    gh = forge.parse_remote("https://github.com/a/b")
    ghe = forge.parse_remote("https://ghe.corp/a/b")
    gitea = forge.parse_remote("http://git.lan:3000/a/b")
    from smackcicd.forge import default_api_url
    assert default_api_url("github", gh) == "https://api.github.com"
    assert default_api_url("github", ghe) == "https://ghe.corp/api/v3"
    assert default_api_url("gitea", gitea) == "http://git.lan:3000/api/v1"


def test_detect_kind_by_host_and_probe(server):
    assert forge.detect_kind("https://github.com/a/b") == "github"
    server.route("GET", "/api/v1/version", body={"version": "1.22.0"})
    assert forge.detect_kind(server.base + "/a/b.git") == "gitea"
    assert forge.detect_kind("not a url") == "none"
