# SPDX-License-Identifier: GPL-3.0-or-later
"""The daemon: watch for new tags, queue them, build them one at a time.

Two triggers feed one queue. The forge webhook starts a build within a second
of the push; the poller is the safety net for anything the webhook missed
(runner restarted, network blip, webhook misconfigured).

One HTTP server carries both the webhook route and the dashboard.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import dashboard as dash
from . import runner as runner_mod
from . import tagspec
from .control import CONTROL
from .forge import ForgeError, event_name, verify_signature, webhook_tags
from .forge import from_config as forge_from_config
from .logging_setup import get_logger
from .util import run

DOWNLOAD_CHUNK = 1024 * 1024


class TagSource:
    """Lists remote tags, preferring the forge API and falling back to git."""

    def __init__(self, cfg, logger, forge=None):
        self.cfg = cfg
        self.log = logger
        self.forge = forge or forge_from_config(cfg)

    def list_tags(self):
        try:
            return self.forge.list_tags()
        except ForgeError as exc:
            self.log.debug("tag API unavailable (%s); using git ls-remote", exc)
        from .workspace import Workspace
        lines = []
        command = ["git"] + Workspace(self.cfg, self.log)._auth_args() + [
            "ls-remote", "--tags", self.cfg.remote_url]
        run(command, on_line=lines.append, check=True)
        found = {}
        for line in lines:
            parts = line.split("\t")
            if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
                continue
            name = parts[1][len("refs/tags/"):]
            peeled = name.endswith("^{}")
            name = name[:-3] if peeled else name
            if peeled or name not in found:
                found[name] = parts[0]
        return list(found.items())


class Poller(threading.Thread):
    def __init__(self, cfg, state, logger, stop_event):
        super().__init__(name="smackcicd-poller", daemon=True)
        self.cfg = cfg
        self.state = state
        self.log = logger
        self.stop_event = stop_event
        self.source = TagSource(cfg, logger)
        self.pattern = re.compile(cfg.get("triggers.tag_pattern") or r"^v\d+\.\d+\.\d+")

    def run(self):
        interval = max(10, int(self.cfg.get("triggers.poll_seconds", 60)))
        first_pass = self.state.seen_tag_count() == 0
        while not self.stop_event.is_set():
            try:
                self.scan(first_pass)
                CONTROL.note_poll()
                first_pass = False
            except Exception as exc:
                self.log.exception("tag poll failed")
                CONTROL.note_poll(error=str(exc)[:200])
            self.stop_event.wait(interval)

    def scan(self, first_pass=False):
        queued = []
        for name, sha in self.source.list_tags():
            if self.state.has_seen_tag(name):
                continue
            self.state.mark_tag_seen(name, sha)
            if not self.pattern.search(name):
                self.log.debug("ignoring tag %s (does not match the pattern)", name)
                continue
            if first_pass and not self.cfg.get("triggers.build_backlog_on_first_run", False):
                self.log.info("baseline: recording existing tag %s without building it", name)
                continue
            if self.state.enqueue(name, sha, source="poll"):
                self.log.info("queued %s (found by poll)", name)
                queued.append(name)
        return queued


class Worker(threading.Thread):
    def __init__(self, cfg, state, logger, stop_event):
        super().__init__(name="smackcicd-worker", daemon=True)
        self.cfg = cfg
        self.state = state
        self.log = logger
        self.stop_event = stop_event

    def run(self):
        closed = self.state.close_stale_builds()
        if closed:
            self.log.warning("marked %d build(s) interrupted by a previous run", closed)
        requeued = self.state.requeue_stale_jobs()
        if requeued:
            self.log.info("requeued %d job(s) interrupted by a previous run", requeued)
        while not self.stop_event.is_set():
            if CONTROL.paused:
                self.stop_event.wait(5)
                continue
            job = self.state.claim_next_job()
            if not job:
                self.stop_event.wait(5)
                continue
            try:
                runner_mod.run_job(self.cfg, self.state, job, self.log)
            except Exception:
                self.log.error("job %s ended with an error; continuing", job["tag"])


def handle_webhook(cfg, state, logger, headers, body):
    """(status, response dict) for one webhook delivery."""
    secret = cfg.secret("server.webhook_secret_env")
    if secret and not verify_signature(headers, body, secret):
        logger.warning("rejected a webhook with a missing or bad signature")
        return 401, {"error": "bad signature"}
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "invalid json"}
    event = event_name(headers)
    if event == "ping":
        return 200, {"pong": True}
    pattern = re.compile(cfg.get("triggers.tag_pattern") or r"^v\d+\.\d+\.\d+")
    queued = []
    for name, sha in webhook_tags(event, payload):
        state.mark_tag_seen(name, sha)
        if not pattern.search(name):
            logger.info("webhook: ignoring tag %s (does not match the pattern)", name)
            continue
        try:
            tagspec.parse(name, cfg)
        except tagspec.TagRejected as exc:
            logger.info("webhook: ignoring tag %s (%s)", name, exc)
            continue
        if state.enqueue(name, sha, source="webhook"):
            logger.info("queued %s (webhook %s)", name, event or "push")
            queued.append(name)
        else:
            logger.info("webhook: %s is already queued or running", name)
    return 200, {"queued": queued}


def _make_handler(cfg, state, logger):
    hook_path = (cfg.get("server.webhook_path") or "/webhook").rstrip("/")
    webhook_on = cfg.get("server.webhook", True)
    dashboard_on = cfg.get("server.dashboard", True)

    class Handler(BaseHTTPRequestHandler):
        server_version = "smackcicd"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            logger.debug("http %s - %s", self.address_string(), fmt % args)

        def _send(self, code, body, content_type="application/json", download_name=None):
            """Body is bytes, str, or a Path to stream from disk."""
            if isinstance(body, Path):
                size = body.stat().st_size
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                if download_name:
                    self.send_header("Content-Disposition",
                                     'attachment; filename="%s"' % download_name)
                self.end_headers()
                with open(body, "rb") as handle:
                    while True:
                        chunk = handle.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            payload = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/healthz":
                snapshot = CONTROL.snapshot()
                self._send(200, json.dumps({"ok": True, "paused": snapshot["paused"],
                                            "uptimeSeconds": snapshot["uptimeSeconds"]}))
                return
            if not dashboard_on:
                self._send(404, json.dumps({"error": "the dashboard is disabled"}))
                return
            try:
                if not dash.handle_get(cfg, state, logger, path, query, self._send):
                    self._send(404, json.dumps({"error": "not found"}))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as exc:
                logger.exception("dashboard GET %s failed", path)
                self._send(500, json.dumps({"error": str(exc)}))

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if webhook_on and path == hook_path:
                status, response = handle_webhook(cfg, state, logger, dict(self.headers), body)
                self._send(status, json.dumps(response))
                return
            if dashboard_on and path.startswith("/api/actions/"):
                self._action(path[len("/api/actions/"):], body)
                return
            self._send(404, json.dumps({"error": "not found"}))

        def _action(self, name, body):
            if not cfg.get("server.allow_actions", True):
                self._send(403, json.dumps({"error": "actions are disabled in the config"}))
                return
            if not dash.authorised(cfg, self.headers):
                logger.warning("rejected %s action from %s (bad or missing token)",
                               name, self.address_string())
                self._send(401, json.dumps({"error": "a valid admin token is required"}))
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._send(400, json.dumps({"error": "invalid json"}))
                return
            try:
                self._send(200, json.dumps(dash.do_action(cfg, state, logger, name, payload)))
            except dash.ActionError as exc:
                self._send(exc.status, json.dumps({"error": str(exc)}))
            except Exception as exc:
                logger.exception("action %s failed", name)
                self._send(500, json.dumps({"error": str(exc)}))

    return Handler


def serve(cfg, state, logger):
    """Start the HTTP server for the webhook and dashboard."""
    if not cfg.get("server.enabled", True):
        logger.info("HTTP server disabled (server.enabled = false)")
        return None
    host = cfg.get("server.host", "0.0.0.0")
    port = int(cfg.get("server.port", 9099))
    httpd = ThreadingHTTPServer((host, port), _make_handler(cfg, state, logger))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="smackcicd-http", daemon=True).start()
    shown = "localhost" if host in ("0.0.0.0", "", "::") else host
    if cfg.get("server.dashboard", True):
        logger.info("dashboard: http://%s:%d/", shown, port)
    if cfg.get("server.webhook", True):
        logger.info("webhook: http://%s:%d%s", shown, port, cfg.get("server.webhook_path"))
        if not cfg.secret("server.webhook_secret_env"):
            logger.warning("webhook secret is empty -- anyone who can reach this port can "
                           "queue builds; set %s", cfg.get("server.webhook_secret_env"))
    if cfg.get("server.allow_actions", True) and not cfg.secret("server.admin_token_env"):
        logger.warning("dashboard is read-only: %s is empty", cfg.get("server.admin_token_env"))
    return httpd


def watch(cfg, state, logger=None):
    """Run the daemon until interrupted."""
    logger = logger or get_logger("watch")
    stop_event = threading.Event()
    worker = Worker(cfg, state, logger, stop_event)
    httpd = serve(cfg, state, logger)
    worker.start()
    if cfg.get("triggers.poll", True):
        Poller(cfg, state, logger, stop_event).start()
    for warning in cfg.warnings:
        logger.warning("config: %s", warning)
    logger.info("watching %s for tags matching %s", cfg.remote_url or "(no repo.url)",
                cfg.get("triggers.tag_pattern"))
    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        stop_event.set()
        if httpd:
            httpd.shutdown()
        worker.join(timeout=5)
    return 0
