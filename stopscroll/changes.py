"""Validation and application of owner-requested policy changes."""
import ipaddress
import re
from urllib.parse import urlsplit

from .model import Site, State

SITE_RE = r'^[a-z][a-z0-9]{0,23}$'


class ChangeError(Exception):
    """Некорректное или устаревшее изменение правил."""


def normalize_domain(raw: str) -> str:
    if not isinstance(raw, str) or not raw or any(c.isspace() for c in raw):
        raise ChangeError('Укажите корректный домен')
    try:
        parsed = urlsplit(raw if '://' in raw else '//' + raw)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError()
        parsed.port  # Reject invalid ports rather than silently discarding them.
        host = (parsed.hostname or '').rstrip('.').lower()
        if host.startswith('www.'):
            host = host[4:]
        host = host.encode('idna').decode('ascii')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ChangeError('IP-адрес вместо домена не допускается')
        labels = host.split('.')
        if len(host) > 253 or len(labels) < 2 or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in labels
        ):
            raise ValueError()
        return host
    except (ValueError, UnicodeError):
        raise ChangeError('Укажите корректный домен') from None


def _minutes(value):
    if type(value) is not int or not 1 <= value <= 1440:
        raise ChangeError('Укажите целое число минут от 1 до 1440')
    return value


def _validated(state, change):
    if not isinstance(change, dict):
        raise ChangeError('Некорректное изменение')
    c = dict(change)
    kind = c.get('kind')
    fields = {
        'add_site': {'site','mode','domains','budget_min'}, 'remove_site': {'site'},
        'add_domain': {'site','domain'}, 'remove_domain': {'site','domain'},
        'add_exception': {'site','domain'}, 'remove_exception': {'site','domain'},
        'set_mode': {'site','mode','budget_min'}, 'set_budget': {'site','minutes'},
        'bonus': {'site','minutes'}, 'set_delay': {'minutes'}, 'pause': {'minutes'}, 'uninstall': set(),
    }
    if not isinstance(kind, str) or kind not in fields or set(c) != fields[kind] | {'kind'}:
        raise ChangeError('Неизвестное или неполное изменение')
    site = c.get('site')
    if 'site' in c and (not isinstance(site,str) or not re.fullmatch(SITE_RE,site)):
        raise ChangeError('Имя сайта: латинская буква, затем до 23 букв или цифр')
    if 'site' in c and kind != 'add_site' and site not in state.sites:
        raise ChangeError(f'Неизвестный сайт: {site}')
    policy = state.sites.get(site)
    if 'minutes' in c:
        _minutes(c['minutes'])
    if kind in ('add_site','set_mode'):
        if c['mode'] not in ('limited','blocked'):
            raise ChangeError('Неизвестный режим сайта')
        if c['mode'] == 'limited':
            _minutes(c['budget_min'])
        elif type(c['budget_min']) is not int or c['budget_min'] != 0:
            raise ChangeError('Для закрытого сайта бюджет должен быть нулевым')
    if kind == 'add_site':
        if policy is not None:
            raise ChangeError('Сайт уже существует')
        if not isinstance(c['domains'],list) or not c['domains']:
            raise ChangeError('Укажите хотя бы один домен')
        c['domains'] = list(dict.fromkeys(normalize_domain(d) for d in c['domains']))
    if 'domain' in c:
        c['domain'] = normalize_domain(c['domain'])
        values = policy.exceptions if 'exception' in kind else policy.domains
        adding = kind.startswith('add_')
        if (c['domain'] in values) == adding:
            raise ChangeError('Домен уже добавлен' if adding else 'Домен отсутствует')
        if kind == 'remove_domain' and len(values) == 1:
            raise ChangeError('Нельзя убрать последний домен сайта')
    if kind in ('set_budget','bonus') and policy.mode != 'limited':
        raise ChangeError('Бюджет доступен только сайту с лимитом')
    if kind == 'set_mode' and c['mode'] == policy.mode:
        raise ChangeError('Режим уже установлен')
    if kind == 'set_budget' and c['minutes'] == policy.budget_min:
        raise ChangeError('Бюджет уже установлен')
    if kind == 'set_delay' and c['minutes'] == state.delay_min:
        raise ChangeError('Задержка уже установлена')
    return c


def classify(state: State, change: dict) -> str:
    c = _validated(state,change)
    kind = c['kind']
    tight = kind in ('add_site','add_domain','remove_exception')
    if kind == 'set_mode': tight = c['mode'] == 'blocked'
    if kind == 'set_budget': tight = c['minutes'] < state.sites[c['site']].budget_min
    if kind == 'set_delay': tight = c['minutes'] > state.delay_min
    return 'tighten' if tight else 'loosen'


def apply(state: State, change: dict, now: float) -> None:
    c = _validated(state,change)
    kind, site = c['kind'], c.get('site')
    if kind == 'add_site': state.sites[site] = Site(c['mode'],c['domains'],[],c['budget_min'])
    elif kind == 'remove_site': del state.sites[site]
    elif kind in ('add_domain','remove_domain','add_exception','remove_exception'):
        values = state.sites[site].exceptions if 'exception' in kind else state.sites[site].domains
        (values.append if kind.startswith('add_') else values.remove)(c['domain'])
    elif kind == 'set_mode':
        state.sites[site].mode = c['mode']; state.sites[site].budget_min = c['budget_min']
    elif kind == 'set_budget': state.sites[site].budget_min = c['minutes']
    elif kind == 'bonus': state.bonus_min[site] = state.bonus_min.get(site,0) + c['minutes']
    elif kind == 'set_delay': state.delay_min = c['minutes']
    elif kind == 'pause': state.pause_until = now + c['minutes'] * 60


def summarize(state: State, change: dict) -> str:
    c = _validated(state,change)
    kind, site = c['kind'], c.get('site')
    if kind == 'set_budget': return f'{site}: бюджет {state.sites[site].budget_min} → {c["minutes"]} мин'
    if kind == 'set_delay': return f'Задержка {state.delay_min} → {c["minutes"]} мин'
    if kind == 'pause': return f'Приостановить блокировку на {c["minutes"]} мин'
    if kind == 'uninstall': return 'Удалить систему'
    if kind == 'bonus': return f'{site}: добавка {c["minutes"]} мин'
    if kind == 'set_mode': return f'{site}: режим «{"закрыто" if c["mode"] == "blocked" else "с лимитом"}»'
    labels = {'add_site':'добавить сайт','remove_site':'убрать сайт','add_domain':'добавить домен','remove_domain':'убрать домен','add_exception':'добавить исключение','remove_exception':'убрать исключение'}
    return f'{site}: {labels[kind]}' + (f' {c["domain"]}' if 'domain' in c else '')
