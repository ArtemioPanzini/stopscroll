# Patched Firestack for Android G0

Build on Linux x86_64 with JDK 17, Python 3, Git, curl, unzip, GNU binutils and standard shell tools. Supply the published AAR locked in `android/upstream.lock.json` and an output path. The first build downloads Go and the Android native tools into the selected cache. The output contains all four upstream ABIs. Build one ABI at a time with up to two Go compilation jobs.

```sh
FIRESTACK_NATIVE_CACHE=/path/to/dedicated/cache \
  android/native/build-firestack.sh /path/to/firestack-d3d530ea3e.aar /path/to/firestack-stopscroll.aar
```

`FIRESTACK_SOURCE` may name an existing Git clone containing the exact upstream commit. The script exports that commit into a private source directory, applies the patch, checks the resulting Git tree and runs the Go regressions. A fixed source path is necessary because gomobile includes the local module replacement path in the ELF build information.

## Provenance

The published Maven Central AAR has SHA-256 `7fa2cc5493005bdd9e31730b19baf7af2acac1e8e833f205a00a483b3df56d64`. The publisher used the debug Make target with its Android runtime logging overlay, gomobile Java package, API 23, Android/debuglog build tags, 16 KiB linker alignment and NDK debug-symbol stripping. The local build uses those inputs and byte-compares each regenerated Java class, the remaining AAR metadata, the exported JNI symbol names and the Go module versions against the published AAR. [Upstream build](https://github.com/celzero/firestack/actions/runs/37378442923/job/111993750274) · [Upstream Makefile](https://github.com/celzero/firestack/blob/d3d530ea3e67a3ca9dd2e0ad472d20908ea0b596/Makefile) · [Maven Central artifact](https://repo.maven.apache.org/maven2/com/celzero/firestack/d3d530ea3e/firestack-d3d530ea3e.aar)

| Input | Local pin |
| --- | --- |
| Firestack | `d3d530ea3e67a3ca9dd2e0ad472d20908ea0b596` |
| Patched source tree | `f97b7102f7468ab73a365c8edc031ce509226aad` |
| Go | `1.27.1` |
| NDK | `29.0.14206865` |
| SDK Java bootclasspath | Android `37.2` revision `2` |
| gomobile and gobind CLI | `v0.0.0-20260908204917-8b95e45f8d3e` |
| go-patch-overlay | `v0.0.0-20250623154806-ec286d1fd9dc` |
| Mobile runtime library | `v0.0.0-20260508232728-bebd421c7fa8` from upstream go.mod |

Upstream installs the CLI tools at `@latest`. The local pins correspond to the latest upstream commits available at the original build time. The build preserves the runtime dependencies in upstream go.mod and go.sum. The local native timestamp is the source commit timestamp. The commit label includes the Stopscroll patch digest. The final AAR retains the verified upstream binding metadata and packages the rebuilt libraries with fixed entry timestamps and no ZIP compression.

## Source changes

`firestack-cutoff.patch` is distributed under MPL-2.0 and includes its regression tests. A tunnel records an admission generation before calling the policy listener for each decision. `CloseConns("")` advances that generation and sweeps the handlers under one lock. A previously approved flow that finishes dialing afterward closes its endpoints before it can forward payload. TCP, UDP and ICMP share the generation. Trapped TCP and UDP DNS sockets remain tracked for the duration of the resolver's serving loop. Their internal tracking keys are distinct from numeric UIDs, so cleanup of a retired socket cannot close fresh queries from another UID.

A full TCP retrier close retires the logical connection and closes the published socket without waiting for an in-progress retry dial or response. A pending dial that returns after retirement closes its socket before replaying the saved request. Standalone TCP half-closes retain their retry behavior. The tests exercise handler-level admission and trapped DNS, plus the production split retrier through loopback TCP and through the handler sweep. [Firestack license](https://github.com/celzero/firestack/blob/d3d530ea3e67a3ca9dd2e0ad472d20908ea0b596/LICENSE)
