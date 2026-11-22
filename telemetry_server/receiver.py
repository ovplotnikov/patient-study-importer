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
    COUNTERS_PATH, DEFAULT_CODES_PER_WEEK, MAX_CODES_PER_WEEK, MAX_REPORT_BYTES, MAX_VERSIONS_PER_WEEK,
    OTHER, OTHER_VERSION, REFUSALS, receive_report, week_of,
)

HEALTH_PATH = '/healthz'
MAX_ROWS_PER_WEEK = 6000
MAX_TOTAL = 2**53 - 1
CONTENT_TYPE = re.compile(r'application/json(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?', re.I | re.ASCII)


class CounterStore:
    """Sums by receiving week, version, group and code; accepted and refused reports by week.

    The week comes from this server's clock. Nothing else about a request is kept.
    """
    def __init__(self, database, clock=None):
        self.database = Path(database)
        self.clock = clock
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS weekly_counters (
                    week TEXT NOT NULL, app_version TEXT NOT NULL, metric TEXT NOT NULL,
                    code TEXT NOT NULL, total INTEGER NOT NULL,
                    PRIMARY KEY (week, app_version, metric, code)
                ) WITHOUT ROWID
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS reception (
                    week TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, total INTEGER NOT NULL,
                    PRIMARY KEY (week, outcome, reason)
                ) WITHOUT ROWID
            """)

    def connect(self):
        return sqlite3.connect(self.database, timeout=2)

    def week(self):
        return week_of(self.clock() if self.clock else None)

    def add(self, report):
        """Store one accepted report. False when this week is full: the report is refused."""
        week = self.week()
        with closing(self.connect()) as db, db:
            versions = {row[0] for row in db.execute(
                'SELECT DISTINCT app_version FROM weekly_counters WHERE week=?', (week,))}
            version = report['app_version']
            if version not in versions and len(versions - {OTHER_VERSION}) >= MAX_VERSIONS_PER_WEEK:
                version = OTHER_VERSION
            rows = []
            for metric, counts in report['counters'].items():
                known = {row[0] for row in db.execute(
                    'SELECT DISTINCT code FROM weekly_counters WHERE week=? AND metric=?', (week, metric))}
                limit = MAX_CODES_PER_WEEK.get(metric, DEFAULT_CODES_PER_WEEK)
                for code, count in counts.items():
                    if code not in known and len(known - {OTHER}) >= limit:
                        code = OTHER
                    known.add(code)
                    rows.append((week, version, metric, code, count, MAX_TOTAL))
            stored = db.execute('SELECT count(*) FROM weekly_counters WHERE week=?', (week,)).fetchone()[0]
            new = {row[:4] for row in rows} - {key for key in (
                tuple(item) for item in db.execute(
                    'SELECT week, app_version, metric, code FROM weekly_counters WHERE week=?', (week,)))}
            if stored + len(new) > MAX_ROWS_PER_WEEK - 1:  # The sheet of Code.gs keeps a header row.
                return False
            db.executemany("""
                INSERT INTO weekly_counters (week, app_version, metric, code, total) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (week, app_version, metric, code)
                DO UPDATE SET total = min(total + excluded.total, ?)
            """, rows)
            self._reception(db, week, 'accepted', 'schema_' + str(report['schema_version']))
        return True

    def refuse(self, reason):
        try:
            with closing(self.connect()) as db, db:
                self._reception(db, self.week(), 'refused', reason if reason in REFUSALS else 'format')
        except sqlite3.Error:
            pass

    @staticmethod
    def _reception(db, week, outcome, reason):
        db.execute("""
            INSERT INTO reception (week, outcome, reason, total) VALUES (?, ?, ?, 1)
            ON CONFLICT (week, outcome, reason) DO UPDATE SET total = min(total + 1, ?)
        """, (week, outcome, reason, MAX_TOTAL))


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
    """The report with unknown codes folded, or ValueError. Only printable ASCII is accepted."""
    if any(not 0x20 <= byte <= 0x7e for byte in body):
        raise ValueError('Not ASCII')
    payload = json.loads(body.decode('ascii'), object_pairs_hook=unique_fields,
                         parse_constant=invalid_constant)
    report, reason = receive_report(payload)
    if report is None:
        raise ValueError(reason)
    return report


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
            self.refuse(429, 'rate_limited')
            return
        if self.headers.get_all('Expect'):
            self.reply(417)
            return
        if self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Encoding'):
            self.reply(400)
            return
        content_type = self.headers.get_all('Content-Type') or []
        if len(content_type) != 1 or not CONTENT_TYPE.fullmatch(content_type[0]):
            self.refuse(415, 'content_type')
            return
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            self.reply(411 if not lengths else 400)
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_REPORT_BYTES:
            self.refuse(413 if length else 400, 'too_large' if length else 'format')
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete body')
            report = decode_report(body)
        except (ValueError, RecursionError):
            self.refuse(400, 'format')
            return
        try:
            stored = self.server.store.add(report)
        except (sqlite3.Error, OSError):
            self.refuse(503, 'storage')
            return
        if not stored:
            self.refuse(507, 'full')
            return
        self.reply(204)

    def refuse(self, code, reason):
        self.server.store.refuse(reason)
        self.reply(code)


class CounterServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, port, store, allowed_versions=(), *, per_minute=60, burst=20):
        # Any version of the right form is accepted now; the argument stays for old configurations.
        self.store = store
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
