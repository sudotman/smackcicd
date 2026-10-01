# Android builds

Android packaging needs three things on the build machine:

- the **Android SDK**, with the platform, build-tools and CMake versions your
  engine asks for,
- the **NDK** version your engine asks for,
- **JDK 17**.

Each Unreal release pins exact versions in
`Engine/Extras/Android/SetupAndroid.bat` (or `.sh`). `smackcicd doctor` reads
that file for every installed engine and tells you precisely what is missing:

```
[warn] UE 5.8 Android builds need SDK package build-tools;36.1.0 -- install it with sdkmanager "build-tools;36.1.0"
[warn] NDKROOT points at 25.1.8937393, but UE 5.8 wants NDK 27.2.12479018
```

## Without Android Studio

Epic's `SetupAndroid.bat` expects Android Studio and takes the JDK from its
bundled runtime. A headless build machine does not need Studio:

1. Install a JDK 17, e.g. Microsoft Build of OpenJDK, Eclipse Temurin, or your
   Linux distribution's `openjdk-17-jdk`.
2. Download the Android **command-line tools**. Unpack them so that
   `<sdk>/cmdline-tools/latest/bin/sdkmanager` exists.
3. Install exactly what `doctor` lists, for example:

   ```bash
   sdkmanager --sdk_root=<sdk> "platform-tools" "platforms;android-36" \
     "build-tools;36.1.0" "cmake;3.22.1" "ndk;27.2.12479018"
   ```

4. Run `smackcicd init` again, or edit `[platforms.Android] env`, so builds
   get `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `NDKROOT`, `NDK_ROOT` and `JAVA_HOME`.

`init` writes those variables into the config only when they are not already
set system-wide. The service then needs no machine-level environment setup.

## Gradle

The first APK packaging downloads the Gradle distribution (via GitHub) and
then the Android and Maven libraries. On a machine with flaky or filtered
internet access, seed them from a machine that has built before: copy
`~/.gradle/wrapper/dists/gradle-<version>-all` and `~/.gradle/caches/modules-2`
into the runner account's home. The folder name inside `dists/` is a hash of
the download URL, so it matches across machines.

## Signing

Without a keystore, APKs are signed with Unreal's debug key. That is fine for
QA, but stores reject it, and **a debug-signed app cannot be updated in place
by a release-signed one**.

Create a release keystore once, ideally on the build machine:

```bash
keytool -genkeypair -keystore release.keystore -alias game -keyalg RSA \
  -keysize 2048 -validity 10000 -dname "CN=Your Studio"
```

Then configure it:

```toml
[signing.android]
keystore = "secrets/release.keystore"     # relative to the runner home
alias = "game"
```

and put the password in `secrets.env` as `SMACKCICD_ANDROID_STORE_PASSWORD`
(and `SMACKCICD_ANDROID_KEY_PASSWORD` if the key's password differs).

**Back the keystore up, with its password, somewhere other than the build
machine.** If it is lost, apps already installed can never be updated again.

The keystore is copied into the workspace only for the duration of an
Android build and removed afterwards. Its passwords are scrubbed from logs.

## Meta Quest and other headsets

Quest builds are ordinary Android builds. The `quest` tag word is an alias for
`android`: `v1.4.0-beta.1+quest`.
