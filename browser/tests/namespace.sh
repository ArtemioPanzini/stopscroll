#!/bin/bash
set -euo pipefail
# This is deliberately the first mutation: no mounts may propagate to the host.
mount --make-rprivate /
fixture=$1
uid=$2
node_binary=$3
playwright_module=$4
owner=$(getent passwd "$uid" | cut -d: -f1)
[[ $fixture == /tmp/stopscroll-browser-test-* && $uid =~ ^[0-9]+$ && $uid != 0 ]]
mount -t overlay overlay -o "lowerdir=/etc,upperdir=$fixture/etc-upper,workdir=$fixture/etc-work" /etc
mount --bind "$fixture/install" /usr/local/lib/stopscroll
mount --bind "$fixture/state" /var/lib/stopscroll
mount --bind "$fixture/policy" /run/stopscroll
mkdir -p /etc/opt/chrome/policies/managed
cp "$fixture/legacy-policy.json" /etc/opt/chrome/policies/managed/existing-fixture.json
# Hide host browser policies, including any real forced-extension update URL.
find /etc/opt/chrome/policies/managed -maxdepth 1 -type f ! -name existing-fixture.json -delete
PYTHONPATH=/usr/local/lib/stopscroll /usr/bin/python3 /usr/local/lib/stopscroll/browser/install.py
chown -R root:root "$fixture/install" "$fixture/state"
chown "$uid:$uid" "$fixture/runtime"
chmod 0700 "$fixture/runtime"
exec runuser -u "$owner" -- env PLAYWRIGHT_MODULE="$playwright_module" HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/config" XDG_CACHE_HOME="$fixture/home/cache" XDG_RUNTIME_DIR="$fixture/runtime" STOPSCROLL_BROWSER_TEST_ROOT="$fixture" dbus-run-session -- xvfb-run -a "$node_binary" /usr/local/lib/stopscroll/browser/tests/acceptance.cjs
