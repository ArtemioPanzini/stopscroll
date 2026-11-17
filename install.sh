#!/bin/bash
set -euo pipefail
umask 022
fail() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID == 0 && ${SUDO_UID:-} =~ ^[0-9]+$ && ${SUDO_UID:-0} != 0 && -n ${SUDO_USER:-} ]] || fail 'Запустите через sudo из обычной учётной записи.'
[[ $(id -u "$SUDO_USER") == "$SUDO_UID" ]] || fail 'Не совпадают имя и uid владельца.'
for command in python3 systemctl ss nft useradd sudo; do
    command -v "$command" >/dev/null || fail "Не найдена команда $command. Установите зависимости из README."
done
[[ -x /usr/sbin/dnsmasq ]] || fail 'Не найден /usr/sbin/dnsmasq.'
if [[ -f /etc/stopscroll/owner-uid ]]; then
    [[ $(cat /etc/stopscroll/owner-uid) == "$SUDO_UID" ]] || fail 'Установка уже принадлежит другому пользователю.'
fi
# Every listener must belong to the running unit, with the expected executable/config.
python3 - <<'PY'
from pathlib import Path
import re
import subprocess
import sys
listeners = subprocess.run(['ss', '-H', '-lntup', 'sport = :5354'], capture_output=True, text=True, check=True).stdout
if listeners.strip():
    main = subprocess.run(['systemctl', 'show', '--property=MainPID', '--value', 'stopscroll-dns.service'], capture_output=True, text=True, check=True).stdout.strip()
    try:
        pid = int(main)
        args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        own = pid > 0 and Path(f'/proc/{pid}/exe').resolve() == Path('/usr/sbin/dnsmasq').resolve() and b'--conf-file=/var/lib/stopscroll/dnsmasq.conf' in args
    except (OSError, ValueError):
        own = False
    for line in listeners.splitlines():
        pids = re.findall(r'pid=(\d+)', line)
        if not own or not pids or any(p != main for p in pids):
            sys.exit('Ошибка: порт 5354 занят посторонним процессом; установка остановлена.')
PY
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Prevent two state writers and a watchdog restart during a repeat installation.
for unit in stopscroll-watchdog.timer stopscrolld.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl stop "$unit"
    fi
done
if ! id stopscroll-dns >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin stopscroll-dns
fi
install -d -o root -g root -m 0755 /usr/local/lib/stopscroll
if [[ $SOURCE != /usr/local/lib/stopscroll ]]; then
    rm -rf /usr/local/lib/stopscroll/stopscroll
    cp -R -- "$SOURCE/stopscroll" /usr/local/lib/stopscroll/
    install -o root -g root -m 0755 "$SOURCE/uninstall.sh" /usr/local/lib/stopscroll/uninstall.sh
fi
chown -R root:root /usr/local/lib/stopscroll/stopscroll
chmod -R u=rwX,go=rX /usr/local/lib/stopscroll/stopscroll
chown root:root /usr/local/lib/stopscroll/uninstall.sh
chmod 0755 /usr/local/lib/stopscroll/uninstall.sh
cat > /usr/local/bin/stopscroll <<'EOF'
#!/bin/sh
PYTHONPATH=/usr/local/lib/stopscroll exec /usr/bin/python3 -m stopscroll.cli "$@"
EOF
chown root:root /usr/local/bin/stopscroll
chmod 0755 /usr/local/bin/stopscroll
install -d -o root -g root -m 0755 /etc/stopscroll
printf '%s\n' "$SUDO_UID" > /etc/stopscroll/owner-uid
chown root:root /etc/stopscroll/owner-uid
chmod 0644 /etc/stopscroll/owner-uid
for unit in stopscroll-dns.service stopscrolld.service stopscroll-watchdog.service stopscroll-watchdog.timer; do
    install -o root -g root -m 0644 "$SOURCE/system/$unit" "/etc/systemd/system/$unit"
done
install -d -o root -g root -m 0755 /etc/systemd/user
install -o root -g root -m 0644 "$SOURCE/system/stopscroll-notify.service" /etc/systemd/user/stopscroll-notify.service
systemctl daemon-reload
install -d -o root -g root -m 0700 /var/lib/stopscroll
PYTHONPATH=/usr/local/lib/stopscroll /usr/bin/python3 -m stopscroll.daemon --init
systemctl enable --now stopscroll-dns.service stopscrolld.service stopscroll-watchdog.timer
if ! sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$SUDO_UID" systemctl --user daemon-reload ||
   ! sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$SUDO_UID" systemctl --user enable --now stopscroll-notify.service; then
    echo 'Предупреждение: уведомитель не запущен; войдите в графический сеанс и включите stopscroll-notify.service.' >&2
fi
for ((attempt=0; attempt<10; attempt++)); do
    [[ -S /run/stopscroll/stopscroll.sock ]] && break
    sleep 1
done
# --init logs reconciliation errors; only the owner's doctor decides success.
set +e
sudo -u "$SUDO_USER" /usr/local/bin/stopscroll doctor
exit $?
