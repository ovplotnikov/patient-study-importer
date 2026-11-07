"""Local opt-in counters. This module has no access to clinical workspaces."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.parse import unquote_to_bytes, urlsplit

import portalocker

from . import __version__
from .telemetry_schema import (
    COUNTERS_PATH, MAX_COUNT, MAX_REPORT_BYTES, METRICS, MIN_ATTEMPTS, SCHEMA_VERSION,
    valid_version, validate_counters, validate_report,
)

CONSENT_VERSION = 1
SEND_INTERVAL = 86_400
NETWORK_TIMEOUT = 4
STATE_LOCK_TIMEOUT = .25
CONSENT_LOCK_TIMEOUT = 6
MAX_STATE_BYTES = MAX_REPORT_BYTES + 1024
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
    'schema_version', 'consent_version', 'consent', 'endpoint',
    'app_version', 'last_attempt', 'counters', 'epoch',
})


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


class Diagnostics:
    def __init__(self, state_dir: Path, endpoint: str = '', app_version=__version__):
        self._directory = Path(state_dir)
        self._state = self._directory / 'diagnostics.json'
        self._endpoint = endpoint_url(endpoint)
        self._version = app_version if valid_version(app_version) else ''

    @property
    def configured(self):
        return bool(self._endpoint and self._version)

    @contextmanager
    def _lock(self, name='state', timeout=STATE_LOCK_TIMEOUT):
        self._directory.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(self._directory / f'{name}.lock', mode='a+b',
                              timeout=timeout, check_interval=.025):
            yield

    def _empty(self, consent=None, last_attempt=0, epoch=0):
        return {'schema_version': SCHEMA_VERSION, 'consent_version': CONSENT_VERSION,
                'consent': consent, 'endpoint': self._endpoint, 'app_version': self._version,
                'last_attempt': last_attempt, 'epoch': epoch, 'counters': {}}

    def _write(self, state):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self._directory, prefix='.diagnostics-',
                                             delete=False) as output:
                temporary = Path(output.name)
                output.write(json.dumps(state, separators=(',', ':'), sort_keys=True).encode('ascii'))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._state)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _load(self):
        if self._state.is_symlink():
            raise ValueError('Invalid diagnostics state')
        try:
            with self._state.open('rb') as source:
                raw = source.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return self._empty()
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('Oversized diagnostics state')
        state = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        if (type(state) is not dict or state.keys() != STATE_KEYS
                or any(type(state[key]) is not int or not 1 <= state[key] <= 9999
                       for key in ('schema_version', 'consent_version'))
                or (state['consent'] is not None and type(state['consent']) is not bool)
                or not valid_version(state['app_version'])
                or type(state['epoch']) is not int or not 0 <= state['epoch'] <= MAX_EPOCH
                or type(state['last_attempt']) is not int or not 0 <= state['last_attempt'] <= 4_102_444_800
                or type(state['endpoint']) is not str or state['endpoint'] != endpoint_url(state['endpoint'])):
            raise ValueError('Invalid diagnostics state')
        state['counters'] = validate_counters(state['counters'])
        if state['consent'] is not True and state['counters']:
            raise ValueError('Counters without consent')
        if (state['schema_version'] != SCHEMA_VERSION or state['consent_version'] != CONSENT_VERSION
                or state['endpoint'] != self._endpoint):
            # A previous decision must not activate a new destination or consent scope.
            state = self._empty(False, epoch=self._next_epoch(state))
            self._write(state)
        elif state['app_version'] != self._version:
            state = self._empty(state['consent'], state['last_attempt'], self._next_epoch(state))
            self._write(state)
        return state

    @staticmethod
    def _next_epoch(state):
        # A local sequence guards the acknowledgement of an in-flight report.
        # It is not included in the wire contract.
        return (state['epoch'] + 1) % (MAX_EPOCH + 1)

    @property
    def enabled(self):
        if not self.configured:
            return False
        try:
            with self._lock():
                return self._load()['consent'] is True
        except Exception:
            return False

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
                if self._load()['consent'] is not None:
                    return False
                self._write(self._empty(False))
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
                if allowed and state['consent'] is True:
                    return True
                self._write(self._empty(allowed, state['last_attempt'], self._next_epoch(state)))
                return True
        except Exception:
            return False

    def record(self, metric, code, count=1):
        if (not self.configured or type(metric) is not str or type(code) is not str
                or type(count) is not int or not 1 <= count <= MAX_COUNT
                or code not in METRICS.get(metric, ())):
            return False
        try:
            with self._lock():
                state = self._load()
                if state['consent'] is not True:
                    return False
                bucket = state['counters'].setdefault(metric, {})
                bucket[code] = min(MAX_COUNT, bucket.get(code, 0) + count)
                self._write(state)
                return True
        except Exception:
            return False

    def _report(self, state):
        if state['consent'] is not True or not state['counters']:
            return None
        return validate_report({'schema_version': SCHEMA_VERSION, 'app_version': self._version,
                                'counters': state['counters']})

    def snapshot(self):
        if not self.configured:
            return None
        try:
            with self._lock():
                return self._report(self._load())
        except Exception:
            return None

    def send_if_due(self):
        if not self.configured:
            return False
        try:
            with self._lock('send', timeout=0):
                with self._lock():
                    state = self._load()
                    report = self._report(state)
                    now = int(time.time())
                    if (report is None or sum(report['counters'].get('formats', {}).values()) < MIN_ATTEMPTS
                            or state['last_attempt'] and now - state['last_attempt'] < SEND_INTERVAL):
                        return False
                    body = json.dumps(report, separators=(',', ':'), sort_keys=True).encode('ascii')
                    if len(body) > MAX_REPORT_BYTES:
                        return False
                    state['last_attempt'] = now
                    epoch = state['epoch']
                    self._write(state)
                # Cancel a reserved report if consent changed before dispatch.
                # A request already in progress cannot be recalled.
                with self._lock():
                    state = self._load()
                    if state['consent'] is not True or state['epoch'] != epoch:
                        return False
                if not post_report(self._endpoint, body):
                    return False
                with self._lock(timeout=CONSENT_LOCK_TIMEOUT):
                    state = self._load()
                    if state['consent'] is not True or state['epoch'] != epoch:
                        return True
                    for metric, sent in report['counters'].items():
                        bucket = state['counters'].get(metric, {})
                        for code, count in sent.items():
                            remaining = bucket.get(code, 0) - count
                            if remaining > 0:
                                bucket[code] = remaining
                            else:
                                bucket.pop(code, None)
                        if not bucket:
                            state['counters'].pop(metric, None)
                    self._write(state)
                return True
        except Exception:
            return False
