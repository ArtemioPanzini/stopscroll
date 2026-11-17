#!/usr/bin/env python3
"""Package and register a root-owned local Chrome/Chromium extension on Linux."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE = Path('/usr/local/lib/stopscroll/browser')
KEY = Path('/var/lib/stopscroll/browser-key.pem')
HOST = 'org.stopscroll.youtube'
POLICIES = ['/etc/opt/chrome/policies/managed', '/etc/chromium/policies/managed']
HOSTS = ['/etc/opt/chrome/native-messaging-hosts', '/etc/chromium/native-messaging-hosts']


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.stopscroll-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def install():
    if os.geteuid() != 0:
        raise SystemExit('Установка браузерной защиты требует root')
    browser = next((p for p in ['/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'] if Path(p).is_file()), None)
    if browser is None or shutil.which('openssl') is None:
        raise SystemExit('Для браузерной защиты нужны Chrome/Chromium и openssl')
    BASE.mkdir(parents=True, exist_ok=True)
    KEY.parent.mkdir(parents=True, exist_ok=True)
    if not KEY.exists():
        # A generated signing key stays root-private and is reused on reinstall.
        fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        try:
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(KEY)], check=True, capture_output=True)
        except BaseException:
            KEY.unlink(missing_ok=True)
            raise
    KEY.chmod(0o600)
    public = subprocess.check_output(['openssl', 'pkey', '-in', str(KEY), '-pubout', '-outform', 'DER'])
    extension_id = ''.join(chr(ord('a') + int(n, 16)) for n in hashlib.sha256(public).hexdigest()[:32])
    with tempfile.TemporaryDirectory(prefix='stopscroll-pack-') as temp:
        directory = Path(temp)
        extension = directory / 'extension'
        shutil.copytree(BASE / 'extension', extension)
        manifest = json.loads((extension / 'manifest.json').read_text())
        manifest['update_url'] = (BASE / 'updates.xml').as_uri()
        write(extension / 'manifest.json', json.dumps(manifest))
        env = dict(os.environ, HOME=temp, XDG_CONFIG_HOME=temp, XDG_CACHE_HOME=temp)
        subprocess.run([browser, '--headless', '--no-sandbox', '--no-message-box',
                        f'--user-data-dir={directory / "profile"}',
                        f'--pack-extension={extension}', f'--pack-extension-key={KEY}'],
                       env=env, check=True, capture_output=True, timeout=30)
        packaged = extension.with_suffix('.crx')
        if not packaged.is_file():
            raise RuntimeError('Chrome не создал CRX')
        temporary = BASE / '.guard.crx'
        shutil.copyfile(packaged, temporary)
        temporary.chmod(0o644)
        os.replace(temporary, BASE / 'guard.crx')
    version = manifest['version']
    update = f'<?xml version="1.0" encoding="UTF-8"?><gupdate xmlns="http://www.google.com/update2/response" protocol="2.0"><app appid="{extension_id}"><updatecheck codebase="{(BASE / "guard.crx").as_uri()}" version="{version}"/></app></gupdate>\n'
    write(BASE / 'updates.xml', update)
    write(BASE / 'native-host', '#!/bin/sh\nPYTHONPATH=/usr/local/lib/stopscroll exec /usr/bin/python3 -m stopscroll.browser\n', 0o755)
    write(BASE / 'package.json', json.dumps(dict(id=extension_id, version=version)) + '\n')
    # The daemon owns ongoing registration repair and the short-lived policy.
    from stopscroll.browser import register
    for message in register():
        print(message)
    print('Установлена браузерная защита YouTube:', extension_id)


if __name__ == '__main__':
    install()
