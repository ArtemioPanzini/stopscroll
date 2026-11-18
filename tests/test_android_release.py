import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from android.release import _publish_staged_release, install_release
from stopscroll.android_cli import PACKAGE
from stopscroll.android_transport import AndroidTransport, AndroidTransportError


CERTIFICATE = 'a' * 64
VERSION = 30000071
APK = b'new APK with raw bytes \x00\x0d\x0a~.\xff'
OLD_APK = b'previous installed APK'
TOOLS = ('apksigner', 'apkanalyzer')
PACKAGE_PATH = b'package:/data/app/~~app/stopscroll-YQ==/base.apk\n'
PACKAGE_LIST = f'package:{PACKAGE}\n'.encode()


def completed(output, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=output)


def sdk_output(arguments, *, package=PACKAGE, version=VERSION, certificate=CERTIFICATE):
    if arguments[:3] == ['apksigner', 'verify', '--print-certs']:
        return completed(f'Signer #1 certificate SHA-256 digest: {certificate}\n'.encode())
    if arguments[:3] == ['apkanalyzer', 'manifest', 'application-id']:
        return completed(f'{package}\n'.encode())
    if arguments[:3] == ['apkanalyzer', 'manifest', 'version-code']:
        return completed(f'{version}\n'.encode())
    raise AssertionError(arguments)


