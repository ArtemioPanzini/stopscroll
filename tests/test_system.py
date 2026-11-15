import json
import subprocess
import tempfile
import unittest

from stopscroll.model import Session
from stopscroll.seed import initial_state
from stopscroll.system import (Runner, System, DNS_CONF, RESOLVED_DROPIN,
                               FIREFOX_POLICY, CHROME_POLICIES, DNS_UNIT)


class FakeRunner(Runner):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.answers = {}
        self.inputs = []
        self.missing_uid = False

    def uid(self, name):
        if self.missing_uid:
            raise KeyError(name)
        return {'stopscroll-dns': 991, 'systemd-resolve': 992}[name]

    def run(self, argv, input=None):
        self.calls.append(argv)
        self.inputs.append(input)
        answer = self.answers.get(tuple(argv), self.answers.get(argv[0], (0, '')))
        return subprocess.CompletedProcess(argv, answer[0], answer[1], 'ошибка' if answer[0] else '')


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = FakeRunner(self.tmp.name)
        self.r.answers[('systemctl', 'is-active', DNS_UNIT)] = (0, 'active\n')
        self.s = System(self.r)
        self.st = initial_state(1000, '2026-11-15')

    def sync(self):
        return self.s.sync(self.st, 1000)

    def write(self, path, text):
        p = self.r.path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_first_sync_writes_everything(self):
        self.assertTrue(self.sync())
        for p in [DNS_CONF, RESOLVED_DROPIN, FIREFOX_POLICY] + CHROME_POLICIES:
            self.assertTrue(self.r.path(p).is_file())
        for cmd in [['nft', '-f', '-'], ['systemctl', 'restart', DNS_UNIT], ['resolvectl', 'flush-caches']]:
            self.assertIn(cmd, self.r.calls)
        self.assertTrue(any('991, 992' in (s or '') for s in self.r.inputs))

    def test_second_sync_changes_nothing(self):
        self.sync()
        self.r.calls.clear()
        self.assertEqual(self.sync(), [])
        self.assertFalse(any('restart' in c for c in self.r.calls))

    def test_changed_blocklist_restarts_dns_and_flushes(self):
        self.sync()
        self.r.calls.clear()
        self.st.sessions['youtube'] = Session(1000, 1060)
        self.sync()
        self.assertIn(['systemctl', 'restart', DNS_UNIT], self.r.calls)
        self.assertIn(['resolvectl', 'flush-caches'], self.r.calls)

    def test_deleted_policy_is_restored_and_reported(self):
        self.sync()
        self.r.path(CHROME_POLICIES[0]).unlink()
        self.assertEqual(len(self.sync()), 1)
        self.assertTrue(self.r.path(CHROME_POLICIES[0]).exists())

    def test_firefox_policy_is_merged(self):
        self.write(FIREFOX_POLICY, '{"other": 1, "policies": {"DisableTelemetry": true}}')
        self.sync()
        data = json.loads(self.r.path(FIREFOX_POLICY).read_text())
        self.assertEqual(data['other'], 1)
        self.assertTrue(data['policies']['DisableTelemetry'])
        self.assertEqual(data['policies']['DNSOverHTTPS'], {'Enabled': False, 'Locked': True})

    def test_unreadable_firefox_policy_is_backed_up(self):
        self.write(FIREFOX_POLICY, 'не json')
        self.sync()
        self.assertEqual(self.r.path(FIREFOX_POLICY + '.stopscroll-bak').read_text(), 'не json')
        self.assertIn('policies', json.loads(self.r.path(FIREFOX_POLICY).read_text()))

    def test_resolved_dropin_exact_text(self):
        self.sync()
        self.assertEqual(self.r.path(RESOLVED_DROPIN).read_text(), '[Resolve]\nDNS=127.0.0.1:5354\nDomains=~.\n')

    def test_dns_not_active_is_started(self):
        self.r.answers[('systemctl', 'is-active', DNS_UNIT)] = (3, 'inactive')
        self.sync()
        self.assertIn(['systemctl', 'start', DNS_UNIT], self.r.calls)

    def test_missing_table_forces_dns_restart(self):
        self.sync()
        self.r.calls.clear()
        self.r.answers[('nft', 'list', 'table', 'inet', 'stopscroll')] = (1, '')
        self.sync()
        self.assertIn(['systemctl', 'restart', DNS_UNIT], self.r.calls)

    def test_command_failure_does_not_raise(self):
        self.r.answers[('nft', '-f', '-')] = (1, '')
        self.assertTrue(any('сбой' in s for s in self.sync()))
        self.assertTrue(self.r.path(CHROME_POLICIES[-1]).exists())
        self.r.answers.clear()
        self.r.calls.clear()
        self.sync()
        self.assertIn(['nft', '-f', '-'], self.r.calls)

    def test_unwritable_file_does_not_raise(self):
        self.write(str(self.r.path(CHROME_POLICIES[0]).parent.relative_to(self.tmp.name)), 'file')
        self.assertTrue(any('сбой' in s for s in self.sync()))
        self.assertTrue(self.r.path(CHROME_POLICIES[1]).exists())

    def test_uid_failure_continues(self):
        self.r.missing_uid = True
        self.assertTrue(any('сбой' in s for s in self.sync()))
        self.assertTrue(self.r.path(DNS_CONF).exists())

    def test_failed_side_effect_is_retried_after_write(self):
        for cmd in [('systemctl', 'restart', DNS_UNIT), ('resolvectl', 'flush-caches'), ('systemctl', 'restart', 'systemd-resolved')]:
            with self.subTest(cmd=cmd):
                self.s = System(self.r)
                self.r.answers[cmd] = (1, '')
                # Force a new file write even for successive subtests.
                self.r.path(DNS_CONF).unlink(missing_ok=True)
                self.r.path(RESOLVED_DROPIN).unlink(missing_ok=True)
                self.assertTrue(any('сбой' in s for s in self.sync()))
                del self.r.answers[cmd]
                self.r.calls.clear()
                self.sync()
                self.assertIn(list(cmd), self.r.calls)

    def test_quarantine_moves_elements(self):
        self.r.answers[('nft', '-j', 'list', 'set', 'inet', 'stopscroll', 'site_youtube4')] = (0, '{"nftables":[{"metainfo":{"json_schema_version":1}},{"set":{"family":"inet","table":"stopscroll","name":"site_youtube4","type":"ipv4_addr","elem":["142.250.1.1","142.250.1.2"]}}]}')
        self.r.answers[('nft', '-j', 'list', 'set', 'inet', 'stopscroll', 'site_youtube6')] = (0, '{"nftables":[{"set":{"family":"inet","table":"stopscroll","name":"site_youtube6","type":"ipv6_addr"}}]}')
        self.s.quarantine('youtube')
        scripts = '\n'.join(value or '' for value in self.r.inputs)
        self.assertIn('add element inet stopscroll quarantine4 { 142.250.1.1 timeout 120000ms, 142.250.1.2 timeout 120000ms }', scripts)
        self.assertIn('delete element inet stopscroll site_youtube4 { 142.250.1.1, 142.250.1.2 }', scripts)
        self.assertNotIn('flush set', scripts)

    def test_quarantine_survives_nft_error(self):
        self.r.answers['nft'] = (1, '')
        self.s.quarantine('youtube')

    def test_uninstall(self):
        self.s.uninstall()
        self.assertIn(['systemd-run', '--unit=stopscroll-uninstall', '/usr/local/lib/stopscroll/uninstall.sh'], self.r.calls)

    def test_restart_recovers_service_configuration(self):
        self.sync()
        for command in [('systemctl', 'restart', DNS_UNIT), ('resolvectl', 'flush-caches'), ('systemctl', 'restart', 'systemd-resolved')]:
            self.r.answers[command] = (1, '')
        self.s = System(self.r)
        self.sync()
        self.r.answers.clear()
        self.r.calls.clear()
        self.s = System(self.r)
        self.sync()
        for command in [('systemctl', 'restart', DNS_UNIT), ('resolvectl', 'flush-caches'), ('systemctl', 'restart', 'systemd-resolved')]:
            self.assertIn(list(command), self.r.calls)

    def test_durable_quarantine_retry_preserves_exception_and_deadline(self):
        from stopscroll.model import State
        self.s.queue_quarantine(self.st, 'youtube')
        read = ('nft', '-j', 'list', 'set', 'inet', 'stopscroll', 'site_youtube4')
        self.r.answers[read] = (0, '{"nftables":[{"set":{"elem":["142.250.1.1"]}}]}')
        self.r.answers[('nft', '-j', 'list', 'set', 'inet', 'stopscroll', 'site_youtube6')] = (0, '{"nftables":[{"set":{}}]}')
        self.r.answers[('nft', '-f', '-')] = (1, '')
        messages = self.s.retry_quarantine(self.st, 1000, lambda: True)
        self.assertTrue(any('сбой' in message for message in messages))
        self.assertIn('site_youtube4', self.st.quarantine)
        script = self.r.inputs[self.r.calls.index(['nft', '-f', '-'])]
        self.assertIn('delete element inet stopscroll site_youtube4', script)
        self.assertNotIn('flush set', script)
        self.st = State.from_dict(self.st.to_dict())
        self.s = System(self.r)
        self.r.answers.pop(('nft', '-f', '-'))
        self.r.calls.clear(); self.r.inputs.clear()
        # An exception address arriving after the snapshot must not be quarantined.
        self.r.answers[read] = (0, '{"nftables":[{"set":{"elem":["142.250.1.1","142.250.2.2"]}}]}')
        self.s.retry_quarantine(self.st, 1060, lambda: True)
        script = self.r.inputs[self.r.calls.index(['nft', '-f', '-'])]
        self.assertIn('timeout 60000ms', script)
        self.assertNotIn('142.250.2.2', script)
        self.assertEqual(self.st.quarantine, {})
        self.r.calls.clear()
        self.s.retry_quarantine(self.st, 1070, lambda: True)
        self.assertEqual(self.r.calls, [])

    def test_quarantine_snapshot_must_be_saved_before_enforcement(self):
        self.s.queue_quarantine(self.st, 'youtube')
        self.r.answers['nft'] = (0, '{"nftables":[{"set":{"elem":["142.250.1.1"]}}]}')
        self.s.retry_quarantine(self.st, 1000, lambda: False)
        self.assertNotIn(['nft', '-f', '-'], self.r.calls)

    def test_failed_quarantine_after_deadline_retries_full_interval(self):
        self.s.queue_quarantine(self.st, 'youtube')
        read = ('nft', '-j', 'list', 'set', 'inet', 'stopscroll', 'site_youtube4')
        self.r.answers[read] = (0, '{"nftables":[{"set":{"elem":["142.250.1.1"]}}]}')
        self.r.answers[('nft', '-j', 'list', 'set', 'inet', 'stopscroll', 'site_youtube6')] = (0, '{"nftables":[{"set":{}}]}')
        self.r.answers[('nft', '-f', '-')] = (1, '')
        self.s.retry_quarantine(self.st, 1000, lambda: True)
        self.r.answers.pop(('nft', '-f', '-'))
        self.r.calls.clear(); self.r.inputs.clear()
        self.s.retry_quarantine(self.st, 1300, lambda: True)
        self.assertIn('timeout 120000ms', self.r.inputs[self.r.calls.index(['nft', '-f', '-'])])
        self.assertFalse(self.st.quarantine)

    def test_replayed_success_after_deadline_leaves_exception_untouched(self):
        from stopscroll.model import Quarantine
        self.st.quarantine['site_youtube4'] = Quarantine(['142.250.1.1'], 1120)
        self.r.answers['nft'] = (0, '{"nftables":[{"set":{"elem":["142.250.2.2"]}}]}')
        self.s.retry_quarantine(self.st, 1300, lambda: True)
        self.assertNotIn(['nft', '-f', '-'], self.r.calls)
        self.assertFalse(self.st.quarantine)

    def test_closed_site_without_close_job_does_not_quarantine_exceptions(self):
        self.r.answers['nft'] = (0, '{"nftables":[{"set":{"elem":["142.250.2.2"]}}]}')
        self.s.retry_quarantine(self.st, 1000, lambda: True)
        self.assertEqual(self.r.calls, [])

    def test_new_close_recaptures_addresses_after_earlier_failed_job(self):
        from stopscroll.model import Quarantine
        self.st.quarantine['site_youtube4'] = Quarantine(['142.250.1.1'], 1120)
        self.s.queue_quarantine(self.st, 'youtube')
        self.assertIsNone(self.st.quarantine['site_youtube4'].addresses)

    def test_quarantine_replay_after_wall_clock_rollback_is_bounded(self):
        from stopscroll.model import Quarantine
        self.st.quarantine['site_youtube4'] = Quarantine(['142.250.1.1'], 1120)
        self.r.answers['nft'] = (0, '{"nftables":[{"set":{"elem":["142.250.1.1"]}}]}')
        self.s.retry_quarantine(self.st, -2600, lambda: True)
        script = self.r.inputs[self.r.calls.index(['nft', '-f', '-'])]
        self.assertIn('timeout 120000ms', script)
        self.assertNotIn('3720000ms', script)
