"""Manual Android policy, session and diagnostic commands."""
import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .android_policy import (
    PolicyError, canonical_policy_bytes, domain_within, normalize_policy_domain,
    policy_diff, policy_hash, validate_policy, validate_site_id,
)
from .android_transport import (
    AndroidProtocolError, AndroidRequestError, AndroidTimeoutError, AndroidTransport, AndroidTransportError,
    decode_json, validate_usb_serial,
)

PACKAGE = 'com.celzero.bravedns.stopscroll'
HASH_RE = re.compile(r'[0-9a-f]{64}')
NETWORK_CHECK = 'Сетевая приёмка из другого UID требует отдельной проверки.'
POLICY_COMMANDS = {'apply', 'import-desktop', 'block', 'unblock', 'limit'}
PROTECTION_FLAGS = ('tunnel', 'always_on', 'lockdown', 'ignoring_battery_optimizations')
MAX_POLICY_FILE_BYTES = 1024 * 1024


class CommandRefused(Exception):
    """A well-formed command could not be completed."""


def _argument(convert, value):
    try:
        return convert(value)
    except (PolicyError, AndroidRequestError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _minutes(value):
    if not re.fullmatch(r'[0-9]{1,4}', value) or not 1 <= int(value) <= 1440:
        raise argparse.ArgumentTypeError('Укажите целое число минут от 1 до 1440')
    return int(value)


def _parser():
    parser = argparse.ArgumentParser(prog='stopscroll android', description='Управление Android по USB',
                                     allow_abbrev=False)
    serial = partial(_argument, validate_usb_serial)
    parser.add_argument('--usb-serial', metavar='СЕРИЙНЫЙ_НОМЕР', type=serial,
                        help='серийный номер USB-устройства')
    commands = parser.add_subparsers(dest='command')
    descriptions = {
        'status': 'показать защиту и остаток дневного бюджета',
        'list': 'показать конфигурацию сайтов телефона',
        'export': 'сохранить политику телефона в новый файл',
        'import-desktop': 'заменить правила телефона сайтами компьютера',
        'apply': 'заменить правила телефона политикой из файла',
        'block': 'закрыть группу или добавить в неё домен',
        'unblock': 'удалить группу или один домен',
        'limit': 'установить дневной бюджет группы',
        'open': 'открыть отрезок для группы с лимитом',
        'close': 'закрыть отрезок группы',
        'doctor': 'проверить APK и состояние защиты телефона',
    }
    for name, help_text in descriptions.items():
        command = commands.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        command.add_argument('--usb-serial', metavar='СЕРИЙНЫЙ_НОМЕР', default=argparse.SUPPRESS,
                             type=serial, help='серийный номер USB-устройства')
        if name in POLICY_COMMANDS:
            modes = command.add_mutually_exclusive_group()
            modes.add_argument('--yes', action='store_true', help='подтвердить замену без вопроса')
            modes.add_argument('--dry-run', action='store_true', help='показать изменения без записи')
        if name in ('block', 'unblock', 'limit', 'open', 'close'):
            command.add_argument('site', metavar='ГРУППА', type=partial(_argument, validate_site_id))
        if name in ('block', 'unblock'):
            command.add_argument('domain', metavar='ДОМЕН', nargs='?', type=partial(_argument, normalize_policy_domain))
        if name in ('limit', 'open'):
            command.add_argument('minutes', metavar='МИНУТЫ', type=_minutes, nargs='?' if name == 'open' else None)
        if name == 'apply':
            command.add_argument('file', metavar='ФАЙЛ', type=Path)
        if name == 'export':
            command.add_argument('--output', metavar='ФАЙЛ', type=Path, required=True)
    return parser


def _text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _saved_revision(reply):
    applied = reply['applied_revision'] if reply['applied_revision'] is not None else 'нет'
    return f"сохранённая ревизия {reply['revision']}, применённая {applied}"


def _rpc(transport, op, args=None, *, revision=None):
    request = {'schema': 1, 'request_id': str(uuid4()), 'op': op, 'args': args or {}}
    if revision is not None:
        request['expected_revision'] = revision
    try:
        reply = transport.call(request)
    except AndroidTransportError as exc:
        if revision is None:
            raise
        message = f'{exc}. Результат изменения неизвестен.'
        if isinstance(exc, AndroidTimeoutError):
            try:
                observed = _read_status(transport)
            except (CommandRefused, AndroidTransportError):
                pass
            else:
                print('Текущее состояние телефона после таймаута')
                _print_status(observed)
                raise AndroidTransportError(message) from exc
        raise AndroidTransportError(message + ' Проверьте состояние командой stopscroll android status.') from exc
    if reply['ok']:
        return reply
    error = reply['error']
    message = f"{error['code']} — {_text(error['message'])}"
    if reply.get('saved'):
        message += f". Изменение сохранено, применение не подтверждено ({_saved_revision(reply)})."
    raise CommandRefused(message)


def _require(condition, field):
    if not condition:
        raise AndroidProtocolError(f'Некорректное поле {field} в ответе Android')


def _uint(value):
    return type(value) is int and value >= 0


def _sha256(value):
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def _read_policy(transport):
    reply = _rpc(transport, 'policy.get')
    data = reply['data']
    _require(set(data) == {'policy', 'policy_hash'}, 'policy.get')
    try:
        digest = policy_hash(data['policy'])
    except PolicyError as exc:
        raise AndroidProtocolError(f'Некорректная политика телефона. {exc}') from None
    _require(data['policy_hash'] == digest, 'policy_hash')
    return reply


def _read_status(transport):
    reply = _rpc(transport, 'status')
    data = reply['data']
    _require(_sha256(data.get('policy_hash')), 'policy_hash')
    _require(isinstance(data.get('timezone'), str) and bool(data['timezone']), 'timezone')
    try:
        day = date.fromisoformat(data.get('day_id', ''))
    except (TypeError, ValueError):
        raise AndroidProtocolError('Некорректная дата бюджета Android') from None
    _require(data['day_id'] == day.isoformat(), 'day_id')
    _require(type(data.get('reset_hour')) is int and data['reset_hour'] == 4, 'reset_hour')
    default = data.get('default_session_min')
    _require(_uint(default) and 1 <= default <= 1440, 'default_session_min')
    clock = data.get('clock')
    _require(isinstance(clock, dict), 'clock')
    _require(type(clock.get('wall_ms')) is int, 'clock.wall_ms')
    for field in ('elapsed_ms', 'boot_count'):
        _require(_uint(clock.get(field)), 'clock.' + field)
    protection = data.get('protection')
    _require(isinstance(protection, dict), 'protection')
    _require(protection.get('state') in ('active', 'degraded', 'stopped', 'unprotected'), 'protection.state')
    _require(protection.get('preferred_state') in ('pending', 'installing', 'ready', 'failed'),
             'protection.preferred_state')
    _require('reason' in protection and (protection['reason'] is None or isinstance(protection['reason'], str)),
             'protection.reason')
    for field in PROTECTION_FLAGS:
        _require(type(protection.get(field)) is bool, 'protection.' + field)
    if protection['state'] == 'active':
        _require(all(protection[key] for key in PROTECTION_FLAGS) and protection['preferred_state'] == 'ready'
                 and reply['revision'] == reply['applied_revision'], 'protection.state')
    sites = data.get('sites')
    _require(isinstance(sites, dict), 'sites')
    for name, site in sites.items():
        try:
            validate_site_id(name)
        except PolicyError:
            raise AndroidProtocolError('Некорректный ID группы в статусе Android') from None
        _validate_site_status(name, site)
    return reply


def _validate_site_status(name, site):
    _require(isinstance(site, dict), f'sites.{name}')
    mode, budget, remaining, reserved = (site.get(key) for key in ('mode', 'budget_min', 'remaining_sec', 'reserved_sec'))
    path = f'sites.{name}'
    _require(mode in ('limited', 'blocked'), path + '.mode')
    _require(_uint(budget) and (1 <= budget <= 1440 if mode == 'limited' else budget == 0), path + '.budget_min')
    _require(_uint(remaining) and 0 <= remaining <= budget * 60, path + '.remaining_sec')
    _require(_uint(reserved) and 0 <= reserved <= remaining, path + '.reserved_sec')
    _require('session' in site, path + '.session')
    session = site['session']
    if session is None:
        _require(reserved == 0, path + '.reserved_sec')
        return
    _require(mode == 'limited' and isinstance(session, dict), path + '.session')
    start, deadline = session.get('start_elapsed_ms'), session.get('deadline_elapsed_ms')
    _require(_uint(start) and _uint(deadline) and start < deadline, path + '.session')


def _print_status(reply):
    data = reply['data']
    protection = data['protection']
    print(f"Защита Android — {protection['state']}, {_saved_revision(reply)}")
    if protection['reason']:
        print(_text(protection['reason']))
    print(f"День бюджета {data['day_id']}, сброс в {data['reset_hour']:02}:00 ({_text(data['timezone'])})")
    print(f"Отрезок по умолчанию {data['default_session_min']} мин")
    for name, site in sorted(data['sites'].items()):
        if site['mode'] == 'blocked':
            print(f'  {name} — постоянно закрыта')
        else:
            print(f"  {name} — {'открыта' if site['session'] is not None else 'закрыта'}, "
                  f"бюджет {site['budget_min']} мин, остаток {site['remaining_sec']} с, "
                  f"в отрезке {site['reserved_sec']} с, свободно {site['remaining_sec'] - site['reserved_sec']} с")


def _print_policy(policy):
    print(f"Сброс в {policy['reset_hour']:02}:00, отрезок по умолчанию {policy['default_session_min']} мин")
    for name, site in sorted(policy['sites'].items()):
        mode = 'постоянно закрыта' if site['mode'] == 'blocked' else f"бюджет {site['budget_min']} мин"
        print(f"  {name} — {mode}. Домены {', '.join(site['domains'])}.")
        if site['exceptions']:
            print(f"    Исключения {', '.join(site['exceptions'])}.")


def _export(path: Path, policy):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.stopscroll-', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(canonical_policy_bytes(policy) + b'\n')
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise CommandRefused(f'Файл {path} уже существует. Укажите новый путь для экспорта.') from None
    except OSError as exc:
        raise CommandRefused(f'Не удалось сохранить политику в {path}. {exc.strerror or exc}') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f'Политика Android сохранена в {path}')


