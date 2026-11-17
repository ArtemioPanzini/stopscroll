#!/usr/bin/env python3
"""Select the patched client at existing desktop entry points, with restoration."""
import ast
import json
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys
import tempfile

APP = 'org.telegram.desktop.desktop'
SCHEMES = ('x-scheme-handler/tg', 'x-scheme-handler/tonsite')


def command(argv):
    result = subprocess.run(argv, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Не удалось обновить настройки рабочего стола: ' + argv[0])
    return result.stdout.strip()


def favorites():
    text = command(['gsettings', 'get', 'org.gnome.shell', 'favorite-apps'])
    if text == '@as []':
        return []
    value = ast.literal_eval(text)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError('Неверный список закреплённых приложений')
    return value


def set_favorites(value):
    command(['gsettings', 'set', 'org.gnome.shell', 'favorite-apps', repr(value)])


def replace_favorite(items, old, new):
    result = []
    for item in items:
        value = new if item == old else item
        if value != new or value not in result:
            result.append(value)
    return result


def handler(scheme):
    return command(['xdg-mime', 'query', 'default', scheme])


def telegram_handler(app):
    if not app or Path(app).name != app:
        return False
    owner_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    data = os.environ.get('XDG_DATA_HOME', str(owner_home / '.local/share'))
    directories = [data, *os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')]
    for directory in directories:
        entry = Path(directory) / 'applications' / app
        if not entry.is_file():
            continue
        for line in entry.read_text(errors='replace').splitlines():
            if line.startswith('Exec='):
                args = shlex.split(line[5:])
                if args and Path(args[0]).name == 'env':
                    args = args[1:]
                    while args and '=' in args[0]:
                        args.pop(0)
                return bool(args and Path(args[0]).name in ('Telegram', 'telegram', 'telegram-desktop', 'stopscroll-telegram'))
        return False
    return False


def write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.desktop-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save(path, value):
    write(path, json.dumps(value))


def load(path):
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1 or
            not isinstance(value.get('handlers'), dict) or set(value['handlers']) != set(SCHEMES) or
            not all(isinstance(v, str) for v in value['handlers'].values()) or
            not (value.get('favorite') is None or isinstance(value.get('favorite'), str)) or
            not isinstance(value.get('managed_handlers'), list) or
            any(s not in SCHEMES for s in value['managed_handlers']) or
            type(value.get('managed_favorite')) is not bool):
        raise ValueError('Файл восстановления ярлыков повреждён')
    return value


def activate(path):
    current = favorites()
    if path.exists():
        backup = load(path)
    else:
        handlers = {scheme: handler(scheme) for scheme in SCHEMES}
        old = handlers[SCHEMES[0]]
        native = telegram_handler(old)
        backup = dict(version=1, handlers=handlers,
                      favorite=old if native and old in current else None,
                      managed_handlers=[s for s in SCHEMES if native and handlers[s] == old and old != APP],
                      managed_favorite=bool(native and old in current and old != APP))
        write(path.with_name('desktop-recovery.py'), Path(__file__).read_text())
        save(path, backup)
    for scheme in backup['managed_handlers']:
        old = backup['handlers'][scheme]
        if handler(scheme) in (old, APP):
            command(['xdg-mime', 'default', APP, scheme])
    old = backup['favorite']
    if backup['managed_favorite'] and old in current:
        set_favorites(replace_favorite(current, old, APP))


def restore(path):
    if not path.exists():
        path.with_name('desktop-recovery.py').unlink(missing_ok=True)
        return
    backup = load(path)
    for scheme in list(backup['managed_handlers']):
        old = backup['handlers'][scheme]
        if handler(scheme) == APP:
            command(['xdg-mime', 'default', old, scheme])
        backup['managed_handlers'].remove(scheme)
        save(path, backup)
    if backup['managed_favorite']:
        current = favorites()
        old = backup['favorite']
        if APP in current:
            set_favorites(replace_favorite(current, APP, old))
        backup['managed_favorite'] = False
        save(path, backup)
    path.unlink()
    path.with_name('desktop-recovery.py').unlink(missing_ok=True)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('activate', 'restore'):
        raise ValueError('Укажите activate или restore')
    owner_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(owner_home / '.config')))
    path = config / 'stopscroll/desktop.json'
    if sys.argv[1] == 'activate':
        activate(path)
    else:
        restore(path)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        owner_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        config = Path(os.environ.get('XDG_CONFIG_HOME', str(owner_home / '.config')))
        recovery = config / 'stopscroll/desktop-recovery.py'
        if recovery.exists():
            print('Повторить восстановление: python3 ' + shlex.quote(str(recovery)) + ' restore', file=sys.stderr)
        raise SystemExit(1)
