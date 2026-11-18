import base64
import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from stopscroll import android_cli, cli
from stopscroll.android_policy import canonical_policy_bytes, policy_hash
from stopscroll.android_transport import AndroidTimeoutError, AndroidTransport, AndroidTransportError
from tests.test_android_policy import sample_policy
from tests.test_android_transport import DEVICES_HEADER, USB, USERS, bundle, completed, execute_guard, script_action

REVISION = 7
STAMP = datetime(2026, 11, 18, 12, tzinfo=timezone.utc).timestamp()
PACKAGE = 'com.celzero.bravedns.stopscroll'
CERTIFICATE = 'a' * 64


def status_data(policy, *, active=True, open_site=True):
    return {
        'policy_hash': policy_hash(policy), 'timezone': 'UTC', 'day_id': '2026-11-18',
        'reset_hour': 4, 'default_session_min': policy['default_session_min'],
        'clock': {'wall_ms': int(STAMP * 1000), 'elapsed_ms': 120_000, 'boot_count': 3},
        'protection': {
            'state': 'active' if active else 'unprotected', 'tunnel': active, 'always_on': active,
            'lockdown': active, 'ignoring_battery_optimizations': active,
            'preferred_state': 'ready' if active else 'failed', 'reason': None if active else 'Нет защиты',
        },
        'sites': {name: {
            'mode': site['mode'], 'budget_min': site['budget_min'],
            'remaining_sec': site['budget_min'] * 60, 'reserved_sec': 180 if name == 'video' and open_site else 0,
            'session': {'start_elapsed_ms': 10_000, 'deadline_elapsed_ms': 300_000}
            if name == 'video' and open_site else None,
        } for name, site in policy['sites'].items()},
        'elapsed_cutoff_ms': 0,
    }


def receipt(apk=b'signed test fixture'):
    return {'schema': 1, 'package': PACKAGE, 'certificate_sha256': CERTIFICATE,
            'version_code': 7, 'apk_sha256': hashlib.sha256(apk).hexdigest()}


