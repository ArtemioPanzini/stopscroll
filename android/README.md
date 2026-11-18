# Stopscroll on Android

Stopscroll uses a local Android VPN to block domains and enforce daily time
limits. The phone has its own budgets, timezone and 04:00 reset. Once configured,
use the Stopscroll screen to open and close sessions. Elapsed time in a session
counts while the phone sleeps. USB is only needed for manual administration.

The G0 prototype was tested on Android 13 with Wi-Fi IPv4. A separate app and
Chrome confirmed that the VPN blocked fresh requests, cut existing flows and
kept working in Doze. The daily-limit release still needs device acceptance,
including a signed upgrade, reboot, Doze and operation with the cable unplugged.
Mobile data, roaming and IPv6 are outside the verified scope.

## Build from source

On Linux, install JDK 17, Python 3.11 or newer, Git, curl, the Android SDK
command-line tools, OpenSSL for first-time key creation and the dependencies in the
[Firestack build instructions](native/README.md). Set `JAVA_HOME` to JDK 17 and
`ANDROID_SDK_ROOT` to your SDK. Install the platform and build tools from
[upstream.lock.json](upstream.lock.json). The platform package's revision must
match the lock.

```sh
sdkmanager "platforms;android-37.0" "build-tools;36.0.0" "platform-tools"
```

Use the same private release key for every update. The default key and password
file are `release.p12` and `store-password` in
`${XDG_CONFIG_HOME:-$HOME/.config}/stopscroll/android-signing/`. Both files must
have mode 0600 and live outside the checkout. For an existing installation,
restore its original signing files from your private backup. The build stops
if either file is missing.

For a first installation, deliberately create a key once. Run this block only
in a fresh signing directory. It stops if a key or password already exists.

```sh
(
    set -euC
    umask 077
    signing_dir="${XDG_CONFIG_HOME:-$HOME/.config}/stopscroll/android-signing"
    mkdir -p -- "$signing_dir"
    cd -- "$signing_dir"
    if [ -e release.p12 ] || [ -L release.p12 ] ||
        [ -e store-password ] || [ -L store-password ]; then
        printf '%s\n' 'Signing files already exist.' >&2
        exit 1
    fi
    openssl rand -hex 32 > store-password
    keytool -genkeypair -storetype PKCS12 -keystore release.p12 \
        -storepass:file store-password -alias stopscroll -keyalg RSA -keysize 3072 \
        -validity 10000 -dname 'CN=Stopscroll Release'
    chmod 600 -- release.p12 store-password
)
```

From the repository root, run `./android/build.sh` as your normal user. It
reconstructs the pinned Rethink source with the Stopscroll patch and verifies
the patched Firestack build. It publishes `android/out/stopscroll.apk` and a
mode-0600 `android-release.json` in your Stopscroll configuration directory
after checking the APK's signature, identity, manifest, native libraries and
embedded license files. The installer and `stopscroll android doctor` use this
receipt. Keep your key and receipt private. The APK and intermediate AAR are
local build outputs. See [Android source and license notices](LEGAL/README.md).

## Install or update over USB

Enable USB debugging, plug in the phone and authorize your desktop on the
phone. Use Android's primary user (user 0). Make `adb` from the SDK's
`platform-tools` available on your PATH. Run the installer as your normal user
with the same configuration directory used for the build.

```sh
./android/install.sh
# With multiple USB phones, select the intended phone by serial.
./android/install.sh --usb-serial SERIAL
```

The installer verifies the local APK against the receipt, binds the selected
USB device and checks any installed copy before requesting a Package Manager
update. A successful update keeps the phone's configuration and daily usage.
Confirm any Android or HyperOS installation prompt yourself. If the connection
is lost during installation, reconnect and run `stopscroll android doctor`
before retrying.

## Set up the VPN

If setup interrupts connectivity, open Android **Settings → VPN → Stopscroll**.
Turn off **Block connections without VPN** and **Always-on VPN**, then
disconnect Stopscroll. The phone is unprotected while disconnected. After
diagnosing the issue, repeat setup before re-enabling lockdown.

1. In **Settings → Apps → Stopscroll → Battery**, allow unrestricted background
   operation. On HyperOS, also enable **Background autostart** for Stopscroll
   and choose **No restrictions** in its battery settings. Android calls the
   required per-app exemption `isIgnoringBatteryOptimizations`. Apply these
   settings to Stopscroll only. The exemption lets the service use its bounded
   wake lock to close a session during Doze.
2. Android supports one VPN per user. Stop any other VPN in Settings, then open
   Stopscroll and approve Android's VPN prompt. In Android's VPN settings,
   check that Stopscroll is connected.
