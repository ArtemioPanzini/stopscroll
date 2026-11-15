import unittest
from stopscroll.seed import initial_state
from stopscroll.changes import ChangeError, normalize_domain, classify, apply, summarize

class ChangesTests(unittest.TestCase):
    def setUp(self): self.st = initial_state(1000, '2026-11-15')
    def test_normalize(self):
        for raw, want in [('https://www.Twitch.tv/directory','twitch.tv'),('Kick.com.','kick.com'),('m.youtube.com:443','m.youtube.com'),('пример.рф','xn--e1afmkfd.xn--p1ai')]:
            self.assertEqual(normalize_domain(raw), want)
    def test_reject_domains(self):
        for raw in ['', 'com','1.2.3.4','a b.com','x'*254,'https://a.com:bad','a..com']:
            with self.subTest(raw=raw), self.assertRaises(ChangeError): normalize_domain(raw)
    def test_classify_table(self):
        rows = [ ('add_site',dict(site='new',mode='blocked',domains=['new.com'],budget_min=0),'tighten'), ('remove_site',dict(site='youtube'),'loosen'), ('add_domain',dict(site='youtube',domain='new.com'),'tighten'), ('remove_domain',dict(site='youtube',domain='youtu.be'),'loosen'), ('add_exception',dict(site='youtube',domain='music.youtube.com'),'loosen'), ('remove_exception',dict(site='youtube',domain='accounts.youtube.com'),'tighten'), ('set_mode',dict(site='youtube',mode='blocked',budget_min=0),'tighten'), ('set_mode',dict(site='twitch',mode='limited',budget_min=20),'loosen'), ('set_budget',dict(site='youtube',minutes=30),'tighten'), ('set_budget',dict(site='youtube',minutes=60),'loosen'), ('bonus',dict(site='youtube',minutes=30),'loosen'), ('set_delay',dict(minutes=60),'tighten'), ('set_delay',dict(minutes=5),'loosen'), ('pause',dict(minutes=10),'loosen'), ('uninstall',{},'loosen') ]
        for kind, fields, want in rows:
            with self.subTest(kind=kind, fields=fields): self.assertEqual(classify(self.st,dict(kind=kind,**fields)),want)
    def test_invalid_changes(self):
        for change in [dict(kind='boom'),dict(kind='set_budget',site='nope',minutes=10),dict(kind='set_budget',site='youtube',minutes=45),dict(kind='add_site',site='My Site',mode='blocked',domains=['a.com'],budget_min=0),dict(kind='add_site',site='twitch',mode='blocked',domains=['a.com'],budget_min=0),dict(kind='remove_domain',site='liquipedia',domain='liquipedia.net')]:
            with self.subTest(change=change), self.assertRaises(ChangeError): classify(self.st,change)
        for value in [0,-1,'5',True,None]:
            with self.assertRaises(ChangeError): classify(self.st,dict(kind='pause',minutes=value))
    def test_apply_bonus_and_summary(self):
        apply(self.st,dict(kind='bonus',site='youtube',minutes=30),100)
        self.assertEqual(self.st.bonus_min['youtube'],30)
        self.assertEqual(summarize(self.st,dict(kind='set_budget',site='youtube',minutes=60)), 'youtube: бюджет 45 → 60 мин')
