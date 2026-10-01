# SPDX-License-Identifier: GPL-3.0-or-later
"""Process, hashing and formatting helpers shared across the tool."""

from __future__ import annotations

import collections
import hashlib
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

# The Windows service runs under pythonw.exe, which has no console. Without this
# flag every console child (git, RunUAT, cmd) gets a brand-new console window on
# the desktop -- and closing one of those kills the build running inside it.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CommandError(RuntimeError):
    def __init__(self, cmd, returncode, tail):
        self.cmd = cmd
        self.returncode = returncode
        self.tail = tail
        super().__init__("command failed (exit %s): %s%s"
                         % (returncode, Path(cmd[0]).name, self._detail(tail)))

    @staticmethod
    def _detail(tail):
        """Put the last real output lines in the message, not just the exit code."""
        lines = [line.strip() for line in (tail or []) if line.strip()]
        meaty = [line for line in lines if not line.startswith("hint:")] or lines
        return " -- " + " | ".join(meaty[-3:]) if meaty else ""


class CommandTimeout(CommandError):
    pass


class Scrubber:
    """Replaces registered secrets with **** in everything that gets logged."""

    def __init__(self):
        self._secrets = []
        self._lock = threading.Lock()

    def add(self, value):
        value = "" if value is None else str(value)
        if len(value) < 4:
            return
        with self._lock:
            if value not in self._secrets:
                self._secrets.append(value)
                # Longest first, so a secret that contains another is fully hidden.
                self._secrets.sort(key=len, reverse=True)

    def __call__(self, text):
        if not text:
            return text
        for secret in self._secrets:
            text = text.replace(secret, "****")
        return text


SCRUB = Scrubber()


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(dt=None):
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")


def run(cmd, cwd=None, env=None, timeout=None, on_line=None, check=True, tail=300,
        on_start=None):
    """Run a command, streaming each output line to ``on_line``.

    ``on_start`` receives the Popen as soon as it exists, so a long build can be
    cancelled from elsewhere. Returns (returncode, tail_lines); stderr is folded
    into stdout so the log reads in the order things actually happened.

    Batch files are run directly, never through ``cmd /c``: when more than one
    argument is quoted, cmd strips the outer quotes off the whole command line
    and a path with a space in it ("C:\\Program Files\\...") stops resolving.
    """
    cmd = [str(part) for part in cmd]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = NO_WINDOW
    else:
        # Its own process group, so cancelling a build can kill everything it
        # spawned without also killing this runner.
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **kwargs,
    )
    if on_start:
        on_start(proc)
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        kill_process_tree(proc.pid)

    watchdog = threading.Timer(timeout, _kill) if timeout else None
    if watchdog:
        watchdog.daemon = True
        watchdog.start()

    tail_lines = collections.deque(maxlen=tail)
    try:
        for raw in proc.stdout:
            line = SCRUB(raw.rstrip("\r\n"))
            tail_lines.append(line)
            if on_line:
                on_line(line)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        rc = proc.wait()
        if watchdog:
            watchdog.cancel()
        if on_start:
            on_start(None)

    if timed_out.is_set():
        raise CommandTimeout(cmd, rc, list(tail_lines))
    if check and rc != 0:
        raise CommandError(cmd, rc, list(tail_lines))
    return rc, list(tail_lines)


def capture(cmd, cwd=None, check=True, env=None):
    """Run a command and return (trimmed stdout, returncode)."""
    out = []
    rc, _ = run(cmd, cwd=cwd, on_line=out.append, check=check, env=env)
    return "\n".join(out).strip(), rc


def sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def human_size(num_bytes):
    value = float(num_bytes or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return "%.1f %s" % (value, unit) if unit != "B" else "%d B" % value
        value /= 1024
    return "%.1f TiB" % value


def human_duration(seconds):
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%dh %02dm %02ds" % (hours, minutes, secs)
    if minutes:
        return "%dm %02ds" % (minutes, secs)
    return "%ds" % secs


def free_space_gb(path):
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(str(path)).free / (1024 ** 3)


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._+-]", "_", text)


def ensure_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def which(name):
    return shutil.which(name)


def process_alive(pid):
    """Is this PID running?

    ``os.kill(pid, 0)`` is NOT a liveness probe on Windows: CPython maps it to
    TerminateProcess, which kills the very process you asked about. Use the
    Win32 API there and keep the signal probe for POSIX.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means it exists but belongs to someone else.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def kill_process_tree(pid):
    """Kill a process and everything it spawned.

    RunUAT launches UnrealBuildTool, the cook commandlet and a swarm of
    compilers. Killing only the top process leaves those running and holding
    the workspace.
    """
    if not pid:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, check=False, creationflags=NO_WINDOW)
        return result.returncode == 0
    try:
        # run() starts children in their own session, so the group id is the pid.
        os.killpg(pid, signal.SIGKILL)
        return True
    except (OSError, AttributeError):
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False
