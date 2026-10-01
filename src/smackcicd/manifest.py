# SPDX-License-Identifier: GPL-3.0-or-later
"""manifest.json -- the build record that travels with every package.

One manifest per platform package plus a combined manifest per tag: enough to
answer "exactly what is this build?" without access to the runner.
"""

from __future__ import annotations

import json
import platform as host_platform
import socket
import urllib.parse
from pathlib import Path

from . import MANIFEST_SCHEMA_VERSION, __version__
from .inifile import get_key
from .util import ensure_dir, human_size, iso

ANDROID_SECTION = "/Script/AndroidRuntimeSettings.AndroidRuntimeSettings"


def project_facts(workspace_path, uproject, project_name):
    """Package identity and cook scope, read from the project's own config."""
    config_dir = Path(workspace_path) / "Config"
    package = get_key(config_dir / "DefaultEngine.ini", ANDROID_SECTION, "PackageName", "") or ""
    package = package.replace("[PROJECT]", project_name)

    data, plugins = {}, []
    try:
        data = json.loads(Path(uproject).read_text(encoding="utf-8-sig"))
        plugins = sorted(p.get("Name", "") for p in data.get("Plugins", []) if p.get("Enabled"))
    except (OSError, ValueError):
        data = {}

    maps = []
    game_ini = config_dir / "DefaultGame.ini"
    if game_ini.exists():
        marker = 'FilePath="'
        for line in game_ini.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("+MapsToCook="):
                start = stripped.find(marker)
                if start != -1:
                    maps.append(stripped[start + len(marker):].rstrip(') "'))

    return {
        "name": project_name,
        "uproject": Path(uproject).name,
        "androidPackageName": package,
        "engineAssociation": data.get("EngineAssociation", ""),
        "enabledPlugins": plugins,
        "mapsToCook": maps,
    }


def runner_facts():
    return {
        "host": socket.gethostname(),
        "os": "%s %s" % (host_platform.system(), host_platform.release()),
        "osVersion": host_platform.version(),
        "toolVersion": __version__,
    }


def version_block(spec):
    return {
        "tag": spec.tag,
        "semver": spec.semver,
        "core": spec.core,
        "major": spec.major,
        "minor": spec.minor,
        "patch": spec.patch,
        "prerelease": spec.prerelease,
        "metadata": spec.metadata,
        "channel": spec.channel,
        "channelNumber": spec.channel_number,
        "isPrerelease": spec.is_prerelease,
        "androidVersionCode": spec.android_version_code,
    }


def source_block(commit_info, tag_info, branches, submodules, remote, dirty):
    block = dict(commit_info)
    block.update({
        "repository": remote,
        "tag": tag_info.get("tag"),
        "tagAnnotated": tag_info.get("annotated", False),
        "taggerName": tag_info.get("taggerName", ""),
        "taggerEmail": tag_info.get("taggerEmail", ""),
        "taggedAt": tag_info.get("taggedAt", ""),
        "tagMessage": tag_info.get("message", ""),
        "branchesContaining": branches,
        "submodules": submodules,
        "workingTreeDirty": dirty,
    })
    return block


def build_manifest(*, spec, build_number, build_id, started_at, finished_at,
                   duration_seconds, result, engine, project, source, runner,
                   artifacts, target, uat, logs, error=None):
    """One platform's manifest."""
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "generatedAt": iso(),
        "build": {
            "id": build_id,
            "number": build_number,
            "result": result,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "durationSeconds": duration_seconds,
            "error": error,
            "runner": runner,
        },
        "version": version_block(spec),
        "source": source,
        "engine": engine,
        "project": project,
        "target": target,
        "artifacts": artifacts,
        "uat": uat,
        "logs": logs,
    }


def combined_manifest(spec, source, engines, project, runner, per_platform):
    """One manifest for the whole tag, summarising each build target.

    Platforms are keyed by their drop subfolder ("Windows", or "Windows-UE5.4"
    when one tag builds several engines).
    """
    platforms = {}
    for entry in per_platform:
        target = entry["target"]
        platforms[target.get("label") or target["platform"]] = {
            "platform": target["platform"],
            "engine": target.get("engineVersion"),
            "result": entry["build"]["result"],
            "configuration": target["configuration"],
            "buildNumber": entry["build"]["number"],
            "durationSeconds": entry["build"]["durationSeconds"],
            "artifacts": [
                {k: a[k] for k in ("name", "type", "sizeBytes", "sha256", "relativePath")}
                for a in entry["artifacts"]
            ],
            "error": entry["build"].get("error"),
        }
    results = [p["result"] for p in platforms.values()]
    if results and all(r == "success" for r in results):
        overall = "success"
    elif any(r == "cancelled" for r in results):
        overall = "cancelled"
    else:
        overall = "failure"
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "generatedAt": iso(),
        "result": overall,
        "version": version_block(spec),
        "source": source,
        "engines": engines,
        "project": project,
        "runner": runner,
        "platforms": platforms,
    }


