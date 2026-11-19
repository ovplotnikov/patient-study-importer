"""Checking is optional: flags travel with the values, only an unknown patient stops a document.

All data is fictional.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from patient_importer import analysis, quality
from patient_importer.smoke import SAMPLE
from patient_importer.storage import FILING_RULE, Workspace
from patient_importer.telemetry_schema import valid_code, validate_report

IDENTITY = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n'
DAY = 'Дата выполнения исследования: {day}\n'
BLOOD = 'Общий анализ крови\nПоказатель  Результат  Единицы\n'
CLEAN = IDENTITY + DAY.format(day='02.03.2025') + BLOOD + 'Гемоглобин  120  г/л\n'
# «Гемоглобин  121» lost its unit column; «Лейкоциты 5 10^9/л» cannot be split into columns.
UNIT_MISSING = IDENTITY + DAY.format(day='02.03.2025') + BLOOD + 'Гемоглобин  121\n'
UNPARSED = (IDENTITY + DAY.format(day='02.03.2025') + 'Общий анализ крови\nПоказатель  Результат\n'
            + 'Гемоглобин  120 г/л\nЛейкоциты 5 10^9/л\nЭритроциты 4 10^12/л\n')
NO_PATIENT = 'Дата рождения: 01.01.2000\n' + DAY.format(day='02.03.2025') + BLOOD + 'Гемоглобин  120  г/л\n'
PROSE = (IDENTITY + DAY.format(day='02.03.2025') + 'Осмотр\n'
         'Объективно: состояние удовлетворительное, учебный размер 14 мм, Рост 120 см.\n')


def scan(text):
    """An extraction as OCR leaves it: the same text, read from page 1 of a scan."""
    return {'pages': [text], 'native_pages': [text], 'ocr_pages': [1], 'format': 'PDF',
            'extraction_issues': [{'code': 'OCR_USED', 'message': 'Текст распознан со скана.', 'page': 1}]}


class Capture:
    def __init__(self):
        self.counts = {}

    def consent_epoch(self):
        return 1

    def record_batch(self, counters, *, epoch):
        for metric, bucket in counters.items():
            for code, count in bucket.items():
                self.counts[metric, code] = self.counts.get((metric, code), 0) + count
        return True


class FlagTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.capture = Capture()
        self.workspace = Workspace(self.folder / 'учебная', diagnostics=self.capture)
        self.root = self.workspace.root

    def add(self, name, text, extracted=None):
        (self.root / 'Входящие' / name).write_text(text, encoding='utf-8')
        if extracted is None:
            return self.workspace.import_folder()
        with patch('patient_importer.extraction.extract', return_value=extracted):
            return self.workspace.import_folder()

    def book(self):
        return load_workbook(self.root / 'Исследования.xlsx')

    def rows(self, sheet):
        values = list(self.book()[sheet].iter_rows(values_only=True))
        return [dict(zip(values[0], row)) for row in values[1:]]

    # 1. Stops and flags

    def test_flagged_document_is_filed_as_processed_and_needs_nobody(self):
        result = self.add('без единицы.txt', UNIT_MISSING)
        self.assertEqual(result['batch'][0]['status'], 'С пометками')
        self.assertEqual((result['review'], result['total_review'], result['quality_review']), (0, 0, 1))
        self.assertTrue((self.root / 'Обработано' / 'без единицы.txt').is_file())
        self.assertFalse(any((self.root / 'Нужно уточнить').iterdir()))

    def test_scan_is_a_flag_not_a_stop(self):
        result = self.add('скан.pdf', '%PDF-учебный', scan(CLEAN))
        self.assertEqual(result['batch'][0]['status'], 'С пометками')
        self.assertEqual((result['total_review'], result['quality_review'], result['scanned']), (0, 1, 1))
        self.assertTrue((self.root / 'Обработано' / 'скан.pdf').is_file())

    def test_unknown_patient_still_stops(self):
        result = self.add('без ФИО.txt', NO_PATIENT)
        self.assertEqual(result['batch'][0]['status'], 'Уточнить пациента')
        self.assertEqual((result['review'], result['total_review']), (1, 1))
        self.assertTrue((self.root / 'Нужно уточнить' / 'без ФИО.txt').is_file())

    def test_clean_document_has_no_flags(self):
        result = self.add('чистый.txt', CLEAN)
        self.assertEqual(result['batch'][0]['status'], 'Добавлен')
        self.assertEqual(result['quality_review'], 0)

    def test_confirmation_still_removes_the_flags(self):
        result = self.add('без единицы.txt', UNIT_MISSING)
        record, = self.workspace.quality_review_list()
        rows = record['observations']
        rows[0]['unit'] = 'г/л'
        summary = self.workspace.confirm_quality(record['id'], observations=rows, note='Учебная сверка.',
                                                 expected_revision=record['revision'])
        self.assertEqual(summary['quality_review'], 0)
        self.assertEqual(self.rows('Исследования')[0]['Статус'], 'Проверено')
        self.assertEqual(self.rows('Показатели')[0]['Статус извлечения'], 'Проверено')
        self.assertTrue((self.root / 'Обработано' / 'без единицы.txt').is_file())
        self.assertEqual(result['batch'][0]['status'], 'С пометками')

    def hold_back_as_before(self, name):
        """Put a file back where 2.5.2 left it and forget that the new rule was applied."""
        with self.workspace.connect() as db, db:
            arrival = db.execute('SELECT * FROM arrivals WHERE source LIKE ?', ('%' + name,)).fetchone()
            held = self.root / 'Нужно уточнить' / name
            Path(arrival['destination']).replace(held)
            db.execute("UPDATE arrivals SET category='Нужно уточнить', destination=? WHERE id=?", (str(held), arrival['id']))
            db.execute("DELETE FROM meta WHERE key='filing_rule'")
        return held

    def test_files_held_back_by_an_earlier_version_move_on_open(self):
        self.add('без единицы.txt', UNIT_MISSING)
        self.add('без ФИО.txt', NO_PATIENT)
        held = self.hold_back_as_before('без единицы.txt')
        reopened = Workspace(self.root)
        self.assertFalse(held.exists())
        self.assertTrue((self.root / 'Обработано' / 'без единицы.txt').is_file())
        self.assertTrue((self.root / 'Нужно уточнить' / 'без ФИО.txt').is_file())
        self.assertEqual(reopened.summary()['pending_moves'], 0)
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='filing_rule'").fetchone()[0], FILING_RULE)

    def test_a_file_outside_the_work_folder_never_keeps_it_from_opening(self):
        self.add('без единицы.txt', UNIT_MISSING)
        with self.workspace.connect() as db, db:
            db.execute("UPDATE arrivals SET source='архив/вне.txt', destination=NULL, category='Нужно уточнить', status='done'")
            db.execute("DELETE FROM meta WHERE key='filing_rule'")
        reopened = Workspace(self.root)
        problem, = reopened.problems()
        self.assertIn('вне рабочей папки', problem['error'])
        self.assertEqual(reopened.summary()['pending_moves'], 1)

    def test_refiling_runs_once(self):
        self.add('без единицы.txt', UNIT_MISSING)
        with patch.object(Workspace, 'finish_moves') as moves:
            Workspace(self.root)
        moves.assert_not_called()

    def test_file_that_cannot_move_waits_like_any_other_move(self):
        self.add('без единицы.txt', UNIT_MISSING)
        held = self.hold_back_as_before('без единицы.txt')
        with patch('patient_importer.storage.os.replace', side_effect=PermissionError('занят')):
            reopened = Workspace(self.root)
        self.assertTrue(held.exists())
        self.assertEqual(reopened.summary()['pending_moves'], 1)
        reopened.import_folder()
        self.assertTrue((self.root / 'Обработано' / 'без единицы.txt').is_file())

    # 2. Flags in the table

    def test_study_and_rows_show_why_they_are_not_verified(self):
        self.add('без единицы.txt', UNIT_MISSING)
        study, = self.rows('Исследования')
        self.assertEqual(study['Статус'], 'Не сверено · нет единицы')
        row, = self.rows('Показатели')
        self.assertEqual(row['Статус извлечения'], 'Не сверено · нет единицы')
        check, = self.rows('Проверка')
        self.assertEqual(check['Что проверить'], 'Не сверено · нет единицы')
        self.assertIn('единица', check['Подробности'])
        self.assertTrue(self.rows('ОАК')[0]['Статус'].startswith('Не сверено'))

    def test_values_from_a_scan_are_marked_in_every_sheet(self):
        self.add('скан 2015.pdf', '%PDF-учебный-1', scan(CLEAN.replace('02.03.2025', '02.03.2015')))
        self.add('бланк 2017.txt', CLEAN.replace('02.03.2025', '02.03.2017').replace('120', '130'))
        rows = self.rows('Показатели')
        statuses = {row['Дата показателя'].year: row['Статус извлечения'] for row in rows}
        self.assertEqual(statuses[2015], 'Не сверено · со скана')
        self.assertEqual(statuses[2017], 'Извлечено')
        blood = {row['Дата исследования'].year: row['Статус'] for row in self.rows('ОАК')}
        self.assertEqual(blood[2015], 'Не сверено · со скана')
        self.assertEqual(blood[2017], 'Готово')
        trend, = self.rows('Динамика')
        self.assertEqual(trend['Несверенные значения'], '1 из 2, со скана: 1')
        self.assertEqual(self.rows('Свод')[0]['Есть несверенные данные'], 'Нет')
        self.add('скан 2019.pdf', '%PDF-учебный-2', scan(CLEAN.replace('02.03.2025', '02.03.2019')))
        self.assertEqual(self.rows('Свод')[0]['Есть несверенные данные'], 'Да: ОАК (со скана)')
        trend, = self.rows('Динамика')
        self.assertEqual(trend['Несверенные значения'], '2 из 3, со скана: 2; последнее не сверено (со скана)')

    def test_overview_counts_flagged_documents(self):
        self.add('скан.pdf', '%PDF-учебный', scan(CLEAN))
        overview = {row[0]: row[1] for row in self.book()['Обзор'].iter_rows(values_only=True) if row and row[0]}
        self.assertEqual((overview['Не сверено'], overview['Из них со скана']), (1, 1))
        self.assertNotIn('Проверить данные', overview)

    # 3. Unread table lines are missing results

    def test_unread_lines_are_counted_on_the_document(self):
        result = self.add('строки.txt', UNPARSED)
        self.assertEqual(result['unparsed'], 1)
        study, = self.rows('Исследования')
        self.assertEqual(study['Не перенесено строк'], 2)
        self.assertTrue(study['Статус'].startswith('Не сверено · не перенесено строк: 2'))
        self.assertTrue(self.rows('ОАК')[0]['Статус'].startswith('Не сверено · не перенесено строк: 2'))
        self.assertEqual(sum(row['Что проверить'] == 'Не сверено · строка не перенесена' for row in self.rows('Проверка')), 2)

    def test_unread_lines_stay_counted_after_an_incomplete_confirmation(self):
        self.add('строки.txt', UNPARSED)
        record, = self.workspace.quality_review_list()
        self.workspace.confirm_quality(record['id'], note='В учебном бланке две строки не перенесены.')
        study, = self.rows('Исследования')
        self.assertEqual((study['Статус'], study['Не перенесено строк']), ('Сверено · неполные данные', 2))

    def test_clean_document_has_no_missing_lines(self):
        self.add('чистый.txt', CLEAN)
        self.assertIsNone(self.rows('Исследования')[0]['Не перенесено строк'])

    # 5. Diagnostics

    def test_flagged_outcome_is_its_own_code(self):
        self.add('чистый.txt', CLEAN)
        self.add('без единицы.txt', UNIT_MISSING)
        self.add('без ФИО.txt', NO_PATIENT)
        counts = self.capture.counts
        self.assertEqual((counts['outcomes', 'saved'], counts['outcomes', 'saved_flagged'],
                          counts['outcomes', 'identity_review']), (1, 1, 1))
        self.assertNotIn(('outcomes', 'quality_review'), counts)
        self.assertEqual(counts['review_reasons', 'missing_unit'], 1)


class ReasonTests(unittest.TestCase):
    def test_reasons_put_missing_lines_and_scans_first(self):
        found = [{'code': 'missing_date'}, {'code': 'ocr_value', 'observation_index': 0},
                 {'code': 'unparsed_row', 'page': 1, 'line': 5}, {'code': 'unparsed_row', 'page': 1, 'line': 6},
                 {'code': 'PDF_TEXT_UNREADABLE'}]
        self.assertEqual(quality.reasons(found),
                         ['не перенесено строк: 2', 'со скана', 'нет даты', 'прочитано не полностью'])
        self.assertEqual(quality.unverified(quality.reasons(found)[:2]), 'Не сверено · не перенесено строк: 2, со скана')
        self.assertEqual(quality.unverified([]), 'Не сверено')

    def test_same_line_counts_once(self):
        found = [{'code': 'unparsed_row', 'page': 1, 'line': 5}] * 2
        self.assertEqual(quality.unparsed_rows(found), 1)

    def test_row_reasons_are_its_own_and_document_wide_ones(self):
        found = [{'code': 'missing_unit', 'observation_index': 1}, {'code': 'missing_date'},
                 {'code': 'unknown_type'}]
        self.assertEqual(quality.row_reasons(found, 0, {}), ['нет даты'])
        self.assertEqual(quality.row_reasons(found, 1, {'from_ocr': True}), ['со скана', 'нет единицы', 'нет даты'])


class NarrativeTests(unittest.TestCase):
    def parse(self, text):
        return analysis.analyze({'pages': [text], 'ocr_pages': []}, 'учебный.txt')

    def test_number_without_a_name_is_not_extracted_and_its_text_is_kept(self):
        result = self.parse(PROSE)
        self.assertEqual([(row['label'], row['result']) for row in result['observations']], [('Рост', '120')])
        self.assertTrue(any('учебный размер 14 мм' in section['text'] for section in result['sections']))
        self.assertNotIn('ambiguous_value', {issue['code'] for issue in quality.issues(result)})

    def test_unknown_form_keeps_only_named_numbers(self):
        result = self.parse(IDENTITY + DAY.format(day='02.03.2025') + 'Учебная запись: 5 ммоль/л, Креатинин 70 мкмоль/л.')
        self.assertEqual([(row['label'], row['result']) for row in result['observations']], [('Креатинин', '70')])

    def test_no_value_carries_the_unnamed_label(self):
        result = self.parse(IDENTITY + DAY.format(day='02.03.2025') + BLOOD + 'Гемоглобин  120  г/л\n')
        result_two = self.parse(PROSE + 'Ещё 3 мл и 7 %.\n')
        for parsed in (result, result_two):
            self.assertNotIn(analysis.UNNAMED_METRIC, [row['label'] for row in parsed['observations']])


class ParserVersionTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'учебная')

    def store_as(self, version, extra=()):
        (self.workspace.root / 'Входящие' / 'осмотр.txt').write_text(PROSE, encoding='utf-8')
        self.workspace.import_folder()
        with self.workspace.connect() as db, db:
            row = db.execute('SELECT id, analysis FROM documents').fetchone()
            parsed = json.loads(row['analysis'])
            parsed['version'] = version
            parsed['observations'].extend(extra)
            if extra:
                parsed['quality']['issues'].append({'id': 'Q-учебный', 'code': 'ambiguous_value', 'severity': 'review',
                                                    'message': 'Сверьте с оригиналом результат.',
                                                    'observation_index': len(parsed['observations']) - 1})
            db.execute('UPDATE documents SET analysis=? WHERE id=?', (json.dumps(parsed, ensure_ascii=False), row['id']))
        return row['id']

    def test_readings_of_the_reviewed_parser_are_not_called_old(self):
        self.assertFalse(quality.is_legacy_parser('2.5.0'))
        self.assertFalse(quality.is_legacy_parser(analysis.PARSER_VERSION))
        self.assertFalse(quality.is_legacy_parser(None))
        self.assertTrue(quality.is_legacy_parser('2.4.1'))
        self.assertTrue(quality.is_legacy_parser('учебная'))
        stored = self.parse_stored('2.5.0')
        self.assertNotIn('legacy_parser', {issue['code'] for issue in quality.assess(stored)['issues']})
        self.assertIn('legacy_parser', {issue['code'] for issue in quality.assess({**stored, 'version': '2.4.1'})['issues']})

    def parse_stored(self, version):
        parsed = analysis.analyze({'pages': [PROSE], 'ocr_pages': []}, 'учебный.txt')
        parsed['version'] = version
        return parsed

    def test_new_rules_are_offered_to_documents_of_the_previous_parser(self):
        unnamed = {'page': 1, 'page_index': 1, 'line': 4, 'section': 'Объективно', 'label': analysis.UNNAMED_METRIC,
                   'result': '14', 'number': 14.0, 'unit': 'мм', 'reference': '', 'raw': '', 'flag': '',
                   'comment': '', 'side': '', 'date': '2025-03-02', 'date_source': 'Дата документа',
                   'origin': 'Описание', 'status': 'Проверить', 'from_ocr': False}
        document_id = self.store_as('2.5.0', [unnamed])
        self.assertEqual(self.workspace.summary()['reparse'], 1)
        self.assertEqual(self.workspace.summary()['quality_review'], 1)
        report = self.workspace.reparse_unconfirmed()
        self.assertEqual(report['updated'], 1)
        parsed = json.loads(self.workspace.document(document_id)['analysis'])
        self.assertEqual(parsed['version'], analysis.PARSER_VERSION)
        self.assertNotIn(analysis.UNNAMED_METRIC, [row['label'] for row in parsed['observations']])
        self.assertNotIn('legacy_parser', {issue['code'] for issue in quality.issues(parsed)})
        summary = self.workspace.summary()
        self.assertEqual((summary['reparse'], summary['quality_review']), (0, 0))


class WindowTextTests(unittest.TestCase):
    def test_flagged_line_says_values_are_in_and_checking_is_optional(self):
        from patient_importer.gui import flagged_text
        text = flagged_text({'quality_review': 3, 'scanned': 2, 'unparsed': 1})
        self.assertTrue(text.startswith('Данные уже в таблице с отметкой «Не сверено»'))
        self.assertIn('со скана: 2', text)
        self.assertIn('не все строки перенесены: 1', text)
        self.assertTrue(text.endswith('Сверка по желанию'))
        self.assertNotIn('скан', flagged_text({'quality_review': 1, 'scanned': 0, 'unparsed': 0}))


class WireTests(unittest.TestCase):
    def test_schema_accepts_the_new_outcome_and_the_old_one(self):
        self.assertTrue(valid_code('outcomes', 'saved_flagged'))
        self.assertTrue(valid_code('outcomes', 'quality_review'))
        report = {'schema_version': 2, 'app_version': '2.5.2',
                  'counters': {'formats': {'pdf': 10}, 'outcomes': {'saved_flagged': 6, 'saved': 4}}}
        self.assertEqual(validate_report(report)['counters']['outcomes'], {'saved_flagged': 6, 'saved': 4})

    def test_apps_script_lists_the_new_outcome(self):
        code = (Path(__file__).resolve().parents[1] / 'google_apps_script' / 'Code.gs').read_text(encoding='utf8')
        self.assertIn('"saved_flagged"', code)


if __name__ == '__main__':
    unittest.main()
