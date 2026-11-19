"""The window «Кому принадлежит документ?»: folder rows, the file as the doctor named it,
a draft from the folder name and a page large enough to read. Fictional people only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import re
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from patient_importer import analysis, gui, review_queue as queue, ui
from patient_importer.intake import stage_files
from patient_importer.storage import Workspace

TESTOV = ('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01')
# The window redraws the page when the panel changes by this much (ReviewWindow.canvas_resized).
REDRAW_STEP = 24
UNNAMED = 'Дата выполнения исследования: 05.03.2025\nЗаключение: учебное {value}.\n'
BIRTH_ONLY = 'Дата рождения: 01.01.2000\nДата выполнения исследования: 05.03.2025\nЗаключение: учебное.\n'


def record(document, folder, *, names=(), births=(), **other):
    return {'id': document, 'folder': folder, 'names': list(names), 'births': list(births), 'candidates': [],
            'reason': 'Недостаточно идентификаторов', 'filename': document + '.txt', **other}


class RowTests(unittest.TestCase):
    def test_documents_that_name_nobody_share_one_row_per_folder(self):
        rows = queue.review_rows([record('a', 'Входящие/Папка 1'), record('b', 'Входящие/Папка 1'),
                                  record('c', 'Входящие/Папка 2'), record('d', 'Входящие/Папка 1')])
        self.assertEqual([(row['kind'], row['documents']) for row in rows],
                         [('folder', ['a', 'b', 'd']), ('document', ['c'])])

    def test_documents_that_need_their_own_look_stay_separate(self):
        rows = queue.review_rows([
            record('a', 'Входящие/Папка'), record('b', 'Входящие/Папка'),
            record('name', 'Входящие/Папка', names=['ТЕСТОВ ТЕСТ']),
            record('birth', 'Входящие/Папка', births=['2000-01-01']),
            record('scan', 'Входящие/Папка', scan=True),
            record('ocr', 'Входящие/Папка', identity_from_ocr=True),
            record('settled1', 'Входящие/Известная', folder_settled=True),
            record('settled2', 'Входящие/Известная', folder_settled=True),
            record('pile1', 'Входящие'), record('pile2', 'Входящие'),
        ])
        self.assertEqual([row['documents'] for row in rows],
                         [['a', 'b'], ['name'], ['birth'], ['scan'], ['ocr'], ['settled1'], ['settled2'], ['pile1'], ['pile2']])


class DraftTests(unittest.TestCase):
    def test_full_name_and_one_date(self):
        self.assertEqual(queue.folder_name_draft('Входящие/Тестова Анна Тестовна 11.07.2012'),
                         ('Тестова Анна Тестовна', '2012-07-11'))

    def test_capitals_and_a_double_surname(self):
        self.assertEqual(queue.folder_name_draft('Входящие/ТЕСТОВ ТЕСТ ТЕСТОВИЧ'), ('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', None))
        self.assertEqual(queue.folder_name_draft('Входящие/Тестова-Примерова Анна Ильинична')[0],
                         'Тестова-Примерова Анна Ильинична')

    def test_initials_or_no_patronymic_give_nothing(self):
        for folder in ('Входящие/Тестов Т.Т. 01.01.2000', 'Входящие/Тестов Тест 01.01.2000', 'Входящие/2024'):
            self.assertIsNone(queue.folder_name_draft(folder), folder)

    def test_two_dates_give_only_the_name_and_two_names_give_nothing(self):
        self.assertEqual(queue.folder_name_draft('Входящие/Тестов Тест Тестович 01.01.2000 05.03.2025'),
                         ('Тестов Тест Тестович', None))
        self.assertIsNone(queue.folder_name_draft('Входящие/Тестов Тест Тестович и Примеров Пример Примерович'))

    def test_impossible_or_future_date_is_left_out(self):
        self.assertEqual(queue.folder_name_draft('Входящие/Тестов Тест Тестович 31.02.2000')[1], None)
        self.assertEqual(queue.folder_name_draft('Входящие/Тестов Тест Тестович 01.01.2999')[1], None)

    def test_a_year_folder_takes_the_draft_from_the_patient_folder_above(self):
        self.assertEqual(queue.folder_name_draft('Входящие/Тестов Тест Тестович/2024')[0], 'Тестов Тест Тестович')

    def test_matching_never_reads_the_folder_name(self):
        items = [{'id': 'a', 'folder': 'Входящие/ТЕСТОВ ТЕСТ ТЕСТОВИЧ 01.01.2000',
                  'analysis': {'names': [], 'births': [], 'identity_from_ocr': False}}]
        result = analysis.resolve(items, [{'name': TESTOV[0], 'birth': TESTOV[1]}])['a']
        self.assertIsNone(result['key'])
        self.assertEqual(result['candidates'], [])


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'рабочая')
        self.source = self.folder / 'D' / 'База данных' / 'Учебная папка'
        self.source.mkdir(parents=True)

    def add(self, name='анализ.txt', text=UNNAMED.format(value=1)):
        path = self.source / name
        path.write_text(text, encoding='utf-8')
        stage_files(self.workspace, [self.source])
        self.workspace.import_folder()
        return path

    def document_id(self):
        return self.workspace.review_list()[0]['id']

    def test_the_file_opens_where_the_doctor_keeps_it(self):
        path = self.add()
        location = self.workspace.document_location(self.document_id())
        self.assertEqual((location['path'], location['external']), (path.resolve(), True))
        self.assertEqual((location['folder'], location['filename']), (str(path.resolve().parent), 'анализ.txt'))

    def test_without_the_source_the_copy_in_the_work_folder_opens(self):
        path = self.add()
        path.unlink()
        location = self.workspace.document_location(self.document_id())
        self.assertFalse(location['external'])
        self.assertEqual(location['path'], self.workspace.root / 'Нужно уточнить' / 'Учебная папка' / 'анализ.txt')
        self.assertEqual((location['folder'], location['filename']), (str(Path('Нужно уточнить', 'Учебная папка')), 'анализ.txt'))

    def test_a_different_file_at_the_old_place_is_not_opened(self):
        path = self.add()
        path.write_text('Другой учебный документ, длиннее прежнего.\n' * 3, encoding='utf-8')
        self.assertFalse(self.workspace.document_location(self.document_id())['external'])

    def test_file_put_straight_into_the_queue_opens_its_work_copy(self):
        (self.workspace.root / 'Входящие' / 'прямо.txt').write_text(UNNAMED.format(value=2), encoding='utf-8')
        self.workspace.import_folder()
        location = self.workspace.document_location(self.document_id())
        self.assertEqual(location['path'], self.workspace.root / 'Нужно уточнить' / 'прямо.txt')

    def test_the_source_path_never_leaves_the_registry(self):
        self.add()
        from openpyxl import load_workbook
        book = load_workbook(self.workspace.root / 'Исследования.xlsx')
        cells = [str(cell) for sheet in book for row in sheet.iter_rows(values_only=True) for cell in row if cell]
        self.assertFalse([cell for cell in cells if 'База данных' in cell])

    def test_existing_registry_gains_the_table_in_place(self):
        self.add()
        # «with» on a connection only commits; Windows cannot delete a database left open.
        with closing(sqlite3.connect(self.workspace.database)) as db, db:
            db.execute('DROP TABLE external_sources')
        reopened = Workspace(self.workspace.root)
        location = reopened.document_location(reopened.review_list()[0]['id'])
        self.assertFalse(location['external'])
        with closing(sqlite3.connect(reopened.database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_sources').fetchone()[0], 0)


class WindowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f'Tk display unavailable: {exc}') from exc
        # The window is a transient of the main one. On Windows and Linux a transient of a
        # withdrawn main window is never shown, so its size and focus would mean nothing.
        # The main window stays shown, small, as it is when the program runs.
        cls.root.geometry('200x80+0+0')
        ui.setup(cls.root)
        cls.addClassCleanup(cls.root.destroy)

    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'рабочая')
        self.addCleanup(self.finish_widgets)

    def finish_widgets(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def patient_folder(self, name, count=3, text=UNNAMED):
        folder = self.folder / 'архив' / name
        folder.mkdir(parents=True)
        for index in range(count):
            (folder / f'анализ {index}.txt').write_text(text.format(value=f'{name} {index}'), encoding='utf-8')
        return folder

    def open(self, *folders):
        stage_files(self.workspace, list(folders))
        self.workspace.import_folder()
        window = gui.ReviewWindow(self.root, self.workspace, Mock(), ui.Icons(self.root), actions=ui.WorkspaceActions(self.root))
        self.shown(window.window)
        return window

    def shown(self, widget):
        """Wait until the window is on screen and laid out: only then do its sizes mean anything."""
        deadline = time.monotonic() + 10
        while not (widget.winfo_ismapped() and widget.winfo_width() > 1):
            self.root.update()
            self.assertLess(time.monotonic(), deadline, 'The window was never shown')
            time.sleep(.02)
        self.settle()

    def resized(self, widget, width):
        deadline = time.monotonic() + 10
        while widget.winfo_width() != width:
            self.root.update()
            self.assertLess(time.monotonic(), deadline, f'The window did not take the width {width}')
            time.sleep(.02)
        self.settle();self.settle()

    def press_enter(self, window):
        """Run what Enter is bound to in the window, the way Tk runs it for a key press.

        Tk routes a key only to the widget with keyboard focus, and the operating system gives
        focus only to the active program. A test run in the background or on a build server has
        neither, so the binding on the window is called directly. Delivery of a real key press
        is checked by the interface scenario (ui_smoke.py) in a shown window.
        """
        script = window.window.bind('<Return>')
        self.assertTrue(script, 'Enter is not bound in the window')
        command = re.search(r'\[(\S+) ', script)[1]
        # The fields Tk substitutes for %# %b %f %h %k %s %t %w %x %y %A %E %K %N %W %T %X %Y %D.
        fields = ('0', '0', '0', '0', '36', '0', '0', '0', '0', '0', '\r', '0', 'Return', '65293',
                  str(window.name_entry), '2', '0', '0', '0')
        self.assertEqual(len(fields), len(window.window._subst_format))
        window.window.tk.call(command, *fields)
        self.settle(window)

    def settle(self, window=None):
        deadline = time.monotonic() + 15
        while True:
            self.root.update()
            if window is None or not window.saving:
                return
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)


class WindowTests(WindowBase):
    def test_one_answer_for_a_folder_confirms_all_its_documents(self):
        window = self.open(self.patient_folder('Папка 1'))
        row, = window.list.get_children()
        self.assertEqual(window.list.item(row)['values'][0], 'Папка «Папка 1» · документов: 3')
        window.name.set(TESTOV[0]);window.birth.set('01.01.2000')
        window.confirm();self.settle(window)
        self.assertEqual(self.workspace.review_list(), [])
        self.assertEqual(self.workspace.summary()['patients'], 1)

    def test_documents_of_a_folder_row_can_be_paged(self):
        window = self.open(self.patient_folder('Папка 1'))
        self.assertEqual(window.document_label.cget('text'), 'Документ 1 из 3')
        self.assertIn('анализ 0.txt', window.location_text.get())
        window.next_document.invoke();self.settle()
        self.assertEqual(window.document_label.cget('text'), 'Документ 2 из 3')
        self.assertIn('анализ 1.txt', window.location_text.get())

    def test_location_shows_the_doctor_s_folder_and_opens_it(self):
        folder = self.patient_folder('Папка 1', count=1)
        window = self.open(folder)
        self.assertIn(str(folder.resolve()), window.location_text.get())
        with patch.object(gui, 'open_path') as opened, patch.object(gui, 'reveal_path') as revealed:
            window.original_button.invoke();window.reveal_button.invoke()
        self.assertEqual(opened.call_args.args[0], (folder / 'анализ 0.txt').resolve())
        self.assertEqual(revealed.call_args.args[0], (folder / 'анализ 0.txt').resolve())

    def test_draft_from_the_folder_name_waits_for_a_person(self):
        window = self.open(self.patient_folder('Тестов Тест Тестович 01.01.2000'))
        self.assertEqual((window.name.get(), window.birth.get()), ('Тестов Тест Тестович', '01.01.2000'))
        self.assertIn('из названия папки', window.draft_note.get())
        self.assertEqual(self.workspace.summary()['patients'], 0)
        self.press_enter(window)
        self.assertEqual(self.workspace.summary()['patients'], 1)

    def test_initials_leave_the_fields_empty(self):
        window = self.open(self.patient_folder('Тестов Т.Т. 01.01.2000'))
        self.assertEqual((window.name.get(), window.birth.get(), window.draft_note.get()), ('', '', ''))

    def test_birth_read_from_the_document_wins_over_the_folder(self):
        window = self.open(self.patient_folder('Тестов Тест Тестович 02.02.2002', count=1, text=BIRTH_ONLY))
        self.assertEqual((window.name.get(), window.birth.get()), ('Тестов Тест Тестович', '01.01.2000'))
        self.assertEqual(window.draft_note.get(), 'ФИО взято из названия папки. Проверьте по документу.')

    def test_enter_moves_to_the_next_row(self):
        window = self.open(self.patient_folder('Папка 1'), self.patient_folder('Папка 2'))
        self.assertEqual(len(window.list.get_children()), 2)
        window.name.set(TESTOV[0]);window.birth.set('01.01.2000')
        self.press_enter(window)
        row, = window.list.get_children()
        self.assertEqual(window.selected, row)
        self.assertEqual(window.list.item(row)['values'][0], 'Папка «Папка 2» · документов: 3')

    def test_window_takes_the_screen_and_the_page_most_of_it(self):
        window = self.open(self.patient_folder('Папка 1', count=1))
        self.assertGreaterEqual(window.window.winfo_width(), min(1240, self.root.winfo_screenwidth() - 50))
        self.assertGreater(window.left.winfo_width(), window.right.winfo_width())

    def test_nothing_is_cut_at_the_right_edge_of_a_small_window(self):
        window = self.open(self.patient_folder('Тестова-Примерова Анна Ильинична 11.07.2012 длинное название'))
        for width in (960, 850):
            window.window.state('normal');window.window.geometry(f'{width}x680')
            self.resized(window.window, width)
            edge = window.window.winfo_rootx() + window.window.winfo_width()

            def cut(widget):
                for child in widget.winfo_children():
                    if child.winfo_ismapped() and child.winfo_rootx() + child.winfo_width() > edge + 1:
                        yield child.winfo_class()
                    yield from cut(child)
            self.assertEqual(list(cut(window.window)), [], width)
            self.assertGreater(window.left.winfo_width(), window.right.winfo_width() if width > 900 else 0)


class PreviewTests(WindowBase):
    """The page is drawn at the width of the panel, top first."""

    def rendered(self, window):
        """Wait for the page to be drawn at the panel's present width (a resize redraws it later)."""
        deadline = time.monotonic() + 10
        while (window.preview_photo is None or window.preview_render_job is not None
               or abs(window.preview_photo.width() - (window.canvas.winfo_width() - 24)) >= REDRAW_STEP):
            self.root.update()
            if time.monotonic() > deadline:
                return
            time.sleep(.05)

    def test_page_fills_the_panel_and_follows_it(self):
        from reportlab.pdfgen import canvas
        folder = self.folder / 'архив' / 'Папка'
        folder.mkdir(parents=True)
        page = canvas.Canvas(str(folder / 'бланк.pdf'))
        page.drawString(50, 800, 'Дата выполнения исследования: 05.03.2025')
        page.drawString(50, 780, 'Заключение: учебное.')
        page.save()
        window = self.open(folder)
        self.rendered(window)
        self.assertIsNotNone(window.preview_photo)
        self.fills(window)
        self.assertEqual(window.canvas.yview()[0], 0.0)
        before = window.preview_photo.width()
        window.window.state('normal');window.window.geometry('1000x700');self.resized(window.window, 1000)
        self.rendered(window)
        self.fills(window)
        self.assertGreater(abs(window.preview_photo.width() - before), REDRAW_STEP, 'The page did not follow the panel')

    def fills(self, window):
        """The page takes the panel's width, give or take one redraw step, and needs no sideways scrolling."""
        panel = window.canvas.winfo_width()
        self.assertLess(abs(window.preview_photo.width() - (panel - 24)), REDRAW_STEP)
        self.assertLessEqual(window.preview_photo.width() + 12, panel)

if __name__ == '__main__':
    unittest.main()
