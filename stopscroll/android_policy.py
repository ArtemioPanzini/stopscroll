"""Validation, export and comparison of Android site policies."""
from copy import deepcopy
from functools import cache
import hashlib
import json
from pathlib import Path
import re
import stringprep
from typing import cast

from .changes import ChangeError, SITE_RE, normalize_domain
from .model import State

MAX_POLICY_BYTES = 64 * 1024
MAX_SITES = 100
MAX_DOMAINS = 1000
POLICY_FIELDS = {'schema', 'reset_hour', 'default_session_min', 'sites'}
SITE_FIELDS = {'mode', 'budget_min', 'domains', 'exceptions'}
PUBLIC_SUFFIX_RULES = Path(__file__).with_name('data') / 'public-suffix-rules.txt'
PUBLIC_SUFFIX_SHA256 = 'd212c108ac3bd2d1d9ec8997e18a430b7e31a55558ba68b969ae517093b048fb'
# URL host parsers can treat a bare hexadecimal prefix as a zero component.
IPV4_PART = re.compile(r'[0-9]+|0x[0-9a-f]*')


class PolicyError(ValueError):
    """The site configuration cannot be used as an Android policy."""


def validate_site_id(site: object) -> str:
    if not isinstance(site, str) or not re.fullmatch(SITE_RE, site):
        raise PolicyError('ID группы должен начинаться со строчной латинской буквы и содержать не более 24 букв и цифр')
    return site


@cache
def _suffix_rules() -> frozenset[str]:
    try:
        raw = PUBLIC_SUFFIX_RULES.read_bytes()
    except OSError as exc:
        raise PolicyError('Не удалось прочитать таблицу публичных суффиксов Android') from exc
    if hashlib.sha256(raw).hexdigest() != PUBLIC_SUFFIX_SHA256:
        raise PolicyError('Контрольная сумма таблицы публичных суффиксов Android не совпадает')
    lines = raw.decode('utf-8').splitlines()
    return frozenset(line for line in lines if line and not line.startswith('#'))


def _is_public_suffix(domain: str) -> bool:
    rules = _suffix_rules()
    parent = domain.partition('.')[2]
    return domain in rules or ('*.' + parent in rules and '!' + domain not in rules)


def normalize_policy_domain(raw: str) -> str:
    """Normalize a command argument with the desktop URL and IDNA rules."""
    try:
        domain = normalize_domain(raw)
    except ChangeError as exc:
        raise PolicyError(str(exc)) from None
    parts = domain.split('.')
    if len(parts) <= 4 and all(IPV4_PART.fullmatch(part) for part in parts):
        raise PolicyError('IP-адрес вместо домена не допускается')
    if domain.startswith('www.'):
        raise PolicyError('Укажите домен, который после нормализации не начинается с www.')
    try:
        for part in parts:
            if part.startswith('xn--'):
                unicode_label = part.encode('ascii').decode('idna')
                # Python's decoder allows unassigned code points. JDK IDNA2003 rejects them.
                if any(stringprep.in_table_a1(char) for char in unicode_label):
                    raise PolicyError('Метка IDNA содержит символы вне набора Unicode 3.2')
    except UnicodeError:
        raise PolicyError('Домен содержит неверную метку IDNA') from None
    if _is_public_suffix(domain):
        raise PolicyError('Укажите домен внутри зоны, а не публичный суффикс')
    return domain


def _validate_site(site_id: str, site: object, available_entries: int) -> int:
    if not isinstance(site, dict) or set(site) != SITE_FIELDS:
        raise PolicyError(f'Группа {site_id!r} содержит неизвестные или неполные поля')
    mode = site['mode']
    budget = site['budget_min']
    if mode not in ('limited', 'blocked'):
        raise PolicyError(f'Неизвестный режим группы {site_id!r}')
    if type(budget) is not int or not (budget == 0 if mode == 'blocked' else 1 <= budget <= 1440):
        raise PolicyError(f'Некорректный бюджет группы {site_id!r}')

    domains, exceptions = site['domains'], site['exceptions']
    if not isinstance(domains, list) or not domains or not isinstance(exceptions, list):
        raise PolicyError(f'Для группы {site_id!r} нужны списки доменов и исключений и хотя бы один домен')
    entries = len(domains) + len(exceptions)
    if entries > available_entries:
        raise PolicyError(f'Допускается не более {MAX_DOMAINS} доменов и исключений')
    for values in (domains, exceptions):
        for domain in values:
            try:
                normalized = normalize_policy_domain(domain)
            except PolicyError as exc:
                raise PolicyError(f'Не удалось использовать домен {repr(domain)[:100]} группы {site_id!r}. {exc}') from None
            if normalized != domain:
                raise PolicyError(f'Домен {repr(domain)[:100]} группы {site_id!r} должен быть нормализован')
    for exception in exceptions:
        if not any(exception == domain or exception.endswith('.' + domain) for domain in domains):
            raise PolicyError(f'Исключение {exception!r} за пределами доменов группы {site_id!r}')
    return entries


def canonical_policy_bytes(policy: object) -> bytes:
    """Validate the schema and return the UTF-8 bytes used by both clients."""
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise PolicyError('Политика содержит неизвестные или неполные поля')
    if type(policy['schema']) is not int or policy['schema'] != 1:
        raise PolicyError('Поддерживается только схема политики 1')
    if type(policy['reset_hour']) is not int or policy['reset_hour'] != 4:
        raise PolicyError('Android поддерживает только сброс бюджета в 04:00')
    minutes = policy['default_session_min']
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise PolicyError('Длительность отрезка по умолчанию должна быть целым числом от 1 до 1440')
    sites = policy['sites']
    if not isinstance(sites, dict) or len(sites) > MAX_SITES:
        raise PolicyError(f'Требуется объект с группами, максимум {MAX_SITES}')
    available_entries = MAX_DOMAINS
    for site_id, site in sites.items():
        validate_site_id(site_id)
        available_entries -= _validate_site(site_id, site, available_entries)

    encoded = json.dumps(policy, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_POLICY_BYTES:
        raise PolicyError('Размер политики превышает 64 КиБ')
    return encoded


def validate_policy(policy: object) -> dict:
    """Require a complete, normalized policy, preserving its supplied order."""
    canonical_policy_bytes(policy)
    return cast(dict, policy)


def policy_hash(policy: object) -> str:
    return hashlib.sha256(canonical_policy_bytes(policy)).hexdigest()


def export_sites(state: State) -> dict:
    """Return a detached, validated policy for desktop-to-Android import."""
    return validate_policy(state.export_sites())


def policy_diff(before: dict, after: dict) -> list[dict]:
    """Return changed values in path order, using None for absent groups."""
    validate_policy(before)
    validate_policy(after)
    changes = []

    def compare(path, previous, current):
        if previous != current:
            changes.append({'path': path, 'before': deepcopy(previous), 'after': deepcopy(current)})

    for field in ('default_session_min', 'reset_hour'):
        compare(field, before[field], after[field])
    before_sites, after_sites = before['sites'], after['sites']
    for site_id in sorted(before_sites.keys() | after_sites.keys()):
        previous, current = before_sites.get(site_id), after_sites.get(site_id)
        path = f'sites.{site_id}'
        if previous is None or current is None:
            compare(path, previous, current)
        else:
            for field in sorted(SITE_FIELDS):
                compare(f'{path}.{field}', previous[field], current[field])
    return changes
