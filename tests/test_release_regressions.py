"""Defects found by review before 2.5.0, kept from coming back. All data is fictional."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from patient_importer import clinical_sheets as sheets, quality_ui, ui
from patient_importer.analysis import analyze
from patient_importer.parsing import laboratory_rows
from patient_importer.storage import Workspace

HEAD = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n'
DAY = 'Дата выполнения исследования: 02.03.2025\n'
TITLE = 'Общий анализ крови\n'
TABLE = 'Показатель      Результат      Норма      Комментарий\n'
BLOOD = HEAD + DAY + TITLE + 'Показатель  Результат  Единицы\n'


def parse(text):
    return analyze({'pages': [text], 'ocr_pages': []}, 'учебный.txt')


class EmptyResultCellTests(unittest.TestCase):
    def row(self, line):
        return parse(HEAD + DAY + TITLE + TABLE + line)['observations'][0]

    def test_norm_in_the_result_place_goes_to_review(self):
        self.assertEqual(self.row('СРБ                 < 5')['status'], 'Проверить')
        self.assertEqual(self.row('АЛТ                 10-40')['status'], 'Проверить')

    def test_result_with_its_own_norm_is_fine(self):
        self.assertEqual(self.row('СРБ      < 5      (0 - 5)')['status'], 'Извлечено')

    def test_sediment_range_is_a_result(self):
        self.assertEqual(self.row('Лейкоциты     2-4')['status'], 'Извлечено')

    def test_plain_number_is_fine(self):
        self.assertEqual(self.row('Глюкоза     5,1')['status'], 'Извлечено')


class ContinuationPageTests(unittest.TestCase):
    HEADER = 'Показатель  Результат  Ед. изм.  Реф.интервал  Комментарий  Тест-система'
    FIRST = 'Гемоглобин    120    г/л    (110 - 140)'
    NEXT = 'Эритроциты    4,5    г/л    (4 - 5)'

    def test_rows_passed_over_before_a_confirmed_row_go_to_review(self):
        rows, pending = laboratory_rows([self.HEADER + '\n' + self.FIRST, 'Цвет     желтый\n' + self.NEXT])
        self.assertEqual([item['label'] for item in rows], ['Гемоглобин', 'Эритроциты'])
        self.assertEqual([item[2] for item in pending], ['Цвет     желтый'])

    def test_text_on_a_page_that_is_not_a_table_stays_silent(self):
        _rows, pending = laboratory_rows([self.HEADER + '\n' + self.FIRST, 'Врач     Учебный'])
        self.assertEqual(pending, [])


class AnalyteColumnTests(unittest.TestCase):
    CASES = [
        ('Билирубин непрямой', 'мкмоль/л', sheets.BIOCHEMISTRY, 'Билирубин непрямой (мкмоль/л)'),
        ('Непрямой билирубин', 'мкмоль/л', sheets.BIOCHEMISTRY, 'Билирубин непрямой (мкмоль/л)'),
        ('Прямой билирубин', 'мкмоль/л', sheets.BIOCHEMISTRY, 'Билирубин прямой (мкмоль/л)'),
        ('Общая железосвязывающая способность', 'мкмоль/л', sheets.BIOCHEMISTRY, None),
        ('Железо сыворотки', 'мкмоль/л', sheets.BIOCHEMISTRY, 'Железо (мкмоль/л)'),
        ('Кальций ионизированный', 'ммоль/л', sheets.BIOCHEMISTRY, 'Кальций ионизированный (ммоль/л)'),
        ('Холестерин не-ЛПВП', 'ммоль/л', sheets.BIOCHEMISTRY, 'Холестерин не-ЛПВП (ммоль/л)'),
        ('Микроальбумин', 'г/л', sheets.BIOCHEMISTRY, None),
        ('Креатинин в моче', 'мкмоль/л', sheets.BIOCHEMISTRY, None),
        ('Скорость оседания эритроцитов', '', sheets.BLOOD_COUNT, 'СОЭ (мм/ч)'),
        ('Гликированный гемоглобин', '', sheets.BLOOD_COUNT, 'HbA1c (%)'),
        ('Ширина распределения эритроцитов (RDW-SD)', '', sheets.BLOOD_COUNT, 'RDW-SD (фл)'),
    ]

    def test_each_label_lands_where_it_belongs(self):
        for label, unit, analytes, expected in self.CASES:
            with self.subTest(label=label):
                self.assertEqual(sheets.column_for(label, unit, analytes), expected)

    def study(self, label, value, unit='', document='S-1', day='2024-01-01'):
        return {'id': document, 'patient_id': 'P', 'name': 'ТЕСТОВ', 'birth': '2000-01-01', 'date': day, 'kind': 'Реберг',
                'status': 'Готово', 'filename': 'а', 'sections': [],
                'observations': [{'label': label, 'result': str(value), 'unit': unit, 'number': float(value),
                                  'origin': 'Лабораторный бланк', 'date': None}]}

    def test_parentheses_that_are_not_a_unit_do_not_merge_columns(self):
        built = sheets.build([self.study('Креатинин', 80), self.study('Креатинин (моча)', 9000, document='S-2', day='2024-02-01')])
        self.assertIn('Креатинин', built['Реберг'][0])
        self.assertIn('Креатинин (моча)', built['Реберг'][0])

    def test_measurement_keeps_its_own_date(self):
        item = {'label': 'Паренхима', 'result': '9', 'unit': 'мм', 'side': 'Правая', 'origin': 'Описание', 'date': '2021-02-02', 'number': 9.0}
        built = sheets.build([{'id': 'S', 'patient_id': 'P', 'name': 'ТЕСТОВ', 'birth': '2000-01-01', 'date': '2024-05-05', 'kind': 'УЗИ',
                               'status': 'Готово', 'filename': 'а', 'observations': [item],
                               'sections': [{'section': 'Заключение', 'text': 'Учебное.'}]}])
        headers, rows, _dates = built['УЗИ']
        found = {row[headers.index('Дата исследования')]: row for row in rows}
        self.assertEqual(found['2021-02-02'][headers.index('Паренхима правая')], '9 мм')
        self.assertEqual(found['2024-05-05'][headers.index('Заключение')], 'Учебное.')


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'case')
        self.target = self.workspace.root / 'Исследования.xlsx'

    def add(self, name, text):
        (self.workspace.root / 'Входящие' / name).write_text(text, encoding='utf-8')
        self.workspace.import_folder()

    def stored(self, index=0):
        with self.workspace.connect() as db:
            return json.loads(db.execute('SELECT analysis FROM documents ORDER BY filename').fetchall()[index][0])

    def edit(self, change):
        book = load_workbook(self.target)
        change(book)
        book.save(self.target)

    @staticmethod
    def note(book, title, text):
        sheet = book[title]
        column = next(cell.column for cell in sheet[1] if cell.value == 'Заметки')
        sheet.cell(2, column, text)


class ExportResilienceTests(WorkspaceCase):
    def test_user_sheet_differing_only_by_case(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120  г/л\n')
        def rename(book):
            del book['ОАК']
            book.create_sheet('Оак')['A1'] = 'Моё'
            for row in book['_Программа'].iter_rows(min_row=10, max_col=2):
                if row[0].value == 'clinical:ОАК':
                    row[0].value = row[1].value = None
        self.edit(rename)
        self.add('b.txt', BLOOD.replace('02.03.2025', '03.03.2025') + 'Гемоглобин  130  г/л\n')
        book = load_workbook(self.target)
        self.assertEqual(book['Оак']['A1'].value, 'Моё')
        self.assertIn('ОАК (авто)', book.sheetnames)

    def test_kept_sheet_is_reused_when_its_kind_returns(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120  г/л\n')
        self.edit(lambda book: self.note(book, 'ОАК', 'Учебная заметка'))
        with self.workspace.connect() as db:
            document = db.execute('SELECT id FROM documents').fetchone()[0]
        with patch('patient_importer.quality.needs_review', return_value=True):
            self.workspace.confirm_quality(document, study_type='Биохимия крови', note='Учебная сверка')
        self.assertIn('ОАК', load_workbook(self.target).sheetnames)
        self.add('b.txt', BLOOD.replace('02.03.2025', '03.03.2025') + 'Гемоглобин  130  г/л\n')
        self.assertNotIn('ОАК (авто)', load_workbook(self.target).sheetnames)

    def test_note_survives_a_date_correction(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120\n')
        self.edit(lambda book: self.note(book, 'ОАК', 'Учебная заметка'))
        record = self.workspace.quality_review_list()[0]
        self.workspace.confirm_quality(record['id'], date='2025-04-05', note='Учебная сверка')
        rows = list(load_workbook(self.target)['ОАК'].iter_rows(values_only=True))
        self.assertIn('Учебная заметка', rows[1])

    def test_note_on_a_row_that_is_gone_goes_to_the_archive(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120\n')
        self.add('b.txt', BLOOD.replace('02.03.2025', '03.03.2025') + 'Гемоглобин  130  г/л\n')
        self.edit(lambda book: self.note(book, 'ОАК', 'Учебная заметка'))
        record = self.workspace.quality_review_list()[0]
        self.workspace.confirm_quality(record['id'], study_type='ОАМ', note='Учебная сверка')
        book = load_workbook(self.target)
        values = [cell for row in book['Архив заметок'].iter_rows(values_only=True) for cell in row]
        self.assertIn('Учебная заметка', values)

    def test_unsettled_conflict_is_not_reported_as_verified(self):
        self.add('a.txt', HEAD + DAY + DAY.replace('02.03.2025', '03.03.2025') + TITLE + 'Показатель  Результат  Единицы\nГемоглобин  120  г/л\n')
        record = self.workspace.quality_review_list()[0]
        self.workspace.confirm_quality(record['id'], date='', study_type='ОАК', note='Учебная сверка', defer_export=True)
        self.assertTrue(self.stored()['quality']['incomplete'])

    def test_settled_conflict_is_complete(self):
        self.add('a.txt', HEAD + DAY + DAY.replace('02.03.2025', '03.03.2025') + TITLE + 'Показатель  Результат  Единицы\nГемоглобин  120  г/л\n')
        record = self.workspace.quality_review_list()[0]
        self.workspace.confirm_quality(record['id'], date='2025-03-02', study_type='ОАК', note='Учебная сверка', defer_export=True)
        self.assertFalse(self.stored()['quality']['incomplete'])

    def test_confirmed_repeat_follows_a_change_of_patient(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120  г/л\n')
        self.add('b.txt', BLOOD.replace('02.03.2025', '03.03.2025') + 'Гемоглобин  130  г/л\n')
        with self.workspace.connect() as db:
            first, second = [row[0] for row in db.execute('SELECT id FROM documents ORDER BY filename')]
            parsed = json.loads(db.execute('SELECT analysis FROM documents WHERE id=?', (second,)).fetchone()[0])
            parsed['duplicate_of'] = first
            with db:
                db.execute('UPDATE documents SET analysis=? WHERE id=?', (json.dumps(parsed, ensure_ascii=False), second))
        self.workspace.change_patient(first, 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
        with self.workspace.connect() as db:
            owners = {row[0] for row in db.execute('SELECT patient_id FROM documents')}
        self.assertEqual(len(owners), 1)


class WindowRegressionTests(WorkspaceCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f'Tk display unavailable: {exc}') from exc
        cls.root.withdraw()
        ui.setup(cls.root)
        cls.addClassCleanup(cls.root.destroy)

    def setUp(self):
        super().setUp()
        self.actions = ui.WorkspaceActions(self.root)
        self.errors = []
        self.enterContext(patch.object(self.root, 'report_callback_exception', lambda _k, e, _t: self.errors.append(e)))
        self.addCleanup(self.finish_widgets)

    def finish_widgets(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def open(self):
        review = quality_ui.QualityReviewWindow(self.root, self.workspace, Mock(), ui.Icons(self.root), actions=self.actions)
        self.root.update()
        return review

    def settle(self, review):
        deadline = monotonic() + 20
        while review.save_queue or self.actions.active or review.finishing or review.session_saved:
            self.root.update()
            self.assertFalse(self.errors, self.errors)
            self.assertLess(monotonic(), deadline)
            sleep(.005)
        self.root.update()

    def wait_saves(self, review):
        deadline = monotonic() + 20
        while review.save_queue or self.actions.active:
            self.root.update()
            self.assertLess(monotonic(), deadline)
            sleep(.005)
        self.root.update()

    def test_edited_conclusion_is_saved_with_the_queue(self):
        self.add('a.txt', HEAD + 'Ультразвуковое исследование почек\nЗаключение: старый текст.')
        review = self.open()
        self.assertEqual(review.current['kind'], 'date')
        review.show_full()
        sections = deepcopy(review.sections)
        sections[-1]['text'] = 'Заключение: исправленный текст.'
        review.sections = sections
        review.back_to_queue()
        self.root.update()
        review.focus_date.set('02.03.2025')
        review.accept()
        self.settle(review)
        self.assertEqual(self.stored()['sections'][-1]['text'], 'Заключение: исправленный текст.')

    def test_mistyped_date_stops_the_return_to_the_queue(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120\nЭритроциты  4,5  10^12/л\n')
        review = self.open()
        review.show_full()
        review.date.set('31.02.2025')
        review.back_to_queue()
        self.assertTrue(review.validation.get())
        self.assertTrue(review.full_mode)

    def test_mistyped_date_is_never_saved_as_no_date(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120\nЭритроциты  4,5  10^12/л\n')
        review = self.open()
        review.show_full()
        review.date.set('31.02.2025')
        review.full_mode = False
        review.current = review.found[review.selected][0]
        review.vars['unit'].set('г/л')
        review.accept()
        self.wait_saves(review)
        self.assertEqual(self.stored()['date'], '2025-03-02')
        self.assertFalse(self.stored()['quality'].get('confirmed'))
        self.assertEqual(len(review.records), 1)

    def test_removing_a_row_closes_its_question(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120\nЭритроциты  4,5\n')
        review = self.open()
        self.assertEqual(len(review.items), 2)
        review.show_full()
        review.rows.selection_set('0')
        with patch.object(quality_ui.messagebox, 'askyesno', return_value=True):
            review.remove_row()
        self.assertEqual(len(review.items), 1)
        review.back_to_queue()
        self.root.update()
        self.assertFalse(self.errors, self.errors)
        self.assertEqual(review.current['label'], 'Эритроциты')

    def test_entering_a_date_does_not_close_a_number_without_a_name(self):
        self.add('a.txt', HEAD + 'Учебное описание 12 мг/л.')
        review = self.open()
        self.assertEqual(review.current['kind'], 'date')
        before = len(review.items)
        review.focus_date.set('02.03.2025')
        review.accept()
        self.assertEqual(len(review.items), before - 1)
        self.assertTrue(any(item['kind'] == 'row' for item in review.open_stumbles(review.selected)))

    def test_duplicate_check_uses_the_current_revision(self):
        self.add('a.txt', BLOOD + 'Гемоглобин  120\n')
        review = self.open()
        review.revision = 'новая'
        record = review.records[review.selected]
        record['issues'] = [{'code': 'possible_duplicate', 'message': 'м', 'related_ids': ['S-учебный']}]
        with patch.object(quality_ui, 'DuplicateWindow') as window:
            review.compare_duplicate()
        self.assertEqual(window.call_args.args[2]['revision'], 'новая')


if __name__ == '__main__':
    unittest.main()
