# Tag grammar

Push an **annotated or lightweight tag** that matches `triggers.tag_pattern`
(by default `v<major>.<minor>.<patch>…`) and the runner builds it.

```
v<major>.<minor>.<patch>[-<prerelease>][+<metadata>]
```

## Channel → configuration

The first word of the prerelease is the channel; a number after it is the
build number within the channel.

| Tag | Channel | Configuration | Pre-release |
| --- | --- | --- | --- |
| `v1.4.2` | release | Shipping | no |
| `v1.4.2-rc.1` | rc | Shipping | yes |
| `v1.4.2-beta.3` | beta | Development | yes |
| `v1.4.2-alpha.1` | alpha | Development | yes |
| `v1.4.2-nightly.7` | (unknown) | Development | yes |

Channels are configurable; see [`[channels]`](configuration.md#channels).

## Platforms

Platform words anywhere in the prerelease or metadata select platforms. With
none, `platforms.default` applies.

| Word | Platform |
| --- | --- |
| `win`, `win64`, `windows`, `pc` | Windows |
| `android`, `quest`, `apk` | Android |
| `linux` | Linux |
| `all`, `both`, `any` | every enabled platform |

```
v1.4.2-beta.1+android       Android only
v1.4.2-beta.1+win.linux     Windows and Linux
v1.4.2-quest                Shipping, Android only
```

A platform that is disabled in the config is dropped. A tag that selects no
enabled platform is ignored.

## Engines

`ue<major><minor>` or `ue<major>_<minor>` picks the Unreal install, which makes
an engine migration testable without changing the project:

```
v1.4.2-beta.1+ue54          build with Unreal 5.4
v1.4.2-beta.1+ue53.ue54     build twice, once per engine
v1.4.2-beta.1+ue5_10        Unreal 5.10
```

When a tag builds with several engines, each engine gets its own drop folder
(`Windows-UE5.3`, `Windows-UE5.4`), so the runs cannot overwrite each other.
With no engine word: `engine.default`, else the `.uproject`'s
`EngineAssociation`.

Selector words are removed from the version: `v2.0.1-beta.4+android.ue54` has
the version `2.0.1-beta.4`.

## Android version code

Every tag gets a version code that always increases with the version:

```
((major * 100 + minor) * 100 + patch) * 1000 + rank * 100 + number
```

`v1.2.3-beta.2` is `10203302`, and `v1.2.3` is `10203900`. A release
always outranks its own pre-releases, so testers can update in place. This is
valid while `major` ≤ 209, since Android caps version codes at 2,100,000,000.
`versioning.android_version_code_offset` shifts every code, for a store
listing that already used higher ones.

## Try it

```bash
smackcicd explain v1.4.2-beta.3+android.ue54
```

shows the configuration, platforms, engines, version code and drop folder,
without building anything.
