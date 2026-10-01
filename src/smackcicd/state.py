# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable runner state: seen tags, the job queue and build history."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .util import ensure_dir, iso, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_tags (
    tag        TEXT PRIMARY KEY,
    sha        TEXT,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag         TEXT NOT NULL,
    sha         TEXT,
    source      TEXT NOT NULL,
    platforms   TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    enqueued_at TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS builds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    build_number  INTEGER NOT NULL,
    job_id        INTEGER,
    tag           TEXT NOT NULL,
    platform      TEXT NOT NULL,
    configuration TEXT,
    commit_sha    TEXT,
    status        TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    duration_s    INTEGER,
    manifest_path TEXT,
    log_path      TEXT,
    drop_path     TEXT,
    error         TEXT,
    manifest_json TEXT
);
CREATE INDEX IF NOT EXISTS builds_tag ON builds(tag);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class State:
    def __init__(self, path):
        self.path = Path(path)
        ensure_dir(self.path.parent)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def _write(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _read(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # -- tags ----------------------------------------------------------
    def has_seen_tag(self, tag):
        return bool(self._read("SELECT 1 FROM seen_tags WHERE tag=?", (tag,)))

    def mark_tag_seen(self, tag, sha=None):
        self._write(
            "INSERT OR IGNORE INTO seen_tags(tag, sha, first_seen) VALUES(?,?,?)",
            (tag, sha, iso()),
        )

    def seen_tag_count(self):
        return self._read("SELECT COUNT(*) AS n FROM seen_tags")[0]["n"]

    # -- queue ---------------------------------------------------------
    def enqueue(self, tag, sha=None, source="poll", platforms=None):
        rows = self._read(
            "SELECT id FROM jobs WHERE tag=? AND status IN ('queued','running')", (tag,)
        )
        if rows:
            return None
        cur = self._write(
            "INSERT INTO jobs(tag, sha, source, platforms, status, enqueued_at)"
            " VALUES(?,?,?,?, 'queued', ?)",
            (tag, sha, source, json.dumps(platforms) if platforms else None, iso()),
        )
        return cur.lastrowid

    def claim_next_job(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?",
                (iso(), row["id"]),
            )
            self._conn.commit()
            job = dict(row)
            job["status"] = "running"
            return job

    def finish_job(self, job_id, status, error=None):
        self._write(
            "UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
            (status, iso(), error, job_id),
        )

    def requeue_stale_jobs(self):
        cur = self._write(
            "UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'"
        )
        return cur.rowcount

    def close_stale_builds(self):
        """Builds left 'running' by a daemon that died under them.

        Nothing will ever finish them, so without this they show as running on
        the dashboard forever.  Their job is requeued separately, so the work
        itself is retried; this only corrects the history.
        """
        cur = self._write(
            "UPDATE builds SET status='interrupted', finished_at=?,"
            " error=COALESCE(error, 'the runner stopped while this build was in progress')"
            " WHERE status='running'", (iso(),))
        return cur.rowcount

    def queued_jobs(self):
        return [dict(r) for r in self._read(
            "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id")]

    # -- builds --------------------------------------------------------
    def next_build_number(self):
        with self._lock:
            self._conn.execute(
                "INSERT INTO counters(name, value) VALUES('build_number', 1)"
                " ON CONFLICT(name) DO UPDATE SET value=value+1"
            )
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name='build_number'"
            ).fetchone()
            self._conn.commit()
            return int(row["value"])

    def start_build(self, build_number, job_id, tag, platform, configuration, commit_sha):
        cur = self._write(
            "INSERT INTO builds(build_number, job_id, tag, platform, configuration,"
            " commit_sha, status, started_at) VALUES(?,?,?,?,?,?, 'running', ?)",
            (build_number, job_id, tag, platform, configuration, commit_sha, iso()),
        )
        return cur.lastrowid

    def finish_build(self, build_id, status, started, manifest=None, manifest_path=None,
                     log_path=None, drop_path=None, error=None):
        duration = int((utc_now() - started).total_seconds()) if started else None
        self._write(
            "UPDATE builds SET status=?, finished_at=?, duration_s=?, manifest_path=?,"
            " log_path=?, drop_path=?, error=?, manifest_json=? WHERE id=?",
            (
                status,
                iso(),
                duration,
                str(manifest_path) if manifest_path else None,
                str(log_path) if log_path else None,
                str(drop_path) if drop_path else None,
                error,
                json.dumps(manifest) if manifest else None,
                build_id,
            ),
        )
        return duration

    def recent_builds(self, limit=25):
        return [dict(r) for r in self._read(
            "SELECT id, build_number, job_id, tag, platform, configuration, commit_sha,"
            " status, started_at, finished_at, duration_s, drop_path, log_path,"
            " manifest_path, error FROM builds ORDER BY id DESC LIMIT ?", (limit,))]

    def all_builds(self):
        return [dict(r) for r in self._read(
            "SELECT id, build_number, job_id, tag, platform, status, drop_path, log_path"
            " FROM builds ORDER BY id")]

    def delete_builds(self, build_ids):
        """Forget these build rows, and any finished job left with no builds.

        seen_tags is deliberately untouched: forgetting a tag there would make
        the poller treat it as new and build it again.
        """
        ids = sorted({int(i) for i in build_ids})
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        job_ids = {r["job_id"] for r in self._read(
            "SELECT job_id FROM builds WHERE id IN (%s) AND job_id IS NOT NULL" % marks, ids)}
        deleted = self._write("DELETE FROM builds WHERE id IN (%s)" % marks, ids).rowcount
        for job_id in job_ids:
            self._write(
                "DELETE FROM jobs WHERE id=? AND status NOT IN ('queued','running')"
                " AND NOT EXISTS (SELECT 1 FROM builds WHERE job_id=?)", (job_id, job_id))
        return deleted

    def build(self, build_id):
        rows = [dict(r) for r in self._read("SELECT * FROM builds WHERE id=?", (build_id,))]
        return rows[0] if rows else None

    def builds_for_tag(self, tag):
        return [dict(r) for r in self._read(
            "SELECT * FROM builds WHERE tag=? ORDER BY id", (tag,))]
