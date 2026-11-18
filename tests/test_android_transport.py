import base64
import json
import shlex
import subprocess
import unittest
from unittest.mock import Mock

from stopscroll.android_transport import (
    AndroidProtocolError,
    AndroidRequestError,
    AndroidTransport,
    AndroidTransportError,
)
from tests.test_android_policy import sample_policy


REQUEST_ID = '12345678-1234-4123-8123-123456789abc'
USB = b'usb123 device usb:1-2 product:phone model:Phone device:phone transport_id:1\n'
USB_SECOND = b'usb456 device usb:1-3 product:phone model:Phone device:phone transport_id:2\n'
WIRELESS = b'192.0.2.1:5555 device product:phone model:Phone device:phone transport_id:3\n'
DEVICES_HEADER = b'List of devices attached\n'
USERS = b'Users:\n\tUserInfo{0:Owner:13} running\n\tUserInfo{10:Work:30}\n'
MAX_RPC_BYTES = 96 * 1024
URI = 'content://com.celzero.bravedns.stopscroll.usb'


def response(**fields):
    return {
        'schema': 1, 'request_id': REQUEST_ID, 'ok': True,
        'revision': 3, 'applied_revision': 3, 'data': {},
    } | fields


def bundle(reply):
    return wrap(base64.b64encode(json.dumps(reply, separators=(',', ':')).encode()))


def wrap(encoded):
    return b'Result: Bundle[{result_b64=' + encoded + b'}]\n'


def completed(output, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=output, stderr=b'')


