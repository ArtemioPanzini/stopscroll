# Android source and license notices

Stopscroll builds on Celzero's Rethink DNS + Firewall and Firestack. The
retained upstream copyright and license notices apply to their code. This
repository supplies the Stopscroll changes and the build instructions needed
to reconstruct the patched sources.

| Component | Source and changes | License |
| --- | --- | --- |
| Rethink DNS + Firewall by Celzero and contributors | [Rethink v0.5.8 source](https://github.com/celzero/rethink-app/tree/b750334a363065b94f83e818ac5dc38048713c1f), [Stopscroll patch](../patches/0001-stopscroll-g0.patch) | [Apache License 2.0](RethinkDNS-Apache-2.0.txt) for the upstream source |
| Firestack by Celzero and contributors | [Firestack source](https://github.com/celzero/firestack/tree/d3d530ea3e67a3ca9dd2e0ad472d20908ea0b596), [Firestack modifications and their tests](../native/firestack-cutoff.patch), [rebuild instructions](../native/README.md) | [MPL 2.0](Firestack-MPL-2.0.txt), including the Firestack modifications |
| Stopscroll | [Project source and build instructions](../../README.md) | [GPL 3.0](../../LICENSE), with component-specific notices where indicated |
| Public suffix data used in policy validation | [Guava/PSL provenance and extraction](../../stopscroll/data/README.md) | [Apache License 2.0](../../stopscroll/data/LICENSE-APACHE-2.0.txt) and [MPL 2.0](../../stopscroll/data/LICENSE-MPL-2.0.txt) for the respective sources |

The Android build pins the upstream commits and the patch, tree and AAR
digests in [upstream.lock.json](../upstream.lock.json). Its APK includes
`assets/RethinkDNS-Apache-2.0.txt`, `assets/Firestack-MPL-2.0.txt` and
`assets/Firestack-modifications.patch`. The patched Firestack source is
available by applying that patch to the pinned Firestack source. The patch
changes flow retirement, DNS socket tracking and the TCP retrier used by the
VPN to stop traffic at a session cutoff.

Rethink's pinned Gradle catalog and Firestack's pinned `go.mod` and `go.sum`
identify the library dependencies. To inspect the Android runtime dependency
tree, run the following command from the patched Rethink checkout with the
same verified Firestack paths used by the [host test recipe](../README.md#android-host-tests).
Review each resolved dependency's original license and notices, including the
license files retained in the built APK's `META-INF` directory.

```sh
./gradlew :app:dependencies --configuration fdroidFullStopscrollReleaseRuntimeClasspath \
    --no-configuration-cache --console=plain
```