class PublisherEntryPointTests(unittest.TestCase):
    def test_direct_publish_cannot_replace_release_or_consume_rollback(self):
        repo = Path(__file__).resolve().parents[1]
        for name in ('unchecked.apk', 'stopscroll-g0.apk'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                fixture_repo = Path(directory)
                android = fixture_repo / 'android'
                out = android / 'out'
                out.mkdir(parents=True)
                (fixture_repo / 'stopscroll').symlink_to(repo / 'stopscroll', target_is_directory=True)
                script = android / 'release.py'
                script.write_bytes((repo / 'android/release.py').read_bytes())
                output = out / 'stopscroll.apk'
                output.write_bytes(APK)
                candidate = out / name
                candidate.write_bytes(OLD_APK)
                config = fixture_repo / 'config'
                receipt = config / 'stopscroll/android-release.json'
                receipt.parent.mkdir(parents=True)
                receipt.write_text(json.dumps({
                    'schema': 1, 'package': PACKAGE, 'certificate_sha256': CERTIFICATE,
                    'version_code': VERSION, 'apk_sha256': hashlib.sha256(APK).hexdigest(),
                }))
                receipt.chmod(0o600)
                original_receipt = receipt.read_bytes()

                result = subprocess.run(
                    [sys.executable, '-I', str(script), 'publish', str(candidate), CERTIFICATE, str(VERSION)],
                    env=os.environ | {'XDG_CONFIG_HOME': str(config)}, capture_output=True, check=False,
                )

                self.assertNotEqual(result.returncode, 0, result.stdout.decode())
                self.assertEqual(output.read_bytes(), APK)
                self.assertEqual(receipt.read_bytes(), original_receipt)
                self.assertEqual(candidate.read_bytes(), OLD_APK)


class BuildPublicationFixtureTests(unittest.TestCase):
    def test_build_publishes_only_the_verified_copy_and_preserves_g0(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            fixture_repo = Path(directory)
            android = fixture_repo / 'android'
            out = android / 'out'
            out.mkdir(parents=True)
            (fixture_repo / 'stopscroll').symlink_to(repo / 'stopscroll', target_is_directory=True)
            (android / 'release.py').write_bytes((repo / 'android/release.py').read_bytes())
            rollback = out / 'stopscroll-g0.apk'
            rollback.write_bytes(OLD_APK)
            upstream = android / 'rethink-app'
            gradle_apk = upstream / ('app/build/outputs/apk/fdroidFull/stopscrollRelease/'
                                     'app-fdroid-full-stopscrollRelease.apk')
            gradle_apk.parent.mkdir(parents=True)
            notices = {'RethinkDNS-Apache-2.0.txt': upstream / 'LICENSE',
                       'Firestack-MPL-2.0.txt': fixture_repo / 'firestack-license',
                       'Firestack-modifications.patch': fixture_repo / 'firestack.patch'}
            for name, path in notices.items():
                path.write_text(name)
            aar = fixture_repo / 'firestack.aar'
            with ZipFile(gradle_apk, 'w') as apk_zip, ZipFile(aar, 'w') as native_zip:
                for abi in ('arm64-v8a', 'armeabi-v7a', 'x86_64'):
                    apk_zip.writestr(f'lib/{abi}/libgojni.so', abi)
                    native_zip.writestr(f'jni/{abi}/libgojni.so', abi)
                for name, path in notices.items():
                    apk_zip.write(path, f'assets/{name}')
            original_apk = gradle_apk.read_bytes()
            manifest = fixture_repo / 'decoded-manifest.xml'
            manifest.write_text(f"""
                <manifest xmlns:android="http://schemas.android.com/apk/res/android">
                  <application android:label="Stopscroll" android:allowBackup="false">
                    <provider android:name="{PACKAGE}.UsbControlProvider"
                        android:authorities="{PACKAGE}.usb" android:exported="true"
                        android:grantUriPermissions="false" />
                    <service android:permission="android.permission.BIND_VPN_SERVICE">
                      <intent-filter><action android:name="android.net.VpnService" /></intent-filter>
                    </service>
                  </application>
                </manifest>
            """)
            apksigner = fixture_repo / 'apksigner'
            apksigner.write_text(f'#!/bin/sh\nprintf "Signer #1 certificate SHA-256 digest: {CERTIFICATE}\\n"\n')
            apkanalyzer = fixture_repo / 'apkanalyzer'
            apkanalyzer.write_text(f"""#!/bin/sh
case "$2" in
    application-id) printf '%s\\n' '{PACKAGE}' ;;
    debuggable) printf '%s\\n' 'false' ;;
    target-sdk) printf '%s\\n' '37' ;;
    version-code) printf '%s\\n' "$fixture_version" ;;
    print) cat "$fixture_manifest" ;;
    *) exit 1 ;;
esac
""")
            apksigner.chmod(0o700)
            apkanalyzer.chmod(0o700)
            build = (repo / 'android/build.sh').read_text()
            tail = build[build.index('\ngradle_apk='):]
            host = "umask 077\nfail() { printf '%s\\n' \"$*\" >&2; exit 1; }\nlock_value() { printf '37\\n'; }\n"
            config = fixture_repo / 'config'
            env = os.environ | {
                'repo_dir': str(fixture_repo), 'android_dir': str(android), 'upstream': str(upstream),
                'scratch': str(fixture_repo), 'apksigner': str(apksigner), 'apkanalyzer': str(apkanalyzer),
                'expected_cert': CERTIFICATE, 'release_version': str(VERSION), 'fixture_manifest': str(manifest),
                'STOPSCROLL_FIRESTACK_AAR': str(aar),
                'STOPSCROLL_FIRESTACK_PATCH': str(notices['Firestack-modifications.patch']),
                'STOPSCROLL_FIRESTACK_LICENSE': str(notices['Firestack-MPL-2.0.txt']),
                'XDG_CONFIG_HOME': str(config),
            }
            output = out / 'stopscroll.apk'
            receipt = config / 'stopscroll/android-release.json'

            def finalize(*, version):
                return subprocess.run(['bash', '-euo', 'pipefail'], text=True, input=host + tail,
                                      env=env | {'fixture_version': str(version)}, capture_output=True, check=False)

            wrong_version = finalize(version=VERSION - 1)
            self.assertNotEqual(wrong_version.returncode, 0, wrong_version.stdout)
            self.assertIn('versionCode', wrong_version.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(receipt.exists())
            self.assertEqual(rollback.read_bytes(), OLD_APK)
            self.assertEqual(gradle_apk.read_bytes(), original_apk)

            firestack_license = notices['Firestack-MPL-2.0.txt']
            license_text = firestack_license.read_bytes()
            firestack_license.write_text('modified license')
            wrong_archive = finalize(version=VERSION)
            self.assertNotEqual(wrong_archive.returncode, 0, wrong_archive.stdout)
            self.assertFalse(output.exists())
            self.assertFalse(receipt.exists())
            firestack_license.write_bytes(license_text)

            verified = finalize(version=VERSION)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertIn('Verified APK', verified.stdout)
            self.assertEqual(rollback.read_bytes(), OLD_APK)
            self.assertEqual(gradle_apk.read_bytes(), original_apk)
            self.assertEqual(output.read_bytes(), original_apk)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(receipt.read_bytes()), {
                'schema': 1, 'package': PACKAGE, 'version_code': VERSION, 'certificate_sha256': CERTIFICATE,
                'apk_sha256': hashlib.sha256(original_apk).hexdigest(),
            })


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        self.apks = directory / 'out'
        self.apks.mkdir()
        self.candidate = self.apks / 'staged.apk'
        self.candidate.write_bytes(APK)
        self.candidate.chmod(0o600)
        self.apk = self.apks / 'stopscroll.apk'
        self.receipt = directory / 'config' / 'stopscroll' / 'android-release.json'

    def publish(self, *, cert=CERTIFICATE, version=VERSION):
        return _publish_staged_release(self.candidate, self.apk, self.receipt,
                                       certificate=cert, version=version)

    def test_publish_private_receipt_for_same_bytes_and_preserve_g0(self):
        rollback = self.apks / 'stopscroll-g0.apk'
        rollback.write_bytes(OLD_APK)
        self.assertEqual(self.publish(), json.loads(self.receipt.read_bytes()))
        self.assertEqual(self.apk.read_bytes(), APK)
        self.assertEqual(rollback.read_bytes(), OLD_APK)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.apk.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.receipt.read_bytes()), {
            'schema': 1, 'package': PACKAGE, 'certificate_sha256': CERTIFICATE,
            'version_code': VERSION, 'apk_sha256': hashlib.sha256(APK).hexdigest(),
        })

    def test_refuses_key_rotation_and_version_decrease_without_publishing(self):
        self.publish()
        established = self.receipt.read_bytes()
        self.candidate.write_bytes(OLD_APK)
        for cert, version, reason in [('b' * 64, VERSION + 1, 'certificate'),
                                      (CERTIFICATE, VERSION - 1, 'older')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(OSError, reason):
                self.publish(cert=cert, version=version)
            self.assertEqual(self.receipt.read_bytes(), established)
            self.assertEqual(self.apk.read_bytes(), APK)

    def test_receipt_uses_rename_and_publication_failure_leaves_old_receipt_whole(self):
        self.publish()
        old_receipt = self.receipt.read_bytes()
        self.candidate.write_bytes(b'updated release')
        self.candidate.chmod(0o600)
        real_replace = os.replace

        def interrupted(source, destination):
            if destination == self.receipt:
                self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(source.read_bytes())['version_code'], VERSION + 1)
                self.assertEqual(self.receipt.read_bytes(), old_receipt)
                raise OSError('receipt rename failed')
            real_replace(source, destination)

        with patch('android.release.os.replace', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'rename failed'):
                self.publish(version=VERSION + 1)
        self.assertEqual(self.receipt.read_bytes(), old_receipt)
        self.assertEqual(list(self.receipt.parent.iterdir()), [self.receipt])
        self.assertEqual(self.apk.read_bytes(), b'updated release')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        directory = Path(temp.name)
        self.apk = directory / 'stopscroll.apk'
        self.apk.write_bytes(APK)
        self.receipt = directory / 'android-release.json'
        self.receipt.write_text(json.dumps({
            'schema': 1, 'package': PACKAGE, 'certificate_sha256': CERTIFICATE,
            'version_code': VERSION, 'apk_sha256': hashlib.sha256(APK).hexdigest(),
        }))
        self.receipt.chmod(0o600)
        self.transport = Mock()
        self.bound = self.transport.bind.return_value
        self.installed_bytes = OLD_APK
        self.installed_fields = {'version': VERSION - 1}
        self.local_fields = {}
        self.ok_output = b'Success\n'
        self.bound.shell.side_effect = self.shell
        self.bound.pull.side_effect = self.pull
        self.bound.stream_to_shell.side_effect = self.stream
        self.installs = []

    def shell(self, *args):
        if args == ('pm', 'list', 'packages', '--user', '0', '-u', PACKAGE):
            return PACKAGE_LIST if self.installed_bytes else b''
        self.assertEqual(args, ('pm', 'path', '--user', '0', PACKAGE))
        return PACKAGE_PATH

    def pull(self, source, destination):
        self.assertEqual(source, '/data/app/~~app/stopscroll-YQ==/base.apk')
        destination.write_bytes(self.installed_bytes)

    def stream(self, source, *args):
        self.assertEqual(args, ('pm', 'install', '--user', '0', '-r', '--pkg', PACKAGE, '-S', str(len(APK))))
        self.installs.append(source.read())
        if self.ok_output == b'Success\n':
            self.installed_bytes = self.installs[-1]
        return self.ok_output

    def sdk(self, arguments, **options):
        candidate = Path(arguments[-1])
        self.assertEqual(candidate.parent.stat().st_mode & 0o777, 0o700)
        fixture = candidate.read_bytes()
        return sdk_output(arguments, **(self.local_fields if fixture == APK else self.installed_fields))

    def install(self):
        return install_release(self.apk, self.receipt, self.transport, tools=TOOLS, runner=self.sdk)

    def test_fresh_install_update_and_idempotent_reinstall_use_bound_channel(self):
        self.installed_bytes = None
        self.assertEqual(self.install()['version_code'], VERSION)
        self.assertEqual(self.installs, [APK])
        self.assertEqual(self.bound.pull.call_count, 1)
        self.transport.bind.assert_called_once_with()

        self.installed_bytes = OLD_APK
        self.assertEqual(self.install()['version_code'], VERSION)
        self.assertEqual(self.install()['version_code'], VERSION)
        self.assertEqual(self.installs, [APK, APK, APK])
        self.assertEqual(self.bound.pull.call_count, 5)
        self.assertEqual(self.transport.bind.call_count, 3)

    def test_refuses_bad_receipt_and_changed_local_artifact_before_usb(self):
        self.receipt.chmod(0o644)
        with self.assertRaisesRegex(OSError, '0600'):
            self.install()
        self.receipt.chmod(0o600)
        self.apk.write_bytes(b'unverified payload')
        with self.assertRaisesRegex(OSError, 'apk_sha256'):
            self.install()
        self.transport.bind.assert_not_called()

    def test_refuses_locally_mismatched_signature_package_and_version(self):
        for change, field in [({'certificate': 'b' * 64}, 'certificate_sha256'),
                              ({'package': 'org.example.other'}, 'package'),
                              ({'version': VERSION - 1}, 'version_code')]:
            with self.subTest(field=field), self.assertRaisesRegex(OSError, field):
                self.local_fields = change
                self.install()
        self.transport.bind.assert_not_called()

    def test_refuses_installed_package_mismatch_bad_path_and_downgrade(self):
        for change, reason in [({'certificate': 'b' * 64}, 'signing certificate'),
                               ({'package': 'org.example.other'}, 'package name'),
                               ({'version': VERSION + 1}, 'newer'),
                               ({'version': 0}, 'invalid version')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(OSError, reason):
                self.installed_fields = change
                self.install()
        self.bound.shell.side_effect = [PACKAGE_LIST, b'package:/system/app/stopscroll/base.apk\n']
        with self.assertRaisesRegex(OSError, 'base.apk'):
            self.install()
        self.bound.stream_to_shell.assert_not_called()

    def test_uses_private_snapshot_if_local_output_is_replaced_during_preflight(self):
        def swap(*args):
            self.apk.write_bytes(b'changed after verification')
            return self.shell(*args)

        self.bound.shell.side_effect = swap
        self.assertEqual(self.install()['version_code'], VERSION)
        self.assertEqual(self.installs, [APK])

    def test_failed_commit_is_indeterminate_and_never_retried(self):
        self.ok_output = b'Failure [INSTALL_FAILED_USER_RESTRICTED]\n'
        with self.assertRaisesRegex(OSError, 'doctor.*before retrying'):
            self.install()
        self.bound.stream_to_shell.assert_called_once()
        self.transport.bind.assert_called_once()
        self.assertEqual(self.installed_bytes, OLD_APK)

    def test_claimed_success_requires_post_install_apk_verification(self):
        self.bound.stream_to_shell.side_effect = None
        self.bound.stream_to_shell.return_value = b'Success\n'
        with self.assertRaisesRegex(OSError, 'apk_sha256'):
            self.install()
        self.bound.stream_to_shell.assert_called_once()
        self.transport.bind.assert_called_once()


class InstallTransportFixtureTests(unittest.TestCase):
    def test_same_raw_shell_checks_serial_and_user_before_binary_stdin(self):
        identity = 'approved'
        users = 'UserInfo{0:Owner:13} running'
        consumed = []

        def runner(arguments, **options):
            if arguments == ['adb', 'devices', '-l']:
                return completed(b'List of devices attached\napproved device usb:1-4 transport_id:9\n')
            self.assertEqual(arguments[:-1], ['adb', '-s', 'usb:1-4', 'shell', '-T', '-e', 'none'])
            command = shlex.split(arguments[-1])
            self.assertEqual(command[:2], ['sh', '-c'])
            self.assertNotIn('input', options)
            functions = (
                f'getprop() {{ printf "%s" {shlex.quote(identity)}; }}\n'
                f'pm() {{ read unexpected || :; printf "%s\\n" {shlex.quote(users)}; }}\n'
            )
            result = subprocess.run(['sh', '-c', functions + command[2]], stdin=options['stdin'],
                                    capture_output=True, check=False)
            consumed.append(result.stdout)
            return result

        transport = AndroidTransport(runner=Mock(side_effect=runner))
        bound = transport.bind()
        with tempfile.TemporaryFile() as stream:
            stream.write(APK)
            stream.seek(0)
            self.assertEqual(bound.stream_to_shell(stream, 'cat'), APK)
            for identity, users, error in [('replacement', users, 'Серийный'),
                                           ('approved', 'UserInfo{10:Work:30}', '0')]:
                stream.seek(0)
                with self.subTest(identity=identity), self.assertRaisesRegex(AndroidTransportError, error):
                    bound.stream_to_shell(stream, 'cat')
        self.assertEqual(consumed, [APK, b'', b''])
        self.assertEqual(transport._runner.call_count, 4)
