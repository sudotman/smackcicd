# SPDX-License-Identifier: GPL-3.0-or-later
"""Logging: console, a rolling daemon log, and one log file per build."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

from .util import SCRUB, ensure_dir

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
TIME_FORMAT = "%H:%M:%S"


class ScrubFilter(logging.Filter):
    """Last line of defence: no secret reaches a handler."""

    def filter(self, record):
        try:
            record.msg = SCRUB(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def configure(log_root=None, verbose=False, quiet=False):
    root = logging.getLogger("smackcicd")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    # Under pythonw.exe there is no stderr at all; a console handler would just
    # swallow every record.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO))
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT, TIME_FORMAT))
        console.addFilter(ScrubFilter())
        root.addHandler(console)

    if log_root:
        ensure_dir(log_root)
        daemon_log = Path(log_root) / "smackcicd.log"
        rolling = logging.handlers.RotatingFileHandler(
            daemon_log, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8")
        rolling.setLevel(logging.DEBUG)
        rolling.setFormatter(logging.Formatter(FILE_FORMAT, "%Y-%m-%d %H:%M:%S"))
        rolling.addFilter(ScrubFilter())
        root.addHandler(rolling)
        _log_uncaught(root)
    return root


def _log_uncaught(logger):
    """Without a console, an unhandled exception would leave no trace at all.

    This is how a daemon that "just stopped" explains itself in its log.
    """
    def on_main(exc_type, exc, tb):
        logger.critical("daemon crashed", exc_info=(exc_type, exc, tb))

    def on_thread(args):
        if args.exc_type is SystemExit:
            return
        logger.critical("thread %s crashed", getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = on_main
    threading.excepthook = on_thread


def get_logger(name="smackcicd"):
    return logging.getLogger(name if name.startswith("smackcicd") else "smackcicd.%s" % name)


class BuildLogFile:
    """Context manager that tees the smackcicd logger into one build's own file."""

    def __init__(self, path):
        self.path = Path(path)
        self.handler = None

    def __enter__(self):
        ensure_dir(self.path.parent)
        self.handler = logging.FileHandler(self.path, encoding="utf-8")
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(logging.Formatter(FILE_FORMAT, "%Y-%m-%d %H:%M:%S"))
        self.handler.addFilter(ScrubFilter())
        logging.getLogger("smackcicd").addHandler(self.handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handler:
            logging.getLogger("smackcicd").removeHandler(self.handler)
            self.handler.close()
        return False
