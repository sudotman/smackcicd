# SPDX-License-Identifier: GPL-3.0-or-later
"""Finding things on this machine, so `init` can fill the config in for you."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .engines import _fixed_drives
from .util import capture, exists, is_dir

ANDROID_ENV = ("ANDROID_HOME", "ANDROID_SDK_ROOT", "NDKROOT", "NDK_ROOT", "JAVA_HOME")


def find_uprojects(root):
    root = Path(root)
    return sorted(root.glob("*.uproject")) if is_dir(root) else []


def git_remote(path):
    """The origin URL of the clone at ``path``, or ""."""
    if not (Path(path) / ".git").exists():
        return ""
    try:
        out, rc = capture(["git", "-C", str(path), "remote", "get-url", "origin"], check=False)
    except OSError:
        return ""
    return out.strip() if rc == 0 else ""


def project_targets_android(workspace):
    """Does the project look like it ships on Android?"""
    workspace = Path(workspace)
    if is_dir(workspace / "Config" / "Android") or is_dir(workspace / "Build" / "Android"):
        return True
    engine_ini = workspace / "Config" / "DefaultEngine.ini"
    try:
        return "AndroidRuntimeSettings" in engine_ini.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _version_key(path):
    return [int(n) for n in re.findall(r"\d+", Path(path).name)] or [0]


def _android_sdk_candidates():
    candidates = []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]))
    if os.name == "nt":
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
        for drive in _fixed_drives():
            candidates += [drive / "Android" / "Sdk", drive / "Android" / "sdk"]
    else:
        home = Path.home()
        candidates += [home / "Android" / "Sdk", home / "Library" / "Android" / "sdk",
                       Path("/opt/android-sdk"), Path("/usr/lib/android-sdk")]
    return candidates


def _java_candidates():
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]))
    if os.name == "nt":
        roots = [Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))]
        for root in roots:
            # Android Studio's bundled runtime is what Epic's own setup uses.
            candidates.append(root / "Android" / "Android Studio" / "jbr")
            for vendor in ("Microsoft", "Eclipse Adoptium", "Java", "Zulu", "Amazon Corretto"):
                base = root / vendor
                if is_dir(base):
                    candidates += sorted((p for p in base.iterdir() if "17" in p.name),
                                         key=_version_key, reverse=True)
    else:
        for base in (Path("/usr/lib/jvm"), Path("/Library/Java/JavaVirtualMachines")):
            if is_dir(base):
                for path in sorted((p for p in base.iterdir() if "17" in p.name),
                                   key=_version_key, reverse=True):
                    candidates.append(path / "Contents" / "Home" if is_dir(path / "Contents")
                                      else path)
        candidates.append(Path("/opt/android-studio/jbr"))
    return candidates


def android_toolchain():
    """Where the Android SDK, NDK and a JDK are, and what Android builds need set.

    ``env`` lists only variables that are not already set in this
    environment; `init` writes them into [platforms.Android] env, so nothing
    has to be configured machine-wide.
    """
    sdk = next((p for p in _android_sdk_candidates()
                if is_dir(p / "platform-tools") or is_dir(p / "build-tools")), None)
    ndk = None
    for var in ("NDKROOT", "NDK_ROOT"):
        if os.environ.get(var) and is_dir(os.environ[var]):
            ndk = Path(os.environ[var])
            break
    if ndk is None and sdk and is_dir(sdk / "ndk"):
        versions = sorted((p for p in (sdk / "ndk").iterdir() if is_dir(p)),
                          key=_version_key, reverse=True)
        ndk = versions[0] if versions else None
    java_exe = "java.exe" if os.name == "nt" else "java"
    java = next((p for p in _java_candidates() if exists(p / "bin" / java_exe)), None)

    wanted = {}
    if sdk:
        wanted["ANDROID_HOME"] = sdk
        wanted["ANDROID_SDK_ROOT"] = sdk
    if ndk:
        wanted["NDKROOT"] = ndk
        wanted["NDK_ROOT"] = ndk
    if java:
        wanted["JAVA_HOME"] = java
    env = {key: str(value).replace("\\", "/") for key, value in wanted.items()
           if not os.environ.get(key)}
    missing = [name for name, found in (("Android SDK", sdk), ("Android NDK", ndk),
                                        ("JDK 17", java)) if not found]
    return {"sdk": sdk, "ndk": ndk, "java": java, "env": env, "missing": missing}
