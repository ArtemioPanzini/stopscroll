from datetime import datetime, timezone
import unittest
from stopscroll.engine import Engine, EngineError, WARN_BEFORE_SEC
from stopscroll.seed import initial_state

T = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
RESET = T + 16 * 3600


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.st = initial_state(1000, '2026-11-15')
        self.e = Engine(self.st, timezone.utc)

    def test_open_and_early_close_charges_elapsed_rounded_up(self):
        self.assertEqual(self.e.open('youtube', 20, T), dict(ends=T+1200, granted_min=20, capped=False, extended=False))
        events = self.e.close('youtube', T+170)
        self.assertEqual(self.st.spent_sec['youtube'], 180)
        self.assertEqual([(e.kind, e.site) for e in events], [('session_closed', 'youtube')])
        self.assertIn('вручную', events[0].text)
        self.assertEqual(self.e.close('youtube', T+180), [])

    def test_open_default_is_15_min(self):
        self.assertEqual(self.e.open('youtube', None, T)['ends'], T+900)

    def test_open_capped_by_remaining(self):
        self.st.spent_sec['youtube'] = 35*60
        self.assertEqual(self.e.open('youtube', 20, T), dict(ends=T+600, granted_min=10, capped=True, extended=False))

    def test_open_refuses_when_exhausted(self):
        self.st.spent_sec['youtube'] = 45*60
        with self.assertRaisesRegex(EngineError, '04:00'):
            self.e.open('youtube', 5, T)

    def test_open_invalid_site(self):
        for site in ('twitch', 'nope'):
            with self.subTest(site=site), self.assertRaises(EngineError):
                self.e.open(site, 5, T)

    def test_open_rejects_bad_minutes(self):
        for minutes in (0, -5, 1441, '5', 1.5, True):
            with self.subTest(minutes=minutes), self.assertRaises(EngineError):
                self.e.open('youtube', minutes, T)
        self.assertEqual(self.st.sessions, {})

    def test_reopen_extends(self):
        self.e.open('youtube', 10, T)
        self.assertEqual(self.e.open('youtube', 5, T+60), dict(ends=T+900, granted_min=5, capped=False, extended=True))

    def test_extend_capped_by_remaining(self):
        self.e.open('youtube', 40, T)
        self.assertEqual(self.e.open('youtube', 20, T+60), dict(ends=T+2700, granted_min=5, capped=True, extended=True))

    def test_sites_have_separate_budgets_and_bonus(self):
        self.st.bonus_min['youtube'] = 5
        self.e.open('youtube', 20, T)
        self.assertEqual(self.e.remaining_sec('youtube', T+61), 3000-61)
        self.assertEqual(self.e.remaining_sec('x', T+61), 1800)

    def test_warning_fires_once(self):
        self.assertEqual(WARN_BEFORE_SEC, 120)
        self.e.open('youtube', 10, T)
        self.assertEqual([e.kind for e in self.e.tick(T+480, 0, 'b')], ['session_ending'])
        self.assertEqual(self.e.tick(T+490, 0, 'b'), [])

    def test_expiry_closes_and_charges(self):
        self.e.open('youtube', 10, T)
        events = self.e.tick(T+600, 0, 'b')
        self.assertEqual([e.kind for e in events], ['session_closed'])
        self.assertIn('истекло', events[0].text)
        self.assertEqual(self.st.spent_sec['youtube'], 600)
        self.assertEqual(self.st.sessions, {})

    def test_budget_closes_before_session_end(self):
        self.e.open('youtube', 20, T)
        self.st.sites['youtube'].budget_min = 5
        events = self.e.tick(T+300, 0, 'b')
        self.assertIn('бюджет', events[0].text)
        self.assertEqual(self.st.sessions, {})

    def test_overrun_while_daemon_down_is_charged(self):
        self.e.open('youtube', 20, T)
        self.e.tick(T+1500, 0, 'b')
        self.assertEqual(self.st.spent_sec['youtube'], 1500)

    def test_day_rollover_resets_usage(self):
        self.st.spent_sec['youtube'] = 2700
        self.st.bonus_min['youtube'] = 10
        self.e.tick(RESET, 0, 'b')
        self.assertEqual((self.st.day, self.st.spent_sec, self.st.bonus_min), ('2026-11-16', {}, {}))

    def test_session_across_rollover_charges_only_new_day_part(self):
        self.e.open('youtube', 20, RESET-600)
        self.e.tick(RESET+60, 0, 'b')
        self.assertEqual(self.st.sessions['youtube'].segment_start, RESET)
        self.e.close('youtube', RESET+180)
        self.assertEqual(self.st.spent_sec['youtube'], 180)

    def test_clock_back_does_not_reset_day(self):
        self.st.spent_sec['youtube'] = 600
        self.e.tick(T-86400, 0, 'b')
        self.assertEqual((self.st.day, self.st.spent_sec), ('2026-11-15', {'youtube': 600}))

    def test_clock_back_does_not_go_negative(self):
        self.e.open('youtube', 10, T)
        self.assertEqual(self.e.remaining_sec('youtube', T-300), 2700)
        self.e.close('youtube', T-300)
        self.assertEqual(self.st.spent_sec['youtube'], 0)

    def test_revisiting_reset_after_clock_back_does_not_refresh_budget(self):
        self.e.tick(RESET, 0, 'b')
        self.st.spent_sec['youtube'] = 600
        self.e.tick(RESET-60, 0, 'b')
        self.e.tick(RESET+60, 0, 'b')
        self.assertEqual(self.st.spent_sec['youtube'], 600)

    def test_clock_back_status_keeps_nonnegative_spending(self):
        self.e.open('youtube', 10, T)
        self.assertEqual(self.e.status(T-300)['limited'][0]['spent_min'], 0)
        self.assertEqual(self.e.tick(T-300, 0, 'b'), [])

    def test_status_shape(self):
        self.st.spent_sec['youtube'] = 23*60
        self.e.open('youtube', 12, T)
        self.assertEqual(self.e.status(T+1), {
            'blocked': ['twitch', 'liquipedia', 'kick', 'instagram'],
            'limited': [dict(site='youtube', open=True, ends=T+720, left_sec=719, spent_min=24, budget_min=45, bonus_min=0),
                        dict(site='x', open=False, ends=None, left_sec=0, spent_min=0, budget_min=30, bonus_min=0)],
            'pending': [], 'delay_min': 45, 'reset_hour': 4, 'pause_until': None})
