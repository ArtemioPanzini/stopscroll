"""YouTube-only browser policy and read-only native messaging host."""
import base64
import json
import math
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import time

POLICY = '/run/stopscroll/browser.json'
LEASE_SEC = 4
HOST_NAME = 'org.stopscroll.youtube'
SIGNING_KEY = '/var/lib/stopscroll/browser-key.pem'
_last_sequence = 0


def browser_policy(state, now):
    site = state.sites.get('youtube')
    deadline = 0
    if site is not None:
        session = state.sessions.get('youtube')
        if site.mode == 'limited' and session is not None:
            deadline = session.ends
        if state.pause_until is not None and state.pause_until > now:
            deadline = max(deadline, state.pause_until)
    else:
        # Removing the group through the normal delayed command removes its guard.
        deadline = now + LEASE_SEC
    return dict(version=1, issued_at=now, lease_until=now + LEASE_SEC,
                allowed_until=max(0, deadline),
                domains=sorted(site.domains) if site else [],
                exceptions=sorted(site.exceptions) if site else [])


def sign_policy(data, key=SIGNING_KEY):
    global _last_sequence
    _last_sequence = max(time.monotonic_ns(), _last_sequence + 1)
    data = dict(data, sequence=str(_last_sequence))
    payload = json.dumps(data, separators=(',', ':'), allow_nan=False).encode()
    signature = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key)],
                               input=payload, capture_output=True, check=True, timeout=5).stdout
    return dict(payload=base64.b64encode(payload).decode(), signature=base64.b64encode(signature).decode())


def publish_policy(state, now, path=POLICY, key=SIGNING_KEY):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.browser-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(sign_policy(browser_policy(state, now), key), stream, separators=(',', ':'), allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_policy(path=POLICY, now=None):
    from .changes import ChangeError, normalize_domain
    now = time.time() if now is None else now
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            return None
        envelope = json.loads(raw)
        if (not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'}
                or any(not isinstance(envelope[k], str) for k in envelope)):
            return None
        payload = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        if len(payload) > 65536 or not 128 <= len(signature) <= 1024:
            return None
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {
                'version', 'sequence', 'issued_at', 'lease_until', 'allowed_until', 'domains', 'exceptions'}:
            return None
        if (not isinstance(data['sequence'], str) or not data['sequence'].isascii()
                or not data['sequence'].isdigit() or not 1 <= len(data['sequence']) <= 25
                or data['sequence'].startswith('0')):
            return None
        if type(data['version']) is not int or data['version'] != 1:
            return None
        for key in ('issued_at', 'lease_until', 'allowed_until'):
            if type(data[key]) not in (float, int) or not math.isfinite(data[key]):
                return None
        if not (now - LEASE_SEC <= data['issued_at'] <= now + 1
                and now < data['lease_until'] <= data['issued_at'] + LEASE_SEC):
            return None
        for key in ('domains', 'exceptions'):
            if not isinstance(data[key], list) or len(data[key]) > 100:
                return None
            if any(not isinstance(domain, str) or normalize_domain(domain) != domain
                   for domain in data[key]):
                return None
        return envelope
    except (OSError, ValueError, TypeError, ChangeError):
        return None


def send_message(stream, data):
    payload = json.dumps(data, separators=(',', ':'), allow_nan=False).encode()
    stream.write(struct.pack('=I', len(payload)) + payload)
    stream.flush()


def native_host(input_stream=None, output_stream=None, path=POLICY):
    """Only publish sanitized policy. Browser input is never interpreted as a command."""
    input_stream = sys.stdin.buffer if input_stream is None else input_stream
    output_stream = sys.stdout.buffer if output_stream is None else output_stream
    try:
        while True:
            send_message(output_stream, read_policy(path))
            # A disconnected browser closes stdin. Reject any inbound data rather
            # than permitting a native bridge to become a daemon command channel.
            readable, _, _ = select.select([input_stream], [], [], 0.5)
            if readable:
                return 0
    except (BrokenPipeError, OSError, ValueError):
        return 0


PACKAGE = '/usr/local/lib/stopscroll/browser/package.json'
REGISTRATION = 'stopscroll-youtube.json'


def check_conflicts(runner=None):
    """Reject competing platform dictionaries before packaging or registration."""
    from .system import Runner
    runner = Runner() if runner is None else runner
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    for directory in ('/etc/opt/chrome/policies/managed', '/etc/chromium/policies/managed'):
        folder = runner.path(directory)
        if not folder.exists():
            continue
        for path in folder.iterdir():
            if path.name == REGISTRATION or path.is_dir():
                continue
            try:
                # Chromium loads every file and accepts JSONC. Reject syntax we
                # cannot unambiguously interpret, rather than silently hiding it.
                data = json.loads(path.read_text(), object_pairs_hook=unique_object,
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(data, dict):
                    raise ValueError('policy is not an object')
            except (OSError, ValueError, UnicodeError) as exc:
                raise ValueError(f'Неоднозначный или нечитаемый файл политики {path.name}; установка остановлена') from exc
            if 'ExtensionSettings' in data:
                raise ValueError(f'Конфликт ExtensionSettings в {path.name}; политика не изменена')


def register(runner=None):
    """Repair our dedicated managed/native-host files, preserving other policies."""
    import re
    from .system import Runner
    runner = Runner() if runner is None else runner
    check_conflicts(runner)
    metadata = runner.path(PACKAGE)
    if not metadata.exists():
        return []
    data = json.loads(metadata.read_text())
    extension_id = data.get('id')
    if not isinstance(extension_id, str) or not re.fullmatch('[a-p]{32}', extension_id):
        raise ValueError('Некорректный ID браузерной защиты')
    base = '/usr/local/lib/stopscroll/browser'
    registration = dict(ExtensionSettings={extension_id: dict(installation_mode='force_installed', update_url=f'file://{base}/updates.xml')})
    host = dict(name=HOST_NAME, description='Stopscroll YouTube policy (read-only)',
                path=f'{base}/native-host', type='stdio',
                allowed_origins=[f'chrome-extension://{extension_id}/'])
    paths = [(f'{directory}/{REGISTRATION}', registration) for directory in (
        '/etc/opt/chrome/policies/managed', '/etc/chromium/policies/managed')]
    paths += [(f'{directory}/{HOST_NAME}.json', host) for directory in (
        '/etc/opt/chrome/native-messaging-hosts', '/etc/chromium/native-messaging-hosts')]
    messages = []
    for name, value in paths:
        path = runner.path(name)
        text = json.dumps(value, separators=(',', ':')) + '\n'
        if path.exists() and path.read_text() == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.browser-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(text)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        messages.append(f'Исправлена браузерная защита: {name}')
    return messages


if __name__ == '__main__':
    raise SystemExit(native_host())
