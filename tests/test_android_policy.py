import hashlib
import unittest
from copy import deepcopy

from stopscroll.android_policy import (
    PolicyError,
    canonical_policy_bytes,
    export_sites,
    normalize_policy_domain,
    policy_diff,
    policy_hash,
    validate_policy,
    validate_site_id,
)
from stopscroll.model import Pending, Session
from stopscroll.seed import initial_state


def sample_policy():
    return {
        'schema': 1,
        'reset_hour': 4,
        'default_session_min': 15,
        'sites': {
            'video': {
                'mode': 'limited',
                'budget_min': 45,
                'domains': ['video.example.org', 'cdn.example.org'],
                'exceptions': ['account.video.example.org'],
            },
            'news': {
                'mode': 'blocked',
                'budget_min': 0,
                'domains': ['news.example.org'],
                'exceptions': [],
            },
        },
    }


class AndroidPolicyTests(unittest.TestCase):
    def test_export_configuration_only_and_copy_lists(self):
        state = initial_state(1001, '2026-11-17')
        state.default_session_min = 7
        state.spent_sec['youtube'] = 16
        state.bonus_min['youtube'] = 5
        state.sessions['youtube'] = Session(100, 200)
        state.pending.append(Pending(1, {'kind': 'remove_site', 'site': 'youtube'}, 12, 'queued'))
        state.telegram_channels['example'] = []
        before = state.to_dict()

        exported = export_sites(state)
        self.assertEqual(set(exported), {'schema', 'reset_hour', 'default_session_min', 'sites'})
        self.assertEqual(exported['reset_hour'], 4)
        self.assertEqual(exported['default_session_min'], 7)
        self.assertEqual(set(exported['sites']), set(state.sites))
        self.assertEqual(exported['sites']['youtube'], {
            'mode': 'limited',
            'budget_min': 45,
            'domains': before['sites']['youtube']['domains'],
            'exceptions': ['accounts.youtube.com'],
        })
        self.assertEqual(exported['sites']['twitch']['mode'], 'blocked')
        self.assertEqual(exported['sites']['twitch']['budget_min'], 0)
        exported['sites']['youtube']['exceptions'].clear()
        exported['sites']['youtube']['domains'].clear()
        self.assertEqual(state.to_dict(), before)

    def test_export_reports_unrepresentable_legacy_values(self):
        state = initial_state(1001, '2026-11-17')
        state.sites['youtube'].exceptions = ['unrelated.example.org']
        before = state.to_dict()
        with self.assertRaisesRegex(PolicyError, r'unrelated\.example\.org.*youtube'):
            export_sites(state)
        self.assertEqual(state.to_dict(), before)

    def test_command_domain_normalization_and_strict_site_id(self):
        self.assertEqual(normalize_policy_domain('https://www.BÜCHER.example.:443/page'),
                         'xn--bcher-kva.example')
        self.assertEqual(validate_site_id('site42'), 'site42')
        for value in ('Site42', 'site-42', '', '0site', 'site42\n', 'a' * 25, 42):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                validate_site_id(value)

    def test_canonical_json_hash_and_array_order(self):
        policy = sample_policy()
        expected = (
            b'{"default_session_min":15,"reset_hour":4,"schema":1,"sites":'
            b'{"news":{"budget_min":0,"domains":["news.example.org"],"exceptions":[],"mode":"blocked"},'
            b'"video":{"budget_min":45,"domains":["video.example.org","cdn.example.org"],'
            b'"exceptions":["account.video.example.org"],"mode":"limited"}}}'
        )
        self.assertEqual(canonical_policy_bytes(policy), expected)
        self.assertEqual(policy_hash(policy), hashlib.sha256(expected).hexdigest())
        reordered = dict(reversed(tuple(policy.items())))
        reordered['sites'] = dict(reversed(tuple(policy['sites'].items())))
        self.assertEqual(policy_hash(reordered), policy_hash(policy))
        policy['sites']['video']['domains'].reverse()
        self.assertNotEqual(policy_hash(policy), hashlib.sha256(expected).hexdigest())
        self.assertEqual(policy_diff(policy, policy), [])

    def test_policy_shape_modes_and_integer_ranges(self):
        for field, value in (
            ('schema', True), ('schema', 2), ('schema', 1.0), ('reset_hour', 3),
            ('reset_hour', 4.0), ('default_session_min', True), ('default_session_min', 0),
            ('default_session_min', 1441), ('sites', []), ('owner_uid', 1001),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(PolicyError):
                validate_policy(sample_policy() | {field: value})
        for field, value in (
            ('budget_min', True), ('budget_min', 0), ('budget_min', 1441),
            ('mode', 'open'), ('mode', 'blocked'), ('domains', []),
            ('domains', ('video.example.org',)), ('spent_sec', 12), ('exceptions', None),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(PolicyError):
                policy = sample_policy()
                policy['sites']['video'][field] = value
                validate_policy(policy)
        policy = sample_policy()
        del policy['sites']['video']['exceptions']
        with self.assertRaises(PolicyError):
            validate_policy(policy)
        policy = sample_policy()
        policy['sites']['news']['budget_min'] = 1
        with self.assertRaises(PolicyError):
            validate_policy(policy)
        validate_policy(sample_policy() | {'sites': {}})

    def test_wire_domains_have_to_be_normalized(self):
        for value in (
            'https://video.example.org', 'VIDEO.example.org', 'video.example.org.',
            'www.video.example.org', 'bücher.example.org', '*.example.org',
            'org', '127.0.0.1', '127.000.0.1', '2001:db8::1',
            'invalid_.example.org', '-invalid.example.org', 'bad..example.org',
            'video.example.org\n', 'xn--invalidpunycode0.example', '',
        ):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                policy = sample_policy()
                policy['sites']['news']['domains'] = [value]
                validate_policy(policy)
        policy = sample_policy()
        policy['sites']['news']['domains'] = ['xn--bcher-kva.example', 'xn--bcher-kva.example']
        self.assertEqual(validate_policy(policy), policy)

    def test_ace_labels_use_the_unicode_3_2_idna_repertoire(self):
        for domain in ('xn--e28h.example', 'xn--ls8h.example', '😀.example', '💩.example'):
            with self.subTest(domain=domain), self.assertRaisesRegex(PolicyError, 'IDNA'):
                normalize_policy_domain(domain)
        for domain in ('xn--bcher-kva.example', 'xn--caf-dma.example', 'xn--r8jz45g.example'):
            with self.subTest(domain=domain):
                self.assertEqual(normalize_policy_domain(domain), domain)

    def test_alternate_numeric_ipv4_spellings_are_not_domains(self):
        for domain in ('127.1', '0177.0.0.1', '0x7f.0.0.1', '0x7f.1', '127.0.1',
                       '127.0x', '0x.1', '0.0.0.0x'):
            with self.subTest(domain=domain), self.assertRaisesRegex(PolicyError, 'IP'):
                normalize_policy_domain(domain)
        for domain in ('site.123', 'a.0x1', '0x7f.example', '0x.example'):
            with self.subTest(domain=domain):
                self.assertEqual(normalize_policy_domain(domain), domain)

    def test_exception_lies_within_its_group(self):
        for exception in ('notvideo.example.org', 'example.org', 'news.example.org'):
            with self.subTest(exception=exception), self.assertRaises(PolicyError):
                policy = sample_policy()
                policy['sites']['video']['exceptions'] = [exception]
                validate_policy(policy)
        policy = sample_policy()
        policy['sites']['video']['exceptions'] = [
            'video.example.org', 'deep.account.video.example.org',
        ]
        validate_policy(policy)

    def test_pinned_guava_public_suffix_rules(self):
        for suffix in ('co.uk', 'github.io', 'arbitrary.ck', 'xn--55qx5d.cn'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(PolicyError, 'публичный суффикс'):
                normalize_policy_domain(suffix)
        for domain in ('example.co.uk', 'repo.github.io', 'example.arbitrary.ck',
                       'city.kawasaki.jp', 'site.unknowntld', 'example.xn--55qx5d.cn'):
            with self.subTest(domain=domain):
                self.assertEqual(normalize_policy_domain(domain), domain)

    def test_policy_count_and_byte_limits_are_inclusive(self):
        group = {'mode': 'blocked', 'budget_min': 0, 'domains': ['site.example'], 'exceptions': []}
        policy = sample_policy() | {'sites': {f's{i}': group for i in range(100)}}
        validate_policy(policy)
        policy['sites']['s100'] = group
        with self.assertRaisesRegex(PolicyError, '100'):
            validate_policy(policy)

        policy['sites'] = {'s': group | {'domains': ['site.example'] * 999, 'exceptions': ['a.site.example']}}
        validate_policy(policy)
        policy['sites']['s']['exceptions'].append('a.site.example')
        with self.assertRaisesRegex(PolicyError, '1000'):
            validate_policy(policy)

        long_domain = ('a' * 63 + '.') * 3 + 'example'
        group = group | {'domains': [long_domain] * 323}
        policy['sites'] = {'s': group}
        final_length = 65536 - len(canonical_policy_bytes(policy)) - 3
        group['domains'].append(('b' * 63 + '.') * 2 + 'b' * (final_length - 128))
        self.assertEqual(len(canonical_policy_bytes(policy)), 65536)
        group['domains'][-1] += 'b'
        with self.assertRaisesRegex(PolicyError, '64'):
            validate_policy(policy)

    def test_diff_is_ordered_and_contains_before_and_after_values(self):
        before = sample_policy()
        after = deepcopy(before)
        after['default_session_min'] = 20
        after['sites']['blog'] = {'mode': 'blocked', 'budget_min': 0,
                                  'domains': ['blog.example.org'], 'exceptions': []}
        del after['sites']['news']
        after['sites']['video']['budget_min'] = 30
        after['sites']['video']['domains'].reverse()

        diff = policy_diff(before, after)
        self.assertEqual(diff, [
            {'path': 'default_session_min', 'before': 15, 'after': 20},
            {'path': 'sites.blog', 'before': None, 'after': after['sites']['blog']},
            {'path': 'sites.news', 'before': before['sites']['news'], 'after': None},
            {'path': 'sites.video.budget_min', 'before': 45, 'after': 30},
            {'path': 'sites.video.domains',
             'before': ['video.example.org', 'cdn.example.org'],
             'after': ['cdn.example.org', 'video.example.org']},
        ])
        diff[1]['after']['domains'].clear()
        self.assertEqual(after['sites']['blog']['domains'], ['blog.example.org'])
        self.assertEqual(policy_diff(before, dict(reversed(tuple(before.items())))), [])
