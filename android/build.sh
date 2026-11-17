#!/usr/bin/env bash
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
export STOPSCROLL_STORE_PASSWORD="$(< "$password_file")"
export STOPSCROLL_KEY_PASSWORD="$STOPSCROLL_STORE_PASSWORD"
[[ -n $STOPSCROLL_STORE_PASSWORD ]] || fail 'Signing password file is empty.'

mkdir -p -- "$cache"
scratch=$(mktemp -d "$cache/build.XXXXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
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
GIT_INDEX_FILE="$scratch/index" git -C "$upstream" apply --cached "$patch"
expected_tree=$(GIT_INDEX_FILE="$scratch/index" git -C "$upstream" write-tree)
if [[ -z $(git -C "$upstream" status --porcelain) ]]; then
  git -C "$upstream" apply --index "$patch"
fi
[[ $(git -C "$upstream" write-tree) == "$expected_tree" ]] || fail 'The upstream index differs from the locked patch.'
git -C "$upstream" diff --quiet || fail 'Unexpected unstaged changes in the upstream checkout.'
[[ -z $(git -C "$upstream" ls-files --others --exclude-standard) ]] || fail 'Unexpected untracked sources in the upstream checkout.'
wrapper_hash=$(sed -n 's/^distributionSha256Sum=//p' "$upstream/gradle/wrapper/gradle-wrapper.properties")
[[ $wrapper_hash == "$(lock_value gradle_distribution_sha256)" ]] || fail 'Gradle wrapper checksum differs from the lock.'

export STOPSCROLL_FIRESTACK_AAR="$cache/firestack-d3d530ea3e.aar"
fetch_verified "$(lock_value firestack_aar_url)" "$(lock_value firestack_aar_sha256)" "$STOPSCROLL_FIRESTACK_AAR"
export STOPSCROLL_FIRESTACK_LICENSE="$cache/firestack-license.txt"
fetch_verified "https://raw.githubusercontent.com/celzero/firestack/$(lock_value firestack_commit)/LICENSE" \
  "$(lock_value firestack_license_sha256)" "$STOPSCROLL_FIRESTACK_LICENSE"

variant=$(lock_value build_variant)
task=":app:assemble${variant^}"
(
  cd -- "$upstream"
  ./gradlew "$task" --no-configuration-cache --console=plain --max-workers=4
)

apk="$upstream/app/build/outputs/apk/fdroidFull/stopscrollRelease/app-fdroid-full-stopscrollRelease.apk"
apksigner="$sdk/build-tools/$(lock_value build_tools)/apksigner"
apkanalyzer=$(find "$sdk/cmdline-tools" -path '*/bin/apkanalyzer' -type f -executable | sort -V | tail -n 1)
[[ -f $apk && -x $apksigner && -n $apkanalyzer ]] || fail 'APK or SDK verification tools missing.'
signer_report=$("$apksigner" verify --print-certs "$apk")
actual_cert=$(sed -nE 's/^Signer #[0-9]+ certificate SHA-256 digest: //p' <<< "$signer_report")
keytool=${JAVA_HOME:+$JAVA_HOME/bin/}keytool
expected_cert=$("$keytool" -exportcert -storetype PKCS12 -keystore "$STOPSCROLL_KEYSTORE" \
  -storepass:file "$password_file" -alias "$STOPSCROLL_KEY_ALIAS" | sha256sum | cut -d ' ' -f1)
[[ $actual_cert == "$expected_cert" ]] || fail 'APK signer differs from the configured release alias.'
printf '%s\n' "$signer_report"
[[ $("$apkanalyzer" manifest application-id "$apk") == com.celzero.bravedns.stopscroll ]]
[[ $("$apkanalyzer" manifest debuggable "$apk") == false ]]
[[ $("$apkanalyzer" manifest target-sdk "$apk") == "$(lock_value target_sdk)" ]]
"$apkanalyzer" manifest print "$apk" > "$scratch/manifest.xml"
python3 - "$scratch/manifest.xml" <<'PY'
import sys
from xml.etree import ElementTree as ET

android = '{http://schemas.android.com/apk/res/android}'
app = ET.parse(sys.argv[1]).getroot().find('application')
authority = 'com.celzero.bravedns.stopscroll.usb'
providers = [p for p in app.findall('provider') if p.get(android + 'authorities') == authority]
assert len(providers) == 1
assert providers[0].get(android + 'exported') == 'true'
assert providers[0].get(android + 'grantUriPermissions') == 'false'
assert providers[0].get(android + 'name') == 'com.celzero.bravedns.stopscroll.UsbControlProvider'
assert all('VpnControlReceiver' not in r.get(android + 'name', '') for r in app.findall('receiver'))
PY
unzip -p "$apk" lib/arm64-v8a/libgojni.so > "$scratch/libgojni.so"
[[ -s $scratch/libgojni.so ]] || fail 'APK has no arm64-v8a Firestack library.'
install -d -m 700 "$android_dir/out"
output="$android_dir/out/stopscroll-g0.apk"
if ! cmp -s "$apk" "$output"; then
  install -m 600 "$apk" "$output"
fi
printf 'Verified APK %s\n' "$output"
sha256sum "$output"
