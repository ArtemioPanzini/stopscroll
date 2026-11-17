"""Isolated build-helper checks; no private credentials or Docker required."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('telegram_build', Path(__file__).parents[1] / 'build.py')
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class BuildHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'tdesktop/out'
        (self.output / 'bin').mkdir(parents=True)
        self.binary = self.output / 'bin/Telegram'
        self.binary.write_bytes(b'dummy binary')

    def test_manifest_binds_binary_and_has_no_credentials(self):
        BUILD.write_build_manifest(self.output, False)
        manifest = self.output / 'stopscroll-build.json'
        data = json.loads(manifest.read_text())
        self.assertEqual(data, dict(version=1, configuration='Debug', auto_update=False,
                                  test_credentials=False, binary_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest()))
        self.assertIs(data['test_credentials'], False)
        self.assertIs(data['auto_update'], False)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        self.binary.write_bytes(b'new binary')
        self.assertNotEqual(data['binary_sha256'], hashlib.sha256(self.binary.read_bytes()).hexdigest())

    def test_test_classification(self):
        own = dict(api_id=123, api_hash='a' * 32)
        self.assertIs(BUILD.uses_test_credentials(own), False)
        self.assertIs(BUILD.uses_test_credentials(own, True), True)
        self.assertIs(BUILD.uses_test_credentials(dict(own, api_id=BUILD.TEST_API_ID)), True)
        self.assertIs(BUILD.uses_test_credentials(dict(own, api_hash=BUILD.TEST_API_HASH.upper())), True)
        for value in (0, 1, 'false', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BUILD.write_build_manifest(self.output, value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                BUILD.uses_test_credentials(own, value)

    def test_reconfigure_and_failure_invalidate(self):
        BUILD.write_build_manifest(self.output, False)
        with patch.object(BUILD, 'ROOT', Path(self.tmp.name)), patch.object(sys, 'argv', ['build.py', '--configure-only']):
            with self.assertRaisesRegex(RuntimeError, 'Missing source checkout'):
                BUILD.main()
        self.assertFalse((self.output / 'stopscroll-build.json').exists())
        BUILD.write_build_manifest(self.output, True)
        BUILD.invalidate_build_manifest(self.output)
        self.binary.unlink()
        with self.assertRaises(FileNotFoundError):
            BUILD.write_build_manifest(self.output, False)
        self.assertFalse((self.output / 'stopscroll-build.json').exists())

    def test_full_build_failure_cannot_keep_old_manifest(self):
        root = Path(self.tmp.name)
        (root / 'tdesktop/.git').mkdir()
        version = root / 'tdesktop/Telegram/build/version'
        version.parent.mkdir(parents=True)
        version.write_text(f'AppVersion {BUILD.APP_VERSION}\n')
        credentials = root / 'api.json'
        credentials.write_text(json.dumps(dict(api_id=123, api_hash='a' * 32)))
        credentials.chmod(0o600)
        BUILD.write_build_manifest(self.output, True)
        def container(command, log, **kwargs):
            if log == 'build.log':
                raise RuntimeError('simulated compiler failure')
        with patch.object(BUILD, 'ROOT', root), patch.object(BUILD, 'run'), patch.object(BUILD, 'container', side_effect=container), patch.object(BUILD.subprocess, 'check_output', return_value=BUILD.BASE + '\n'), patch.object(BUILD.subprocess, 'run', return_value=Mock(returncode=0)), patch.object(sys, 'argv', ['build.py', '--api-file', str(credentials)]):
            with self.assertRaisesRegex(RuntimeError, 'simulated compiler failure'):
                BUILD.main()
        self.assertFalse((self.output / 'stopscroll-build.json').exists())

    def test_source_pin_ancestry_and_actual_version(self):
        root = Path(self.tmp.name)
        (root / 'tdesktop/.git').mkdir()
        version = root / 'tdesktop/Telegram/build/version'
        version.parent.mkdir(parents=True)
        version.write_text(f'AppVersion {BUILD.APP_VERSION}\n')
        with patch.object(BUILD, 'ROOT', root), patch.object(BUILD.subprocess, 'check_output', return_value=BUILD.BASE + '\n') as tag, patch.object(BUILD.subprocess, 'run', return_value=Mock(returncode=0)) as ancestry:
            BUILD.validate_source()
            tag.assert_called_once_with(['git', '-C', str(root / 'tdesktop'), 'rev-parse', BUILD.TAG], text=True)
            self.assertIn('--is-ancestor', ancestry.call_args.args[0])
            ancestry.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, 'does not descend'):
                BUILD.validate_source()
            ancestry.return_value.returncode = 0
            version.write_text('AppVersion 7000006\n')
            with self.assertRaisesRegex(RuntimeError, 'Source AppVersion'):
                BUILD.validate_source()
            tag.return_value = 'wrongtag\n'
            with self.assertRaisesRegex(RuntimeError, 'Unexpected upstream base'):
                BUILD.validate_source()

    def test_inherited_environment_redaction(self):
        root = Path(self.tmp.name)
        with patch.object(BUILD, 'ROOT', root), patch.dict(os.environ, TDESKTOP_API_ID='12345678', TDESKTOP_API_HASH='abcdeffedcba01234567890123456789ab'):
            BUILD.run([sys.executable, '-c', 'import os; print(os.environ["TDESKTOP_API_ID"], os.environ["TDESKTOP_API_HASH"])'], 'inherited.log')
        self.assertEqual((root / 'inherited.log').read_text(), '[REDACTED] [REDACTED]\n')

    def test_log_permissions_and_redaction(self):
        root = Path(self.tmp.name)
        log = root / 'test.log'
        log.write_text('old'); log.chmod(0o644)
        env = dict(os.environ, TDESKTOP_API_ID='12345678', TDESKTOP_API_HASH='abcdeffedcba01234567890123456789ab')
        with patch.object(BUILD, 'ROOT', root):
            BUILD.run([sys.executable, '-c', 'import os; print(os.environ["TDESKTOP_API_ID"], os.environ["TDESKTOP_API_HASH"])'], 'test.log', env)
        self.assertEqual(log.read_text(), '[REDACTED] [REDACTED]\n')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
