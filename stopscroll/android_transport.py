"""Manual, USB-only transport for the Stopscroll Android content provider."""
import base64
import json
import math
import os
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
USB_DEVPATH = re.compile(rb'usb:[0-9]+(?::[0-9]+|-[0-9]+(?:\.[0-9]+)*)')


class AndroidRequestError(ValueError):
    """The local RPC request or device selection is invalid."""


class AndroidTransportError(RuntimeError):
    """ADB could not reach the authorized USB device and user 0."""


class AndroidProtocolError(AndroidTransportError):
    """The device response could not be verified as an RPC reply."""


class AndroidTimeoutError(AndroidTransportError):
    """A bounded ADB command did not complete before its deadline."""


def validate_usb_serial(serial: str) -> str:
    if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*', serial):
        raise AndroidRequestError('Некорректный серийный номер --usb-serial')
    return serial


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


def decode_json(raw: bytes) -> object:
    """Decode strict UTF-8 JSON, including checked strings and unique object keys."""
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=_finite_number, parse_float=_finite_number)
    _validate_unicode(value)
    return value


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
        reply = decode_json(raw)
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


def _rpc_command(request_bytes: bytes) -> tuple[str, ...]:
    encoded = base64.b64encode(request_bytes).decode('ascii')
    return ('content', 'call', '--user', '0', '--uri', PROVIDER_URI, '--method', 'rpc', '--arg', encoded)


def _exec_line(arguments: tuple[str, ...]) -> str:
    return shlex.join(('exec', *arguments)) + '\n'


class AndroidTransport:
    def __init__(self, usb_serial: str | None = None, *, runner=subprocess.run, timeout: float = 20):
        self.usb_serial = usb_serial
        self._runner = runner
        self._timeout = timeout

    def _run(self, *arguments: str, input: bytes = b'', stdout=subprocess.PIPE) -> bytes:
        try:
            result = self._runner(['adb', *arguments], input=input, stdout=stdout,
                                  stderr=subprocess.DEVNULL, timeout=self._timeout)
        except FileNotFoundError:
            raise AndroidTransportError('Команда adb не найдена') from None
        except subprocess.TimeoutExpired:
            raise AndroidTimeoutError('Истекло время ожидания ответа adb') from None
        except OSError as exc:
            raise AndroidTransportError('Не удалось запустить adb') from exc
        if result.returncode == 75:
            raise AndroidTransportError('Серийный номер Android не совпадает с выбранным USB-устройством')
        if result.returncode == 76:
            raise AndroidTransportError('Android-пользователь 0 недоступен')
        if result.returncode:
            raise AndroidTransportError('Команда adb завершилась с ошибкой')
        return result.stdout or b''

    def _usb_device(self) -> tuple[bytes, list[bytes]]:
        explicit = self.usb_serial
        if explicit is not None:
            validate_usb_serial(explicit)
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
                # adb devices -l emits devpath immediately after the connection state.
                usb = bool(details and details[0].startswith(b'usb:') and len(details[0]) > 4)
                devices.append((serial, state, usb, details))
        matches = ([device for device in devices if device[0] == explicit.encode('ascii')]
                   if explicit is not None else [device for device in devices if device[2]])
        if explicit is None and len(matches) > 1:
            raise AndroidTransportError('Подключено несколько USB-устройств, укажите --usb-serial')
        if len(matches) != 1 or not matches[0][2]:
            raise AndroidTransportError('Не найдено подходящее USB-устройство adb')
        serial, state, _, details = matches[0]
        if state == b'unauthorized':
            raise AndroidTransportError('Разрешите отладку по USB на телефоне')
        if state != b'device':
            raise AndroidTransportError(f'USB-устройство недоступно ({state.decode("ascii", "replace")})')
        return serial, details

    def _usb_selector(self) -> list[str]:
        _, details = self._usb_device()
        if self.usb_serial is None:
            return ['-d']
        ids = [detail.removeprefix(b'transport_id:') for detail in details
               if detail.startswith(b'transport_id:')]
        if (len(ids) != 1 or not re.fullmatch(rb'[1-9][0-9]{0,19}', ids[0])
                or int(ids[0]) >= 2**64):
            raise AndroidTransportError('Не удалось определить transport_id USB-устройства')
        return ['-t', ids[0].decode('ascii')]

    def bind(self) -> 'BoundAndroidTransport':
        """Bind the Linux USB path and the device's serial across one command."""
        serial, details = self._usb_device()
        usb_path = details[0]
        if not USB_DEVPATH.fullmatch(usb_path):
            raise AndroidTransportError('Не удалось определить путь USB-устройства Linux')
        try:
            identity = validate_usb_serial(serial.decode('ascii'))
        except (UnicodeError, AndroidRequestError):
            raise AndroidTransportError('Не удалось определить серийный номер USB-устройства') from None
        return BoundAndroidTransport(self._run, usb_path.decode('ascii'), identity)

    def call(self, request: dict) -> dict:
        """Make one RPC call. A well-formed ok=false reply is returned to the CLI."""
        request_bytes, request_id = _request_bytes(request)
        selector = self._usb_selector()
        users = self._run(*selector, 'shell', 'pm', 'list', 'users')
        if not re.search(rb'^[ \t]*UserInfo\{0:[^\r\n]*\}(?:[ \t]+running)?\r?$', users, re.MULTILINE):
            raise AndroidTransportError('Android-пользователь 0 недоступен')
        script = _exec_line(_rpc_command(request_bytes)).encode('ascii')
        output = self._run(*selector, 'shell', '-T', 'sh', input=script)
        return _reply(output, request_id)


class BoundAndroidTransport:
    def __init__(self, run, usb_path: str, serial: str):
        self._run = run
        self._usb_path = usb_path
        identity = shlex.quote(serial)
        self._guard = (
            f'if [ "$(getprop ro.serialno)" != {identity} ] && '
            f'[ "$(getprop ro.boot.serialno)" != {identity} ]; then exit 75; fi\n'
            'users="$(pm list users)" || exit 76\n'
            "printf '%s\\n' \"$users\" | grep -qE '^[[:blank:]]*UserInfo[{]0:.*[}]"
            "([[:blank:]]+running)?[[:space:]]*$' || exit 76\n"
        )

    def _execute(self, arguments: tuple[str, ...], *, stdout=subprocess.PIPE) -> bytes:
        # ADB can restart between subprocesses. Check the phone and user in this
        # shell, before exec. Stdin also avoids the ADB service-string length limit.
        script = (self._guard + _exec_line(arguments)).encode('utf-8')
        return self._run('-s', self._usb_path, 'shell', '-T', 'sh', input=script, stdout=stdout)

    def shell(self, *arguments: str) -> bytes:
        return self._execute(arguments)

    def pull(self, source: str, destination) -> None:
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            self._execute(('cat', source), stdout=stream)

    def call(self, request: dict) -> dict:
        request_bytes, request_id = _request_bytes(request)
        return _reply(self._execute(_rpc_command(request_bytes)), request_id)
