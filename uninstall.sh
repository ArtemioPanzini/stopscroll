#!/bin/bash
# Do not abort: every cleanup step must be attempted after a partial installation.
if [[ $EUID != 0 ]]; then
    echo 'Ошибка: удаление требует root.' >&2
    exit 1
fi
failed=0
cleanup() {
    "$@" || { echo "Предупреждение: не выполнено: $*" >&2; failed=1; }
}
# Stop the owner's notifier while its unit and account information still exist.
if [[ -f /etc/stopscroll/owner-uid ]]; then
    owner_uid=$(cat /etc/stopscroll/owner-uid)
    if [[ $owner_uid =~ ^[0-9]+$ && $owner_uid != 0 ]]; then
        if [[ -f /usr/local/lib/stopscroll/telegram/desktop.py ]]; then
            if [[ -S /run/user/$owner_uid/bus ]]; then
                cleanup sudo -u "#$owner_uid" XDG_RUNTIME_DIR="/run/user/$owner_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$owner_uid/bus" /usr/bin/python3 /usr/local/lib/stopscroll/telegram/desktop.py restore
            else
                cleanup sudo -u "#$owner_uid" dbus-run-session -- /usr/bin/python3 /usr/local/lib/stopscroll/telegram/desktop.py restore
            fi
        fi
        cleanup sudo -u "#$owner_uid" XDG_RUNTIME_DIR="/run/user/$owner_uid" systemctl --user disable --now stopscroll-notify.service
    fi
fi
for unit in stopscroll-watchdog.timer stopscrolld.service stopscroll-dns.service; do
    cleanup systemctl disable "$unit"
    cleanup systemctl stop "$unit"
done
cleanup rm -f /etc/systemd/resolved.conf.d/stopscroll.conf
cleanup systemctl restart systemd-resolved
cleanup nft delete table inet stopscroll
cleanup rm -f /etc/opt/chrome/policies/managed/stopscroll.json
cleanup rm -f /etc/opt/chrome/policies/managed/stopscroll-youtube.json /etc/chromium/policies/managed/stopscroll-youtube.json
cleanup rm -f /etc/opt/chrome/native-messaging-hosts/org.stopscroll.youtube.json /etc/chromium/native-messaging-hosts/org.stopscroll.youtube.json
cleanup rm -f /run/stopscroll/browser.json
cleanup rm -f /etc/chromium/policies/managed/stopscroll.json
cleanup /usr/bin/python3 - <<'PY'
import json
import os
from pathlib import Path
import tempfile
path = Path('/etc/firefox/policies/policies.json')
if path.exists():
    data = json.loads(path.read_text())
    policies = data.get('policies', {})
    policies.pop('DNSOverHTTPS', None)
    stat = path.stat()
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.st_mode & 0o777)
            os.fchown(stream.fileno(), stat.st_uid, stat.st_gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
PY
cleanup rm -f /etc/systemd/system/stopscroll-dns.service /etc/systemd/system/stopscrolld.service /etc/systemd/system/stopscroll-watchdog.service /etc/systemd/system/stopscroll-watchdog.timer /etc/systemd/user/stopscroll-notify.service
cleanup rm -f /usr/local/bin/stopscroll
cleanup rm -f /usr/local/bin/stopscroll-telegram /usr/local/share/applications/org.telegram.desktop.desktop
# Includes the published Telegram channel policy telegram.json.
cleanup rm -rf /etc/stopscroll /var/lib/stopscroll
if id stopscroll-dns >/dev/null 2>&1; then
    cleanup userdel stopscroll-dns
fi
cleanup systemctl daemon-reload
if [[ ${owner_uid:-} =~ ^[0-9]+$ && ${owner_uid:-0} != 0 ]]; then
    cleanup sudo -u "#$owner_uid" XDG_RUNTIME_DIR="/run/user/$owner_uid" systemctl --user daemon-reload
fi
cleanup rm -rf /usr/local/lib/stopscroll
exit "$failed"
