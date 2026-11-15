"""Desktop notification subscriber; reconnects without changing daemon policy."""
import json
import socket
import subprocess
import sys
import time
from .daemon import SOCKET

REASONS={'expired':'время вышло','budget':'бюджет на сегодня исчерпан','manual':'закрыт вручную','rules':'изменились правила'}
TEXT_REASONS={'время истекло':'expired','дневной бюджет исчерпан':'budget','закрыто вручную':'manual','закрыто из-за изменения правил':'rules'}


def format_event(event):
    kind=event.get('event')
    site=event.get('site','')
    text=event.get('text','')
    if kind=='session_ending': body=f'{site} закроется через 2 минуты'
    elif kind=='session_closed':
        reason=event.get('reason') or TEXT_REASONS.get(text.removeprefix(site+': ')) or text
        body=f"{site} закрыт: {REASONS.get(reason,text or 'отрезок закрыт')}"
    elif kind=='change_applied': body='Применено: '+text
    elif kind=='change_dropped': body='Не применено: '+text
    else: return None
    return 'stopscroll',body


def main():
    try:
        while True:
            try:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                    sock.settimeout(10)
                    sock.connect(SOCKET)
                    sock.sendall(b'{"cmd":"subscribe"}\n')
                    with sock.makefile('r',encoding='utf-8') as stream:
                        reply=json.loads(stream.readline(1024*1024))
                        if not isinstance(reply,dict) or not reply.get('ok'):
                            raise ValueError('Подписка отклонена')
                        sock.settimeout(None)
                        for line in stream:
                            event=json.loads(line)
                            if not isinstance(event,dict): continue
                            message=format_event(event)
                            if message:
                                try:
                                    subprocess.run(['notify-send','-a','stopscroll',*message],check=False,timeout=10)
                                except (OSError,subprocess.TimeoutExpired):
                                    print('Не удалось показать уведомление stopscroll',file=sys.stderr)
            except (OSError,ValueError,UnicodeError,TypeError):
                print('Связь со службой потеряна; повтор через 5 секунд',file=sys.stderr)
            time.sleep(5)
    except KeyboardInterrupt:
        return 0


if __name__=='__main__':
    raise SystemExit(main())
