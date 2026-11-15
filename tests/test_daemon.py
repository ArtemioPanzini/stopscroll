import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from stopscroll.daemon import Daemon, bootstrap
from stopscroll.engine import Engine
from stopscroll.seed import initial_state
from stopscroll import store

T = 1794744000

class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.state = initial_state(os.getuid(), '2026-11-15')
        self.system = Mock()
        self.system.sync.return_value = []
        self.save = Mock()
        self.d = Daemon(Engine(self.state), self.system, self.save, lambda: [])
    def call(self, **req):
        return self.d.handle(req, os.getuid(), T)
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
    def test_open_close(self):
        self.assertTrue(self.call(cmd='open',site='youtube',minutes=10)['ok'])
        self.save.assert_called_once(); self.system.sync.assert_called_once()
        self.call(cmd='close',site='youtube')
        self.system.quarantine.assert_called_once_with('youtube')
    def test_expiry(self):
        self.call(cmd='open',site='youtube',minutes=10)
        events=self.d.tick(T+600,600,'boot')
        self.assertEqual(events[0]['event'],'session_closed')
        self.system.quarantine.assert_called_with('youtube')
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
        system=Mock(); system.sync.return_value=[]
        d=Daemon(Engine(state),system,Mock(),lambda:[])
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'socket')
            task=asyncio.create_task(d.serve(path))
            try:
                for _ in range(100):
                    if Path(path).exists(): break
                    await asyncio.sleep(.01)
                self.assertEqual(Path(path).stat().st_mode & 0o777,0o666)
                for line,ok in [(b'{oops\n',False),(b'{"cmd":"status"}\n',True)]:
                    reader,writer=await asyncio.open_unix_connection(path)
                    writer.write(line); await writer.drain()
                    self.assertEqual(json.loads(await reader.readline())['ok'],ok)
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
                    writer.write(b'{"cmd":"status"}\n'); await writer.drain()
                    self.assertEqual(json.loads(await reader.readline())['error'],'нет доступа')
                    writer.close(); await writer.wait_closed()
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
