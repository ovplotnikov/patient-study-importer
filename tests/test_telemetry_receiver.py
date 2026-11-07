"""Real loopback HTTP requests with synthetic diagnostics only."""
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import socket
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from patient_importer import extraction
from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace
from patient_importer.telemetry import Diagnostics
from telemetry_server.receiver import COUNTERS_PATH, HEALTH_PATH, CounterServer, CounterStore


MARKER = 'SYNTHETIC_PRIVATE_MARKER'


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='psi-test-receiver-')
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / 'counts.sqlite3'
        self.store = CounterStore(self.database)
        self.server = CounterServer(0, self.store, {'2.4.0'})
        self.port = self.server.server_address[1]
        self.logs = io.StringIO()
        stderr = redirect_stderr(self.logs)
        stdout = redirect_stdout(self.logs)
        stderr.__enter__()
        stdout.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        self.addCleanup(stdout.__exit__, None, None, None)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    @staticmethod
    def report(*, version='2.4.0', count=10):
        return {'schema_version': 1, 'app_version': version,
                'counters': {'formats': {'pdf': count}, 'outcomes': {'saved': count}}}

    def send(self, body, *, path=COUNTERS_PATH, content_type='application/json'):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request('POST', path, body=body, headers={'Content-Type': content_type,
                                                                'X-Forwarded-For': MARKER})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def send_raw(self, request):
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := connection.recv(4096):
                chunks.append(chunk)
        result = b''.join(chunks)
        self.assertNotIn(MARKER.encode(), result)
        return int(result.split(b' ', 2)[1])

    def rows(self):
        with sqlite3.connect(self.database) as connection:
            return connection.execute('SELECT * FROM counters ORDER BY app_version, metric, code').fetchall()

    def test_aggregate_totals_only(self):
        self.assertEqual(self.send(json.dumps(self.report())), (204, b''))
        self.assertEqual(self.send(json.dumps(self.report(count=12)), content_type='application/json; charset=UTF-8'), (204, b''))
        self.assertEqual(self.rows(), [('2.4.0', 'formats', 'pdf', 22), ('2.4.0', 'outcomes', 'saved', 22)])
        with sqlite3.connect(self.database) as connection:
            columns = [row[1] for row in connection.execute('PRAGMA table_info(counters)')]
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(columns, ['app_version', 'metric', 'code', 'total'])
        self.assertEqual(tables, [('counters',)])
        self.assertNotIn(MARKER.encode(), self.database.read_bytes())
        self.assertEqual(self.logs.getvalue(), '')

    def test_rejects_unexpected_fields_and_untrusted_values_before_writing(self):
        self.assertEqual(self.send(json.dumps(self.report())), (204, b''))
        baseline = self.rows()
        cases = [
            {**self.report(), 'filename': MARKER},
            {**self.report(), 'counters': {'formats': {'pdf': 10, MARKER: 10}}},
            self.report(version='9.2718.42'),
            self.report(count=9),
            self.report(count=True),
            self.report(count=1_000_001),
            self.report(count=10.5),
        ]
        for report in cases:
            with self.subTest(report=report):
                self.assertEqual(self.send(json.dumps(report)), (400, b''))
        self.assertEqual(self.rows(), baseline)
        self.assertNotIn(MARKER.encode(), self.database.read_bytes())
        self.assertEqual(self.logs.getvalue(), '')

    def test_rejects_malformed_json_duplicate_keys_and_wrong_route(self):
        report = json.dumps(self.report()).encode()
        cases = [
            report[:-1],
            report + b'\xff',
            report.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'),
            report.replace(b'"pdf": 10', b'"pdf": 10, "pdf": 10'),
            report.replace(b'"pdf": 10', b'"pdf": NaN'),
            b'[' * 2000 + b']' * 2000,
        ]
        for body in cases:
            self.assertEqual(self.send(body), (400, b''))
        for path in (COUNTERS_PATH + '?' + MARKER, '/v1%2fcounters', '/' + COUNTERS_PATH, '/'):
            self.assertEqual(self.send(report, path=path), (404, b''))
        self.assertEqual(self.send(report, content_type='multipart/form-data; boundary=' + MARKER), (415, b''))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.logs.getvalue(), '')

    def test_bounds_body_and_does_not_reflect_parse_errors(self):
        start = b'POST /v1/counters HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
        cases = [
            (b'\r\n', 411),
            (b'Content-Length: 5\r\nContent-Length: 5\r\n\r\n', 400),
            (b'Content-Length: -1\r\n\r\n', 400),
            (b'Content-Length: 5\r\n\r\n{}', 400),
            (b'Content-Length: 8193\r\n\r\n', 413),
            (b'Transfer-Encoding: chunked\r\n\r\n', 400),
            (b'Content-Encoding: gzip\r\nContent-Length: 5\r\n\r\n', 400),
            (b'Expect: 100-continue\r\nContent-Length: 5\r\n\r\n', 417),
        ]
        for headers, status in cases:
            self.assertEqual(self.send_raw(start + headers), status)
        self.assertEqual(self.send_raw(f'{MARKER} / HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode()), 501)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.logs.getvalue(), '')

    def test_health_rate_limit_and_write_failure_do_not_expose_details(self):
        health = f'GET {HEALTH_PATH} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode()
        self.assertEqual(self.send_raw(health), 204)
        self.assertEqual(self.send_raw(health.replace(b'/healthz', b'/healthz?test')), 404)
        body = json.dumps(self.report())
        with patch.object(self.store, 'add', side_effect=sqlite3.OperationalError(MARKER)):
            self.assertEqual(self.send(body), (503, b''))
        with patch.object(self.server.budget, 'take', return_value=False):
            self.assertEqual(self.send(body), (429, b''))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.logs.getvalue(), '')

    def send_client_over_loopback(self, client):
        serialized = []

        class RecordingConnection(HTTPConnection):
            def send(self, data):
                serialized.append(bytes(data))
                super().send(data)

        # Only the connection is substituted. http.client serializes and sends
        # the real request to the real receiver. This check does not use TLS.
        with patch('patient_importer.telemetry.http.client.HTTPSConnection',
                   side_effect=lambda *args, **kwargs: RecordingConnection('127.0.0.1', self.port, timeout=3)) as connect:
            self.assertTrue(client.send_if_due())
            connect.assert_called_once()
        wire = b''.join(serialized)
        headers, body = wire.split(b'\r\n\r\n', 1)
        self.assertTrue(headers.startswith(b'POST /v1/counters HTTP/1.1\r\n'))
        self.assertNotIn(MARKER.encode(), wire)
        self.assertNotIn(b'X-Forwarded-For', headers)
        self.assertIsNone(client.snapshot())
        return json.loads(body)

    def make_client(self):
        client = Diagnostics(self.database.parent / 'client',
                             endpoint='https://diagnostics.example.com/v1/counters', app_version='2.4.0')
        self.assertTrue(client.set_consent(True))
        return client

    def test_client_serializes_only_allowlisted_counters_over_loopback(self):
        client = self.make_client()
        self.assertFalse(client.record('review_reasons', MARKER))
        self.assertTrue(client.record('formats', 'pdf', 10))
        self.assertTrue(client.record('outcomes', 'saved', 10))
        self.assertEqual(self.send_client_over_loopback(client), self.report())
        self.assertEqual(self.rows(), [('2.4.0', 'formats', 'pdf', 10), ('2.4.0', 'outcomes', 'saved', 10)])

    def test_workspace_review_and_errors_do_not_cross_wire_boundary(self):
        client = self.make_client()
        workspace = Workspace(self.database.parent / (MARKER + '_directory'), diagnostics=client)
        incoming = workspace.root / 'Входящие'
        for index in range(10):
            text = SAMPLE.replace('71.5', str(71.5 + index))
            if index == 0:
                text = text.replace('Креатинин', MARKER + '_label').replace('мкмоль/л', MARKER + '_unit')
                text = text.replace('(40 - 90)', '(' + MARKER + '_reference)') + '\n' + MARKER + '_ocr_page'
            if index == 9:
                text += '\n' + MARKER + '_failure_trigger'
            (incoming / f'{index}-{MARKER}_filename.txt').write_text(text, encoding='utf-8')

        native_extract = extraction.extract

        def synthetic_extract(path):
            data = native_extract(path)
            if MARKER + '_failure_trigger' in data['pages'][0]:
                raise ValueError(MARKER + '_raw_exception')
            if MARKER + '_ocr_page' in data['pages'][0]:
                data['native_pages'] = [SAMPLE]
                data['ocr_pages'] = [1]
            return data

        with patch.object(extraction, 'extract', side_effect=synthetic_extract):
            result = workspace.import_folder()
        self.assertEqual((result['documents'], result['errors'], result['quality_review']), (9, 1, 1))
        record, = workspace.quality_review_list()
        rows = record['observations']
        rows[0].update(result=MARKER + '_manual_value', unit='мкмоль/л')
        workspace.confirm_quality(record['id'], observations=rows, note=MARKER + '_manual_note',
                                  expected_revision=record['revision'])
        self.assertNotIn(MARKER.encode(), (self.database.parent / 'client' / 'diagnostics.json').read_bytes())

        report = self.send_client_over_loopback(client)
        counters = report['counters']
        self.assertEqual(counters['formats'], {'txt': 10})
        self.assertEqual(counters['outcomes'], {'saved': 8, 'error': 1, 'quality_review': 1})
        self.assertEqual(counters['failure_stages'], {'extraction': 1})
        self.assertEqual(counters['edit_categories']['result'], 1)
        self.assertEqual(counters['edit_categories']['unit'], 1)
        self.assertEqual(self.rows(), sorted(
            ('2.4.0', metric, code, count)
            for metric, values in counters.items() for code, count in values.items()
        ))
        self.assertNotIn(MARKER.encode(), self.database.read_bytes())
        self.assertEqual(self.logs.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
