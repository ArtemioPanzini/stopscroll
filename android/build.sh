#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077

android_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(dirname -- "$android_dir")
upstream="$android_dir/rethink-app"
lock="$android_dir/upstream.lock.json"
patch="$android_dir/patches/0001-stopscroll-g0.patch"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/stopscroll-android/g0"
key_dir="${XDG_CONFIG_HOME:-$HOME/.config}/stopscroll/android-signing"

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

lock_value() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$lock" "$1"
}

verify_sha256() {
  printf '%s  %s\n' "$1" "$2" | sha256sum --check --status
}

fetch_verified() {
  local url=$1 hash=$2 destination=$3
  if [[ ! -f $destination ]] || ! verify_sha256 "$hash" "$destination"; then
    curl --fail --silent --show-error --location --retry 2 "$url" --output "$scratch/download"
    verify_sha256 "$hash" "$scratch/download" || fail "SHA-256 mismatch for $url"
    mv -- "$scratch/download" "$destination"
  fi
}

sdk=${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}
[[ -n $sdk && -d $sdk ]] || fail 'Set ANDROID_SDK_ROOT to the Android SDK directory.'
sdk=$(realpath "$sdk")
export ANDROID_HOME="$sdk" ANDROID_SDK_ROOT="$sdk"
platform="$sdk/platforms/android-$(lock_value compile_sdk).0"
[[ -f $platform/package.xml && -f $platform/android.jar ]] || fail 'Install the locked SDK platform using sdkmanager.'
actual_revision=$(sed -n 's/^Pkg.Revision=//p' "$platform/source.properties")
[[ $actual_revision == "$(lock_value compile_sdk_revision)" ]] || fail 'Android SDK platform revision differs from the lock.'
java_bin=${JAVA_HOME:+$JAVA_HOME/bin/}java
java_major=$("$java_bin" -XshowSettings:properties -version 2>&1 | sed -n 's/^ *java.specification.version = //p')
[[ $java_major == "$(lock_value jdk_major)" ]] || fail 'Use the JDK major recorded in upstream.lock.json.'

