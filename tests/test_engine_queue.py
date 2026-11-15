import unittest
from datetime import datetime, timezone
from stopscroll.seed import initial_state
from stopscroll.engine import Engine, EngineError
from stopscroll.model import State
T = datetime(2026,11,15,12,tzinfo=timezone.utc).timestamp()
class QueueTests(unittest.TestCase):
    def setUp(self):
        self.st=initial_state(1000,'2026-11-15'); self.e=Engine(self.st,timezone.utc)
        self.loosen=dict(kind='set_budget',site='youtube',minutes=60)
        self.e.tick(T,100,'b')
    def test_immediate_and_queue(self):
        r=self.e.submit(dict(kind='set_budget',site='youtube',minutes=30),T)
        self.assertTrue(r['applied']); self.assertEqual(self.st.sites['youtube'].budget_min,30)
        r=self.e.submit(self.loosen,T)
        self.assertEqual((r['applied'],r['id'],r['eta']),(False,1,T+2700))
        self.assertEqual(self.e.status(T)['pending'],[dict(id=1,summary=r['summary'],eta=T+2700)])
    def test_boot_elapsed_and_persistence(self):
        self.e.submit(self.loosen,T)
        self.assertEqual(self.e.tick(T+10,2799,'b'),[])
        self.e=Engine(State.from_dict(self.st.to_dict()),timezone.utc)
        self.assertEqual([ev.kind for ev in self.e.tick(T+20,2801,'b')],['change_applied'])
        self.assertEqual(self.e.state.sites['youtube'].budget_min,60)
    def test_wall_clock_and_reboot(self):
        self.e.submit(self.loosen,T); self.e.tick(T+86400,110,'b')
        self.assertEqual(self.st.pending[0].remaining_sec,2690)
        self.e.tick(T+86401,5,'c'); self.assertEqual(self.st.pending[0].remaining_sec,2690)
    def test_delay_and_cancel(self):
        self.assertEqual(self.e.submit(dict(kind='set_delay',minutes=5),T)['eta'],T+2700)
        self.assertIsInstance(self.e.cancel(1),str)
        with self.assertRaises(EngineError): self.e.cancel(99)
    def test_stale_changes(self):
        self.e.submit(self.loosen,T); self.e.submit(dict(kind='set_budget',site='x',minutes=40),T)
        self.e.submit(dict(kind='set_mode',site='youtube',mode='blocked',budget_min=0),T)
        self.assertEqual([ev.kind for ev in self.e.tick(T+2700,2800,'b')],['change_dropped','change_applied'])
        self.assertEqual(self.st.sites['x'].budget_min,40)
    def test_closes_sessions(self):
        for change,reason in [(dict(kind='set_mode',site='youtube',mode='blocked',budget_min=0),'правил'),(dict(kind='set_budget',site='youtube',minutes=10),'бюджет')]:
            self.setUp(); self.e.open('youtube',30,T); self.st.spent_sec['youtube']=1200
            events=self.e.submit(change,T)['events']
            self.assertEqual(events[0].kind,'session_closed'); self.assertIn(reason,events[0].text)
    def test_uninstall(self):
        self.e.submit(dict(kind='uninstall'),T)
        self.assertEqual([ev.kind for ev in self.e.tick(T+2700,2800,'b')],['change_applied','uninstall'])
    def test_remove_open_site(self):
        self.e.open('youtube',15,T); self.e.submit(dict(kind='remove_site',site='youtube'),T)
        self.e.tick(T+1,2800,'b'); self.assertNotIn('youtube',self.st.sessions)

    def test_bonus_applies_on_new_day_and_pause_waits(self):
        self.e.submit(dict(kind='bonus',site='youtube',minutes=30),T)
        self.e.submit(dict(kind='pause',minutes=5),T)
        self.assertIsNone(self.st.pause_until)
        self.e.tick(T+86400,2800,'b')
        self.assertEqual(self.st.bonus_min['youtube'],30)
        self.assertEqual(self.st.pause_until,T+86400+300)
    def test_queue_copies_request_and_preserves_individual_delay(self):
        request=dict(self.loosen); self.e.submit(request,T); request['minutes']=90
        self.e.submit(dict(kind='set_delay',minutes=60),T)
        self.e.submit(dict(kind='bonus',site='x',minutes=10),T)
        self.assertEqual([p.remaining_sec for p in self.st.pending],[2700,3600])
        self.e.tick(T+2700,2800,'b')
        self.assertEqual(self.st.sites['youtube'].budget_min,60)
        self.assertEqual(self.st.pending[0].remaining_sec,900)
    def test_stale_removed_site_does_not_block_queue(self):
        self.e.submit(dict(kind='remove_site',site='youtube'),T)
        self.e.submit(self.loosen,T)
        self.e.submit(dict(kind='bonus',site='x',minutes=5),T)
        self.assertEqual([ev.kind for ev in self.e.tick(T+2700,2800,'b')],['change_applied','change_dropped','change_applied'])
        self.assertEqual(self.st.bonus_min['x'],5)
