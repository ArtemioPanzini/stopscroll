"""Russian owner command line for the daemon's local JSON protocol."""
from datetime import datetime
import json
import math
import re
import socket
import sys
import time

from .daemon import SOCKET
from .telegram import normalize_channel
from .changes import normalize_domain, ChangeError, SITE_RE

HELP = '''Использование: stopscroll [status | doctor | open САЙТ [МИНУТЫ] | close САЙТ |
  telegram block ИМЯ [--yes] | request telegram-unblock ИМЯ |
  block САЙТ [ДОМЕН ...] | remove-exception САЙТ ДОМЕН |
  limit САЙТ МИНУТЫ | delay МИНУТЫ | cancel НОМЕР |
  request budget САЙТ МИНУТЫ | request bonus САЙТ МИНУТЫ |
  request unblock САЙТ [ДОМЕН] | request limit САЙТ МИНУТЫ |
  request except САЙТ ДОМЕН | request delay МИНУТЫ |
  request pause МИНУТЫ | request uninstall | android --help]
--yes — подтвердить немедленное изменение без вопроса.'''


def _bad(message='Неверные аргументы команды'):
    print(message + '\n' + HELP, file=sys.stderr)
    raise SystemExit(2)


def _number(text, maximum=1440):
    try:
        value = int(text)
        if not 1 <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        _bad('Укажите положительное целое число' + (f' от 1 до {maximum}' if maximum == 1440 else ''))


def build_request(argv, known=None):
    args = list(argv)
    if '--yes' in args:
        if args.count('--yes') > 1 or not args or args[0] not in ('block','limit','delay','remove-exception','telegram'):
            _bad()
        args.remove('--yes')
    if not args:
        _bad()
    cmd, *a = args
    change = None
    if cmd in ('status','doctor') and not a:
        return dict(cmd=cmd)
    if cmd == 'telegram' and len(a) == 2 and a[0] == 'block':
        change = dict(kind='telegram_block', username=a[1])
    elif cmd == 'open' and len(a) in (1,2):
        req = dict(cmd=cmd,site=a[0],minutes=_number(a[1]) if len(a)==2 else None)
    elif cmd == 'close' and len(a)==1:
        req = dict(cmd=cmd,site=a[0])
    elif cmd == 'cancel' and len(a)==1:
        return dict(cmd=cmd,id=_number(a[0],2**63-1))
    elif cmd == 'block' and a:
        if len(a)==1:
            change=dict(kind='set_mode',site=a[0],mode='blocked',budget_min=0)
        elif a[0] in (known or []):
            if len(a)!=2:
                _bad('Добавляйте домены существующего сайта отдельными запросами')
            change=dict(kind='add_domain',site=a[0],domain=a[1])
        else:
            change=dict(kind='add_site',site=a[0],domains=a[1:],mode='blocked',budget_min=0)
    elif cmd=='remove-exception' and len(a)==2:
        change=dict(kind='remove_exception',site=a[0],domain=a[1])
    elif cmd=='limit' and len(a)==2:
        change=dict(kind='set_budget',site=a[0],minutes=_number(a[1]))
    elif cmd=='delay' and len(a)==1:
        change=dict(kind='set_delay',minutes=_number(a[0]))
    elif cmd=='request' and a:
        sub,*b=a
        if sub == 'telegram-unblock' and len(b) == 1:
            change = dict(kind='telegram_unblock', username=b[0])
        elif sub in ('budget','bonus','limit') and len(b)==2:
            change=dict(kind={'budget':'set_budget','bonus':'bonus','limit':'set_mode'}[sub],site=b[0])
            change.update(dict(mode='limited',budget_min=_number(b[1])) if sub=='limit' else dict(minutes=_number(b[1])))
        elif sub=='unblock' and len(b) in (1,2):
            change=dict(kind='remove_site' if len(b)==1 else 'remove_domain',site=b[0])
            if len(b)==2: change['domain']=b[1]
        elif sub=='except' and len(b)==2:
            change=dict(kind='add_exception',site=b[0],domain=b[1])
        elif sub in ('delay','pause') and len(b)==1:
            change=dict(kind='set_delay' if sub=='delay' else 'pause',minutes=_number(b[0]))
        elif sub=='uninstall' and not b:
            change=dict(kind='uninstall')
        else:
            _bad()
    else:
        _bad()
    if change is not None:
        req=dict(cmd='change',change=change)
    target=change if change is not None else req
    if 'site' in target and not re.fullmatch(SITE_RE,target['site']):
        _bad('Некорректное имя сайта')
    try:
        if 'username' in target: target['username'] = normalize_channel(target['username'])
        if 'domain' in target: target['domain']=normalize_domain(target['domain'])
        if 'domains' in target: target['domains']=[normalize_domain(d) for d in target['domains']]
    except (ChangeError, ValueError) as exc:
        _bad(str(exc))
    return req


def needs_confirmation(argv):
    return bool(argv) and argv[0] in ('block','limit','delay','remove-exception','telegram') and '--yes' not in argv


def _hm(stamp,tz):
    return datetime.fromtimestamp(stamp,tz).strftime('%H:%M')


