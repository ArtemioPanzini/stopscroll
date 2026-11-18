"""Install a verified Stopscroll release over USB."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ANDROID_DIR = Path(__file__).resolve().parent
if __package__ in ('', None):
    sys.path.insert(0, str(ANDROID_DIR.parent))

from stopscroll.android_cli import (
    PACKAGE, _open_regular_file, _read_release_receipt, _receipt_path, _sdk_tool,
    inspect_installed_apk, inspect_release_apk,
)
from stopscroll.android_transport import AndroidRequestError, AndroidTransport, AndroidTransportError


def _publish_staged_release(candidate, output, receipt_path, *, certificate, version):
    """Finalize a private staging file after build.sh has checked all release gates."""
    if not re.fullmatch(r'[a-f0-9]{64}', certificate) or type(version) is not int or version <= 0:
        raise OSError('Invalid verified APK certificate or version.')
    with _open_regular_file(candidate) as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        os.fsync(stream.fileno())
    receipt = {
        'schema': 1, 'package': PACKAGE, 'certificate_sha256': certificate,
        'version_code': version, 'apk_sha256': digest,
    }
    receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(receipt_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        try:
            previous = _read_release_receipt(receipt_path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if previous['certificate_sha256'] != certificate:
                raise OSError('The signing certificate differs from the previous release receipt.')
            if previous['version_code'] > version:
                raise OSError('The APK version is older than the previous release receipt.')

        with tempfile.TemporaryDirectory(prefix='.release-', dir=receipt_path.parent) as temp:
            new_receipt = Path(temp) / receipt_path.name
            with new_receipt.open('w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(receipt, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(candidate, output)
            output_dir = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(output_dir)
            finally:
                os.close(output_dir)
            os.replace(new_receipt, receipt_path)
            os.fsync(directory)
    finally:
        os.close(directory)
    return receipt


def install_release(apk, receipt_path, transport, *, tools, runner=subprocess.run):
    """Check both APK identities, then install one private snapshot through the bound shell."""
    expected = _read_release_receipt(receipt_path)
    if expected['version_code'] <= 0:
        raise OSError('Invalid version in the APK release receipt.')

    with tempfile.TemporaryDirectory(prefix='stopscroll-install-') as directory:
        candidate = Path(directory) / 'stopscroll.apk'
        with _open_regular_file(apk) as source, candidate.open('xb') as target:
            shutil.copyfileobj(source, target)
        inspect_release_apk(candidate, tools=tools, runner=runner, expected=expected)

        bound = transport.bind()
        output = bound.shell('pm', 'list', 'packages', '--user', '0', '-u', PACKAGE)
        packages = output.splitlines()
        if any(not re.fullmatch(rb'package:[A-Za-z][A-Za-z0-9_.]*', row) for row in packages):
            raise OSError('Unrecognized Android package listing.')
        if f'package:{PACKAGE}'.encode('ascii') in packages:
            installed = inspect_installed_apk(bound, tools=tools, runner=runner)
            if installed['package'] != PACKAGE:
                raise OSError('Installed base APK has a different package name.')
            if installed['certificate_sha256'] != expected['certificate_sha256']:
                raise OSError('Installed Stopscroll uses a different signing certificate.')
            if installed['version_code'] <= 0:
                raise OSError('Installed Stopscroll has an invalid version code.')
            if installed['version_code'] > expected['version_code']:
                raise OSError('Installed Stopscroll is newer than this APK.')

        print('Installing Stopscroll. Confirm any installation prompt on the phone.', flush=True)
        try:
            with candidate.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                result = bound.stream_to_shell(
                    stream, 'pm', 'install', '--user', '0', '-r', '--pkg', PACKAGE, '-S', str(size), '-',
                )
            if result.strip() != b'Success':
                raise OSError('Android Package Manager did not confirm installation.')
            return inspect_installed_apk(bound, tools=tools, runner=runner, expected=expected)
        except (AndroidTransportError, OSError, UnicodeError) as exc:
            raise OSError(f'Installation could not be confirmed. {exc} '
                          'Run stopscroll android doctor over USB before retrying.') from exc


def main():
    parser = argparse.ArgumentParser(prog='android/install.sh', description=__doc__)
    parser.add_argument('--usb-serial', metavar='SERIAL', help='select an authorized USB phone by serial')
    args = parser.parse_args()

    output = ANDROID_DIR / 'out' / 'stopscroll.apk'
    try:
        tools = tuple(_sdk_tool(tool) for tool in ('apksigner', 'apkanalyzer'))
        receipt = install_release(output, _receipt_path(), AndroidTransport(args.usb_serial, timeout=180),
                                  tools=tools)
        print(f"Verified installed Android release {receipt['version_code']}.")
    except (AndroidRequestError, AndroidTransportError, OSError, UnicodeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
