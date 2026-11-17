import base64
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from stopscroll.browser import (browser_policy, publish_policy, read_policy,
                                send_message, native_host, register, check_conflicts, sign_policy, PACKAGE)
from stopscroll.model import Session
from stopscroll.seed import initial_state
from stopscroll.system import Runner


class BrowserPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.keys.cleanup)
        cls.key = Path(cls.keys.name) / 'key.pem'
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(cls.key)],check=True,capture_output=True)

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
        publish_policy(self.state, 1000, self.path, self.key)
        first_inode = self.path.stat().st_ino
        self.state.sessions['youtube'] = Session(1000, 1060)
        publish_policy(self.state, 1001, self.path, self.key)
        self.assertNotEqual(first_inode, self.path.stat().st_ino)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(base64.b64decode(read_policy(self.path, 1002)['payload']))['allowed_until'], 1060)
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
            data = dict(browser_policy(self.state, 1000), sequence='1')
            data[key] = value
            payload = base64.b64encode(json.dumps(data).encode()).decode()
            self.path.write_text(json.dumps({'payload':payload,'signature':base64.b64encode(b'x'*256).decode()}))
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
        publish_policy(self.state, 1000, self.path, self.key)
        output = io.BytesIO()
        with patch('stopscroll.browser.time.time', return_value=1001), \
                patch('stopscroll.browser.select.select', return_value=([object()], [], [])):
            self.assertEqual(native_host(io.BytesIO(b'{"cmd":"open"}'), output, self.path), 0)
        envelope = json.loads(output.getvalue()[4:])
        self.assertEqual(json.loads(base64.b64decode(envelope['payload']))['allowed_until'], 0)
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

    def test_signature_authenticates_exact_payload_and_rejects_changes(self):
        value = sign_policy(browser_policy(self.state, 1000), self.key)
        public = Path(self.temp.name) / 'public.pem'
        public.write_bytes(subprocess.check_output(['openssl','pkey','-in',str(self.key),'-pubout']))
        signature = Path(self.temp.name) / 'signature'
        signature.write_bytes(base64.b64decode(value['signature']))
        payload = base64.b64decode(value['payload'])
        command = ['openssl','dgst','-sha256','-verify',str(public),'-signature',str(signature)]
        self.assertEqual(subprocess.run(command,input=payload,capture_output=True).returncode,0)
        self.assertNotEqual(subprocess.run(command,input=payload+b' ',capture_output=True).returncode,0)

    def test_pause_and_session_use_later_deadline_on_both_overlap_orders(self):
        self.state.sessions['youtube'] = Session(1000,1060)
        self.state.pause_until = 1030
        self.assertEqual(browser_policy(self.state,1029)['allowed_until'],1060)
        self.assertEqual(browser_policy(self.state,1031)['allowed_until'],1060)
        self.state.pause_until = 1090
        self.assertEqual(browser_policy(self.state,1061)['allowed_until'],1090)

    def test_competing_extension_settings_rejected_before_own_files_change(self):
        runner = Runner(self.temp.name)
        conflict = runner.path('/etc/opt/chrome/policies/managed/existing.json')
        conflict.parent.mkdir(parents=True)
        value = {'ExtensionSettings':{'b'*32:{'installation_mode':'force_installed'}}}
        conflict.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'ExtensionSettings'):
            check_conflicts(runner)
        with self.assertRaises(ValueError):
            register(runner)
        self.assertEqual(json.loads(conflict.read_text()),value)
        self.assertFalse(conflict.with_name('stopscroll-youtube.json').exists())

    def test_all_policy_filenames_and_ambiguous_chromium_json_rejected(self):
        for name, text in [('extensionless','{"ExtensionSettings":{}}'),
                           ('commented','// comment\n{"ExtensionSettings":{}}'),
                           ('trailing.conf','{"ExtensionSettings":{},}'),
                           ('duplicate','{"A":1,"A":2}')]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                runner=Runner(directory)
                policy=runner.path('/etc/opt/chrome/policies/managed/'+name)
                policy.parent.mkdir(parents=True)
                policy.write_text(text)
                with self.assertRaises(ValueError): check_conflicts(runner)
                self.assertEqual(policy.read_text(),text)
                self.assertFalse(policy.with_name('stopscroll-youtube.json').exists())

    def test_unreadable_policy_is_not_silently_ignored(self):
        runner=Runner(self.temp.name)
        policy=runner.path('/etc/opt/chrome/policies/managed/locked')
        policy.parent.mkdir(parents=True)
        policy.write_text('{}')
        with patch.object(Path,'read_text',side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(ValueError,'locked'): check_conflicts(runner)

    def test_actual_tick_handle_same_now_gets_distinct_ordered_signed_revisions(self):
        from unittest.mock import Mock
        from stopscroll.daemon import Daemon
        from stopscroll.engine import Engine
        captured=[]
        def browser_sync(state,now):
            publish_policy(state,now,self.path,self.key)
            value=json.loads(self.path.read_text())
            captured.append(json.loads(base64.b64decode(value['payload'])))
            return []
        system=Mock()
        system.sync_browser.side_effect=browser_sync
        system.sync.side_effect=lambda state,now: browser_sync(state,now)
        system.retry_quarantine.return_value=[]
        system.telegram_policy_dirty=False
        daemon=Daemon(Engine(self.state),system,Mock(),lambda:[])
        now=1794916800
        self.assertTrue(daemon.handle({'cmd':'open','site':'youtube','minutes':1},0,now)['ok'])
        captured.clear()
        daemon.tick(now,100,'boot')
        self.assertTrue(daemon.handle({'cmd':'open','site':'youtube','minutes':1},0,now)['ok'])
        self.assertGreaterEqual(len(captured),2)
        self.assertEqual(len({item['issued_at'] for item in captured}),1)
        sequences=[int(item['sequence']) for item in captured]
        self.assertTrue(all(a<b for a,b in zip(sequences,sequences[1:])))
        self.assertGreater(captured[-1]['allowed_until'],captured[0]['allowed_until'])
