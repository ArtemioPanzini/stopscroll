"""Pure state transitions for sessions and daily budgets."""
from dataclasses import dataclass
from datetime import datetime, tzinfo
import math

from .day import day_key
from copy import deepcopy

from . import changes
from .model import Pending, Session, State

WARN_BEFORE_SEC = 120
ROUTINE_TICK_GAP_SEC = 2.0


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
        self._last_tick = None

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
        if site not in self.state.sessions:
            self._site(site)
        session = self.state.sessions.get(site)
        if session is None:
            return []
        charge = math.ceil(self._elapsed(site, now) / 60) * 60
        self.state.spent_sec[site] = self.state.spent_sec.get(site, 0) + charge
        del self.state.sessions[site]
        reasons = {'manual': 'закрыто вручную', 'expired': 'время истекло', 'budget': 'дневной бюджет исчерпан', 'rules': 'закрыто из-за изменения правил'}
        return [Event('session_closed', site, f'{site}: {reasons.get(reason, "отрезок закрыт")}')]

    def tick(self, now: float, boot: float, boot_id: str) -> list[Event]:
        # Only observations from this live Engine can establish routine ticking.
        # Restart, missed ticks and clock jumps retain actual overrun accounting.
        previous = self._last_tick
        routine = False
        if previous is not None:
            wall_gap, boot_gap = now - previous[0], boot - previous[1]
            routine = (boot_id == previous[2] and
                       0 <= boot_gap <= ROUTINE_TICK_GAP_SEC and
                       0 <= wall_gap <= ROUTINE_TICK_GAP_SEC and
                       math.isclose(wall_gap, boot_gap, abs_tol=0.25))
        self._last_tick = (now, boot, boot_id)
        key = day_key(now, self.state.reset_hour, self.tz)
        if key > self.state.day:
            reset = datetime.fromisoformat(key).replace(hour=self.state.reset_hour, tzinfo=self.tz).timestamp()
            self.state.day = key
            self.state.spent_sec.clear()
            self.state.bonus_min.clear()
            for session in self.state.sessions.values():
                session.segment_start = max(session.segment_start, reset)
        events = []
        elapsed = max(0, boot - self.state.boot_saved) if boot_id == self.state.boot_id else 0
        self.state.boot_id, self.state.boot_saved = boot_id, boot
        for pending in self.state.pending:
            pending.remaining_sec = max(0, pending.remaining_sec - elapsed)
        for pending in sorted(list(self.state.pending), key=lambda p: p.id):
            if pending.remaining_sec > 0:
                continue
            self.state.pending.remove(pending)
            try:
                changes.apply(self.state, pending.change, now)
            except changes.ChangeError as exc:
                events.append(Event('change_dropped', text=f'Изменение #{pending.id} снято: {exc}'))
            else:
                events.append(Event('change_applied', text=pending.summary))
                events.extend(self._after_change(pending.change, now))
        for site, session in list(self.state.sessions.items()):
            if now >= session.ends:
                charged_until = session.ends if routine and previous[0] <= session.ends else now
                events.extend(self.close(site, charged_until, 'expired'))
            elif self.remaining_sec(site, now) == 0:
                events.extend(self.close(site, now, 'budget'))
            elif session.ends - now <= WARN_BEFORE_SEC and not session.warned:
                session.warned = True
                events.append(Event('session_ending', site, f'{site}: отрезок скоро закончится'))
        return events

    def _after_change(self, change, now):
        events = []
        for site in list(self.state.sessions):
            policy = self.state.sites.get(site)
            if policy is None or policy.mode == 'blocked':
                events.extend(self.close(site, now, 'rules'))
            elif self.remaining_sec(site, now) == 0:
                events.extend(self.close(site, now, 'budget'))
        if change['kind'] == 'uninstall':
            events.append(Event('uninstall'))
        return events

    def submit(self, change: dict, now: float) -> dict:
        try:
            classification = changes.classify(self.state, change)
            summary = changes.summarize(self.state, change)
            if classification == 'tighten':
                changes.apply(self.state, change, now)
                return dict(applied=True, summary=summary, events=self._after_change(change, now))
        except changes.ChangeError as exc:
            raise EngineError(str(exc)) from None
        pending = Pending(self.state.next_id, deepcopy(change), self.state.delay_min * 60, summary)
        self.state.next_id += 1
        self.state.pending.append(pending)
        return dict(applied=False, id=pending.id, eta=now + pending.remaining_sec, summary=summary)

    def cancel(self, change_id: int) -> str:
        for pending in self.state.pending:
            if type(change_id) is int and pending.id == change_id:
                self.state.pending.remove(pending)
                return pending.summary
        raise EngineError('Нет ожидающего изменения с таким номером')

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
        return dict(blocked=blocked, limited=limited, pending=[dict(id=p.id, summary=p.summary, eta=now + p.remaining_sec)
                            for p in sorted(self.state.pending, key=lambda p: p.id)],
                    delay_min=self.state.delay_min, reset_hour=self.state.reset_hour,
                    pause_until=self.state.pause_until)
