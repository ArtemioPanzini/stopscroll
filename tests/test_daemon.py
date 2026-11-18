import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from stopscroll.daemon import Daemon, bootstrap
from stopscroll.engine import Engine
from stopscroll.model import Pending, Session
from stopscroll.seed import initial_state
from stopscroll import store

T = 1794744000

class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.state = initial_state(os.getuid(), '2026-11-15')
        self.system = Mock()
        self.system.sync_browser.return_value = []
        self.system.sync.return_value = []
        self.system.retry_quarantine.return_value = []
        self.save = Mock()
        self.d = Daemon(Engine(self.state), self.system, self.save, lambda: [])
    def call(self, **req):
        return self.d.handle(req, os.getuid(), T)
    def test_browser_heartbeat_publishes_even_without_state_events(self):
        self.d.tick(T, 100, 'boot')
        self.d.tick(T+1, 101, 'boot')
        self.assertEqual(self.system.sync_browser.call_count, 2)
        self.system.sync_browser.assert_called_with(self.state, T+1)
    def test_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'state')
            self.assertEqual(bootstrap(path, 1000, T).sites['twitch'].mode, 'blocked')
            with self.assertRaises(SystemExit): bootstrap(path, None, T)
            store.save(self.state,path); store.save(self.state,path)
            Path(path).write_text('{bad')
            self.assertEqual(bootstrap(path,None,T).owner_uid,os.getuid())
    def test_access_and_status(self):
        self.assertFalse(self.d.handle({'cmd':'status'},99999,T)['ok'])
        self.assertTrue(self.d.handle({'cmd':'status'},0,T)['ok'])
        self.assertEqual(self.call(cmd='status')['status']['delay_min'],45)
    def test_export_sites_is_authenticated_and_read_only(self):
        self.state.owner_uid = 1001
        self.state.spent_sec['youtube'] = 120
        self.state.bonus_min['youtube'] = 4
        self.state.sessions['youtube'] = Session(T, T + 120)
        self.state.pending.append(Pending(1, {'kind': 'remove_site', 'site': 'youtube'}, 30, 'queued'))
        self.state.telegram_channels['example'] = []
        before = self.state.to_dict()
        request = {'cmd': 'export-sites'}

        self.assertEqual(self.d.handle(request, 1002, T), {'ok': False, 'error': 'нет доступа'})
        owner = self.d.handle(request, 1001, T)
        self.assertEqual(owner, self.d.handle(request, 0, T))
        self.assertEqual(set(owner), {'ok', 'policy'})
        self.assertTrue(owner['ok'])
        policy = owner['policy']
        self.assertEqual(set(policy), {'schema', 'reset_hour', 'default_session_min', 'sites'})
        self.assertEqual(policy['sites'], before['sites'])
        policy['sites']['youtube']['domains'].clear()
        self.assertEqual(self.state.to_dict(), before)
        self.save.assert_not_called()
        self.assertEqual(self.system.mock_calls, [])
    def test_export_sites_preserves_legacy_values_for_import_validation(self):
        self.state.sites['youtube'].domains.append('https://www.example.org/path')
        before = self.state.to_dict()
        reply = self.call(cmd='export-sites')
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['policy']['sites'], before['sites'])
        self.assertEqual(self.state.to_dict(), before)
        self.save.assert_not_called()
        self.assertEqual(self.system.mock_calls, [])
    def test_open_close(self):
        self.assertTrue(self.call(cmd='open',site='youtube',minutes=10)['ok'])
        self.save.assert_called_once(); self.system.sync.assert_called_once()
        self.call(cmd='close',site='youtube')
        self.system.queue_quarantine.assert_called_once_with(self.state, 'youtube')
    def test_expiry(self):
        self.call(cmd='open',site='youtube',minutes=10)
        events=self.d.tick(T+600,600,'boot')
        self.assertEqual(events[0]['event'],'session_closed')
        self.system.queue_quarantine.assert_called_with(self.state, 'youtube')
    def test_queue_cancel_uninstall(self):
        result=self.call(cmd='change',change={'kind':'uninstall'})
        self.assertFalse(result['applied'])
        self.assertTrue(self.call(cmd='cancel',id=result['id'])['ok'])
        self.call(cmd='change',change={'kind':'uninstall'})
        self.d.tick(T,0,'boot'); self.d.tick(T+2700,2700,'boot')
        self.system.uninstall.assert_called_once()
    def test_invalid(self):
        for req in [None,'x',[],{}, {'cmd':'boom'}, {'cmd':'open'}, {'cmd':'open','site':[], 'minutes':10}, {'cmd':'open','site':'youtube','minutes':'ten'}, {'cmd':'cancel','id':'one'}, {'cmd':'change','change':5}]:
            self.assertFalse(self.d.handle(req,os.getuid(),T)['ok'])
        self.assertIn('закрыт',self.call(cmd='open',site='twitch')['error'])
    def test_monotonic_housekeeping(self):
        self.call(cmd='change',change={'kind':'uninstall'})
        self.d.tick(T,100,'boot')
        self.save.reset_mock(); self.system.sync.reset_mock()
        self.d.tick(T-3600,160,'boot')
        self.save.assert_called_once(); self.system.sync.assert_called_once()
    def test_eventless_changes_wait_for_minute(self):
        self.d.tick(T,100,'boot'); self.system.sync.reset_mock()
        self.state.pause_until=T+1
        self.d.tick(T+2,102,'boot')
        self.system.sync.assert_not_called()
        self.d.tick(T+60,160,'boot'); self.system.sync.assert_called_once()

