import unittest
from stopscroll import notify
class NotifyTests(unittest.TestCase):
    def test_format_each_kind(self):
        rows=[(dict(event='session_ending',site='youtube'),'youtube закроется через 2 минуты'),(dict(event='change_applied',text='правило'),'Применено: правило'),(dict(event='change_dropped',text='правило'),'Не применено: правило')]
        reasons={'expired':'время вышло','budget':'бюджет на сегодня исчерпан','manual':'закрыт вручную','rules':'изменились правила'}
        rows += [(dict(event='session_closed',site='youtube',reason=r),'youtube закрыт: '+t) for r,t in reasons.items()]
        rows += [(dict(event='session_closed',site='youtube',text='youtube: закрыто вручную'),'youtube закрыт: закрыт вручную')]
        for event,text in rows:
            with self.subTest(event=event): self.assertEqual(notify.format_event(event),('stopscroll',text))
    def test_unknown(self): self.assertIsNone(notify.format_event(dict(event='other')))
    def test_notify_and_reconnect(self):
        import contextlib
        import io
        from unittest.mock import MagicMock,patch
        sock=MagicMock()
        sock.makefile.return_value.__enter__.return_value=io.StringIO('{"ok":true}\n{"event":"session_ending","site":"youtube"}\n')
        with patch.object(notify.socket,'socket') as factory, patch.object(notify.subprocess,'run') as run, patch.object(notify.time,'sleep',side_effect=KeyboardInterrupt) as sleep:
            factory.return_value.__enter__.return_value=sock
            self.assertEqual(notify.main(),0)
        sock.sendall.assert_called_once_with(b'{"cmd":"subscribe"}\n')
        run.assert_called_once_with(['notify-send','-a','stopscroll','stopscroll','youtube закроется через 2 минуты'],check=False,timeout=10)
        sleep.assert_called_once_with(5)
    def test_unavailable_retries(self):
        import contextlib
        import io
        from unittest.mock import patch
        with patch.object(notify.socket,'socket',side_effect=OSError),patch.object(notify.time,'sleep',side_effect=KeyboardInterrupt) as sleep,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(notify.main(),0)
        sleep.assert_called_once_with(5)
