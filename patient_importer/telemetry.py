"""Local opt-in counters. This module has no access to clinical workspaces."""
from contextlib import contextmanager
from datetime import date, datetime
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import threading
import time
from urllib.parse import unquote_to_bytes, urlsplit

import portalocker

from . import __version__
from . import telemetry_schema as schema
from .telemetry_schema import (
    COUNTERS_PATH, MAX_COUNT, MAX_REPORT_BYTES, SCHEMA_VERSION,
    valid_version, validate_counters, validate_report,
)

# 2: weekly activity, actions in windows, failure places and computer ranges. Asked again.
CONSENT_VERSION = 2
STATE_VERSION = 2
SEND_INTERVAL = 86_400
NETWORK_TIMEOUT = 4
STATE_LOCK_TIMEOUT = .25
CONSENT_LOCK_TIMEOUT = 6
MAX_STATE_BYTES = 96 * 1024
# Counts of older versions wait for delivery after an update; the oldest go first when full.
MAX_QUEUED_VERSIONS = 6
EPOCH_CACHE_SECONDS = 30
MAX_EPOCH = (1 << 63) - 1
HOST_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
APPS_SCRIPT_HOST = 'script.google.com'
APPS_SCRIPT_PATH = re.compile(r'/macros/s/[A-Za-z0-9_-]{20,256}/exec')
APPS_ACK_HOST = 'script.googleusercontent.com'
APPS_ACK_PATH = '/macros/echo'
MAX_ACK_URL = 2048
MAX_ACK_BYTES = 128
ACK_PARAMETERS = frozenset({'user_content_key', 'lib'})
ACK_TOKEN = re.compile(r'(?:[A-Za-z0-9_+=./~-]|%[0-9A-Fa-f]{2})+')
ACK_DECODED_TOKEN = re.compile(rb'[A-Za-z0-9_+=./~-]+')
ACK_CONTENT_TYPE = re.compile(r'application/json(?:\s*;\s*charset\s*=\s*(?:"utf-8"|utf-8))?', re.I)
STATE_KEYS = frozenset({
    'state_version', 'consent_version', 'consent', 'endpoint', 'last_attempt', 'epoch',
    'queue', 'first_week', 'active_week', 'last_version', 'session',
})
LEGACY_STATE_KEYS = frozenset({
    'schema_version', 'consent_version', 'consent', 'endpoint',
    'app_version', 'last_attempt', 'counters', 'epoch',
})
WEEK = re.compile(r'[0-9]{4}-W[0-9]{2}')
SESSIONS = (None, 'idle', 'processing')


def endpoint_url(value):
    """Accept only an explicitly configured origin and the fixed route."""
    if type(value) is not str or not value or len(value) > 360 or not value.isascii():
        return ''
    if not value.startswith('https://') or any(not 33 <= ord(char) <= 126 for char in value):
        return ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        labels = host.split('.')
        if (parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or len(host) > 253 or len(labels) < 2
                or not all(HOST_LABEL.fullmatch(label) for label in labels)
                or not any(char.isalpha() for char in labels[-1])):
            return ''
        if host == APPS_SCRIPT_HOST:
            if not APPS_SCRIPT_PATH.fullmatch(parsed.path):
                return ''
        elif host == APPS_ACK_HOST or parsed.path != COUNTERS_PATH:
            return ''
        # Exact comparison rejects an empty query/fragment, backslashes and odd netloc spellings.
        if value not in (f'https://{host}{parsed.path}', f'https://{host}:443{parsed.path}'):
            return ''
        return f'https://{host}{parsed.path}'
    except ValueError:
        return ''


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate diagnostics key')
        result[key] = value
    return result


def apps_ack_target(value):
    """Validate the Google-supplied one-time acknowledgement URL."""
    if type(value) is not str or len(value) > MAX_ACK_URL:
        return ''
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ''
    if (parsed.scheme != 'https' or parsed.netloc not in (APPS_ACK_HOST, APPS_ACK_HOST + ':443')
            or parsed.path != APPS_ACK_PATH or not parsed.query
            or value != f'https://{parsed.netloc}{APPS_ACK_PATH}?{parsed.query}'):
        return ''
    keys = set()
    for pair in parsed.query.split('&'):
        key, separator, token = pair.partition('=')
        if (not separator or key not in ACK_PARAMETERS or key in keys
                or not ACK_TOKEN.fullmatch(token)
                or not ACK_DECODED_TOKEN.fullmatch(unquote_to_bytes(token))):
            return ''
        keys.add(key)
    if keys != ACK_PARAMETERS:
        return ''
    return f'{APPS_ACK_PATH}?{parsed.query}'


