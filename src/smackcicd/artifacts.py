# SPDX-License-Identifier: GPL-3.0-or-later
"""Collecting, hashing, zipping and pruning packaged output.

UAT archives straight into the drop folder, so nothing multi-gigabyte is ever
copied twice.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from . import platforms as platforms_mod
from .util import CommandError, ensure_dir, human_size, run, sha256_file, which

TYPE_BY_SUFFIX = {
    ".apk": "apk",
    ".obb": "obb",
    ".aab": "aab",
    ".bat": "installer",
    ".sh": "installer",
    ".command": "installer",
    ".exe": "executable",
    ".zip": "archive",
    ".so": "symbols",
    ".pdb": "symbols",
}

# Files the runner itself writes into a target folder; never package content.
OWN_FILES = {"manifest.json"}


def build_root(cfg, spec):
    return cfg.drop_root / spec.slug()


def archive_dir(cfg, spec, label):
    """One folder per build target.

    ``label`` is the platform ("Windows"), or platform + engine
    ("Windows-UE5.4") when one tag is built with several engines.
    """
    return ensure_dir(build_root(cfg, spec) / label)


def _is_package(folder, info):
    return any(any(Path(folder).glob(pattern)) for pattern in info.markers)


def platform_output_dir(cfg, spec, label, platform):
    """Where UAT actually put the packaged files.

    Older engines archive into a platform subfolder (Windows/, Android_ASTC/);
    UE 5.8 archives straight into -archivedirectory. Both are accepted.
    """
    info = platforms_mod.get(platform)
    root = archive_dir(cfg, spec, label)
    flavor = cfg.platform(info.name).get("cook_flavor") or "ASTC"
    for sub in info.archive_subdirs:
        candidate = root / sub.format(flavor=flavor)
        if candidate.is_dir():
            return candidate
    if _is_package(root, info):
        return root
    subdirs = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    return subdirs[0] if len(subdirs) == 1 else root


def make_zip(source_dir, dest_zip, logger):
    """Zip a directory, preferring 7-Zip when it is installed.

    The zip may live inside the folder it zips (UE 5.8 layout); it is left out.
    """
    source_dir = Path(source_dir)
    dest_zip = Path(dest_zip)
    if dest_zip.exists():
        dest_zip.unlink()
    ensure_dir(dest_zip.parent)
    exclude = ["-xr!%s" % dest_zip.name] if dest_zip.parent == source_dir else []
    seven = which("7z") or which("7za")
    if seven:
        logger.info("zipping %s with 7-Zip", source_dir.name)
        try:
            run([seven, "a", "-tzip", "-mx=3", "-bso0", "-bsp0", str(dest_zip),
                 source_dir.name] + exclude, cwd=source_dir.parent, on_line=logger.debug)
            return dest_zip
        except CommandError as exc:
            logger.warning("7-Zip failed (%s); falling back to zipfile", exc)
            dest_zip.unlink(missing_ok=True)
    logger.info("zipping %s", source_dir.name)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1,
                         allowZip64=True) as archive:
        for folder, _dirs, files in os.walk(source_dir):
            for name in files:
                full = Path(folder) / name
                if full == dest_zip:
                    continue
                archive.write(full, full.relative_to(source_dir.parent).as_posix())
    return dest_zip


def describe(path, relative_to):
    path = Path(path)
    return {
        "name": path.name,
        "path": str(path),
        "relativePath": path.relative_to(relative_to).as_posix(),
        "type": TYPE_BY_SUFFIX.get(path.suffix.lower(), "other"),
        "sizeBytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def collect(cfg, spec, label, platform, project_name, logger):
    """Describe every artifact produced for one build target."""
    info = platforms_mod.get(platform)
    root = archive_dir(cfg, spec, label)
    output = platform_output_dir(cfg, spec, label, platform)
    found = []

    if not output.is_dir() or not (output != root or _is_package(root, info)):
        logger.warning("no packaged output found under %s", root)
        return found

    # Loose deliverables at the top of the package folder: the APK, OBBs and
    # install scripts for Android; the zip below covers staged trees.
    if info.name == "Android":
        for item in sorted(output.iterdir()):
            if item.is_file() and item.name not in OWN_FILES:
                found.append(describe(item, root))
    if cfg.platform(info.name).get("zip", True):
        zip_path = make_zip(output, root / ("%s-%s-%s.zip" % (project_name, spec.semver, label)),
                            logger)
        found.append(describe(zip_path, root))

    for artifact in found:
        logger.info("artifact %s (%s)", artifact["name"], human_size(artifact["sizeBytes"]))
    return found


def write_checksums(build_root_path, per_platform):
    """SHA256SUMS.txt, verifiable with `sha256sum -c` from the tag folder."""
    lines = []
    for entry in per_platform:
        label = entry["target"].get("label") or entry["target"]["platform"]
        for artifact in entry["artifacts"]:
            lines.append("%s  %s/%s" % (artifact["sha256"], label, artifact["relativePath"]))
    path = Path(build_root_path) / "SHA256SUMS.txt"
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def prune(drop_root, keep, logger, protect=()):
    """Delete the oldest tag folders beyond ``keep`` (0 keeps everything)."""
    drop_root = Path(drop_root)
    if keep <= 0 or not drop_root.is_dir():
        return []
    protected = {Path(p).resolve() for p in protect}
    folders = sorted((p for p in drop_root.iterdir() if p.is_dir()),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for folder in folders[keep:]:
        if folder.resolve() in protected:
            continue
        logger.info("pruning old build folder %s", folder.name)
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(folder)
    return removed