export STOPSCROLL_KEYSTORE=${STOPSCROLL_KEYSTORE:-"$key_dir/release.p12"}
password_file=${STOPSCROLL_PASSWORD_FILE:-"$key_dir/store-password"}
for signing_file in "$STOPSCROLL_KEYSTORE" "$password_file"; do
  [[ -f $signing_file ]] || fail "Missing signing file: $signing_file"
  signing_path=$(realpath "$signing_file")
  [[ $signing_path != "$repo_dir"/* ]] || fail 'Keep signing files outside the Stopscroll checkout.'
  [[ $(stat -c '%a' "$signing_path") == 600 ]] || fail 'Signing files must be mode 0600.'
done
export STOPSCROLL_KEYSTORE=$(realpath "$STOPSCROLL_KEYSTORE")
export STOPSCROLL_KEY_ALIAS=${STOPSCROLL_KEY_ALIAS:-stopscroll}
store_password="$(< "$password_file")"
[[ -n $store_password ]] || fail 'Signing password file is empty.'

release_version=$(lock_value release_version_code) || fail 'The release version is missing from upstream.lock.json.'
[[ $release_version =~ ^[1-9][0-9]{0,9}$ ]] || fail 'Invalid release version in upstream.lock.json.'
apksigner="$sdk/build-tools/$(lock_value build_tools)/apksigner"
apkanalyzer=$(find "$sdk/cmdline-tools" -path '*/bin/apkanalyzer' -type f -executable | sort -V | tail -n 1)
[[ -x $apksigner && -n $apkanalyzer ]] || fail 'Install the locked SDK build tools and command-line tools.'
keytool=${JAVA_HOME:+$JAVA_HOME/bin/}keytool
expected_cert=$("$keytool" -exportcert -storetype PKCS12 -keystore "$STOPSCROLL_KEYSTORE" \
  -storepass:file "$password_file" -alias "$STOPSCROLL_KEY_ALIAS" | sha256sum | cut -d ' ' -f1)

rollback="$android_dir/out/stopscroll-g0.apk"
if [[ -e $rollback ]]; then
  rollback_signers=$("$apksigner" verify --print-certs "$rollback")
  rollback_cert=$(sed -nE 's/^Signer #[0-9]+ certificate SHA-256 digest: //p' <<< "$rollback_signers")
  [[ $rollback_cert == "$expected_cert" ]] || fail 'The signing certificate differs from the saved G0 APK.'
  rollback_version=$("$apkanalyzer" manifest version-code "$rollback")
  [[ $rollback_version =~ ^[1-9][0-9]{0,9}$ && $release_version -gt $rollback_version ]] \
    || fail 'The release must be newer than the saved G0 APK.'
fi

mkdir -p -- "$cache"
scratch=$(mktemp -d "$cache/build.XXXXXXXX")
staging=''
trap 'rm -rf -- "$scratch" ${staging:+"$staging"}' EXIT
verify_sha256 "$(lock_value rethink_patch_sha256)" "$patch" || fail 'Rethink patch differs from the lock.'
commit=$(lock_value upstream_commit)
if [[ ! -e $upstream ]]; then
  git init --quiet "$upstream"
  git -C "$upstream" remote add origin "$(lock_value upstream_repository)"
  tag=$(lock_value upstream_tag)
  git -C "$upstream" fetch --depth=1 origin "refs/tags/$tag:refs/tags/$tag"
  git -C "$upstream" switch --detach "$commit"
fi
[[ -d $upstream/.git ]] || fail "Expected a dedicated upstream Git checkout at $upstream"
[[ $(git -C "$upstream" rev-parse HEAD) == "$commit" ]] || fail 'Upstream checkout is at the wrong commit.'

# Compute the expected tree in a separate index. A repeat build accepts exactly that patch.
GIT_INDEX_FILE="$scratch/index" git -C "$upstream" read-tree HEAD
GIT_INDEX_FILE="$scratch/index" git -C "$upstream" apply --cached --whitespace=error-all "$patch"
expected_tree=$(GIT_INDEX_FILE="$scratch/index" git -C "$upstream" write-tree)
[[ $expected_tree == "$(lock_value rethink_patched_tree)" ]] || fail 'Patched Rethink source tree differs from the lock.'
if [[ -z $(git -C "$upstream" status --porcelain) ]]; then
  git -C "$upstream" apply --index "$patch"
fi
[[ $(git -C "$upstream" write-tree) == "$expected_tree" ]] || fail 'The upstream index differs from the locked patch.'
git -C "$upstream" diff --quiet || fail 'Unexpected unstaged changes in the upstream checkout.'
[[ -z $(git -C "$upstream" ls-files --others --exclude-standard) ]] || fail 'Unexpected untracked sources in the upstream checkout.'
wrapper_hash=$(sed -n 's/^distributionSha256Sum=//p' "$upstream/gradle/wrapper/gradle-wrapper.properties")
[[ $wrapper_hash == "$(lock_value gradle_distribution_sha256)" ]] || fail 'Gradle wrapper checksum differs from the lock.'

published="$cache/firestack-d3d530ea3e.aar"
fetch_verified "$(lock_value firestack_aar_url)" "$(lock_value firestack_published_aar_sha256)" "$published"
export STOPSCROLL_FIRESTACK_PATCH="$android_dir/native/firestack-cutoff.patch"
verify_sha256 "$(lock_value firestack_patch_sha256)" "$STOPSCROLL_FIRESTACK_PATCH" || fail 'Firestack patch differs from the lock.'
native_hash=$(lock_value firestack_aar_sha256)
export STOPSCROLL_FIRESTACK_AAR="$cache/firestack-stopscroll-$native_hash.aar"
if [[ ! -f $STOPSCROLL_FIRESTACK_AAR ]] || ! verify_sha256 "$native_hash" "$STOPSCROLL_FIRESTACK_AAR"; then
  "$android_dir/native/build-firestack.sh" "$published" "$scratch/firestack.aar"
  verify_sha256 "$native_hash" "$scratch/firestack.aar" || fail 'Rebuilt Firestack AAR differs from the lock.'
  mv -- "$scratch/firestack.aar" "$STOPSCROLL_FIRESTACK_AAR"
fi
export STOPSCROLL_FIRESTACK_LICENSE="$cache/firestack-license.txt"
fetch_verified "https://raw.githubusercontent.com/celzero/firestack/$(lock_value firestack_commit)/LICENSE" \
  "$(lock_value firestack_license_sha256)" "$STOPSCROLL_FIRESTACK_LICENSE"
cmp -s "$STOPSCROLL_FIRESTACK_LICENSE" "$android_dir/LEGAL/Firestack-MPL-2.0.txt" \
  || fail 'Recorded Firestack license differs from the locked upstream license.'
cmp -s "$upstream/LICENSE" "$android_dir/LEGAL/RethinkDNS-Apache-2.0.txt" \
  || fail 'Recorded Rethink license differs from the pinned source.'

variant=$(lock_value build_variant)
task=":app:assemble${variant^}"
(
  cd -- "$upstream"
  export STOPSCROLL_STORE_PASSWORD="$store_password"
  export STOPSCROLL_KEY_PASSWORD="$store_password"
  ./gradlew "$task" --no-configuration-cache --console=plain --max-workers=4
)
unset store_password

gradle_apk="$upstream/app/build/outputs/apk/fdroidFull/stopscrollRelease/app-fdroid-full-stopscrollRelease.apk"
[[ -f $gradle_apk ]] || fail 'Gradle did not produce the expected APK.'
install -d -m 700 "$android_dir/out"
staging=$(mktemp -d "$android_dir/out/.build.XXXXXXXX")
apk="$staging/stopscroll.apk"
install -m 600 -- "$gradle_apk" "$apk"
signer_report=$("$apksigner" verify --print-certs "$apk")
actual_cert=$(sed -nE 's/^Signer #[0-9]+ certificate SHA-256 digest: //p' <<< "$signer_report")
[[ $actual_cert == "$expected_cert" ]] || fail 'APK signer differs from the configured release alias.'
printf '%s\n' "$signer_report"
[[ $("$apkanalyzer" manifest application-id "$apk") == com.celzero.bravedns.stopscroll ]] || fail 'Unexpected APK package.'
[[ $("$apkanalyzer" manifest debuggable "$apk") == false ]] || fail 'The release APK is debuggable.'
[[ $("$apkanalyzer" manifest target-sdk "$apk") == "$(lock_value target_sdk)" ]] || fail 'Unexpected target SDK.'
actual_version=$("$apkanalyzer" manifest version-code "$apk")
[[ $actual_version == "$release_version" ]] || fail 'APK versionCode differs from the locked release version.'
"$apkanalyzer" manifest print "$apk" > "$scratch/manifest.xml"
python3 -I - "$scratch/manifest.xml" <<'PY'
import sys
from xml.etree import ElementTree as ET

android = '{http://schemas.android.com/apk/res/android}'
manifest = ET.parse(sys.argv[1]).getroot()
app = manifest.find('application')
assert app.get(android + 'label') == 'Stopscroll'
assert app.get(android + 'allowBackup') == 'false'
assert app.get(android + 'testOnly', 'false') == 'false'
authority = 'com.celzero.bravedns.stopscroll.usb'
providers = [p for p in app.findall('provider') if p.get(android + 'authorities') == authority]
assert len(providers) == 1
assert providers[0].get(android + 'exported') == 'true'
assert providers[0].get(android + 'grantUriPermissions') == 'false'
assert providers[0].get(android + 'name') == 'com.celzero.bravedns.stopscroll.UsbControlProvider'
assert not providers[0].findall('grant-uri-permission')
assert all('VpnControlReceiver' not in r.get(android + 'name', '') for r in app.findall('receiver'))
vpn = [s for s in app.findall('service')
       if any(a.get(android + 'name') == 'android.net.VpnService' for a in s.findall('./intent-filter/action'))]
assert len(vpn) == 1
assert vpn[0].get(android + 'permission') == 'android.permission.BIND_VPN_SERVICE'
assert not any('com.google.firebase' in node.get(android + 'name', '') for node in app.iter())
PY
python3 -I - "$apk" "$STOPSCROLL_FIRESTACK_AAR" "$STOPSCROLL_FIRESTACK_PATCH" \
  "$STOPSCROLL_FIRESTACK_LICENSE" "$upstream/LICENSE" <<'PYZIP'
import hashlib
import sys
from pathlib import Path
from zipfile import ZipFile

with ZipFile(sys.argv[1]) as apk_zip, ZipFile(sys.argv[2]) as aar:
    names = apk_zip.namelist()
    assert len(names) == len(set(names)), 'Duplicate APK ZIP entries'
    natives = sorted(name for name in names if name.endswith('/libgojni.so'))
    assert natives == [
        'lib/arm64-v8a/libgojni.so', 'lib/armeabi-v7a/libgojni.so', 'lib/x86_64/libgojni.so',
    ], 'Unexpected native libraries in the release APK'
    for name in natives:
        expected = aar.read('jni/' + name.removeprefix('lib/'))
        actual = apk_zip.read(name)
        assert actual == expected, f'{name} differs from the locked native build'
        print(f'{hashlib.sha256(actual).hexdigest()}  {name}')
    assert apk_zip.read('assets/Firestack-modifications.patch') == Path(sys.argv[3]).read_bytes()
    assert apk_zip.read('assets/Firestack-MPL-2.0.txt') == Path(sys.argv[4]).read_bytes()
    assert apk_zip.read('assets/RethinkDNS-Apache-2.0.txt') == Path(sys.argv[5]).read_bytes()
PYZIP
python3 -I -S - "$repo_dir" "$apk" "$actual_cert" "$actual_version" <<'PUBLISH'
import sys
from pathlib import Path

repo, staged, certificate, version = sys.argv[1:]
sys.path.insert(0, repo)
from android.release import _publish_staged_release
from stopscroll.android_cli import _receipt_path

output = Path(repo) / 'android/out/stopscroll.apk'
try:
    receipt = _publish_staged_release(
        Path(staged), output, _receipt_path(), certificate=certificate, version=int(version),
    )
except OSError as exc:
    sys.exit(str(exc))
print(f"Verified APK {output}\n{receipt['apk_sha256']}  {output}")
PUBLISH
