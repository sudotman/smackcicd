# SPDX-License-Identifier: GPL-3.0-or-later
"""Translate a git tag into everything a build needs to know.

Grammar::

    v<major>.<minor>.<patch>[-<prerelease>][+<metadata>]

The channel -- the first non-numeric, non-selector token of the prerelease --
picks the build configuration. Platform tokens anywhere in the prerelease or
metadata pick the targets, and engine tokens pick the Unreal install::

    v1.4.2                   Shipping,    default platforms
    v1.4.2-rc.1              Shipping,    default platforms
    v1.4.2-beta.3            Development, default platforms
    v1.4.2-alpha.1+android   Development, Android only
    v1.4.2-beta.2+win.linux  Development, Windows and Linux
    v1.4.2-beta.1+ue54       Development, built with Unreal 5.4
    v1.4.2-beta.1+ue53.ue54  Development, built twice, once per engine

Channels and their configurations come from ``[channels]`` in the config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import platforms as platforms_mod

SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.\-]+))?"
    r"(?:\+(?P<meta>[0-9A-Za-z.\-]+))?$"
)

# ue54 / ue5_4 -> "5.4". A bare "5.4" cannot work: tokens are split on ".".
# With a separator the major may be several digits (ue5_4, ue10_1); without
# one the major is a single digit, so ue510 reads as 5.10, not 51.0.
ENGINE_TOKEN_RE = re.compile(r"^ue(?:(\d+)[._](\d+)|(\d)(\d+))$", re.IGNORECASE)


def engine_version(token):
    """"5.4" for an engine token, or None if this is not one."""
    match = ENGINE_TOKEN_RE.match(token or "")
    if not match:
        return None
    major, minor = ((match.group(1), match.group(2)) if match.group(1)
                    else (match.group(3), match.group(4)))
    return "%s.%s" % (int(major), int(minor))


class TagRejected(ValueError):
    """The tag is not one this runner should build."""


@dataclass
class TagSpec:
    tag: str
    major: int
    minor: int
    patch: int
    prerelease: str = ""
    metadata: str = ""
    channel: str = "release"
    channel_number: int = 0
    platforms: list = field(default_factory=list)
    configuration: str = "Shipping"
    is_prerelease: bool = False
    android_version_code: int = 0
    # Engine versions to build with. Empty: decided at build time.
    engines: list = field(default_factory=list)

    @property
    def core(self):
        return "%d.%d.%d" % (self.major, self.minor, self.patch)

    @property
    def semver(self):
        return self.core + ("-" + self.prerelease if self.prerelease else "")

    def slug(self):
        """Folder-safe form of the tag, used for drop and log folders."""
        return self.tag.replace("+", "_").replace("/", "-")

    def summary(self):
        return "%s -> %s [%s] platforms=%s engine=%s" % (
            self.tag, self.semver, self.configuration,
            ", ".join(self.platforms) or "none",
            ", ".join(self.engines) if self.engines else "from .uproject")


def _tokens(text):
    return [part for part in re.split(r"[.\-]", text or "") if part]


def parse(tag, cfg=None, platform_override=None, engine_override=None):
    """Parse ``tag`` into a :class:`TagSpec`, honouring config rules."""
    tag = tag.strip()
    if tag.startswith("refs/tags/"):
        tag = tag[len("refs/tags/"):]

    pattern = cfg.get("triggers.tag_pattern") if cfg else r"^v\d+\.\d+\.\d+"
    if pattern and not re.search(pattern, tag):
        raise TagRejected("tag %r does not match triggers.tag_pattern %r" % (tag, pattern))

    match = SEMVER_RE.match(tag)
    if not match:
        raise TagRejected("tag %r is not v<major>.<minor>.<patch>[-pre][+meta]" % tag)

    aliases = platforms_mod.alias_map()
    pre_tokens = _tokens(match.group("prerelease"))
    meta_tokens = _tokens(match.group("meta"))
    every = pre_tokens + meta_tokens
    configured = cfg.default_platforms() if cfg else list(platforms_mod.PLATFORMS)

    platforms = []
    for token in every:
        mapped = aliases.get(token.lower())
        if mapped == "*":
            platforms = list(cfg.enabled_platforms() if cfg else platforms_mod.PLATFORMS)
            break
        if mapped and mapped not in platforms:
            platforms.append(mapped)

    engines = []
    for token in every:
        version = engine_version(token)
        if version and version not in engines:
            engines.append(version)

    def is_selector(token):
        """Platform and engine tokens steer the build; they are not version text."""
        return token.lower() in aliases or engine_version(token) is not None

    clean_pre = [t for t in pre_tokens if not is_selector(t)]
    clean_meta = [t for t in meta_tokens if not is_selector(t)]

    channel, channel_number = "release", 0
    for token in clean_pre:
        if not token.isdigit() and channel == "release":
            channel = token.lower()
        elif token.isdigit() and channel_number == 0:
            channel_number = int(token)

    channels = (cfg.get("channels") if cfg else None) or {}
    rules = channels.get(channel) or channels.get("_unknown") or {}
    configuration = rules.get("configuration", "Shipping" if channel == "release" else "Development")
    is_prerelease = rules.get("prerelease", channel != "release")
    rank = rules.get("rank", 0)

    if engine_override:
        engines = list(engine_override)
    if not engines and cfg:
        engines = list(cfg.get("engine.default") or [])

    if platform_override:
        platforms = [platforms_mod.get(p).name for p in platform_override]
    if not platforms:
        platforms = list(configured)

    enabled = [name for name in platforms
               if cfg is None or cfg.platform(name).get("enabled", False)]

    spec = TagSpec(
        tag=tag,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=".".join(clean_pre),
        metadata=".".join(clean_meta),
        channel=channel,
        channel_number=channel_number,
        platforms=enabled,
        configuration=configuration,
        is_prerelease=bool(is_prerelease),
        engines=engines,
    )
    offset = (cfg.get("versioning.android_version_code_offset", 0) if cfg else 0) or 0
    spec.android_version_code = android_version_code(spec, rank, offset)

    if not spec.platforms:
        raise TagRejected("tag %r selects no enabled platform (asked for %s)"
                          % (tag, ", ".join(platforms) or "none"))
    return spec


def android_version_code(spec, rank, offset=0):
    """A versionCode that always increases with the version.

    ``((major * 100 + minor) * 100 + patch) * 1000 + rank * 100 + n``. A
    release outranks its own prereleases because the release rank (9) is the
    highest. Valid while major <= 209 (Android caps versionCode at 2100000000).
    """
    base = ((spec.major * 100 + spec.minor) * 100 + spec.patch) * 1000
    return base + int(rank) * 100 + min(spec.channel_number, 99) + int(offset)
