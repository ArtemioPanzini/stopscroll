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
        self.assertIn(['nft', 'add', 'element', 'inet', 'stopscroll', 'quarantine4', '{ 142.250.1.1 timeout 120s, 142.250.1.2 timeout 120s }'], self.r.calls)
        for n in ('site_youtube4', 'site_youtube6'):
            self.assertIn(['nft', 'flush', 'set', 'inet', 'stopscroll', n], self.r.calls)

    def test_quarantine_survives_nft_error(self):
        self.r.answers['nft'] = (1, '')
        self.s.quarantine('youtube')

    def test_uninstall(self):
        self.s.uninstall()
        self.assertIn(['systemd-run', '--unit=stopscroll-uninstall', '/usr/local/lib/stopscroll/uninstall.sh'], self.r.calls)
