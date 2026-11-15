"""Persisted state and strict decoding of its versioned JSON schema."""
from dataclasses import asdict, dataclass, field, fields
import math
import types
from typing import get_args, get_origin, get_type_hints


class StateError(Exception):
    """Файл состояния повреждён или имеет неподдерживаемую версию."""


@dataclass
class Site:
    mode: str
    domains: list[str]
    exceptions: list[str]
    budget_min: int


@dataclass
class Session:
    segment_start: float
    ends: float
    warned: bool = False


@dataclass
class Pending:
    id: int
    change: dict
    remaining_sec: float
    summary: str


@dataclass
class State:
    owner_uid: int
    sites: dict[str, Site]
    doh_domains: list[str]
    day: str
    version: int = 1
    delay_min: int = 45
    reset_hour: int = 4
    default_session_min: int = 15
    spent_sec: dict[str, int] = field(default_factory=dict)
    bonus_min: dict[str, int] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    pending: list[Pending] = field(default_factory=list)
    next_id: int = 1
    pause_until: float | None = None
    boot_id: str = ''
    boot_saved: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'State':
        state = _decode(cls, d)
        if state.version != 1:
            raise StateError('Неподдерживаемая версия состояния')
        for site in state.sites.values():
            if site.mode not in ('blocked', 'limited'):
                raise StateError('Неизвестный режим сайта')
            if site.budget_min < 0 or (site.mode == 'blocked' and site.budget_min != 0):
                raise StateError('Некорректный бюджет сайта')
        return state


def _decode(schema, value):
    """Check every persisted field rather than silently using dataclass defaults."""
    origin = get_origin(schema)
    args = get_args(schema)
    if origin is types.UnionType:
        for member in args:
            try:
                return _decode(member, value)
            except StateError:
                pass
    elif origin is list:
        if isinstance(value, list):
            return [_decode(args[0], item) for item in value]
    elif origin is dict:
        if isinstance(value, dict):
            return {_decode(args[0], k): _decode(args[1], v) for k, v in value.items()}
    elif schema in (State, Site, Session, Pending):
        if isinstance(value, dict) and set(value) == {f.name for f in fields(schema)}:
            hints = get_type_hints(schema)
            return schema(**{k: _decode(hints[k], v) for k, v in value.items()})
    elif schema is float:
        if type(value) in (int, float) and math.isfinite(value):
            return value
    elif type(value) is schema:
        return value
    raise StateError('Неполные или некорректные данные состояния')
