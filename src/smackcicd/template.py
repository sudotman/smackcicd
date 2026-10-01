# SPDX-License-Identifier: GPL-3.0-or-later
"""The commented smackcicd.toml that `smackcicd init` writes."""

from __future__ import annotations

from string import Template

from .tomlw import value

TEMPLATE = Template("""\
# smackcicd configuration -- written by `smackcicd init` on $created.
#
# Relative paths are relative to this file's folder (the runner's home).
# Secrets never go in this file: settings ending in _env name an environment
# variable, and secrets.env next to this file is loaded at start-up.
# Every setting is documented in docs/configuration.md.

[project]
# The .uproject, relative to the workspace root. Empty: the only .uproject
# there, which keeps working if the project is renamed.
uproject = $project_uproject

[repo]
url = $repo_url
# gitea (also Forgejo) | github | none
forge = $repo_forge
# Empty: derived from url.
api_url = $repo_api_url
owner = $repo_owner
name = $repo_name
token_env = "SMACKCICD_FORGE_TOKEN"
# Fetch over HTTPS with the token above instead of stored git credentials.
# GitHub: "x-access-token". Gitea: your Gitea user name. Empty: off.
git_user = $repo_git_user

[workspace]
# The build checkout. It belongs to the runner: each build hard-resets and
# cleans it, so never point this at a checkout you work in.
path = $workspace_path
# fast keeps DerivedDataCache, Intermediate, Binaries, Saved and plugin builds
# between builds; standard keeps only DerivedDataCache and Saved; deep nothing.
clean = "fast"
submodules = true
# auto runs `git lfs pull` when .gitattributes uses LFS.
lfs = "auto"

[engine]
# Unreal installs found on this machine. A tag can pick one with +ue54 etc.
versions = $engine_versions
# Engine(s) for tags that name none. Empty: the .uproject's EngineAssociation.
default = []
# Extra folders containing UE_x.y installs, searched in addition to the
# registry, the Epic launcher's records and the usual install locations.
search_dirs = []

[triggers]
tag_pattern = '^v\\d+\\.\\d+\\.\\d+'
poll = true
poll_seconds = 60
build_backlog_on_first_run = false

[server]
host = "0.0.0.0"
port = $server_port
# How your team reaches this machine, for download links in release notes.
# Empty: derived from this machine's LAN address.
public_url = $server_public_url
webhook_path = "/webhook"
webhook_secret_env = "SMACKCICD_WEBHOOK_SECRET"
admin_token_env = "SMACKCICD_ADMIN_TOKEN"

[platforms]
# What a tag builds when it names no platform (v1.2.0-beta.1+android picks one).
default = $platforms_default

[platforms.Windows]
enabled = $windows_enabled
prereqs = true
zip = true
extra_args = []

[platforms.Android]
enabled = $android_enabled
cook_flavor = "ASTC"
# auto: a distribution build when a release keystore is configured and the
# configuration is Shipping.
distribution = "auto"
zip = true
extra_args = []
# Passed to Android builds only, so nothing has to be set machine-wide.
env = $android_env

[platforms.Linux]
enabled = $linux_enabled
zip = true
extra_args = []

[signing.android]
# A release keystore. Empty: Unreal's debug key (fine for QA, not for stores).
# Back the keystore up: if it is lost, installed apps can never be updated.
keystore = ""
alias = ""
store_password_env = "SMACKCICD_ANDROID_STORE_PASSWORD"
key_password_env = "SMACKCICD_ANDROID_KEY_PASSWORD"

[artifacts]
path = "artifacts"
retain_builds = 20

[publish]
release = true
# Attached to the forge release. Zips and OBBs are linked from the dashboard
# instead -- multi-GB uploads are slow, and some forges cap them.
upload = ["apk", "aab", "manifest", "checksums"]
max_upload_mb = 0
commit_status = true
download_links = true

[notify]
# POSTed to when a job finishes. kind: generic | slack | discord | teams
url = ""
kind = "generic"

[runner]
build_timeout_minutes = 300
logs = "logs"
state = "state"
""")


def render(values):
    """``values`` maps the template's placeholder names to Python values."""
    formatted = {name: (raw if name == "created" else value(raw))
                 for name, raw in values.items()}
    return TEMPLATE.substitute(formatted)
