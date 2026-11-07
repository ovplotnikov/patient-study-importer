"""Receive bounded diagnostic counters and retain aggregate totals only."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

from patient_importer.telemetry_schema import (
    COUNTERS_PATH, MAX_REPORT_BYTES, MIN_ATTEMPTS, valid_version, validate_report,
)

HEALTH_PATH = '/healthz'
MAX_TOTAL = 2**53 - 1
CONTENT_TYPE = re.compile(r'application/json(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?', re.I | re.ASCII)


class CounterStore:
    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS counters (
                    app_version TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    code TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    PRIMARY KEY (app_version, metric, code)
                ) WITHOUT ROWID
            """)

    def connect(self):
        return sqlite3.connect(self.database, timeout=2)

    def add(self, report):
        rows = [
            (report['app_version'], metric, code, count, MAX_TOTAL)
            for metric, counts in report['counters'].items()
            for code, count in counts.items()
        ]
        with closing(self.connect()) as db, db:
            db.executemany("""
                INSERT INTO counters (app_version, metric, code, total) VALUES (?, ?, ?, ?)
                ON CONFLICT (app_version, metric, code)
                DO UPDATE SET total = min(total + excluded.total, ?)
            """, rows)


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError('Invalid JSON constant')


def decode_report(body):
    payload = json.loads(body.decode('utf-8'), object_pairs_hook=unique_fields,
                         parse_constant=invalid_constant)
    return validate_report(payload)


class RequestBudget:
    """A single shared token bucket. It has no client-specific state."""
    def __init__(self, per_minute=60, burst=20):
        if per_minute <= 0 or burst <= 0:
            raise ValueError('Positive request budget required')
        self.refill = per_minute / 60
        self.capacity = burst
        self.tokens = burst
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill)
            self.updated = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'DiagnosticCounters'
    sys_version = ''

    def log_message(self, *_):
        pass

    def send_error(self, code, message=None, explain=None):
        # The standard error page reflects attacker-controlled text.
        self.reply(code)

    def handle_expect_100(self):
        self.reply(417)
        return False

    def reply(self, code):
        self.close_connection = True
        self.send_response_only(code)
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        if code != 204:
            self.send_header('Content-Length', '0')
        self.end_headers()

    def route(self, expected):
        # BaseHTTPRequestHandler normalizes some targets. Match the wire target.
        parts = self.requestline.split()
        return len(parts) == 3 and parts[1] == expected

    def do_GET(self):
        self.reply(204 if self.route(HEALTH_PATH) else 404)

    def do_POST(self):
        if not self.route(COUNTERS_PATH):
            self.reply(404)
            return
        if not self.server.budget.take():
            self.reply(429)
            return
        if self.headers.get_all('Expect'):
            self.reply(417)
            return
        if self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Encoding'):
            self.reply(400)
            return
        content_type = self.headers.get_all('Content-Type') or []
        if len(content_type) != 1 or not CONTENT_TYPE.fullmatch(content_type[0]):
            self.reply(415)
            return
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            self.reply(411 if not lengths else 400)
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_REPORT_BYTES:
            self.reply(413 if length else 400)
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete body')
            report = decode_report(body)
            if report['app_version'] not in self.server.allowed_versions:
                raise ValueError('Unknown version')
            if sum(report['counters'].get('formats', {}).values()) < MIN_ATTEMPTS:
                raise ValueError('Report below aggregate threshold')
        except (ValueError, RecursionError):
            self.reply(400)
            return
        try:
            self.server.store.add(report)
        except (sqlite3.Error, OSError):
            self.reply(503)
            return
        self.reply(204)


class CounterServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, port, store, allowed_versions, *, per_minute=60, burst=20):
        self.store = store
        self.allowed_versions = frozenset(allowed_versions)
        if not 0 < len(self.allowed_versions) <= 8 or not all(map(valid_version, self.allowed_versions)):
            raise ValueError('Configure one to eight known x.y.z versions')
        self.budget = RequestBudget(per_minute, burst)
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(('127.0.0.1', port), Handler)

    def get_request(self):
        socket, address = super().get_request()
        socket.settimeout(3)
        return socket, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # No stack traces, request data or socket addresses in stderr.
        pass
