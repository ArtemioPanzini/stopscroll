#!/usr/bin/env bash
set -euo pipefail
umask 077

probe_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
checkout=$(cd -- "$probe_dir/../../.." && pwd -P)
sdk_root=${SDK_ROOT:-${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}}
compile_sdk=${COMPILE_SDK:-android-37.0}
build_tools=${BUILD_TOOLS:-36.0.0}
javac=${JAVA_HOME:+$JAVA_HOME/bin/}javac
keytool=${JAVA_HOME:+$JAVA_HOME/bin/}keytool

if [[ -z $sdk_root ]]; then
    echo 'Set SDK_ROOT to the Android SDK directory.' >&2
    exit 1
fi

android_jar="$sdk_root/platforms/$compile_sdk/android.jar"
tools="$sdk_root/build-tools/$build_tools"
for sdk_file in "$android_jar" "$tools/"{aapt2,d8,zipalign,apksigner,core-lambda-stubs.jar}; do
    if [[ ! -f $sdk_file ]]; then
        echo "Missing Android SDK file: $sdk_file" >&2
        exit 1
    fi
done

out="$probe_dir/out"
mkdir -p -- "$out"
stage=$(mktemp -d -- "$out/build.XXXXXX")
key_stage=
cleanup() {
    rm -rf -- "$stage"
    if [[ -n $key_stage ]]; then
        rm -rf -- "$key_stage"
    fi
}
trap cleanup EXIT

mkdir -- "$stage/classes"
"$javac" -source 8 -target 8 -bootclasspath "$android_jar:$tools/core-lambda-stubs.jar" \
    -Xlint:all,-options -Werror \
    -d "$stage/classes" "$probe_dir"/src/org/stopscroll/probe/*.java
"$tools/d8" --lib "$android_jar" --release --min-api 33 --output "$stage" \
    "$stage"/classes/org/stopscroll/probe/*.class
"$tools/aapt2" link -I "$android_jar" --manifest "$probe_dir/AndroidManifest.xml" \
    -o "$stage/unaligned.apk"
(
    cd -- "$stage"
    zip -q -0 unaligned.apk classes.dex
)
"$tools/zipalign" -f 4 "$stage/unaligned.apk" "$stage/aligned.apk"

data_dir=$(realpath -m -- "${XDG_DATA_HOME:-$HOME/.local/share}/stopscroll-android/probe")
case "$data_dir/" in
    "$checkout/"*)
        echo 'The test signing key must live outside the checkout.' >&2
        exit 1
        ;;
esac
install -d -m 700 -- "$data_dir"
exec 9>"$data_dir/signing.lock"
flock 9
signing="$data_dir/signing"
if [[ ! -d $signing ]]; then
    key_stage=$(mktemp -d -- "$data_dir/.key.XXXXXX")
    openssl rand -hex 32 > "$key_stage/password"
    "$keytool" -genkeypair -noprompt -storetype PKCS12 -keystore "$key_stage/probe.p12" \
        -storepass:file "$key_stage/password" -alias probe -keyalg RSA -keysize 3072 \
        -validity 3650 -dname 'CN=Stopscroll G0 Test Probe' > /dev/null
    mv -- "$key_stage" "$signing"
    key_stage=
fi
flock -u 9

"$tools/apksigner" sign --ks "$signing/probe.p12" \
    --ks-key-alias probe --ks-pass "file:$signing/password" \
    --v4-signing-enabled false --out "$stage/probe.apk" "$stage/aligned.apk"
"$tools/apksigner" verify "$stage/probe.apk"
mv -- "$stage/probe.apk" "$out/probe.apk"
printf '%s\n' "$out/probe.apk"
