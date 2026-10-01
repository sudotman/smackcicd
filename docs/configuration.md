# Configuration

A runner lives in one folder, its **home**. The home holds:

```
smackcicd.toml    settings (this page)
secrets.env       tokens and passwords -- never commit this
workspace/        the build clone (configurable)
artifacts/        packaged builds, one folder per tag
logs/             smackcicd.log and per-build UAT logs
state/            the build history database and the build lock
```

`smackcicd init` writes both files. Edit them freely; restart the service
(`smackcicd service restart`) to apply changes.

**Which home is used:** `--home <folder>`, else `$SMACKCICD_HOME`, else the
current folder if it contains a `smackcicd.toml`, else the per-OS default
(`%ProgramData%\smackcicd` on Windows, `~/.local/share/smackcicd` elsewhere).

**Paths** in the config may be relative; they resolve against the home.

**Secrets** are never stored in the TOML. Settings ending in `_env` name an
environment variable. `secrets.env` is loaded at start-up, but a variable
already set in the environment (by a service manager, say) takes precedence.
Secret values are scrubbed from every log line.

**Validation:** a value of the wrong type stops the runner with a message
naming the setting. An unknown setting is a warning, shown by `doctor` and at
start-up.

## `[project]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `uproject` | `""` | The `.uproject`, relative to the workspace. Empty: the only `.uproject` at the workspace root. A configured file that no longer exists falls back to that too, so renaming the project does not break older tags. |
| `name` | `""` | Project name for zip names and manifests. Empty: the `.uproject` file name. |

## `[repo]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `url` | `""` | Clone/fetch URL: `https://...`, `ssh://...` or `git@host:owner/repo.git`. |
| `forge` | `"auto"` | `gitea` (also Forgejo), `github`, `none`, or `auto` to detect at start-up. `init` writes the detected value. |
| `api_url` | `""` | Forge API root. Empty: `https://api.github.com`, `https://<host>/api/v3` for GitHub Enterprise, `<web root>/api/v1` for Gitea. |
| `owner`, `name` | `""` | Repository owner and name. Empty: parsed from `url`. |
| `token_env` | `"SMACKCICD_FORGE_TOKEN"` | Variable holding the forge API token. Without one, builds still run; releases and commit status are skipped. |
| `git_user` | `""` | When set, git fetches over HTTPS with this user name and the forge token, sent as a header for that command only. GitHub: `x-access-token`. Gitea: your Gitea user name. Empty: git uses whatever credentials the account has. |
| `verify_tls` | `true` | Set `false` only for a forge with a self-signed certificate on a trusted network. |

## `[workspace]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `path` | `"workspace"` | The build clone. **It belongs to the runner**: every build hard-resets and cleans it. Never point it at a checkout you work in. |
| `clean` | `"fast"` | What survives `git clean` between builds. `fast`: DerivedDataCache, Intermediate, Binaries, Saved, Build and every plugin's Intermediate and Binaries. `standard`: DerivedDataCache and Saved. `deep`: nothing. |
| `submodules` | `true` | Sync and update submodules after checkout. |
| `lfs` | `"auto"` | `auto` runs `git lfs pull` when `.gitattributes` uses LFS; `true` always; `false` never. |

## `[engine]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `path` | `""` | Use this engine root for every build, ignoring tags and the `.uproject`. |
| `versions` | `{}` | `{ "5.4" = "C:/Program Files/Epic Games/UE_5.4" }`. Pins or adds installs; they take priority over discovery. `init` fills in what it finds. |
| `default` | `[]` | Engine version(s) for tags that name none. Empty: the `.uproject`'s `EngineAssociation` (a version, or a source build's GUID). |
| `search_dirs` | `[]` | Extra folders that contain `UE_x.y` installs or are an engine root. |

Engines are discovered from the Windows registry (launcher installs and
source builds registered by UnrealVersionSelector), the Epic launcher's
`LauncherInstalled.dat`, Linux/macOS `Install.ini`, and the usual folders on
every fixed drive (`Program Files/Epic Games`, `Epic Games`, `Unreal Engine`,
`UnrealEngine`).

## `[triggers]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `tag_pattern` | `'^v\d+\.\d+\.\d+'` | Only tags matching this regular expression build. |
| `poll` | `true` | Poll the forge (or `git ls-remote`) for new tags. Keep it on even with a webhook; it is the safety net. |
| `poll_seconds` | `60` | How often to poll (minimum 10). |
| `build_backlog_on_first_run` | `false` | On a brand-new runner, build tags that already exist. Usually you want only new tags. |

## `[server]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Run the HTTP server (webhook and dashboard). |
| `host`, `port` | `"0.0.0.0"`, `9099` | Where it listens. |
| `public_url` | `""` | How your team reaches the dashboard, used for download links in release notes, e.g. `http://buildpc.studio.lan:9099`. Empty: derived from the address this machine uses to reach the forge. |
| `webhook` | `true` | Accept webhook deliveries. |
| `webhook_path` | `"/webhook"` | The webhook route. |
| `webhook_secret_env` | `"SMACKCICD_WEBHOOK_SECRET"` | Shared secret for signed deliveries. Empty: unsigned deliveries are accepted (a warning is logged). |
| `dashboard` | `true` | Serve the dashboard. |
| `allow_actions` | `true` | Allow rebuild, cancel, pause and delete from the dashboard, given the admin token. |
| `admin_token_env` | `"SMACKCICD_ADMIN_TOKEN"` | The admin token. Empty: the dashboard is read-only. |

