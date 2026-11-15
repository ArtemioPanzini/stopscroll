"""Reconcile the private DNS/firewall setup without stopping on repair errors."""
import ipaddress
import json
import logging
import math
from pathlib import Path
import pwd
import subprocess

from .blocklist import DNS_PORT, QUARANTINE_SEC, render_dnsmasq, render_nft, set_names
from .model import State, Quarantine

DNS_CONF = '/var/lib/stopscroll/dnsmasq.conf'
RESOLVED_DROPIN = '/etc/systemd/resolved.conf.d/stopscroll.conf'
FIREFOX_POLICY = '/etc/firefox/policies/policies.json'
CHROME_POLICIES = ['/etc/opt/chrome/policies/managed/stopscroll.json',
                   '/etc/chromium/policies/managed/stopscroll.json']
DNS_UNIT = 'stopscroll-dns.service'


class Runner:
    def __init__(self, root: str = '/'):
        self.root = Path(root)

    def path(self, p: str) -> Path:
        relative = Path(p.lstrip('/'))
        if '..' in relative.parts:
            raise ValueError('Путь выходит за корневой каталог')
        return self.root / relative

    def run(self, argv: list[str], input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(argv, input=input, text=True, capture_output=True, check=False)

    def uid(self, name: str) -> int:
        return pwd.getpwnam(name).pw_uid


class System:
    def __init__(self, runner: Runner):
        self.runner = runner
        self._nft = None
        self._dns_restart = True
        self._flush = True
        self._resolved_restart = True

    def _run(self, argv, input=None):
        result = self.runner.run(argv, input=input)
        if result.returncode:
            raise OSError(result.stderr.strip() or f'код {result.returncode}')
        return result

    def _write(self, name, text):
        path = self.runner.path(name)
        if path.exists() and path.read_text() == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return True

    def sync(self, state: State, now: float) -> list[str]:
        messages = []

        def step(label, action):
            try:
                action()
            except Exception as exc:
                messages.append(f'сбой: {label}: {exc}')

        def nft():
            exists = self.runner.run(['nft', 'list', 'table', 'inet', 'stopscroll']).returncode == 0
            if not exists:
                self._dns_restart = True
            text = render_nft(state, self.runner.uid('stopscroll-dns'), self.runner.uid('systemd-resolve'))
            # Reapply on each reconciliation: external rules may have been changed.
            self._run(['nft', '-f', '-'], input=text)
            if not exists or self._nft != text:
                messages.append('Исправлена таблица сетевых правил')
            self._nft = text

        def dns():
            if self._write(DNS_CONF, render_dnsmasq(state, now)):
                self._dns_restart = True
                self._flush = True
                messages.append('Обновлена конфигурация DNS')
            if self._dns_restart:
                self._run(['systemctl', 'restart', DNS_UNIT])
                self._dns_restart = False
                messages.append('Перезапущена служба DNS')
            if self._flush:
                self._run(['resolvectl', 'flush-caches'])
                self._flush = False
                messages.append('Очищен кэш DNS')

        def active():
            result = self.runner.run(['systemctl', 'is-active', DNS_UNIT])
            if result.returncode or result.stdout.strip() != 'active':
                self._run(['systemctl', 'start', DNS_UNIT])
                messages.append('Запущена служба DNS')

        def resolved():
            if self._write(RESOLVED_DROPIN, f'[Resolve]\nDNS=127.0.0.1:{DNS_PORT}\nDomains=~.\n'):
                self._resolved_restart = True
                messages.append('Обновлена настройка системного DNS')
            if self._resolved_restart:
                self._run(['systemctl', 'restart', 'systemd-resolved'])
                self._resolved_restart = False
                messages.append('Перезапущена служба системного DNS')

        def firefox():
            path = self.runner.path(FIREFOX_POLICY)
            data = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    if not isinstance(data, dict) or not isinstance(data.get('policies', {}), dict):
                        raise ValueError('Некорректный файл политик')
                except (OSError, ValueError):
                    path.rename(path.with_name(path.name + '.stopscroll-bak'))
                    data = {}
            policy = {'Enabled': False, 'Locked': True}
            if data.get('policies', {}).get('DNSOverHTTPS') == policy:
                return
            data.setdefault('policies', {})['DNSOverHTTPS'] = policy
            self._write(FIREFOX_POLICY, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            messages.append('Исправлена политика Firefox')

        step('сетевые правила', nft)
        step('конфигурация DNS', dns)
        step('служба DNS', active)
        step('системный DNS', resolved)
        step('политика Firefox', firefox)
        for name in CHROME_POLICIES:
            def chrome(name=name):
                if self._write(name, '{"DnsOverHttpsMode": "off"}\n'):
                    messages.append(f'Исправлена политика браузера: {name}')
            step(f'политика браузера {name}', chrome)
        return messages

    def queue_quarantine(self, state: State, site: str) -> None:
        for name in set_names(site):
            state.quarantine[name] = Quarantine()

    def _addresses(self, name):
        result = self._run(['nft', '-j', 'list', 'set', 'inet', 'stopscroll', name])
        data = json.loads(result.stdout)
        addresses = [str(ipaddress.ip_address(elem)) for item in data['nftables']
                     for elem in item.get('set', {}).get('elem', [])]
        if any(ipaddress.ip_address(addr).version != int(name[-1]) for addr in addresses):
            raise ValueError('Неверное семейство адреса')
        return addresses

    def retry_quarantine(self, state: State, now: float, persist) -> list[str]:
        """Journal snapshots before applying; only actual close jobs are retried."""
        messages = []
        for name, job in list(state.quarantine.items()):
            try:
                current = self._addresses(name)
                if job.addresses is None:
                    job.addresses = current
                    job.expires = now + QUARANTINE_SEC
                remaining = set(job.addresses) & set(current)
                if job.expires <= now:
                    if not remaining:
                        # Atomic move already succeeded, including a crash before ack.
                        del state.quarantine[name]
                        persist()
                        continue
                    # The source still contains unprocessed close work.
                    job.expires = now + QUARANTINE_SEC
                if not persist():
                    continue
                lines = []
                if job.addresses:
                    # Wall-clock rollback must never lengthen the quarantine interval.
                    timeout = min(QUARANTINE_SEC * 1000,
                                  max(1, math.ceil((job.expires - now) * 1000)))
                    entries = ', '.join(f'{addr} timeout {timeout}ms' for addr in job.addresses)
                    lines.append(f'add element inet stopscroll quarantine{name[-1]} {{ {entries} }}')
                if remaining:
                    entries = ', '.join(sorted(remaining))
                    lines.append(f'delete element inet stopscroll {name} {{ {entries} }}')
                if lines:
                    # One nft transaction: failure retains every source element.
                    self._run(['nft', '-f', '-'], input='\n'.join(lines) + '\n')
                del state.quarantine[name]
                persist()
            except Exception as exc:
                messages.append(f'сбой: карантин {name}: {exc}')
        return messages

    def quarantine(self, site: str) -> None:
        # Compatibility entry point for standalone callers; daemon journals jobs.
        state = State(0, {}, [], '')
        self.queue_quarantine(state, site)
        for message in self.retry_quarantine(state, 0, lambda: True):
            logging.getLogger(__name__).warning('%s', message)

    def uninstall(self) -> None:
        self._run(['systemd-run', '--unit=stopscroll-uninstall', '/usr/local/lib/stopscroll/uninstall.sh'])
