"""Root daemon and authenticated, newline-delimited local JSON protocol."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import struct
import time

from . import store
from .telegram import normalize_channel, validate_channel_id
from .day import day_key
from .engine import Engine, EngineError
from .changes import ChangeError
from .seed import initial_state
from .system import Runner, System
from .doctor import run_checks

SOCKET = '/run/stopscroll/stopscroll.sock'
STATE = '/var/lib/stopscroll/state.json'
OWNER_FILE = '/etc/stopscroll/owner-uid'
TICK_SEC = 1.0
SYNC_EVERY_SEC = 60.0
LOG = logging.getLogger(__name__)


def bootstrap(state_path, owner_uid, now):
    state = store.load(state_path)
    if state is not None:
        return state
    if owner_uid is None:
        raise SystemExit('Нет состояния и UID владельца')
    return initial_state(owner_uid, day_key(now, 4))


def _clock():
    return time.time(), time.clock_gettime(time.CLOCK_BOOTTIME), Path('/proc/sys/kernel/random/boot_id').read_text().strip()


class Daemon:
    def __init__(self, engine, system, save, doctor):
        self.engine, self.system, self.save, self.doctor = engine, system, save, doctor
        self._last_sync = None
        self._last_save = None
        self._boot_id = None
        self._dirty = False
        self._telegram_dirty = False
        self._subscribers = set()

    def _sync(self, now):
        try:
            for message in self.system.sync(self.engine.state, now):
                LOG.warning('%s', message)
            self._telegram_dirty = self.system.telegram_policy_dirty is True
            for message in self.system.retry_quarantine(self.engine.state, now, self._persist):
                LOG.warning('%s', message)
        except Exception:
            LOG.exception('Сбой сверки системы')

    def _persist(self):
        self._dirty = True
        try:
            self.save()
            self._dirty = False
            return True
        except Exception:
            LOG.exception('Не удалось сохранить состояние')
            return False

    def _changed(self, events, now):
        for event in events:
            if event.kind == 'session_closed':
                self.system.queue_quarantine(self.engine.state, event.site)
        self._persist()
        for event in events:
            try:
                if event.kind == 'uninstall':
                    self.system.uninstall()
            except Exception:
                LOG.exception('Сбой обработки события %s', event.kind)
        self._sync(now)
        payloads = [dict(event=e.kind, site=e.site, text=e.text) for e in events]
        for queue in tuple(self._subscribers):
            for payload in payloads:
                if queue.full():
                    self._subscribers.discard(queue)
                    break
                queue.put_nowait(payload)
        return payloads

    def handle(self, req, peer_uid, now):
        if peer_uid not in (0, self.engine.state.owner_uid):
            return dict(ok=False, error='нет доступа')
        try:
            if not isinstance(req, dict) or not isinstance(req.get('cmd'), str):
                raise ValueError('Некорректная команда')
            cmd = req['cmd']
            result, events = {}, []
            if cmd == 'status':
                result['status'] = self.engine.status(now)
                result['status']['telegram_policy_pending'] = self._telegram_dirty
            elif cmd == 'export-sites':
                if set(req) != {'cmd'}:
                    raise ValueError('Некорректная команда экспорта сайтов')
                result['policy'] = self.engine.state.export_sites()
            elif cmd == 'doctor':
                result['checks'] = self.doctor()
            elif cmd in ('open', 'close'):
                if not isinstance(req.get('site'), str):
                    raise ValueError('Укажите сайт')
                if cmd == 'open':
                    result = self.engine.open(req['site'], req.get('minutes'), now)
                else:
                    events = self.engine.close(req['site'], now)
            elif cmd == 'change':
                result = self.engine.submit(req.get('change'), now)
                events = result.pop('events', [])
            elif cmd == 'telegram-observe':
                if set(req) != {'cmd', 'username', 'id'}:
                    raise ValueError('Некорректное наблюдение Telegram')
                username = normalize_channel(req['username'])
                channel_id = validate_channel_id(req['id'])
                if username not in self.engine.state.telegram_channels:
                    raise ValueError('Канал не заблокирован')
                ids = self.engine.state.telegram_channels[username]
                if channel_id not in ids:
                    ids.append(channel_id)
            elif cmd == 'cancel':
                if type(req.get('id')) is not int:
                    raise ValueError('Укажите целый номер изменения')
                result['summary'] = self.engine.cancel(req['id'])
            elif cmd != 'subscribe':
                raise ValueError('Неизвестная команда')
            if cmd in ('open', 'close', 'change', 'cancel', 'telegram-observe'):
                self._changed(events, now)
                if self._telegram_dirty:
                    return dict(result, ok=False, error='Политика Telegram не опубликована; изменение сохранено в памяти, публикация будет повторена',
                                telegram_policy_pending=True, state_save_pending=self._dirty)
                if self._dirty:
                    return dict(result, ok=False, error='Не удалось сохранить состояние; изменение действует в памяти, сохранение будет повторено')
            return dict(ok=True, **result)
        except (EngineError, ChangeError, ValueError) as exc:
            return dict(ok=False, error=str(exc))
        except Exception:
            LOG.exception('Ошибка обработки команды')
            return dict(ok=False, error='Не удалось выполнить команду')

    def tick(self, now, boot, boot_id):
        if self._boot_id != boot_id:
            self._boot_id = boot_id
            self._last_sync = self._last_save = boot
        events = self.engine.tick(now, boot, boot_id)
        try:
            for message in self.system.sync_browser(self.engine.state, now):
                LOG.warning('%s', message)
        except Exception:
            LOG.exception('Сбой публикации браузерной политики')
        payloads = self._changed(events, now) if events else []
        if events:
            self._last_sync = self._last_save = boot
        if self._telegram_dirty:
            for message in self.system.sync_telegram(self.engine.state):
                LOG.warning('%s', message)
            self._telegram_dirty = self.system.telegram_policy_dirty is True
        if boot - self._last_sync >= SYNC_EVERY_SEC:
            self._sync(now)
            self._last_sync = boot
        if (self._dirty or self.engine.state.pending) and boot - self._last_save >= SYNC_EVERY_SEC:
            self._persist()
            self._last_save = boot
        return payloads

    async def serve(self, socket_path):
        async def client(reader, writer):
            queue = None
            try:
                peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                uid = struct.unpack('3i', peer)[1]
                line = await asyncio.wait_for(reader.readline(), 10)
                try:
                    req = json.loads(line)
                except (ValueError, UnicodeError):
                    req = None
                now, boot, boot_id = _clock()
                self.tick(now, boot, boot_id)
                reply = self.handle(req, uid, now)
                subscribed = reply['ok'] and isinstance(req, dict) and req.get('cmd') == 'subscribe'
                if subscribed:
                    queue = asyncio.Queue(maxsize=256)
                    self._subscribers.add(queue)
                writer.write((json.dumps(reply, ensure_ascii=False) + '\n').encode())
                await writer.drain()
                if subscribed:
                    async def send_events():
                        while queue in self._subscribers:
                            payload = await queue.get()
                            writer.write((json.dumps(payload, ensure_ascii=False) + '\n').encode())
                            await asyncio.wait_for(writer.drain(), 10)
                    sender = asyncio.create_task(send_events())
                    try:
                        await reader.read()
                    finally:
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
            except (OSError, ValueError, asyncio.TimeoutError):
                pass
            finally:
                if queue is not None:
                    self._subscribers.discard(queue)
                writer.close()
                await writer.wait_closed()
        async def ticker():
            while True:
                self.tick(*_clock())
                await asyncio.sleep(TICK_SEC)
        server = await asyncio.start_unix_server(client, path=socket_path)
        os.chmod(socket_path, 0o666)
        timer = asyncio.create_task(ticker())
        try:
            async with server:
                await server.serve_forever()
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            Path(socket_path).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--init', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        owner_uid = int(Path(OWNER_FILE).read_text().strip())
        if owner_uid < 0:
            raise ValueError()
    except (OSError, ValueError):
        owner_uid = None
    now, boot, boot_id = _clock()
    state = bootstrap(STATE, owner_uid, now)
    runner = Runner()
    daemon = Daemon(Engine(state), System(runner), lambda: store.save(state, STATE), lambda: run_checks(runner, state))
    Path(STATE).parent.mkdir(parents=True, exist_ok=True)
    events = daemon.tick(now, boot, boot_id)
    if not events:
        daemon._persist()
        daemon._sync(now)
    if not args.init:
        Path(SOCKET).parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(daemon.serve(SOCKET))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