class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_socket_bad_json_and_roundtrip(self):
        state=initial_state(os.getuid(),'2026-11-15')
        system=Mock(); system.sync_browser.return_value=[]; system.sync.return_value=[]; system.retry_quarantine.return_value=[]
        d=Daemon(Engine(state),system,Mock(),lambda:[])
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'socket')
            task=asyncio.create_task(d.serve(path))
            try:
                for _ in range(100):
                    if Path(path).exists(): break
                    await asyncio.sleep(.01)
                self.assertEqual(Path(path).stat().st_mode & 0o777,0o666)
                for line,ok in [(b'{oops\n',False),(b'{"cmd":"status"}\n',True),
                                (b'{"cmd":"export-sites"}\n',True)]:
                    reader,writer=await asyncio.open_unix_connection(path)
                    writer.write(line); await writer.drain()
                    reply = json.loads(await reader.readline())
                    self.assertEqual(reply['ok'],ok)
                    if b'export-sites' in line:
                        self.assertEqual(reply['policy'], state.export_sites())
                    writer.close(); await writer.wait_closed()
                reader,writer=await asyncio.open_unix_connection(path)
                writer.write(b'{"cmd":"subscribe"}\n'); await writer.drain()
                self.assertTrue(json.loads(await reader.readline())['ok'])
                d.handle({'cmd':'open','site':'youtube','minutes':10},os.getuid(),T)
                d.handle({'cmd':'close','site':'youtube'},os.getuid(),T)
                event=json.loads(await asyncio.wait_for(reader.readline(),1))
                self.assertEqual(event['event'],'session_closed')
                self.assertIn('вручную',event['text'])
                writer.close(); await writer.wait_closed()
                state.owner_uid=99999
                if os.getuid()!=0:
                    reader,writer=await asyncio.open_unix_connection(path)
                    writer.write(b'{"cmd":"export-sites"}\n'); await writer.drain()
                    self.assertEqual(json.loads(await reader.readline())['error'],'нет доступа')
                    writer.close(); await writer.wait_closed()
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task

class PersistenceTests(unittest.TestCase):
    setUp = DaemonTests.setUp
    call = DaemonTests.call
    def test_failed_save_is_reported_and_retried_without_queue(self):
        self.d.tick(T, 0, 'boot')
        self.save.side_effect = [OSError('disk full'), None]
        reply = self.call(cmd='change', change={'kind':'set_budget', 'site':'youtube', 'minutes':20})
        self.assertFalse(reply['ok'])
        self.assertIn('сохран', reply['error'])
        self.assertEqual(self.state.sites['youtube'].budget_min, 20)
        self.assertFalse(self.state.pending)
        self.d.tick(T+60, 60, 'boot')
        self.assertEqual(self.save.call_count, 2)
        self.assertFalse(self.d._dirty)

    def test_failed_close_save_keeps_dns_tightening_and_retries(self):
        self.call(cmd='open', site='youtube', minutes=10)
        self.d.tick(T, 0, 'boot')
        self.save.reset_mock()
        self.save.side_effect = OSError('disk full')
        reply = self.call(cmd='close', site='youtube')
        self.assertFalse(reply['ok'])
        self.assertNotIn('youtube', self.state.sessions)
        self.system.queue_quarantine.assert_called_once_with(self.state, 'youtube')
        self.assertTrue(self.d._dirty)
        self.save.side_effect = None
        self.d.tick(T+60, 60, 'boot')
        self.assertFalse(self.d._dirty)
        self.assertEqual(self.save.call_count, 2)

    def test_startup_with_events_reconciles_once(self):
        from unittest.mock import patch
        from stopscroll.daemon import main
        from stopscroll.model import Session
        self.state.sessions['youtube'] = Session(T-100, T-1)
        with tempfile.TemporaryDirectory() as tmp, \
                patch('stopscroll.daemon.STATE', str(Path(tmp)/'state.json')), \
                patch('stopscroll.daemon.OWNER_FILE', str(Path(tmp)/'owner')), \
                patch('stopscroll.daemon.bootstrap', return_value=self.state), \
                patch('stopscroll.daemon._clock', return_value=(T, 100, 'boot')), \
                patch('stopscroll.daemon.System', return_value=self.system), \
                patch('stopscroll.daemon.store.save'):
            self.assertEqual(main(['--init']), 0)
        self.system.sync.assert_called_once()