def fetch_apps_ack(target, context):
    connection = http.client.HTTPSConnection(APPS_ACK_HOST, timeout=NETWORK_TIMEOUT, context=context)
    try:
        # Only this bodyless request may follow Google's ContentService redirect.
        connection.request('GET', target)
        response = connection.getresponse()
        content_type = response.getheader('Content-Type', '')
        if response.status != 200 or not ACK_CONTENT_TYPE.fullmatch(content_type.strip()):
            return False
        body = response.read(MAX_ACK_BYTES + 1)
        if len(body) > MAX_ACK_BYTES:
            return False
        try:
            parsed = json.loads(body.decode('utf-8'), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            return False
        return type(parsed) is dict and parsed.keys() == {'ok'} and parsed['ok'] is True
    finally:
        connection.close()


def post_report(endpoint, body):
    """Post directly over TLS. The optional Apps Script ACK is one bodyless GET."""
    endpoint = endpoint_url(endpoint)
    if not endpoint:
        return False
    parsed = urlsplit(endpoint)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=NETWORK_TIMEOUT, context=context)
    try:
        connection.request('POST', parsed.path, body=body, headers={
            'Content-Type': 'application/json',
            'User-Agent': 'PatientStudyImporter-Diagnostics',
        })
        response = connection.getresponse()
        if parsed.hostname != APPS_SCRIPT_HOST:
            return response.status == 204
        if response.status not in (302, 303):
            return False
        target = apps_ack_target(response.getheader('Location', ''))
    finally:
        connection.close()
    return bool(target) and fetch_apps_ack(target, context)


def local_week(day=None):
    """This computer's calendar week. It stays here: only ranges derived from it are counted."""
    year, week, _ = (day or date.today()).isocalendar()
    return f'{year}-W{week:02d}'


def week_start(label):
    year, week = label.split('-W')
    return date.fromisocalendar(int(year), int(week), 1)


def install_age(first_week, today=None):
    days = ((today or date.today()) - week_start(first_week)).days
    return schema.bucket(days, (7, 28, 91), schema.AGES)


def total_memory():
    try:
        if os.name == 'nt':
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                            ('total', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong),
                            ('page_total', ctypes.c_ulonglong), ('page_free', ctypes.c_ulonglong),
                            ('virtual_total', ctypes.c_ulonglong), ('virtual_free', ctypes.c_ulonglong),
                            ('extended', ctypes.c_ulonglong)]
            status = Status()
            status.length = ctypes.sizeof(Status)
            return status.total if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
        return os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except Exception:
        return None


def system_code():
    if os.name != 'nt':
        return 'other'
    try:
        release = sys.getwindowsversion()
    except Exception:
        return 'windows_other'
    if release.major == 10 and release.build >= 22000:
        return 'win11'
    return 'win10' if release.major == 10 else 'windows_other'


def activity_counters(summary, *, first_week, scale=None, today=None):
    """One week of one installation, in ranges only. No identifier: weeks are summed."""
    documents = summary.get('documents', 0) if summary else 0
    codes = [
        'active', 'age.' + install_age(first_week, today), 'os.' + system_code(),
        'cores.' + schema.count_bucket(os.cpu_count() or 1, schema.CORES),
        'memory.' + schema.memory_bucket(total_memory()), 'scale.' + schema.scale_bucket(scale),
        'documents.' + schema.count_bucket(documents, schema.DOCUMENTS),
        'patients.' + schema.count_bucket(summary.get('patients', 0) if summary else 0, schema.PATIENTS),
        'waiting.' + schema.count_bucket(summary.get('total_review', 0) if summary else 0, schema.WAITING),
        'flagged.' + schema.share_bucket(summary.get('quality_review', 0) if summary else 0, documents),
    ]
    return {'activity': {code: 1 for code in codes}}


