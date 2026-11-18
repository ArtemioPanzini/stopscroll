"""Manual, USB-only transport for the Stopscroll Android content provider."""
import base64
import json
import math
import re
import shlex
import subprocess
from uuid import UUID

from .android_policy import PolicyError, validate_policy, validate_site_id

PROVIDER_URI = 'content://com.celzero.bravedns.stopscroll.usb'
MAX_RPC_BYTES = 96 * 1024
MAX_BASE64_BYTES = 4 * ((MAX_RPC_BYTES + 2) // 3)
MAX_REPLY_BYTES = MAX_BASE64_BYTES + len(b'Result: Bundle[{result_b64=}]\r\n')
BUNDLE = re.compile(rb'(?:Result: )?Bundle\[\{result_b64=([A-Za-z0-9+/]+={0,2})\}\](?:\r?\n)?')
ARG_FIELDS = {
    'status': set(),
    'policy.get': set(),
    'policy.replace': {'policy'},
    'session.open': {'site', 'minutes'},
    'session.close': {'site'},
}
MUTATIONS = {'policy.replace', 'session.open', 'session.close'}
REQUEST_FIELDS = {'schema', 'request_id', 'op', 'args'}
REPLY_FIELDS = {'schema', 'request_id', 'ok', 'revision', 'applied_revision'}


class AndroidRequestError(ValueError):
    """The local RPC request or device selection is invalid."""


class AndroidTransportError(RuntimeError):
    """ADB could not reach the authorized USB device and user 0."""


class AndroidProtocolError(AndroidTransportError):
    """The device response could not be verified as an RPC reply."""


def _revision(value: object) -> bool:
    return type(value) is int and value >= 0


def _request_bytes(request: dict) -> tuple[bytes, str]:
    if not isinstance(request, dict) or not isinstance(request.get('op'), str):
        raise AndroidRequestError('Некорректный Android RPC-запрос')
    op = request['op']
    fields = REQUEST_FIELDS | {'expected_revision'} if op in MUTATIONS else REQUEST_FIELDS
    if op not in ARG_FIELDS or set(request) != fields:
        raise AndroidRequestError('Неизвестная операция или поля Android RPC-запроса')
    if type(request['schema']) is not int or request['schema'] != 1:
        raise AndroidRequestError('Поддерживается только схема Android RPC 1')
    request_id = request['request_id']
    try:
        valid_id = isinstance(request_id, str) and str(UUID(request_id)) == request_id
    except ValueError:
        valid_id = False
    if not valid_id:
        raise AndroidRequestError('request_id должен быть каноническим UUID')
    if op in MUTATIONS and not _revision(request['expected_revision']):
        raise AndroidRequestError('expected_revision должна быть неотрицательным целым числом')
    args = request['args']
    if not isinstance(args, dict) or set(args) != ARG_FIELDS[op]:
        raise AndroidRequestError('Некорректные аргументы Android RPC')
    try:
        if op == 'policy.replace':
            validate_policy(args['policy'])
        if 'site' in args:
            validate_site_id(args['site'])
    except PolicyError as exc:
        raise AndroidRequestError(str(exc)) from None
    minutes = args.get('minutes')
    if minutes is not None and (type(minutes) is not int or not 1 <= minutes <= 1440):
        raise AndroidRequestError('Укажите целое число минут от 1 до 1440')
    try:
        encoded = json.dumps(request, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError) as exc:
        raise AndroidRequestError('Не удалось закодировать Android RPC-запрос') from exc
    if len(encoded) > MAX_RPC_BYTES:
        raise AndroidRequestError('Размер запроса Android RPC превышает 96 КиБ')
    return encoded, request_id


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite JSON number')
    return number


def _validate_unicode(value: object) -> None:
    """Reject escaped lone surrogates left in strings by the JSON decoder."""
    if isinstance(value, str):
        value.encode('utf-8')
    elif isinstance(value, dict):
        for key, member in value.items():
            key.encode('utf-8')
            _validate_unicode(member)
    elif isinstance(value, list):
        for member in value:
            _validate_unicode(member)


def _reply(output: bytes, request_id: str) -> dict:
    if len(output) > MAX_REPLY_BYTES:
        raise AndroidProtocolError('Размер ответа Android RPC превышает 96 КиБ')
    framed = BUNDLE.fullmatch(output)
    if framed is None:
        raise AndroidProtocolError('Ожидается Bundle с единственным ключом result_b64')
    encoded = framed[1]
    if len(encoded) > MAX_BASE64_BYTES:
        raise AndroidProtocolError('Размер ответа Android RPC превышает 96 КиБ')
    try:
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw) != encoded:
            raise ValueError('noncanonical Base64')
        reply = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=_finite_number, parse_float=_finite_number)
        _validate_unicode(reply)
    except (ValueError, RecursionError) as exc:
        raise AndroidProtocolError('Некорректные Base64, UTF-8 или JSON в ответе Android RPC') from exc

    if not isinstance(reply, dict) or type(reply.get('ok')) is not bool:
        raise AndroidProtocolError('Некорректный ответ Android RPC')
    fields = REPLY_FIELDS | ({'data'} if reply['ok'] else {'error'})
    if not reply['ok'] and 'saved' in reply:
        fields.add('saved')
    if (set(reply) != fields or type(reply['schema']) is not int or reply['schema'] != 1
            or reply['request_id'] != request_id or not _revision(reply['revision'])
            or (reply['applied_revision'] is not None and not _revision(reply['applied_revision']))):
        raise AndroidProtocolError('Некорректная схема или request_id ответа Android RPC')
    if reply['ok']:
        if not isinstance(reply['data'], dict):
            raise AndroidProtocolError('Поле data ответа Android RPC должно быть объектом')
    else:
        error = reply['error']
        if (not isinstance(error, dict) or set(error) != {'code', 'message'}
                or not isinstance(error['code'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', error['code'])
                or not isinstance(error['message'], str)):
            raise AndroidProtocolError('Некорректное поле error ответа Android RPC')
        if 'saved' in reply and (reply['saved'] is not True or error['code'] != 'APPLY_FAILED'):
            raise AndroidProtocolError('Некорректное поле saved ответа Android RPC')
    return reply


class AndroidTransport:
    def __init__(self, usb_serial: str | None = None, *, runner=subprocess.run, timeout: float = 20):
        self.usb_serial = usb_serial
        self._runner = runner
        self._timeout = timeout

    def _run(self, *arguments: str, input: bytes = b'') -> bytes:
        try:
            result = self._runner(['adb', *arguments], input=input, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=self._timeout)
        except FileNotFoundError:
            raise AndroidTransportError('Команда adb не найдена') from None
        except subprocess.TimeoutExpired:
            raise AndroidTransportError('Истекло время ожидания ответа adb') from None
        except OSError as exc:
            raise AndroidTransportError('Не удалось запустить adb') from exc
        if result.returncode:
            raise AndroidTransportError('Команда adb завершилась с ошибкой')
        return result.stdout

    def _usb_selector(self) -> list[str]:
        explicit = self.usb_serial
        if explicit is not None and (not isinstance(explicit, str)
                                     or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*', explicit)):
            raise AndroidRequestError('Некорректный серийный номер --usb-serial')
        output = self._run('devices', '-l')
        header, _, rows = output.partition(b'\n')
        if header.rstrip(b'\r') != b'List of devices attached':
            raise AndroidTransportError('Не удалось прочитать список устройств adb')
        devices = []
        for row in rows.splitlines():
            columns = row.split()
            if columns:
                if len(columns) < 2:
                    raise AndroidTransportError('Не удалось прочитать список устройств adb')
                serial, state, *details = columns
                usb = any(detail.startswith(b'usb:') and len(detail) > 4 for detail in details)
                devices.append((serial, state, usb, details))
        matches = ([device for device in devices if device[0] == explicit.encode('ascii')]
                   if explicit is not None else [device for device in devices if device[2]])
        if explicit is None and len(matches) > 1:
            raise AndroidTransportError('Подключено несколько USB-устройств, укажите --usb-serial')
        if len(matches) != 1 or not matches[0][2]:
            raise AndroidTransportError('Не найдено подходящее USB-устройство adb')
        _, state, _, details = matches[0]
        if state == b'unauthorized':
            raise AndroidTransportError('Разрешите отладку по USB на телефоне')
        if state != b'device':
            raise AndroidTransportError(f'USB-устройство недоступно ({state.decode("ascii", "replace")})')
        if explicit is None:
            return ['-d']
        ids = [detail.removeprefix(b'transport_id:') for detail in details
               if detail.startswith(b'transport_id:')]
        if (len(ids) != 1 or not re.fullmatch(rb'[1-9][0-9]{0,19}', ids[0])
                or int(ids[0]) >= 2**64):
            raise AndroidTransportError('Не удалось определить transport_id USB-устройства')
        return ['-t', ids[0].decode('ascii')]

    def call(self, request: dict) -> dict:
        """Make one RPC call. A well-formed ok=false reply is returned to the CLI."""
        request_bytes, request_id = _request_bytes(request)
        encoded = base64.b64encode(request_bytes).decode('ascii')
        selector = self._usb_selector()
        users = self._run(*selector, 'shell', 'pm', 'list', 'users')
        if not re.search(rb'^[ \t]*UserInfo\{0:[^\r\n]*\}(?:[ \t]+running)?\r?$', users, re.MULTILINE):
            raise AndroidTransportError('Android-пользователь 0 недоступен')

        # Stream the command over stdin to fit policies larger than ADB's 16-bit service string.
        command = ['exec', 'content', 'call', '--user', '0', '--uri', PROVIDER_URI,
                   '--method', 'rpc', '--arg', encoded]
        output = self._run(*selector, 'shell', '-T', 'sh', input=(shlex.join(command) + '\n').encode('ascii'))
        return _reply(output, request_id)
