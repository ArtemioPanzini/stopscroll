#!/usr/bin/env bash
# Build the MPL-2.0 Firestack patch for Linux x86_64. See README.md for pins.
set -euo pipefail
umask 077
[[ $# == 2 ]] || { echo "Usage: $0 PUBLISHED.aar OUTPUT.aar" >&2; exit 2; }
native=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
original=$(realpath "$1")
output=$(realpath -m "$2")
[[ "$output" != "$original" && -d $(dirname "$output") ]]
cache=${FIRESTACK_NATIVE_CACHE:-"${XDG_CACHE_HOME:-$HOME/.cache}/stopscroll-android/native"}
mkdir -p "$cache"
cache=$(realpath "$cache")
exec 9>"$cache/build.lock"
flock -x 9

commit=d3d530ea3e67a3ca9dd2e0ad472d20908ea0b596
tree=f97b7102f7468ab73a365c8edc031ce509226aad
patch=f8fa406a5aed7108d5be621eedd3394d0ed7cdd949d90d220a1e9c2288f7a15c
mobile=v0.0.0-20260908204917-8b95e45f8d3e
overlay=v0.0.0-20250623154806-ec286d1fd9dc

check() { printf '%s  %s\n' "$1" "$2" | sha256sum -c --status; }
fetch() {
    local url=$1 digest=$2 file=$3
    if ! [[ -f "$file" ]] || ! check "$digest" "$file"; then
        curl -fLsS --retry 3 "$url" -o "$file.part"
        check "$digest" "$file.part"
        mv -f "$file.part" "$file"
    fi
}
check 7fa2cc5493005bdd9e31730b19baf7af2acac1e8e833f205a00a483b3df56d64 "$original"
check "$patch" "$native/firestack-cutoff.patch"
[[ "$(uname -sm)" == "Linux x86_64" ]]
if [[ -n ${JAVA_HOME:-} ]]; then export PATH="$JAVA_HOME/bin:$PATH"; fi
[[ "$(javac -version 2>&1)" == "javac 17."* ]]
mkdir -p "$cache"/{toolchain,downloads,sdk/ndk,sdk/platforms,bin,tmp,gopath,gocache}
fetch https://dl.google.com/go/go1.27.1.linux-amd64.tar.gz \
    63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445 "$cache/downloads/go.tar.gz"
fetch https://dl.google.com/android/repository/android-ndk-r29-linux.zip \
    4abbbcdc842f3d4879206e9695d52709603e52dd68d3c1fff04b3b5e7a308ecf "$cache/downloads/ndk.zip"
fetch https://dl.google.com/android/repository/platform-37.2_r02.zip \
    17af98ec1427a1fed9ea278184aba71e53b87eca103655602f9fc64ef11ea4d3 "$cache/downloads/platform.zip"
[[ -x "$cache/toolchain/go/bin/go" ]] || tar -xzf "$cache/downloads/go.tar.gz" -C "$cache/toolchain"
export ANDROID_HOME="$cache/sdk" ANDROID_SDK_ROOT="$cache/sdk"
export ANDROID_NDK_HOME="$cache/sdk/ndk/android-ndk-r29"
[[ -f "$ANDROID_NDK_HOME/source.properties" ]] || unzip -qo "$cache/downloads/ndk.zip" -d "$cache/sdk/ndk"
platform="$ANDROID_HOME/platforms/android-37.2/android.jar"
[[ -f "$platform" ]] || unzip -qo "$cache/downloads/platform.zip" -d "$cache/sdk/platforms"
check 2507cf762abe2171a8cb3c289837fc71f2e66e598f396f1cc10bb7dc10020329 "$platform"
grep -Eq '^Pkg.Revision *= *29\.0\.14206865$' "$ANDROID_NDK_HOME/source.properties"
export PATH="$cache/toolchain/go/bin:$cache/bin:$PATH"
export GOPATH="$cache/gopath" GOCACHE="$cache/gocache" GOBIN="$cache/bin" TMPDIR="$cache/tmp"
jobs=${FIRESTACK_JOBS:-2}
[[ "$jobs" == 1 || "$jobs" == 2 ]]
unset GOROOT GOARCH GOOS GOARM GOARM64 GOAMD64 GO386 GOEXPERIMENT CGO_CFLAGS CGO_CPPFLAGS CGO_CXXFLAGS CGO_LDFLAGS
export GOENV=off GOWORK=off GOTOOLCHAIN=local GOMAXPROCS="$jobs" GOFLAGS="-p=$jobs"
[[ "$(go version)" == 'go version go1.27.1 linux/amd64' ]]
source_repo=${FIRESTACK_SOURCE:-"$cache/upstream.git"}
if [[ ! -d "$source_repo" ]]; then
    [[ -z ${FIRESTACK_SOURCE:-} ]] || { echo "Missing FIRESTACK_SOURCE" >&2; exit 1; }
    git init --bare --quiet "$source_repo"
fi
if ! git -C "$source_repo" cat-file -e "$commit^{commit}" 2>/dev/null; then
    [[ -z ${FIRESTACK_SOURCE:-} ]] || { echo "Missing Firestack commit" >&2; exit 1; }
    git -C "$source_repo" fetch -q --depth=1 https://github.com/celzero/firestack.git "$commit"
fi
source_repo=$(realpath "$source_repo")
work=$(mktemp -d "$cache/tmp/bind.XXXXXXXX")
# gomobile embeds this absolute source-module replacement path in libgojni.so.
# Reserve the same empty, private directory on every builder.
src="/tmp/stopscroll-firestack-$tree"
mkdir -m 700 "$src" || { echo "Canonical source path already exists: $src" >&2; exit 1; }
trap 'rm -rf -- "$src" "$work"' EXIT
git -C "$source_repo" archive "$commit" | tar -x -C "$src"
cd "$src"
git init -q
git apply "$native/firestack-cutoff.patch"
git -c core.autocrlf=false add -f .
[[ "$(git write-tree)" == "$tree" ]]
go mod download
go mod verify
go test -ldflags=-checklinkname=0 -run '^TestCutoff' -count=20 ./intra ./intra/netstack
go install "golang.org/x/mobile/cmd/gomobile@$mobile"
go install "golang.org/x/mobile/cmd/gobind@$mobile"
go install "github.com/felixge/go-patch-overlay@$overlay"
mkdir -p "$GOPATH/pkg/gomobile"
go-patch-overlay -overlay "$work" tools/runtime_write_err_android.patch
# The Go build ID includes the CC command, so use a fixed compiler path, too.
ln -s "$ANDROID_NDK_HOME" "$src/.git/ndk"
export ANDROID_NDK_HOME="$src/.git/ndk"
export GODEBUG=cgocheck=0 CGO_LDFLAGS='-Wl,-z,max-page-size=16384'
libtools="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin"
package=github.com/celzero/firestack/intra
# A fixed source timestamp and a patched-build identifier replace CI's wall clock.
ldflags="-checklinkname=0 -X $package/core.Date=20261005213952 -X $package/core.Commit=d3d530e-stopscroll.${patch:0:8}"
for entry in arm64:arm64-v8a arm:armeabi-v7a 386:x86 amd64:x86_64; do
    arch=${entry%:*}
    abi=${entry#*:}
    dir="$work/$abi"
    mkdir "$dir"
    echo "Building $abi" >&2
    gomobile bind -trimpath -javapkg com.celzero.firestack -androidapi 23 \
        "-target=android/$arch" -tags=android,debuglog -bootclasspath "$platform" \
        "-overlay=$work/overlay.json" -ldflags "$ldflags" -o "$dir/bound.aar" \
        "$package" "$package/backend" "$package/settings"
    unzip -p "$dir/bound.aar" "jni/$abi/libgojni.so" > "$dir/libgojni.so"
    "$libtools/llvm-objcopy" --strip-debug --strip-unneeded "$dir/libgojni.so"
done
git diff --quiet
python3 "$native/package.py" "$original" "$work" "$work/patched.aar"
install -m 600 "$work/patched.aar" "$output.part"
mv -f "$output.part" "$output"
sha256sum "$output"
