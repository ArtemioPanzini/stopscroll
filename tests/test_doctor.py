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
