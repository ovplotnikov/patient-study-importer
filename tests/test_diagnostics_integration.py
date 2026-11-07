"""Verify that registry diagnostics expose fixed categories, never source data."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace
from patient_importer.telemetry import Diagnostics
from patient_importer.telemetry_schema import MAX_COUNT, METRICS


class Capture:
    def __init__(self):
        self.calls = []
        self.enabled = True

    def consent_epoch(self):
        return 1 if self.enabled else None

    def record_batch(self, counters, *, epoch):
        if epoch != self.consent_epoch():
            return False
        self.calls.append(counters)
        return True

    @property
    def counts(self):
        totals = Counter()
        for report in self.calls:
            for metric, bucket in report.items():
                for code, count in bucket.items():
                    totals[metric, code] += count
        return totals


class DiagnosticsIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.capture = Capture()
        self.workspace = Workspace(Path(directory.name) / 'registry', diagnostics=self.capture)

    def add(self, name, content):
        path = self.workspace.root/'Входящие'/name
        path.write_text(content, encoding='utf-8')
        return path

    def assert_fixed_diagnostics(self, *canaries):
        for report in self.capture.calls:
            for metric, bucket in report.items():
                for code, count in bucket.items():
                    self.assertIs(type(metric), str)
                    self.assertIs(type(code), str)
                    self.assertIn(code, METRICS[metric])
                    self.assertIs(type(count), int)
                    self.assertTrue(1 <= count <= MAX_COUNT)
        payload = json.dumps(self.capture.calls, ensure_ascii=False)
        for canary in canaries:
            self.assertNotIn(canary, payload)

    def live_diagnostics(self):
        diagnostics = Diagnostics(self.workspace.root.parent/'diagnostics',
                                  endpoint='https://metrics.example.com/v1/counters')
        self.assertTrue(diagnostics.set_consent(True))
        self.workspace.diagnostics = diagnostics
        return diagnostics

    def test_import_attempts_include_success_duplicate_and_failure_without_input_data(self):
        path_canary = 'PRIVATE_FILENAME_78591'
        exception_canary = 'PRIVATE_EXCEPTION_48734'
        self.add('01-'+path_canary+'.txt', SAMPLE)
        self.add('02-copy.txt', SAMPLE)
        self.add('03-bad.pdf', '%PDF-synthetic')
        from patient_importer.extraction import extract

        def read(path):
            if path.suffix == '.pdf':
                raise ValueError(exception_canary)
            return extract(path)

        with patch('patient_importer.extraction.extract', side_effect=read):
            self.workspace.import_folder()
        counts = self.capture.counts
        self.assertEqual(counts['formats', 'txt'], 2)
        self.assertEqual(counts['formats', 'pdf'], 1)
        self.assertEqual(counts['outcomes', 'saved'], 1)
        self.assertEqual(counts['outcomes', 'duplicate'], 1)
        self.assertEqual(counts['outcomes', 'error'], 1)
        self.assertEqual(counts['failure_stages', 'extraction'], 1)
        self.assert_fixed_diagnostics(path_canary, exception_canary, 'ТЕСТОВИЧ', '71.5')

    def test_review_never_reports_text_fields_even_as_unrecognized_reason_codes(self):
        private = ['OCR_BODY_12009', 'REASON_12903', 'CODE_38219', 'MEDICAL_LABEL_98102',
                   '598412.67', 'UNIT_77234', 'REFERENCE_82673', 'NOTE_60913', 'CONCLUSION_70181']
        document = SAMPLE + '\n' + private[0]
        extracted = {'pages': [document], 'native_pages': [document], 'ocr_pages': [1], 'format': 'PDF',
                     'extraction_issues': [
                         {'code': 'OCR_USED', 'message': private[1], 'page': 1},
                         {'code': 'OCR_USED', 'message': private[1], 'page': 2},
                         {'code': private[2], 'message': private[0]},
                     ]}
        self.add('скан.pdf', '%PDF-synthetic')
        with patch('patient_importer.extraction.extract', return_value=extracted):
            self.workspace.import_folder()
        record = self.workspace.quality_review_list()[0]
        record['observations'][0].update(label=private[3], result=private[4], unit=private[5], reference=private[6])
        sections = record['analysis']['sections'] + [{'section': 'Заключение', 'text': private[8], 'page': 1}]
        self.workspace.confirm_quality(record['id'], observations=record['observations'], sections=sections,
                                       note=private[7], expected_revision=record['revision'])
        counts = self.capture.counts
        self.assertEqual(counts['review_reasons', 'OCR_USED'], 1)
        self.assertEqual(counts['outcomes', 'quality_review'], 1)
        self.assertEqual(counts['review_outcomes', 'confirmed_incomplete'], 1)
        for category in ('result', 'unit', 'reference', 'observation', 'conclusion'):
            self.assertEqual(counts['edit_categories', category], 1)
        self.assert_fixed_diagnostics(*private, record['id'])

    def test_export_failures_are_never_counted_as_completed_imports(self):
        self.add('study.txt', SAMPLE)
        lock = self.workspace.root/'~$Исследования.xlsx'
        lock.touch()
        with self.assertRaises(RuntimeError):
            self.workspace.import_folder()
        lock.unlink()
        with patch('patient_importer.workbook.export_workbook', side_effect=OSError('PRIVATE_EXPORT_90818')):
            with self.assertRaises(OSError):
                self.workspace.import_folder()
        counts = self.capture.counts
        self.assertEqual(counts['formats', 'txt'], 2)
        self.assertEqual(counts['outcomes', 'error'], 2)
        self.assertEqual(counts['failure_stages', 'export_locked'], 1)
        self.assertEqual(counts['failure_stages', 'export'], 1)
        self.assertNotIn(('outcomes', 'saved'), counts)
        self.assert_fixed_diagnostics('PRIVATE_EXPORT_90818')

    def test_archive_failure_has_an_error_outcome_and_retains_source(self):
        source = self.add('study.txt', SAMPLE)
        destination = self.workspace.root/'Обработано'/'occupied.txt'
        destination.write_bytes(b'different synthetic file')
        with patch.object(self.workspace, 'destination', return_value=destination):
            self.workspace.import_folder()
        self.assertTrue(source.is_file())
        counts = self.capture.counts
        self.assertEqual(counts['failure_stages', 'archive'], 1)
        self.assertEqual(counts['outcomes', 'error'], 1)
        self.assertNotIn(('outcomes', 'saved'), counts)
        self.assert_fixed_diagnostics('occupied.txt')

    def test_diagnostic_exceptions_do_not_break_import_or_manual_review(self):
        self.add('study.txt', SAMPLE.replace('Дата выполнения исследования: 02.03.2025\n', ''))
        with patch.object(self.capture, 'record_batch', side_effect=OSError('PRIVATE_SINK_ERROR')) as record:
            result = self.workspace.import_folder()
            self.assertEqual(result['quality_review'], 1)
            self.assertEqual(result['pending_moves'], 0)
            document_id = result['batch'][0]['document_id']
            result = self.workspace.confirm_quality(document_id, date='02.03.2025', note='Дата сверена.')
            self.assertEqual(result['quality_review'], 0)
            self.assertFalse(result['pending_export'])
            self.assertTrue((self.workspace.root/'Обработано'/'study.txt').is_file())
            self.assertGreater(record.call_count, 0)

    def test_existing_registry_is_not_replayed_when_diagnostics_are_enabled(self):
        self.workspace.diagnostics = None
        self.add('study.txt', SAMPLE)
        self.workspace.import_folder()
        reopened = Workspace(self.workspace.root, diagnostics=self.capture)
        reopened.summary()
        reopened.quality_review_list()
        reopened.import_folder()
        self.assertEqual(self.capture.calls, [])

    def test_enabling_during_import_does_not_count_a_preconsent_attempt(self):
        self.capture.enabled = False
        self.add('study.txt', SAMPLE)

        def opt_in(**event):
            if event.get('phase') == 'Сохранение таблицы':
                self.capture.enabled = True

        self.workspace.import_folder(progress=opt_in)
        self.assertEqual(self.capture.calls, [])

    def test_large_import_persists_one_diagnostics_update(self):
        diagnostics = self.live_diagnostics()
        for index in range(1000):
            self.add(f'{index:04d}.txt', SAMPLE)
        with patch.object(diagnostics, '_write', wraps=diagnostics._write) as write:
            result = self.workspace.import_folder()
            write.assert_called_once()
        self.assertEqual(len(result['batch']), 1000)
        self.assertEqual(diagnostics.snapshot()['counters'], {
            'formats': {'txt': 1000}, 'outcomes': {'saved': 1, 'duplicate': 999},
        })

    def test_reconsent_drops_old_attempt_errors_and_review_reasons(self):
        from patient_importer.extraction import extract

        diagnostics = self.live_diagnostics()
        review = self.add('01-review.pdf', '%PDF-synthetic review')
        self.add('02-bad.pdf', '%PDF-synthetic error')
        self.add('03-new.txt', SAMPLE.replace('71.5', '77.1'))
        occupied = self.workspace.root/'Нужно уточнить'/'occupied.pdf'
        occupied.write_bytes(b'different synthetic PDF')
        destination = self.workspace.destination

        def read(path):
            if path.suffix == '.txt':
                return extract(path)
            if path.read_bytes() == b'%PDF-synthetic review':
                return {'pages': [SAMPLE], 'native_pages': [SAMPLE], 'ocr_pages': [1], 'format': 'PDF',
                        'extraction_issues': [{'code': 'OCR_USED', 'page': 1}]}
            self.assertTrue(diagnostics.set_consent(False))
            self.assertTrue(diagnostics.set_consent(True))
            raise ValueError('Synthetic extraction failure')

        def planned_destination(row):
            return occupied if row['source'] == str(review) else destination(row)

        with patch('patient_importer.extraction.extract', side_effect=read), \
             patch.object(self.workspace, 'destination', side_effect=planned_destination):
            result = self.workspace.import_folder()
        self.assertEqual(result['quality_review'], 1)
        self.assertEqual(result['errors'], 1)
        self.assertTrue(review.is_file())
        self.assertEqual(diagnostics.snapshot()['counters'], {
            'formats': {'txt': 1}, 'outcomes': {'saved': 1},
        })

    def test_opt_out_before_flush_discards_buffered_attempts(self):
        diagnostics = self.live_diagnostics()
        self.add('study.txt', SAMPLE)

        def opt_out(**event):
            if event.get('phase') == 'Готово':
                self.assertTrue(diagnostics.set_consent(False))

        with patch.object(diagnostics, '_write', wraps=diagnostics._write) as write:
            self.workspace.import_folder(progress=opt_out)
            write.assert_called_once()  # Consent withdrawal is the only durable change.
        self.assertIsNone(diagnostics.snapshot())
        self.assertIsNone(diagnostics.consent_epoch())

    def test_flush_counts_failed_and_completed_attempts_when_cancelled(self):
        from patient_importer.extraction import extract

        diagnostics = self.live_diagnostics()
        self.add('01-study.txt', SAMPLE)
        self.add('02-bad.pdf', '%PDF-synthetic error')
        untouched = self.add('03-unstarted.txt', SAMPLE)
        cancel = threading.Event()

        def read(path):
            if path.suffix == '.pdf':
                raise ValueError('Synthetic extraction failure')
            return extract(path)

        def stop(**event):
            if event.get('done') == 2 and 'current' not in event:
                cancel.set()

        with patch('patient_importer.extraction.extract', side_effect=read):
            result = self.workspace.import_folder(progress=stop, cancel=cancel)
        self.assertEqual(result['remaining'], 1)
        self.assertTrue(untouched.is_file())
        self.assertEqual(diagnostics.snapshot()['counters'], {
            'formats': {'txt': 1, 'pdf': 1},
            'outcomes': {'saved': 1, 'error': 1},
            'failure_stages': {'extraction': 1},
        })

    def test_concurrent_event_is_not_buffered_in_another_threads_batch(self):
        diagnostics = self.live_diagnostics()
        with self.workspace._diagnostic_batch(), ThreadPoolExecutor(max_workers=1) as pool:
            self.workspace._diagnostic('formats', 'txt')
            pool.submit(self.workspace._diagnostic, 'failure_stages', 'intake').result(timeout=3)
            self.assertEqual(diagnostics.snapshot()['counters'], {'failure_stages': {'intake': 1}})
        self.assertEqual(diagnostics.snapshot()['counters'], {
            'failure_stages': {'intake': 1}, 'formats': {'txt': 1},
        })


if __name__ == '__main__':
    unittest.main()
