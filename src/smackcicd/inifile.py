# SPDX-License-Identifier: GPL-3.0-or-later
"""Surgical edits to Unreal .ini files.

Only the keys we are asked to set are touched; ordering, comments, array (+Key)
entries and everything else in the file are preserved byte for byte.
"""

from __future__ import annotations

import re
from pathlib import Path

SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")


def set_keys(path, section, values):
    """Set ``key=value`` pairs inside ``[section]``, creating what is missing.

    Returns the list of keys that were changed.
    """
    path = Path(path)
    # newline="" keeps \r\n as it is: read_text() would turn it into \n before
    # we look, and a CRLF file (most Unreal configs) would be rewritten as LF.
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
        text = handle.read()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    start = end = None
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if not match:
            continue
        if match.group("name").strip() == section:
            start = index
        elif start is not None and end is None:
            end = index
            break
    if start is not None and end is None:
        end = len(lines)

    changed = []
    if start is None:
        block = ["", "[%s]" % section]
        block += ["%s=%s" % (key, value) for key, value in values.items()]
        lines.extend(block)
        changed = list(values)
    else:
        remaining = dict(values)
        for index in range(start + 1, end):
            stripped = lines[index].lstrip()
            if not stripped or stripped.startswith((";", "#", "+", "-", ".", "!")):
                continue
            if "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                replacement = "%s=%s" % (key, remaining.pop(key))
                if lines[index] != replacement:
                    lines[index] = replacement
                    changed.append(key)
        if remaining:
            insert_at = end
            while insert_at > start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            addition = ["%s=%s" % (key, value) for key, value in remaining.items()]
            lines[insert_at:insert_at] = addition
            changed.extend(remaining)

    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(newline.join(lines) + newline)
    return changed


def get_key(path, section, key, default=None):
    path = Path(path)
    if not path.exists():
        return default
    inside = False
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        match = SECTION_RE.match(line)
        if match:
            inside = match.group("name").strip() == section
            continue
        if not inside:
            continue
        stripped = line.strip()
        if stripped.startswith((";", "#")) or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return value.strip()
    return default
