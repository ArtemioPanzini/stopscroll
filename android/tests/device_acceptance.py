"""Run one foreground acceptance case on a USB phone with an installed release and probe."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
if __package__ in ('', None):
    sys.path.insert(0, str(REPO))

from stopscroll.android_cli import (
    PACKAGE, CommandRefused, _confirm_mutation_receipt, _read_policy, _read_status,
    _receipt_path, _require, _sha256, _uint, inspect_installed_release,
)
from stopscroll.android_policy import PolicyError, domain_within, policy_hash
from stopscroll.android_transport import (
    AndroidRequestError, AndroidTransport, AndroidTransportError, decode_json, validate_usb_serial,
)

CASES = ('basic', 'tcp', 'udp', 'deadline', 'doze', 'wifi')
PROBE_PACKAGE = 'org.stopscroll.probe'
PROBE_RESULT = f'/sdcard/Android/data/{PROBE_PACKAGE}/files/result.json'
TCP_HOST = 'httpbingo.org'
UDP_HOST = 'stun.cloudflare.com'
STREAM_BYTES = 100
STREAM_URL = f'https://{TCP_HOST}/drip?duration=10&numbytes={STREAM_BYTES}&delay=0'
CUTOFF_MS = 2_000


class AcceptanceError(RuntimeError):
    """A prerequisite or case assertion failed."""


class RejectedMutation(AcceptanceError):
    def __init__(self, request, reply):
        self.saved = reply.get('saved') is True
        error = reply['error']
        message = f"{request['op']} {error['code']} — {error['message']}"
        if self.saved:
            message += f" (saved revision {reply['revision']}, applied revision {reply['applied_revision']})"
        super().__init__(message)


class IndeterminateMutation(AcceptanceError):
    """The mutation was sent but there is no trustworthy result."""


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def request(op, args=None, revision=None):
    value = {'schema': 1, 'request_id': str(uuid4()), 'op': op, 'args': args or {}}
    if revision is not None:
        value['expected_revision'] = revision
    return value


def release_evidence(bound):
    installed = inspect_installed_release(bound)
    lock = decode_json((REPO / 'android/upstream.lock.json').read_bytes())
    require(isinstance(lock, dict) and type(lock.get('release_version_code')) is int
            and lock['release_version_code'] > 0 and _sha256(lock.get('firestack_aar_sha256'))
            and isinstance(lock.get('firestack_commit'), str)
            and re.fullmatch(r'[0-9a-f]{40}', lock['firestack_commit']),
            'The checkout has invalid Android release metadata.')
    require(installed['version_code'] == lock['release_version_code'],
            'The verified APK version differs from the release in this checkout.')
    return {'installed_apk': installed, 'firestack_commit_from_lock': lock['firestack_commit'],
            'firestack_aar_sha256_from_lock': lock['firestack_aar_sha256']}


def open_report(path, *, create_parent=False):
    path = path.expanduser()
    parent = path.parent.resolve()
    require(not any((folder / '.git').exists() for folder in (parent, *parent.parents)),
            'Choose a report location outside Git worktrees.')
    if create_parent:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(parent / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    stream = os.fdopen(descriptor, 'w', encoding='utf-8')
    try:
        os.fchmod(descriptor, 0o600)
    except OSError:
        stream.close()
        raise
    return stream


class DeviceAcceptance:
    def __init__(self, bound):
        self.bound = bound
        self.events = []
        self.cleanup_issues = []
        self.fixture_hash = None
        self.owned_grant = None
        self.probe_uid = None

    def record(self, kind, result):
        self.events.append({'kind': kind, 'host_monotonic': time.monotonic(), 'result': result})
        return result

    def call(self, value):
        # Used by the production read helpers, including strict data validation.
        return self.record(value['op'], self.bound.call(value))

    def status(self):
        return _read_status(self)

    def active_status(self):
        state = self.status()
        require(state['data']['protection']['state'] == 'active',
                'The phone must report active protection with its current applied revision.')
        return state

    def mutate(self, value, label=None, validate=None):
        self.record('mutation_request', {key: value[key] for key in ('op', 'request_id', 'expected_revision')})
        try:
            reply = self.record(label or value['op'], self.bound.call(value))
            if validate is not None and reply['ok']:
                validate(reply)
        except (AndroidTransportError, CommandRefused, KeyboardInterrupt) as exc:
            raise IndeterminateMutation(f"{value['op']} {value['request_id']} has an indeterminate result. {exc}") from exc
        if not reply['ok'] and reply['error']['code'] == 'STORAGE_ERROR':
            # The journal can save the mutation before writing its final receipt.
            raise IndeterminateMutation(
                f"{value['op']} {value['request_id']} has an indeterminate result. STORAGE_ERROR — {reply['error']['message']}")
        return reply

    def acknowledged(self, value, validate):
        reply = self.mutate(value, validate=validate)
        if not reply['ok']:
            raise RejectedMutation(value, reply)
        return reply

    def replace(self, policy, revision):
        digest = policy_hash(policy)

        def validate(reply):
            data = reply['data']
            _require(set(data) == {'changed', 'policy_hash'} and data['changed'] is True, 'policy.replace')
            _require(data['policy_hash'] == digest, 'policy_hash')
            _confirm_mutation_receipt(reply, revision, changed=True)

        return self.acknowledged(request('policy.replace', {'policy': policy}, revision), validate)

    def fixture_status(self):
        status = self.active_status()
        require(status['data']['policy_hash'] == self.fixture_hash,
                'The policy changed while the fixture was running.')
        return status

    def session(self, value):
        site = value['args']['site']
        opening = value['op'] == 'session.open'

        def validate(reply):
            data = reply['data']
            fields = {'site', 'remaining_sec'} | ({'granted_sec', 'deadline_elapsed_ms'} if opening else {'closed'})
            _require(set(data) == fields and data['site'] == site and _uint(data['remaining_sec']), value['op'])
            if opening:
                _require(_uint(data['granted_sec']) and 0 < data['granted_sec'] <= 60, 'granted_sec')
                _require(_uint(data['deadline_elapsed_ms']), 'deadline_elapsed_ms')
            else:
                _require(type(data['closed']) is bool, 'closed')
            _confirm_mutation_receipt(reply, value['expected_revision'], changed=opening or data['closed'])

        return self.acknowledged(value, validate)

    @staticmethod
    def grant_identity(status, site):
        data = status['data']
        grant = data['sites'][site]['session']
        if grant is None:
            return None
        return site, data['clock']['boot_count'], grant['start_elapsed_ms'], grant['deadline_elapsed_ms']

    def open_session(self, site):
        before = self.fixture_status()
        require(self.grant_identity(before, site) is None, 'The fixture session was opened outside this run.')
        value = request('session.open', {'site': site, 'minutes': 1}, before['revision'])
        opened = self.session(value)
        current = self.fixture_status()
        identity = self.grant_identity(current, site)
        require(identity is not None and current['revision'] == opened['revision'],
                'The phone state changed before the acknowledged grant could be identified.')
        _, boot, start, deadline = identity
        require(boot == before['data']['clock']['boot_count']
                and before['data']['clock']['elapsed_ms'] <= start <= current['data']['clock']['elapsed_ms']
                and deadline == opened['data']['deadline_elapsed_ms']
                and (deadline - start + 999) // 1000 == opened['data']['granted_sec'],
                'Could not match the live fixture grant to the open receipt.')
        self.owned_grant = identity
        require(deadline - start == 60_000, 'The temporary group needs a full minute before the next budget reset.')
        return value, opened, before

    def close_session(self, site, before):
        require(self.owned_grant is not None and self.grant_identity(before, site) == self.owned_grant,
                'The acknowledged fixture grant is no longer active.')
        value = request('session.close', {'site': site}, before['revision'])
        closed = self.session(value)
        self.owned_grant = None
        require(closed['data']['closed'], 'The fixture session had already closed.')
        return value, closed

    def check_policy_snapshot(self, current, status, *, allowed_session=None):
        policy = current['data']['policy']
        data = status['data']
        require(current['revision'] == status['revision'] and current['data']['policy_hash'] == data['policy_hash']
                and policy['sites'].keys() == data['sites'].keys(),
                'CONFLICT — phone state changed while reading the policy.')
        require(all(state['session'] is None for site, state in data['sites'].items() if site != allowed_session),
                'A real phone session is active. Replacing the policy would close it.')
        if allowed_session is not None:
            active_grant = self.grant_identity(status, allowed_session)
            require(active_grant is None or active_grant == self.owned_grant,
                    'The active fixture session was opened outside this run or ownership could not be verified.')

    def cleanup_fixture(self, site, group):
        current = _read_policy(self)
        policy = current['data']['policy']
        if site in policy['sites']:
            require(policy['sites'][site] == group, 'The fixture group has been edited. Inspect it manually.')
            self.check_policy_snapshot(current, self.status(), allowed_session=site)
            candidate = deepcopy(policy)
            del candidate['sites'][site]
            self.replace(candidate, current['revision'])
        self.record('fixture_cleanup', {'site': site, 'removed': True})

    def cleanup_issue(self, detail):
        self.cleanup_issues.append(str(detail))
        self.record('cleanup_issue', str(detail))

    def observe_indeterminate(self):
        for op, observe in (('status', self.status), ('policy.get', lambda: _read_policy(self))):
            try:
                reply = observe()
                self.record('read_only_observation', {'op': op, 'revision': reply['revision']})
            except (AndroidTransportError, CommandRefused, KeyboardInterrupt) as exc:
                self.record('read_only_observation', {'op': op, 'error': str(exc) or type(exc).__name__})

    def fixture(self, case):
        site, host = ('sscheckudp', UDP_HOST) if case == 'udp' else ('sschecktcp', TCP_HOST)
        if case == 'doze':
            self.doze_preflight()
        current = _read_policy(self)
        self.check_policy_snapshot(current, self.active_status())
        policy = current['data']['policy']
        require(site not in policy['sites'], f'The temporary group ID {site} is already configured.')
        for entry in policy['sites'].values():
            require(not any(domain_within(host, domain) or domain_within(domain, host) for domain in entry['domains']),
                    f'The test host {host} overlaps the current policy.')
        group = {'mode': 'limited', 'budget_min': 20, 'domains': [host], 'exceptions': []}
        candidate = deepcopy(policy)
        candidate['sites'][site] = group
        self.fixture_hash = policy_hash(candidate)
        owns_fixture = True
        try:
            try:
                self.replace(candidate, current['revision'])
            except RejectedMutation as exc:
                owns_fixture = exc.saved
                raise
            if case == 'tcp':
                self.tcp(site)
            elif case == 'udp':
                self.udp(site)
            else:
                self.deadline(site, case)
        except IndeterminateMutation:
            owns_fixture = False
            self.cleanup_issue(f'Inspect {site} before removing it. The last mutation is indeterminate.')
            raise
        finally:
            if owns_fixture:
                try:
                    self.cleanup_fixture(site, group)
                except IndeterminateMutation as exc:
                    self.cleanup_issue(f'Fixture cleanup is indeterminate. Inspect {site}. {exc}')
                    self.observe_indeterminate()
                except (AcceptanceError, AndroidTransportError, CommandRefused, PolicyError, KeyboardInterrupt) as exc:
                    self.cleanup_issue(f'Fixture cleanup could not be confirmed. Inspect {site}. {exc}')

    def setting(self, key):
        return self.bound.shell('settings', 'get', 'global', key).strip()

    def check_radios(self):
        states = {key: self.setting(key).decode('ascii') for key in ('mobile_data', 'data_roaming', 'wifi_on')}
        self.record('radio_settings', states)
        require(states == {'mobile_data': '0', 'data_roaming': '0', 'wifi_on': '1'},
                'Start with Wi-Fi enabled and both mobile data and data roaming disabled.')

    def ready(self):
        self.record('release', release_evidence(self.bound))
        self.active_status()
        self.check_radios()
        listing = self.bound.shell('pm', 'list', 'packages', '--user', '0', '-U')
        uids = []
        for package in (PACKAGE, PROBE_PACKAGE):
            matches = re.findall(rb'^package:' + re.escape(package.encode('ascii')) + rb' uid:([0-9]+)\r?$',
                                 listing, re.MULTILINE)
            require(len(matches) == 1 and 10_000 <= int(matches[0]) < 100_000,
                    f'Expected {package} installed for Android user 0 with an app UID.')
            uids.append(int(matches[0]))
        vpn_uid, self.probe_uid = uids
        require(self.probe_uid != vpn_uid, 'The probe must run under a separate application UID.')
        self.record('app_uids', {'release': vpn_uid, 'probe': self.probe_uid})

    def probe(self, op, **fields):
        token = f'accept-{uuid4().hex}'
        args = ['am', 'start', '--user', '0', '-n', f'{PROBE_PACKAGE}/.ProbeActivity',
                '--es', 'op', op, '--es', 'token', token]
        for name, value in fields.items():
            args.extend(('--es', name, value))
        output = self.bound.shell(*args).decode('utf-8')
        require('Starting: Intent' in output and 'Error' not in output, 'The probe activity failed to start.')
        self.record('probe_start', {'op': op, 'token': token, **fields})
        return op, token

    def sample(self, probe):
        # The first launch may not have published an AtomicFile yet.
        raw = self.bound.shell('sh', '-c', 'if [ -f "$1" ]; then cat "$1"; fi', 'probe-result', PROBE_RESULT)
        if not raw:
            return None
        value = decode_json(raw)
        require(isinstance(value, dict), 'The probe result must be a JSON object.')
        op, token = probe
        if value.get('token') != token:
            return None
        require(value.get('op') == op and type(value.get('uid')) is int and value['uid'] == self.probe_uid,
                'The probe response does not match the requested operation and separate app UID.')
        require(value.get('status') in ('running', 'done', 'eof', 'error', 'timeout', 'denied', 'cancelled')
                and all(_uint(value.get(key)) for key in ('startedElapsedMs', 'sampleElapsedMs', 'bytes', 'sent', 'received')),
                'The probe returned invalid progress data.')
        fields = ('finishedElapsedMs', 'httpStatus', 'error', 'lastSuccessElapsedMs')
        require(all(field in value for field in fields), 'The probe returned incomplete result fields.')
        started, sampled = value['startedElapsedMs'], value['sampleElapsedMs']
        finished, status, error, last_stun = (value[key] for key in fields)
        require(started <= sampled and (finished is None if value['status'] == 'running'
                                       else _uint(finished) and finished == sampled)
                and (status is None or _uint(status) and 100 <= status <= 599)
                and (error is None or isinstance(error, str))
                and (last_stun is None or _uint(last_stun) and started <= last_stun <= sampled),
                'The probe returned invalid result fields.')
        return value

    def read_probe(self, probe, label):
        value = self.record(label, self.sample(probe))
        require(value is not None, 'The requested probe has no current sample.')
        return value

    def wait_probe(self, probe, predicate, timeout):
        until = time.monotonic() + timeout
        last = None
        while time.monotonic() < until:
            last = self.sample(probe)
            if last is not None:
                if predicate(last):
                    return self.record('probe_sample', last)
                if last['status'] != 'running':
                    break
            time.sleep(0.25)
        self.record('probe_last_sample', last)
        raise AcceptanceError(f'The {probe[0]} probe did not reach the expected result.')

    def fetch(self, url):
        return self.wait_probe(self.probe('fetch', url=url), lambda value: value['status'] != 'running', 35)

    def blocked(self, url):
        result = self.fetch(url)
        require(result['httpStatus'] is None and result['status'] in ('error', 'timeout'),
                f'The fresh request to {url} did not fail as expected.')

    def positive(self, url='https://example.com/', expected=200):
        value = self.fetch(url)
        require(value['status'] == 'done' and value['httpStatus'] == expected,
                f'The positive control {url} did not return HTTP {expected}.')

    def basic(self):
        value = self.wait_probe(self.probe('provider'), lambda result: result['status'] != 'running', 12)
        require(value['status'] == 'denied' and value['error'] == 'SecurityException',
                'The provider did not reject the separate app UID with a SecurityException.')
        self.positive()
        self.positive('https://accounts.youtube.com/generate_204', 204)
        self.blocked(f'https://m.youtube.com/generate_204?nonce={uuid4().hex}')
        self.positive()

    def stream(self, timeout):
        token = self.probe('stream', url=STREAM_URL)
        self.wait_probe(token, lambda value: value['status'] == 'running' and value['httpStatus'] == 200
                        and 0 < value['bytes'] < STREAM_BYTES, timeout)
        return token

    def check_stream_end(self, ended, earliest_ms):
        require(ended['httpStatus'] == 200 and ended['status'] in ('eof', 'error')
                and 0 < ended['bytes'] < STREAM_BYTES,
                'The previously live TCP stream did not end with a partial body.')
        delay = self.record('tcp_end_delay_ms', ended['finishedElapsedMs'] - earliest_ms)
        require(0 <= delay <= CUTOFF_MS, 'The partial stream ended outside the two-second cutoff window.')

    def check_tcp_refund(self, site, opened, closed, before_open, before_close, after_close):
        start = before_close['sites'][site]['session']['start_elapsed_ms']
        granted = opened['data']['granted_sec']
        initial = before_open['sites'][site]['remaining_sec']
        earlier = before_close['clock']['elapsed_ms']
        later = after_close['clock']['elapsed_ms']
        require(len({state['clock']['boot_count'] for state in (before_open, before_close, after_close)}) == 1
                and len({state['day_id'] for state in (before_open, before_close, after_close)}) == 1
                and before_open['clock']['elapsed_ms'] <= start <= earlier <= later,
                'The phone clock, boot or budget day changed during the TCP case.')
        lower_charge = (earlier - start + 999) // 1000
        upper_charge = (later - start + 999) // 1000
        charged = initial - closed['data']['remaining_sec']
        self.record('tcp_budget', {'granted_sec': granted, 'charged_sec': charged,
                                   'minimum_charge_sec': lower_charge, 'maximum_charge_sec': upper_charge,
                                   'returned_sec': granted - charged})
        require(initial >= granted and opened['data']['remaining_sec'] == initial
                and before_close['sites'][site]['remaining_sec'] == initial - lower_charge
                and before_close['sites'][site]['reserved_sec'] == granted - lower_charge,
                'Open grant accounting does not match the sampled fixture budget and elapsed time.')
        require(lower_charge <= charged <= upper_charge < granted
                and after_close['sites'][site]['remaining_sec'] == closed['data']['remaining_sec'],
                'The early-close charge or returned reserve does not match the measured time bounds.')

    def tcp(self, site):
        opened_request, opened, before_open = self.open_session(site)
        replay = self.mutate(opened_request, 'open_replay')
        require(replay == opened, 'An identical UUID did not return the original open receipt.')
        conflict = self.mutate({**opened_request, 'args': {'site': site, 'minutes': 2}}, 'changed_uuid_conflict')
        require(not conflict['ok'] and conflict['error']['code'] == 'CONFLICT',
                'A changed command using the same UUID did not return CONFLICT.')
        token = self.stream(15)
        before = self.fixture_status()
        before_close = before['data']['clock']['elapsed_ms']
        close_request, closed = self.close_session(site, before)
        ended = self.wait_probe(token, lambda value: value['status'] != 'running', 10)
        self.record('manual_close_phone_sample_ms', before_close)
        self.check_stream_end(ended, before_close)
        after = self.fixture_status()
        closed_site = after['data']['sites'][site]
        require(after['revision'] == closed['revision'] and closed_site['session'] is None,
                'The phone state changed after the fixture grant was closed.')
        self.check_tcp_refund(site, opened, closed, before_open['data'], before['data'], after['data'])
        repeated = self.mutate(close_request, 'close_replay')
        require(repeated == closed, 'A repeated close did not return the original close receipt.')
        after_repeated = self.fixture_status()
        require(after_repeated['revision'] == after['revision'] and after_repeated['data']['sites'][site] == closed_site,
                'Repeated close changed the fixture session or budget.')
        historical = self.mutate(opened_request, 'historical_open_replay')
        require(historical == opened, 'The historical open replay did not match its receipt.')
        replayed = self.fixture_status()
        require(replayed['revision'] == after['revision'] and replayed['data']['sites'][site] == closed_site,
                'Historical replay changed the fixture session or budget.')
        self.blocked(f'https://{TCP_HOST}/get?nonce={uuid4().hex}')
        self.positive()

    def udp(self, site):
        self.open_session(site)
        token = self.probe('stun', host=UDP_HOST)
        live = self.wait_probe(token, lambda value: value['status'] == 'running' and value['received'] >= 2, 15)
        before = self.fixture_status()
        before_close = before['data']['clock']['elapsed_ms']
        self.close_session(site, before)
        self.record('manual_close_phone_sample_ms', before_close)
        time.sleep(3)
        after = self.read_probe(token, 'udp_early_after_close')
        last = after['lastSuccessElapsedMs']
        require(_uint(last) and _uint(live['lastSuccessElapsedMs'])
                and live['lastSuccessElapsedMs'] <= last <= before_close + CUTOFF_MS
                and after['received'] >= live['received'],
                'The existing STUN socket received a response outside the cutoff window.')
        time.sleep(2)
        later = self.read_probe(token, 'udp_late_after_close')
        require(later['received'] == after['received'] and later['lastSuccessElapsedMs'] == last
                and later['sent'] > after['sent'] and after['sampleElapsedMs'] >= before_close + CUTOFF_MS,
                'The original STUN socket did not show continued sends with no later replies.')
        fresh_token = self.probe('stun', host=UDP_HOST)
        time.sleep(4)
        fresh = self.read_probe(fresh_token, 'udp_fresh_socket')
        require(fresh['received'] == 0 and fresh['sent'] >= 2 and fresh['lastSuccessElapsedMs'] is None,
                'The fresh STUN socket did not show multiple sends with zero replies.')

    def wait_wifi(self, state):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if self.setting('wifi_on') == state:
                self.record('wifi_on', state.decode('ascii'))
                return
            time.sleep(0.25)
        raise AcceptanceError(f'Wi-Fi did not reach the expected state ({state.decode("ascii")}).')

    def doze_preflight(self):
        power = self.bound.shell('dumpsys', 'power')
        idle = self.bound.shell('dumpsys', 'deviceidle')
        awake = re.findall(rb'^[ \t]*mWakefulness=(\w+)\b', power, re.MULTILINE) == [b'Awake']
        screen_on = re.findall(rb'^[ \t]*mScreenOn=(\w+)\b', idle, re.MULTILINE) == [b'true']
        unforced = re.findall(rb'^[ \t]*mForceIdle=(\w+)\b', idle, re.MULTILINE) == [b'false']
        states = re.findall(rb'^[ \t]*mState=(\w+)[ \t]+mLightState=(\w+)\b', idle, re.MULTILINE)
        require(awake and screen_on and unforced and states == [(b'ACTIVE', b'ACTIVE')],
                'The Doze case needs an awake phone with an unforced, active idle controller.')
        battery = self.bound.shell('dumpsys', 'battery')
        require(b'UPDATES STOPPED' not in battery.upper(),
                'Reset the existing battery simulation before the Doze case.')
        self.record('doze_preflight', {'awake': True, 'screen_on': True, 'forced_idle': False,
                                       'deep_state': 'ACTIVE', 'light_state': 'ACTIVE'})

    def enter_mode(self, mode, changes):
        if mode == 'wifi':
            require(self.setting('wifi_on') == b'1', 'Wi-Fi must be on before the Wi-Fi case starts.')
            changes.add('wifi')
            self.bound.shell('svc', 'wifi', 'disable')
            self.wait_wifi(b'0')
        elif mode == 'doze':
            self.doze_preflight()
            changes.add('battery')
            self.bound.shell('dumpsys', 'battery', 'unplug')
            changes.add('screen')
            self.bound.shell('input', 'keyevent', '223')  # sleep
            changes.add('idle')
            result = self.record('force_idle', self.bound.shell('dumpsys', 'deviceidle', 'force-idle').decode('utf-8'))
            require('forced in to deep idle mode' in result.lower(), 'Android did not confirm forced deep idle.')

    def leave_mode(self, changes):
        steps = (
            ('idle', ('dumpsys', 'deviceidle', 'unforce')),
            ('battery', ('dumpsys', 'battery', 'reset')),
            ('screen', ('input', 'keyevent', '224')),  # wake
            ('wifi', ('svc', 'wifi', 'enable')),
        )
        for name, step in steps:
            if name not in changes:
                continue
            try:
                output = self.bound.shell(*step).decode('utf-8')
                self.record('device_restore', {'command': step, 'output': output})
            except (AndroidTransportError, UnicodeError, KeyboardInterrupt) as exc:
                self.cleanup_issue(f'Could not confirm {" ".join(step)}. {exc}')
        if 'wifi' in changes:
            try:
                self.wait_wifi(b'1')
            except (AcceptanceError, AndroidTransportError, KeyboardInterrupt) as exc:
                self.cleanup_issue(f'Could not confirm Wi-Fi restoration. {exc}')

    def deadline(self, site, mode):
        _, opened, _ = self.open_session(site)
        due = opened['data']['deadline_elapsed_ms']
        token = None
        if mode == 'deadline':
            sample = self.fixture_status()['data']['clock']['elapsed_ms']
            time.sleep(max(0, (due - sample - 7_000) / 1000))
            token = self.stream(4)

        before_rpc = time.monotonic()
        before_status = self.fixture_status()
        before = before_status['data']
        remaining = (due - before['clock']['elapsed_ms']) / 1000
        require(remaining > 0 and self.grant_identity(before_status, site) == self.owned_grant,
                'The fixture session must be active before the no-ADB interval.')
        # The phone sampled elapsed time between these host readings. The later
        # bound keeps the next status call safely past the two-second window.
        earliest_due = before_rpc + remaining
        until = time.monotonic() + remaining + 4
        changes = set()
        try:
            self.enter_mode(mode, changes)
            require(time.monotonic() < earliest_due, 'The test setup could not confirm readiness before the deadline.')
            pause = max(0, until - time.monotonic())
            self.record('no_adb_interval', {'sleep_seconds': pause, 'deadline_elapsed_ms': due})
            print(f'Waiting {pause:.1f} seconds without ADB across the deadline.', flush=True)
            time.sleep(pause)
            after = self.fixture_status()['data']
            cutoff = after.get('last_cutoff_elapsed_ms')
            require(after['clock']['boot_count'] == before['clock']['boot_count']
                    and after['clock']['elapsed_ms'] >= due + 4_000,
                    'The first post-deadline status did not establish a continuous no-ADB interval.')
            require(after.get('last_transition_due_elapsed_ms') == due and _uint(cutoff)
                    and due <= cutoff <= due + CUTOFF_MS and after['sites'][site]['session'] is None,
                    'Native cutoff was missing, late, or did not correspond to the fixture deadline.')
            self.owned_grant = None
            self.record('native_cutoff_delay_ms', cutoff - due)
            if token is not None:
                self.check_stream_end(self.read_probe(token, 'deadline_stream'), due)
        finally:
            self.leave_mode(changes)
        if mode == 'wifi' and not self.cleanup_issues:
            time.sleep(5)  # Give Android time to reconnect after restoring Wi-Fi.
            self.active_status()
            self.positive()

    def run(self, case):
        self.ready()
        if case == 'basic':
            self.basic()
        else:
            self.fixture(case)
        self.check_radios()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     epilog='The basic case expects YouTube blocked with Google login allowed. '
                                            'The test probe must already be installed for user 0.')
    parser.add_argument('case', choices=CASES)
    parser.add_argument('--usb-serial', metavar='SERIAL', help='select an authorized USB phone')
    parser.add_argument('--report', metavar='FILE', type=Path, help='create a new private report outside Git')
    args = parser.parse_args(argv)
    started_at = datetime.now(timezone.utc)
    path = args.report
    try:
        if args.usb_serial is not None:
            validate_usb_serial(args.usb_serial)
        if path is None:
            directory = _receipt_path().parent
            path = directory / f"android-acceptance-{args.case}-{started_at:%Y%m%dT%H%M%S%fZ}.json"
        output = open_report(path, create_parent=args.report is None)
    except (AcceptanceError, AndroidRequestError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f'Writing private report to {path}', flush=True)
    result = {'schema': 1, 'case': args.case, 'started_at': started_at.isoformat(), 'result': 'FAIL',
              'events': [], 'cleanup_issues': []}
    with output:
        try:
            bound = AndroidTransport(usb_serial=args.usb_serial, timeout=30).bind()
            check = DeviceAcceptance(bound)
            result['events'] = check.events
            result['cleanup_issues'] = check.cleanup_issues
            try:
                check.run(args.case)
            except IndeterminateMutation:
                check.observe_indeterminate()
                raise
            if not check.cleanup_issues:
                result['result'] = 'PASS'
        except (AcceptanceError, AndroidRequestError, AndroidTransportError, CommandRefused, OSError,
                ValueError, RecursionError, KeyboardInterrupt) as exc:
            result['error'] = str(exc) or type(exc).__name__
            print(result['error'], file=sys.stderr)
        finally:
            json.dump(result, output, indent=2, ensure_ascii=True, allow_nan=False)
            output.write('\n')
        for issue in result['cleanup_issues']:
            print(issue, file=sys.stderr)
    print(f"{args.case} — {result['result']}", flush=True)
    return 0 if result['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
