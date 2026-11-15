"""Packaging checks: no root, network, or system changes."""
import configparser
from pathlib import Path
import subprocess
import unittest

from stopscroll import system

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def unit(self, name):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        with (ROOT / 'system' / name).open() as stream:
            parser.read_file(stream)
        return parser

    def test_units_parse_and_have_required_keys(self):
        expected = {
            'stopscroll-dns.service': ('/usr/sbin/dnsmasq --keep-in-foreground --user=stopscroll-dns --conf-file=/var/lib/stopscroll/dnsmasq.conf', '1', 'multi-user.target'),
            'stopscrolld.service': ('/usr/bin/python3 -m stopscroll.daemon', '2', 'multi-user.target'),
            'stopscroll-notify.service': ('/usr/bin/python3 -m stopscroll.notify', '5', 'graphical-session.target'),
        }
        for name, (command, delay, target) in expected.items():
            with self.subTest(name=name):
                unit = self.unit(name)
                self.assertEqual(unit['Service']['ExecStart'], command)
                self.assertEqual(unit['Service']['Restart'], 'always')
                self.assertEqual(unit['Service']['RestartSec'], delay)
                self.assertEqual(unit['Install']['WantedBy'], target)
        daemon = self.unit('stopscrolld.service')['Service']
        self.assertEqual(daemon['RuntimeDirectory'], 'stopscroll')
        self.assertEqual(daemon['StateDirectoryMode'], '0700')
        for name in ('stopscrolld.service', 'stopscroll-notify.service'):
            self.assertEqual(self.unit(name)['Service']['Environment'], 'PYTHONPATH=/usr/local/lib/stopscroll')
        watchdog = self.unit('stopscroll-watchdog.service')['Service']
        self.assertEqual(watchdog['Type'], 'oneshot')
        self.assertEqual(watchdog['ExecStart'], '/usr/bin/systemctl start stopscrolld.service stopscroll-dns.service')

    def test_dns_unit_uses_same_conf_path_as_system_module(self):
        self.assertIn(system.DNS_CONF, self.unit('stopscroll-dns.service')['Service']['ExecStart'])

    def test_dns_unit_drops_to_dedicated_user(self):
        self.assertIn('--user=stopscroll-dns', self.unit('stopscroll-dns.service')['Service']['ExecStart'])

    def test_watchdog_interval(self):
        unit = self.unit('stopscroll-watchdog.timer')
        self.assertEqual(unit['Timer']['OnBootSec'], '30')
        self.assertEqual(unit['Timer']['OnUnitActiveSec'], '60')
        self.assertEqual(unit['Install']['WantedBy'], 'timers.target')

    def test_scripts_pass_shell_syntax_check(self):
        for name in ('install.sh', 'uninstall.sh'):
            result = subprocess.run(['bash', '-n', str(ROOT / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_removes_every_path_system_module_creates(self):
        script = (ROOT / 'uninstall.sh').read_text()
        for path in (system.RESOLVED_DROPIN, *system.CHROME_POLICIES, system.FIREFOX_POLICY):
            self.assertIn(path, script)
        self.assertIn("pop('DNSOverHTTPS', None)", script)
        self.assertNotIn('set -e', script)

    def test_readme_has_emergency_section(self):
        readme = (ROOT / 'README.md').read_text()
        for text in ('Аварийное восстановление',
                     'sudo systemctl disable --now stopscroll-watchdog.timer stopscrolld.service stopscroll-dns.service',
                     'sudo rm /etc/systemd/resolved.conf.d/stopscroll.conf && sudo systemctl restart systemd-resolved',
                     'sudo nft delete table inet stopscroll'):
            self.assertIn(text, readme)
