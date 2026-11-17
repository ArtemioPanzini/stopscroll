import json
import unittest
from stopscroll.model import State, StateError, Session, Pending
from stopscroll.seed import initial_state, DOH_DOMAINS


class ModelTests(unittest.TestCase):
    def test_roundtrip(self):
        state = initial_state(1000, '2026-11-15')
        state.sessions['youtube'] = Session(100.0, 700.0, True)
        state.pending.append(Pending(1, {'kind': 'bonus', 'site': 'x', 'minutes': 10}, 2700.0, 'x: +10 мин'))
        self.assertEqual(State.from_dict(json.loads(json.dumps(state.to_dict()))), state)

    def test_seed_matches_spec(self):
        s = initial_state(1000, '2026-11-15')
        self.assertEqual({k for k, v in s.sites.items() if v.mode == 'blocked'}, {'twitch', 'liquipedia', 'kick', 'instagram'})
        self.assertEqual(s.sites['twitch'].domains, ['twitch.tv', 'ttvnw.net', 'jtvnw.net', 'twitchcdn.net', 'ext-twitch.tv'])
        self.assertEqual(s.sites['instagram'].domains, ['instagram.com', 'cdninstagram.com', 'instagr.am', 'ig.me'])
        self.assertEqual(s.sites['liquipedia'].domains, ['liquipedia.net'])
        self.assertEqual(s.sites['kick'].domains, ['kick.com'])
        yt = s.sites['youtube']
        self.assertEqual((yt.mode, yt.budget_min, yt.exceptions), ('limited', 45, ['accounts.youtube.com']))
        self.assertEqual(yt.domains, ['youtube.com', 'youtu.be', 'googlevideo.com', 'youtube-nocookie.com', 'youtubei.googleapis.com', 'youtube.googleapis.com'])
        self.assertEqual((s.sites['x'].domains, s.sites['x'].budget_min), (['x.com', 'twitter.com'], 10))
        self.assertEqual((s.delay_min, s.reset_hour, s.default_session_min), (45, 4, 15))
        self.assertEqual(s.doh_domains, ['use-application-dns.net', 'dns.google', 'cloudflare-dns.com', 'mozilla.cloudflare-dns.com', 'dns.quad9.net', 'doh.opendns.com', 'dns.nextdns.io', 'dns.adguard-dns.com'])

    def test_from_dict_rejects_other_version(self):
        d = initial_state(1000, '2026-11-15').to_dict()
        d['version'] = 2
        with self.assertRaises(StateError):
            State.from_dict(d)

    def test_from_dict_rejects_missing_fields(self):
        data = initial_state(1000, '2026-11-15').to_dict()
        for key in data.keys() - {'quarantine', 'telegram_channels'}:
            with self.subTest(key=key), self.assertRaises(StateError):
                State.from_dict({k: v for k, v in data.items() if k != key})

    def test_invalid_nested_state_rejected(self):
        for value in (None, [], {'youtube': {'mode': 'open'}}):
            data = initial_state(1000, '2026-11-15').to_dict()
            data['sites'] = value
            with self.subTest(value=value), self.assertRaises(StateError):
                State.from_dict(data)

    def test_seed_does_not_share_mutable_data(self):
        s = initial_state(1000, '2026-11-15')
        s.doh_domains.clear()
        s.sites['youtube'].domains.clear()
        self.assertTrue(initial_state(1000, '2026-11-15').sites['youtube'].domains)
        self.assertTrue(DOH_DOMAINS)

    def test_huge_integer_float_field_rejected(self):
        data = initial_state(1000, '2026-11-15').to_dict()
        data['boot_saved'] = 10 ** 400
        with self.assertRaises(StateError):
            State.from_dict(data)

    def test_old_state_load_preserves_custom_policy_sessions_and_queue(self):
        state = initial_state(1000, '2026-11-15')
        state.sites['youtube'].budget_min = 17
        state.sessions['youtube'] = Session(100, 700)
        state.spent_sec['youtube'] = 180
        state.pending.append(Pending(1, {'kind': 'uninstall'}, 50, 'remove'))
        data = state.to_dict()
        del data['quarantine']
        self.assertEqual(State.from_dict(data), state)

    def test_quarantine_roundtrip_and_invalid_metadata(self):
        from stopscroll.model import Quarantine
        state = initial_state(1000, '2026-11-15')
        state.quarantine['site_youtube4'] = Quarantine(['142.250.1.1'], 1234)
        self.assertEqual(State.from_dict(state.to_dict()), state)
        for value in (None, [], {'site_youtube4': {'addresses': 5, 'expires': 1234}}):
            data = state.to_dict()
            data['quarantine'] = value
            with self.assertRaises(StateError):
                State.from_dict(data)
