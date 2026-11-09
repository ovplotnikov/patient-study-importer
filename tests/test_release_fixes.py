"""Correcting a patient, choosing a folder, keeping backups. All data is fictional."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from patient_importer import analysis, extraction, intake
from patient_importer.storage import KEEP_DAILY_BACKUPS, KEEP_NEWEST_BACKUPS, Workspace

FORM = ('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nДата выполнения исследования: 02.03.2025\n'
        'Общий анализ крови\nПоказатель  Результат  Единицы\nГемоглобин  120  г/л\n')


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'case')

    def add(self, name='a.txt', text=FORM):
        (self.workspace.root / 'Входящие' / name).write_text(text, encoding='utf-8')
        self.workspace.import_folder()

    def document(self):
        with self.workspace.connect() as db:
            return dict(db.execute('SELECT * FROM documents').fetchone())

    def patients(self):
        with self.workspace.connect() as db:
            return {row['id']: (row['name'], row['birth']) for row in db.execute('SELECT * FROM patients')}


class ChangePatientTests(WorkspaceCase):
    def test_wrong_patient_can_be_corrected(self):
        self.add()
        self.workspace.change_patient(self.document()['id'], 'примеров пример примерович', '2001-02-03')
        self.assertEqual(self.patients()[self.document()['patient_id']], ('ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03'))

    def test_previous_owner_is_recorded(self):
        self.add()
        self.workspace.change_patient(self.document()['id'], 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
        with self.workspace.connect() as db:
            action, note = db.execute('SELECT action,note FROM study_history').fetchone()
        self.assertEqual(action, 'patient_changed')
        self.assertIn('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', note)

    def test_workbook_follows_the_change(self):
        self.add()
        self.workspace.change_patient(self.document()['id'], 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
        book = load_workbook(self.workspace.root / 'Исследования.xlsx')
        names = {row[1] for row in book['Пациенты'].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(names, {'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ'})
        self.assertIn('Сверки', book.sheetnames)

    def test_later_import_does_not_undo_the_change(self):
        self.add()
        self.workspace.change_patient(self.document()['id'], 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
        self.add('b.txt', 'Пациент: ДРУГОВ ДРУГ ДРУГОВИЧ\nДата рождения: 05.05.2005\nЗаключение: учебное.')
        with self.workspace.connect() as db:
            owner = db.execute("SELECT patient_id FROM documents WHERE filename='a.txt'").fetchone()[0]
        self.assertEqual(self.patients()[owner][0], 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ')

    def test_same_patient_is_refused(self):
        self.add()
        with self.assertRaises(ValueError):
            self.workspace.change_patient(self.document()['id'], 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01')

    def test_bad_date_is_refused(self):
        self.add()
        with self.assertRaises(ValueError):
            self.workspace.change_patient(self.document()['id'], 'ПРИМЕРОВ ПРИМЕР', 'вчера')

    def test_open_workbook_stops_before_any_change(self):
        self.add()
        before = self.document()['patient_id']
        (self.workspace.root / '~$Исследования.xlsx').write_text('lock')
        with self.assertRaises(RuntimeError):
            self.workspace.change_patient(self.document()['id'], 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
        self.assertEqual(self.document()['patient_id'], before)

    def test_duplicate_is_refused(self):
        self.add()
        document = self.document()
        parsed = json.loads(document['analysis'])
        parsed['duplicate_of'] = 'S-учебный'
        with self.workspace.connect() as db, db:
            db.execute('UPDATE documents SET analysis=?', (json.dumps(parsed, ensure_ascii=False),))
        with self.assertRaises(ValueError):
            self.workspace.change_patient(document['id'], 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')


class ManualNameTests(WorkspaceCase):
    def waiting(self):
        self.add(text='Дата рождения: 01.01.2000\nЗаключение: учебный пример')
        return self.workspace.review_list()[0]['id']

    def test_name_without_patronymic_is_accepted(self):
        self.workspace.assign(self.waiting(), 'Тестов Тест', '2000-01-01')
        self.assertEqual(list(self.patients().values()), [('ТЕСТОВ ТЕСТ', '2000-01-01')])

    def test_single_word_is_refused(self):
        with self.assertRaises(ValueError):
            self.workspace.assign(self.waiting(), 'Тестов', '2000-01-01')


class NamesakeTests(unittest.TestCase):
    def item(self, name, birth, document):
        return {'id': document, 'folder': 'Входящие',
                'analysis': {'names': [name], 'births': [birth], 'identity_from_ocr': False}}

    def test_known_name_with_a_new_birth_date_goes_to_a_person(self):
        known = [{'name': 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'birth': '2000-01-01'}]
        result = analysis.resolve([self.item('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-02', 'a')], known)['a']
        self.assertIsNone(result['key'])
        self.assertIn(('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01'), result['candidates'])

    def test_known_pair_is_still_matched(self):
        known = [{'name': 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'birth': '2000-01-01'}]
        result = analysis.resolve([self.item('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01', 'a')], known)['a']
        self.assertEqual(result['key'], ('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01'))

    def test_confirmed_namesakes_stay_apart(self):
        known = [{'name': 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'birth': '2000-01-01'}, {'name': 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'birth': '2005-05-05'}]
        result = analysis.resolve([self.item('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2005-05-05', 'a')], known)['a']
        self.assertEqual(result['key'], ('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2005-05-05'))

    def test_new_patient_without_a_namesake_is_created(self):
        self.assertTrue(analysis.resolve([self.item('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01', 'a')], [])['a']['key'])


class FolderTests(WorkspaceCase):
    def test_documents_in_a_plain_folder_are_counted(self):
        folder = self.folder / 'Документы'
        (folder / 'вложенная').mkdir(parents=True)
        (folder / 'договор.pdf').write_bytes(b'%PDF-1.4')
        (folder / 'вложенная' / 'заметки.txt').write_text('учебное', encoding='utf-8')
        self.assertEqual(intake.documents_in_folder(folder), 2)

    def test_empty_folder_is_silent(self):
        folder = self.folder / 'Пустая'
        folder.mkdir()
        self.assertEqual(intake.documents_in_folder(folder), 0)

    def test_existing_workspace_is_silent(self):
        self.add()
        self.assertEqual(intake.documents_in_folder(self.workspace.root), 0)

    def test_external_folder_named_like_a_service_folder_is_read(self):
        folder = self.folder / 'внешняя' / 'Обработано'
        folder.mkdir(parents=True)
        (folder / 'a.txt').write_text(FORM, encoding='utf-8')
        self.assertEqual(len(intake.stage_files(self.workspace, [self.folder / 'внешняя'])['added']), 1)

    def test_service_folders_of_the_workspace_stay_excluded(self):
        inner = self.workspace.root / 'Подпапка' / 'Обработано'
        inner.mkdir(parents=True)
        (inner / 'a.txt').write_text(FORM, encoding='utf-8')
        self.assertEqual(intake.stage_files(self.workspace, [self.workspace.root / 'Подпапка'])['added'], [])


class BackupTests(WorkspaceCase):
    def test_a_long_session_keeps_the_last_copy_of_earlier_days(self):
        self.add()
        folder = self.workspace.root / 'Резервные копии'
        for path in folder.glob('База_*.sqlite'):
            path.unlink()
        for hour in range(3):
            (folder / f'База_20200101_0{hour}0000_000000.sqlite').write_bytes(b'x')
        with self.workspace.connect() as db:
            for _ in range(KEEP_NEWEST_BACKUPS + 5):
                self.workspace.backup(db)
        copies = sorted(path.name for path in folder.glob('База_*.sqlite'))
        self.assertEqual(copies[0], 'База_20200101_020000_000000.sqlite')
        self.assertEqual(len(copies), KEEP_NEWEST_BACKUPS + 1)

    def test_old_days_beyond_the_limit_are_removed(self):
        self.add()
        folder = self.workspace.root / 'Резервные копии'
        for day in range(1, KEEP_DAILY_BACKUPS + 6):
            (folder / f'База_202001{day:02d}_000000_000000.sqlite').write_bytes(b'x')
        with self.workspace.connect() as db:
            for _ in range(KEEP_NEWEST_BACKUPS):
                self.workspace.backup(db)
        self.assertFalse((folder / 'База_20200101_000000_000000.sqlite').exists())


class TextEncodingTests(unittest.TestCase):
    def read(self, data):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'a.txt'
            path.write_bytes(data)
            return extraction.extract(path)['pages'][0]

    def test_even_length_windows_text_is_not_utf16(self):
        text = 'Учебный текст бланка, 12 символов'
        self.assertEqual(len(text.encode('cp1251')) % 2, 1)
        self.assertEqual(self.read((text + 'я').encode('cp1251')), text + 'я')

    def test_utf16_with_a_mark_is_read(self):
        self.assertEqual(self.read('Учебный'.encode('utf-16')), 'Учебный')

    def test_utf8_is_read(self):
        self.assertEqual(self.read('Учебный'.encode('utf-8')), 'Учебный')


if __name__ == '__main__':
    unittest.main()
