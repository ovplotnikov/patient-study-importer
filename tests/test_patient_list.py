"""Confirming patients in bulk. All data is fictional."""
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from patient_importer import gui, review_queue as queue, ui
from patient_importer.storage import Workspace

TESTOV = ('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01')
OTHER = ('ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
FULL = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nЗаключение: учебное.\n'
NAME_ONLY = 'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nЗаключение: учебное.\n'


def record(names=(), births=(), candidates=(), **other):
    return {'id': 'S', 'names': list(names), 'births': list(births), 'candidates': [list(c) for c in candidates], **other}


class SuggestionTests(unittest.TestCase):
    def test_name_found_in_one_known_patient(self):
        self.assertEqual(queue.patient_suggestion(record([TESTOV[0]], [], [TESTOV])), (TESTOV, queue.NAME_BASIS))

    def test_birth_date_found_in_one_known_patient(self):
        self.assertEqual(queue.patient_suggestion(record([], [TESTOV[1]], [TESTOV])), (TESTOV, queue.BIRTH_BASIS))

    def test_folder_suggestion_is_marked_as_weak(self):
        self.assertEqual(queue.patient_suggestion(record([], [], [TESTOV])), (TESTOV, queue.FOLDER_BASIS))

    def test_namesakes_are_left_to_a_person(self):
        self.assertIsNone(queue.patient_suggestion(record([TESTOV[0]], [], [TESTOV, (TESTOV[0], '2005-05-05')])))

    def test_two_folder_candidates_are_left_to_a_person(self):
        self.assertIsNone(queue.patient_suggestion(record([], [], [TESTOV, OTHER])))

    def test_name_that_matches_nobody_is_left_to_a_person(self):
        self.assertIsNone(queue.patient_suggestion(record(['ДРУГОВ ДРУГ ДРУГОВИЧ'], [], [TESTOV])))

    def test_full_identity_in_review_means_a_conflict(self):
        self.assertIsNone(queue.patient_suggestion(record([TESTOV[0]], [TESTOV[1]], [TESTOV])))

    def test_two_names_are_left_to_a_person(self):
        self.assertIsNone(queue.patient_suggestion(record([TESTOV[0], OTHER[0]], [], [TESTOV])))

    def test_identity_from_a_scan_is_left_to_a_person(self):
        self.assertIsNone(queue.patient_suggestion(record([TESTOV[0]], [], [TESTOV], identity_from_ocr=True)))

    def test_changed_identity_is_left_to_a_person(self):
        self.assertIsNone(queue.patient_suggestion(record([TESTOV[0]], [], [TESTOV], reason='После повторного чтения')))

    def test_groups_join_documents_of_a_patient_by_basis(self):
        groups = queue.patient_groups([dict(record([TESTOV[0]], [], [TESTOV]), id='a'), dict(record([TESTOV[0]], [], [TESTOV]), id='b'),
                                       dict(record([], [], [TESTOV]), id='c'), dict(record([], [], []), id='d')])
        self.assertEqual([(group['basis'], group['documents']) for group in groups],
                         [(queue.NAME_BASIS, ['a', 'b']), (queue.FOLDER_BASIS, ['c'])])


class AssignManyTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'case')

    def add(self, name, text):
        (self.workspace.root / 'Входящие' / name).write_text(text, encoding='utf-8')

    def waiting(self):
        return [item['id'] for item in self.workspace.review_list()]

    def imported(self):
        self.add('full.txt', FULL)
        for index in range(3):
            self.add(f'n{index}.txt', NAME_ONLY + f'Учебная строка {index}.\n')
        self.workspace.import_folder()
        return self.waiting()

    def test_candidates_come_from_a_fully_named_document(self):
        items = self.workspace.review_list()
        self.assertEqual(items, [])
        self.imported()
        self.assertEqual({tuple(c) for item in self.workspace.review_list() for c in item['candidates']}, {TESTOV})

    def test_all_are_confirmed_at_once(self):
        ids = self.imported()
        result = self.workspace.assign_many([(item, *TESTOV) for item in ids])
        self.assertEqual(result['assigned'], 3)
        self.assertEqual(self.waiting(), [])
        self.assertEqual(result['patients'], 1)

    def test_table_is_rebuilt_once(self):
        ids = self.imported()
        with patch('patient_importer.workbook.export_workbook') as export:
            self.workspace.assign_many([(item, *TESTOV) for item in ids])
        export.assert_called_once()

    def test_document_already_confirmed_is_not_changed(self):
        ids = self.imported()
        self.workspace.assign(ids[0], *OTHER)
        result = self.workspace.assign_many([(item, *TESTOV) for item in ids])
        self.assertEqual(result['assigned'], 2)
        with self.workspace.connect() as db:
            owner = db.execute('SELECT name FROM patients JOIN documents ON patient_id=patients.id WHERE documents.id=?', (ids[0],)).fetchone()[0]
        self.assertEqual(owner, OTHER[0])

    def test_bad_entry_stops_everything(self):
        ids = self.imported()
        with self.assertRaises(ValueError):
            self.workspace.assign_many([(ids[0], *TESTOV), (ids[1], 'Тестов', '2000-01-01')])
        self.assertEqual(len(self.waiting()), 3)

    def test_open_workbook_stops_before_any_change(self):
        ids = self.imported()
        (self.workspace.root / '~$Исследования.xlsx').write_text('lock')
        with self.assertRaises(RuntimeError):
            self.workspace.assign_many([(item, *TESTOV) for item in ids])
        self.assertEqual(len(self.waiting()), 3)


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
        self.addCleanup(self.finish_widgets)

    def finish_widgets(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def open(self, count=2):
        (self.workspace.root / 'Входящие' / 'full.txt').write_text(FULL, encoding='utf-8')
        for index in range(count):
            (self.workspace.root / 'Входящие' / f'n{index}.txt').write_text(NAME_ONLY + f'Строка {index}.\n', encoding='utf-8')
        self.workspace.import_folder()
        window = gui.ReviewWindow(self.root, self.workspace, Mock(), ui.Icons(self.root), actions=ui.WorkspaceActions(self.root))
        self.root.update()
        return window

    def test_missing_birth_date_is_filled_in(self):
        window = self.open()
        self.assertEqual((window.name.get(), window.birth.get()), (TESTOV[0], '01.01.2000'))

    def test_list_button_offers_every_found_document(self):
        window = self.open()
        self.assertIn('2', window.bulk_button.cget('text'))

    def test_single_document_needs_no_list(self):
        window = self.open(1)
        self.assertFalse(window.bulk_button.winfo_manager())

    def test_weak_suggestions_start_unselected(self):
        groups = [{'name': TESTOV[0], 'birth': TESTOV[1], 'basis': queue.NAME_BASIS, 'documents': ['a']},
                  {'name': OTHER[0], 'birth': OTHER[1], 'basis': queue.FOLDER_BASIS, 'documents': ['b']}]
        dialog = gui.BulkPatientDialog(self.root, groups, Mock(return_value=True))
        self.assertEqual([group['name'] for group in dialog.chosen()], [TESTOV[0]])
        dialog.list.selection_set(('0', '1'))
        self.root.update()
        dialog.save()
        self.assertEqual(dialog.on_save.call_args.args[0], [('a', *TESTOV), ('b', *OTHER)])


if __name__ == '__main__':
    unittest.main()
