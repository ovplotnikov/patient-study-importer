from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from patient_importer import analysis, workbook
from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace


def extraction(*pages, **extra):
    return {'format': 'TXT', 'pages': list(pages), 'ocr_pages': [], **extra}


class AnalysisRegressionTests(unittest.TestCase):
    def test_lab_dates_belong_to_each_text_fragment_even_with_same_page_number(self):
        second = SAMPLE.replace('02.03.2025', '03.03.2025')
        undated = SAMPLE.replace('Дата выполнения исследования: 02.03.2025\n', '')
        conflicting = SAMPLE + '\nДата исследования: 04.03.2025\n'
        result = analysis.analyze(
            extraction(SAMPLE, second, undated, conflicting, page_numbers=[1, 1, 2, 3]), 'учебный.txt')
        self.assertIsNone(result['date'])
        self.assertEqual(len(result['observations']), 8)
        self.assertEqual([(row['page'], row['page_index'], row['date']) for row in result['observations']], [
            (1, 1, '2025-03-02'), (1, 1, '2025-03-02'),
            (1, 2, '2025-03-03'), (1, 2, '2025-03-03'),
            (2, 3, None), (2, 3, None), (3, 4, None), (3, 4, None),
        ])
        fallback = analysis.analyze(extraction(SAMPLE, undated), 'учебный.txt')
        self.assertEqual(len(fallback['observations']), 4)
        self.assertEqual({(row['date'], row['date_source']) for row in fallback['observations']},
                         {('2025-03-02', 'Дата документа')})

    def test_candidates_are_the_sorted_union_without_ocr_auto_assignment(self):
        exact = ('ТЕСТОВА ТЕСТ ТЕСТОВИЧ', '2000-01-01')
        same_name = (exact[0], '2001-01-01')
        same_birth = ('ТЕСТОВБ ТЕСТ ТЕСТОВИЧ', exact[1])
        unrelated = ('ТЕСТОВВ ТЕСТ ТЕСТОВИЧ', '2002-01-01')
        patients = [{'name': name, 'birth': birth} for name, birth in
                    (unrelated, same_birth, exact, same_name, exact)]
        records = [
            {'id': key, 'folder': '', 'analysis': {
                'names': [exact[0]], 'births': [exact[1]], 'identity_from_ocr': ocr,
            }}
            for key, ocr in (('текст', False), ('скан', True))
        ]
        result = analysis.resolve(records, patients)
        self.assertEqual(result['текст']['key'], exact)
        self.assertEqual(result['текст']['candidates'], [])
        self.assertIsNone(result['скан']['key'])
        self.assertEqual(result['скан']['candidates'], sorted((exact, same_name, same_birth)))

    def test_colliding_compact_names_remain_ambiguous(self):
        names = ['ФИКТИВ ОВА ТЕСТОВИЧ', 'ФИК ТИВОВА ТЕСТОВИЧ']
        birth = '2000-01-01'
        records = [{'id': 'учебный', 'folder': '', 'analysis': {
            'names': names[:1], 'births': [birth], 'identity_from_ocr': False,
        }}]
        result = analysis.resolve(records, [{'name': name, 'birth': birth} for name in names])['учебный']
        self.assertIsNone(result['key'])
        self.assertEqual(result['candidates'], sorted((name, birth) for name in names))


class StorageRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Workspace(Path(self.temporary.name))

    def insert_document(self, db, document_id, parsed, extracted, patient_id=None):
        digest = sha256(document_id.encode()).hexdigest()
        db.execute('''INSERT INTO documents(id,hash,filename,folder,original,extracted,analysis,patient_id,created)
                      VALUES (?,?,?,?,?,?,?,?,?)''',
                   (document_id, digest, document_id + '.txt', '',
                    str(self.workspace.originals / (digest + '.txt')), json.dumps(extracted), json.dumps(parsed),
                    patient_id, '2025-03-02T12:00:00+00:00'))

    def test_summary_keeps_legacy_quality_and_live_arrivals(self):
        extracted = extraction(SAMPLE)
        ready = analysis.analyze(extracted, 'учебный.txt')
        legacy = deepcopy(ready)
        del legacy['quality']
        legacy_source = {**extracted, 'extraction_issues': [{'code': 'PDF_TEXT_UNREADABLE'}]}
        confirmed = deepcopy(ready)
        confirmed['quality'] = {'confirmed': True, 'incomplete': True, 'issues': [{'code': 'OCR_EMPTY'}]}
        duplicate = {**ready, 'duplicate_of': 'готово', 'quality': {'issues': [{'code': 'OCR_EMPTY'}]}}
        target = self.workspace.root / 'Исследования.xlsx'
        with self.workspace.connect() as db:
            db.execute('INSERT INTO patients VALUES (?,?,?)', ('учебный-пациент', 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01'))
            self.insert_document(db, 'готово', ready, extracted, 'учебный-пациент')
            self.insert_document(db, 'старый', legacy, legacy_source)
            self.insert_document(db, 'сверено', confirmed, extracted, 'учебный-пациент')
            self.insert_document(db, 'повтор', duplicate, extracted)
            db.execute('INSERT INTO arrivals(source,hash,category,status,created) VALUES (?,?,?,?,?)',
                       ('копия.txt', 'учебный-хэш', 'Дубли', 'pending', '2025-03-02'))
            db.execute("INSERT INTO meta VALUES ('exported_revision',?)", (self.workspace._revision(db),))
        target.touch()
        self.assertEqual(self.workspace.summary(), {
            'pending_export': False, 'documents': 3, 'patients': 1, 'review': 1,
            'quality_review': 1, 'total_review': 1, 'incomplete': 1, 'reparse': 0, 'duplicates': 1,
            'errors': 0, 'pending_moves': 1, 'workbook': str(target),
        })
        self.assertTrue(self.workspace.has_serious_review_issue())
        with self.workspace.connect() as db:
            db.execute("UPDATE arrivals SET category='Ошибки', status='done'")
        updated = self.workspace.summary()
        self.assertEqual((updated['duplicates'], updated['errors'], updated['pending_moves']), (0, 1, 0))
        self.assertFalse(updated['pending_export'])
        target.unlink()
        self.assertTrue(self.workspace.summary()['pending_export'])
        with self.workspace.connect() as db:
            db.execute("DELETE FROM documents WHERE id='старый'")
            self.workspace.touch(db)
        self.assertFalse(self.workspace.has_serious_review_issue())

    def test_bulk_status_records_match_single_reads_across_chunks(self):
        extracted = extraction(SAMPLE)
        parsed = analysis.analyze(extracted, 'учебный.txt')
        with self.workspace.connect() as db:
            self.insert_document(db, 'первый', parsed, extracted)
            self.insert_document(db, 'последний', parsed, extracted)
        ids = ['первый', *(f'отсутствует-{index}' for index in range(503)), 'последний', 'первый']
        actual = self.workspace.documents_by_ids(iter(ids))
        expected = {key: {field: value for field, value in self.workspace.document(key).items()
                          if field in {'id', 'patient_id', 'analysis'}}
                    for key in ('первый', 'последний')}
        self.assertEqual(actual, expected)
        self.assertEqual(self.workspace.documents_by_ids([]), {})

    def test_index_is_created_on_reopen_without_changing_existing_registry(self):
        extracted = extraction(SAMPLE)
        parsed = analysis.analyze(extracted, 'учебный.txt')
        with self.workspace.connect() as db:
            db.execute('DROP INDEX arrivals_document_index')
            self.insert_document(db, 'учебный', parsed, extracted)
            db.execute('''INSERT INTO arrivals(source,hash,document_id,category,status,created)
                          VALUES (?,?,?,?,?,?)''',
                       ('учебный.txt', 'учебный-хэш', 'учебный', 'Обработано', 'done', '2025-03-02'))
        original = self.workspace.document('учебный')
        reopened = Workspace(self.workspace.root)
        self.assertEqual(reopened.document('учебный'), original)
        with reopened.connect() as db:
            self.assertEqual([row['name'] for row in db.execute('PRAGMA index_info(arrivals_document_index)')],
                             ['document_id'])
            self.assertEqual(db.execute('SELECT document_id FROM arrivals').fetchone()[0], 'учебный')
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_export_keeps_variant_originals_notes_formulas_and_styles(self):
        incoming = self.workspace.root / 'Входящие'
        (incoming / 'учебный.txt').write_text(SAMPLE, encoding='utf-8')
        first = self.workspace.import_folder()
        document = self.workspace.document(first['batch'][0]['document_id'])
        target = self.workspace.root / 'Исследования.xlsx'
        book = load_workbook(target)
        try:
            sheet = book['Показатели']
            notes = next(cell.column for cell in sheet[1] if cell.value == 'Заметки')
            sheet.cell(2, notes, 'Синтетическая заметка')
            sheet.cell(1, notes + 1, 'Моя колонка')
            sheet.cell(2, notes + 1, '=1+2')
            book.create_sheet('Мои записи')['A1'] = 'Синтетический лист'
            book.save(target)
        finally:
            book.close()
        copy = SAMPLE.replace('Пациент: ', 'Пациент:  ')
        source = incoming / 'вариант.txt'
        source.write_text(copy, encoding='utf-8')
        digest = sha256(source.read_bytes()).hexdigest()
        second = self.workspace.import_folder()
        self.assertEqual((second['documents'], second['duplicates']), (1, 1))
        original = Path(document['original'])
        variant = next(self.workspace.originals.glob(digest + '.*'))
        book = load_workbook(target)
        try:
            sheet = book['Показатели']
            self.assertEqual(sheet.cell(2, notes).value, 'Синтетическая заметка')
            self.assertEqual(sheet.cell(2, notes + 1).value, '=1+2')
            self.assertEqual(sheet.cell(2, notes + 1).data_type, 'f')
            self.assertEqual(book['Мои записи']['A1'].value, 'Синтетический лист')
            self.assertTrue(sheet['A1'].font.bold)
            self.assertFalse(sheet['A2'].alignment.wrap_text)
            self.assertTrue(sheet['D2'].alignment.wrap_text)
            self.assertEqual(sheet['E2'].number_format, 'dd.mm.yyyy')
            sources = {row[3].value: row[5] for row in book['Источники'].iter_rows(min_row=2)}
            for name, path in (('учебный.txt', original), ('вариант.txt', variant)):
                relative = path.relative_to(self.workspace.root).as_posix()
                self.assertEqual(sources[name].value, relative)
                self.assertEqual(sources[name].hyperlink.target, relative)
        finally:
            book.close()
        variant.unlink()
        with self.workspace.connect() as db:
            sources = {row[3]: row[5] for row in workbook.rows_for(self.workspace, db)['Источники']}
        self.assertEqual(sources['вариант.txt'], original.relative_to(self.workspace.root).as_posix())


if __name__ == '__main__':
    unittest.main()
