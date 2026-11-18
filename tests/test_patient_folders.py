"""One patient per folder: documents that name nobody follow the folder's patient. Fictional people only."""
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
import unittest
from unittest.mock import Mock

from patient_importer import analysis, gui, ui
from patient_importer.intake import stage_files
from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace

TESTOV = ('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01')
PRIMEROV = ('ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
OTHER_FORM = SAMPLE.replace('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ').replace('01.01.2000', '03.02.2001')


def unnamed(value):
    return ('Дата выполнения исследования: 05.03.2025\n'
            '                         Показатель                       Результат  Единицы\n'
            f'Креатинин                              {value}   мкмоль/л   (40 - 90)\n')


def item(document, folder, person=None, *, ocr=False, **extra):
    names, births = ([person[0]], [person[1]]) if person else ([], [])
    return {'id': document, 'folder': folder,
            'analysis': {'names': names, 'births': births, 'identity_from_ocr': ocr}, **extra}


class FolderOwnerTests(unittest.TestCase):
    def keys(self, items, known=()):
        return {key: value['key'] for key, value in analysis.resolve(items, list(known)).items()}

    def test_one_named_document_is_enough_for_its_folder(self):
        folder = 'Входящие/Архив/Тестов Тест Тестович'
        result = analysis.resolve([item('a', folder, TESTOV), item('b', folder), item('c', folder)], [])
        self.assertEqual({key: value['key'] for key, value in result.items()}, {'a': TESTOV, 'b': TESTOV, 'c': TESTOV})
        self.assertEqual(result['b']['reason'], analysis.FOLDER_REASON)

    def test_folder_name_is_never_read(self):
        # Initials, another surname, a nickname or plain digits: only the documents inside decide.
        for folder in ('Тестов Т.Т. 2017', 'Примеров@', 'пациент 17', '2025', 'Архив/Тестова (девичья)'):
            with self.subTest(folder=folder):
                self.assertEqual(self.keys([item('a', folder, TESTOV), item('b', folder)])['b'], TESTOV)

    def test_windows_separators(self):
        folder = 'Входящие\\Архив\\Тестов Тест Тестович'
        self.assertEqual(self.keys([item('a', folder, TESTOV), item('b', folder)])['b'], TESTOV)

    def test_files_loose_in_the_queue_are_a_pile(self):
        for folder in ('Входящие', '.', ''):
            with self.subTest(folder=folder):
                self.assertIsNone(self.keys([item('a', folder, TESTOV), item('b', folder)])['b'])

    def test_folder_with_two_patients_passes_nothing_and_offers_both(self):
        folder = 'Архив/Общая'
        result = analysis.resolve([item('a', folder, TESTOV), item('b', folder, PRIMEROV), item('c', folder)], [])
        self.assertEqual((result['a']['key'], result['b']['key'], result['c']['key']), (TESTOV, PRIMEROV, None))
        self.assertEqual(result['c']['candidates'], sorted([TESTOV, PRIMEROV]))

    def test_neighbouring_folders_never_share_a_patient(self):
        keys = self.keys([item('a', 'Архив/Первая', TESTOV), item('b', 'Архив/Вторая'), item('loose', 'Архив'),
                          item('year', 'Архив/Первая/2025')])
        self.assertEqual((keys['b'], keys['loose'], keys['year']), (None, None, None))

    def test_document_that_disagrees_goes_to_a_person_with_the_owner_offered(self):
        folder = 'Тестов Тест Тестович'
        known = [{'id': 'P-1', 'name': TESTOV[0], 'birth': TESTOV[1]}]
        other_birth = item('b', folder, ('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-02'))
        other_name = {'id': 'c', 'folder': folder,
                      'analysis': {'names': ['ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ'], 'births': [], 'identity_from_ocr': False}}
        result = analysis.resolve([item('a', folder, TESTOV), other_birth, other_name], known)
        self.assertEqual(result['a']['key'], TESTOV)
        self.assertIsNone(result['b']['key'])
        self.assertIsNone(result['c']['key'])
        self.assertIn(TESTOV, result['b']['candidates'])
        self.assertIn(TESTOV, result['c']['candidates'])

    def test_scan_identity_is_accepted_only_when_it_matches_the_folder(self):
        folder = 'Тестов Тест Тестович'
        keys = self.keys([item('a', folder, TESTOV), item('same', folder, TESTOV, ocr=True),
                          item('typo', folder, ('ТЕСТОВ ТЕСТ ТЕСТОВНЧ', '2000-01-01'), ocr=True)])
        self.assertEqual(keys['same'], TESTOV)
        self.assertIsNone(keys['typo'])

    def test_scans_alone_never_decide_for_a_folder(self):
        folder = 'Тестов Тест Тестович'
        self.assertIsNone(self.keys([item('a', folder, TESTOV, ocr=True), item('b', folder)])['b'])

    def test_a_person_s_answer_for_one_document_covers_the_folder(self):
        known = [{'id': 'P-1', 'name': TESTOV[0], 'birth': TESTOV[1]}]
        folder = 'пациент 17'
        keys = self.keys([item('a', folder, patient_id='P-1', decision='2026-01-01'), item('b', folder),
                          item('scan', folder, PRIMEROV, ocr=True)], known)
        self.assertEqual(keys['b'], TESTOV)
        self.assertIsNone(keys['scan'])
        pile = self.keys([item('a', 'Входящие', patient_id='P-1', decision='2026-01-01'), item('b', 'Входящие')], known)
        self.assertIsNone(pile['b'])


class FolderImportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.root / 'workspace')
        self.archive = self.root / 'Архив'

    def write(self, folder, name, text):
        target = self.archive / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(text, encoding='utf-8')

    def rows(self):
        with self.workspace.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT documents.*, patients.name AS patient FROM documents '
                'LEFT JOIN patients ON patients.id=documents.patient_id ORDER BY folder, filename')]

    def test_added_archive_keeps_patient_folders_and_files_every_document(self):
        self.write('Тестов Тест Тестович', 'ОАК 02.03.2025.txt', SAMPLE)
        self.write('Тестов Тест Тестович', 'биохимия 05.03.2025.txt', unnamed('80'))
        self.write('Тестов Тест Тестович/2024', 'биохимия 05.03.2024.txt', unnamed('81'))
        self.write('Тестов Тест Тестович/2024', 'ОАМ 06.03.2024.txt', SAMPLE.replace('02.03.2025', '06.03.2024'))
        self.write('пациент 17', 'ОАК 02.03.2025.txt', OTHER_FORM)
        self.write('пациент 17', 'биохимия 05.03.2025.txt', unnamed('82'))
        staged = stage_files(self.workspace, [self.archive])
        self.assertEqual(len(set(staged['added'])), 6)
        incoming = self.workspace.root / 'Входящие'
        self.assertTrue((incoming / 'Архив' / 'пациент 17' / 'ОАК 02.03.2025.txt').is_file())
        summary = self.workspace.import_folder()
        self.assertEqual((summary['patients'], summary['review']), (2, 0))
        owners = {(Path(row['folder']).parts[2], row['patient']) for row in self.rows()}
        self.assertEqual(owners, {('Тестов Тест Тестович', TESTOV[0]), ('пациент 17', PRIMEROV[0])})
        self.assertEqual(sum(row['reason'] == analysis.FOLDER_REASON for row in self.rows()), 3)
        # Filed documents leave no empty patient folders behind in the queue.
        self.assertEqual(list(incoming.iterdir()), [])
        self.assertTrue((self.workspace.root / 'Обработано' / 'Архив' / 'Тестов Тест Тестович' / '2024' / 'биохимия 05.03.2024.txt').is_file())

    def test_files_picked_inside_a_patient_folder_keep_that_folder(self):
        self.write('Тестов Тест Тестович', 'ОАК.txt', SAMPLE)
        self.write('Тестов Тест Тестович', 'биохимия.txt', unnamed('80'))
        stage_files(self.workspace, sorted((self.archive / 'Тестов Тест Тестович').iterdir()))
        summary = self.workspace.import_folder()
        self.assertEqual((summary['patients'], summary['review']), (1, 0))

    def test_adding_the_same_folder_twice_does_not_double_the_queue(self):
        self.write('Тестов Тест Тестович', 'ОАК.txt', SAMPLE)
        first = stage_files(self.workspace, [self.archive])
        second = stage_files(self.workspace, [self.archive])
        self.assertEqual(first['added'], second['added'])
        self.assertEqual(len(self.workspace.files()[0]), 1)

    def test_folder_named_like_a_service_folder_is_still_imported(self):
        self.write('Дубли', 'ОАК.txt', SAMPLE)
        stage_files(self.workspace, [self.archive])
        self.assertEqual(self.workspace.import_folder()['documents'], 1)

    def test_one_answer_covers_a_folder_where_no_document_names_the_patient(self):
        for value in ('80', '81', '82'):
            self.write('пациент 17', f'биохимия {value}.txt', unnamed(value))
        self.write('пациент 18', 'биохимия 90.txt', unnamed('90'))
        stage_files(self.workspace, [self.archive])
        self.assertEqual(self.workspace.import_folder()['review'], 4)
        waiting = [row for row in self.workspace.review_list() if row['folder'].endswith('17')]
        self.assertFalse(waiting[0]['folder_settled'])
        summary = self.workspace.assign(waiting[0]['id'], *TESTOV)
        self.assertEqual(summary['review'], 1)
        self.assertEqual({row['patient'] for row in self.rows() if row['folder'].endswith('17')}, {TESTOV[0]})

    def test_documents_loaded_from_a_pile_learn_their_folder_when_the_archive_is_added_again(self):
        incoming = self.workspace.root / 'Входящие'
        (incoming / 'ОАК.txt').write_text(SAMPLE, encoding='utf-8')
        (incoming / 'биохимия.txt').write_text(unnamed('80'), encoding='utf-8')
        self.assertEqual(self.workspace.import_folder()['review'], 1)
        self.write('Тестов Тест Тестович', 'ОАК.txt', SAMPLE)
        self.write('Тестов Тест Тестович', 'биохимия.txt', unnamed('80'))
        stage_files(self.workspace, [self.archive])
        summary = self.workspace.import_folder()
        self.assertEqual((summary['documents'], summary['patients'], summary['review']), (2, 1, 0))

    def test_folder_never_inherits_from_a_neighbour(self):
        self.write('Тестов Тест Тестович', 'ОАК.txt', SAMPLE)
        self.write('Другая папка', 'биохимия.txt', unnamed('80'))
        stage_files(self.workspace, [self.archive])
        self.assertEqual(self.workspace.import_folder()['review'], 1)


