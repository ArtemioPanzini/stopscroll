"""Pure state transitions for sessions and daily budgets."""
from dataclasses import dataclass
from datetime import datetime, tzinfo
import math

from .day import day_key
from .model import Session, State

WARN_BEFORE_SEC = 120


class EngineError(Exception):
    """Ошибка команды, которую можно показать владельцу."""


@dataclass
class Event:
    kind: str
    site: str = ''
    text: str = ''


class Engine:
    def __init__(self, state: State, tz: tzinfo | None = None):
        self.state = state
        self.tz = tz

    def _site(self, site):
        if site not in self.state.sites:
            raise EngineError(f'Неизвестный сайт: {site}')
        return self.state.sites[site]

    def _elapsed(self, site, now):
        session = self.state.sessions.get(site)
        return max(0, now - session.segment_start) if session else 0

    def remaining_sec(self, site: str, now: float) -> int:
        policy = self._site(site)
        budget = (policy.budget_min + self.state.bonus_min.get(site, 0)) * 60
        return max(0, math.floor(budget - self.state.spent_sec.get(site, 0) - self._elapsed(site, now)))

    def open(self, site: str, minutes: int | None, now: float) -> dict:
        policy = self._site(site)
        if policy.mode != 'limited':
            raise EngineError(f'Сайт {site} закрыт без лимита')
        if minutes is None:
            minutes = self.state.default_session_min
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise EngineError('Укажите целое число минут от 1 до 1440')
        remaining = self.remaining_sec(site, now)
        session = self.state.sessions.get(site)
        extended = session is not None
        # The unused part of an existing session is already reserved.
        reserved = max(0, session.ends - now) if session else 0
        available = max(0, remaining - reserved)
        granted = min(minutes * 60, available)
        if granted <= 0:
            raise EngineError(f'Бюджет исчерпан или уже выделен. Обновление в {self.state.reset_hour:02d}:00; запросите добавку командой stopscroll request')
        if session:
            session.ends = max(now, session.ends) + granted
            session.warned = False
        else:
            session = Session(now, now + granted)
            self.state.sessions[site] = session
        return dict(ends=session.ends, granted_min=math.ceil(granted / 60),
                    capped=granted < minutes * 60, extended=extended)

    def close(self, site: str, now: float, reason: str = 'manual') -> list[Event]:
        self._site(site)
        session = self.state.sessions.get(site)
        if session is None:
            return []
        charge = math.ceil(self._elapsed(site, now) / 60) * 60
        self.state.spent_sec[site] = self.state.spent_sec.get(site, 0) + charge
        del self.state.sessions[site]
        reasons = {'manual': 'закрыто вручную', 'expired': 'время истекло', 'budget': 'дневной бюджет исчерпан'}
        return [Event('session_closed', site, f'{site}: {reasons.get(reason, "отрезок закрыт")}')]

    def tick(self, now: float, boot: float, boot_id: str) -> list[Event]:
        key = day_key(now, self.state.reset_hour, self.tz)
        if key > self.state.day:
            reset = datetime.fromisoformat(key).replace(hour=self.state.reset_hour, tzinfo=self.tz).timestamp()
            self.state.day = key
            self.state.spent_sec.clear()
            self.state.bonus_min.clear()
            for session in self.state.sessions.values():
                session.segment_start = max(session.segment_start, reset)
        events = []
        # Queue processing and boot-clock accounting are added by Task 3.
        for site, session in list(self.state.sessions.items()):
            if now >= session.ends:
                events.extend(self.close(site, now, 'expired'))
            elif self.remaining_sec(site, now) == 0:
                events.extend(self.close(site, now, 'budget'))
            elif session.ends - now <= WARN_BEFORE_SEC and not session.warned:
                session.warned = True
                events.append(Event('session_ending', site, f'{site}: отрезок скоро закончится'))
        return events

    def status(self, now: float) -> dict:
        blocked, limited = [], []
        for site, policy in self.state.sites.items():
            if policy.mode == 'blocked':
                blocked.append(site)
                continue
            session = self.state.sessions.get(site)
            spent = self.state.spent_sec.get(site, 0) + self._elapsed(site, now)
            limited.append(dict(site=site, open=session is not None,
                                ends=session.ends if session else None,
                                left_sec=max(0, math.ceil(session.ends - now)) if session else 0,
                                spent_min=math.ceil(spent / 60), budget_min=policy.budget_min,
                                bonus_min=self.state.bonus_min.get(site, 0)))
        return dict(blocked=blocked, limited=limited, pending=[],
                    delay_min=self.state.delay_min, reset_hour=self.state.reset_hour,
                    pause_until=self.state.pause_until)
