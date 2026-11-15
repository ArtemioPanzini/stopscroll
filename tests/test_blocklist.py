import unittest

from stopscroll.blocklist import blocked_domains, render_dnsmasq, render_nft, set_names
from stopscroll.model import Session
from stopscroll.seed import initial_state

T = 1000.0
YOUTUBE_NFTSET_LINE = 'nftset=/youtube.com/youtu.be/googlevideo.com/youtube-nocookie.com/youtubei.googleapis.com/youtube.googleapis.com/4#inet#stopscroll#site_youtube4,6#inet#stopscroll#site_youtube6'
NFT_TEXT = '''add table inet stopscroll
add set inet stopscroll quarantine4 { type ipv4_addr; flags timeout; }
add set inet stopscroll quarantine6 { type ipv6_addr; flags timeout; }
add set inet stopscroll site_x4 { type ipv4_addr; }
add set inet stopscroll site_x6 { type ipv6_addr; }
add set inet stopscroll site_youtube4 { type ipv4_addr; }
add set inet stopscroll site_youtube6 { type ipv6_addr; }
add chain inet stopscroll output { type filter hook output priority filter; policy accept; }
flush chain inet stopscroll output
add rule inet stopscroll output oifname "lo" accept
add rule inet stopscroll output ip daddr @quarantine4 meta l4proto tcp reject with tcp reset
add rule inet stopscroll output ip daddr @quarantine4 reject
add rule inet stopscroll output ip6 daddr @quarantine6 meta l4proto tcp reject with tcp reset
add rule inet stopscroll output ip6 daddr @quarantine6 reject
add rule inet stopscroll output meta l4proto { tcp, udp } th dport { 53, 853 } meta skuid != { 991, 992 } reject
'''


class BlocklistTests(unittest.TestCase):
    def setUp(self):
        self.st = initial_state(1000, '2026-11-15')

    def test_blocked_when_no_session(self):
        domains = blocked_domains(self.st, T)
        for domain in ('twitch.tv', 'youtube.com', 'x.com', 'dns.google', 'use-application-dns.net'):
            self.assertIn(domain, domains)
        self.assertEqual(domains, sorted(set(domains)))

    def test_open_session_unblocks_only_that_site(self):
        self.st.sessions['youtube'] = Session(T, T + 60)
        domains = blocked_domains(self.st, T)
        self.assertNotIn('youtube.com', domains)
        self.assertNotIn('googlevideo.com', domains)
        self.assertIn('x.com', domains)

    def test_pause_lifts_sites_but_not_doh(self):
        self.st.pause_until = T + 60
        self.assertEqual(blocked_domains(self.st, T), sorted(self.st.doh_domains))

    def test_pause_expired_blocks_again(self):
        for end in (T - 1, T):
            self.st.pause_until = end
            self.assertIn('twitch.tv', blocked_domains(self.st, T))

    def test_dnsmasq_header_and_lines(self):
        text = render_dnsmasq(self.st, T)
        self.assertTrue(text.startswith('port=5354\nlisten-address=127.0.0.1\nbind-interfaces\nno-hosts\nresolv-file=/run/NetworkManager/no-stub-resolv.conf\ncache-size=1000\n'))
        self.assertIn('address=/twitch.tv/\n', text)
        self.assertIn('server=/accounts.youtube.com/#\n', text)
        self.assertIn(YOUTUBE_NFTSET_LINE + '\n', text)
        lines = text.splitlines()[6:]
        addresses = ['address=/' + d + '/' for d in blocked_domains(self.st, T)]
        self.assertEqual(lines[:len(addresses)], addresses)
        self.assertEqual(lines[len(addresses)], 'server=/accounts.youtube.com/#')
        self.assertTrue(lines[len(addresses) + 1].startswith('nftset=/x.com/twitter.com/'))

    def test_dnsmasq_keeps_nftset_when_open(self):
        self.st.sessions['youtube'] = Session(T, T + 60)
        text = render_dnsmasq(self.st, T)
        self.assertNotIn('address=/youtube.com/\n', text)
        self.assertIn(YOUTUBE_NFTSET_LINE + '\n', text)

    def test_exception_only_for_blocked_site(self):
        self.st.sessions['youtube'] = Session(T, T + 60)
        self.assertNotIn('server=/accounts.youtube.com/#\n', render_dnsmasq(self.st, T))
        self.st.sessions.clear()
        self.st.pause_until = T + 60
        self.assertNotIn('server=/accounts.youtube.com/#\n', render_dnsmasq(self.st, T))

    def test_nft_exact_text(self):
        self.assertEqual(render_nft(self.st, 991, 992), NFT_TEXT)

    def test_set_names(self):
        self.assertEqual(set_names('x'), ('site_x4', 'site_x6'))

    def test_blocklist_deduplicates(self):
        self.st.sites['x'].domains.extend(['x.com', 'dns.google'])
        self.st.doh_domains.append('dns.google')
        domains = blocked_domains(self.st, T)
        self.assertEqual(domains, sorted(set(domains)))

    def test_expired_session_blocks(self):
        self.st.sessions['youtube'] = Session(T - 60, T)
        self.assertIn('youtube.com', blocked_domains(self.st, T))


if __name__ == '__main__':
    unittest.main()