class FolderWindowTests(unittest.TestCase):
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
        self.workspace = Workspace(self.folder / 'workspace')
        self.addCleanup(self.finish_widgets)

    def finish_widgets(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def test_folder_name_is_only_a_draft_and_the_folder_is_one_row(self):
        source = self.folder / 'Тестова Анна Тестовна 11.07.2012'
        source.mkdir()
        for value in ('80', '81', '82'):
            (source / f'биохимия {value}.txt').write_text(unnamed(value), encoding='utf-8')
        stage_files(self.workspace, [source])
        self.workspace.import_folder()
        # Matching never read the name: nobody is assigned until a person confirms the draft.
        self.assertEqual(len(self.workspace.review_list()), 3)
        window = gui.ReviewWindow(self.root, self.workspace, Mock(), ui.Icons(self.root), actions=ui.WorkspaceActions(self.root))
        self.root.update()
        self.assertEqual((window.name.get(), window.birth.get()), ('Тестова Анна Тестовна', '11.07.2012'))
        self.assertIn('из названия папки', window.draft_note.get())
        self.assertEqual(len(window.list.get_children()), 1)
        notes = [child.cget('text') for child in window.candidate_area.winfo_children() if child.winfo_class() == 'TLabel']
        self.assertTrue(any('все документы этой папки: 3' in note for note in notes), notes)

if __name__ == '__main__':
    unittest.main()
