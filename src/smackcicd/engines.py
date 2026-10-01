# SPDX-License-Identifier: GPL-3.0-or-later
"""Finding Unreal Engine installs, and driving RunUAT.

Installs are discovered, not configured: the Windows registry (launcher
installs and registered source builds), the Epic launcher's install list,
Linux/macOS Install.ini, and the usual install folders on every fixed drive.
``engine.versions`` in the config only pins or adds to what is found.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
from pathlib import Path

from . import platforms as platforms_mod
from .util import exists, is_dir, run

ERROR_RE = re.compile(r"(?:^|\s)(?:ERROR|Error):(?!\s*0\b)")
WARNING_RE = re.compile(r"(?:^|\s)(?:WARNING|Warning):")
EXITCODE_RE = re.compile(r"ExitCode\s*=\s*(-?\d+)")
VERSION_RE = re.compile(r"^\d+\.\d+$")


class EngineError(RuntimeError):
    pass


def _host_binaries():
    if sys.platform == "win32":
        return "Win64", ("UnrealEditor-Cmd.exe", "UE4Editor-Cmd.exe")
    if sys.platform == "darwin":
        return "Mac", ("UnrealEditor.app/Contents/MacOS/UnrealEditor",
                       "UE4Editor.app/Contents/MacOS/UE4Editor")
    return "Linux", ("UnrealEditor", "UE4Editor")


class Engine:
    def __init__(self, root):
        self.root = Path(root)
        batch = "RunUAT.bat" if os.name == "nt" else "RunUAT.sh"
        self.uat = self.root / "Engine" / "Build" / "BatchFiles" / batch
        if not exists(self.uat):
            raise EngineError("%s not found under %s" % (batch, self.root))

    @property
    def editor_cmd(self):
        folder, names = _host_binaries()
        candidates = [self.root / "Engine" / "Binaries" / folder / name for name in names]
        return next((c for c in candidates if c.exists()), candidates[0])

    @property
    def version(self):
        path = self.root / "Engine" / "Build" / "Build.version"
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    @property
    def version_string(self):
        data = self.version
        if not data:
            return "unknown"
        return "%s.%s.%s" % (data.get("MajorVersion", "?"), data.get("MinorVersion", "?"),
                             data.get("PatchVersion", "?"))

    @property
    def short_version(self):
        """"5.4" -- what tags and folder labels use (version_string is 5.4.2)."""
        data = self.version
        if not data:
            return "unknown"
        return "%s.%s" % (data.get("MajorVersion", "?"), data.get("MinorVersion", "?"))

    def describe(self):
        data = self.version
        return {
            "version": self.version_string,
            "shortVersion": self.short_version,
            "changelist": data.get("Changelist"),
            "branch": data.get("BranchName", ""),
            "path": str(self.root),
        }

    def __repr__(self):
        return "Engine(%s, %s)" % (self.short_version, self.root)


def engine_association(uproject):
    try:
        data = json.loads(Path(uproject).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    return str(data.get("EngineAssociation", "")).strip()


def _norm_guid(text):
    return (text or "").strip().strip("{}").lower()


# ------------------------------------------------------------- discovery
def _registry_entries():
    """(association, path) pairs from the Windows registry."""
    entries = []
    try:
        import winreg
    except ImportError:
        return entries
    # Launcher installs: HKLM\SOFTWARE\EpicGames\Unreal Engine\<version>.
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\EpicGames\Unreal Engine") as parent:
                index = 0
                while True:
                    try:
                        version = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(parent, version) as sub:
                            path = winreg.QueryValueEx(sub, "InstalledDirectory")[0]
                            entries.append((version, path))
                    except OSError:
                        continue
        except OSError:
            continue
    # Source builds registered by UnrealVersionSelector: GUID -> path.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"SOFTWARE\Epic Games\Unreal Engine\Builds") as key:
            index = 0
            while True:
                try:
                    name, path, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                entries.append((name, path))
    except OSError:
        pass
    return entries


def _launcher_entries():
    """(AppName, InstallLocation) from the Epic launcher's install list."""
    files = []
    if os.name == "nt":
        files.append(Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                     / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat")
    else:
        files.append(Path("/Users/Shared/Epic Games/UnrealEngineLauncher/LauncherInstalled.dat"))
    entries = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for item in data.get("InstallationList", []):
            name, location = item.get("AppName", ""), item.get("InstallLocation", "")
            if name.upper().startswith("UE_") and location:
                entries.append((name[3:], location))
    return entries


def _install_ini_entries():
    """(GUID, path) from Linux/macOS Install.ini."""
    candidates = [Path.home() / ".config" / "Epic" / "UnrealEngine" / "Install.ini",
                  Path.home() / "Library" / "Application Support" / "Epic" / "UnrealEngine"
                  / "Install.ini"]
    entries = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        inside = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                inside = stripped.lower() == "[installations]"
                continue
            if inside and "=" in stripped:
                name, value = stripped.split("=", 1)
                entries.append((name.strip(), value.strip()))
    return entries


def _fixed_drives():
    if os.name != "nt":
        return []
    try:
        import ctypes
        get_type = ctypes.windll.kernel32.GetDriveTypeW
    except (AttributeError, OSError):
        get_type = None
    drives = []
    for letter in string.ascii_uppercase[2:]:          # C..Z
        root = "%s:\\" % letter
        if get_type is not None and get_type(root) != 3:  # 3 == DRIVE_FIXED
            continue
        if os.path.isdir(root):
            drives.append(Path(root))
    return drives


def default_search_dirs():
    if os.name == "nt":
        dirs = []
        for drive in _fixed_drives():
            for sub in ("Program Files/Epic Games", "Epic Games", "Unreal Engine",
                        "UnrealEngine", "Program Files/Unreal Engine"):
                dirs.append(drive / sub)
        return dirs
    home = Path.home()
    return [Path("/opt"), Path("/opt/UnrealEngine"), home / "UnrealEngine", home / "Epic Games",
            Path("/Users/Shared/Epic Games")]


def _folder_entries(search_dirs):
    """(None, path) for every UE_x.y (or engine root) inside the search dirs."""
    entries = []
    for base in search_dirs:
        base = Path(base)
        if not is_dir(base):
            continue
        if is_dir(base / "Engine" / "Build" / "BatchFiles"):
            entries.append((None, str(base)))
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if is_dir(child) and is_dir(child / "Engine" / "Build" / "BatchFiles"):
                entries.append((None, str(child)))
    return entries


def all_entries(cfg=None):
    """Every (association-or-None, path) worth trying, highest priority first."""
    entries = []
    pinned = (cfg.get("engine.versions") if cfg else None) or {}
    entries += [(version, path) for version, path in pinned.items()]
    entries += _registry_entries()
    entries += _launcher_entries()
    entries += _install_ini_entries()
    extra = [Path(p) for p in ((cfg.get("engine.search_dirs") if cfg else None) or [])]
    entries += _folder_entries(extra + default_search_dirs())
    return entries


def discover(cfg=None):
    """{short version: Engine}, the first valid install per version.

    Config-pinned versions come first, so ``engine.versions`` always wins.
    """
    found = {}
    seen = set()
    for _association, path in all_entries(cfg):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            engine = Engine(path)
        except EngineError:
            continue
        version = engine.short_version
        if version != "unknown" and version not in found:
            found[version] = engine
    return dict(sorted(found.items(), key=lambda kv: _version_key(kv[0]), reverse=True))


def _version_key(version):
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return (0,)


def find_engine(cfg, uproject, version=None):
    """Resolve the engine for a build.

    ``version`` ("5.4") comes from the tag or ``engine.default`` and wins.
    Without it: ``engine.path``, then the .uproject's EngineAssociation, which
    is either a version or the GUID of a registered source build.
    """
    if version:
        engines = discover(cfg)
        if version in engines:
            return engines[version]
        raise EngineError("Unreal Engine %s is not installed here (found: %s). Add it to "
                          "[engine] versions in %s if it lives somewhere unusual."
                          % (version, ", ".join(engines) or "none", cfg.path.name))

    fixed = cfg.get("engine.path")
    if fixed:
        return Engine(fixed)

    association = engine_association(uproject)
    if not association:
        raise EngineError("%s has no EngineAssociation and the tag named no engine -- tag "
                          "with +ue54 (say) or set [engine] default" % Path(uproject).name)
    if VERSION_RE.match(association):
        return find_engine(cfg, uproject, association)

    # A source build: its GUID is registered by UnrealVersionSelector.
    for name, path in all_entries(cfg):
        if name and _norm_guid(name) == _norm_guid(association):
            try:
                return Engine(path)
            except EngineError:
                continue
    raise EngineError("the .uproject is associated with source build %s, which is not "
                      "registered on this machine -- set [engine] path to its root"
                      % association)


SETUP_DEFAULT_RE = re.compile(
    r'(?:if\s+"%?(?P<a>[A-Z_]+)%?"\s*==\s*""\s*SET\s+[A-Z_]+=|'
    r'^\s*(?P<b>[A-Z_]+)=\$\{\d+:-)(?P<value>[^\s"}]+)', re.IGNORECASE | re.MULTILINE)


def android_requirements(engine):
    """The SDK pieces this engine's Android toolchain asks for.

    Read from Engine/Extras/Android/SetupAndroid.bat (or .sh), where Epic keeps
    the exact NDK, build-tools, platform and CMake versions per release.
    Returns {} when the engine has no such script.
    """
    extras = Path(engine.root) / "Engine" / "Extras" / "Android"
    for name in ("SetupAndroid.bat", "SetupAndroid.sh", "SetupAndroid.command"):
        try:
            text = (extras / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = {}
        for match in SETUP_DEFAULT_RE.finditer(text):
            key = (match.group("a") or match.group("b") or "").upper()
            if key in ("PLATFORMS_VERSION", "BUILDTOOLS_VERSION", "CMAKE_VERSION", "NDK_VERSION"):
                found.setdefault(key, match.group("value"))
        if found:
            return found
    return {}


def missing_android_pieces(engine, sdk_root, ndk_root=None):
    """Human-readable list of what the SDK lacks for this engine."""
    wanted = android_requirements(engine)
    if not wanted or not sdk_root:
        return []
    sdk = Path(sdk_root)
    missing = []
    checks = (("NDK_VERSION", sdk / "ndk", "ndk;%s"),
              ("BUILDTOOLS_VERSION", sdk / "build-tools", "build-tools;%s"),
              ("PLATFORMS_VERSION", sdk / "platforms", "platforms;%s"),
              ("CMAKE_VERSION", sdk / "cmake", "cmake;%s"))
    for key, folder, package in checks:
        version = wanted.get(key)
        if version and not (folder / version).is_dir():
            missing.append(package % version)
    ndk = wanted.get("NDK_VERSION")
    if ndk and ndk_root and Path(ndk_root).name != ndk and (sdk / "ndk" / ndk).is_dir():
        missing.append("NDKROOT points at %s, but UE %s wants NDK %s"
                       % (Path(ndk_root).name, engine.short_version, ndk))
    return missing


# ----------------------------------------------------------------- RunUAT
def build_cook_run_args(cfg, engine, uproject, platform, configuration, archive_dir,
                        distribution=False):
    """The BuildCookRun argument list for one platform."""
    info = platforms_mod.get(platform)
    settings = cfg.platform(info.name)
    args = [
        "BuildCookRun",
        "-project=%s" % uproject,
        "-noP4",
        "-utf8output",
        "-unattended",
        "-platform=%s" % info.ue,
        "-clientconfig=%s" % configuration,
        "-build",
        "-cook",
        "-stage",
        "-pak",
        "-iostore",
        "-compressed",
        "-archive",
        "-archivedirectory=%s" % archive_dir,
        "-unrealexe=%s" % engine.editor_cmd,
    ]
    if configuration in ("Shipping", "Test"):
        args.append("-nodebuginfo")
    if info.name == "Android":
        args.append("-cookflavor=%s" % (settings.get("cook_flavor") or "ASTC"))
        args.append("-package")
        if distribution:
            args.append("-distribution")
    elif info.name == "Windows" and settings.get("prereqs", True):
        args.append("-prereqs")
    args.extend(str(a) for a in settings.get("extra_args") or [])
    return args


class UATFailure(RuntimeError):
    def __init__(self, returncode, counters, tail):
        self.returncode = returncode
        self.counters = counters
        self.tail = tail
        first_error = next((line for line in tail if ERROR_RE.search(line)), "")
        detail = first_error.strip()[:300] or "see the UAT log"
        super().__init__("RunUAT exited %s: %s" % (returncode, detail))


def run_uat(engine, args, log_file, logger, timeout_seconds=None, env=None, on_start=None):
    """Run RunUAT, streaming to ``log_file``. Returns a result summary."""
    counters = {"errors": 0, "warnings": 0, "exitCode": None}
    # Line-buffered: the dashboard tails this file while the build runs.
    handle = open(log_file, "w", encoding="utf-8", errors="replace", buffering=1)

    def on_line(line):
        handle.write(line + "\n")
        if ERROR_RE.search(line):
            counters["errors"] += 1
            logger.warning("uat| %s", line[:400])
        elif WARNING_RE.search(line):
            counters["warnings"] += 1
        match = EXITCODE_RE.search(line)
        if match:
            counters["exitCode"] = int(match.group(1))

    command = [str(engine.uat)] + [str(a) for a in args]
    logger.info("running: %s %s", engine.uat.name, " ".join(str(a) for a in args))
    try:
        rc, tail = run(command, env=env or os.environ.copy(), timeout=timeout_seconds,
                       on_line=on_line, check=False, on_start=on_start)
    finally:
        handle.flush()
        handle.close()
    counters["returnCode"] = rc
    counters["tail"] = tail
    if rc != 0:
        raise UATFailure(rc, counters, tail)
    return counters
