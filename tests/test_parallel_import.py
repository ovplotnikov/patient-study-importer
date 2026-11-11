"""Reading files ahead in worker processes during import. All data is fictional."""
import json
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from patient_importer import parallel, reading
from patient_importer.storage import Workspace

FORM = ('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nДата выполнения исследования: {day}.03.2025\n'
        'Общий анализ крови\nПоказатель  Результат  Единицы\nГемоглобин  1{day}  г/л\n')


class ParallelImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))

    def workspace(self, name):
        workspace = Workspace(self.folder / name)
        incoming = workspace.root / 'Входящие'
        for day in range(10, 22):
            (incoming / f'анализ {day}.txt').write_text(FORM.format(day=day), encoding='utf-8')
        (incoming / 'копия.txt').write_text(FORM.format(day=10), encoding='utf-8')
        (incoming / 'пустой.txt').write_text('   \n', encoding='utf-8')
        (incoming / 'сломанный.docx').write_bytes(b'not a zip')
        return workspace

    def stored(self, workspace):
        with workspace.connect() as db:
            documents = {row['filename']: (json.loads(row['extracted']), self.readable(row['analysis']))
                         for row in db.execute('SELECT * FROM documents')}
            arrivals = sorted((Path(row['source']).name, row['category'], row['error'])
                              for row in db.execute('SELECT * FROM arrivals'))
        return documents, arrivals

    @staticmethod
    def readable(text):
        parsed = json.loads(text)
        for row in parsed['observations']:
            row.pop('result_id', None)
        return parsed

    def test_workers_store_what_this_process_stores(self):
        with patch.object(parallel, 'MINIMUM_FOR_POOL', 10 ** 6):
            here = self.workspace('here').import_folder()
        pooled_workspace = self.workspace('pooled')
        with patch.object(parallel, 'MINIMUM_FOR_POOL', 1), patch.object(parallel, 'worker_count', return_value=2):
            pooled = pooled_workspace.import_folder()
        for key in ('documents', 'duplicates', 'errors', 'patients', 'quality_review'):
            self.assertEqual(pooled[key], here[key], key)
        self.assertEqual(self.stored(pooled_workspace), self.stored(Workspace(self.folder / 'here')))
        self.assertEqual([item['status'] for item in pooled['batch']], [item['status'] for item in here['batch']])

    def test_reading_really_leaves_the_main_process(self):
        workspace = self.workspace('pooled')
        seen = []
        original = parallel.ordered_map

        def spy(function, jobs, **options):
            seen.append((function, options['workers']))
            return original(function, jobs, **{**options, 'minimum': 1, 'workers': 2})

        with patch.object(parallel, 'ordered_map', side_effect=spy):
            result = workspace.import_folder()
        self.assertEqual(seen[0][0], reading.read_original)
        self.assertEqual(result['documents'], 12)

    def test_a_failure_in_a_worker_reads_like_one_here(self):
        workspace = self.workspace('pooled')
        with patch.object(parallel, 'MINIMUM_FOR_POOL', 1), patch.object(parallel, 'worker_count', return_value=2):
            result = workspace.import_folder()
        errors = {item['file']: item['reason'] for item in result['batch'] if item['status'] == 'Ошибка'}
        self.assertEqual(set(errors), {'пустой.txt', 'сломанный.docx'})
        self.assertEqual(errors['пустой.txt'], 'В файле не обнаружен читаемый текст')

    def test_a_file_changed_after_reading_is_read_again(self):
        workspace = Workspace(self.folder / 'case')
        path = workspace.root / 'Входящие' / 'анализ.txt'
        path.write_text(FORM.format(day=11), encoding='utf-8')
        stale = {'digest': '0' * 64, 'original': path,
                 'reading': {'extracted': {'pages': ['чужой текст'], 'ocr_pages': []}, 'parsed': {}}}
        with workspace.connect() as db, db:
            workspace.add_file(db, path, prepared=stale)
            stored = json.loads(db.execute('SELECT extracted FROM documents').fetchone()[0])
        self.assertIn('ТЕСТОВ', stored['pages'][0])

    def test_stop_keeps_what_was_stored_and_ends_the_workers(self):
        workspace = self.workspace('pooled')
        cancel = threading.Event()

        def progress(**state):
            if state.get('done') == 3:
                cancel.set()

        with patch.object(parallel, 'MINIMUM_FOR_POOL', 1), patch.object(parallel, 'worker_count', return_value=2):
            result = workspace.import_folder(progress=progress, cancel=cancel)
        self.assertEqual(len(result['batch']), 3)
        self.assertEqual(result['remaining'], 12)
        self.assertEqual(multiprocessing.active_children(), [])
        resumed = workspace.import_folder()
        self.assertEqual(resumed['documents'], 12)


class WorkerCountTests(unittest.TestCase):
    def test_free_memory_limits_heavy_work(self):
        with patch.object(parallel.os, 'cpu_count', return_value=12), \
                patch.object(parallel, 'available_memory', return_value=2 * 1024 ** 3):
            self.assertEqual(parallel.worker_count(400 * 1024 ** 2), 3)
            self.assertEqual(parallel.worker_count(), 8)

    def test_little_memory_still_leaves_one_worker(self):
        with patch.object(parallel.os, 'cpu_count', return_value=12), \
                patch.object(parallel, 'available_memory', return_value=100 * 1024 ** 2):
            self.assertEqual(parallel.worker_count(400 * 1024 ** 2), 1)

    def test_unknown_memory_does_not_limit(self):
        with patch.object(parallel.os, 'cpu_count', return_value=4), \
                patch.object(parallel, 'available_memory', return_value=None):
            self.assertEqual(parallel.worker_count(400 * 1024 ** 2), 3)

    def test_memory_is_read_on_this_system(self):
        free = parallel.available_memory()
        self.assertTrue(free is None or free > 0)


if __name__ == '__main__':
    unittest.main()
