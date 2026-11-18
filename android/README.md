# Android host tests

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