3. In Android **Settings → VPN → Stopscroll**, turn on **Always-on VPN** and
   **Block connections without VPN**. The second setting enables Android's
   lockdown mode. Stopscroll requires these settings and the battery
   exemption before it allows traffic. After a reboot, network access can
   stay blocked until your first unlock, when the VPN can open its journal.
4. If the browser uses its own Secure DNS resolver, switch it to the system
   resolver so Stopscroll can observe the domain lookups. Domain filtering
   applies to fresh network traffic. Offline content already stored by the
   browser can still be visible. Unknown encrypted resolvers can prevent domain
   identification.
5. Run `stopscroll android doctor` over USB to check the release, VPN, policy
   and protection settings. With those settings enabled, verify in the browser
   that a normal website loads and a fresh request to a domain in a blocked
   group fails. If the phone has no blocked sites, choose an unused group ID,
   such as `setupcheck`, and run
   `stopscroll android block setupcheck example.org` to make a temporary
   test rule. Visit `example.org`, then remove the test domain with
   `stopscroll android unblock setupcheck example.org`. On Android 13, allow notifications
   if you want to see VPN and session updates. The VPN service enforces
   deadlines if you decline.

## Manual commands

The desktop `stopscroll` command is part of the
[desktop installation](../README.md). Plug in the phone when you want to run
an Android command. CLI changes are immediate once applied. Import reads site
configuration from the authenticated desktop service and previews the full
replacement. The phone retains its own timezone and spent budget. Approved
policy changes close active phone sessions and return unused reservation time.

```sh
stopscroll android status
stopscroll android list
stopscroll android import-desktop --dry-run
stopscroll android import-desktop
stopscroll android limit video 30
stopscroll android open video 10
stopscroll android close video
stopscroll android doctor
```

Keep actual policy exports and device reports outside Git. For example,
`stopscroll android export --output "$HOME/phone-policy.json"` creates a
private file. The Android CLI uses a single manually selected USB connection.
Android also trusts any wireless ADB hosts you authorize, so keep wireless
debugging disabled for a USB-only setup. The schema and operation descriptions
are in the [USB protocol](PROTOCOL.md).

`doctor` verifies the installed APK against the release receipt and checks the
saved/applied policy, clock, VPN and protection settings. Device acceptance also
needs separate-app network tests. For a Doze deadline test, compare the
elapsed-time trigger and native cutoff with the deadline while leaving the
phone asleep through the deadline. A status request after the phone wakes
can't establish when the existing flows were cut.

## Android host tests

Run the unsigned unit tests on Linux with JDK 17 and the SDK platform and build tools recorded in [upstream.lock.json](upstream.lock.json). Set `ANDROID_HOME` to the installed SDK. The tests use the patched Firestack AAR and its upstream license. Verify both SHA-256 digests against the lock. The [native build instructions](native/README.md) describe how to produce the AAR.

For a fresh checkout, run these commands once from the Stopscroll repository root. The resulting index tree should match `rethink_patched_tree` in the lock. An existing checkout prepared with the same patch is ready for testing.

```sh
git clone --depth 1 --branch v0.5.8 https://github.com/celzero/rethink-app.git android/rethink-app
git -C android/rethink-app rev-parse HEAD
git -C android/rethink-app apply --index ../patches/0001-stopscroll-g0.patch
git -C android/rethink-app write-tree
```

From the repository root, set the paths to the verified inputs. This invocation uses one test worker at a time and starts a fresh worker after twelve test classes to bound Robolectric memory usage.

```sh
export STOPSCROLL_FIRESTACK_AAR=/path/to/firestack-stopscroll.aar
export STOPSCROLL_FIRESTACK_LICENSE=/path/to/firestack/LICENSE
export STOPSCROLL_FIRESTACK_PATCH="$PWD/android/native/firestack-cutoff.patch"

test_init=$(mktemp --suffix=.gradle)
trap 'rm -f -- "$test_init"' EXIT
cat > "$test_init" <<'GRADLE'
allprojects {
    tasks.withType(Test).configureEach {
        maxParallelForks = 1
        forkEvery = 12
    }
}
GRADLE

cd android/rethink-app
./gradlew :app:testFdroidFullStopscrollReleaseUnitTest --no-configuration-cache \
    --console=plain --max-workers=3 --init-script "$test_init"
```

For a focused run, append `--tests 'com.celzero.bravedns.stopscroll.*'` to the Gradle command. Results are under `app/build/test-results/testFdroidFullStopscrollReleaseUnitTest/`.