class AndroidCLITests(unittest.TestCase):
    def setUp(self):
        self.policy = sample_policy()
        self.desktop = Mock()
        self.bound = Mock()
        self.bound.call.side_effect = self.reply
        self.transport = Mock()
        self.transport.bind.return_value = self.bound
        self.factory = Mock(return_value=self.transport)
        self.confirm = Mock(return_value=True)
        self.inspect = Mock(return_value=receipt())
        self.app_error = None
        self.apply_receipt = REVISION + 1
        self.status = status_data(self.policy)

    def reply(self, request):
        op, args = request['op'], request['args']
        if op in ('policy.replace', 'session.open', 'session.close') and self.app_error:
            return {'ok': False, 'revision': REVISION + 1, 'applied_revision': REVISION, **self.app_error}
        if op == 'status':
            return {'ok': True, 'revision': REVISION, 'applied_revision': REVISION, 'data': self.status}
        if op == 'policy.get':
            return {'ok': True, 'revision': REVISION, 'applied_revision': REVISION,
                    'data': {'policy': self.policy, 'policy_hash': policy_hash(self.policy)}}
        if op == 'policy.replace':
            data = {'changed': True, 'policy_hash': policy_hash(args['policy'])}
        elif op == 'session.open':
            data = {'site': args['site'], 'granted_sec': 180, 'remaining_sec': 180,
                    'deadline_elapsed_ms': 300_000}
        else:
            data = {'site': args['site'], 'closed': False, 'remaining_sec': 135}
        return {'ok': True, 'revision': REVISION + 1, 'applied_revision': self.apply_receipt, 'data': data}

    def invoke(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = android_cli.main(argv, exchange=self.desktop, transport_factory=self.factory,
                                    confirm=self.confirm, inspect_release=self.inspect, now=lambda: STAMP)
        return code, out.getvalue(), err.getvalue()

    def requests(self):
        return [entry.args[0] for entry in self.bound.call.call_args_list]

    def test_early_desktop_dispatch_and_dependency_free_help(self):
        with patch.object(cli, 'exchange') as exchange, patch.object(android_cli, 'main', return_value=0) as main:
            self.assertEqual(cli.main(['android', 'status']), 0)
        exchange.assert_not_called()
        main.assert_called_once_with(['status'], exchange=exchange)
        code, stdout, stderr = self.invoke('--help')
        self.assertEqual(code, 0)
        self.assertIn('import-desktop', stdout)
        self.assertFalse(stderr)
        self.factory.assert_not_called()
        self.desktop.assert_not_called()

    def test_invalid_arguments_are_rejected_before_dependencies(self):
        for argv in (('open', 'video', '0'), ('limit', 'video', '1441'), ('open', 'UPPER'),
                     ('block', 'news', 'https://127.1/path'), ('export',), ('status', '--yes'),
                     ('nonsense',), ('limit', 'video', '30', '--y'), ('import-desktop', '--usb-serial', '--yes')):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(*argv)[0], 2)
        self.factory.assert_not_called()
        self.desktop.assert_not_called()

    def test_status_lists_budget_and_reports_unprotected_as_successful_read(self):
        self.status = status_data(self.policy, active=False)
        code, stdout, _ = self.invoke('--usb-serial', 'phone1', 'status')
        self.assertEqual(code, 0)
        self.assertIn('unprotected', stdout)
        self.assertIn('video', stdout)
        self.assertIn('180', stdout)
        self.assertIn('UTC', stdout)
        self.factory.assert_called_once_with(usb_serial='phone1')
        self.assertEqual(self.requests()[0]['op'], 'status')

    def test_list_outputs_phone_configuration(self):
        code, stdout, _ = self.invoke('list', '--usb-serial', 'phone1')
        self.assertEqual(code, 0)
        self.assertIn('video.example.org', stdout)
        self.assertIn('account.video.example.org', stdout)
        self.assertIn('45', stdout)
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get'])

    def test_export_creates_private_canonical_file_and_keeps_existing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'phone.json'
            self.assertEqual(self.invoke('export', '--output', str(path))[0], 0)
            self.assertEqual(path.read_bytes(), canonical_policy_bytes(self.policy) + b'\n')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_text('existing')
            self.assertEqual(self.invoke('export', '--output', str(path))[0], 1)
            self.assertEqual(path.read_text(), 'existing')

    def test_apply_rejects_duplicate_keys_and_legacy_values_before_adb(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text('{"schema":1,"schema":1}')
            self.assertEqual(self.invoke('apply', str(path), '--yes')[0], 2)
            invalid = deepcopy(self.policy)
            invalid['sites']['news']['domains'] = ['WWW.example.org']
            path.write_text(json.dumps(invalid))
            self.assertEqual(self.invoke('apply', str(path), '--yes')[0], 2)
        self.factory.assert_not_called()

    def test_apply_refuses_nonregular_file_without_opening_adb(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pipe'
            os.mkfifo(path)
            self.assertEqual(self.invoke('apply', str(path), '--yes')[0], 2)
        self.factory.assert_not_called()

    def test_apply_previews_and_sends_full_replacement(self):
        target = deepcopy(self.policy)
        target['default_session_min'] = 20
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(target, indent=2))
            code, stdout, _ = self.invoke('apply', str(path))
        self.assertEqual(code, 0)
        self.assertIn('default_session_min', stdout)
        self.assertIn('video', stdout)
        self.confirm.assert_called_once()
        self.transport.bind.assert_called_once_with()
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get', 'status', 'policy.replace'])
        request = self.requests()[-1]
        self.assertEqual(request['expected_revision'], REVISION)
        self.assertEqual(request['args'], {'policy': target})
        self.assertEqual(request['schema'], 1)
        self.assertEqual(str(UUID(request['request_id'])), request['request_id'])
        self.assertEqual(len({r['request_id'] for r in self.requests()}), 3)

    def test_import_desktop_preview_replaces_without_writing_and_yes_skips_confirmation(self):
        target = {'schema': 1, 'reset_hour': 4, 'default_session_min': 15, 'sites': {
            'other': {'mode': 'blocked', 'budget_min': 0, 'domains': ['other.example.org'], 'exceptions': []}
        }}
        self.desktop.return_value = {'ok': True, 'policy': target}
        code, stdout, _ = self.invoke('import-desktop', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('sites.other', stdout)
        self.assertIn('sites.video', stdout)
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get', 'status'])
        self.desktop.assert_called_once_with({'cmd': 'export-sites'})
        self.bound.call.reset_mock()
        self.assertEqual(self.invoke('import-desktop', '--yes')[0], 0)
        self.assertEqual(self.requests()[-1]['args']['policy'], target)
        self.confirm.assert_not_called()

    def test_desktop_import_refusal_and_legacy_payload(self):
        self.desktop.return_value = {'ok': False, 'error': 'no owner access'}
        self.assertEqual(self.invoke('import-desktop', '--yes')[0], 1)
        self.desktop.return_value = {'ok': True, 'policy': self.policy | {'spent_sec': {}}}
        self.assertEqual(self.invoke('import-desktop', '--dry-run')[0], 2)
        self.factory.assert_not_called()

    def test_invalid_usb_serial_is_rejected_before_desktop_export(self):
        self.desktop.side_effect = OSError('desktop socket unavailable')
        self.assertEqual(self.invoke('import-desktop', '--usb-serial=;', '--dry-run')[0], 2)
        self.desktop.assert_not_called()
        self.factory.assert_not_called()

    def test_apply_caps_raw_json_even_when_canonical_policy_would_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'oversized.json'
            path.write_bytes(b' ' * (1024 * 1024) + canonical_policy_bytes(self.policy))
            self.assertEqual(self.invoke('apply', str(path), '--yes')[0], 2)
        self.factory.assert_not_called()

    def test_identical_policy_is_no_op_with_open_session(self):
        self.desktop.return_value = {'ok': True, 'policy': deepcopy(self.policy)}
        self.assertEqual(self.invoke('import-desktop')[0], 0)
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get'])
        self.confirm.assert_not_called()

    def test_policy_transforms(self):
        cases = [
            (('block', 'blog', 'https://www.BLOG.example.org/page'), 'blog',
             {'mode': 'blocked', 'budget_min': 0, 'domains': ['blog.example.org'], 'exceptions': []}),
            (('block', 'video', 'https://www.new.example.org'), 'video',
             self.policy['sites']['video'] | {'domains': ['video.example.org', 'cdn.example.org', 'new.example.org']}),
            (('block', 'video'), 'video', self.policy['sites']['video'] | {'mode': 'blocked', 'budget_min': 0}),
            (('limit', 'news', '30'), 'news', self.policy['sites']['news'] | {'mode': 'limited', 'budget_min': 30}),
            (('limit', 'video', '32'), 'video', self.policy['sites']['video'] | {'budget_min': 32}),
            (('unblock', 'video', 'video.example.org'), 'video',
             self.policy['sites']['video'] | {'domains': ['cdn.example.org'], 'exceptions': []}),
            (('unblock', 'news', 'news.example.org'), 'news', None),
            (('unblock', 'video'), 'video', None),
        ]
        for argv, name, expected in cases:
            with self.subTest(argv=argv):
                self.bound.call.reset_mock()
                code, out, err = self.invoke(*argv, '--yes')
                self.assertEqual(code, 0, err)
                result = self.requests()[-1]['args']['policy']
                self.assertEqual(result['sites'].get(name), expected)
                if argv == ('unblock', 'video', 'video.example.org'):
                    self.assertIn('account.video.example.org', out)

    def test_cancel_conflict_transport_failure_and_saved_failure_are_never_retried(self):
        self.confirm.return_value = False
        self.assertEqual(self.invoke('limit', 'video', '30')[0], 2)
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get', 'status'])
        for error in ({'error': {'code': 'CONFLICT', 'message': 'revision changed'}},
                      {'error': {'code': 'APPLY_FAILED', 'message': 'apply error'}, 'saved': True}):
            self.bound.call.reset_mock()
            self.app_error = error
            code, _, stderr = self.invoke('limit', 'video', '30', '--yes')
            self.assertEqual(code, 1)
            self.assertIn(error['error']['code'], stderr)
            if error.get('saved'):
                self.assertIn('сохранено', stderr.lower())
                self.assertIn('7', stderr)
                self.assertIn('8', stderr)
            self.assertEqual([r['op'] for r in self.requests()], ['policy.get', 'status', 'policy.replace'])
        self.bound.call.reset_mock()
        self.bound.call.side_effect = [
            self.reply({'op': 'policy.get', 'args': {}}),
            self.reply({'op': 'status', 'args': {}}),
            AndroidTransportError('timeout'),
        ]
        code, _, stderr = self.invoke('limit', 'video', '30', '--yes')
        self.assertEqual(code, 3)
        self.assertIn('stopscroll android status', stderr)
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get', 'status', 'policy.replace'])

    def test_mutation_timeout_observes_current_status_once_without_retrying(self):
        current = self.reply({'op': 'status', 'args': {}})
        for observation in (current, AndroidTimeoutError('status timeout')):
            with self.subTest(observation=observation):
                self.bound.call.reset_mock()
                self.transport.bind.reset_mock()
                self.bound.call.side_effect = [current, AndroidTimeoutError('mutation timeout'), observation]
                code, stdout, stderr = self.invoke('open', 'video', '5')
                self.assertEqual(code, 3)
                self.assertIn('Результат изменения неизвестен', stderr)
                self.transport.bind.assert_called_once_with()
                self.assertEqual([r['op'] for r in self.requests()], ['status', 'session.open', 'status'])
                self.assertNotIn('expected_revision', self.requests()[-1])
                self.assertIn('stopscroll android status' if isinstance(observation, Exception)
                              else 'Текущее состояние телефона', stderr if isinstance(observation, Exception) else stdout)

    def test_preview_read_timeout_does_not_make_an_observation(self):
        self.bound.call.side_effect = AndroidTimeoutError('preview timeout')
        self.assertEqual(self.invoke('limit', 'video', '30', '--yes')[0], 3)
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get'])

    def test_preview_cannot_apply_to_replacement_with_reused_id_after_server_restart(self):
        calls = []
        mutations = []
        restarted = False
        replacement = USB.replace(b'usb123', b'usb456')

        def run(argv, **kwargs):
            calls.append(argv)
            if argv == ['adb', 'devices', '-l']:
                return completed(DEVICES_HEADER + (replacement if restarted else USB))
            self.assertIn(argv[:3], (['adb', '-t', '1'], ['adb', '-s', 'usb:1-2']))
            if argv[3:] == ['shell', 'pm', 'list', 'users']:
                return completed(USERS)
            script = kwargs['input']
            _, action = script_action(script)
            self.assertEqual(action[:3], ['exec', 'content', 'call'])
            guard_exit = execute_guard(script, serial='usb456' if restarted else 'usb123')
            if guard_exit:
                return completed(b'', returncode=guard_exit)
            request = json.loads(base64.b64decode(action[-1]))
            if request['op'] == 'policy.replace':
                mutations.append((restarted, request))
            reply = self.reply(request) | {'schema': 1, 'request_id': request['request_id']}
            return completed(bundle(reply))

        def confirm():
            nonlocal restarted
            restarted = True
            return True

        self.factory.side_effect = lambda usb_serial: AndroidTransport(usb_serial, runner=run)
        self.confirm.side_effect = confirm
        self.assertEqual(self.invoke('limit', 'video', '30')[0], 3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls.count(['adb', 'devices', '-l']), 1)
        self.assertEqual(calls[-1][:3], ['adb', '-s', 'usb:1-2'])
        self.assertEqual(mutations, [])

    def test_open_close_use_revision_and_render_actual_result(self):
        code, stdout, _ = self.invoke('open', 'video', '10')
        self.assertEqual(code, 0)
        self.assertIn('180', stdout)
        self.assertEqual(self.requests()[-1]['args'], {'site': 'video', 'minutes': 10})
        self.assertEqual(self.requests()[-1]['expected_revision'], REVISION)
        self.bound.call.reset_mock()
        self.assertEqual(self.invoke('open', 'video')[0], 0)
        self.assertIsNone(self.requests()[-1]['args']['minutes'])
        self.bound.call.reset_mock()
        code, stdout, _ = self.invoke('close', 'video')
        self.assertEqual(code, 0)
        self.assertEqual(self.requests()[-1]['args'], {'site': 'video'})
        self.assertIn('135', stdout)
        self.confirm.assert_not_called()

    def test_invalid_payload_and_unacknowledged_mutation_are_not_success(self):
        self.status['sites']['video']['remaining_sec'] = True
        self.assertEqual(self.invoke('status')[0], 3)
        self.status = status_data(self.policy)
        self.apply_receipt = None
        code, _, stderr = self.invoke('open', 'video', '4')
        self.assertEqual(code, 1)
        self.assertIn('применение', stderr.lower())

    def test_wrong_hash_and_inconsistent_active_state_fail_as_protocol_errors(self):
        self.status['policy_hash'] = 'b' * 64
        self.assertEqual(self.invoke('limit', 'video', '30', '--yes')[0], 3)
        self.assertEqual([r['op'] for r in self.requests()], ['policy.get', 'status'])
        self.status = status_data(self.policy)
        self.status['protection']['lockdown'] = False
        self.assertEqual(self.invoke('status')[0], 3)

    def test_mutation_receipts_must_match_the_preview_and_sent_revision(self):
        cases = [
            (('limit', 'video', '30', '--yes'), {'data': {'changed': False}}, 3),
            (('limit', 'video', '30', '--yes'), {'revision': REVISION, 'applied_revision': REVISION}, 3),
            (('limit', 'video', '30', '--yes'), {'applied_revision': None}, 1),
            (('open', 'video', '4'), {'revision': 0, 'applied_revision': 0}, 3),
            (('open', 'video', '4'), {'revision': REVISION, 'applied_revision': REVISION}, 3),
            (('open', 'video', '4'), {'applied_revision': REVISION + 2}, 3),
            (('close', 'video'), {'revision': REVISION, 'applied_revision': REVISION, 'data': {'closed': True}}, 3),
            (('close', 'video'), {'revision': REVISION, 'applied_revision': REVISION}, 0),
            (('close', 'video'), {'revision': REVISION + 2, 'applied_revision': None}, 0),
        ]
        for argv, override, expected in cases:
            with self.subTest(argv=argv, override=override):
                def reply(request):
                    result = self.reply(request)
                    if 'expected_revision' in request:
                        result = (result | override) | {'data': result['data'] | override.get('data', {})}
                    return result

                self.bound.call.side_effect = reply
                self.assertEqual(self.invoke(*argv)[0], expected)

    def test_doctor_reports_local_checks_and_network_acceptance_boundary(self):
        code, stdout, stderr = self.invoke('doctor')
        self.assertEqual(code, 0, stderr)
        self.assertIn(PACKAGE, stdout)
        self.assertIn(CERTIFICATE, stdout)
        self.assertIn('Сетевая приёмка', stdout)
        self.assertIn('другого UID', stdout)
        self.inspect.assert_called_once_with(self.bound)
        self.assertEqual([r['op'] for r in self.requests()], ['status', 'policy.get'])

    def test_doctor_fails_if_latest_policy_reply_loses_applied_acknowledgement(self):
        def reply(request):
            result = self.reply(request)
            return result | {'applied_revision': None} if request['op'] == 'policy.get' else result

        self.bound.call.side_effect = reply
        code, stdout, _ = self.invoke('doctor')
        self.assertEqual(code, 1)
        self.assertIn('применённая нет', stdout)

    def test_doctor_fails_unprotected_clock_or_unverified_package(self):
        self.status = status_data(self.policy, active=False)
        self.status['clock']['wall_ms'] -= 1_000_000
        self.inspect.side_effect = OSError('SDK missing')
        code, stdout, _ = self.invoke('doctor')
        self.assertEqual(code, 1)
        for detail in ('unprotected', 'SDK missing', 'Сетевая приёмка'):
            self.assertIn(detail, stdout)

    def test_pinned_future_budget_day_is_readable_and_doctor_reports_discrepancy(self):
        self.status['day_id'] = '2026-11-19'
        self.assertEqual(self.invoke('status')[0], 0)
        code, stdout, _ = self.invoke('doctor')
        self.assertEqual(code, 1)
        self.assertIn('день по часам 2026-11-18, день бюджета 2026-11-19', stdout)

    def test_doctor_when_usb_is_unavailable_reports_failed_check(self):
        self.transport.bind.side_effect = AndroidTransportError('USB unavailable')
        code, stdout, _ = self.invoke('doctor')
        self.assertEqual(code, 1)
        self.assertIn('USB unavailable', stdout)
        self.assertIn('Сетевая приёмка', stdout)
        self.inspect.assert_not_called()


class InstalledReleaseTests(unittest.TestCase):
    def test_verify_pulled_base_apk_against_private_receipt(self):
        apk = b'signed test fixture'
        expected = receipt(apk)
        transport = Mock()
        transport.shell.return_value = b'package:/data/app/package/base.apk\n'
        paths = []

        def pull(source, target):
            self.assertEqual(source, '/data/app/package/base.apk')
            paths.append(target)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
            target.write_bytes(apk)

        transport.pull.side_effect = pull

        def run(argv, **kwargs):
            if argv[:3] == ['apksigner', 'verify', '--print-certs']:
                stdout = f'Signer #1 certificate SHA-256 digest: {CERTIFICATE}\n'
            elif argv[:3] == ['apkanalyzer', 'manifest', 'application-id']:
                stdout = PACKAGE + '\n'
            else:
                self.assertEqual(argv[:3], ['apkanalyzer', 'manifest', 'version-code'])
                stdout = '7\n'
            return subprocess.CompletedProcess(argv, 0, stdout=stdout.encode())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'release.json'
            path.write_text(json.dumps(expected))
            path.chmod(0o600)
            actual = android_cli.inspect_installed_release(
                transport, receipt_path=path, tool_finder=lambda tool: tool, runner=run,
            )
            self.assertEqual(actual, expected)
            transport.shell.assert_called_once_with('pm', 'path', '--user', '0', PACKAGE)
            self.assertFalse(paths[0].exists())
            for field, altered in (('certificate_sha256', 'b' * 64), ('version_code', 8), ('apk_sha256', 'c' * 64)):
                with self.subTest(field=field):
                    path.write_text(json.dumps(expected | {field: altered}))
                    sdk = Mock(side_effect=run)
                    with self.assertRaisesRegex(OSError, field):
                        android_cli.inspect_installed_release(
                            transport, receipt_path=path, tool_finder=lambda tool: tool, runner=sdk,
                        )
                    if field == 'apk_sha256':
                        sdk.assert_not_called()
            path.write_text(json.dumps(expected))
            transport.shell.reset_mock()
            with self.assertRaisesRegex(OSError, 'apksigner'):
                android_cli.inspect_installed_release(
                    transport, receipt_path=path, tool_finder=Mock(side_effect=OSError('missing apksigner')),
                    runner=run,
                )
            transport.shell.assert_not_called()
            transport.shell.return_value = b'package:/data/app/../../other/base.apk\n'
            transport.pull.reset_mock()
            with self.assertRaisesRegex(OSError, 'base.apk'):
                android_cli.inspect_installed_release(
                    transport, receipt_path=path, tool_finder=lambda tool: tool, runner=run,
                )
            transport.pull.assert_not_called()
            path.chmod(0o644)
            with self.assertRaisesRegex(OSError, '0600'):
                android_cli.inspect_installed_release(
                    transport, receipt_path=path, tool_finder=lambda tool: tool, runner=run,
                )
