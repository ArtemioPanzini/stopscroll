"""Initial policy approved in specification sections 5.4 and 5.9."""
from .model import Site, State

DOH_DOMAINS = [
    'use-application-dns.net', 'dns.google', 'cloudflare-dns.com',
    'mozilla.cloudflare-dns.com', 'dns.quad9.net', 'doh.opendns.com',
    'dns.nextdns.io', 'dns.adguard-dns.com',
]


def initial_state(owner_uid: int, day: str) -> State:
    return State(owner_uid=owner_uid, day=day, doh_domains=list(DOH_DOMAINS), sites={
        'twitch': Site('blocked', ['twitch.tv', 'ttvnw.net', 'jtvnw.net', 'twitchcdn.net', 'ext-twitch.tv'], [], 0),
        'liquipedia': Site('blocked', ['liquipedia.net'], [], 0),
        'kick': Site('blocked', ['kick.com'], [], 0),
        'instagram': Site('blocked', ['instagram.com', 'cdninstagram.com', 'instagr.am', 'ig.me'], [], 0),
        'youtube': Site('limited', ['youtube.com', 'youtu.be', 'googlevideo.com', 'youtube-nocookie.com', 'youtubei.googleapis.com', 'youtube.googleapis.com'], ['accounts.youtube.com'], 45),
        'x': Site('limited', ['x.com', 'twitter.com'], [], 10),
    })