def _candidate(args, exchange):
    if args.command == 'apply':
        try:
            with _open_regular_file(args.file) as stream:
                raw = stream.read(MAX_POLICY_FILE_BYTES + 1)
            if len(raw) > MAX_POLICY_FILE_BYTES:
                raise PolicyError('Размер JSON-файла превышает 1 МиБ')
            return validate_policy(decode_json(raw))
        except (OSError, ValueError, RecursionError) as exc:
            raise PolicyError(f'Не удалось прочитать политику из {args.file}. {exc}') from None
    if args.command == 'import-desktop':
        try:
            reply = exchange({'cmd': 'export-sites'})
        except (OSError, ValueError) as exc:
            raise AndroidTransportError('Служба stopscroll недоступна или прислала некорректный ответ') from exc
        if not isinstance(reply, dict) or type(reply.get('ok')) is not bool:
            raise AndroidProtocolError('Некорректный ответ службы на export-sites')
        if not reply['ok']:
            raise CommandRefused(f"Экспорт сайтов отклонён. {_text(str(reply.get('error', '')))}")
        if 'policy' not in reply:
            raise AndroidProtocolError('В ответе export-sites отсутствует политика')
        return validate_policy(reply['policy'])
    return None


def _edit_policy(policy, args):
    result = deepcopy(policy)
    sites = result['sites']
    name = args.site
    site = sites.get(name)
    if args.command == 'block' and args.domain is not None:
        if site is None:
            sites[name] = {'mode': 'blocked', 'budget_min': 0, 'domains': [args.domain], 'exceptions': []}
        elif args.domain not in site['domains']:
            site['domains'].append(args.domain)
        return result
    if site is None:
        raise CommandRefused(f'Группа {name} отсутствует в политике Android')
    if args.command == 'limit':
        site.update(mode='limited', budget_min=args.minutes)
    elif args.command == 'block':
        site.update(mode='blocked', budget_min=0)
    elif args.domain is None:
        del sites[name]
    else:
        if args.domain not in site['domains']:
            raise CommandRefused(f'В группе {name} отсутствует домен {args.domain}')
        domains = [domain for domain in site['domains'] if domain != args.domain]
        if domains:
            site['domains'] = domains
            site['exceptions'] = [exception for exception in site['exceptions']
                                  if any(domain_within(exception, domain) for domain in domains)]
        else:
            del sites[name]
    return result


