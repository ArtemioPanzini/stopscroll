"""Pure rendering of the DNS policy and the private output firewall."""
from .model import State

DNS_PORT = 5354
QUARANTINE_SEC = 120
TABLE = 'stopscroll'
RESOLV_FILE = '/run/NetworkManager/no-stub-resolv.conf'


def set_names(site: str) -> tuple[str, str]:
    return f'site_{site}4', f'site_{site}6'


def _site_blocked(state: State, site: str, now: float) -> bool:
    if state.pause_until is not None and state.pause_until > now:
        return False
    session = state.sessions.get(site)
    return state.sites[site].mode == 'blocked' or session is None or session.ends <= now


def blocked_domains(state: State, now: float) -> list[str]:
    domains = set(state.doh_domains)
    for name, site in state.sites.items():
        if _site_blocked(state, name, now):
            domains.update(site.domains)
    return sorted(domains)


def render_dnsmasq(state: State, now: float) -> str:
    lines = [f'port={DNS_PORT}', 'listen-address=127.0.0.1', 'bind-interfaces',
             'no-hosts', f'resolv-file={RESOLV_FILE}', 'cache-size=1000']
    lines.extend(f'address=/{domain}/' for domain in blocked_domains(state, now))
    exceptions = {domain for name, site in state.sites.items()
                  if _site_blocked(state, name, now) for domain in site.exceptions}
    lines.extend(f'server=/{domain}/#' for domain in sorted(exceptions))
    for name, site in sorted(state.sites.items()):
        if site.mode == 'limited':
            ipv4, ipv6 = set_names(name)
            lines.append(f'nftset=/{"/".join(site.domains)}/4#inet#{TABLE}#{ipv4},6#inet#{TABLE}#{ipv6}')
    return '\n'.join(lines) + '\n'


def render_nft(state: State, dns_uid: int, resolved_uid: int) -> str:
    # `add` keeps existing sets and elements; only our chain is rebuilt.
    lines = [f'add table inet {TABLE}']
    for version in (4, 6):
        lines.append(f'add set inet {TABLE} quarantine{version} {{ type ipv{version}_addr; flags timeout; }}')
    for name, site in sorted(state.sites.items()):
        if site.mode == 'limited':
            for version, set_name in zip((4, 6), set_names(name)):
                lines.append(f'add set inet {TABLE} {set_name} {{ type ipv{version}_addr; }}')
    lines.extend([
        f'add chain inet {TABLE} output {{ type filter hook output priority filter; policy accept; }}',
        f'flush chain inet {TABLE} output',
        f'add rule inet {TABLE} output oifname "lo" accept',
    ])
    for family, version in (('ip', 4), ('ip6', 6)):
        rule = f'add rule inet {TABLE} output {family} daddr @quarantine{version}'
        lines.extend([f'{rule} meta l4proto tcp reject with tcp reset', f'{rule} reject'])
    lines.append(f'add rule inet {TABLE} output meta l4proto {{ tcp, udp }} th dport {{ 53, 853 }} meta skuid != {{ {dns_uid}, {resolved_uid} }} reject')
    return '\n'.join(lines) + '\n'
