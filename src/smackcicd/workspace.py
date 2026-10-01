# SPDX-License-Identifier: GPL-3.0-or-later
"""The persistent build checkout.

Unreal repositories are large, so the runner keeps one clone and moves it to
each tag instead of cloning per build. Derived data and intermediates are
deliberately kept between builds -- that is most of the speed.
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

from .util import SCRUB, CommandError, ensure_dir, run, which

# Never removed by `git clean`; these are what make an incremental build fast.
# Plugin build output matters as much as the project's: without it every
# build recompiles every plugin from scratch.
KEEP_BY_LEVEL = {
    "fast": ["DerivedDataCache", "Intermediate", "Binaries", "Saved", "Build", ".smackcicd",
             "Plugins/**/Intermediate", "Plugins/**/Binaries"],
    "standard": ["DerivedDataCache", "Saved", ".smackcicd"],
    "deep": [".smackcicd"],
}


class TagNotFound(RuntimeError):
    """The tag we were asked to build is not on the remote (any more)."""


class Workspace:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.path = Path(cfg.workspace)
        self.remote = cfg.remote_url

    def exists(self):
        return (self.path / ".git").exists()

    # -- git plumbing ------------------------------------------------------
    def _auth_args(self):
        """Per-command HTTPS credentials from the forge token, if configured.

        Sent as an http.extraHeader for this one command, so the token is never
        written into the clone's config or the URL.
        """
        user = self.cfg.get("repo.git_user")
        token = self.cfg.secret("repo.token_env")
        if not (user and token and self.remote.startswith("https://")):
            return []
        basic = base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")
        SCRUB.add(basic)
        return ["-c", "http.extraHeader=Authorization: Basic %s" % basic]

    def _cmd(self, args):
        """git, pinned to this workspace and tolerant of long paths.

        Windows' own LongPathsEnabled is not enough: git needs core.longpaths of
        its own, or clean and checkout fail with "Filename too long" on deep
        plugin and cache paths.
        """
        return (["git", "-C", str(self.path), "-c", "core.longpaths=true"]
                + self._auth_args() + [str(a) for a in args])

    def git(self, *args, check=True, quiet=False):
        sink = (lambda line: None) if quiet else self.log.debug
        return run(self._cmd(args), on_line=sink, check=check)

    def git_out(self, *args, check=True):
        lines = []
        rc, _ = run(self._cmd(args), on_line=lines.append, check=check)
        return "\n".join(lines).strip(), rc

    # -- lifecycle ---------------------------------------------------------
    def clone(self, partial=False):
        """Initial clone. Slow for a big repository -- expect a long first run.

        ``partial`` uses a blobless clone: history comes down now, file
        contents only for what a checkout needs. Much faster for huge repos.
        """
        if self.exists():
            self.log.info("workspace already present at %s", self.path)
            return
        if not self.remote:
            raise RuntimeError("repo.url is empty -- nothing to clone")
        ensure_dir(self.path.parent)
        self.log.info("cloning %s into %s (this can take a long time)", self.remote, self.path)
        command = ["git", "-c", "core.longpaths=true"] + self._auth_args() + ["clone", "--progress"]
        if partial:
            command.append("--filter=blob:none")
        run(command + [self.remote, str(self.path)], on_line=self.log.info)
        self.git("config", "core.longpaths", "true")

    def fetch(self):
        self.log.info("fetching tags from origin")
        if self.remote:
            self.git("remote", "set-url", "origin", self.remote)
        self.git("fetch", "--prune", "--prune-tags", "--tags", "--force", "origin")

    def checkout_tag(self, tag):
        """Hard-reset the workspace onto ``tag`` and return the commit sha."""
        _, rc = self.git_out("rev-parse", "--verify", "--quiet",
                             "refs/tags/%s^{commit}" % tag, check=False)
        if rc != 0:
            raise TagNotFound("tag %s does not exist on the remote (deleted, or the push "
                              "never landed)" % tag)
        self.log.info("checking out %s", tag)
        self.git("-c", "advice.detachedHead=false", "checkout", "--force", "--detach",
                 "refs/tags/%s" % tag)
        self.git("reset", "--hard", "HEAD")
        self.clean()
        if self.cfg.get("workspace.submodules", True):
            self.git("submodule", "sync", "--recursive", check=False)
            self.git("submodule", "update", "--init", "--recursive", "--force", check=False)
        self.lfs_pull()
        sha, _ = self.git_out("rev-parse", "HEAD")
        return sha

    def uses_lfs(self):
        attributes = self.path / ".gitattributes"
        try:
            return "filter=lfs" in attributes.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def lfs_pull(self):
        setting = str(self.cfg.get("workspace.lfs", "auto")).lower()
        if setting in ("false", "off", "no") or (setting == "auto" and not self.uses_lfs()):
            return
        if not which("git-lfs"):
            self.log.warning("the repository uses Git LFS but git-lfs is not installed; "
                             "LFS files will be pointer stubs and the build will fail")
            return
        self.log.info("pulling Git LFS objects")
        self.git("lfs", "pull")

    def clean(self):
        level = str(self.cfg.get("workspace.clean") or "fast").lower()
        keep = KEEP_BY_LEVEL.get(level, KEEP_BY_LEVEL["fast"])
        self.log.info("cleaning workspace (level=%s)", level)
        args = ["clean", "-ffdx"]
        for name in keep:
            args += ["-e", "/%s" % name]
        try:
            self.git(*args)
        except CommandError as exc:
            # Cleaning is hygiene, not correctness: one locked file must not
            # throw away an hour of packaging.
            self.log.warning("git clean did not finish cleanly (exit %s); continuing. %s",
                             exc.returncode, " | ".join(exc.tail[-3:]))

    def restore(self, *paths):
        """Undo our own working-tree edits (version and keystore injection)."""
        for item in paths:
            self.git("checkout", "--", item, check=False)

    # -- metadata ----------------------------------------------------------
    def commit_info(self, sha="HEAD"):
        fields = ["%H", "%h", "%an", "%ae", "%aI", "%cI", "%s"]
        out, _ = self.git_out("show", "-s", "--format=%s" % "%n".join(fields), sha)
        parts = (out.split("\n") + [""] * 7)[:7]
        return {
            "commit": parts[0], "shortCommit": parts[1], "authorName": parts[2],
            "authorEmail": parts[3], "authoredAt": parts[4], "committedAt": parts[5],
            "subject": parts[6],
        }

    def tag_info(self, tag):
        fmt = ("%(objecttype)%0a%(taggername)%0a%(taggeremail)%0a"
               "%(taggerdate:iso-strict)%0a%(contents:subject)")
        out, _ = self.git_out("for-each-ref", "--format=%s" % fmt, "refs/tags/%s" % tag,
                              check=False)
        parts = (out.split("\n") + [""] * 5)[:5]
        annotated = parts[0] == "tag"
        return {
            "tag": tag,
            "annotated": annotated,
            "taggerName": parts[1] if annotated else "",
            "taggerEmail": (parts[2] or "").strip("<>") if annotated else "",
            "taggedAt": parts[3] if annotated else "",
            "message": parts[4] if annotated else "",
        }

    def branches_containing(self, sha):
        out, rc = self.git_out("branch", "--remotes", "--contains", sha, check=False)
        if rc != 0 or not out:
            return []
        names = set()
        for line in out.splitlines():
            name = line.strip().lstrip("* ").strip()
            if name and "->" not in name:
                names.add(name.replace("origin/", "", 1))
        return sorted(names)

    def submodule_info(self):
        out, rc = self.git_out("submodule", "status", "--recursive", check=False)
        if rc != 0 or not out:
            return []
        entries = []
        for line in out.splitlines():
            parts = line.strip().lstrip("+-U").split()
            if len(parts) >= 2:
                entries.append({"commit": parts[0], "path": parts[1]})
        return entries

    def is_dirty(self):
        """True only if tracked files differ from the tag (kept caches are untracked)."""
        out, _ = self.git_out("status", "--porcelain", "--untracked-files=no")
        return bool(out.strip())

    def free_space_gb(self):
        target = self.path if self.path.exists() else self.path.parent
        while not target.exists() and target.parent != target:
            target = target.parent
        return shutil.disk_usage(str(target)).free / (1024 ** 3)
