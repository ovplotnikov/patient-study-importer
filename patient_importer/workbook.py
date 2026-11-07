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

from . import quality

HEADERS={
 'Пациенты':['PatientID','ФИО','Дата рождения','Документов'],
 'Исследования':['StudyID','PatientID','Пациент','Дата документа','Вид документа','Статус','Показателей','Основание сопоставления','Файл'],
 'Показатели':['ResultID','StudyID','PatientID','Пациент','Дата показателя','Основание даты','Раздел','Показатель','Результат','Число','Единицы','Сторона','Референс','Флаг','Источник значения','Статус извлечения','Страница','Строка','Исходный фрагмент','Примечание сверки'],
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
    parsed_documents = [(document, workspace._load_analysis(document)) for document in documents]
    counts=Counter(document['patient_id'] for document, parsed in parsed_documents if not parsed.get('duplicate_of'))
    for patient in sorted(patients.values(),key=lambda p:p['name']):
        if counts[patient['id']]:rows['Пациенты'].append([patient['id'],patient['name'],patient['birth'],counts[patient['id']]])
    for document, a in parsed_documents:
        sid=document['id'];pid=document['patient_id']
        name=patients[pid]['name'] if pid else ''
        duplicate_of = a.get('duplicate_of')
        outstanding = quality.issues(a)
        if duplicate_of:
            status = 'Подтверждённый дубль'
            observations, sections = [], []
            reason = 'Основное исследование: '+duplicate_of
        else:
            observations, sections = a['observations'], a['sections']
            reason = document['reason']
            if not pid:
                status = 'Уточнить пациента'
                rows['Проверка'].append([stable_id(sid,'identity'),sid,document['filename'],'Уточнить пациента',reason])
            elif outstanding:
                status = 'Проверить данные'
            elif a['quality'].get('confirmed'):
                status = 'Сверено · неполные данные' if a['quality'].get('incomplete') else 'Проверено'
            else:
                status = 'Готово'
            if a['quality'].get('confirmed') and a['quality'].get('accepted_missing'):
                findings = '\n'.join(issue['message'] for issue in a['quality']['accepted_missing'])
                detail = f"{findings}\nРешение при сверке: {a['quality'].get('note', '')}"
                rows['Проверка'].append([stable_id(sid,'quality-accepted'),sid,document['filename'],'Сверено · неполные данные',detail])
            for issue in outstanding:
                detail = issue['message']
                if issue.get('page'):
                    detail = f"Страница {issue['page']}. {detail}"
                rows['Проверка'].append([stable_id(sid,'quality',issue['id']),sid,document['filename'],'Проверить данные',detail])
        rows['Исследования'].append([sid,pid,name,a['date'],a['type'],status,len(observations),reason,document['filename']])
        for index,item in enumerate(observations):
            result_id=item.get('result_id') or stable_id(sid,item['page'],item['line'],index)
            item_status = 'Черновик · сверить' if not pid or outstanding else item['status']
            rows['Показатели'].append([result_id,sid,pid,name,item['date'],item['date_source'],item['section'],item['label'],item['result'],item['number'],item['unit'],item['side'],item['reference'],item['flag'],item['origin'],item_status,item['page'],item['line'],item['raw'],item.get('review_comment','')])
        for index,item in enumerate(sections):
            for part,start in enumerate(range(0,max(1,len(item['text'])),30000)):
                if item.get('section_id') and part == 0:
                    text_id = item['section_id']
                elif item.get('section_id') and 'legacy_section_index' not in item:
                    text_id = stable_id(item['section_id'], part)
                else:
                    text_id = stable_id(sid, 'section', item.get('legacy_section_index', index), part)
                rows['Заключения'].append([text_id,sid,pid,item['section'],item['text'][start:start+30000],item['page'],item['line']])
        for page,text in enumerate(a['pages'],1):
            for part,start in enumerate(range(0,max(1,len(text)),30000),1):
                source_page=a.get('page_numbers',list(range(1,len(a['pages'])+1)))[page-1]
                rows['Тексты'].append([stable_id(sid,'page',page,part),sid,source_page,part,text[start:start+30000]])
        for arrival in db.execute('SELECT * FROM arrivals WHERE document_id=? ORDER BY id',(sid,)):
            destination=arrival['destination'] or str(workspace.destination(arrival))
            preserved=next(workspace.originals.glob(arrival['hash']+'.*'),Path(document['original']))
            archive_path=Path(destination).relative_to(workspace.root).as_posix()
            original_path=preserved.relative_to(workspace.root).as_posix()
            rows['Источники'].append([str(arrival['id']),sid,arrival['hash'],Path(arrival['source']).name,archive_path,original_path,arrival['category'],arrival['created']])
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
    for row_no,row in enumerate(rows,2):
        clean=[safe(value) for value in row]
        for column in date_columns:
            if clean[column]:clean[column]=datetime.strptime(clean[column],'%Y-%m-%d')
        clean.extend(custom_values.get(str(row[0]),{}).get(h) for h in custom_headers)
        sheet.append(clean)
        # Imported text must not become executable spreadsheet formulas.
        for column,value in enumerate(row,1):
            if isinstance(value,str) and column-1 not in date_columns:sheet.cell(row_no,column).data_type='s'
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
                row[column].hyperlink=row[column].value
                row[column].style='Hyperlink'


def archive_removed_rows(book, sheet_name, current_rows, archive_label, metadata_row):
    """Keep old row IDs and custom notes when rows cannot be matched safely."""
    if sheet_name not in book or '_Программа' not in book:
        return
    previous = book[sheet_name]
    current_ids = {row[0] for row in current_rows}
    removed = [row for row in previous.iter_rows(min_row=2) if row[0].value and row[0].value not in current_ids]
    if not removed:
        return
    meta = book['_Программа']
    key = 'result_archive' if sheet_name == 'Показатели' else 'section_archive'
    archive_name = meta[f'B{metadata_row}'].value if meta[f'A{metadata_row}'].value == key else None
    if archive_name and archive_name in book:
        archive = book[archive_name]
    else:
        archive = book.create_sheet(archive_label)
        archive.freeze_panes = 'B2'
        archive.append(['В архиве с'])
        meta[f'A{metadata_row}'], meta[f'B{metadata_row}'] = key, archive.title
    columns = {cell.value: cell.column for cell in archive[1] if cell.value}
    source_columns = []
    for cell in previous[1]:
        if not cell.value:
            source_columns.append(None)
            continue
        header = str(cell.value)
        if header not in columns:
            index = max(columns.values())+1
            archive.cell(1, index, header).data_type = 's'
            columns[header] = index
        source_columns.append(columns[header])
    for row in removed:
        index = archive.max_row+1
        archive.cell(index, 1, datetime.now()).number_format = 'dd.mm.yyyy hh:mm'
        for source, column in zip(row, source_columns):
            if column is not None:
                target = archive.cell(index, column, source.value)
                target.data_type = source.data_type
                target.number_format = source.number_format


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
    if 'Показатели' in book and '_Программа' in book and book['_Программа']['A4'].value != 'quality_comments':
        sheet = book['Показатели']
        headers = {cell.value for cell in sheet[1]}
        for cell in sheet[1]:
            if cell.value == 'Примечание сверки':
                new_name = 'Примечание сверки (пользователь)'
                index = 2
                while new_name in headers:
                    new_name = f'Примечание сверки (пользователь {index})'
                    index += 1
                cell.value = new_name
                headers.add(new_name)
    archive_removed_rows(book, 'Показатели', data['Показатели'], 'Архив показателей', 3)
    archive_removed_rows(book, 'Заключения', data['Заключения'], 'Архив заключений', 5)
    if 'Обзор' in book:book.remove(book['Обзор'])
    summary=book.create_sheet('Обзор',0)
    summary.append(['Исследования пациентов'])
    summary.merge_cells('A1:B1')
    summary['A1'].font=Font(name='Calibri',size=18,bold=True,color='244C65')
    summary.append(['Обновлено',datetime.now()]);summary['B2'].number_format='dd.mm.yyyy hh:mm'
    metrics=workspace.summary(db)
    for label,value in [('Документов',metrics['documents']),('Пациентов',metrics['patients']),('Уточнить пациента',metrics['review']),('Проверить данные',metrics['quality_review']),('Повторов файлов',metrics['duplicates']),('Ошибок чтения',metrics['errors']),('Извлечено показателей',len(data['Показатели']))]:summary.append([label,value])
    summary.append([])
    summary.append(['Новые файлы','Добавьте в «Входящие» и нажмите «Обработать».'])
    summary.append(['Уточнения','Откройте «Проверить документы» в программе. Строки со статусом «Черновик · сверить» требуют проверки по оригиналу.'])
    summary.append(['Ручные записи','Колонка «Заметки», дополнительные колонки и отдельные листы сохраняются при обновлении.'])
    summary.append(['Измерения из описаний','Сохраняются вместе с датой, стороной и исходным контекстом. Отметка «Проверить» означает необходимость сверки.'])
    summary.column_dimensions['A'].width=33;summary.column_dimensions['B'].width=95
    summary.sheet_view.showGridLines=False
    for row in summary:
        for cell in row:cell.alignment=Alignment(wrap_text=True,vertical='center')
        summary.row_dimensions[row[0].row].height=32
    for index,(name,headers) in enumerate(HEADERS.items(),1):write_sheet(book,name,headers,data[name],index)
    meta=book['_Программа'] if '_Программа' in book else book.create_sheet('_Программа')
    history = db.execute('SELECT id,document_id,created,action,note FROM study_history ORDER BY id').fetchall()
    if history:
        journal = meta['B6'].value if meta['A6'].value == 'review_journal' else None
        if not journal or journal not in book:
            journal = book.create_sheet('Сверки').title
            meta['A6'], meta['B6'] = 'review_journal', journal
        actions = {'quality_confirmed': 'Данные сверены', 'duplicate_confirmed': 'Дубль подтверждён',
                   'reprocessed': 'Повторная обработка'}
        rows = [[str(row['id']), row['document_id'], row['created'], actions[row['action']], row['note']] for row in history]
        write_sheet(book, journal, ['ReviewID','StudyID','Время (UTC)','Действие','Решение'], rows, len(HEADERS)+1)
    meta['A1']='workspace';meta['B1']=workspace_id;meta['A2']='revision';meta['B2']=db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
    meta['A4']='quality_comments'
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