class AndroidTransportTests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock()
        self.transport = AndroidTransport(runner=self.runner)
        self.request = {'schema': 1, 'request_id': REQUEST_ID, 'op': 'status', 'args': {}}

    def replies(self, output, devices=USB, users=USERS):
        self.runner.reset_mock()
        self.runner.side_effect = [
            completed(DEVICES_HEADER + devices), completed(users), completed(output),
        ]

    def test_roundtrip_uses_usb_shell_and_fixed_user(self):
        reply = response(data={'opaque': 'value'})
        self.replies(bundle(reply))
        self.assertEqual(self.transport.call(self.request), reply)
        discovery, user_check, rpc = self.runner.call_args_list
        self.assertEqual(discovery.args[0], ['adb', 'devices', '-l'])
        self.assertEqual(user_check.args[0], ['adb', '-d', 'shell', 'pm', 'list', 'users'])
        self.assertEqual(rpc.args[0], ['adb', '-d', 'shell', '-T', 'sh'])
        command = shlex.split(rpc.kwargs['input'].decode())
        self.assertEqual(command[:-1], ['exec', 'content', 'call', '--user', '0', '--uri', URI,
                                         '--method', 'rpc', '--arg'])
        self.assertEqual(json.loads(base64.b64decode(command[-1], validate=True)), self.request)
        self.assertTrue(all(not call.kwargs.get('shell') for call in self.runner.call_args_list))

    def test_unprefixed_bundle_and_windows_newline(self):
        self.replies(bundle(response()).removeprefix(b'Result: ').replace(b'\n', b'\r\n'))
        self.assertEqual(self.transport.call(self.request), response())

    def test_64_kib_policy_fits_via_stdin(self):
        policy = sample_policy()
        policy['sites']['video']['domains'] += [('a' * 63 + '.') * 3 + 'example'] * 300
        request = self.request | {
            'op': 'policy.replace', 'args': {'policy': policy}, 'expected_revision': 3,
        }
        self.replies(bundle(response()))
        self.assertEqual(self.transport.call(request), response())
        script = self.runner.call_args_list[-1].kwargs['input']
        self.assertGreater(len(script), 0xffff)
        self.assertEqual(json.loads(base64.b64decode(shlex.split(script.decode())[-1])), request)

    def test_explicit_serial_is_verified_and_selects_from_multiple_usb_devices(self):
        self.replies(bundle(response()), devices=WIRELESS + USB + USB_SECOND)
        transport = AndroidTransport(usb_serial='usb456', runner=self.runner)
        self.assertEqual(transport.call(self.request), response())
        for call in self.runner.call_args_list[1:]:
            self.assertEqual(call.args[0][:3], ['adb', '-t', '2'])

    def test_explicit_usb_requires_one_positive_numeric_transport_id(self):
        for detail in (b'', b'transport_id:', b'transport_id:abc', b'transport_id:0',
                       b'transport_id:1 transport_id:2', b'transport_id:18446744073709551616'):
            with self.subTest(detail=detail):
                devices = USB.replace(b'transport_id:1', detail)
                self.replies(bundle(response()), devices=devices)
                with self.assertRaisesRegex(AndroidTransportError, 'transport_id'):
                    AndroidTransport(usb_serial='usb123', runner=self.runner).call(self.request)
                self.assertEqual(self.runner.call_count, 1)

    def test_explicit_transport_disappearance_does_not_reselect(self):
        self.runner.side_effect = [
            completed(DEVICES_HEADER + USB), completed(USERS), completed(b'', returncode=1),
        ]
        request = self.request | {'op': 'session.close', 'args': {'site': 'video'},
                                  'expected_revision': 3}
        with self.assertRaises(AndroidTransportError):
            AndroidTransport(usb_serial='usb123', runner=self.runner).call(request)
        self.assertEqual(self.runner.call_count, 3)
        self.assertEqual(self.runner.call_args_list[-1].args[0][:3], ['adb', '-t', '1'])

    def test_default_usb_ignores_wireless_peers(self):
        self.replies(bundle(response()), devices=WIRELESS + USB)
        self.assertEqual(self.transport.call(self.request), response())
        self.assertEqual(self.runner.call_args_list[1].args[0][1], '-d')

    def test_missing_or_unauthorized_usb_and_multiple_devices_stop_before_shell(self):
        for devices, reason in (
            (b'', 'USB'), (WIRELESS, 'USB'),
            (USB.replace(b' device ', b' unauthorized '), 'отлад'),
            (USB.replace(b' device ', b' offline '), 'offline'),
            (USB + USB_SECOND, '--usb-serial'),
        ):
            with self.subTest(devices=devices):
                self.replies(bundle(response()), devices=devices)
                with self.assertRaisesRegex(AndroidTransportError, reason):
                    self.transport.call(self.request)
                self.assertEqual(self.runner.call_count, 1)

    def test_explicit_wireless_missing_or_duplicate_serial_is_rejected(self):
        for serial, devices in (
            ('192.0.2.1:5555', WIRELESS),
            ('absent', USB),
            ('usb123', USB + WIRELESS.replace(b'192.0.2.1:5555', b'usb123')),
        ):
            with self.subTest(serial=serial, devices=devices):
                self.replies(bundle(response()), devices=devices)
                with self.assertRaisesRegex(AndroidTransportError, 'USB'):
                    AndroidTransport(usb_serial=serial, runner=self.runner).call(self.request)
                self.assertEqual(self.runner.call_count, 1)

    def test_unauthorized_explicit_usb_is_rejected(self):
        self.replies(bundle(response()), devices=USB.replace(b' device ', b' unauthorized '))
        with self.assertRaisesRegex(AndroidTransportError, 'отлад'):
            AndroidTransport(usb_serial='usb123', runner=self.runner).call(self.request)
        self.assertEqual(self.runner.call_count, 1)

    def test_explicit_serial_with_shell_characters_rejected_without_adb(self):
        for serial in ('', '-e', 'usb123;touch /tmp/unwanted', "`id`", 'usb123\nusb456'):
            with self.subTest(serial=serial), self.assertRaises(AndroidRequestError):
                AndroidTransport(usb_serial=serial, runner=self.runner).call(self.request)
        self.runner.assert_not_called()

    def test_missing_user_zero_stops_before_rpc(self):
        for users in (b'Users:\n\tUserInfo{10:Owner:13} running\n',
                      b'Users:\n\tUserInfo{10:UserInfo{0:Owner:13}:30}\n'):
            self.replies(bundle(response()), users=users)
            with self.assertRaisesRegex(AndroidTransportError, '0'):
                self.transport.call(self.request)
            self.assertEqual(self.runner.call_count, 2)

    def test_missing_adb_and_nonzero_exit_are_transport_errors(self):
        self.runner.side_effect = FileNotFoundError('adb')
        with self.assertRaisesRegex(AndroidTransportError, 'adb'):
            self.transport.call(self.request)
        self.runner.side_effect = [completed(DEVICES_HEADER + USB), completed(USERS),
                                   completed(b'', returncode=1)]
        with self.assertRaises(AndroidTransportError) as caught:
            self.transport.call(self.request)
        self.assertNotIsInstance(caught.exception, AndroidProtocolError)

    def test_mutation_timeout_does_not_retry(self):
        self.runner.side_effect = [completed(DEVICES_HEADER + USB), completed(USERS),
                                   subprocess.TimeoutExpired('adb', 5)]
        request = self.request | {'op': 'session.open', 'args': {'site': 'video', 'minutes': None},
                                  'expected_revision': 3}
        with self.assertRaisesRegex(AndroidTransportError, 'время'):
            self.transport.call(request)
        self.assertEqual(self.runner.call_count, 3)

    def test_application_error_is_returned(self):
        reply = response(ok=False, applied_revision=None,
                         error={'code': 'APPLY_FAILED', 'message': 'Apply failed'}, saved=True)
        del reply['data']
        self.replies(bundle(reply))
        self.assertEqual(self.transport.call(self.request), reply)
        self.assertEqual(self.runner.call_count, 3)

        reply.pop('saved')
        reply['error'] = {'code': 'FUTURE_CODE', 'message': 'Application error'}
        self.replies(bundle(reply))
        self.assertEqual(self.transport.call(self.request), reply)

    def test_rejects_escaped_lone_surrogates_in_error_keys_and_nested_values(self):
        error = response(ok=False, error={'code': 'CONFLICT', 'message': chr(0xd800)})
        del error['data']
        for reply in (
            error,
            response(data={chr(0xdfff): 'invalid key'}),
            response(data={'outer': [{'inner': [chr(0xd800)]}]}),
        ):
            with self.subTest(reply=reply):
                self.replies(bundle(reply))
                with self.assertRaises(AndroidProtocolError):
                    self.transport.call(self.request)

    def test_accepts_escaped_pair_and_literal_utf8_supplementary_characters(self):
        text = chr(0x1f600)
        reply = response(data={text: [text]})
        for ascii_json in (True, False):
            with self.subTest(ascii_json=ascii_json):
                raw = json.dumps(reply, ensure_ascii=ascii_json).encode('utf-8')
                self.replies(wrap(base64.b64encode(raw)))
                self.assertEqual(self.transport.call(self.request), reply)

    def test_bad_wrapper_base64_json_or_size_is_protocol_error(self):
        valid = bundle(response())
        for output in (
            b'', b'noise\n' + valid, valid + valid, valid.replace(b'result_b64', b'other'),
            valid.replace(b'}]', b', extra=1}]'),
            valid.replace(b'}]', b', result_b64=e30=}]'),
            wrap(b'!abcd'), wrap(b'YQ='), wrap(b'YR=='),
            wrap(base64.b64encode(b'\xff')), wrap(base64.b64encode(b'{')),
            wrap(base64.b64encode(b'[]')),
            wrap(base64.b64encode(b'{"schema":1,"schema":1}')),
            wrap(base64.b64encode(b'{"value":{"x":1,"x":2}}')),
            wrap(base64.b64encode(b'{"x":NaN}')),
            wrap(base64.b64encode(b'{"x":1e999}')),
            wrap(base64.b64encode(b' ' * (MAX_RPC_BYTES + 1))),
        ):
            with self.subTest(output=output[:150]):
                self.replies(output)
                with self.assertRaises(AndroidProtocolError):
                    self.transport.call(self.request)

    def test_exact_response_byte_limit_is_accepted(self):
        reply = response(data={'sample': ''})
        empty_size = len(json.dumps(reply, separators=(',', ':')).encode())
        reply['data']['sample'] = 'a' * (MAX_RPC_BYTES - empty_size)
        self.replies(bundle(reply))
        self.assertEqual(self.transport.call(self.request), reply)
        reply['data']['sample'] += 'a'
        self.replies(bundle(reply).removeprefix(b'Result: '))
        with self.assertRaisesRegex(AndroidProtocolError, '96'):
            self.transport.call(self.request)

    def test_rejects_mismatched_incomplete_or_wrongly_typed_envelope(self):
        for reply in (
            response(request_id='abcdefab-1234-4123-8123-123456789abc'),
            response(request_id=REQUEST_ID.upper()), response(schema=2), response(schema=True),
            response(ok=1), response(revision=True), response(revision=-1),
            response(applied_revision='3'), response(applied_revision=-1), response(data=[]),
            response(error={'code': 'CONFLICT'}), response(saved=True),
            response(ok=False, saved='yes', error={'code': 'CONFLICT'}),
            response(ok=False, data=[], error={'code': 'CONFLICT'}),
            {key: value for key, value in response().items() if key != 'applied_revision'},
        ):
            with self.subTest(reply=reply):
                self.replies(bundle(reply))
                with self.assertRaises(AndroidProtocolError):
                    self.transport.call(self.request)

    def test_error_envelope_requires_code_and_message_and_bounds_saved(self):
        for error, extra in (
            ('CONFLICT', {}),
            ({'code': 'CONFLICT'}, {}),
            ({'code': 'CONFLICT', 'message': 3}, {}),
            ({'code': '', 'message': 'Empty code'}, {}),
            ({'code': 'A' * 65, 'message': 'Long code'}, {}),
            ({'code': 'CONFLICT', 'message': 'Conflict', 'extra': 3}, {}),
            ({'code': 'CONFLICT', 'message': 'Conflict'}, {'saved': True}),
            ({'code': 'APPLY_FAILED', 'message': 'Apply failed'}, {'saved': False}),
        ):
            with self.subTest(error=error, extra=extra):
                reply = response(ok=False, error=error, **extra)
                del reply['data']
                self.replies(bundle(reply))
                with self.assertRaises(AndroidProtocolError):
                    self.transport.call(self.request)

    def test_request_schema_and_arguments_checked_before_adb(self):
        for request in (
            self.request | {'schema': True}, self.request | {'schema': 2},
            self.request | {'request_id': REQUEST_ID.upper()},
            self.request | {'request_id': REQUEST_ID.replace('-', '')},
            self.request | {'request_id': 'malformed'},
            self.request | {'op': 'status;id'}, self.request | {'args': {'x': '`id`'}},
            self.request | {'op': 'policy.get', 'args': {'x': 1}},
            self.request | {'expected_revision': 0},
            self.request | {'op': 'session.close', 'args': {'site': 'video'}},
            self.request | {'op': 'session.close', 'args': {'site': 'video'}, 'expected_revision': True},
            self.request | {'op': 'session.close', 'args': {'site': 'video'}, 'expected_revision': -1},
            self.request | {'op': 'session.close', 'args': {'site': 'video;id'}, 'expected_revision': 0},
            self.request | {'op': 'session.open', 'args': {'site': 'video', 'minutes': True},
                            'expected_revision': 0},
            self.request | {'op': 'session.open', 'args': {'site': 'video', 'minutes': 1441},
                            'expected_revision': 0},
            self.request | {'op': 'policy.replace', 'args': {'policy': {}}, 'expected_revision': 0},
            self.request | {'args': {'x': 'a' * MAX_RPC_BYTES}},
            self.request | {'uid': 2000},
        ):
            with self.subTest(request={**request, 'args': str(request['args'])[:100]}):
                with self.assertRaises(AndroidRequestError):
                    self.transport.call(request)
        self.runner.assert_not_called()