def write(manifest, path):
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def update_index(drop_root, combined, limit=50):
    """index.json -- a rolling list of builds in the drop folder, for scripts."""
    index_path = Path(drop_root) / "index.json"
    entries = []
    if index_path.exists():
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8")).get("builds", [])
        except (ValueError, AttributeError):
            entries = []
    version = combined["version"]
    entry = {
        "tag": version["tag"],
        "semver": version["semver"],
        "channel": version["channel"],
        "commit": combined["source"].get("shortCommit", ""),
        "result": combined["result"],
        "generatedAt": combined["generatedAt"],
        "platforms": {name: data["result"] for name, data in combined["platforms"].items()},
        "folder": version["tag"].replace("+", "_").replace("/", "-"),
    }
    entries = [e for e in entries if e.get("tag") != entry["tag"]]
    entries.insert(0, entry)
    ensure_dir(index_path.parent)
    index_path.write_text(json.dumps({"updatedAt": iso(), "builds": entries[:limit]}, indent=2)
                          + "\n", encoding="utf-8")
    return index_path


# ------------------------------------------------------------- release notes
def quick_downloads(combined, drop_folder, base_url):
    """(platform, label, url, size) for each platform's one-click downloads.

    The zip of the whole package, plus the bare APK for Android, served by the
    runner's dashboard so multi-GB files stay off the forge.
    """
    if not base_url:
        return []
    slug = Path(str(drop_folder).replace("\\", "/")).name
    links = []
    for folder, data in sorted(combined["platforms"].items()):
        if data.get("result") != "success":
            continue
        for artifact in data["artifacts"]:
            if artifact["type"] not in ("archive", "apk"):
                continue
            relative = "%s/%s/%s" % (slug, folder, artifact["relativePath"])
            label = "zip" if artifact["type"] == "archive" else "APK only"
            links.append((folder, label, "%s/artifacts/%s"
                          % (base_url.rstrip("/"), urllib.parse.quote(relative)),
                          artifact["sizeBytes"]))
    return sorted(links, key=lambda link: (link[0], link[1] != "zip"))


def release_notes(combined, drop_folder, base_url=""):
    """Markdown body for the forge release."""
    tick = chr(96)
    version = combined["version"]
    source = combined["source"]

    def code(text):
        return "%s%s%s" % (tick, text, tick)

    lines = ["**%s** - channel %s - commit %s"
             % (version["semver"], code(version["channel"]), code(source.get("shortCommit", ""))),
             ""]
    links = quick_downloads(combined, drop_folder, base_url)
    if links:
        lines += ["### Quick download", ""]
        for platform, label, url, size in links:
            lines.append("- **%s** %s - [%s](%s) (%s)"
                         % (platform, label, Path(url).name, url, human_size(size)))
        lines += ["", "_Served by the build machine; reachable on its network._", ""]
    engines = " + ".join(e.get("version", "?") for e in combined.get("engines") or [])
    lines += [
        "| Field | Value |",
        "| --- | --- |",
        "| Tag | %s |" % code(version["tag"]),
        "| Commit | %s |" % code(source.get("commit", "")),
        "| Subject | %s |" % (source.get("subject", "").replace("|", "\\|") or "-"),
        "| Author | %s |" % (source.get("authorName", "") or "-"),
        "| Engine | %s |" % (engines or "-"),
        "| Android versionCode | %s |" % code(version["androidVersionCode"]),
        "| Built on | %s |" % combined["runner"].get("host", "?"),
        "| Built at | %s |" % combined["generatedAt"],
        "",
        "### Packages",
        "",
    ]
    for name, data in sorted(combined["platforms"].items()):
        mark = "OK" if data["result"] == "success" else data["result"].upper()
        lines.append("- **%s** (%s) - %s" % (name, data["configuration"], mark))
        for artifact in data["artifacts"]:
            lines.append("  - %s - %s - sha256 %s" % (
                code(artifact["name"]), human_size(artifact["sizeBytes"]),
                code(artifact["sha256"][:16])))
    lines += ["", "The full build record is %s; checksums are in %s."
              % (code("manifest.json"), code("SHA256SUMS.txt"))]
    return "\n".join(lines)