def split_report(version, counters, limit=MAX_REPORT_BYTES):
    """Reports of one version, each within the size a receiver accepts."""
    reports, current = [], {}

    def body(part):
        return json.dumps({'schema_version': SCHEMA_VERSION, 'app_version': version, 'counters': part},
                          separators=(',', ':'), sort_keys=True).encode('ascii')

    for metric in sorted(counters):
        for code in sorted(counters[metric]):
            trial = {name: dict(bucket) for name, bucket in current.items()}
            trial.setdefault(metric, {})[code] = counters[metric][code]
            if current and len(body(trial)) > limit:
                reports.append(current)
                trial = {metric: {code: counters[metric][code]}}
            current = trial
    if current:
        reports.append(current)
    return [(part, body(part)) for part in reports]


class Diagnostics:
    def __init__(self, state_dir: Path, endpoint: str = '', app_version=__version__):
        self._directory = Path(state_dir)
        self._state = self._directory / 'diagnostics.json'
        self._endpoint = endpoint_url(endpoint)
        self._version = app_version if valid_version(app_version) else ''
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._epoch_cache = (None, 0.0)

    @property
    def configured(self):
        return bool(self._endpoint and self._version)

    @contextmanager
    def _lock(self, name='state', timeout=STATE_LOCK_TIMEOUT):
        self._directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(self._directory / f'{name}.lock', mode='a+b',
                              timeout=timeout, check_interval=.025):
            yield

    def _empty(self, consent=None, last_attempt=0, epoch=0, *, keep=None):
        keep = keep or {}
        return {'state_version': STATE_VERSION, 'consent_version': CONSENT_VERSION,
                'consent': consent, 'endpoint': self._endpoint, 'last_attempt': last_attempt,
                'epoch': epoch, 'queue': {}, 'first_week': keep.get('first_week'),
                'active_week': None, 'last_version': keep.get('last_version'), 'session': None}

    def _write(self, state):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self._directory, prefix='.diagnostics-',
                                             delete=False) as output:
                temporary = Path(output.name)
                output.write(self._serialize(state))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._state)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _serialize(state):
        return json.dumps(state, separators=(',', ':'), sort_keys=True).encode('ascii')

    def _fit(self, state):
        """Keep the state bounded: the counts of the oldest versions go first."""
        while len(self._serialize(state)) > MAX_STATE_BYTES or len(state['queue']) > MAX_QUEUED_VERSIONS:
            others = sorted((v for v in state['queue'] if v != self._version),
                            key=lambda value: tuple(map(int, value.split('.'))))
            if not others:
                return False
            del state['queue'][others[0]]
        return True

    @staticmethod
    def _valid_state(state):
        return (type(state) is dict and state.keys() == STATE_KEYS
                and all(type(state[key]) is int and 1 <= state[key] <= 9999
                        for key in ('state_version', 'consent_version'))
                and (state['consent'] is None or type(state['consent']) is bool)
                and type(state['epoch']) is int and 0 <= state['epoch'] <= MAX_EPOCH
                and type(state['last_attempt']) is int and 0 <= state['last_attempt'] <= 4_102_444_800
                and type(state['endpoint']) is str and state['endpoint'] == endpoint_url(state['endpoint'])
                and all(value is None or (type(value) is str and WEEK.fullmatch(value))
                        for value in (state['first_week'], state['active_week']))
                and (state['last_version'] is None or valid_version(state['last_version']))
                and state['session'] in SESSIONS
                and type(state['queue']) is dict
                and all(valid_version(version) for version in state['queue']))

    def _load(self, *, expected_epoch=None):
        """Read state under lock. An existing operation must stay in its original scope."""
        if self._state.is_symlink():
            raise ValueError('Invalid diagnostics state')
        try:
            with self._state.open('rb') as source:
                raw = source.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return self._empty() if expected_epoch is None else None
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('Oversized diagnostics state')
        state = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        if type(state) is dict and state.keys() == LEGACY_STATE_KEYS:
            # Consent of the first contract does not cover this one: the doctor is asked again.
            if expected_epoch is not None:
                return None
            epoch = state.get('epoch') if type(state.get('epoch')) is int and 0 <= state['epoch'] <= MAX_EPOCH else 0
            state = self._empty(None, epoch=(epoch + 1) % (MAX_EPOCH + 1))
            self._write(state)
            return state
        if not self._valid_state(state):
            raise ValueError('Invalid diagnostics state')
        state['queue'] = {version: validate_counters(counters) for version, counters in state['queue'].items()}
        state['queue'] = {version: counters for version, counters in state['queue'].items() if counters}
        if state['consent'] is not True and state['queue']:
            raise ValueError('Counters without consent')
        same_consent = state['consent_version'] == CONSENT_VERSION and state['state_version'] == STATE_VERSION
        same_destination = state['endpoint'] == self._endpoint
        if expected_epoch is not None:
            # A stale worker must not migrate or clear the current scope.
            if (not same_consent or not same_destination or state['consent'] is not True
                    or state['epoch'] != expected_epoch):
                return None
            return state
        if not same_consent:
            state = self._empty(None, epoch=self._next_epoch(state), keep=state)
            self._write(state)
        elif not same_destination:
            # A previous decision must not activate a new destination.
            state = self._empty(False, epoch=self._next_epoch(state), keep=state)
            self._write(state)
        return state

    @staticmethod
    def _next_epoch(state):
        # The local sequence guards batches and acknowledgements across consent changes.
        return (state['epoch'] + 1) % (MAX_EPOCH + 1)

    @property
    def enabled(self):
        return self.consent_epoch() is not None

    def consent_epoch(self):
        """Capture the local consent generation before starting a diagnostics event."""
        if not self.configured:
            return None
        try:
            if not self._state.exists():
                epoch = None
            else:
                with self._lock():
                    state = self._load()
                    epoch = state['epoch'] if state['consent'] is True else None
        except Exception:
            epoch = None
        self._epoch_cache = (epoch, time.monotonic())
        return epoch

    def _cached_epoch(self):
        epoch, checked = self._epoch_cache
        if time.monotonic() - checked > EPOCH_CACHE_SECONDS:
            return self.consent_epoch()
        return epoch

    def should_prompt(self):
        if not self.configured:
            return False
        try:
            with self._lock():
                return self._load()['consent'] is None
        except Exception:
            return False

    def mark_prompted(self):
        """Reserve the only prompt. Closing the dialog leaves diagnostics off."""
        if not self.configured:
            return False
        try:
            with self._lock():
                state = self._load()
                if state['consent'] is not None:
                    return False
                self._write(self._empty(False, epoch=state['epoch'], keep=state))
                return True
        except Exception:
            return False

    def set_consent(self, allowed):
        if type(allowed) is not bool or not self._version or (allowed and not self.configured):
            return False
        if not self.configured and not self._state.exists():
            return True
        try:
            # Withdrawal never waits for an HTTP response, DNS or the send lock.
            with self._lock(timeout=CONSENT_LOCK_TIMEOUT):
                try:
                    state = self._load()
                except (OSError, ValueError, RecursionError):
                    state = self._empty(False)
                with self._pending_lock:
                    self._pending.clear()
                if allowed and state['consent'] is True:
                    return True
                self._write(self._empty(allowed, state['last_attempt'], self._next_epoch(state), keep=state))
                self._epoch_cache = (None, 0.0)
                return True
        except Exception:
            return False

    def record(self, metric, code, count=1):
        if type(metric) is not str or type(code) is not str:
            return False
        return self._record_batch({metric: {code: count}})

    def record_batch(self, counters, *, epoch):
        """Store a batch only while the consent captured at its start still applies."""
        if type(epoch) is not int or not 0 <= epoch <= MAX_EPOCH:
            return False
        return self._record_batch(counters, epoch=epoch)

    @staticmethod
    def _add(queue, version, clean):
        target = queue.setdefault(version, {})
        for metric, counts in clean.items():
            bucket = target.setdefault(metric, {})
            for code, count in counts.items():
                bucket[code] = min(MAX_COUNT, bucket.get(code, 0) + count)

    def _record_batch(self, counters, *, epoch=None):
        if not self.configured:
            return False
        try:
            clean = validate_counters(counters)
            if not clean:
                return False
            with self._lock():
                state = self._load(expected_epoch=epoch)
                if state is None or state['consent'] is not True:
                    return False
                self._add(state['queue'], self._version, clean)
                if not self._fit(state):
                    return False
                self._write(state)
                return True
        except Exception:
            return False

    # Actions in windows are noted in memory and written in groups.

    def note(self, metric, code, count=1):
        """Count one action. Never blocks on the disk and never raises."""
        try:
            if not schema.valid_code(metric, code) or not schema.valid_count(count):
                return False
            epoch = self._cached_epoch()
            if epoch is None:
                return False
            with self._pending_lock:
                bucket = self._pending.setdefault(epoch, {}).setdefault(metric, {})
                bucket[code] = min(MAX_COUNT, bucket.get(code, 0) + count)
            return True
        except Exception:
            return False

    def flush(self):
        try:
            with self._pending_lock:
                pending, self._pending = self._pending, {}
            for epoch, counters in pending.items():
                self.record_batch(counters, epoch=epoch)
        except Exception:
            pass

    def _update(self, change):
        """Change the state under lock, while consent is given. change(state) returns counters to add."""
        if not self.configured:
            return False
        try:
            with self._lock():
                state = self._load()
                before = self._serialize(state)
                added = change(state)
                if not added and self._serialize(state) == before and self._state.exists():
                    return True
                if added and state['consent'] is True:
                    self._add(state['queue'], self._version, validate_counters(added))
                if not self._fit(state):
                    return False
                self._write(state)
                return True
        except Exception:
            return False

    def note_start(self):
        """Program start: first week, an update that took effect, a session that did not end."""
        def change(state):
            state['first_week'] = state['first_week'] or local_week()
            added = {}
            if state['consent'] is True:
                path = {'start': 1}
                if state['last_version'] and state['last_version'] != self._version:
                    path['update_installed'] = 1
                added['path'] = path
                if state['session']:
                    added['errors'] = {'session_crash.' + state['session']: 1}
                state['session'] = 'idle'
            state['last_version'] = self._version
            return added
        return self._update(change)

    def note_session(self, phase):
        """'processing' while documents are read, 'idle' otherwise, None on a normal exit."""
        if phase not in SESSIONS:
            return False

        def change(state):
            if state['consent'] is True or phase is None:
                state['session'] = phase
            return {}
        return self._update(change)

    def note_week(self, summary, scale=None):
        """At most one activity record per calendar week of this computer."""
        week = local_week()

        def change(state):
            if state['consent'] is not True or state['active_week'] == week:
                return {}
            state['first_week'] = state['first_week'] or week
            state['active_week'] = week
            return activity_counters(summary, first_week=state['first_week'], scale=scale)
        return self._update(change)

    def _reports(self, state):
        if state['consent'] is not True:
            return []
        reports = []
        for version in sorted(state['queue'], key=lambda value: tuple(map(int, value.split('.')))):
            for counters, body in split_report(version, state['queue'][version]):
                validate_report({'schema_version': SCHEMA_VERSION, 'app_version': version, 'counters': counters})
                reports.append((version, counters, body))
        return reports

    def snapshot(self):
        """The counts of this version waiting to be sent, as one report, or None."""
        if not self.configured:
            return None
        try:
            with self._lock():
                state = self._load()
            counters = state['queue'].get(self._version) if state['consent'] is True else None
            return validate_report({'schema_version': SCHEMA_VERSION, 'app_version': self._version,
                                    'counters': counters}) if counters else None
        except Exception:
            return None

    def queued_versions(self):
        """Versions with counts waiting. An update keeps the counts of the earlier version."""
        if not self.configured:
            return []
        try:
            with self._lock():
                state = self._load()
            return sorted(state['queue'], key=lambda value: tuple(map(int, value.split('.'))))
        except Exception:
            return []

    def send_if_due(self):
        """Once a day everything queued goes, oldest version first. A refusal keeps it queued."""
        if not self.configured:
            return False
        self.flush()
        try:
            with self._lock('send', timeout=0):
                with self._lock():
                    state = self._load()
                    reports = self._reports(state)
                    now = int(time.time())
                    if not reports or state['last_attempt'] and now - state['last_attempt'] < SEND_INTERVAL:
                        return False
                    state['last_attempt'] = now
                    epoch = state['epoch']
                    self._write(state)
                for version, sent, body in reports:
                    # Cancel a reserved report if consent changed before dispatch.
                    # A request already in progress cannot be recalled.
                    with self._lock():
                        if self._load(expected_epoch=epoch) is None:
                            return False
                    if not post_report(self._endpoint, body):
                        return False
                    with self._lock(timeout=CONSENT_LOCK_TIMEOUT):
                        state = self._load(expected_epoch=epoch)
                        if state is None:
                            return True
                        queued = state['queue'].get(version, {})
                        for metric, codes in sent.items():
                            bucket = queued.get(metric, {})
                            for code, count in codes.items():
                                remaining = bucket.get(code, 0) - count
                                if remaining > 0:
                                    bucket[code] = remaining
                                else:
                                    bucket.pop(code, None)
                            if not bucket:
                                queued.pop(metric, None)
                        if not queued:
                            state['queue'].pop(version, None)
                        self._write(state)
                return True
        except Exception:
            return False
