"""Verify that registry diagnostics expose fixed categories, never source data."""
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace
from patient_importer.telemetry_schema import METRICS


class Capture:
    def __init__(self):
        self.calls = []

    def record(self, metric, code, count=1):
        self.calls.append((metric, code, count))


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
        for metric, code, count in self.capture.calls:
            self.assertIs(type(metric), str)
            self.assertIs(type(code), str)
            self.assertIn(code, METRICS[metric])
            self.assertEqual(count, 1)
        payload = json.dumps(self.capture.calls, ensure_ascii=False)
        for canary in canaries:
            self.assertNotIn(canary, payload)

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
        counts = Counter(self.capture.calls)
        self.assertEqual(counts['formats', 'txt', 1], 2)
        self.assertEqual(counts['formats', 'pdf', 1], 1)
        self.assertEqual(counts['outcomes', 'saved', 1], 1)
        self.assertEqual(counts['outcomes', 'duplicate', 1], 1)
        self.assertEqual(counts['outcomes', 'error', 1], 1)
        self.assertEqual(counts['failure_stages', 'extraction', 1], 1)
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
        counts = Counter(self.capture.calls)
        self.assertEqual(counts['review_reasons', 'OCR_USED', 1], 1)
        self.assertEqual(counts['outcomes', 'quality_review', 1], 1)
        self.assertEqual(counts['review_outcomes', 'confirmed_incomplete', 1], 1)
        for category in ('result', 'unit', 'reference', 'observation', 'conclusion'):
            self.assertEqual(counts['edit_categories', category, 1], 1)
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
        counts = Counter(self.capture.calls)
        self.assertEqual(counts['formats', 'txt', 1], 2)
        self.assertEqual(counts['outcomes', 'error', 1], 2)
        self.assertEqual(counts['failure_stages', 'export_locked', 1], 1)
        self.assertEqual(counts['failure_stages', 'export', 1], 1)
        self.assertNotIn(('outcomes', 'saved', 1), counts)
        self.assert_fixed_diagnostics('PRIVATE_EXPORT_90818')

    def test_archive_failure_has_an_error_outcome_and_retains_source(self):
        source = self.add('study.txt', SAMPLE)
        destination = self.workspace.root/'Обработано'/'occupied.txt'
        destination.write_bytes(b'different synthetic file')
        with patch.object(self.workspace, 'destination', return_value=destination):
            self.workspace.import_folder()
        self.assertTrue(source.is_file())
        self.assertIn(('failure_stages', 'archive', 1), self.capture.calls)
        self.assertIn(('outcomes', 'error', 1), self.capture.calls)
        self.assertNotIn(('outcomes', 'saved', 1), self.capture.calls)
        self.assert_fixed_diagnostics('occupied.txt')

    def test_diagnostic_exceptions_do_not_break_import_or_manual_review(self):
        self.add('study.txt', SAMPLE.replace('Дата выполнения исследования: 02.03.2025\n', ''))
        with patch.object(self.capture, 'record', side_effect=OSError('PRIVATE_SINK_ERROR')) as record:
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


if __name__ == '__main__':
    unittest.main()
