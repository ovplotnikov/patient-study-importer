import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from patient_importer.storage import Workspace
from patient_importer.intake import stage_files,defer_file,retry_file
from patient_importer.smoke import SAMPLE


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.ws=Workspace(self.root/'workspace');self.src=self.root/'external';self.src.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def test_same_names_never_overwrite_and_external_originals_survive(self):
        a=self.src/'a';b=self.src/'b';a.mkdir();b.mkdir()
        (a/'бланк.txt').write_text(SAMPLE);(b/'бланк.txt').write_text(SAMPLE+'\nДругая версия')
        result=stage_files(self.ws,[a,b])
        self.assertEqual(len(result['added']),2)
        self.assertEqual(len({Path(p).name for p in result['added']}),2)
        self.assertEqual((a/'бланк.txt').read_text(),SAMPLE)
        self.ws.import_folder()
        self.assertTrue((a/'бланк.txt').exists());self.assertTrue((b/'бланк.txt').exists())
    def test_deferred_file_is_not_imported_or_deleted(self):
        source=self.src/'blank.txt';source.write_text(SAMPLE)
        staged=stage_files(self.ws,[source])
        target=defer_file(self.ws,staged['added'][0])
        self.assertTrue(target.exists());self.assertTrue(source.exists())
        self.assertEqual(self.ws.files()[0],[])
        self.assertEqual(self.ws.import_folder()['documents'],0)
    def test_source_change_during_copy_leaves_no_incomplete_input(self):
        source=self.src/'blank.txt';source.write_text(SAMPLE)
        with patch('patient_importer.intake.sha256',side_effect=['a','b']):
            result=stage_files(self.ws,[source])
        self.assertEqual(len(result['errors']),1)
        self.assertEqual(list((self.ws.root/'Входящие').iterdir()),[])
    def test_nested_workspace_and_symlinks_are_excluded(self):
        nested=Workspace(self.src/'other')
        (nested.root/'Входящие'/'secret.txt').write_text(SAMPLE)
        (self.src/'ok.txt').write_text(SAMPLE)
        result=stage_files(self.ws,[self.src])
        self.assertEqual(len(result['added']),1)
        with self.assertRaises(ValueError):stage_files(self.ws,[nested.root])
    def test_retry_clears_original_error_and_does_not_duplicate(self):
        source=self.ws.root/'Входящие'/'test.txt';source.write_text(SAMPLE)
        with patch('patient_importer.extraction.extract',side_effect=ValueError('test failure')):
            first=self.ws.import_folder()
        self.assertEqual(first['batch'][0]['status'],'Ошибка')
        problem=self.ws.problems()[0];retry_file(self.ws,problem['id'])
        success=self.ws.import_folder()
        self.assertEqual(success['errors'],0);self.assertEqual(success['documents'],1)
        self.assertEqual(success['batch'][0]['status'],'Добавлен')
    def test_batch_is_distinct_from_accumulated_registry(self):
        source=self.ws.root/'Входящие'/'test.txt';source.write_text(SAMPLE)
        self.ws.import_folder();source.write_text(SAMPLE)
        second=self.ws.import_folder()
        self.assertEqual(second['documents'],1)
        self.assertEqual([r['status'] for r in second['batch']],['Повтор'])
        third=self.ws.import_folder();self.assertEqual(third['batch'],[])
    def test_confirmed_document_cannot_be_assigned_twice(self):
        source=self.ws.root/'Входящие'/'test.txt';source.write_text('Дата рождения: 01.01.2000\nЗаключение: учебный пример')
        self.ws.import_folder();sid=self.ws.review_list()[0]['id']
        self.ws.assign(sid,'ТЕСТОВ ТЕСТ ТЕСТОВИЧ','2000-01-01')
        with self.assertRaises(ValueError):self.ws.assign(sid,'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ','2000-01-01')
        self.assertEqual(self.ws.summary()['patients'],1)

if __name__=='__main__':unittest.main()
