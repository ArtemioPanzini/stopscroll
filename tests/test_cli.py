import contextlib
import io
import unittest
from unittest.mock import patch
from datetime import timezone
from stopscroll import cli

class CLITests(unittest.TestCase):
    def test_request_table(self):
        rows = [(['status'], {'cmd':'status'}), (['open','youtube'], {'cmd':'open','site':'youtube','minutes':None}), (['open','youtube','20'], {'cmd':'open','site':'youtube','minutes':20}), (['close','youtube'], {'cmd':'close','site':'youtube'}), (['cancel','3'], {'cmd':'cancel','id':3}), (['doctor'], {'cmd':'doctor'})]
        changes = [('block twitch',dict(kind='set_mode',site='twitch',mode='blocked',budget_min=0)),('block games steam.com epic.com',dict(kind='add_site',site='games',mode='blocked',domains=['steam.com','epic.com'],budget_min=0)),('limit youtube 30',dict(kind='set_budget',site='youtube',minutes=30)),('delay 90',dict(kind='set_delay',minutes=90)),('request budget youtube 60',dict(kind='set_budget',site='youtube',minutes=60)),('request bonus youtube 30',dict(kind='bonus',site='youtube',minutes=30)),('request unblock twitch',dict(kind='remove_site',site='twitch')),('request unblock twitch twitch.tv',dict(kind='remove_domain',site='twitch',domain='twitch.tv')),('request limit twitch 20',dict(kind='set_mode',site='twitch',mode='limited',budget_min=20)),('request except youtube accounts.youtube.com',dict(kind='add_exception',site='youtube',domain='accounts.youtube.com')),('request delay 15',dict(kind='set_delay',minutes=15)),('request pause 120',dict(kind='pause',minutes=120)),('request uninstall',dict(kind='uninstall'))]
        rows += [(a.split(),dict(cmd='change',change=c)) for a,c in changes]
        for args, expected in rows:
            with self.subTest(args=args): self.assertEqual(cli.build_request(args),expected)
    def test_bad_usage_exits_2(self):
        for args in [[],['open'],['open','youtube','abc'],['request','boom'],['cancel','x'],['delay','0'],['limit','youtube','-1']]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm: cli.build_request(args)
            self.assertEqual(cm.exception.code,2)
    def test_existing(self):
        self.assertEqual(cli.build_request(['block','twitch','a.com'],['twitch'])['change'],dict(kind='add_domain',site='twitch',domain='a.com'))
    def test_confirmation(self):
        for a in ['block twitch','limit youtube 30','delay 90']: self.assertTrue(cli.needs_confirmation(a.split())); self.assertFalse(cli.needs_confirmation(a.split()+['--yes']))
        for a in ['status','open youtube','request pause 120']: self.assertFalse(cli.needs_confirmation(a.split()))
    def status(self):
        return dict(blocked=['twitch','liquipedia','kick','instagram'],limited=[dict(site='youtube',open=True,ends=53520,spent_min=24,budget_min=45,bonus_min=0),dict(site='x',open=False,ends=None,spent_min=0,budget_min=30,bonus_min=0)],pending=[dict(id=3,summary='youtube: бюджет 45 → 60 мин',eta=55200)],delay_min=45,reset_hour=4,pause_until=None)
    def test_golden(self):
        expected='Закрыто:      twitch  liquipedia  kick  instagram\n\nС лимитом:\n  youtube   открыт до 14:52 (осталось 11 мин)   сегодня 24 из 45 мин\n  x         закрыт                              сегодня  0 из 30 мин\n\nЖдут применения:\n  #3  youtube: бюджет 45 → 60 мин       применится в 15:20  (отмена: stopscroll cancel 3)\n\nЗадержка на ослабление: 45 мин.   Новый день в 04:00.'
        self.assertEqual(cli.render_status(self.status(),52860,timezone.utc),expected)
    def test_variants(self):
        s=self.status(); s['pending']=[]; s['pause_until']=57600; s['limited'][0]['bonus_min']=30
        result=cli.render_status(s,52860,timezone.utc)
        self.assertNotIn('Ждут применения',result); self.assertIn('45+30 мин',result); self.assertTrue(result.startswith('ПАУЗА до 16:00 — сайты открыты.'))
    def test_replies(self):
        self.assertIn('нет доступа',cli.render_reply({},dict(ok=False,error='нет доступа')))
        text=cli.render_reply(dict(cmd='open',site='youtube'),dict(ok=True,capped=True,granted_min=10,ends=53520),timezone.utc)
        self.assertIn('10 мин',text); self.assertIn('остаток бюджета',text)
        text=cli.render_reply(dict(cmd='change'),dict(ok=True,applied=False,id=3,eta=55200,summary='бюджет'),timezone.utc)
        for part in ['в очереди','15:20','stopscroll cancel 3']: self.assertIn(part,text)
    def test_unavailable(self):
        with patch.object(cli,'SOCKET','/nonexistent/stopscroll.sock'),contextlib.redirect_stderr(io.StringIO()) as out: self.assertEqual(cli.main([]),3)
        self.assertIn('недоступна',out.getvalue())
    def test_existing_multiple(self):
        replies=[dict(ok=True,status=self.status()),dict(ok=True,applied=True,summary='a'),dict(ok=True,applied=True,summary='b')]
        with patch.object(cli,'exchange',side_effect=replies) as ex,contextlib.redirect_stdout(io.StringIO()): self.assertEqual(cli.main(['block','twitch','a.com','b.com','--yes']),0)
        self.assertEqual([c.args[0]['change']['domain'] for c in ex.call_args_list[1:]],['a.com','b.com'])
    def test_nonterminal_refuses(self):
        with patch('sys.stdin.isatty',return_value=False),patch.object(cli,'exchange') as ex,contextlib.redirect_stderr(io.StringIO()): self.assertEqual(cli.main(['delay','90']),2)
        ex.assert_not_called()
    def test_doctor_failure(self):
        with patch.object(cli,'exchange',return_value=dict(ok=True,checks=[dict(ok=False,name='dns',detail='сбой')])),contextlib.redirect_stdout(io.StringIO()) as out: self.assertEqual(cli.main(['doctor']),1)
        self.assertIn('СБОЙ',out.getvalue())

    def test_remove_exception_command_and_confirmation(self):
        args = ['remove-exception', 'youtube', 'https://accounts.youtube.com/path']
        expected = dict(cmd='change', change=dict(kind='remove_exception', site='youtube', domain='accounts.youtube.com'))
        self.assertEqual(cli.build_request(args), expected)
        self.assertTrue(cli.needs_confirmation(args))
        self.assertFalse(cli.needs_confirmation(args+['--yes']))
        with patch.object(cli, 'exchange', return_value=dict(ok=True, applied=True, summary='removed')) as exchange, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args+['--yes']), 0)
        exchange.assert_called_once_with(expected)

    def test_remove_exception_interactive_confirmation(self):
        args = ['remove-exception', 'youtube', 'accounts.youtube.com']
        for answer, expected_code in [('y', 0), ('n', 2)]:
            with self.subTest(answer=answer), patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value=answer), patch.object(cli, 'exchange', return_value=dict(ok=True, applied=True, summary='removed')) as exchange, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(args), expected_code)
                self.assertIn('accounts.youtube.com', output.getvalue())
                self.assertEqual(exchange.call_count, int(answer=='y'))
