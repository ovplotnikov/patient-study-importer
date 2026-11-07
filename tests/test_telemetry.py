from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from patient_importer.telemetry import (
    Diagnostics, MAX_ACK_BYTES, SEND_INTERVAL, apps_ack_target, endpoint_url, post_report,
)
from patient_importer.telemetry_schema import MAX_COUNT, validate_report

URL = 'https://metrics.example.com/v1/counters'
VERSION = '2.4.0'
CANARY = r'C:\Данные\ТЕСТОВ ТЕСТ ТЕСТОВИЧ\гемоглобин 13,7.pdf'


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / 'diagnostics'
        self.diag = Diagnostics(self.folder, URL, app_version=VERSION)
        self.state = self.folder / 'diagnostics.json'

    def enable(self):
        self.assertTrue(self.diag.set_consent(True))

    def test_disabled_without_config_or_consent(self):
        for endpoint in ('', 'http://metrics.example.com/v1/counters',
                         'https://metrics.example.com/v1/counters?path=private',
                         'https://user:password@metrics.example.com/v1/counters',
                         'https://metrics.example.com/other',
                         'https://metrics.example.com:444/v1/counters'):
            disabled = Diagnostics(self.folder, endpoint, app_version=VERSION)
            with self.subTest(endpoint=endpoint), patch('patient_importer.telemetry.post_report') as post:
                self.assertFalse(disabled.configured)
                self.assertFalse(disabled.should_prompt())
                self.assertFalse(disabled.set_consent(True))
                self.assertFalse(disabled.record('formats', 'pdf', 10))
                self.assertFalse(disabled.send_if_due())
                self.assertFalse(self.folder.exists())
                post.assert_not_called()
        with patch('patient_importer.telemetry.post_report') as post:
            self.assertTrue(self.diag.should_prompt())
            self.assertFalse(self.diag.enabled)
            self.assertFalse(self.diag.record('formats', 'pdf', 10))
            self.assertIsNone(self.diag.snapshot())
            self.assertFalse(self.diag.send_if_due())
            self.assertFalse(self.state.exists())
            post.assert_not_called()

    def test_one_prompt_and_refusal_survive_updates(self):
        second = Diagnostics(self.folder, URL, app_version=VERSION)
        self.assertTrue(self.diag.mark_prompted())
        self.assertFalse(second.mark_prompted())
        for version in ('2.4.0', '2.5.0', '2.3.0'):
            updated = Diagnostics(self.folder, URL, app_version=version)
            self.assertFalse(updated.should_prompt())
            self.assertFalse(updated.enabled)
            self.assertFalse(updated.record('formats', 'pdf'))

    def test_outbound_only_independent_counters(self):
        self.enable()
        for metric, code, count in ((CANARY, 'pdf', 10), ('formats', CANARY, 10),
                                    ('formats', ValueError(CANARY), 10),
                                    ('formats', 'pdf', True), ('formats', 'pdf', MAX_COUNT+1),
                                    ('formats', 'pdf', '123'), ('formats', 'pdf', 0)):
            self.assertFalse(self.diag.record(metric, code, count))
        self.assertTrue(self.diag.record('formats', 'pdf', 10))
        self.assertTrue(self.diag.record('outcomes', 'quality_review', 3))
        self.assertTrue(self.diag.record('review_reasons', 'missing_unit', 2))
        self.assertTrue(self.diag.record('failure_stages', 'export_locked'))
        connection = Mock()
        connection.getresponse.return_value.status = 204
        with patch.dict(os.environ, {'HTTPS_PROXY': 'https://unexpected.invalid',
                                     'HTTP_PROXY': 'http://unexpected.invalid',
                                     'REQUESTS_CA_BUNDLE': CANARY}), \
             patch('patient_importer.telemetry.http.client.HTTPSConnection', return_value=connection) as connect:
            self.assertTrue(self.diag.send_if_due())
        args, kwargs = connect.call_args
        self.assertEqual(args, ('metrics.example.com',))
        self.assertGreater(kwargs['timeout'], 0)
        self.assertTrue(kwargs['context'].check_hostname)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST', '/v1/counters'))
        self.assertEqual(kwargs['headers'], {'Content-Type': 'application/json',
                                             'User-Agent': 'PatientStudyImporter-Diagnostics'})
        payload = json.loads(kwargs['body'])
        self.assertEqual(payload, {'schema_version': 1, 'app_version': VERSION, 'counters': {
            'formats': {'pdf': 10}, 'outcomes': {'quality_review': 3},
            'review_reasons': {'missing_unit': 2}, 'failure_stages': {'export_locked': 1},
        }})
        self.assertEqual(payload, validate_report(payload))
        self.assertNotIn(b'TEST', kwargs['body'])
        self.assertNotIn(CANARY, self.state.read_text(encoding='utf-8'))
        self.assertIsNone(self.diag.snapshot())
        connection.getresponse.return_value.read.assert_not_called()
        connection.close.assert_called_once()

    def test_validators_reject_injected_state_and_duplicate_keys(self):
        self.enable()
        self.diag.record('formats', 'pdf', 10)
        good = json.loads(self.state.read_text(encoding='utf-8'))
        variants = (
            {**good, 'path': CANARY},
            {**good, 'consent': 'true'},
            {**good, 'endpoint': f'{URL}?patient={CANARY}'},
            {**good, 'app_version': CANARY},
            {**good, 'counters': {'formats': {CANARY: 10}}},
            {**good, 'counters': {'formats': {'pdf': True}}},
            {**good, 'counters': {'formats': {'pdf': MAX_COUNT+1}}},
            {**good, 'counters': {'formats': {'pdf': [CANARY]}}},
            {**good, 'counters': {'exception': {'extract': 1}}},
        )
        bad = [json.dumps(value) for value in variants]
        bad.append(json.dumps(good).replace('"consent": true', '"consent": false, "consent": true'))
        with patch('patient_importer.telemetry.post_report') as post:
            for value in bad:
                with self.subTest(index=bad.index(value)):
                    self.state.write_text(value, encoding='utf-8')
                    self.assertFalse(self.diag.enabled)
                    self.assertFalse(self.diag.should_prompt())
                    self.assertFalse(self.diag.record('formats', 'pdf'))
                    self.assertIsNone(self.diag.snapshot())
                    self.assertFalse(self.diag.send_if_due())
            post.assert_not_called()
        self.assertTrue(self.diag.set_consent(False))
        self.assertNotIn('exception', self.state.read_text(encoding='utf-8'))
        self.assertFalse(self.diag.enabled)

    def test_version_changes_clear_counts_and_destination_changes_pause(self):
        self.enable()
        self.diag.record('formats', 'pdf', 11)
        new = Diagnostics(self.folder, URL, app_version='2.5.0')
        self.assertTrue(new.enabled)
        self.assertIsNone(new.snapshot())
        self.assertTrue(new.record('formats', 'docx', 11))
        old = Diagnostics(self.folder, URL, app_version='2.4.0')
        self.assertTrue(old.enabled)
        self.assertIsNone(old.snapshot())
        new_host = Diagnostics(self.folder, 'https://second.example.com/v1/counters', app_version=VERSION)
        self.assertFalse(new_host.enabled)
        self.assertFalse(new_host.should_prompt())
        self.assertIsNone(new_host.snapshot())
        with patch('patient_importer.telemetry.post_report') as post:
            self.assertFalse(new_host.send_if_due())
            post.assert_not_called()

    def test_scope_change_discards_counts_and_requires_new_consent(self):
        self.enable()
        self.diag.record('formats', 'pdf', 10)
        for key in ('schema_version', 'consent_version'):
            with self.subTest(key=key):
                state = json.loads(self.state.read_text(encoding='utf-8'))
                state[key] += 1
                self.state.write_text(json.dumps(state), encoding='utf-8')
                self.assertFalse(self.diag.enabled)
                self.assertFalse(self.diag.should_prompt())
                self.assertIsNone(self.diag.snapshot())
                self.assertEqual(json.loads(self.state.read_text(encoding='utf-8'))['counters'], {})
                self.enable()

    def test_minimum_attempts_timeout_retry_and_daily_throttle(self):
        self.enable()
        self.diag.record('outcomes', 'saved', 20)
        self.diag.record('formats', 'pdf', 9)
        with patch('patient_importer.telemetry.post_report', side_effect=[TimeoutError(CANARY), True]) as post:
            self.assertFalse(self.diag.send_if_due())
            post.assert_not_called()
            self.diag.record('formats', 'docx')
            now = 1_800_000_000
            with patch('patient_importer.telemetry.time.time', return_value=now):
                self.assertFalse(self.diag.send_if_due())
                self.assertEqual(self.diag.snapshot()['counters']['formats'], {'pdf': 9, 'docx': 1})
                self.assertFalse(self.diag.send_if_due())
            with patch('patient_importer.telemetry.time.time', return_value=now + SEND_INTERVAL - 1):
                self.assertFalse(self.diag.send_if_due())
                self.assertEqual(post.call_count, 1)
            with patch('patient_importer.telemetry.time.time', return_value=now + SEND_INTERVAL):
                self.assertTrue(self.diag.send_if_due())
                self.diag.record('formats', 'pdf', 10)
                self.assertFalse(self.diag.send_if_due())
                self.assertEqual(post.call_count, 2)
        self.assertNotIn(CANARY, self.state.read_text(encoding='utf-8'))

    def test_redirect_and_http_error_are_not_success_or_followed(self):
        self.enable()
        self.diag.record('formats', 'pdf', 10)
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 307
        response.getheader.return_value = f'http://example.com/{CANARY}'
        with patch('patient_importer.telemetry.http.client.HTTPSConnection', return_value=connection) as connect:
            self.assertFalse(self.diag.send_if_due())
            connect.assert_called_once()
            connection.request.assert_called_once()
            response.getheader.assert_not_called()
            response.read.assert_not_called()
        self.assertEqual(self.diag.snapshot()['counters'], {'formats': {'pdf': 10}})
        for status in (200, 400, 429, 500):
            response.status = status
            with patch('patient_importer.telemetry.http.client.HTTPSConnection', return_value=connection):
                self.assertFalse(post_report(URL, b'{}'))

    def test_concurrent_records_from_two_instances_are_retained(self):
        self.enable()
        second = Diagnostics(self.folder, URL, app_version=VERSION)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(diag.record, 'formats', 'pdf', 2) for diag in (self.diag, second) * 4]
            self.assertTrue(all(job.result() for job in jobs))
        self.assertEqual(self.diag.snapshot()['counters'], {'formats': {'pdf': 16}})
        self.assertTrue(second.record('formats', 'pdf', MAX_COUNT))
        self.assertEqual(self.diag.snapshot()['counters'], {'formats': {'pdf': MAX_COUNT}})

    def test_independent_processes_preserve_counter_updates(self):
        self.enable()
        script = (
            "import sys; from pathlib import Path; "
            "from patient_importer.telemetry import Diagnostics; "
            "assert Diagnostics(Path(sys.argv[1]), sys.argv[2], sys.argv[3])"
            ".record('formats', 'pdf', 10)"
        )
        command = [sys.executable, '-c', script, str(self.folder), URL, VERSION]
        with subprocess.Popen(command) as first, subprocess.Popen(command) as second:
            self.assertEqual(first.wait(timeout=10), 0)
            self.assertEqual(second.wait(timeout=10), 0)
        self.assertEqual(self.diag.snapshot()['counters'], {'formats': {'pdf': 20}})

    def test_withdrawal_clears_prior_data_even_when_endpoint_is_now_unset(self):
        self.enable()
        self.diag.record('formats', 'pdf', 10)
        inactive = Diagnostics(self.folder, '', app_version='2.5.0')
        self.assertFalse(inactive.configured)
        self.assertTrue(inactive.set_consent(False))
        self.assertEqual(json.loads(self.state.read_text(encoding='utf-8'))['counters'], {})
        self.assertFalse(self.diag.enabled)
        self.assertFalse(self.diag.should_prompt())

    def test_acknowledging_report_does_not_lose_new_counts(self):
        self.enable()
        self.diag.record('formats', 'pdf', 10)
        second = Diagnostics(self.folder, URL, app_version=VERSION)

        def transport(endpoint, body):
            self.assertFalse(second.send_if_due())
            self.assertTrue(second.record('formats', 'pdf', 2))
            return True

        with patch('patient_importer.telemetry.post_report', side_effect=transport):
            self.assertTrue(self.diag.send_if_due())
        self.assertEqual(self.diag.snapshot()['counters'], {'formats': {'pdf': 2}})

    def test_withdrawal_does_not_wait_for_running_send(self):
        self.enable()
        self.diag.record('formats', 'pdf', 10)
        started = threading.Event()
        finish = threading.Event()
        other = Diagnostics(self.folder, URL, app_version=VERSION)

        def transport(endpoint, body):
            started.set()
            self.assertTrue(finish.wait(3))
            return True

        with patch('patient_importer.telemetry.post_report', side_effect=transport) as post, ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.diag.send_if_due)
            try:
                self.assertTrue(started.wait(2))
                revoked = pool.submit(other.set_consent, False)
                self.assertTrue(revoked.result(timeout=1))
                self.assertFalse(running.done())
                self.assertFalse(other.enabled)
                self.assertIsNone(other.snapshot())
                self.assertFalse(self.diag.record('formats', 'pdf', 10))
                self.assertFalse(self.diag.send_if_due())
            finally:
                finish.set()
            self.assertTrue(running.result(timeout=4))
            self.assertFalse(other.enabled)
            self.assertIsNone(other.snapshot())
            self.assertFalse(self.diag.send_if_due())
            self.assertFalse(other.should_prompt())
            post.assert_called_once()

    def test_old_acknowledgement_does_not_subtract_after_new_consent(self):
        self.enable()
        self.diag.record('formats', 'pdf', 10)
        other = Diagnostics(self.folder, URL, app_version=VERSION)

        def transport(endpoint, body):
            self.assertTrue(other.set_consent(False))
            self.assertTrue(other.set_consent(True))
            self.assertTrue(other.record('formats', 'pdf', 12))
            return True

        with patch('patient_importer.telemetry.post_report', side_effect=transport):
            self.assertTrue(self.diag.send_if_due())
        self.assertEqual(self.diag.snapshot()['counters'], {'formats': {'pdf': 12}})


