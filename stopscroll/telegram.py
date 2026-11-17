"""Telegram channel identities and atomic, client-readable policy publication."""
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

POLICY = '/etc/stopscroll/telegram.json'
NAME = re.compile(r'[a-z][a-z0-9_]{0,31}', re.ASCII)


def normalize_channel(raw):
    if not isinstance(raw, str) or not raw or any(c.isspace() for c in raw):
        raise ValueError('Укажите корректное имя Telegram-канала')
    name = raw
    if raw.startswith('@'):
        name = raw[1:]
    elif '://' in raw or '/' in raw or '.' in raw:
        try:
            url = urlsplit(raw if '://' in raw else 'https://' + raw)
            if (url.scheme.lower() not in ('http', 'https') or
                    url.hostname not in ('t.me', 'telegram.me', 'www.t.me', 'www.telegram.me') or
                    url.username is not None or url.password is not None or url.port is not None):
                raise ValueError()
            parts = url.path.strip('/').split('/')
            if parts[0] == 's':
                parts = parts[1:]
            if not (len(parts) == 1 or len(parts) == 2 and re.fullmatch(r'[1-9][0-9]*', parts[1])):
                raise ValueError()
            name = parts[0]
        except (ValueError, IndexError):
            raise ValueError('Укажите публичную ссылку Telegram-канала') from None
    name = name.lower()
    if not NAME.fullmatch(name) or name in ('joinchat', 'addlist', 'c', 's'):
        raise ValueError('Укажите корректное имя Telegram-канала')
    return name


def validate_channel_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,15}', value) or int(value) > 2**52 - 1:
        raise ValueError('Некорректный ID Telegram-канала')
    return value


def publish_policy(channels, path=POLICY):
    """Replace a complete 0644 policy and fsync both file and directory."""
    rows = []
    for username, ids in sorted(channels.items()):
        if normalize_channel(username) != username:
            raise ValueError('Некорректное имя в политике')
        rows.append(dict(username=username, ids=sorted({validate_channel_id(i) for i in ids}, key=int)))
    data = json.dumps(dict(version=1, channels=rows), ensure_ascii=False, separators=(',', ':')) + '\n'
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.telegram-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
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
