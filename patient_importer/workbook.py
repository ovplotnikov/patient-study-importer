"""Atomic XLSX output with stable row IDs and preserved user columns."""
from collections import Counter
from copy import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from openpyxl import Workbook,load_workbook
from openpyxl.styles import Alignment,Font,PatternFill
from openpyxl.worksheet.table import Table,TableStyleInfo

HEADERS={
 'Пациенты':['PatientID','ФИО','Дата рождения','Документов'],
 'Исследования':['StudyID','PatientID','Пациент','Дата документа','Вид документа','Статус','Показателей','Основание сопоставления','Файл'],
 'Показатели':['ResultID','StudyID','PatientID','Пациент','Дата показателя','Основание даты','Раздел','Показатель','Результат','Число','Единицы','Сторона','Референс','Флаг','Источник значения','Статус извлечения','Страница','Строка','Исходный фрагмент'],
 'Заключения':['TextID','StudyID','PatientID','Раздел','Текст','Страница','Строка'],
 'Проверка':['ReviewID','StudyID','Файл','Что проверить','Подробности'],
 'Источники':['SourceID','StudyID','SHA256','Исходное имя','Архивный путь','Сохранённый оригинал','Тип','Дата загрузки'],
 'Тексты':['PageID','StudyID','Страница','Часть','Полный текст'],
}


def stable_id(*pieces):
    return hashlib.sha256('|'.join(map(str,pieces)).encode()).hexdigest()[:22]


def safe(value):
    if not isinstance(value,str):return value
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',lambda m:f'\\u{ord(m[0]):04x}',value)


def rows_for(workspace,db):
    rows={name:[] for name in HEADERS}
    patients={r['id']:dict(r) for r in db.execute('SELECT * FROM patients')}
    documents=db.execute('SELECT * FROM documents ORDER BY created,id').fetchall()
    counts=Counter(r['patient_id'] for r in documents)
    for patient in sorted(patients.values(),key=lambda p:p['name']):
        if counts[patient['id']]:rows['Пациенты'].append([patient['id'],patient['name'],patient['birth'],counts[patient['id']]])
    for document in documents:
        sid=document['id'];pid=document['patient_id'];a=json.loads(document['analysis'])
        name=patients[pid]['name'] if pid else ''
        rows['Исследования'].append([sid,pid,name,a['date'],a['type'],'Готово' if pid else 'Уточнить пациента',len(a['observations']),document['reason'],document['filename']])
        if not pid:
            rows['Проверка'].append([stable_id(sid,'identity'),sid,document['filename'],'Уточнить пациента',document['reason']])
        for index,item in enumerate(a['observations']):
            result_id=stable_id(sid,item['page'],item['line'],index)
            rows['Показатели'].append([result_id,sid,pid,name,item['date'],item['date_source'],item['section'],item['label'],item['result'],item['number'],item['unit'],item['side'],item['reference'],item['flag'],item['origin'],item['status'],item['page'],item['line'],item['raw']])
            if item['status']=='Проверить':
                rows['Проверка'].append([stable_id(result_id,'value'),sid,document['filename'],'Проверить измерение',f"Страница {item['page']}, строка {item['line']}. {item['raw']}"])
        for index,item in enumerate(a['sections']):
            for part,start in enumerate(range(0,max(1,len(item['text'])),30000)):
                rows['Заключения'].append([stable_id(sid,'section',index,part),sid,pid,item['section'],item['text'][start:start+30000],item['page'],item['line']])
        for index,problem in enumerate(a['problems']):
            rows['Проверка'].append([stable_id(sid,'problem',index),sid,document['filename'],'Проверить извлечение',problem])
        if not a['date']:
            rows['Проверка'].append([stable_id(sid,'date'),sid,document['filename'],'Дата документа','В бланке не найдена однозначная дата исследования или посещения'])
        for page,text in enumerate(a['pages'],1):
            for part,start in enumerate(range(0,max(1,len(text)),30000),1):
                rows['Тексты'].append([stable_id(sid,'page',page,part),sid,page,part,text[start:start+30000]])
        for arrival in db.execute('SELECT * FROM arrivals WHERE document_id=? ORDER BY id',(sid,)):
            destination=arrival['destination'] or str(workspace.destination(arrival))
            preserved=next(workspace.originals.glob(arrival['hash']+'.*'),Path(document['original']))
            rows['Источники'].append([str(arrival['id']),sid,arrival['hash'],Path(arrival['source']).name,destination,str(preserved),arrival['category'],arrival['created']])
    for row in db.execute("SELECT * FROM arrivals WHERE category='Ошибки' OR error!=''"):
        rows['Проверка'].append([stable_id('arrival',row['id']),row['document_id'],Path(row['source']).name,'Ошибка файла',row['error']])
    return rows