DEPLOYMENT = 'AKfycbx_Synthetic-Deployment-test'
GOOGLE_PATH = f'/macros/s/{DEPLOYMENT}/exec/v1/counters'
GOOGLE_URL = f'https://script.google.com{GOOGLE_PATH}'
ACK_TARGET = '/macros/echo?user_content_key=opaque_key-123%3D&lib=opaque_lib-456'
ACK_URL = 'https://script.googleusercontent.com' + ACK_TARGET


class AppsScriptTransportTests(unittest.TestCase):
    @staticmethod
    def response(status, *, headers=None, data=b''):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = status
        response.getheader.side_effect = (headers or {}).get
        response.read.return_value = data
        return connection

    def client(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Diagnostics(Path(temp.name), GOOGLE_URL, app_version=VERSION)

    def test_only_canonical_google_web_app_endpoint(self):
        self.assertEqual(endpoint_url(GOOGLE_URL), GOOGLE_URL)
        self.assertEqual(endpoint_url(GOOGLE_URL.replace('.com/', '.com:443/')), GOOGLE_URL)
        for value in (
            GOOGLE_URL + '?x=y', GOOGLE_URL + '#', GOOGLE_URL + '?', GOOGLE_URL + '/',
            GOOGLE_URL.replace('https:', 'http:'), GOOGLE_URL.replace('/exec/', '/dev/'),
            GOOGLE_URL.replace(DEPLOYMENT, 'x'), GOOGLE_URL.replace(DEPLOYMENT, 'a'*257),
            GOOGLE_URL.replace(DEPLOYMENT, 'a'*19 + '%2f'),
            GOOGLE_URL.replace('.com/', '.com:444/'),
            GOOGLE_URL.replace('script.google.com', 'script.google.com.example.org'),
            GOOGLE_URL.replace('script.google.com', 'test@script.google.com'),
            'https://script.google.com/v1/counters',
        ):
            self.assertEqual(endpoint_url(value), '')

    def test_no_google_connection_without_consent(self):
        client = self.client()
        self.assertTrue(client.configured)
        with patch('patient_importer.telemetry.http.client.HTTPSConnection') as connect:
            self.assertFalse(client.record('formats', 'pdf', 10))
            self.assertFalse(client.send_if_due())
            connect.assert_not_called()

    def test_google_redirect_fetches_only_bodyless_ack(self):
        client = self.client()
        self.assertTrue(client.set_consent(True))
        self.assertTrue(client.record('formats', 'pdf', 10))
        self.assertFalse(client.record('formats', CANARY))
        post = self.response(302, headers={'Location': ACK_URL, 'Set-Cookie': 'not-forwarded=1'})
        ack = self.response(200, headers={'Content-Type': 'application/json; charset=UTF-8'}, data=b'{"ok":true}')
        with patch('patient_importer.telemetry.http.client.HTTPSConnection', side_effect=[post, ack]) as connect:
            self.assertTrue(client.send_if_due())
        self.assertIsNone(client.snapshot())
        self.assertEqual([call.args[0] for call in connect.call_args_list],
                         ['script.google.com', 'script.googleusercontent.com'])
        args, kwargs = post.request.call_args
        self.assertEqual(args, ('POST', GOOGLE_PATH))
        self.assertEqual(kwargs['body'], b'{"app_version":"2.4.0","counters":{"formats":{"pdf":10}},"schema_version":1}')
        self.assertEqual(kwargs['headers'], {'Content-Type': 'application/json',
                                             'User-Agent': 'PatientStudyImporter-Diagnostics'})
        post.getresponse.return_value.read.assert_not_called()
        ack.request.assert_called_once_with('GET', ACK_TARGET)
        ack.getresponse.return_value.read.assert_called_once_with(MAX_ACK_BYTES + 1)
        post.close.assert_called_once()
        ack.close.assert_called_once()

    def test_reject_malicious_redirects_and_post_replay(self):
        for value in (
            ACK_URL.replace('https:', 'http:'), ACK_URL.replace('/echo?', '/elsewhere?'),
            ACK_URL.replace('.com/', '.com:444/'), ACK_URL.replace('.com/', '.com.evil.invalid/'),
            ACK_URL.replace('script.googleusercontent.com', 'user@script.googleusercontent.com'),
            ACK_URL + '&path=' + CANARY, ACK_URL + '&lib=duplicate', ACK_URL + '#',
            ACK_URL.replace('%3D', '%0A'), ACK_URL.replace('%3D', '%26x%3D'),
            ACK_URL.replace('%3D', '%xx'), ACK_URL.replace('&lib=opaque_lib-456', ''),
            ACK_URL.replace('lib=', '%6Cib='), ACK_URL.replace('opaque_key-123%3D', ''),
            ACK_URL + 'x'*2100, 'https://accounts.google.com/signin', ACK_URL + '\r\n',
        ):
            self.assertEqual(apps_ack_target(value), '', value)
            post = self.response(302, headers={'Location': value})
            with patch('patient_importer.telemetry.http.client.HTTPSConnection', return_value=post) as connect:
                self.assertFalse(post_report(GOOGLE_URL, b'{}'))
                connect.assert_called_once()
                post.request.assert_called_once()
        for status in (200, 204, 301, 307, 308):
            post = self.response(status, headers={'Location': ACK_URL})
            with patch('patient_importer.telemetry.http.client.HTTPSConnection', return_value=post) as connect:
                self.assertFalse(post_report(GOOGLE_URL, b'{}'))
                connect.assert_called_once()

    def test_invalid_or_missing_ack_keeps_queue_without_another_redirect(self):
        for status, content_type, data in (
            (200, 'text/html', b'<html>Sign in</html>'),
            (200, 'application/json', b'{"ok":false}'),
            (200, 'application/json', b'{"ok":1}'),
            (200, 'application/json', b'{"ok":true,"details":"error"}'),
            (200, 'application/json', b'{"ok":false,"ok":true}'),
            (200, 'application/json', b'{"ok":true}' + b' '*MAX_ACK_BYTES),
            (200, 'application/json', b'\xff'),
            (200, 'application/json', b'{"ok":'),
            (200, '', b'{"ok":true}'),
            (302, 'application/json', b'{"ok":true}'),
        ):
            with self.subTest(status=status, content_type=content_type, data=data[:16]):
                client = self.client()
                self.assertTrue(client.set_consent(True))
                self.assertTrue(client.record('formats', 'pdf', 10))
                post = self.response(303, headers={'Location': ACK_URL})
                ack = self.response(status, headers={'Content-Type': content_type, 'Location': ACK_URL}, data=data)
                with patch('patient_importer.telemetry.http.client.HTTPSConnection', side_effect=[post, ack]) as connect:
                    self.assertFalse(client.send_if_due())
                self.assertEqual(connect.call_count, 2)
                self.assertEqual(client.snapshot()['counters'], {'formats': {'pdf': 10}})
                self.assertFalse(client.send_if_due())

    def test_ack_timeout_keeps_queue(self):
        client = self.client()
        self.assertTrue(client.set_consent(True))
        self.assertTrue(client.record('formats', 'pdf', 10))
        post = self.response(303, headers={'Location': ACK_URL})
        ack = self.response(200, headers={'Content-Type': 'application/json'})
        ack.getresponse.return_value.read.side_effect = TimeoutError()
        with patch('patient_importer.telemetry.http.client.HTTPSConnection', side_effect=[post, ack]):
            self.assertFalse(client.send_if_due())
        self.assertEqual(client.snapshot()['counters'], {'formats': {'pdf': 10}})
        ack.close.assert_called_once()


class ReportSchemaTests(unittest.TestCase):
    def test_strict_enum_range_and_structure(self):
        good = {'schema_version': 1, 'app_version': VERSION, 'counters': {'formats': {'pdf': 10}}}
        bad = (
            {**good, 'install_id': CANARY}, {**good, 'schema_version': True},
            {**good, 'schema_version': 2}, {**good, 'app_version': '2.4.0-test'},
            {**good, 'app_version': '02.4.0'}, {**good, 'app_version': '２.4.0'},
            {**good, 'counters': {}}, {**good, 'counters': {'formats': {}}},
            {**good, 'counters': {'formats': {'pdf': 0}}},
            {**good, 'counters': {'formats': {'pdf': 1.0}}},
            {**good, 'counters': {'formats': {'pdf': False}}},
            {**good, 'counters': {'formats': {'pdf': MAX_COUNT + 1}}},
            {**good, 'counters': {'formats': {CANARY: 10}}},
        )
        for index, payload in enumerate(bad):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    validate_report(payload)
        clean = validate_report(good)
        good['counters']['formats']['pdf'] = CANARY
        self.assertEqual(clean['counters']['formats']['pdf'], 10)


if __name__ == '__main__':
    unittest.main()