def render_status(status,now,tz=None):
    lines=[]
    if status.get('pause_until',0) and status['pause_until']>now:
        lines += [f"ПАУЗА до {_hm(status['pause_until'],tz)} — сайты открыты."]
    lines += ['Закрыто:      '+'  '.join(status['blocked']),'','С лимитом:']
    for site in status['limited']:
        state=(f"открыт до {_hm(site['ends'],tz)} (осталось {max(0,math.ceil((site['ends']-now)/60))} мин)" if site['open'] else 'закрыт')
        budget=str(site['budget_min'])+(f"+{site['bonus_min']}" if site.get('bonus_min') else '')
        # Preserve the published status layout, including its closed-row spacing.
        width=max(36,len(state)+3)
        lines.append(f"  {site['site']:<10}{state:<{width}}сегодня {site['spent_min']:2} из {budget} мин")
    if status.get('telegram_policy_pending'):
        lines += ['', 'Telegram: ошибка публикации политики; повторная попытка ожидается.']
    if status.get('telegram_channels'):
        lines += ['', 'Telegram: ' + '  '.join('@' + name for name in status['telegram_channels'])]
    if status['pending']:
        lines += ['','Ждут применения:']
        for p in status['pending']:
            lines.append(f"  #{p['id']}  {p['summary']}       применится в {_hm(p['eta'],tz)}  (отмена: stopscroll cancel {p['id']})")
    lines += ['',f"Задержка на ослабление: {status['delay_min']} мин.   Новый день в {status['reset_hour']:02}:00."]
    return '\n'.join(lines)


def render_reply(req,reply,tz=None):
    if not reply.get('ok'):
        return reply.get('error','Служба отказала в выполнении команды')
    cmd=req['cmd']
    if cmd=='status': return render_status(reply['status'],time.time(),tz)
    if cmd=='doctor': return '\n'.join(f"{'ok' if c['ok'] else 'СБОЙ'}  {c['name']}  {c['detail']}" for c in reply['checks'])
    if cmd=='open':
        text=f"{req['site']}: открыт на {reply['granted_min']} мин, до {_hm(reply['ends'],tz)}"
        return text+(' (остаток бюджета)' if reply.get('capped') else '')
    if cmd=='change':
        if reply['applied']: return 'Применено: '+reply['summary']
        return f"Изменение #{reply['id']} в очереди: {reply['summary']}; применится в {_hm(reply['eta'],tz)} (отмена: stopscroll cancel {reply['id']})"
    if cmd=='close': return f"{req['site']}: закрыт"
    if cmd=='cancel': return 'Отменено: '+reply['summary']
    return 'Готово'


def _describe(change):
    site=change.get('site','')
    kind=change['kind']
    if kind=='telegram_block': return f"Telegram: @{change['username']} будет заблокирован."
    if kind=='remove_exception': return f"{site}: исключение {change['domain']} будет удалено; домен подпадает под блокировку сайта."
    if kind=='set_delay': return f"Задержка на ослабление станет {change['minutes']} мин."
    if kind=='set_budget': return f"{site}: дневной бюджет станет {change['minutes']} мин."
    if kind=='set_mode': return f"{site}: сайт будет закрыт без лимита."
    return f"{site}: будут заблокированы домены {', '.join(change['domains'])}."


def exchange(req):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(SOCKET)
        sock.sendall((json.dumps(req,ensure_ascii=False)+'\n').encode())
        with sock.makefile('r',encoding='utf-8') as stream:
            line=stream.readline(1024*1024)
        reply=json.loads(line)
        if not isinstance(reply,dict) or type(reply.get('ok')) is not bool:
            raise ValueError('Некорректный ответ службы')
        return reply


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv) or ['status']
    if args[0] == 'android':
        from .android_cli import main as android_main
        return android_main(args[1:], exchange=exchange)
    if args in (['--help'],['-h']):
        print(HELP)
        return 0
    try:
        req=build_request(args)
        if needs_confirmation(args):
            if not sys.stdin.isatty():
                print('Для подтверждения нужен терминал или флаг --yes',file=sys.stderr)
                return 2
            print(_describe(req['change']))
            if input('Действует сразу, отменить можно только через задержку. Продолжить? [y/N] ').lower()!='y':
                print('Изменение отменено')
                return 2
        requests=[req]
        if args[0]=='block' and 'domains' in req['change']:
            status_reply=exchange(dict(cmd='status'))
            if not status_reply['ok']:
                print(render_reply(dict(cmd='status'),status_reply),file=sys.stderr)
                return 1
            status=status_reply['status']
            known=status['blocked']+[s['site'] for s in status['limited']]
            change=req['change']
            if change['site'] in known:
                requests=[build_request(['block',change['site'],d],known) for d in change['domains']]
        for req in requests:
            reply=exchange(req)
            print(render_reply(req,reply),file=sys.stdout if reply['ok'] else sys.stderr)
            if not reply['ok'] or (req['cmd']=='doctor' and any(not c['ok'] for c in reply['checks'])):
                return 1
        return 0
    except SystemExit as exc:
        return exc.code
    except EOFError:
        print('Подтверждение не получено',file=sys.stderr)
        return 2
    except (OSError,ValueError,UnicodeError,KeyError,TypeError):
        print('Служба stopscroll недоступна или прислала некорректный ответ',file=sys.stderr)
        return 3


if __name__=='__main__':
    raise SystemExit(main())
