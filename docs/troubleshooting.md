# Troubleshooting

Start with `smackcicd doctor`. It checks the forge, the token, the workspace,
every engine, the Android SDK pieces each engine asks for, the folders and the
port.

Most of the problems below were found the hard way on a real build machine.
smackcicd handles them; they are listed so you recognise the symptoms.

## The build fails before it starts

**`Filename too long` from git clean or checkout.** Windows' `LongPathsEnabled`
is not enough; git needs its own `core.longpaths`. smackcicd passes
`-c core.longpaths=true` to every git command. For your own clones, run
`git config --global core.longpaths true`.

**`Unable to find plugin '...'`.** The project enables a plugin that is not in
the repository, usually a Fab/Marketplace plugin installed into someone's
engine. Either commit it under `Plugins/` (best: every clean checkout then
builds), or build it for this engine with
`RunUAT BuildPlugin -Plugin=<.uplugin> -Package=<out> -TargetPlatforms=Win64`
and copy the result into `Engine/Plugins/Marketplace/`. Don't leave it
untracked in the workspace: `git clean` removes it.

**`'C:\Program' is not recognized`.** A batch file was launched through
`cmd /c` with several quoted arguments. smackcicd runs `RunUAT.bat` directly;
if you wrap it in your own scripts, do the same.

**Prebuilt `.lib` / `.a` files missing from a clean checkout.** A `.gitignore`
rule for build output has swallowed a third-party plugin's prebuilt
libraries. Check `git status --ignored Plugins/<plugin>` and track them with
`!` exceptions.

## The build is slow

**Compiles run one at a time.** UnrealBuildToolAccelerator kills compile jobs
when committed memory nears the commit limit, then falls back to one job at a
time. The log shows `Low on memory ... Kill threshold`. Give the machine a
pagefile at least the size of its RAM, or cap parallelism in
`%APPDATA%\Unreal Engine\UnrealBuildTool\BuildConfiguration.xml`:

```xml
<Configuration xmlns="https://www.unrealengine.com/BuildConfiguration">
  <BuildConfiguration><MaxParallelActions>16</MaxParallelActions></BuildConfiguration>
</Configuration>
```

**`... is very slow (0.01 MiB/s); consider disabling this cache store`.**
Antivirus is scanning the local derived-data cache. Exclude the workspace,
`%LOCALAPPDATA%\UnrealEngine`, the engine folder, the Android SDK and
`~/.gradle` from real-time scanning. Don't set `UE-LocalDataCachePath=None`
on UE 5.8+: the Zen cache hangs off the same setting, and every build then
dies with `Unable to use cache graph 'Installed' because it has no writable
nodes`.

**Every build recompiles every plugin.** The workspace clean was deleting
plugin build output. The `fast` clean level keeps `Plugins/**/Intermediate`
and `Plugins/**/Binaries`.

## The build succeeds, but no artifacts

**"packaging reported success but produced no artifacts".** UE 5.8 archives
straight into `-archivedirectory`, where older engines used a `Windows\` or
`Android_ASTC\` subfolder. smackcicd accepts both. If you see this on another
layout, open an issue with the archive folder's listing.

## Android

**`Connection timed out` from `org.gradle.wrapper.Download`.** Gradle could
not download itself. Retry, or seed `~/.gradle` (see [Android](android.md)).

**The cook succeeds, then the build fails with exit code 1.** The cook
commandlet fails the build when content logs errors: broken Blueprints,
missing assets. The UAT log's `Error:` lines name them. They are content
fixes, not build machine problems.

## Publishing

**Uploads time out, or the forge goes unresponsive during an upload.** Forges
answer an upload only after storing the whole file, which for multi-GB files
on a small server can take many minutes. smackcicd waits up to 10 minutes plus
2 MB/s. By default only the APK and small files are attached, and the zips are
linked from the release notes instead.

**A Gitea webhook is never delivered.** See [Forges](forges.md#webhooks):
`ALLOWED_HOST_LIST`, or no route between the forge and the build machine.

## The service

**The daemon disappears.** Closing a console window kills a process that owns
one. The service runs under `pythonw.exe` and starts every child without a
window. A crash is logged to `smackcicd.log`; the watchdog trigger relaunches
it within five minutes.

**It registered `py.exe` or a Store alias.** It doesn't: the task runs the
interpreter smackcicd is installed into. (A PowerShell 5.1 quirk splits a
splatted `-3` into `- 3`, which makes Python read a script from stdin and
hang. That is why the installer never goes through `py`.)

**Cancelling a build on Linux killed the runner.** Fixed: builds run in their
own process group.

## Asking for help

Open an issue with the output of `smackcicd doctor`, the end of
`logs/smackcicd.log`, and, for a failed build, the last 100 lines of its UAT
log. Remove tokens and internal host names first; smackcicd scrubs its own
secrets from logs, but not anything else.
