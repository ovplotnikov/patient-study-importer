"""Synthetic end-to-end checks, also executed inside the frozen Windows executable."""
import json
from pathlib import Path
import shutil

from openpyxl import load_workbook

from .storage import Workspace

SAMPLE='''Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ
Дата рождения: 01.01.2000
Дата выполнения исследования: 02.03.2025
                         Показатель                       Результат  Единицы
Креатинин                              71.5   мкмоль/л   (40 - 90)
Лейкоциты                              0    10^9/л
Комментарий:
'''


def make_mixed_pdf(source,target):
    from pypdf import PdfReader,PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=PdfWriter(clone_from=source);page=writer.pages[0]
    page['/Resources'][NameObject('/Font')]=DictionaryObject({NameObject('/F1'):DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})})
    stream=DecodedStreamObject();stream.set_data(page.get_contents().get_data()+b'\nBT /F1 12 Tf 20 10 Td (Synthetic cover text with embedded test image) Tj ET')
    page.replace_contents(stream)
    writer.write(target)


def smoke_test(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    workspace=Workspace(root/'Набор с пробелами и кириллицей')
    incoming=workspace.root/'Входящие'
    first=incoming/'анализ.txt';first.write_text(SAMPLE,encoding='utf-8')
    initial=workspace.import_folder()
    assert initial['documents']==1 and initial['patients']==1 and initial['review']==0,initial
    assert not first.exists() and (workspace.root/'Обработано'/'анализ.txt').exists()
    table=workspace.root/'Исследования.xlsx'
    book=load_workbook(table);book['Пациенты']['E2']='Собственная заметка'
    book.create_sheet('Мои записи')['A1']='Сохранить этот лист';book.save(table)
    (incoming/'копия.txt').write_text(SAMPLE,encoding='utf-8')
    repeat=workspace.import_folder()
    assert repeat['documents']==1 and repeat['duplicates']==1,repeat
    assert (workspace.root/'Дубли'/'копия.txt').exists()
    (incoming/'следующий анализ.txt').write_text(SAMPLE.replace('02.03.2025','03.03.2025').replace('71.5','72.1'),encoding='utf-8')
    third=workspace.import_folder()
    assert third['documents']==2 and third['patients']==1,third
    book=load_workbook(table)
    assert book['Пациенты']['E2'].value=='Собственная заметка'
    assert book['Мои записи']['A1'].value=='Сохранить этот лист'
    ambiguous=incoming/'без ФИО.txt';ambiguous.write_text('Дата рождения: 01.01.2000\nЗаключение: тестовый текст.',encoding='utf-8')
    workspace.import_folder();pending=workspace.review_list()
    assert len(pending)==1
    workspace.assign(pending[0]['id'],'ТЕСТОВ ТЕСТ ТЕСТОВИЧ','2000-01-01')
    assert workspace.summary()['review']==0
    assert workspace.summary()['quality_review']==1
    assert (workspace.root/'Нужно уточнить'/'без ФИО.txt').exists()
    workspace.confirm_quality(pending[0]['id'],note='Тестовое заключение сверено; в оригинале нет даты и вида.')
    assert workspace.summary()['total_review']==0
    assert (workspace.root/'Обработано'/'без ФИО.txt').exists()
    assert not (workspace.root/'Нужно уточнить'/'без ФИО.txt').exists()
    smoke_parallel()
    smoke_ocr(root)
    return {'ok':True,'checks':['worker_processes','first_import','archive_move','byte_duplicate','incremental_patient','notes_preserved','custom_sheet_preserved','review_confirmation','separate_quality_confirmation','review_move','bundled_ocr','bundled_pdf_rendering','mixed_pdf_ocr'],'summary':workspace.summary()}


def smoke_parallel():
    """Worker processes start from the packaged executable and read like this process does."""
    import os
    from . import analysis, parallel

    jobs=[(index,(json.dumps({'pages':[SAMPLE],'ocr_pages':[]},ensure_ascii=False),f'{index}.txt')) for index in range(4)]
    pids={pid for _key,pid in parallel.ordered_map(parallel.process_id,jobs,total=len(jobs),workers=2,minimum=1)}
    assert pids and os.getpid() not in pids,'reading did not leave the main process'
    readings=[reading for _key,reading in parallel.ordered_map(analysis.analyze_stored,jobs,total=len(jobs),workers=2,minimum=1)]
    assert readings==[analysis.analyze_stored(payload) for _key,payload in jobs],'worker reading differs'


def smoke_ocr(root):
    """Use real packaged engines on image, scanned PDF, and mixed PDF."""
    import os
    import sys
    from PIL import Image, ImageDraw, ImageFont
    from .extraction import extract

    image = Image.new('RGB', (1200, 450), 'white')
    font_path = (Path('C:/Windows/Fonts/arial.ttf') if os.name == 'nt'
                 else Path('/System/Library/Fonts/Supplemental/Arial.ttf'))
    font = ImageFont.truetype(str(font_path), 42)
    text = ('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ',
            'Дата рождения: 01.01.2000',
            'Креатинин 71,5 мкмоль/л',
            'Глюкоза <0,50 ммоль/л')
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(text):
        draw.text((40, 35 + index * 83), line, font=font, fill='black')
    image_path = root/'ocr-test.png'
    image.save(image_path)
    pdf_path = root/'ocr-test.pdf'
    image.save(pdf_path, 'PDF', resolution=180)
    mixed_path = root/'ocr-mixed.pdf'
    make_mixed_pdf(pdf_path, mixed_path)
    from unittest.mock import patch
    def offline_extract(path):
        with patch('urllib.request.urlopen',side_effect=AssertionError('OCR must work offline')), \
             patch('requests.sessions.Session.request',side_effect=AssertionError('OCR must work offline')):
            return extract(path)
    for path in (image_path, pdf_path, mixed_path):
        data = offline_extract(path)
        recognized = ' '.join(' '.join(data['pages']).casefold().split())
        assert all(line.casefold() in recognized for line in text), (path.name, recognized)
        assert data['ocr_pages'], path.name
        assert 'OCR_USED' in {item['code'] for item in data['extraction_issues']}, path.name
        if os.name == 'nt' and getattr(sys, 'frozen', False):
            assert data['ocr_evidence'], path.name
            for item in data['ocr_evidence']:
                assert item['engine'] == 'paddle-cyrillic' and item['verification_engine'] == 'tesseract-fast', item
                assert item['bbox'] and item['image_size'], item
            assert 'OCR_ENGINE_UNAVAILABLE' not in {item['code'] for item in data['extraction_issues']}
        if path == mixed_path:
            assert data['page_numbers'] == [1, 1], data['page_numbers']
            assert 'Synthetic cover text' in data['native_pages'][0], data['native_pages']
