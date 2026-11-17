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
# A competing dictionary must fail before keys, packages or policies change.
printf '%s\n' '{"ExtensionSettings":{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":{"installation_mode":"blocked"}}}' > /etc/opt/chrome/policies/managed/conflict-fixture.json
if PYTHONPATH="$fixture/source" /usr/bin/python3 "$fixture/source/browser/install.py" --stage "$fixture/conflict-stage" >/dev/null 2>&1; then
    echo 'conflict preflight unexpectedly accepted' >&2; exit 1
fi
[[ ! -e /var/lib/stopscroll/browser-key.pem && ! -e /usr/local/lib/stopscroll/browser/package.json ]]
rm /etc/opt/chrome/policies/managed/conflict-fixture.json
for syntax in extensionless commented trailing; do
    case $syntax in
        extensionless) printf '%s' '{"ExtensionSettings":{}}' ;;
        commented) printf '%s\n' '// Chromium JSONC' '{"ExtensionSettings":{}}' ;;
        trailing) printf '%s' '{"ExtensionSettings":{},}' ;;
    esac > "/etc/opt/chrome/policies/managed/$syntax"
    if PYTHONPATH="$fixture/source" /usr/bin/python3 "$fixture/source/browser/install.py" --stage "$fixture/conflict-stage" >/dev/null 2>&1; then
        echo "preflight unexpectedly accepted $syntax" >&2; exit 1
    fi
    [[ ! -e /var/lib/stopscroll/browser-key.pem && ! -e /usr/local/lib/stopscroll/browser/package.json ]]
    rm "/etc/opt/chrome/policies/managed/$syntax"
done
printf '%s\n' 'allPolicyFilesAndJsoncPreflightPreserved=true'

# A broken key must fail the normal installer before any stop or code replacement,
# even when the old installed version has no stopscroll.browser module.
mkdir -p "$fixture/bin"
printf '#!/bin/sh\necho "$*" >> "%s/systemctl.log"\nexit 0\n' "$fixture" > "$fixture/bin/systemctl"
printf '#!/bin/sh\nexit 0\n' > "$fixture/bin/ss"
chmod 0755 "$fixture/bin/systemctl" "$fixture/bin/ss"
printf 'broken-key' > /var/lib/stopscroll/browser-key.pem
rm /usr/local/lib/stopscroll/stopscroll/browser.py
if env PATH="$fixture/bin:$PATH" SUDO_UID="$uid" SUDO_USER="$owner" bash "$fixture/source/install.sh" >"$fixture/failure.log" 2>&1; then
    echo 'broken key unexpectedly accepted' >&2; exit 1
fi
[[ ! -e "$fixture/systemctl.log" && ! -e /usr/local/lib/stopscroll/stopscroll/browser.py ]]
rm /var/lib/stopscroll/browser-key.pem
cp "$fixture/source/stopscroll/browser.py" /usr/local/lib/stopscroll/stopscroll/browser.py
printf '%s\n' 'conflictPreflightPreserved=true packagingFailurePreservedServices=true oldInstalledModuleNotRequired=true'
PYTHONPATH=/usr/local/lib/stopscroll /usr/bin/python3 /usr/local/lib/stopscroll/browser/install.py
# Shadow the system host in a disposable user config, as an unprivileged user can.
python3 - "$fixture" <<'PYHOST'
import json,os,sys
from pathlib import Path
fixture=Path(sys.argv[1]); ident=json.loads(Path('/usr/local/lib/stopscroll/browser/package.json').read_text())['id']
directories=[fixture/'home/config/google-chrome/NativeMessagingHosts',fixture/'profile/NativeMessagingHosts']
for directory in directories: directory.mkdir(parents=True)
host=Path('/usr/local/lib/stopscroll/browser/tests/fake_host.py');host.chmod(0o755)
for directory in directories: (directory/'org.stopscroll.youtube.json').write_text(json.dumps(dict(name='org.stopscroll.youtube',description='Adversarial fixture',path=str(host),type='stdio',allowed_origins=[f'chrome-extension://{ident}/'])))
PYHOST
chown -R "$uid:$uid" "$fixture/home" "$fixture/profile"
PYTHONPATH=/usr/local/lib/stopscroll /usr/bin/python3 /usr/local/lib/stopscroll/browser/tests/publisher.py "$fixture" &
publisher=$!
trap 'kill "$publisher" 2>/dev/null || true' EXIT
chown -R root:root "$fixture/install" "$fixture/state"
chown "$uid:$uid" "$fixture/runtime"
chmod 0700 "$fixture/runtime"
runuser -u "$owner" -- env PLAYWRIGHT_MODULE="$playwright_module" HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/config" XDG_CACHE_HOME="$fixture/home/cache" XDG_RUNTIME_DIR="$fixture/runtime" STOPSCROLL_BROWSER_TEST_ROOT="$fixture" dbus-run-session -- xvfb-run -a "$node_binary" /usr/local/lib/stopscroll/browser/tests/acceptance.cjs
