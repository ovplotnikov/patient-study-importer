"""The queue of stumbles: its rules alone, then the real window over a real workspace. Fictional data."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from patient_importer import quality_ui, review_queue, ui
from patient_importer.storage import Workspace

FORM = ('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n'
        'Дата выполнения исследования: 02.03.2025\nОбщий анализ крови\nПоказатель  Результат\n')


def row_issue(code, index, message='Учебное замечание.'):
    return {'code': code, 'message': message, 'observation_index': index}


def record(issues, observations=(), **analysis):
    return {'issues': issues, 'observations': list(observations), 'analysis': analysis}


class StumbleTests(unittest.TestCase):
    ROWS = [{'result_id': 'a', 'label': 'Гемоглобин', 'page': 1, 'page_index': 1, 'line': 8, 'raw': 'Гемоглобин 120'},
            {'result_id': 'b', 'label': 'Креатинин', 'page': 1, 'page_index': 1, 'line': 5, 'raw': 'Креатинин 90'}]

    def test_issues_of_one_row_are_one_question(self):
        found = review_queue.stumbles(record([row_issue('missing_unit', 0), row_issue('ambiguous_value', 0)], self.ROWS))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['codes'], ['missing_unit', 'ambiguous_value'])

    def test_date_and_type_come_first_then_reading_order(self):
        issues = [row_issue('missing_unit', 0), row_issue('missing_unit', 1),
                  {'code': 'unknown_type', 'message': 'м'}, {'code': 'missing_date', 'message': 'м'}]
        kinds = [item.get('result_id', item['kind']) for item in review_queue.stumbles(record(issues, self.ROWS))]
        self.assertEqual(kinds, ['date', 'type', 'b', 'a'])

    def test_unparsed_row_brings_its_line(self):
        issue = {'code': 'unparsed_row', 'message': 'м', 'page': 1, 'page_index': 1, 'line': 2}
        found = review_queue.stumbles(record([issue], pages=['первая\n  Лейкоциты 5 10^9/л\nтретья']))
        self.assertEqual(found[0]['raw'], 'Лейкоциты 5 10^9/л')

    def test_scan_needs_the_whole_document(self):
        self.assertTrue(review_queue.needs_full_review(record([{'code': 'ocr_text'}])))
        self.assertFalse(review_queue.needs_full_review(record([{'code': 'missing_date'}])))


class PrefillTests(unittest.TestCase):
    def test_cells_by_content(self):
        self.assertEqual(review_queue.prefill_row('Гемоглобин    120    г/л    (110 - 140)'),
                         {'label': 'Гемоглобин', 'result': '120', 'unit': 'г/л', 'reference': '(110 - 140)'})

    def test_label_split_over_two_cells(self):
        fields = review_queue.prefill_row('Общий  белок    70    г/л')
        self.assertEqual((fields['label'], fields['result']), ('Общий белок', '70'))

    def test_line_with_no_gaps(self):
        fields = review_queue.prefill_row('Лейкоциты 5 10^9/л')
        self.assertEqual((fields['label'], fields['result'], fields['unit']), ('Лейкоциты', '5', '10^9/л'))

    def test_deviation_mark_is_not_a_cell(self):
        self.assertEqual(review_queue.prefill_row('!   Гемоглобин   90   г/л')['label'], 'Гемоглобин')

    def test_words_only_go_to_the_label(self):
        self.assertEqual(review_queue.prefill_row('Учебная строка')['result'], '')


class FindLineTests(unittest.TestCase):
    RECTS = [('Гемоглобин', (50, 700, 150, 712)), ('120 г/л', (300, 701, 350, 712)),
             ('Лейкоциты', (50, 680, 150, 692)), ('5,2', (300, 680, 320, 692)),
             ('Лейкоциты', (50, 200, 150, 212)), ('5,2', (300, 200, 320, 212))]

    def lines(self):
        return review_queue.visual_lines(self.RECTS, 800)

    def test_parts_of_a_line_join_from_the_top_edge(self):
        first = self.lines()[0]
        self.assertEqual(first['text'], 'Гемоглобин 120 г/л')
        self.assertEqual(first['box'], (50, 88, 350, 100))

    def test_finds_by_words_and_numbers(self):
        self.assertEqual(review_queue.find_line(self.lines(), 'Гемоглобин     120   г/л')['box'][1], 88)

    def test_identical_rows_are_told_apart_by_position(self):
        low = review_queue.find_line(self.lines(), 'Лейкоциты  5,2', line=30, total=30)
        high = review_queue.find_line(self.lines(), 'Лейкоциты  5,2', line=2, total=30)
        self.assertGreater(low['box'][1], high['box'][1])

    def test_unrelated_text_is_not_framed(self):
        self.assertIsNone(review_queue.find_line(self.lines(), 'Креатинин 88 мкмоль/л'))

    def test_empty_text_is_not_framed(self):
        self.assertIsNone(review_queue.find_line(self.lines(), ''))


class WindowTests(unittest.TestCase):
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
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'case')
        self.actions = ui.WorkspaceActions(self.root)
        self.icons = ui.Icons(self.root)
        self.on_change = Mock()
        self.errors = []
        self.enterContext(patch.object(self.root, 'report_callback_exception', lambda _k, e, _t: self.errors.append(e)))
        self.addCleanup(self.finish_widgets)

    def finish_widgets(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def add(self, name, text):
        (self.workspace.root / 'Входящие' / name).write_text(text, encoding='utf-8')

    def pump(self, condition):
        deadline = monotonic() + 20
        while not condition():
            self.root.update()
            self.assertFalse(self.errors, self.errors)
            self.assertLess(monotonic(), deadline, 'Tk operation timed out')
            sleep(.005)
        self.root.update()

    def open(self):
        self.workspace.import_folder()
        return quality_ui.QualityReviewWindow(self.root, self.workspace, self.on_change, self.icons, actions=self.actions)

    def settled(self, review):
        self.pump(lambda: not review.save_queue and not self.actions.active and not review.finishing and not review.session_saved)

    def stored(self):
        with self.workspace.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT analysis FROM documents ORDER BY filename')]

    def workbook_values(self):
        book = load_workbook(self.workspace.root / 'Исследования.xlsx')
        return {str(cell) for row in book['Показатели'].iter_rows(values_only=True) for cell in row if cell is not None}

    def test_queue_opens_on_the_first_stumble_with_fields_filled(self):
        self.add('a.txt', FORM + 'Гемоглобин  120\nЭритроциты  4,5 10^12/л')
        review = self.open()
        self.root.update()
        self.assertFalse(review.full_mode)
        self.assertEqual(review.current['title'], 'Нет единицы измерения')
        self.assertEqual(review.vars['label'].get(), 'Гемоглобин')
        self.assertEqual(review.vars['result'].get(), '120')

    def test_accepting_the_last_stumble_saves_and_updates_excel_once(self):
        self.add('a.txt', FORM + 'Гемоглобин  120\nЭритроциты  4,5 10^12/л')
        review = self.open()
        self.root.update()
        with patch.object(self.workspace, 'finish_review_session', wraps=self.workspace.finish_review_session) as finish:
            review.vars['unit'].set('г/л')
            review.accept()
            self.settled(review)
        finish.assert_called_once_with()
        rows = self.stored()[0]['observations']
        self.assertEqual((rows[0]['unit'], rows[0]['status']), ('г/л', 'Проверено'))
        self.assertEqual(rows[1]['status'], 'Извлечено', 'A row nobody was asked about must not claim review')
        self.assertTrue(self.stored()[0]['quality']['confirmed'])
        self.assertIn('г/л', self.workbook_values())
        self.on_change.assert_called_once_with()

    def test_unparsed_line_is_offered_laid_out_and_becomes_a_row(self):
        self.add('a.txt', FORM + 'Гемоглобин  120 г/л\nЛейкоциты 5 10^9/л')
        review = self.open()
        self.root.update()
        self.assertEqual(review.current['kind'], 'unparsed')
        self.assertEqual(review.vars['label'].get(), 'Лейкоциты')
        self.assertEqual(review.vars['unit'].get(), '10^9/л')
        review.accept()
        self.settled(review)
        parsed = self.stored()[0]
        self.assertEqual(parsed['table_issues'], [])
        added = parsed['observations'][-1]
        self.assertEqual((added['label'], added['result'], added['origin']), ('Лейкоциты', '5', 'Введено при сверке'))
        self.assertEqual((added['line'], added['raw']), (7, 'Лейкоциты 5 10^9/л'))
        self.assertFalse(parsed['quality']['incomplete'])

    def test_line_that_is_not_a_result_is_closed_without_a_row(self):
        self.add('a.txt', FORM + 'Гемоглобин  120 г/л\nУчебная подпись 5 штук')
        review = self.open()
        self.root.update()
        review.dismiss()
        self.settled(review)
        parsed = self.stored()[0]
        self.assertEqual(len(parsed['observations']), 1)
        self.assertEqual(parsed['table_issues'], [])

    def test_empty_result_is_refused_and_keeps_the_question_open(self):
        self.add('a.txt', FORM + 'Гемоглобин  120\nЭритроциты  4,5 10^12/л')
        review = self.open()
        self.root.update()
        review.vars['result'].set('')
        review.accept()
        self.assertTrue(review.focus_error.get())
        self.assertIsNotNone(review.current)
        self.workspace_is_unconfirmed = self.stored()[0]['quality'].get('confirmed')
        self.assertFalse(self.workspace_is_unconfirmed)

    def test_same_unit_is_applied_across_documents(self):
        self.add('a.txt', FORM + 'Учебный тест  5 ктл')
        self.add('b.txt', FORM.replace('02.03.2025', '03.03.2025') + 'Учебный тест  7 ктл')
        review = self.open()
        self.root.update()
        self.assertEqual(len(review.similar), 1)
        review.bulk.set(True)
        review.accept()
        self.settled(review)
        self.assertTrue(all(item['quality']['confirmed'] for item in self.stored()))
        self.assertFalse(review.records)

    def test_skip_leaves_the_question_in_the_queue(self):
        self.add('a.txt', FORM + 'Гемоглобин  120\nЭритроциты  4,5')
        review = self.open()
        self.root.update()
        first = review.selected_item
        review.skip()
        self.assertNotEqual(review.selected_item, first)
        self.assertEqual(len(review.items), 2)

    def test_date_is_asked_and_clears_values_that_only_lacked_it(self):
        text = FORM.replace('Дата выполнения исследования: 02.03.2025\n', '').replace('Общий анализ крови\nПоказатель  Результат\n', '')
        self.add('a.txt', text + 'Ультразвуковое исследование почек\nПаренхима 15 мм.\nЗаключение: учебное.')
        review = self.open()
        self.root.update()
        self.assertEqual(review.current['kind'], 'date')
        review.focus_date.set('02.03.2025')
        review.accept()
        self.settled(review)
        parsed = self.stored()[0]
        self.assertEqual(parsed['date'], '2025-03-02')
        self.assertEqual(parsed['observations'][0]['date'], '2025-03-02')
        self.assertEqual(parsed['observations'][0]['status'], 'Извлечено')

    def test_wrong_date_is_refused(self):
        text = FORM.replace('Дата выполнения исследования: 02.03.2025\n', '')
        self.add('a.txt', text + 'Гемоглобин  120 г/л')
        review = self.open()
        self.root.update()
        review.focus_date.set('32.13.2025')
        review.accept()
        self.assertTrue(review.focus_error.get())

    def test_scan_opens_whole_document_and_needs_the_attestation(self):
        self.workspace.import_folder()
        with patch('patient_importer.extraction.extract') as extract:
            extract.return_value = {'pages': [FORM + 'Гемоглобин  120 г/л'], 'native_pages': [''], 'page_numbers': [1],
                                    'ocr_pages': [1], 'format': 'Изображение', 'ocr_evidence': [], 'ocr_alternatives': [],
                                    'extraction_issues': []}
            self.add('scan.txt', 'учебный скан')
            review = self.open()
        self.root.update()
        self.assertTrue(review.full_mode)
        self.assertEqual(str(review.confirm_button['state']), 'disabled')
        review.accept()
        self.assertFalse(self.stored()[0]['quality'].get('confirmed'))

    def test_failed_save_returns_the_document_with_edits(self):
        self.add('a.txt', FORM + 'Гемоглобин  120\nЭритроциты  4,5 10^12/л')
        review = self.open()
        self.root.update()
        review.vars['unit'].set('г/л')
        with patch.object(self.workspace, 'confirm_quality', side_effect=ValueError('Учебный сбой')):
            review.accept()
            self.pump(lambda: not review.save_queue and not self.actions.active)
        self.assertEqual(len(review.records), 1)
        self.assertTrue(review.full_mode)
        self.assertEqual(review.validation.get(), 'Учебный сбой')
        self.assertEqual(review.observations[0]['unit'], 'г/л')

    def test_closing_mid_session_updates_excel_first(self):
        self.add('a.txt', FORM + 'Гемоглобин  120\nЭритроциты  4,5 10^12/л')
        self.add('b.txt', FORM.replace('02.03.2025', '03.03.2025') + 'Гемоглобин  90\nЭритроциты  4,5 10^12/л')
        review = self.open()
        self.root.update()
        review.vars['unit'].set('г/л')
        review.accept()
        self.pump(lambda: not review.save_queue and not self.actions.active)
        self.assertTrue(review.session_saved)
        review.close()
        self.pump(lambda: not review.window.winfo_exists())
        self.assertIn('г/л', self.workbook_values())

    def test_frame_is_drawn_on_a_text_pdf_and_scrolled_to(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        path = self.folder / 'text.pdf'
        writer = PdfWriter()
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 50 120 Td (Hemoglobin 120 g/l) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(path)
        window = tk.Toplevel(self.root)
        window.geometry('900x400')
        preview = quality_ui.SourcePreview(window, self.icons)
        preview.pack(fill='both', expand=True)
        window.update()
        preview.load({'original': str(path)}, {'pages': ['Hemoglobin 120 g/l'], 'page_numbers': [1]})
        self.pump(lambda: preview.displayed is not None)
        preview.locate({'page': 1, 'page_index': 1, 'line': 1, 'raw': 'Hemoglobin   120   g/l'})
        self.root.update()
        frames = preview.canvas.find_withtag('source-highlight')
        self.assertEqual(len(frames), 1)
        self.assertGreater(preview.canvas.yview()[0], 0)
        _left, top, _right, bottom = preview.canvas.coords(frames[0])
        self.assertLess(preview.canvas.canvasy(0), top)
        self.assertLess(bottom, preview.canvas.canvasy(preview.canvas.winfo_height()))


class DeferredSaveTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'case')
        (self.workspace.root / 'Входящие' / 'a.txt').write_text(FORM + 'Гемоглобин  120', encoding='utf-8')
        self.workspace.import_folder()
        self.record = self.workspace.quality_review_list()[0]

    def backups(self):
        return len(list((self.workspace.root / 'Резервные копии').glob('База_*.sqlite')))

    def confirm(self, **options):
        return self.workspace.confirm_quality(self.record['id'], note='Учебная сверка', defer_export=True, **options)

    def test_deferred_save_leaves_the_workbook_until_the_end(self):
        with patch('patient_importer.workbook.export_workbook') as export:
            self.confirm()
            export.assert_not_called()
            self.workspace.finish_review_session()
            export.assert_called_once()

    def test_deferred_save_reports_the_new_revision(self):
        result = self.confirm()
        with self.workspace.connect() as db:
            self.assertEqual(result['revision'], self.workspace._revision(db))

    def test_backup_can_be_skipped_after_the_first(self):
        before = self.backups()
        self.confirm(skip_backup=True)
        self.assertEqual(self.backups(), before)

    def test_only_checked_rows_are_marked_reviewed(self):
        self.confirm(checked_ids=set())
        with self.workspace.connect() as db:
            parsed = json.loads(db.execute('SELECT analysis FROM documents').fetchone()[0])
        self.assertNotEqual(parsed['observations'][0]['status'], 'Проверено')

    def test_without_a_list_every_row_is_reviewed(self):
        self.workspace.confirm_quality(self.record['id'], note='Учебная сверка')
        with self.workspace.connect() as db:
            parsed = json.loads(db.execute('SELECT analysis FROM documents').fetchone()[0])
        self.assertIn(parsed['observations'][0]['status'], {'Проверено', 'Сверено · неполные данные'})


if __name__ == '__main__':
    unittest.main()
