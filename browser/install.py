#!/usr/bin/env python3
"""Stage a signed local extension before changing the running installation."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE = Path('/usr/local/lib/stopscroll/browser')
KEY = Path('/var/lib/stopscroll/browser-key.pem')


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


def stage(destination, source=None, key=KEY):
    from stopscroll.browser import check_conflicts
    check_conflicts()
    source = Path(__file__).parent if source is None else Path(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if destination.stat().st_uid != 0 or destination.stat().st_mode & 0o077:
        raise ValueError('Каталог подготовки должен принадлежать root и иметь режим 0700')
    browser = next((p for p in ['/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'] if Path(p).is_file()), None)
    if browser is None or shutil.which('openssl') is None:
        raise RuntimeError('Для браузерной защиты нужны Chrome/Chromium и openssl')
    signing_key = destination / 'browser-key.pem'
    if Path(key).exists():
        shutil.copyfile(key, signing_key)
        signing_key.chmod(0o600)
    else:
        fd = os.open(signing_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(signing_key)], check=True, capture_output=True)
    public = subprocess.check_output(['openssl', 'pkey', '-in', str(signing_key), '-pubout', '-outform', 'DER'])
    extension_id = ''.join(chr(ord('a') + int(n, 16)) for n in hashlib.sha256(public).hexdigest()[:32])
    output = destination / 'browser'
    output.mkdir()
    shutil.copytree(source / 'extension', output / 'extension')
    write(output / 'extension/public-key.js', 'globalThis.StopscrollPublicKey = ' + json.dumps(base64.b64encode(public).decode()) + ';\n')
    manifest = json.loads((output / 'extension/manifest.json').read_text())
    manifest['update_url'] = (BASE / 'updates.xml').as_uri()
    write(output / 'extension/manifest.json', json.dumps(manifest))
    env = dict(os.environ, HOME=str(destination), XDG_CONFIG_HOME=str(destination), XDG_CACHE_HOME=str(destination))
    subprocess.run([browser, '--headless', '--no-sandbox', '--no-message-box',
                    f'--user-data-dir={destination / "profile"}',
                    f'--pack-extension={output / "extension"}', f'--pack-extension-key={signing_key}'],
                   env=env, check=True, capture_output=True, timeout=30)
    packaged = output / 'extension.crx'
    if not packaged.is_file():
        raise RuntimeError('Chrome не создал CRX')
    packaged.rename(output / 'guard.crx')
    (output / 'guard.crx').chmod(0o644)
    version = manifest['version']
    write(output / 'updates.xml', f'<?xml version="1.0" encoding="UTF-8"?><gupdate xmlns="http://www.google.com/update2/response" protocol="2.0"><app appid="{extension_id}"><updatecheck codebase="{(BASE / "guard.crx").as_uri()}" version="{version}"/></app></gupdate>\n')
    write(output / 'native-host', '#!/bin/sh\nPYTHONPATH=/usr/local/lib/stopscroll exec /usr/bin/python3 -m stopscroll.browser\n', 0o755)
    write(output / 'package.json', json.dumps(dict(id=extension_id, version=version)) + '\n')
    write(output / 'install.py', Path(__file__).read_text())
    return extension_id


def activate(destination):
    from stopscroll.browser import check_conflicts, register
    check_conflicts()
    destination = Path(destination)
    BASE.mkdir(parents=True, exist_ok=True)
    KEY.parent.mkdir(parents=True, exist_ok=True)
    # Preparation has already validated the key and created a CRX successfully.
    shutil.copyfile(destination / 'browser-key.pem', KEY)
    KEY.chmod(0o600)
    shutil.copytree(destination / 'browser', BASE, dirs_exist_ok=True)
    for message in register():
        print(message)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Установка браузерной защиты требует root')
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=Path)
    args = parser.parse_args()
    if args.stage:
        stage(args.stage)
    else:
        with tempfile.TemporaryDirectory(prefix='stopscroll-stage-') as temporary:
            extension_id = stage(temporary)
            activate(temporary)
            print('Установлена браузерная защита YouTube:', extension_id)


if __name__ == '__main__':
    main()
