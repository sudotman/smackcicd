# SPDX-License-Identifier: GPL-3.0-or-later
"""What smackcicd knows about each target platform.

Everything platform-specific lives in this table: the name Unreal's
``-platform=`` expects, the tag tokens that select it, where UAT archives it,
and which file shows a package actually finished. Adding a platform is adding
an entry here (plus a ``[platforms.<Name>]`` section in the config).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformInfo:
    name: str
    ue: str
    # Archive subfolders older engines create under -archivedirectory. UE 5.8
    # archives straight into the folder instead; artifacts.py handles both.
    archive_subdirs: tuple
    # Globs that match a finished package at the top of its folder.
    markers: tuple
    # Lowercase tag tokens that select this platform.
    aliases: tuple
    # os.name values that can build it.
    hosts: tuple
    architectures: tuple


PLATFORMS = {
    "Windows": PlatformInfo(
        name="Windows", ue="Win64",
        archive_subdirs=("Windows", "WindowsNoEditor"),
        markers=("*.exe",),
        aliases=("win", "win64", "windows", "pc"),
        hosts=("nt",),
        architectures=("x64",)),
    "Android": PlatformInfo(
        name="Android", ue="Android",
        archive_subdirs=("Android_{flavor}", "Android"),
        markers=("*.apk", "*.aab"),
        aliases=("android", "quest", "apk"),
        hosts=("nt", "posix"),
        architectures=("arm64",)),
    "Linux": PlatformInfo(
        name="Linux", ue="Linux",
        archive_subdirs=("Linux", "LinuxNoEditor"),
        markers=("*.sh",),
        aliases=("linux",),
        hosts=("nt", "posix"),
        architectures=("x64",)),
}

# Tokens that mean "every platform this runner builds".
ALL_TOKENS = ("all", "both", "any")


class UnknownPlatform(ValueError):
    pass


def get(name):
    """Look a platform up by name or alias, case-insensitively."""
    if not name:
        raise UnknownPlatform("empty platform name")
    for info in PLATFORMS.values():
        if name.lower() == info.name.lower() or name.lower() in info.aliases:
            return info
    raise UnknownPlatform("unknown platform %r (known: %s)" % (name, ", ".join(PLATFORMS)))


def alias_map():
    """{token: platform name} for tag parsing, plus "*" for the all-tokens."""
    mapping = {}
    for info in PLATFORMS.values():
        mapping[info.name.lower()] = info.name
        for alias in info.aliases:
            mapping[alias] = info.name
    for token in ALL_TOKENS:
        mapping[token] = "*"
    return mapping


def buildable_here(name):
    return os.name in PLATFORMS[name].hosts
