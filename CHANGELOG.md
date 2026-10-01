# Changelog

All notable changes are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## 0.1.0 - unreleased

First public version, generalised from a production build runner.

- `init` detects the project, Unreal installs (registry, Epic launcher,
  source builds, common folders), the Android SDK/NDK/JDK, the git remote and
  the forge, and writes a commented `smackcicd.toml`.
- Tag-triggered builds: channel → configuration, platform and engine
  selectors in the tag, monotonic Android version codes.
- Windows, Android and Linux packaging through `RunUAT BuildCookRun`, with the
  UE 5.8 and older archive layouts.
- Gitea, Forgejo and GitHub (including Enterprise Server): releases, asset
  upload, commit status, signed webhooks, polling fallback.
- Download links in release notes. Large packages are linked, not attached.
- Dashboard: build history, downloads, logs, rebuild, cancel, pause, delete.
- `doctor`, including each engine's exact Android SDK requirements.
- `service install` for Windows (scheduled task with a watchdog) and Linux
  (systemd user unit).
