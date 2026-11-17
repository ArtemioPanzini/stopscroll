"""Root signer for isolated fixtures only; never uses the live state/key."""
import json
from pathlib import Path
import sys
import time
from stopscroll.browser import sign_policy
fixture = Path(sys.argv[1])
assert str(fixture).startswith('/tmp/stopscroll-browser-test-')
last = None
producer_token = None

def produce_revisions(now):
    from unittest.mock import Mock
    from stopscroll.seed import initial_state
    from stopscroll.daemon import Daemon
    from stopscroll.engine import Engine
    from stopscroll.browser import browser_policy
    values=[]
    def publish(state, evaluated):
        values.append(sign_policy(browser_policy(state,evaluated)))
        return []
    system=Mock()
    system.sync_browser.side_effect=publish
    system.sync.side_effect=publish
    system.retry_quarantine.return_value=[]
    system.telegram_policy_dirty=False
    state=initial_state(1000,'1970-01-01')
    daemon=Daemon(Engine(state),system,Mock(),lambda:[])
    assert daemon.handle({'cmd':'open','site':'youtube','minutes':1},0,now)['ok']
    values.clear()
    daemon.tick(now,100,'fixture-boot')
    assert daemon.handle({'cmd':'open','site':'youtube','minutes':1},0,now)['ok']
    return values[0],values[-1]

while True:
    try:
        command = json.loads((fixture/'control.json').read_text())
        mode = command.get('mode','signed')
        if mode == 'signed':
            now = time.time()
            last = sign_policy(dict(version=1,issued_at=now,lease_until=now+4,
                allowed_until=command.get('deadline',0),domains=command.get('domains',[
                'youtube.com','youtu.be','googlevideo.com','youtube-nocookie.com',
                'youtubei.googleapis.com','youtube.googleapis.com']),
                exceptions=command.get('exceptions',['accounts.youtube.com'])))
        elif mode == 'producer':
            if command['token'] != producer_token:
                producer_token=command['token']
                first,last=produce_revisions(time.time())
                (fixture/'producer-evidence.json').write_text(json.dumps([first,last]))
                (fixture/'policy/browser.json').write_text(json.dumps(first))
                time.sleep(.5)
        elif mode == 'passthrough':
            last = command['message']
        elif mode != 'freeze':
            raise ValueError('unknown mode')
        target = fixture/'policy/browser.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(last))
        temporary.replace(target)
    except (OSError, ValueError, KeyError):
        pass
    time.sleep(.2)
