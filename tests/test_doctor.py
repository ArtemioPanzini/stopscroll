import json
import tempfile
import unittest
from tests.test_system import FakeRunner
from stopscroll.system import System
from stopscroll.seed import initial_state
from stopscroll.doctor import run_checks

class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.r=FakeRunner(self.tmp.name); self.state=initial_state(1000,'2026-11-15')
        System(self.r).sync(self.state,0)
        self.r.answers['systemctl']=(0,'active\n')
        self.r.answers[('resolvectl','status')]=(0,'Global\n DNS Servers: 127.0.0.1:5354\n DNS Domain: ~.\nLink 2 (eth0)\n DNS Domain: local\n')
        self.r.answers[('resolvectl','query','twitch.tv')]=(1,'')
        self.r.answers['dig']=(1,'')
    def test_green(self):
        self.assertTrue(all(c['ok'] for c in run_checks(self.r,self.state)))
    def test_vpn(self):
        self.r.answers[('resolvectl','status')]=(0,'Global\n DNS Servers: 127.0.0.1:5354\n DNS Domain: ~.\nLink 2 (tun0)\n DNS Domain: ~.\n')
        self.assertTrue(any(not c['ok'] and 'VPN' in c['detail'] for c in run_checks(self.r,self.state)))
    def test_direct(self):
        self.r.answers['dig']=(0,'answer')
        self.assertFalse(next(c for c in run_checks(self.r,self.state) if c['name']=='direct_dns')['ok'])
    def test_missing(self):
        def missing(*a,**kw): raise FileNotFoundError('нет команды')
        self.r.run=missing
        self.assertTrue(any(not c['ok'] for c in run_checks(self.r,self.state)))

    def test_removed_seed_domain_uses_current_blocked_site(self):
        del self.state.sites['twitch']
        self.r.answers[('resolvectl', 'query', 'liquipedia.net')] = (1, '')
        self.r.calls.clear()
        checks = run_checks(self.r, self.state)
        self.assertTrue(next(c for c in checks if c['name']=='blocked_dns')['ok'])
        self.assertIn(['resolvectl', 'query', 'liquipedia.net'], self.r.calls)
        self.assertNotIn(['resolvectl', 'query', 'twitch.tv'], self.r.calls)

    def test_pause_marks_blocked_check_inapplicable_and_keeps_other_probes(self):
        self.state.pause_until = 9999999999
        self.r.answers[('resolvectl', 'query', 'twitch.tv')] = (0, '')
        self.r.calls.clear()
        result = next(c for c in run_checks(self.r, self.state) if c['name']=='blocked_dns')
        self.assertTrue(result['ok'])
        self.assertIn('Не применяется', result['detail'])
        self.assertIn(['resolvectl', 'query', 'example.com'], self.r.calls)
        self.assertTrue(any(call[0]=='dig' for call in self.r.calls))

    def test_no_policy_sites_marks_blocked_probe_inapplicable(self):
        self.state.sites.clear()
        self.r.calls.clear()
        result = next(c for c in run_checks(self.r, self.state) if c['name']=='blocked_dns')
        self.assertTrue(result['ok'])
        self.assertIn('Не применяется', result['detail'])
        self.assertNotIn(['resolvectl', 'query', 'twitch.tv'], self.r.calls)
