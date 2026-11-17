import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from stopscroll.telegram import normalize_channel, validate_channel_id, publish_policy
from stopscroll.model import State, StateError, Session, Pending
from stopscroll.seed import initial_state
from stopscroll.engine import Engine, EngineError
from stopscroll.daemon import Daemon
from stopscroll.cli import build_request, needs_confirmation, render_status
from stopscroll import store


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.state = initial_state(1234, '2026-11-15')
        self.engine = Engine(self.state)

    def test_normalization(self):
        for raw in ('Topor', '@TOPOR', 'https://t.me/Topor', 't.me/Topor/123',
                    'https://telegram.me/Topor/', 'https://t.me/s/Topor/123?single', 't.me/s/Topor'):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_channel(raw), 'topor')

    def test_rejection(self):
        for raw in (None, 123, [], {}, '', '@', ' foo', 'foo bar', 't.me/+abcd',
                    't.me/joinchat/abc', 't.me/c/123/456', 'https://example.com/topor',
                    'https://user@t.me/topor', 'https://t.me:443/topor', 'tg://resolve?domain=topor',
                    't.me/topor/nope', 't.me/topor/123/456', 'foo-bar', 'топор'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_channel(raw)

    def test_ids(self):
        for value in ('1', str(2**52-1)):
            self.assertEqual(validate_channel_id(value), value)
        for value in (1, True, None, [], '0', '-100123', '01', '1.0', str(2**52), '9'*10000):
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                validate_channel_id(value)

    def test_old_state_lossless(self):
        self.state.sites['x'].budget_min = 10
        self.state.sessions['x'] = Session(10, 20)
        self.state.pending.append(Pending(1, {'kind':'bonus','site':'x','minutes':1}, 120, 'bonus'))
        data = self.state.to_dict()
        data.pop('telegram_channels'); data.pop('quarantine')
        migrated = State.from_dict(data)
        self.assertEqual(migrated, self.state)
        self.assertEqual(migrated.sites['x'].budget_min, 10)

    def test_invalid_state(self):
        for channels in ({'Topor':[]}, {'topor':['0']}, {'topor':[123]}, {'topor':'123'}):
            data = self.state.to_dict(); data['telegram_channels'] = channels
            with self.subTest(channels=channels), self.assertRaises(StateError):
                State.from_dict(data)

    def test_block_unblock_delay_cancel(self):
        self.assertTrue(self.engine.submit({'kind':'telegram_block','username':'@Topor'}, 0)['applied'])
        self.state.telegram_channels['topor'].append('123')
        result = self.engine.submit({'kind':'telegram_unblock','username':'t.me/topor'}, 0)
        self.assertFalse(result['applied'])
        self.engine.cancel(result['id'])
        self.assertEqual(self.state.telegram_channels, {'topor':['123']})
        self.engine.submit({'kind':'telegram_unblock','username':'topor'}, 0)
        self.engine.tick(0, 0, 'boot'); self.engine.tick(2699,2699,'boot')
        self.assertIn('topor', self.state.telegram_channels)
        self.engine.tick(2700,2700,'boot')
        self.assertEqual(self.state.telegram_channels, {})

    def test_change_shape(self):
        for change in ({'kind':'telegram_block','username':1}, {'kind':'telegram_block','username':'foo','ids':[]},
                       {'kind':'telegram_unblock','username':'foo'}):
            with self.subTest(change=change), self.assertRaises(EngineError):
                self.engine.submit(change, 0)

    def test_observation_auth_persistence_and_retry(self):
        system = Mock(); system.sync.return_value=[]; system.retry_quarantine.return_value=[]
        self.state.telegram_channels['topor']=['111']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            save = Mock(side_effect=[OSError('full'), None])
            daemon = Daemon(self.engine,system,save,lambda:[])
            req = dict(cmd='telegram-observe',username='TOPOR',id='222')
            self.assertFalse(daemon.handle(req, 9999, 0)['ok'])
            self.assertEqual(self.state.telegram_channels['topor'], ['111'])
            with self.assertLogs('stopscroll.daemon',level='ERROR'):
                self.assertFalse(daemon.handle(req,1234,0)['ok'])
            self.assertTrue(daemon._dirty)
            self.assertTrue(daemon.handle(req,0,0)['ok'])
            self.assertFalse(daemon._dirty)
            self.assertEqual(self.state.telegram_channels['topor'], ['111','222'])
            store.save(self.state,path)
            self.assertEqual(store.load(str(path)).telegram_channels,self.state.telegram_channels)
            self.assertEqual(system.sync.call_count,2)
            self.assertFalse(daemon.handle(dict(req,username='unknown'),1234,0)['ok'])
            self.assertFalse(daemon.handle(dict(req,id=123),1234,0)['ok'])

    def test_atomic_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'telegram.json'
            publish_policy({'topor':['222','111']}, path)
            before=path.read_bytes()
            self.assertEqual(json.loads(before), {'version':1,'channels':[{'username':'topor','ids':['111','222']}]})
            self.assertEqual(path.stat().st_mode & 0o777,0o644)
            with patch('stopscroll.telegram.os.replace',side_effect=OSError('failure')):
                with self.assertRaises(OSError): publish_policy({},path)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(Path(tmp).iterdir()),[path])
            publish_policy({},path)
            self.assertEqual(json.loads(path.read_text())['channels'],[])

    def test_cli(self):
        self.assertEqual(build_request(['telegram','block','t.me/Topor/1','--yes']),
                         {'cmd':'change','change':{'kind':'telegram_block','username':'topor'}})
        self.assertEqual(build_request(['request','telegram-unblock','@Topor'])['change'],
                         {'kind':'telegram_unblock','username':'topor'})
        self.assertTrue(needs_confirmation(['telegram','block','topor']))
        self.assertFalse(needs_confirmation(['telegram','block','topor','--yes']))
        self.state.telegram_channels['topor']=[]
        self.assertIn('@topor',render_status(self.engine.status(0),0))

    def test_publication_failure_degraded_and_tick_retry(self):
        from stopscroll.system import System
        from tests.test_system import FakeRunner
        with tempfile.TemporaryDirectory() as tmp:
            system = System(FakeRunner(tmp))
            path = system.runner.path('/etc/stopscroll/telegram.json')
            publish_policy({}, path)
            save = Mock()
            daemon = Daemon(self.engine, system, save, lambda: [])
            with patch('stopscroll.telegram.os.replace', side_effect=OSError('disk full')):
                with self.assertLogs('stopscroll.daemon', level='WARNING'):
                    result = daemon.handle({'cmd':'change', 'change':{'kind':'telegram_block', 'username':'topor'}}, 1234, 0)
            self.assertFalse(result['ok'])
            self.assertTrue(result['applied'])
            self.assertIn('Telegram', result['error'])
            self.assertEqual(json.loads(path.read_text())['channels'], [])
            pending_status = daemon.handle({'cmd':'status'}, 1234, 0)['status']
            self.assertTrue(pending_status['telegram_policy_pending'])
            self.assertIn('ошибка публикации', render_status(pending_status, 0))
            daemon.tick(1, 1, 'boot')
            self.assertEqual(json.loads(path.read_text())['channels'], [{'username':'topor','ids':[]}])
            self.assertFalse(daemon.handle({'cmd':'status'}, 1234, 1)['status']['telegram_policy_pending'])
            self.assertFalse(daemon._dirty)
            req = {'cmd':'telegram-observe', 'username':'topor', 'id':'123'}
            with patch('stopscroll.telegram.os.replace', side_effect=OSError('disk full')):
                with self.assertLogs('stopscroll.daemon', level='WARNING'):
                    self.assertFalse(daemon.handle(req, 1234, 1)['ok'])
            self.assertTrue(daemon.handle(req, 1234, 1)['ok'])
            self.assertEqual(self.state.telegram_channels['topor'], ['123'])
            self.assertEqual(json.loads(path.read_text())['channels'][0]['ids'], ['123'])

    def test_pause_preserves_channels(self):
        from stopscroll.system import System
        from tests.test_system import FakeRunner
        self.state.telegram_channels['topor']=['123']
        self.state.pause_until=100
        with tempfile.TemporaryDirectory() as tmp:
            system = System(FakeRunner(tmp))
            system.sync(self.state, 0)
            path=system.runner.path('/etc/stopscroll/telegram.json')
            self.assertEqual(json.loads(path.read_text())['channels'], [{'username':'topor','ids':['123']}])
