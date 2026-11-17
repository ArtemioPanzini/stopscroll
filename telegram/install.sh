#!/bin/bash
set -euo pipefail

[[ $EUID == 0 && ${SUDO_UID:-0} != 0 && -n ${SUDO_USER:-} ]] || {
    echo 'Запустите через sudo из учётной записи владельца Stopscroll.' >&2
    exit 1
}
[[ $(cat /etc/stopscroll/owner-uid) == "$SUDO_UID" ]] || {
    echo 'Установка принадлежит другому пользователю.' >&2
    exit 1
}
[[ $(id -u "$SUDO_USER") == "$SUDO_UID" ]] || { echo 'Не совпадают имя и UID владельца.' >&2; exit 1; }
owner_gid=$(id -g "$SUDO_USER")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
binary="$script_dir/tdesktop/out/bin/Telegram"
[[ -x $binary ]] || { echo 'Сначала завершите сборку Telegram.' >&2; exit 1; }
python3 - "$script_dir/tdesktop/out/stopscroll-build.json" "$binary" <<'PY'
from pathlib import Path
import hashlib
import json
import sys
try:
    manifest = json.loads(Path(sys.argv[1]).read_text())
    with Path(sys.argv[2]).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
except (OSError, ValueError):
    raise SystemExit('Нет подтверждения успешной сборки. Завершите build.py.') from None
if (type(manifest.get('version')) is not int or manifest['version'] != 1 or
        manifest.get('configuration') != 'Debug' or
        manifest.get('auto_update') is not False or manifest.get('test_credentials') is not False or
        manifest.get('binary_sha256') != digest):
    raise SystemExit('Бинарный файл не соответствует проверенной рабочей сборке.')
PY
install -d -o root -g "$owner_gid" -m 0750 /usr/local/lib/stopscroll/telegram
install -o root -g "$owner_gid" -m 0750 "$binary" /usr/local/lib/stopscroll/telegram/.Telegram.new
python3 - "$script_dir/tdesktop/out/stopscroll-build.json" <<'PY'
from pathlib import Path
import hashlib
import json
import sys
manifest = json.loads(Path(sys.argv[1]).read_text())
target = Path('/usr/local/lib/stopscroll/telegram/.Telegram.new')
with target.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
if manifest['binary_sha256'] != digest:
    target.unlink()
    raise SystemExit('Файл изменился во время установки; повторите после окончания сборки.')
target.replace('/usr/local/lib/stopscroll/telegram/Telegram')
PY
install -o root -g root -m 0644 "$script_dir/tdesktop/Telegram/Resources/art/icon256.png" /usr/local/lib/stopscroll/telegram/icon.png
install -o root -g root -m 0755 "$script_dir/../uninstall.sh" /usr/local/lib/stopscroll/uninstall.sh
install -o root -g root -m 0644 "$script_dir/desktop.py" /usr/local/lib/stopscroll/telegram/desktop.py
cat > /usr/local/bin/stopscroll-telegram <<'SH'
#!/bin/sh
exec /usr/local/lib/stopscroll/telegram/Telegram "$@"
SH
chown root:"$owner_gid" /usr/local/bin/stopscroll-telegram
chmod 0750 /usr/local/bin/stopscroll-telegram
install -d -o root -g root -m 0755 /usr/local/share/applications
cat > /usr/local/share/applications/org.telegram.desktop.desktop <<'DESKTOP'
[Desktop Entry]
Version=1.0
Type=Application
Name=Telegram (Stopscroll)
Comment=Telegram с блокировкой отвлекающих каналов
Exec=/usr/local/bin/stopscroll-telegram -- %u
TryExec=/usr/local/bin/stopscroll-telegram
Icon=/usr/local/lib/stopscroll/telegram/icon.png
Terminal=false
Categories=Network;InstantMessaging;
MimeType=x-scheme-handler/tg;x-scheme-handler/tonsite;
StartupWMClass=TelegramDesktop
DESKTOP
chown root:root /usr/local/share/applications/org.telegram.desktop.desktop
chmod 0644 /usr/local/share/applications/org.telegram.desktop.desktop
if command -v update-desktop-database >/dev/null; then
    update-desktop-database /usr/local/share/applications
fi
if ! sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$SUDO_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$SUDO_UID/bus" /usr/bin/python3 /usr/local/lib/stopscroll/telegram/desktop.py activate; then
    echo 'Не удалось переключить ярлыки; запустите stopscroll-telegram вручную.' >&2
fi
echo 'Установлен Telegram (Stopscroll). Запуск: stopscroll-telegram'
echo 'Работающий Telegram не остановлен; для переключения завершите его и запустите Telegram (Stopscroll).'
