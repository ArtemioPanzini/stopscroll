"""Host-only regressions for the manual acceptance helper's state guards."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from android.tests import device_acceptance as acceptance
from stopscroll.android_policy import policy_hash
from stopscroll.android_transport import AndroidTimeoutError


class FakePhone:
    def __init__(self):
        self.policy = {'schema': 1, 'reset_hour': 4, 'default_session_min': 15, 'sites': {
            'personal': {'mode': 'limited', 'budget_min': 45, 'domains': ['youtube.com'],
                         'exceptions': ['accounts.youtube.com']},
        }}
        self.revision = 7
        self.calls = []
        self.active = {}
        self.spent = {'personal': 92}
        self.elapsed = 50_000
        self.applied = True
        self.next_replace_failure = None
        self.open_storage_error = False
        self.charge_full_minute = False
        self.refund_twice = False
        self.session_receipts = {}

    def site_status(self, site, group):
        session = self.active.get(site)
        consumed = (max(0, self.elapsed - session['start_elapsed_ms']) + 999) // 1000 if session else 0
        return {'mode': group['mode'], 'budget_min': group['budget_min'],
                'remaining_sec': group['budget_min'] * 60 - self.spent.get(site, 0) - consumed,
                'reserved_sec': 60 - consumed if session else 0, 'session': session}

    def reply(self, request, *, data=None, error=None, **fields):
        outcome = {'error': {'code': error, 'message': 'Fixture response'}} if error else {'data': data}
        return {'schema': 1, 'request_id': request['request_id'], 'ok': error is None,
                'revision': self.revision, 'applied_revision': self.revision if self.applied else None,
                **outcome, **fields}

    def call(self, request):
        self.calls.append(deepcopy(request))
        op = request['op']
        if op == 'status':
            return self.reply(request, data={
                'policy_hash': policy_hash(self.policy), 'timezone': 'Europe/Belgrade', 'day_id': '2026-11-18',
                'reset_hour': 4, 'default_session_min': self.policy['default_session_min'],
                'clock': {'wall_ms': 1_795_014_000_000, 'elapsed_ms': self.elapsed, 'boot_count': 1},
                'protection': {'state': 'active' if self.applied else 'degraded', 'tunnel': True,
                               'always_on': True, 'lockdown': True, 'ignoring_battery_optimizations': True,
                               'preferred_state': 'ready', 'reason': None},
                'sites': {site: self.site_status(site, group) for site, group in self.policy['sites'].items()},
            })
        if op == 'policy.get':
            return self.reply(request, data={'policy': deepcopy(self.policy), 'policy_hash': policy_hash(self.policy)})
        if op in ('session.open', 'session.close'):
            return self.session(request)
        if op != 'policy.replace':
            raise AssertionError(f'Unexpected operation {op}')
        failure, self.next_replace_failure = self.next_replace_failure, None
        if failure == 'conflict' or request['expected_revision'] != self.revision:
            return self.reply(request, error='CONFLICT')
        self.policy = deepcopy(request['args']['policy'])
        self.active.clear()  # The real operation also closes every active session.
        self.revision += 1
        self.applied = failure != 'apply'
        if failure == 'apply':
            return self.reply(request, error='APPLY_FAILED', saved=True)
        if failure == 'storage':
            return self.reply(request, error='STORAGE_ERROR')
        if failure == 'lost':
            raise AndroidTimeoutError('Fixture reply was lost')
        return self.reply(request, data={'changed': True, 'policy_hash': policy_hash(self.policy)})

    def session(self, request):
        identifier = request['request_id']
        site = request['args']['site']
        if identifier in self.session_receipts:
            original, reply = self.session_receipts[identifier]
            if original != request:
                return self.reply(request, error='CONFLICT')
            if self.refund_twice and request['op'] == 'session.close':
                self.spent[site] -= 1
            return reply
        if request['expected_revision'] != self.revision:
            return self.reply(request, error='CONFLICT')
        remaining = self.policy['sites'][site]['budget_min'] * 60 - self.spent.get(site, 0)
        if request['op'] == 'session.open':
            if site in self.active:
                return self.reply(request, error='SESSION_ACTIVE')
            self.elapsed += 1_000
            self.active[site] = {'start_elapsed_ms': self.elapsed, 'deadline_elapsed_ms': self.elapsed + 60_000}
            self.revision += 1
            if self.open_storage_error:
                return self.reply(request, error='STORAGE_ERROR')
            data = {'site': site, 'remaining_sec': remaining, 'granted_sec': 60,
                    'deadline_elapsed_ms': self.active[site]['deadline_elapsed_ms']}
        else:
            session = self.active.pop(site, None)
            if session is not None:
                self.elapsed += 800
                spent = 60 if self.charge_full_minute else (self.elapsed - session['start_elapsed_ms'] + 999) // 1000
                self.spent[site] = self.spent.get(site, 0) + spent
                self.revision += 1
                remaining -= spent
            data = {'site': site, 'remaining_sec': remaining, 'closed': session is not None}
        reply = self.reply(request, data=data)
        self.session_receipts[identifier] = request, reply
        return reply

    def revisions_submitted(self):
        return [value['expected_revision'] for value in self.calls if value['op'] == 'policy.replace']


class DeviceAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.phone = FakePhone()
        self.check = acceptance.DeviceAcceptance(self.phone)
        self.check.tcp = Mock()

    def test_existing_session_blocks_fixture_install(self):
        self.phone.active['personal'] = {'start_elapsed_ms': 40_000, 'deadline_elapsed_ms': 70_000}
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'real phone session is active'):
            self.check.fixture('tcp')
        self.assertEqual(self.phone.revisions_submitted(), [])
        self.assertIn('personal', self.phone.active)
        self.check.tcp.assert_not_called()

    def test_preexisting_fixture_id_is_preserved(self):
        self.phone.policy['sites']['sschecktcp'] = deepcopy(self.phone.policy['sites']['personal'])
        before = deepcopy(self.phone.policy)
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'already configured'):
            self.check.fixture('tcp')
        self.assertEqual(self.phone.revisions_submitted(), [])
        self.assertEqual(self.phone.policy, before)

    def test_cleanup_uses_fresh_policy_and_preserves_other_group_and_spend(self):
        changed_group = {'mode': 'blocked', 'budget_min': 0, 'domains': ['example.org'], 'exceptions': []}

        def concurrent_update(site):
            self.assertEqual(self.phone.policy['sites'][site], {
                'mode': 'limited', 'budget_min': 20, 'domains': ['httpbingo.org'], 'exceptions': [],
            })
            self.phone.policy['sites']['new'] = changed_group
            self.phone.policy['default_session_min'] = 17
            self.phone.revision += 1
            raise acceptance.AcceptanceError('Case failed')

        original_group = deepcopy(self.phone.policy['sites']['personal'])
        self.check.tcp.side_effect = concurrent_update
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'Case failed'):
            self.check.fixture('tcp')
        self.assertEqual(self.phone.revisions_submitted(), [7, 9])
        self.assertEqual(self.phone.policy, {'schema': 1, 'reset_hour': 4, 'default_session_min': 17,
                                            'sites': {'personal': original_group, 'new': changed_group}})
        self.assertEqual(self.phone.spent, {'personal': 92})
        self.assertEqual(self.check.cleanup_issues, [])

    def test_active_session_during_cleanup_marks_cli_report_failed_without_closing_it(self):
        def session_started(_site):
            self.phone.active['personal'] = {'start_elapsed_ms': 40_000, 'deadline_elapsed_ms': 70_000}
            self.phone.revision += 1

        self.check.tcp.side_effect = session_started
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'device-report.json'
            with (patch.object(acceptance, 'AndroidTransport') as transport,
                  patch.object(acceptance, 'DeviceAcceptance', return_value=self.check),
                  patch.object(self.check, 'ready'), patch.object(self.check, 'check_radios'),
                  redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
                transport.return_value.bind.return_value = self.phone
                result = acceptance.main(['tcp', '--usb-serial', 'fake-serial', '--report', str(path)])
                transport.return_value.bind.assert_called_once_with()
                transport.assert_called_once_with(usb_serial='fake-serial', timeout=30)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            report = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(result, 1)
        self.assertEqual(report['result'], 'FAIL')
        self.assertIn('real phone session is active', report['cleanup_issues'][0])
        self.assertEqual(self.phone.revisions_submitted(), [7])
        self.assertIn('personal', self.phone.active)
        self.assertIn('sschecktcp', self.phone.policy['sites'])

    def test_saved_but_unapplied_fixture_is_removed_without_running_case(self):
        self.phone.next_replace_failure = 'apply'
        with self.assertRaisesRegex(acceptance.RejectedMutation, 'APPLY_FAILED'):
            self.check.fixture('tcp')
        self.check.tcp.assert_not_called()
        self.assertEqual(self.phone.revisions_submitted(), [7, 8])
        self.assertNotIn('sschecktcp', self.phone.policy['sites'])
        self.assertEqual(self.check.cleanup_issues, [])

    def test_conflict_during_cleanup_leaves_group_and_never_retries(self):
        def conflict(_site):
            self.phone.next_replace_failure = 'conflict'

        self.check.tcp.side_effect = conflict
        self.check.fixture('tcp')
        self.assertEqual(self.phone.revisions_submitted(), [7, 8])
        self.assertIn('sschecktcp', self.phone.policy['sites'])
        self.assertIn('CONFLICT', self.check.cleanup_issues[0])

    def test_lost_reply_emits_only_read_observations_and_partial_cleanup(self):
        self.phone.next_replace_failure = 'lost'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'device-report.json'
            with (patch.object(acceptance, 'AndroidTransport') as transport,
                  patch.object(acceptance, 'DeviceAcceptance', return_value=self.check),
                  patch.object(self.check, 'ready'), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
                transport.return_value.bind.return_value = self.phone
                result = acceptance.main(['tcp', '--report', str(path)])
            report = json.loads(path.read_text(encoding='utf-8'))
        self.check.tcp.assert_not_called()
        self.assertEqual(result, 1)
        self.assertEqual([value['op'] for value in self.phone.calls],
                         ['policy.get', 'status', 'policy.replace', 'status', 'policy.get'])
        self.assertIn('sschecktcp', self.phone.policy['sites'])
        self.assertIn('indeterminate result', report['error'])
        self.assertEqual(len(report['cleanup_issues']), 1)

    def test_storage_error_after_fixture_save_requires_manual_inspection(self):
        self.phone.next_replace_failure = 'storage'
        self.assert_uncertain_mutation(['policy.get', 'status', 'policy.replace'])
        self.assertIn('sschecktcp', self.phone.policy['sites'])

    def test_storage_error_after_open_never_replaces_policy_in_cleanup(self):
        self.phone.open_storage_error = True
        self.check.tcp.side_effect = self.check.open_session
        self.assert_uncertain_mutation(['policy.get', 'status', 'policy.replace', 'status', 'session.open'])
        self.assertIn('sschecktcp', self.phone.active)

    def assert_uncertain_mutation(self, operations):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'device-report.json'
            with (patch.object(acceptance, 'AndroidTransport') as transport,
                  patch.object(acceptance, 'DeviceAcceptance', return_value=self.check),
                  patch.object(self.check, 'ready'), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
                transport.return_value.bind.return_value = self.phone
                result = acceptance.main(['tcp', '--report', str(path)])
            report = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(result, 1)
        self.assertEqual([value['op'] for value in self.phone.calls], operations + ['status', 'policy.get'])
        self.assertIn('STORAGE_ERROR', report['error'])
        self.assertIn('indeterminate', report['error'])
        self.assertIn('Inspect sschecktcp', report['cleanup_issues'][0])

    def test_cleanup_leaves_another_actors_fixture_session_open(self):
        def outside_open(site):
            self.phone.active[site] = {'start_elapsed_ms': 49_000, 'deadline_elapsed_ms': 109_000}
            self.phone.revision += 1
            self.check.open_session(site)

        self.check.tcp.side_effect = outside_open
        with self.assertRaises(acceptance.AcceptanceError):
            self.check.fixture('tcp')
        self.assertEqual(self.phone.revisions_submitted(), [7])
        self.assertIn('sschecktcp', self.phone.active)
        self.assertIn('opened outside', self.check.cleanup_issues[0])

    def test_cleanup_leaves_a_fixture_session_reopened_after_close(self):
        def outside_reopen(site):
            self.check.open_session(site)
            self.check.close_session(site, self.check.fixture_status())
            self.phone.active[site] = {'start_elapsed_ms': 51_800, 'deadline_elapsed_ms': 111_800}
            self.phone.revision += 1

        self.check.tcp.side_effect = outside_reopen
        self.check.fixture('tcp')
        self.assertEqual(self.phone.revisions_submitted(), [7])
        self.assertIn('sschecktcp', self.phone.active)
        self.assertIn('opened outside', self.check.cleanup_issues[0])

    def test_cleanup_can_close_the_grant_opened_by_this_run(self):
        self.check.tcp.side_effect = self.check.open_session
        self.check.fixture('tcp')
        self.assertNotIn('sschecktcp', self.phone.policy['sites'])
        self.assertEqual(self.check.cleanup_issues, [])

    def test_close_and_cleanup_both_leave_a_replacement_grant_open(self):
        def outside_replaces_grant(site):
            self.check.open_session(site)
            starting = self.phone.active[site]['start_elapsed_ms'] + 500
            self.phone.elapsed = starting
            self.phone.active[site]['start_elapsed_ms'] = starting
            self.phone.revision += 2
            self.check.close_session(site, self.check.fixture_status())

        self.check.tcp.side_effect = outside_replaces_grant
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'acknowledged fixture grant is no longer active'):
            self.check.fixture('tcp')
        self.assertNotIn('session.close', {value['op'] for value in self.phone.calls})
        self.assertEqual(self.phone.revisions_submitted(), [7])
        self.assertIn('sschecktcp', self.phone.active)
        self.assertIn('opened outside', self.check.cleanup_issues[0])

    def prepare_tcp_checks(self):
        self.check = acceptance.DeviceAcceptance(self.phone)

        def stream(_timeout):
            self.phone.elapsed += 6_000
            return 'fake-stream'

        def end(*_args):
            elapsed = self.phone.elapsed
            self.phone.elapsed += 1_000
            return {'status': 'error', 'httpStatus': 200, 'bytes': 20, 'finishedElapsedMs': elapsed}

        self.check.stream = Mock(side_effect=stream)
        self.check.wait_probe = Mock(side_effect=end)
        self.check.blocked = Mock()
        self.check.positive = Mock()

    def test_tcp_checks_discard_a_full_minute_charge_on_early_close(self):
        self.prepare_tcp_checks()
        self.phone.charge_full_minute = True
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'early-close charge'):
            self.check.fixture('tcp')

    def test_tcp_checks_discard_a_second_refund_after_close_replay(self):
        self.prepare_tcp_checks()
        self.phone.refund_twice = True
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'Repeated close changed'):
            self.check.fixture('tcp')

    def test_tcp_checks_accept_rounded_charge_and_repeated_close(self):
        self.prepare_tcp_checks()
        self.check.fixture('tcp')
        close_calls = [value for value in self.phone.calls if value['op'] == 'session.close']
        self.assertGreaterEqual(len(close_calls), 2)
        self.assertEqual(self.phone.spent['sschecktcp'], 7)
        self.assertEqual(self.check.cleanup_issues, [])

    def test_doze_preflight_leaves_sleeping_or_forced_idle_phone_untouched(self):
        awake = b'mForceIdle=false\nmScreenOn=true\nmState=ACTIVE mLightState=ACTIVE\n'
        cases = ((b'Awake', awake.replace(b'mForceIdle=false', b'mForceIdle=true')),
                 (b'Asleep', awake), (b'Awake', b''))
        for wakefulness, idle in cases:
            with self.subTest(wakefulness=wakefulness, idle=idle):
                commands = {('dumpsys', 'power'): b'mWakefulness=' + wakefulness + b'\n',
                            ('dumpsys', 'deviceidle'): idle}
                self.phone.shell = Mock(side_effect=lambda *args: commands[args])
                with patch.object(self.check, 'ready'):
                    with self.assertRaisesRegex(acceptance.AcceptanceError, 'awake.*unforced'):
                        self.check.run('doze')
                self.assertEqual(self.phone.revisions_submitted(), [])

    def test_failed_battery_simulation_only_attempts_battery_reset(self):
        self.phone.shell = Mock(side_effect=[b'mWakefulness=Awake\n',
                                            b'mForceIdle=false\nmScreenOn=true\nmState=ACTIVE mLightState=ACTIVE\n',
                                            b'Current Battery Service state\n',
                                            AndroidTimeoutError('Fixture failure'), b''])
        changes = set()
        with self.assertRaises(AndroidTimeoutError):
            self.check.enter_mode('doze', changes)
        self.check.leave_mode(changes)
        self.assertEqual([call.args for call in self.phone.shell.call_args_list[-2:]], [
            ('dumpsys', 'battery', 'unplug'), ('dumpsys', 'battery', 'reset'),
        ])
        self.assertEqual(len(self.phone.shell.call_args_list), 5)

    def test_doze_restoration_attempts_all_steps_after_first_failure(self):
        self.phone.shell = Mock(side_effect=[AndroidTimeoutError('Fixture failure'), b'', b''])
        self.check.leave_mode({'idle', 'battery', 'screen'})
        self.assertEqual([call.args for call in self.phone.shell.call_args_list], [
            ('dumpsys', 'deviceidle', 'unforce'), ('dumpsys', 'battery', 'reset'), ('input', 'keyevent', '224'),
        ])
        self.assertIn('unforce', self.check.cleanup_issues[0])

    def test_report_path_protects_existing_files_and_git_worktrees(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report = directory / 'existing.json'
            report.write_text('keep me', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                acceptance.open_report(report)
            self.assertEqual(report.read_text(encoding='utf-8'), 'keep me')
            (directory / '.git').mkdir()
            with self.assertRaisesRegex(acceptance.AcceptanceError, 'outside Git'):
                acceptance.open_report(directory / 'new.json')
            self.assertFalse((directory / 'new.json').exists())


if __name__ == '__main__':
    unittest.main()
