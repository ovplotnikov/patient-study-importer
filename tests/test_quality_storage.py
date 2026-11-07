"""Synthetic checks for review, duplicate decisions and reprocessing."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace, sha256


class QualityStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Workspace(Path(temp.name) / 'registry')
        self.workbook = self.workspace.root / 'Исследования.xlsx'

    def add(self, name, text):
        path = self.workspace.root / 'Входящие' / name
        path.write_text(text, encoding='utf-8')
        return path

    def parsed(self, document_id):
        return json.loads(self.workspace.document(document_id)['analysis'])

    def test_confirming_patient_leaves_draft_until_separate_review(self):
        self.add('вымышленный.txt', SAMPLE.replace('Дата рождения: 01.01.2000\n', '').replace('71.5   мкмоль/л   (40 - 90)', '71.5'))
        result = self.workspace.import_folder()
        self.assertEqual((result['review'], result['quality_review'], result['total_review']), (1, 1, 1))
        document_id = self.workspace.review_list()[0]['id']
        result = self.workspace.assign(document_id, 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01')
        self.assertEqual((result['review'], result['quality_review'], result['total_review']), (0, 1, 1))
        self.assertTrue((self.workspace.root/'Нужно уточнить'/'вымышленный.txt').exists())
        book = load_workbook(self.workbook)
        self.assertEqual(book['Исследования']['F2'].value, 'Проверить данные')
        self.assertEqual(book['Показатели']['P2'].value, 'Черновик · сверить')
        review = self.workspace.quality_review_list()[0]
        self.assertTrue(any(issue['code']=='missing_unit' for issue in review['issues']))
        rows = review['observations']
        rows[0]['result'] = '< 71,5'
        rows[0]['unit'] = 'мкмоль/л'
        result = self.workspace.confirm_quality(document_id, observations=rows,
                                               note='Единицы и знак сверены с учебным оригиналом.',
                                               expected_revision=review['revision'])
        self.assertEqual(result['total_review'], 0)
        self.assertTrue((self.workspace.root/'Обработано'/'вымышленный.txt').exists())
        parsed = self.parsed(document_id)
        self.assertEqual(parsed['observations'][0]['result'], '< 71,5')
        self.assertIsNone(parsed['observations'][0]['number'])
        self.assertEqual(parsed['observations'][0]['result_id'], rows[0]['result_id'])
        with self.workspace.connect() as db:
            previous = json.loads(db.execute('SELECT analysis FROM study_history').fetchone()[0])
        self.assertEqual(previous['observations'][0]['result'], '71.5')
        self.assertEqual(load_workbook(self.workbook)['Показатели']['P2'].value, 'Проверено')

    def test_missing_fields_need_explicit_note_and_remain_marked(self):
        self.add('неполный.txt', 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n')
        result = self.workspace.import_folder()
        document_id = result['batch'][0]['document_id']
        self.assertEqual(result['batch'][0]['status'], 'Проверить данные')
        with self.assertRaisesRegex(ValueError, 'замечания'):
            self.workspace.confirm_quality(document_id, note='Сверены все страницы и строки оригинала')
        self.workspace.confirm_quality(document_id, note='В учебном оригинале отсутствуют дата и результаты.')
        self.assertEqual(load_workbook(self.workbook)['Исследования']['F2'].value, 'Сверено · неполные данные')
        self.assertEqual(self.workspace.summary()['quality_review'], 0)
        self.assertEqual(self.workspace.summary()['incomplete'], 1)
        self.assertIn('отсутствуют дата и результаты', load_workbook(self.workbook)['Проверка']['E2'].value)

    def test_same_ocr_text_keeps_both_sources_until_explicit_duplicate_decision(self):
        extracted = {'pages': [SAMPLE], 'native_pages': [SAMPLE], 'ocr_pages': [1], 'format': 'PDF'}
        with patch('patient_importer.extraction.extract', return_value=extracted):
            self.add('первый.pdf', '%PDF-first synthetic')
            first_id = self.workspace.import_folder()['batch'][0]['document_id']
            self.add('второй.pdf', '%PDF-second synthetic')
            result = self.workspace.import_folder()
            second_id = result['batch'][0]['document_id']
            self.assertEqual((result['documents'], result['duplicates']), (2, 0))
            review = next(row for row in self.workspace.quality_review_list() if row['id']==second_id)
            issue = next(issue for issue in review['issues'] if issue['code']=='possible_duplicate')
            self.assertEqual(issue['related_ids'], [first_id])
            self.add('побайтовая-копия.pdf', '%PDF-second synthetic')
            result = self.workspace.import_folder()
        self.assertEqual((result['documents'], result['duplicates']), (2, 1))
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.workspace.mark_duplicate(second_id, first_id, 'Сверены обе страницы.', review['revision'])
        self.workspace.mark_duplicate(second_id, first_id, 'Сверены обе страницы.')
        self.assertTrue(Path(self.workspace.document(second_id)['original']).is_file())
        self.assertEqual(self.workspace.summary()['documents'], 1)
        self.assertEqual(self.parsed(second_id)['duplicate_of'], first_id)
        book = load_workbook(self.workbook)
        self.assertIn('Подтверждённый дубль', [row[5] for row in book['Исследования'].values])
        self.assertEqual(book['Показатели'].max_row, 3)

    def test_legacy_text_alias_is_reconsidered_when_original_is_added_again(self):
        extracted = {'pages': [SAMPLE], 'native_pages': [SAMPLE], 'ocr_pages': [1], 'format': 'PDF'}
        with patch('patient_importer.extraction.extract', return_value=extracted):
            self.add('прежний.pdf', '%PDF-first synthetic')
            first_id = self.workspace.import_folder()['batch'][0]['document_id']
            path = self.add('ошибочный-дубль.pdf', '%PDF-other synthetic')
            digest = sha256(path)
            self.workspace.preserve(path, digest)
            prior_copy = self.workspace.root/'Дубли'/path.name
            prior_copy.write_bytes(path.read_bytes())
            with self.workspace.connect() as db:
                db.execute('''INSERT INTO arrivals(source,hash,document_id,category,destination,status,created)
                              VALUES (?,?,?,?,?,?,?)''',
                           (str(path), digest, first_id, 'Дубли', str(prior_copy), 'done', '2025-03-02'))
            result = self.workspace.import_folder()
        self.assertEqual(result['documents'], 2)
        self.assertNotEqual(result['batch'][0]['document_id'], first_id)
        self.assertTrue(prior_copy.is_file())

    def test_clean_ocr_accepts_explicit_confirmation_without_a_custom_note(self):
        extracted = {'pages': [SAMPLE], 'native_pages': [SAMPLE], 'ocr_pages': [1], 'format': 'PDF',
                     'extraction_issues': [{'code': 'OCR_USED', 'message': 'Учебный скан.', 'page': 1}]}
        with patch('patient_importer.extraction.extract', return_value=extracted):
            self.add('читаемый-скан.pdf', '%PDF-clean synthetic')
            result = self.workspace.import_folder()
        self.assertEqual(result['quality_review'], 1)
        result = self.workspace.confirm_quality(result['batch'][0]['document_id'],
                                               note='Сверены все страницы и строки оригинала')
        self.assertEqual(result['quality_review'], 0)
        self.assertEqual(load_workbook(self.workbook)['Исследования']['F2'].value, 'Проверено')

    def test_legacy_registry_exposes_quality_without_rewriting_data_at_open(self):
        self.add('старый.txt', 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n')
        document_id = self.workspace.import_folder()['batch'][0]['document_id']
        previous = self.parsed(document_id)
        previous.pop('quality')
        with self.workspace.connect() as db:
            db.execute('UPDATE documents SET analysis=? WHERE id=?', (json.dumps(previous), document_id))
            db.execute('DROP TABLE study_history')
        xlsx_hash = sha256(self.workbook)
        reopened = Workspace(self.workspace.root)
        self.assertEqual(reopened.summary()['quality_review'], 1)
        self.assertEqual(sha256(self.workbook), xlsx_hash)
        self.assertNotIn('quality', json.loads(reopened.document(document_id)['analysis']))

    def test_reprocess_preserves_unique_result_id_and_archives_unmatched_note(self):
        self.add('анализ.txt', SAMPLE)
        document_id = self.workspace.import_folder()['batch'][0]['document_id']
        book = load_workbook(self.workbook)
        values = book['Показатели']
        original_id = values['A2'].value
        notes_column = next(cell.column for cell in values[1] if cell.value == 'Заметки')
        values.cell(2,notes_column,'заметка первого показателя')
        values.cell(3,notes_column,'заметка удалённой строки')
        book.save(self.workbook)
        new_text = SAMPLE.replace('71.5', '72.5').replace('Лейкоциты                              0    10^9/л\n', '')
        extracted = {'pages':[new_text], 'ocr_pages':[], 'format':'TXT'}
        with patch('patient_importer.extraction.extract', return_value=extracted):
            candidate = self.workspace.prepare_reprocess(document_id)
        self.assertEqual(self.parsed(document_id)['observations'][0]['result'], '71.5')
        self.workspace.apply_reprocess(document_id, candidate)
        book = load_workbook(self.workbook)
        self.assertEqual(book['Показатели']['A2'].value, original_id)
        self.assertEqual(book['Показатели']['I2'].value, '72.5')
        self.assertEqual(book['Показатели'].cell(2,notes_column).value, 'заметка первого показателя')
        self.assertIn('заметка удалённой строки', list(book['Архив показателей'].values)[1])
        self.assertEqual(self.workspace.document_list()[0]['id'], document_id)
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.workspace.apply_reprocess(document_id, candidate)

    def test_reprocess_protects_confirmed_fields(self):
        self.add('анализ.txt', SAMPLE.replace('Дата выполнения исследования: 02.03.2025\n', ''))
        document_id = self.workspace.import_folder()['batch'][0]['document_id']
        review = self.workspace.quality_review_list()[0]
        review['observations'][0]['comment'] = 'Ручная сверка единицы'
        self.workspace.confirm_quality(document_id, date='02.03.2025', observations=review['observations'],
                                       note='Дата сверена с оригиналом.')
        self.assertEqual(self.parsed(document_id)['observations'][0]['date'], '2025-03-02')
        self.assertEqual(load_workbook(self.workbook)['Показатели']['T2'].value, 'Ручная сверка единицы')
        extracted = {'pages':[SAMPLE.replace('71.5', '75.1')], 'ocr_pages':[], 'format':'TXT'}
        with patch('patient_importer.extraction.extract', return_value=extracted):
            candidate = self.workspace.prepare_reprocess(document_id)
        self.assertEqual(candidate['proposed_analysis']['observations'][0]['result'], '75.1')
        self.assertEqual(candidate['analysis']['observations'][0]['result'], '71.5')
        self.assertTrue(candidate['protected_fields'])
        self.workspace.apply_reprocess(document_id, candidate)
        self.assertEqual(self.parsed(document_id)['observations'][0]['result'], '71.5')
        self.assertEqual(self.workspace.summary()['quality_review'], 1)
        with patch('patient_importer.extraction.extract', return_value=extracted):
            another = self.workspace.prepare_reprocess(document_id)
        self.workspace.apply_reprocess(document_id, another)
        self.assertEqual(self.parsed(document_id)['observations'][0]['result'], '71.5')

    def test_export_failure_keeps_decision_and_marks_export_pending(self):
        self.add('проверка.txt', 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n')
        document_id = self.workspace.import_folder()['batch'][0]['document_id']
        with patch('patient_importer.workbook.export_workbook', side_effect=OSError('учебный отказ')):
            with self.assertRaisesRegex(RuntimeError, 'Решение сохранено'):
                self.workspace.confirm_quality(document_id, note='Сохранить неполный учебный оригинал.')
        self.assertTrue(self.workspace.summary()['pending_export'])
        self.assertEqual(self.workspace.summary()['quality_review'], 0)
        result = self.workspace.import_folder()
        self.assertFalse(result['pending_export'])
        self.assertEqual(result['pending_moves'], 0)

    def test_manual_conclusion_and_journal_survive_reprocessing(self):
        source = ('УЗИ почек\nПациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n'
                  'Дата выполнения исследования: 02.03.2025\nЗаключение: Учебный черновик.')
        extracted = {'pages': [source], 'native_pages': [source], 'ocr_pages': [1], 'format': 'PDF'}
        with patch('patient_importer.extraction.extract', return_value=extracted):
            self.add('заключение.pdf', '%PDF-synthetic')
            document_id = self.workspace.import_folder()['batch'][0]['document_id']
        book = load_workbook(self.workbook)
        book.create_sheet('Сверки')['A1'] = 'Сохранить пользовательский лист'
        book.save(self.workbook)
        review = self.workspace.quality_review_list()[0]
        sections = review['analysis']['sections']
        conclusion = next(section for section in sections if section['section']=='Заключение')
        conclusion['text'] = 'Сверенный учебный текст'
        self.workspace.confirm_quality(document_id, sections=sections, note='Сверено заключение.')
        revised = dict(extracted, pages=[source.replace('черновик', 'иной вариант')])
        with patch('patient_importer.extraction.extract', return_value=revised):
            for _ in range(2):
                self.workspace.apply_reprocess(document_id, self.workspace.prepare_reprocess(document_id))
        book = load_workbook(self.workbook)
        self.assertEqual(book['Сверки']['A1'].value, 'Сохранить пользовательский лист')
        journal = book[book['_Программа']['B6'].value]
        self.assertEqual(journal['E2'].value, 'Сверено заключение.')
        self.assertIn('Сверенный учебный текст', [row[4] for row in book['Заключения'].values])
        self.assertIn('иной вариант', self.parsed(document_id)['pages'][0])

    def test_missing_workbook_is_regenerated_from_registry(self):
        self.add('анализ.txt', SAMPLE)
        self.workspace.import_folder()
        self.workbook.rename(self.workbook.with_name('резерв.xlsx'))
        self.assertTrue(self.workspace.summary()['pending_export'])
        self.assertFalse(self.workspace.import_folder()['pending_export'])
        self.assertTrue(self.workbook.is_file())


if __name__ == '__main__':
    unittest.main()
