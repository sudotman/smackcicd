# smackcicd

**Push a tag. Get a packaged Unreal Engine build.**

smackcicd is a small, self-hosted build runner for Unreal Engine projects. It
watches your git server for version tags, builds, cooks and packages the
project for Windows, Android and Linux, and publishes the result: a release on
your forge, commit status, checksums, and one-click download links on its own
dashboard.

It is built for studios and solo developers who already have a build machine
and a Gitea, Forgejo or GitHub repository, and want packaging to be boring.

- **No CI platform needed.** One Python process on the build machine. No agents,
  no YAML pipelines, no cloud runners, and no runtime dependencies beyond the
  Python standard library.
- **Set up by detection, not configuration.** `smackcicd init` finds your
  Unreal installs (registry, Epic launcher, source builds, the usual folders),
  the Android SDK/NDK/JDK, the project and the forge, and writes a commented
  config you can read.
- **Tag-driven.** `v1.4.2-beta.3` builds Development for every platform;
  `v1.4.2` builds Shipping; `+android` or `+win` narrows it; `+ue54` picks the
  engine. See [tag grammar](docs/tags.md).
- **Gitea, Forgejo and GitHub** (including Enterprise Server): releases, asset
  upload, commit status and signed webhooks, with polling as a fallback.
- **A dashboard** with build history, download buttons, logs, rebuild, cancel,
  pause and delete.
- **Built from real build-machine scars.** Long paths, UE 5.8's archive layout,
  multi-GB uploads, incremental plugin builds, a service that survives logoff
  and crashes. See [troubleshooting](docs/troubleshooting.md).

## Quickstart

On the build machine, with Python 3.11+, git and Unreal Engine installed:

```bash
pip install git+https://github.com/sudotman/smackcicd.git
smackcicd init          # detects what it can and asks for the rest
smackcicd doctor        # checks this machine can build
smackcicd service install
```

Then push a tag:

```bash
git tag -a v0.1.0-alpha.1 -m "first CI build" && git push origin v0.1.0-alpha.1
```

and open the dashboard that `init` printed (by default
`http://<build-machine>:9099/`). The tag is picked up within a minute by
polling, or instantly once you add the webhook (`smackcicd webhook` prints
what to enter).

## What a build does

1. Fetches the tag into a persistent workspace clone and hard-resets onto it,
   keeping derived data and intermediates so builds stay incremental.
2. Stamps the version into the project (`ProjectVersion`, and the Android
   `StoreVersion` / `VersionDisplayName`) and stages a release keystore when
   one is configured. Both edits are undone afterwards.
3. Runs `RunUAT BuildCookRun` once per platform (and per engine, for tags that
   name several), streaming the log to the dashboard.
4. Collects the packages, zips each platform into one file, and writes
   `manifest.json` (what was built, from what, with what) and
   `SHA256SUMS.txt`.
5. Creates or updates the forge release, attaches the APK and the build
   record, links the large zips from the release notes, sets commit status,
   and optionally posts to Slack, Discord, Teams or any URL.

## Commands

| Command | What it does |
| --- | --- |
| `smackcicd init` | Detect, ask, and write `smackcicd.toml` and `secrets.env` |
| `smackcicd doctor` | Check git, the forge, engines, Android SDK pieces, disk, ports |
| `smackcicd clone` | Create the build workspace clone (`--partial` for huge repos) |
| `smackcicd service install` | Run in the background: a Windows scheduled task or a systemd user unit |
| `smackcicd watch` | Run the daemon in the foreground |
| `smackcicd build <tag>` | Build one tag now, in the foreground |
| `smackcicd explain <tag>` | Show what a tag would build, without building |
| `smackcicd tags` | List remote tags and how each would build |
| `smackcicd status` | Recent builds and the queue |
| `smackcicd webhook` | What to enter on your forge's webhook page |

Every command takes `--home <folder>` to pick a runner; a machine can host several.

## Documentation

- [Configuration reference](docs/configuration.md)
- [Tag grammar](docs/tags.md)
- [Forges: Gitea, Forgejo, GitHub](docs/forges.md)
- [Android builds and signing](docs/android.md)
- [Running as a service](docs/service.md)
- [The dashboard](docs/dashboard.md)
- [Troubleshooting](docs/troubleshooting.md)

## Requirements

- Python 3.11 or newer (the standard library only).
- git, plus git-lfs if your repository uses LFS.
- Unreal Engine 4.27 or 5.x on the build machine. Launcher installs and
  source builds both work.
- For Android: the Android SDK, the NDK version your engine asks for, and
  JDK 17. `doctor` reads your engine's own requirements and tells you exactly
  which SDK packages are missing.
- Windows builds Windows, Android and Linux (cross-compile). Linux hosts build
  Linux and Android.

## Status

smackcicd is young. It grew out of a production build machine that packages
Windows and Meta Quest builds from Gitea tags, and was then generalised.
Bug reports and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

smackcicd is free software, licensed under the
[GNU General Public License v3.0 or later](LICENSE).

Unreal and Unreal Engine are trademarks or registered trademarks of Epic
Games, Inc. smackcicd is not affiliated with or endorsed by Epic Games.
