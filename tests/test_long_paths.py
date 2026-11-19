"""Long paths never cost a document its patient folder. Fictional people only.

Matching reads the folder a file lies in directly, so the queue must keep it: files of one source
folder share one queue folder, files of different source folders never do.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from patient_importer import analysis
from patient_importer.intake import FILE_ROOM, STAGED_PATH_LIMIT, safe_folder, stage_files, staging_location
from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace

OTHER_FORM = SAMPLE.replace('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ').replace('01.01.2000', '03.02.2001')
LONG = 'Учебная папка с очень длинным названием для проверки укорачивания пути '


def unnamed(value):
    return ('Дата выполнения исследования: 05.03.2025\n'
            '                         Показатель                       Результат  Единицы\n'
            f'Креатинин                              {value}   мкмоль/л   (40 - 90)\n')


class LocationTests(unittest.TestCase):
    incoming = Path('C:/' + 'Рабочая папка/' * 6 + 'Входящие')

    def test_one_folder_one_place_whatever_the_file_names(self):
        folders = ('Архив', LONG * 2, 'Тестов Тест Тестович')
        places = {staging_location(self.incoming, folders, name)[0] for name in ('a.pdf', 'б' * 150 + '.pdf', 'в.txt')}
        self.assertEqual(len(places), 1)
        folder, = places
        self.assertLessEqual(len(str(folder)) + 1 + FILE_ROOM, STAGED_PATH_LIMIT)
        self.assertIn('Тестов Тест Тестов', folder.name)

    def test_different_folders_never_share_a_place(self):
        first = staging_location(self.incoming, ('Архив', LONG * 2, 'Тестов Тест Тестович'), 'a.pdf')[0]
        second = staging_location(self.incoming, ('Архив', LONG * 2, 'Тестов Тест Тестович 2'), 'a.pdf')[0]
        subfolder = staging_location(self.incoming, ('Архив', LONG * 2, 'Тестов Тест Тестович', '2024'), 'a.pdf')[0]
        self.assertEqual(len({first, second, subfolder}), 3)

    def test_names_with_the_same_start_stay_apart(self):
        first, second = safe_folder(LONG + 'Тестов'), safe_folder(LONG + 'Примеров')
        self.assertNotEqual(first, second)
        self.assertLessEqual(max(len(first), len(second)), 65)
        self.assertEqual(safe_folder('Короткое название'), 'Короткое название')

    def test_a_long_file_name_is_shortened_not_the_folder(self):
        folder, name = staging_location(Path('C:/Данные/Входящие'), ('Тестов Тест Тестович',), 'б' * 300 + '.pdf')
        self.assertEqual(folder, Path('C:/Данные/Входящие/Тестов Тест Тестович'))
        self.assertTrue(name.endswith('.pdf'))
        self.assertLessEqual(len(str(folder / name)), STAGED_PATH_LIMIT)


class LongRootTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        root = self.folder.joinpath(*(['Данные клиники'] * 8))
        self.assertGreater(len(str(root)), 130)
        self.workspace = Workspace(root)
        self.archive = self.folder / 'Архив'

    def patient(self, name, form, values):
        folder = self.archive / name
        folder.mkdir(parents=True)
        (folder / 'анализ с ФИО.txt').write_text(form, encoding='utf-8')
        for value in values:
            (folder / f'биохимия {value}.txt').write_text(unnamed(value), encoding='utf-8')
        return folder

    def owners(self):
        with self.workspace.connect() as db:
            rows = db.execute('''SELECT documents.folder, patients.name FROM documents
                                 LEFT JOIN patients ON patients.id=documents.patient_id''').fetchall()
        return rows

    def test_long_patient_folders_stay_apart_and_keep_their_patients(self):
        self.patient(LONG + 'Тестов', SAMPLE, ('80', '81'))
        self.patient(LONG + 'Примеров', OTHER_FORM, ('82', '83'))
        stage_files(self.workspace, [self.archive])
        self.workspace.import_folder()
        rows = self.owners()
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(name for _, name in rows), 'Every document gets the patient of its own folder')
        by_folder = {}
        for folder, name in rows:
            by_folder.setdefault(folder, set()).add(name)
        self.assertEqual(sorted(len(names) for names in by_folder.values()), [1, 1])
        self.assertTrue(all(analysis.folder_parts(folder) for folder in by_folder))

    def test_adding_again_finds_the_same_folder(self):
        folder = self.patient(LONG * 2 + 'Тестов', SAMPLE, ('80', '81'))
        first = stage_files(self.workspace, [self.archive])
        second = stage_files(self.workspace, [self.archive])
        self.assertEqual(sorted(first['added']), sorted(second['added']))
        self.assertEqual(len({Path(path).parent for path in first['added']}), 1)
        staged, = {Path(path).parent for path in first['added']}
        self.assertEqual(len(list(staged.iterdir())), len(list(folder.iterdir())))

    def test_a_very_long_file_name_keeps_its_folder(self):
        folder = self.patient(LONG + 'Тестов', SAMPLE, ('80',))
        (folder / ('Длинное имя файла ' * 6 + '.txt')).write_text(unnamed('84'), encoding='utf-8')
        stage_files(self.workspace, [self.archive])
        self.workspace.import_folder()
        rows = self.owners()
        self.assertEqual(len({folder for folder, _ in rows}), 1)
        self.assertTrue(all(name == 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ' for _, name in rows))

    def test_a_document_from_a_pile_learns_its_long_folder_and_is_filed_there(self):
        folder = self.patient(LONG * 2 + 'Тестов', SAMPLE, ('80',))
        pile = self.workspace.root / 'Входящие' / 'биохимия 80.txt'
        pile.write_text(unnamed('80'), encoding='utf-8')
        self.workspace.import_folder()
        self.assertIn(('Входящие', None), [tuple(row) for row in self.owners()])
        stage_files(self.workspace, [self.archive])
        self.workspace.import_folder()
        rows = self.owners()
        self.assertEqual(len({folder for folder, _ in rows}), 1)
        self.assertTrue(all(name == 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ' for _, name in rows))
        filed = [path for path in (self.workspace.root / 'Обработано').rglob('*.txt')]
        self.assertEqual({path.parent.name for path in filed if path.name != 'биохимия 80.txt'},
                         {Path(rows[0][0]).name})
        self.assertFalse(any((self.workspace.root / 'Входящие').rglob('*.txt')))
        self.assertTrue(folder.is_dir())


if __name__ == '__main__':
    unittest.main()
