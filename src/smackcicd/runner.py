# SPDX-License-Identifier: GPL-3.0-or-later
"""The build pipeline: tag in, packaged and published artifacts out."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import artifacts as artifacts_mod
from . import engines as engines_mod
from . import manifest as manifest_mod
from . import platforms as platforms_mod
from . import publish as publish_mod
from . import tagspec
from .control import CONTROL
from .forge import from_config as forge_from_config
from .inifile import set_keys
from .logging_setup import BuildLogFile, get_logger
from .state import State
from .util import (SCRUB, CommandError, ensure_dir, human_duration, iso, process_alive, stamp,
                   utc_now)
from .workspace import TagNotFound, Workspace

ANDROID_SECTION = "/Script/AndroidRuntimeSettings.AndroidRuntimeSettings"
PROJECT_SECTION = "/Script/EngineSettings.GeneralProjectSettings"
TOUCHED_CONFIGS = ["Config/DefaultGame.ini", "Config/DefaultEngine.ini"]


class BuildLock:
    """Cross-process guard: one Unreal build per machine at a time."""

    def __init__(self, path, logger):
        self.path = Path(path)
        self.logger = logger
        self._held = False

    def acquire(self, wait_seconds=0, poll=5):
        if self._held:
            return True
        ensure_dir(self.path.parent)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(handle, ("%d\n%s\n" % (os.getpid(), iso())).encode("utf-8"))
                os.close(handle)
                self._held = True
                return True
            except FileExistsError:
                if self._stale():
                    self.logger.warning("removing stale build lock %s", self.path)
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(poll)

    def _stale(self):
        """A lock is stale only if the process that wrote it is gone."""
        try:
            pid = int(self.path.read_text(encoding="utf-8").splitlines()[0])
        except (OSError, ValueError, IndexError):
            return True
        return not process_alive(pid)

    def release(self):
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


class Pipeline:
    def __init__(self, cfg, state=None, logger=None):
        self.cfg = cfg
        self.log = logger or get_logger("runner")
        self.state = state or State(cfg.state_db)
        self.forge = forge_from_config(cfg)
        self.workspace = Workspace(cfg, self.log)

    # -- signing and versioning ---------------------------------------------
    def _android_signing(self, spec):
        """Stage the release keystore into Build/Android and return ini keys."""
        if "Android" not in spec.platforms:
            return {}, "n/a", None
        keystore = self.cfg.get("signing.android.keystore")
        if keystore:
            keystore = self.cfg.path_of("signing.android.keystore")
        alias = self.cfg.get("signing.android.alias")
        if not keystore or not Path(keystore).exists():
            if keystore:
                self.log.warning("keystore %s not found; using Unreal's debug keystore", keystore)
            return {}, "debug", None
        store_password = self.cfg.secret("signing.android.store_password_env")
        key_password = self.cfg.secret("signing.android.key_password_env") or store_password
        SCRUB.add(store_password)
        SCRUB.add(key_password)
        if not store_password or not alias:
            self.log.warning("keystore configured but alias or password missing; "
                             "using the debug keystore")
            return {}, "debug", None
        target_dir = ensure_dir(self.workspace.path / "Build" / "Android")
        staged = target_dir / Path(keystore).name
        shutil.copy2(keystore, staged)
        self.log.info("staged release keystore as Build/Android/%s", staged.name)
        return ({"KeyStore": staged.name, "KeyAlias": alias,
                 "KeyStorePassword": store_password, "KeyPassword": key_password},
                "release-keystore", staged)

    def stamp_version(self, spec, short_sha, signing_keys):
        config_dir = self.workspace.path / "Config"
        stamped = {}
        if self.cfg.get("versioning.inject_project_version", True):
            project_version = "%s+%s" % (spec.semver, short_sha)
            set_keys(config_dir / "DefaultGame.ini", PROJECT_SECTION,
                     {"ProjectVersion": project_version})
            stamped["ProjectVersion"] = project_version
        android_keys = {}
        if "Android" in spec.platforms and self.cfg.get("versioning.inject_android_version", True):
            android_keys = {"StoreVersion": str(spec.android_version_code),
                            "VersionDisplayName": spec.semver}
            stamped.update(android_keys)
        android_keys.update(signing_keys)
        if android_keys:
            set_keys(config_dir / "DefaultEngine.ini", ANDROID_SECTION, android_keys)
        if stamped:
            self.log.info("stamped version: %s",
                          ", ".join("%s=%s" % kv for kv in stamped.items()))
        return stamped

    def _wants_distribution(self, platform, configuration, signing_mode):
        if platform != "Android":
            return False
        setting = self.cfg.platform("Android").get("distribution", "auto")
        if isinstance(setting, bool):
            return setting
        if str(setting).lower() == "auto":
            return signing_mode == "release-keystore" and configuration in ("Shipping", "Test")
        return str(setting).lower() in ("true", "yes", "1")

    def _platform_env(self, platform):
        """The build environment: this process's, plus platforms.<P>.env."""
        env = os.environ.copy()
        for key, value in (self.cfg.platform(platform).get("env") or {}).items():
            env[str(key)] = str(value)
        return env

    # -- one platform ---------------------------------------------------------
    def build_platform(self, spec, platform, engine, source, project, runner_info,
                       job_id, commit_sha, signing_mode, label=None):
        started = utc_now()
        info = platforms_mod.get(platform)
        label = label or platform
        build_number = self.state.next_build_number()
        build_id = "%s-%s-%s" % (stamp(started), spec.slug(), label)
        configuration = spec.configuration
        build_row = self.state.start_build(build_number, job_id, spec.tag, label,
                                           configuration, commit_sha)

        log_dir = ensure_dir(self.cfg.log_root / spec.slug())
        uat_log = log_dir / ("%s-uat.log" % label)
        build_log = log_dir / ("%s-build.log" % label)
        CONTROL.begin_platform(platform, uat_log, build_number)

        archive_dir = artifacts_mod.archive_dir(self.cfg, spec, label)
        if archive_dir.exists():
            shutil.rmtree(archive_dir, ignore_errors=True)
        ensure_dir(archive_dir)

        distribution = self._wants_distribution(platform, configuration, signing_mode)
        uproject = self.cfg.uproject
        args = engines_mod.build_cook_run_args(self.cfg, engine, str(uproject), platform,
                                               configuration, str(archive_dir),
                                               distribution=distribution)
        target = {
            "platform": platform,
            "label": label,
            "engineVersion": engine.short_version,
            "unrealPlatform": info.ue,
            "configuration": configuration,
            "distribution": distribution,
            "cookFlavor": self.cfg.platform("Android").get("cook_flavor")
            if platform == "Android" else None,
            "signing": signing_mode if platform == "Android" else "n/a",
            "architectures": list(info.architectures),
        }

        result, error, uat_summary, collected = "success", None, {}, []
        with BuildLogFile(build_log):
            self.log.info("=== build #%d  %s  %s  %s  UE%s ===", build_number, spec.tag,
                          label, configuration, engine.short_version)
            timeout = int(self.cfg.get("runner.build_timeout_minutes", 300)) * 60
            try:
                uat_summary = engines_mod.run_uat(engine, args, uat_log, self.log,
                                                  timeout_seconds=timeout,
                                                  env=self._platform_env(platform),
                                                  on_start=CONTROL.set_proc)
            except engines_mod.UATFailure as exc:
                uat_summary = exc.counters
                if CONTROL.cancelled:
                    result = "cancelled"
                    error = "cancelled by %s" % (CONTROL.cancel_requested_by or "operator")
                    self.log.warning("packaging %s", error)
                else:
                    result, error = "failure", str(exc)
                    self.log.error("packaging failed: %s", error)
            except CommandError as exc:
                result, error = "failure", "RunUAT could not run: %s" % exc
                self.log.error(error)

            if result == "success":
                try:
                    collected = artifacts_mod.collect(self.cfg, spec, label, platform,
                                                      project["name"], self.log)
                    if not collected:
                        result = "failure"
                        error = "packaging reported success but produced no artifacts"
                        self.log.error(error)
                except OSError as exc:
                    result, error = "failure", "collecting artifacts failed: %s" % exc
                    self.log.error(error)

        finished = utc_now()
        duration = int((finished - started).total_seconds())
        entry = manifest_mod.build_manifest(
            spec=spec, build_number=build_number, build_id=build_id,
            started_at=iso(started), finished_at=iso(finished), duration_seconds=duration,
            result=result, engine=engine.describe(), project=project, source=source,
            runner=runner_info, artifacts=collected, target=target,
            uat={"arguments": [str(a) for a in args],
                 "returnCode": uat_summary.get("returnCode"),
                 "reportedExitCode": uat_summary.get("exitCode"),
                 "errorLines": uat_summary.get("errors", 0),
                 "warningLines": uat_summary.get("warnings", 0)},
            logs={"uat": str(uat_log), "build": str(build_log)},
            error=error)
        manifest_path = manifest_mod.write(entry, archive_dir / "manifest.json")
        self.state.finish_build(build_row, result, started, manifest=entry,
                                manifest_path=manifest_path, log_path=uat_log,
                                drop_path=archive_dir, error=error)
        self.log.info("build #%d %s in %s", build_number, result, human_duration(duration))
        return entry

    # -- whole job --------------------------------------------------------------
    def run_tag(self, tag, job_id=None, platform_override=None, engine_override=None):
        spec = tagspec.parse(tag, self.cfg, platform_override=platform_override,
                             engine_override=engine_override)
        self.log.info("job: %s", spec.summary())
        if not self.workspace.exists():
            raise RuntimeError("workspace %s is not a git clone -- run `smackcicd init` "
                               "with --clone, or point workspace.path at a clone"
                               % self.workspace.path)

        self.workspace.fetch()
        commit_sha = self.workspace.checkout_tag(spec.tag)
        commit_info = self.workspace.commit_info(commit_sha)
        source = manifest_mod.source_block(
            commit_info, self.workspace.tag_info(spec.tag),
            self.workspace.branches_containing(commit_sha), self.workspace.submodule_info(),
            self.cfg.remote_url, self.workspace.is_dirty())
        # Resolve every engine up front: a missing one should fail in seconds,
        # not after the first engine has spent an hour packaging.
        uproject = self.cfg.uproject
        engines = [engines_mod.find_engine(self.cfg, uproject, version)
                   for version in (spec.engines or [None])]
        multi_engine = len(engines) > 1
        project = manifest_mod.project_facts(self.workspace.path, uproject,
                                             self.cfg.project_name)
        runner_info = manifest_mod.runner_facts()
        self.log.info("engine %s, commit %s (%s)", " + ".join(e.version_string for e in engines),
                      commit_info["shortCommit"], commit_info["subject"][:60])

        publish_mod.set_commit_status(
            self.cfg, self.forge, commit_sha, "pending",
            "packaging %s for %s" % (spec.semver, ", ".join(spec.platforms)), logger=self.log)

        signing_keys, signing_mode, staged_keystore = self._android_signing(spec)
        per_platform = []
        try:
            self.stamp_version(spec, commit_info["shortCommit"], signing_keys)
            for engine in engines:
                for platform in spec.platforms:
                    label = ("%s-UE%s" % (platform, engine.short_version) if multi_engine
                             else platform)
                    per_platform.append(self.build_platform(
                        spec, platform, engine, source, project, runner_info, job_id,
                        commit_sha, signing_mode, label=label))
                    if CONTROL.cancelled:
                        break
                if CONTROL.cancelled:
                    self.log.warning("cancelled; skipping remaining targets")
                    break
        finally:
            if staged_keystore and staged_keystore.exists():
                staged_keystore.unlink(missing_ok=True)
            self.workspace.restore(*TOUCHED_CONFIGS)

        combined = manifest_mod.combined_manifest(
            spec, source, [e.describe() for e in engines], project, runner_info, per_platform)
        root = artifacts_mod.build_root(self.cfg, spec)
        combined_path = manifest_mod.write(combined, root / "manifest.json")
        checksums = artifacts_mod.write_checksums(root, per_platform)
        manifest_mod.update_index(self.cfg.drop_root, combined)

        publish_result = publish_mod.publish_release(
            self.cfg, self.forge, spec, combined, per_platform, [combined_path, checksums],
            self.log)
        combined["publish"] = publish_result
        manifest_mod.write(combined, combined_path)

        state = "success" if combined["result"] == "success" else "failure"
        publish_mod.set_commit_status(
            self.cfg, self.forge, commit_sha, state, "%s %s" % (spec.semver, combined["result"]),
            target_url=publish_result.get("releaseUrl", ""), logger=self.log)
        publish_mod.notify(self.cfg, {
            "summary": "%s %s: %s" % (spec.tag, combined["result"], ", ".join(spec.platforms)),
            "tag": spec.tag,
            "result": combined["result"],
            "release": publish_result.get("releaseUrl", ""),
        }, self.log)

        artifacts_mod.prune(self.cfg.drop_root, int(self.cfg.get("artifacts.retain_builds", 20)),
                            self.log, protect=[root])
        self.log.info("job finished: %s (%s)", combined["result"], root)
        return combined


def run_job(cfg, state, job, logger):
    """Execute one queued job, updating its row as it goes."""
    pipeline = Pipeline(cfg, state, logger)
    lock = BuildLock(cfg.lock_path, logger)
    if not lock.acquire(wait_seconds=6 * 60 * 60):
        state.finish_job(job["id"], "failed", "another build holds the lock")
        raise RuntimeError("timed out waiting for the build lock")
    try:
        CONTROL.begin(job)
        platforms = json.loads(job["platforms"]) if job.get("platforms") else None
        combined = pipeline.run_tag(job["tag"], job_id=job["id"], platform_override=platforms)
        state.finish_job(job["id"], combined["result"])
        return combined
    except (tagspec.TagRejected, TagNotFound) as exc:
        logger.info("skipping %s: %s", job["tag"], exc)
        state.finish_job(job["id"], "skipped", str(exc))
        return None
    except Exception as exc:
        logger.exception("job for %s failed", job["tag"])
        state.finish_job(job["id"], "failed", str(exc))
        raise
    finally:
        CONTROL.end()
        lock.release()
