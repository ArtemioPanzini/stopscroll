"""Exercise the installer's actual validation without installing or running an app."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TelegramInstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binary = Path(self.directory.name) / 'Telegram'
        self.binary.write_bytes(b'compiled-client-fixture')
        self.manifest = self.binary.with_suffix('.json')
        self.valid = dict(version=1, configuration='Debug', auto_update=False,
                          test_credentials=False,
                          binary_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest())
        script = (ROOT / 'telegram/install.sh').read_text()
        self.validation = script.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]

    def validate(self, data):
        self.manifest.write_text(json.dumps(data))
        return subprocess.run([sys.executable, '-c', self.validation,
                               str(self.manifest), str(self.binary)],
                              text=True, capture_output=True)

    def test_accepts_only_matching_successful_working_build(self):
        self.assertEqual(self.validate(self.valid).returncode, 0)
        for update in (dict(version=True), dict(configuration='Release'),
                       dict(auto_update=True), dict(auto_update=0),
                       dict(test_credentials=True), dict(test_credentials=0),
                       dict(binary_sha256='0' * 64)):
            with self.subTest(update=update):
                self.assertNotEqual(self.validate(dict(self.valid, **update)).returncode, 0)
        self.binary.write_bytes(b'old-test-binary')
        self.assertNotEqual(self.validate(self.valid).returncode, 0)

    def test_missing_success_manifest_fails_before_installation(self):
        result = subprocess.run([sys.executable, '-c', self.validation,
                                 str(self.manifest), str(self.binary)],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('подтверждения успешной сборки', result.stderr)
