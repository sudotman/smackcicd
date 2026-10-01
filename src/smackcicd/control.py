# SPDX-License-Identifier: GPL-3.0-or-later
"""Live handle on the daemon, shared by the worker and the dashboard.

Single-runner by design: one worker, one build at a time, so a module-level
singleton is the whole coordination story.  The dashboard reads snapshots from
here and can ask for a cancel or a pause; the worker honours both.
"""

from __future__ import annotations

import threading

from .util import iso, kill_process_tree, utc_now


class RunControl:
    def __init__(self):
        self._lock = threading.RLock()
        self.daemon_started = utc_now()
        self.job = None
        self.job_started = None
        self.platform = None
        self.log_path = None
        self.build_number = None
        self.cancelled = False
        self.cancel_requested_by = None
        self.paused = False
        self.last_poll = None
        self.last_poll_error = None
        self._proc = None

    # -- worker side ---------------------------------------------------
    def begin(self, job):
        with self._lock:
            self.job = dict(job)
            self.job_started = utc_now()
            self.platform = None
            self.log_path = None
            self.build_number = None
            self.cancelled = False
            self.cancel_requested_by = None
            self._proc = None

    def begin_platform(self, platform, log_path, build_number):
        with self._lock:
            self.platform = platform
            self.log_path = str(log_path)
            self.build_number = build_number

    def set_proc(self, proc):
        """Called by util.run when a child process starts."""
        with self._lock:
            self._proc = proc
            if self.cancelled and proc is not None:
                # Cancel landed between platforms; stop this one immediately.
                kill_process_tree(proc.pid)

    def end(self):
        with self._lock:
            self.job = None
            self.job_started = None
            self.platform = None
            self.log_path = None
            self.build_number = None
            self._proc = None

    def note_poll(self, error=None):
        with self._lock:
            self.last_poll = utc_now()
            self.last_poll_error = error

    # -- dashboard side ------------------------------------------------
    def cancel(self, requested_by="dashboard"):
        """Ask the running build to stop. Returns False if nothing is running."""
        with self._lock:
            if not self.job:
                return False
            self.cancelled = True
            self.cancel_requested_by = requested_by
            if self._proc is not None:
                kill_process_tree(self._proc.pid)
            return True

    def set_paused(self, paused):
        with self._lock:
            self.paused = bool(paused)
            return self.paused

    def snapshot(self):
        with self._lock:
            running = None
            if self.job:
                running = {
                    "tag": self.job.get("tag"),
                    "jobId": self.job.get("id"),
                    "source": self.job.get("source"),
                    "platform": self.platform,
                    "buildNumber": self.build_number,
                    "logPath": self.log_path,
                    "startedAt": iso(self.job_started) if self.job_started else None,
                    "elapsedSeconds": int((utc_now() - self.job_started).total_seconds())
                    if self.job_started else 0,
                    "cancelling": self.cancelled,
                }
            return {
                "daemonStartedAt": iso(self.daemon_started),
                "uptimeSeconds": int((utc_now() - self.daemon_started).total_seconds()),
                "paused": self.paused,
                "lastPollAt": iso(self.last_poll) if self.last_poll else None,
                "lastPollError": self.last_poll_error,
                "running": running,
            }


CONTROL = RunControl()
