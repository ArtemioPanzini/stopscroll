"""Read-only checks of the live DNS, firewall and browser policy setup."""
import json
import re
from .system import DNS_UNIT, FIREFOX_POLICY, CHROME_POLICIES


def run_checks(runner, state):
    checks = []
    def check(name, action, detail):
        try:
            ok = bool(action())
            checks.append(dict(name=name, ok=ok, detail='В порядке' if ok else detail))
        except Exception as exc:
            checks.append(dict(name=name, ok=False, detail=f'{detail}: {exc}'))
    def active(unit):
        result = runner.run(['systemctl', 'is-active', unit])
        return result.returncode == 0 and result.stdout.strip() == 'active'
    check('dns_service', lambda: active(DNS_UNIT), 'Служба DNS не работает')
    check('watchdog', lambda: active('stopscroll-watchdog.timer'), 'Сторож не работает')
    try:
        result = runner.run(['resolvectl', 'status'])
        if result.returncode:
            raise OSError(result.stderr)
        sections = re.split(r'(?m)^Link\s+\d+[^\n]*', result.stdout)
        global_text = sections[0]
        global_ok = bool(re.search(r'(?m)^\s*DNS Servers:\s+127\.0\.0\.1:5354\s*$', global_text)) and bool(re.search(r'(?m)^\s*DNS Domain:.*(?<!\S)~\.(?!\S)', global_text))
        vpn_ok = not any(re.search(r'(?m)^\s*DNS Domain:.*(?<!\S)~\.(?!\S)', part) for part in sections[1:])
        check('global_dns', lambda: global_ok, 'Глобальный DNS должен быть 127.0.0.1:5354 с доменом ~.')
        check('link_dns', lambda: vpn_ok, 'блокировка не действует: похоже на VPN')
    except Exception as exc:
        for name in ('global_dns', 'link_dns'):
            checks.append(dict(name=name, ok=False, detail=f'Не удалось проверить системный DNS: {exc}'))
    check('blocked_dns', lambda: runner.run(['resolvectl','query','twitch.tv']).returncode != 0, 'twitch.tv разрешается')
    check('normal_dns', lambda: runner.run(['resolvectl','query','example.com']).returncode == 0, 'example.com не разрешается')
    check('direct_dns', lambda: runner.run(['dig','+time=2','+tries=1','@1.1.1.1','example.com']).returncode != 0, 'Прямой DNS доступен')
    check('firewall', lambda: runner.run(['nft','list','table','inet','stopscroll']).returncode == 0, 'Нет таблицы inet stopscroll')
    check('firefox_policy', lambda: json.loads(runner.path(FIREFOX_POLICY).read_text())['policies']['DNSOverHTTPS'] == {'Enabled':False,'Locked':True}, 'Неверная политика Firefox')
    for index, path in enumerate(CHROME_POLICIES):
        check(f'chrome_policy_{index}', lambda path=path: json.loads(runner.path(path).read_text()).get('DnsOverHttpsMode') == 'off', f'Неверная политика браузера: {path}')
    return checks
