# SPDX-License-Identifier: GPL-3.0-or-later
"""Just enough TOML writing for the config template.

The standard library reads TOML (tomllib) but cannot write it, and the config
`init` produces is meant to be read and edited by people, so it is rendered
from a commented template with values formatted here.
"""

from __future__ import annotations

import json
import re

BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def key(name):
    return name if BARE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def value(obj):
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int | float):
        return repr(obj)
    if isinstance(obj, str):
        # Literal strings keep regexes and Windows paths readable; fall back to
        # a basic string when the text contains what a literal string cannot.
        if "'" not in obj and "\n" not in obj and "\\" in obj:
            return "'%s'" % obj
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, list | tuple):
        return "[%s]" % ", ".join(value(item) for item in obj)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        return "{ %s }" % ", ".join("%s = %s" % (key(k), value(v)) for k, v in obj.items())
    raise TypeError("cannot write %r as TOML" % (obj,))
