from pathlib import Path
import json
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from patient_importer import analysis,extraction
from patient_importer.smoke import SAMPLE,smoke_test,make_mixed_pdf
from patient_importer.storage import Workspace,sha256

ORIGINAL_EXTRACT=extraction.extract


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.workspace=Workspace(self.root/'case')

    def tearDown(self):self.temp.cleanup()

    def sample(self,name='test.txt',text=SAMPLE):
        path=self.workspace.root/'Входящие'/name;path.write_text(text,encoding='utf-8');return path

    def test_end_to_end(self):
        # OCR itself runs separately against the packaged executable and bundled engine.
        with patch('patient_importer.extraction.extract',side_effect=self.extract_with_test_image):
            self.assertTrue(smoke_test(self.root/'smoke')['ok'])

    @staticmethod
    def extract_with_test_image(path):
        if path.name in {'ocr-test.png','ocr-test.pdf','ocr-mixed.pdf'}:return {'pages':['TEST 12345 01.01.2000'],'ocr_pages':[1],'format':'image'}
        return ORIGINAL_EXTRACT(path)

    def test_export_failure_keeps_source_and_retry_deduplicates(self):
        path=self.sample()
        with patch('patient_importer.workbook.export_workbook',side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):self.workspace.import_folder()
        self.assertTrue(path.exists());self.assertEqual(self.workspace.summary()['documents'],1)
        result=self.workspace.import_folder()
        self.assertFalse(path.exists());self.assertEqual(result['documents'],1);self.assertEqual(result['pending_moves'],0)

    def test_crash_after_archive_copy_is_recovered(self):
        self.sample();original=self.workspace.finish_moves
        def simulate(db,progress):
            row=db.execute('SELECT * FROM arrivals LIMIT 1').fetchone();dest=self.workspace.destination(row);dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(Path(row['source']).read_bytes())
            with db:db.execute('UPDATE arrivals SET destination=? WHERE id=?',(str(dest),row['id']))
            raise RuntimeError('power failure')
        with patch.object(self.workspace,'finish_moves',side_effect=simulate):
            with self.assertRaises(RuntimeError):self.workspace.import_folder()
        self.workspace.import_folder()
        self.assertEqual(self.workspace.summary()['pending_moves'],0)
        self.assertEqual(self.workspace.summary()['documents'],1)

    def test_filename_collision_keeps_both(self):
        self.sample();self.workspace.import_folder()
        self.sample(text=SAMPLE.replace('71.5','72.5'));self.workspace.import_folder()
        files=list((self.workspace.root/'Обработано').glob('*.txt'))
        self.assertEqual(len(files),2);self.assertEqual(len({sha256(p) for p in files}),2)

    def test_identical_file_returned_to_same_path_goes_to_duplicates(self):
        self.sample();self.workspace.import_folder()
        self.sample();result=self.workspace.import_folder()
        self.assertEqual(result['documents'],1);self.assertEqual(result['duplicates'],1)
        self.assertTrue((self.workspace.root/'Дубли'/'test.txt').exists())

    def test_embedded_images_in_text_pdf_are_not_skipped(self):
        from PIL import Image
        source=self.root/'image.pdf';target=self.root/'mixed.pdf'
        Image.new('RGB',(700,300),'white').save(source,'PDF');make_mixed_pdf(source,target)
        with patch('patient_importer.extraction.recognize_embedded',return_value='Креатинин 71.5 мкмоль/л'):
            result=extraction.extract(target)
        self.assertIn('71.5',result['pages'][1]);self.assertEqual(result['ocr_pages'],[2])
        self.assertEqual(result['page_numbers'],[1,1])
        self.assertNotIn('71.5',result['native_pages'][0])

    def test_text_fingerprint_catches_whitespace_changes(self):
        self.sample();self.workspace.import_folder()
        self.sample('other.txt',SAMPLE.replace('Пациент: ','Пациент:  '));result=self.workspace.import_folder()
        self.assertEqual(result['documents'],1);self.assertEqual(result['duplicates'],1)

    def test_open_excel_prevents_moves(self):
        path=self.sample();lock=self.workspace.root/'~$Исследования.xlsx';lock.write_text('lock')
        with self.assertRaises(RuntimeError):self.workspace.import_folder()
        self.assertTrue(path.exists());lock.unlink();self.workspace.import_folder();self.assertFalse(path.exists())

    def test_formula_in_text_is_literal(self):
        self.sample(text=SAMPLE+'\n=HYPERLINK("evil")');self.workspace.import_folder()
        book=load_workbook(self.workspace.root/'Исследования.xlsx')
        self.assertFalse(any(c.data_type=='f' for sheet in book for row in sheet for c in row))

    def test_cancel_before_start(self):
        path=self.sample();event=threading.Event();event.set();self.workspace.import_folder(cancel=event)
        self.assertTrue(path.exists());self.assertEqual(self.workspace.summary()['documents'],0)

    def test_lock(self):
        with self.workspace.lock():
            with self.assertRaises(RuntimeError):
                with self.workspace.lock():pass

    def test_partial_original_is_recovered(self):
        path=self.sample();digest=sha256(path)
        (self.workspace.originals/(digest+'.txt.part')).write_bytes(b'incomplete')
        self.workspace.import_folder()
        self.assertEqual(self.workspace.summary()['documents'],1)

    def test_workspace_can_move_with_pending_files(self):
        path=self.sample()
        with patch('patient_importer.workbook.export_workbook',side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):self.workspace.import_folder()
        moved=self.root/'renamed'
        shutil.move(str(self.workspace.root),moved)
        workspace=Workspace(moved);workspace.import_folder()
        self.assertEqual(workspace.summary()['pending_moves'],0)
        self.assertTrue((moved/'Обработано'/path.name).exists())
        with workspace.connect() as db:
            original=db.execute('SELECT original FROM documents').fetchone()[0]
            self.assertTrue(Path(original).is_file())
        self.assertTrue(list((moved/'Резервные копии').glob('База_*.sqlite')))

    def test_common_incoming_folder_does_not_define_patient(self):
        for i in range(11):self.sample(str(i)+'.txt',SAMPLE.replace('71.5',str(70+i)))
        self.sample('different.txt',SAMPLE.replace('ТЕСТОВ ТЕСТ ТЕСТОВИЧ','ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ'))
        result=self.workspace.import_folder()
        self.assertEqual(result['patients'],2);self.assertEqual(result['review'],0)

    def test_anatomical_measurements_and_recommendations(self):
        text='Дата выполнения услуги: 02.03.2025\nРазмеры почек: правая 88,5*32 мм, левая 52,3*17,8 мм.\nТолщина паренхимы: справа 13 мм, слева 6,4-3,6-5,6 мм.\nРекомендации:\nПить до 1500 мл.'
        result=analysis.analyze({'pages':[text],'ocr_pages':[]},'УЗИ.txt')
        values=result['observations']
        self.assertFalse(any(v['result']=='1500' for v in values))
        self.assertTrue(any(v['result']=='88,5*32' and v['side']=='Правая' for v in values))
        self.assertTrue(any(v['result']=='6,4-3,6-5,6' and v['number'] is None for v in values))

    def test_same_birth_different_names_never_merge(self):
        self.sample();self.sample('another.txt',SAMPLE.replace('ТЕСТОВ ТЕСТ ТЕСТОВИЧ','ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ'))
        self.assertEqual(self.workspace.import_folder()['patients'],2)

    def test_certificate_signer_is_not_a_patient(self):
        certificate='ДОКУМЕНТ ПОДПИСАН ЭЛЕКТРОННОЙ ПОДПИСЬЮ\nСертификат: 1234\nФИО: ВРАЧ ТЕСТ ТЕСТОВИЧ'
        result=analysis.analyze({'pages':[SAMPLE,certificate],'native_pages':[SAMPLE,''],'ocr_pages':[2]},'study.pdf')
        self.assertEqual(result['names'],['ТЕСТОВ ТЕСТ ТЕСТОВИЧ'])
        self.assertFalse(result['identity_from_ocr'])

    def test_docx_tables_are_imported(self):
        from docx import Document
        path=self.workspace.root/'Входящие'/'sample.docx';doc=Document()
        for line in SAMPLE.splitlines()[:3]:doc.add_paragraph(line)
        table=doc.add_table(rows=2,cols=3)
        for c,v in zip(table.rows[0].cells,['Показатель','Результат','Единицы']):c.text=v
        for c,v in zip(table.rows[1].cells,['Креатинин','72.5','мкмоль/л']):c.text=v
        doc.save(path);self.workspace.import_folder()
        with self.workspace.connect() as db:
            parsed=json.loads(db.execute('SELECT analysis FROM documents').fetchone()[0])
        self.assertEqual(parsed['observations'][0]['number'],72.5)


if __name__=='__main__':unittest.main()