def _confirmation():
    if not sys.stdin.isatty():
        print('Для подтверждения нужен терминал или флаг --yes', file=sys.stderr)
        return False
    try:
        return input('Применить изменения на телефоне? [y/N] ').strip().lower() == 'y'
    except (EOFError, KeyboardInterrupt):
        return False


def _print_diff(changes, snapshot):
    print(f"Предпросмотр политики Android, ревизия {snapshot['revision']}")
    for change in changes:
        old = json.dumps(change['before'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        new = json.dumps(change['after'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        print(f"  {change['path']}  {old} → {new}")
    active = sorted(name for name, state in snapshot['data']['sites'].items() if state['session'] is not None)
    if active:
        print(f"Закроются активные отрезки групп {', '.join(active)}. Неиспользованный резерв вернётся в дневной бюджет.")
    else:
        print('Активных отрезков для закрытия нет.')


def _confirm_mutation_receipt(reply, expected_revision, *, changed):
    revision, applied = reply['revision'], reply['applied_revision']
    _require(revision >= expected_revision + int(changed), 'revision')
    _require(applied is None or applied <= revision, 'applied_revision')
    if changed and applied != revision:
        raise CommandRefused(f"Применение на телефоне не подтверждено ({_saved_revision(reply)}).")


def _replace(transport, args, candidate, confirm):
    current = _read_policy(transport)
    policy = current['data']['policy']
    if candidate is None:
        candidate = _edit_policy(policy, args)
    changes = policy_diff(policy, candidate)
    if not changes:
        print('Политика Android уже совпадает.')
        return 0
    snapshot = _read_status(transport)
    if snapshot['revision'] != current['revision']:
        raise CommandRefused('CONFLICT — состояние телефона изменилось во время чтения политики.')
    _require(snapshot['data']['policy_hash'] == current['data']['policy_hash'], 'policy_hash')
    _require(set(snapshot['data']['sites']) == set(policy['sites']), 'sites')
    _print_diff(changes, snapshot)
    if args.dry_run:
        return 0
    if not args.yes and not confirm():
        print('Изменение отменено')
        return 2
    reply = _rpc(transport, 'policy.replace', {'policy': candidate}, revision=current['revision'])
    data = reply['data']
    _require(set(data) == {'changed', 'policy_hash'} and data['changed'] is True, 'policy.replace')
    _require(data['policy_hash'] == policy_hash(candidate), 'policy_hash')
    _confirm_mutation_receipt(reply, current['revision'], changed=True)
    print(f"Политика применена, ревизия {reply['revision']}")
    return 0


def _session(transport, args):
    current = _read_status(transport)
    parameters = {'site': args.site}
    if args.command == 'open':
        parameters['minutes'] = args.minutes
    reply = _rpc(transport, f'session.{args.command}', parameters, revision=current['revision'])
    data = reply['data']
    fields = {'site', 'remaining_sec'}
    fields.update({'granted_sec', 'deadline_elapsed_ms'} if args.command == 'open' else {'closed'})
    _require(set(data) == fields and data['site'] == args.site and _uint(data['remaining_sec']),
             f'session.{args.command}')
    if args.command == 'open':
        _require(_uint(data['granted_sec']) and data['granted_sec'] > 0, 'granted_sec')
        _require(_uint(data['deadline_elapsed_ms']), 'deadline_elapsed_ms')
        _confirm_mutation_receipt(reply, current['revision'], changed=True)
        print(f"{args.site} — отрезок открыт на {data['granted_sec']} с, остаток {data['remaining_sec']} с")
    else:
        _require(type(data['closed']) is bool, 'closed')
        _confirm_mutation_receipt(reply, current['revision'], changed=data['closed'])
        state = 'отрезок закрыт' if data['closed'] else 'активного отрезка уже нет'
        print(f"{args.site} — {state}, остаток {data['remaining_sec']} с")


def _receipt_path():
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return config / 'stopscroll' / 'android-release.json'


def _open_regular_file(path):
    stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb')
    if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        return stream
    stream.close()
    raise OSError(f'Требуется обычный файл {path}')


def _read_release_receipt(path):
    with _open_regular_file(path) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if stat.S_IMODE(mode) != 0o600:
            raise OSError('Для android-release.json нужен обычный файл с правами 0600')
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise OSError('Слишком большой файл android-release.json')
    try:
        value = decode_json(raw)
    except (ValueError, RecursionError):
        raise OSError('Некорректный JSON в android-release.json') from None
    if (not isinstance(value, dict) or set(value) != {
            'schema', 'package', 'certificate_sha256', 'version_code', 'apk_sha256'}
            or type(value['schema']) is not int or value['schema'] != 1 or value['package'] != PACKAGE
            or not _uint(value['version_code']) or not _sha256(value['certificate_sha256'])
            or not _sha256(value['apk_sha256'])):
        raise OSError('Некорректные данные выпуска в android-release.json')
    return value


def _tool_in_sdk(root, tool):
    pattern = 'build-tools/*/apksigner' if tool == 'apksigner' else 'cmdline-tools/*/bin/apkanalyzer'
    return next((str(path) for path in sorted(root.glob(pattern), reverse=True)
                 if path.is_file() and os.access(path, os.X_OK)), None)


def _sdk_tool(tool):
    for name in ('ANDROID_SDK_ROOT', 'ANDROID_HOME'):
        root = os.environ.get(name)
        if root and (path := _tool_in_sdk(Path(root), tool)):
            return path
    path = shutil.which(tool) or _tool_in_sdk(Path.home() / '.cache/stopscroll-android/sdk', tool)
    if not path:
        raise OSError(f'Инструмент Android SDK {tool} не найден')
    return path


def _tool_output(runner, *arguments):
    try:
        result = runner(list(arguments), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise OSError(f'Не удалось запустить {Path(arguments[0]).name}') from None
    if result.returncode:
        raise OSError(f'Проверка APK через {Path(arguments[0]).name} завершилась ошибкой')
    return result.stdout


def inspect_installed_release(transport, *, receipt_path=None, tool_finder=_sdk_tool, runner=subprocess.run):
    """Compare the installed base APK to the local Task 5 release receipt."""
    expected = _read_release_receipt(receipt_path if receipt_path is not None else _receipt_path())
    tools = tuple(tool_finder(tool) for tool in ('apksigner', 'apkanalyzer'))
    return inspect_installed_apk(transport, tools=tools, runner=runner, expected=expected)


def inspect_installed_apk(transport, *, tools, runner=subprocess.run, expected=None):
    """Read and verify the installed base APK on an already bound transport."""
    output = transport.shell('pm', 'path', '--user', '0', PACKAGE)
    paths = re.findall(rb'^package:(/data/app/[A-Za-z0-9_~+=./-]+/base\.apk)\r?$', output, re.MULTILINE)
    if len(paths) != 1 or b'..' in paths[0].split(b'/'):
        raise OSError(f'Для {PACKAGE} не найден единственный установленный base.apk')
    with tempfile.TemporaryDirectory(prefix='stopscroll-apk-') as directory:
        apk = Path(directory) / 'base.apk'
        transport.pull(paths[0].decode('utf-8'), apk)
        return inspect_release_apk(apk, tools=tools, runner=runner, expected=expected)


def inspect_release_apk(apk, *, tools, runner=subprocess.run, expected=None):
    """Verify the signer and manifest of a local immutable APK snapshot."""
    apksigner, apkanalyzer = tools
    with _open_regular_file(apk) as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if expected is not None and digest != expected['apk_sha256']:
        raise OSError('APK отличается от проверенного выпуска по полю apk_sha256')
    signers = _tool_output(runner, apksigner, 'verify', '--print-certs', str(apk))
    certificates = re.findall(rb'^Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-fA-F]{64})\r?$',
                              signers, re.MULTILINE)
    if len(certificates) != 1:
        raise OSError('Не подтверждён единственный подписант APK')
    package = _tool_output(runner, apkanalyzer, 'manifest', 'application-id', str(apk)).strip()
    version = _tool_output(runner, apkanalyzer, 'manifest', 'version-code', str(apk)).strip()
    if not re.fullmatch(rb'[0-9]{1,20}', version):
        raise OSError('Некорректный versionCode в установленном APK')
    actual = {'schema': 1, 'package': package.decode('utf-8'), 'version_code': int(version),
              'certificate_sha256': certificates[0].decode('ascii').lower(), 'apk_sha256': digest}
    if expected is not None:
        mismatches = [key for key, value in actual.items() if expected[key] != value]
        if mismatches:
            raise OSError(f"APK отличается от проверенного выпуска по полям {', '.join(mismatches)}")
    return actual


def _clock_check(data, now):
    wall = data['clock']['wall_ms']
    zone = data['timezone']
    try:
        local = datetime.fromtimestamp(wall / 1000, ZoneInfo(zone))
        reset = local.replace(hour=data['reset_hour'], minute=0, second=0, microsecond=0, fold=0)
        day = local.date()
        if wall < int(reset.timestamp() * 1000):
            day -= timedelta(days=1)
        sampled_day = day.isoformat()
    except (ValueError, OSError, OverflowError, ZoneInfoNotFoundError):
        return False, f'Недоступны время или часовой пояс {_text(zone)}'
    skew = round((wall - now() * 1000) / 1000)
    return (abs(skew) <= 300 and sampled_day == data['day_id'],
            f"{_text(zone)}, местное время {local.isoformat(timespec='seconds')}, "
            f"расхождение {skew:+} с, день по часам {sampled_day}, день бюджета {data['day_id']}")


def _doctor(transport, inspect_release, now):
    checks = []
    try:
        installed = inspect_release(transport)
        checks.append((True, f"APK {installed['package']}, версия {installed['version_code']}, "
                             f"сертификат SHA-256 {installed['certificate_sha256']}"))
    except (OSError, UnicodeError, AndroidTransportError) as exc:
        checks.append((False, f'APK и подпись не подтверждены. {exc}'))
    try:
        status = _read_status(transport)
        data = status['data']
        protection = data['protection']
        for key, label in zip(PROTECTION_FLAGS, ('Туннель VPN', 'Always-on VPN', 'Lockdown', 'Исключение из энергосбережения')):
            checks.append((protection[key], label))
        ready = protection['preferred_state'] == 'ready'
        checks.append((ready, f"Режим Preferred — {protection['preferred_state']}"))
        checks.append((protection['state'] == 'active', f"Защита — {protection['state']}"))
        if protection['reason']:
            print(_text(protection['reason']))
        checks.append(_clock_check(data, now))
        current = _read_policy(transport)
        revisions_match = (current['revision'] == status['revision'] ==
                           current['applied_revision'] == status['applied_revision'])
        checks.append((revisions_match, f"Последнее чтение — {_saved_revision(current)}"))
        hash_matches = current['data']['policy_hash'] == data['policy_hash']
        checks.append((hash_matches, f"Хеш политики SHA-256 {data['policy_hash']}"))
    except (CommandRefused, AndroidTransportError) as exc:
        checks.append((False, str(exc)))
    for ok, text in checks:
        print(f"{'OK' if ok else 'СБОЙ'}  {text}")
    print(NETWORK_CHECK)
    return 0 if all(ok for ok, _ in checks) else 1


def main(argv, *, exchange, transport_factory=AndroidTransport, confirm=_confirmation,
         inspect_release=inspect_installed_release, now=time.time):
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code
    if args.command is None:
        parser.print_help()
        return 0
    try:
        candidate = _candidate(args, exchange)
        transport = transport_factory(usb_serial=args.usb_serial).bind()
        if args.command in POLICY_COMMANDS:
            return _replace(transport, args, candidate, confirm)
        if args.command == 'doctor':
            return _doctor(transport, inspect_release, now)
        if args.command == 'status':
            _print_status(_read_status(transport))
        elif args.command in ('list', 'export'):
            policy = _read_policy(transport)['data']['policy']
            if args.command == 'export':
                _export(args.output, policy)
            else:
                _print_policy(policy)
        else:
            _session(transport, args)
        return 0
    except (PolicyError, AndroidRequestError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except CommandRefused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except AndroidTransportError as exc:
        if args.command == 'doctor':
            print(f'СБОЙ  {exc}\n{NETWORK_CHECK}')
            return 1
        print(str(exc), file=sys.stderr)
        return 3
