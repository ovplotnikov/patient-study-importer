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
    assert (workspace.root/'Обработано'/'без ФИО.txt').exists()
    assert not (workspace.root/'Нужно уточнить'/'без ФИО.txt').exists()
    from PIL import Image,ImageDraw,ImageFont
    from .extraction import extract
    image=Image.new('RGB',(1200,260),'white')
    font_path=Path('C:/Windows/Fonts/arial.ttf') if __import__('os').name=='nt' else Path('/System/Library/Fonts/Supplemental/Arial.ttf')
    font=ImageFont.truetype(str(font_path),42)
    ImageDraw.Draw(image).text((40,35),'TEST 12345\nДАТА РОЖДЕНИЯ 01.01.2000',font=font,fill='black',spacing=30)
    image_path=root/'ocr-test.png';image.save(image_path)
    recognized=' '.join(extract(image_path)['pages'])
    assert '12345' in recognized and '2000' in recognized,recognized
    pdf_path=root/'ocr-test.pdf';image.save(pdf_path,'PDF')
    recognized=' '.join(extract(pdf_path)['pages'])
    assert '12345' in recognized and '2000' in recognized,recognized
    return {'ok':True,'checks':['first_import','archive_move','byte_duplicate','incremental_patient','notes_preserved','custom_sheet_preserved','review_confirmation','review_move','bundled_ocr','bundled_pdf_rendering'],'summary':workspace.summary()}