## `[platforms]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `default` | `[]` | What a tag builds when it names no platform. Empty: every enabled platform this machine can build. |

Each platform has its own table: `[platforms.Windows]`, `[platforms.Android]`,
`[platforms.Linux]`.

| Setting | Platforms | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | all | Windows and Android `true`, Linux `false` | Whether tags may build it. |
| `zip` | all | `true` | Zip the package into one download. |
| `extra_args` | all | `[]` | Extra `BuildCookRun` arguments, e.g. `["-nocompileeditor"]`. |
| `env` | all | `{}` | Environment variables for this platform's builds only. `init` puts the Android SDK/NDK/JDK locations here when they are not set system-wide. |
| `prereqs` | Windows | `true` | Include Unreal's prerequisites installer. |
| `cook_flavor` | Android | `"ASTC"` | Texture format: `ASTC`, `ETC2`, `DXT` or `Multi`. |
| `distribution` | Android | `"auto"` | A distribution (store) build. `auto`: when a release keystore is configured and the configuration is Shipping. Or `true` / `false`. |

## `[channels]`

The first word of a tag's prerelease picks the channel. Each channel maps to a
build configuration, decides whether the release is marked pre-release, and
has a rank used in the Android version code.

```toml
[channels.alpha]
configuration = "Development"
prerelease = true
rank = 1
```

Defaults: `alpha` (Development, rank 1), `beta` (Development, 3), `rc`
(Shipping, 6), a plain version (Shipping, 9). Any other word uses `_unknown`
(Development, 0). Add your own, such as `[channels.nightly]` with
`configuration = "DebugGame"`. Ranks must stay below 10 and increase towards a
release.

## `[signing.android]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `keystore` | `""` | A release keystore file. Empty: Unreal's debug key. |
| `alias` | `""` | The key alias in the keystore. |
| `store_password_env` | `"SMACKCICD_ANDROID_STORE_PASSWORD"` | Keystore password variable. |
| `key_password_env` | `"SMACKCICD_ANDROID_KEY_PASSWORD"` | Key password variable. Empty: the store password. |

See [Android](android.md) for creating one, and back it up.

## `[versioning]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `inject_project_version` | `true` | Set `ProjectVersion` to `<semver>+<commit>` during the build. |
| `inject_android_version` | `true` | Set Android `StoreVersion` (the version code) and `VersionDisplayName`. |
| `android_version_code_offset` | `0` | Added to every version code, to continue above codes already used in a store. |

## `[artifacts]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `path` | `"artifacts"` | Where packaged builds are kept, one folder per tag. |
| `retain_builds` | `20` | Tag folders to keep; older ones are deleted after each build. `0` keeps everything. |

## `[publish]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `release` | `true` | Create or update a release on the forge for each tag. |
| `upload` | `["apk", "aab", "manifest", "checksums"]` | Artifact types attached to the release. Also available: `archive` (the zips), `obb`, `installer`, `executable`, `symbols`. |
| `max_upload_mb` | `0` | Skip attachments larger than this. `0`: no limit of our own. GitHub's own cap of just under 2 GiB is always respected. |
| `commit_status` | `true` | Report pending/success/failure on the tagged commit. |
| `status_context` | `"smackcicd"` | The commit status name. |
| `download_links` | `true` | Put dashboard download links for each platform's zip (and the APK) in the release notes. |

Large packages are linked rather than attached on purpose. Forges reply to an
upload only after storing the whole file. A multi-GB attachment can take many
minutes, and can strain a small server.

## `[notify]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `url` | `""` | POST here when a job finishes. Empty: off. |
| `kind` | `"generic"` | `slack`, `teams` (a `{"text": ...}` body), `discord` (`{"content": ...}`), or `generic` (the full JSON summary). |

## `[runner]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `build_timeout_minutes` | `300` | Kill a platform build that runs longer than this. |
| `logs` | `"logs"` | The log folder. |
| `state` | `"state"` | The history database and build lock folder. |

## `[service]`

| Setting | Default | Meaning |
| --- | --- | --- |
| `name` | `"smackcicd"` | Scheduled task / systemd unit name. Give each runner on one machine its own. |

## `secrets.env`

```sh
SMACKCICD_FORGE_TOKEN=...
SMACKCICD_WEBHOOK_SECRET=...      # generated by init
SMACKCICD_ADMIN_TOKEN=...         # generated by init; paste into the dashboard once
SMACKCICD_ANDROID_STORE_PASSWORD=
SMACKCICD_ANDROID_KEY_PASSWORD=
```

On Linux `init` makes it readable by its owner only. On Windows, keep the home
folder somewhere only the runner's account and administrators can read.
