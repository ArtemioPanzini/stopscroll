import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from stopscroll.browser import (browser_policy, publish_policy, read_policy,
                                send_message, native_host, register, PACKAGE)
from stopscroll.model import Session
from stopscroll.seed import initial_state
from stopscroll.system import Runner


class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        self.state = initial_state(1000, '2026-11-17')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'browser.json'

    def test_closed_open_expiry_pause_and_blocked_mode(self):
        self.assertEqual(browser_policy(self.state, 1000)['allowed_until'], 0)
        self.state.sessions['youtube'] = Session(1000, 1060)
        self.assertEqual(browser_policy(self.state, 1001)['allowed_until'], 1060)
        self.assertLessEqual(browser_policy(self.state, 1061)['allowed_until'], 1061)
        self.state.sites['youtube'].mode = 'blocked'
        self.assertEqual(browser_policy(self.state, 1001)['allowed_until'], 0)
        self.state.pause_until = 1030
        self.assertEqual(browser_policy(self.state, 1001)['allowed_until'], 1030)

    def test_policy_contains_only_youtube_not_budgets_channels_or_owner(self):
        data = browser_policy(self.state, 1000)
        self.assertEqual(data['exceptions'], ['accounts.youtube.com'])
        text = json.dumps(data)
        for private in ['owner_uid', 'budget_min', 'channels', 'x.com', 'used_sec']:
            self.assertNotIn(private, text)

    def test_atomic_replacement_public_mode_and_short_lease(self):
        publish_policy(self.state, 1000, self.path)
        first_inode = self.path.stat().st_ino
        self.state.sessions['youtube'] = Session(1000, 1060)
        publish_policy(self.state, 1001, self.path)
        self.assertNotEqual(first_inode, self.path.stat().st_ino)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(read_policy(self.path, 1002)['allowed_until'], 1060)
        self.assertIsNone(read_policy(self.path, 1005))
        self.assertIsNone(read_policy(self.path, 990))
        self.assertEqual(list(self.path.parent.glob('.browser-*')), [])

    def test_missing_malformed_and_invalid_data_fail_closed(self):
        self.assertIsNone(read_policy(self.path, 1000))
        for raw in ['null', '{}', '[1]', 'not json']:
            self.path.write_text(raw)
            self.assertIsNone(read_policy(self.path, 1000))
        for key, value in [('version', True), ('lease_until', float('nan')),
                           ('issued_at', 1009), ('lease_until', 2000),
                           ('domains', ['https://youtube.com']),
                           ('domains', ['bad domain']), ('exceptions', 'accounts.youtube.com')]:
            data = browser_policy(self.state, 1000)
            data[key] = value
            self.path.write_text(json.dumps(data))
            self.assertIsNone(read_policy(self.path, 1001), (key, value))

    def test_normal_delayed_group_removal_is_respected(self):
        del self.state.sites['youtube']
        data = browser_policy(self.state, 1000)
        self.assertEqual(data['domains'], [])
        self.assertGreater(data['allowed_until'], 1000)

    def test_native_message_frame_and_host_rejects_all_input(self):
        output = io.BytesIO()
        send_message(output, {'version': 1})
        raw = output.getvalue()
        self.assertEqual(struct.unpack('=I', raw[:4])[0], len(raw)-4)
        self.assertEqual(json.loads(raw[4:]), {'version': 1})
        publish_policy(self.state, 1000, self.path)
        output = io.BytesIO()
        with patch('stopscroll.browser.time.time', return_value=1001), \
                patch('stopscroll.browser.select.select', return_value=([object()], [], [])):
            self.assertEqual(native_host(io.BytesIO(b'{"cmd":"open"}'), output, self.path), 0)
        self.assertEqual(json.loads(output.getvalue()[4:])['allowed_until'], 0)
        self.assertFalse(self.state.sessions)

    def test_registration_repairs_own_files_without_touching_other_policies(self):
        runner = Runner(self.temp.name)
        package = runner.path(PACKAGE)
        package.parent.mkdir(parents=True)
        package.write_text(json.dumps({'id': 'a'*32, 'version': '1.0.0'}))
        other = runner.path('/etc/opt/chrome/policies/managed/other.json')
        other.parent.mkdir(parents=True)
        original = '{"ExtensionInstallForcelist":["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb;https://example.invalid/update.xml"]}'
        other.write_text(original)
        self.assertEqual(len(register(runner)), 4)
        self.assertEqual(register(runner), [])
        policy = other.with_name('stopscroll-youtube.json')
        self.assertIn('file:///usr/local/lib/stopscroll/browser/updates.xml', policy.read_text())
        self.assertEqual(other.read_text(), original)
        value = json.loads(policy.read_text())
        self.assertEqual(set(value), {'ExtensionSettings'})
        self.assertEqual(set(value['ExtensionSettings']), {'a'*32})
        host = runner.path('/etc/opt/chrome/native-messaging-hosts/org.stopscroll.youtube.json')
        self.assertEqual(json.loads(host.read_text())['allowed_origins'], ['chrome-extension://'+'a'*32+'/'])
        host.unlink()
        self.assertEqual(len(register(runner)), 1)

    def test_invalid_package_id_cannot_enter_managed_paths(self):
        runner = Runner(self.temp.name)
        package = runner.path(PACKAGE)
        package.parent.mkdir(parents=True)
        package.write_text('{"id":"../bad"}')
        with self.assertRaises(ValueError):
            register(runner)