def write_sheet(book,name,headers,rows,index):
    custom_headers=['Заметки'];custom_values={}
    if name in book:
        previous=book[name]
        old_headers=[cell.value for cell in previous[1]]
        custom_headers=[str(h) for h in old_headers if h and h not in headers]
        if 'Заметки' not in custom_headers:custom_headers.append('Заметки')
        positions={h:old_headers.index(h) for h in custom_headers if h in old_headers}
        for values in previous.iter_rows(min_row=2,values_only=True):
            if values and values[0]:custom_values[str(values[0])]={h:values[i] for h,i in positions.items()}
        book.remove(previous)
    sheet=book.create_sheet(name,index)
    sheet.append(headers+custom_headers)
    date_columns=[i for i,h in enumerate(headers) if h in {'Дата рождения','Дата документа','Дата показателя'}]
    for row in rows:
        clean=[safe(value) for value in row]
        for column in date_columns:
            if clean[column]:clean[column]=datetime.strptime(clean[column],'%Y-%m-%d')
        clean.extend(custom_values.get(str(row[0]),{}).get(h) for h in custom_headers)
        sheet.append(clean)
        # Imported text must not become executable spreadsheet formulas.
        for column,value in enumerate(row,1):
            if isinstance(value,str) and column-1 not in date_columns:sheet.cell(sheet.max_row,column).data_type='s'
    sheet.freeze_panes='D2' if name=='Показатели' else 'B2'
    sheet.sheet_view.showGridLines=False
    for cell in sheet[1]:
        cell.font=Font(name='Calibri',size=11,color='FFFFFF',bold=True)
        cell.fill=PatternFill('solid',fgColor='244C65')
        cell.alignment=Alignment(wrap_text=True,vertical='center')
    sheet.row_dimensions[1].height=34
    for column,header in enumerate(headers+custom_headers,1):
        width=22
        if header in {'ФИО','Пациент','Файл','Исходное имя'}:width=38
        if header in {'Подробности','Исходный фрагмент','Полный текст','Текст','Основание сопоставления','Архивный путь','Сохранённый оригинал'}:width=85
        if header in {'Показатель','Раздел','Референс','Заметки'}:width=38
        sheet.column_dimensions[sheet.cell(1,column).column_letter].width=width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:cell.alignment=Alignment(vertical='top',wrap_text=cell.column>3)
        sheet.row_dimensions[row[0].row].height=32
    for column in date_columns:
        for cells in sheet.iter_rows(min_row=2,min_col=column+1,max_col=column+1):cells[0].number_format='dd.mm.yyyy'
    for cells in sheet.iter_rows(min_row=2,min_col=1,max_col=1):cells[0].font=Font(name='Calibri',size=10,color='5F6C76')
    if rows:
        table=Table(displayName='ImportTable'+str(index),ref=sheet.dimensions)
        table.tableStyleInfo=TableStyleInfo(name='TableStyleMedium2',showRowStripes=True)
        sheet.add_table(table)
    if name=='Источники':
        for row in sheet.iter_rows(min_row=2):
            for column in [4,5]:
                row[column].hyperlink=Path(row[column].value).as_uri()
                row[column].style='Hyperlink'


def export_workbook(workspace,db):
    target=workspace.root/'Исследования.xlsx'
    if target.with_name('~$'+target.name).exists():raise RuntimeError('Закройте Excel с таблицей «Исследования» и нажмите «Обработать» ещё раз. Данные уже сохранены.')
    workspace_id=db.execute("SELECT value FROM meta WHERE key='workspace'").fetchone()[0]
    if target.exists():
        try:book=load_workbook(target)
        except Exception as exc:raise RuntimeError('Не удалось открыть существующую таблицу. Она сохранена без изменений.') from exc
        if '_Программа' not in book or book['_Программа']['B1'].value!=workspace_id:
            raise RuntimeError('В папке уже есть другая таблица «Исследования.xlsx». Переименуйте её или выберите новую рабочую папку.')
    else:
        book=Workbook();book.remove(book.active)
    data=rows_for(workspace,db)
    if 'Обзор' in book:book.remove(book['Обзор'])
    summary=book.create_sheet('Обзор',0)
    summary.append(['Исследования пациентов'])
    summary['A1'].font=Font(name='Calibri',size=18,bold=True,color='244C65')
    summary.append(['Обновлено',datetime.now()]);summary['B2'].number_format='dd.mm.yyyy hh:mm'
    metrics=workspace.summary(db)
    for label,value in [('Документов',metrics['documents']),('Пациентов',metrics['patients']),('Уточнить пациента',metrics['review']),('Повторов файлов',metrics['duplicates']),('Ошибок чтения',metrics['errors']),('Извлечено показателей',len(data['Показатели']))]:summary.append([label,value])
    summary.append([])
    summary.append(['Новые файлы','Добавьте в «Входящие» и нажмите «Обработать».'])
    summary.append(['Уточнения','Откройте «Уточнить пациентов» в программе.'])
    summary.append(['Ручные записи','Колонка «Заметки», дополнительные колонки и отдельные листы сохраняются при обновлении.'])
    summary.append(['Измерения из описаний','Сохраняются вместе с датой, стороной и исходным контекстом. Отметка «Проверить» означает необходимость сверки.'])
    summary.column_dimensions['A'].width=33;summary.column_dimensions['B'].width=95
    summary.sheet_view.showGridLines=False
    for row in summary:
        for cell in row:cell.alignment=Alignment(wrap_text=True,vertical='center')
        summary.row_dimensions[row[0].row].height=32
    for index,(name,headers) in enumerate(HEADERS.items(),1):write_sheet(book,name,headers,data[name],index)
    meta=book['_Программа'] if '_Программа' in book else book.create_sheet('_Программа')
    meta['A1']='workspace';meta['B1']=workspace_id;meta['A2']='revision';meta['B2']=db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
    meta.sheet_state='veryHidden'
    fd,temporary=tempfile.mkstemp(suffix='.xlsx',prefix='.import-',dir=workspace.root);os.close(fd)
    try:
        book.save(temporary)
        with open(temporary,'r+b') as handle:os.fsync(handle.fileno())
        if target.exists():
            backup=workspace.root/'Резервные копии'/('Исследования_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.xlsx')
            shutil.copy2(target,backup)
        try:os.replace(temporary,target)
        except PermissionError as exc:raise RuntimeError('Закройте таблицу в Excel и повторите обработку. Файлы и данные сохранены.') from exc
        for old in sorted((workspace.root/'Резервные копии').glob('Исследования_*.xlsx'))[:-20]:old.unlink()
    finally:
        Path(temporary).unlink(missing_ok=True)
    with db:db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('exported_revision',meta['B2'].value))
