"""Evidence-preserving structured extraction and conservative identity matching."""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import re

from .parsing import identity, laboratory_rows, document_date, normalized, iso_date, study_type

PARSER_VERSION='2.0.0'
SECTIONS=re.compile(r'^(Жалобы|Анамнез(?: заболевания| жизни)?|Объективный статус|Объективно|Диагноз[^:]*|Клинический диагноз|Заключение[^:]*|Рекомендации[^:]*|Лечение|Назначения|Проведенные обследования|Результаты[^:]*|Осмотр)\s*:',re.I)
NUMBER=r'[<>≤≥]?\s*\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)*'
UNITS=r'(?:мкмоль/л|ммоль/л|мл/мин(?:/1[.,]73\s*м[²2])?|мм\s*рт\.?\s*ст\.?|г/л|г/сут|мг/л|мг/сут|мл|мм|см|кг|%|мм/ч|10\^?9/л)'
MEASUREMENT=re.compile(rf'(?P<value>{NUMBER})\s*(?P<unit>{UNITS})(?![а-я])',re.I)
DIMENSIONS=re.compile(r'(\d+(?:[.,]\d+)?(?:\s*[xх×*]\s*\d+(?:[.,]\d+)?){1,2})\s*(мм|см)?',re.I)
DATE_PATTERN=re.compile(r'\b(\d{2}\.\d{2}\.\d{4})\b')


def content_fingerprint(pages):
    # Every character/number remains part of the fingerprint; only whitespace changes.
    text=normalized(' '.join(pages))
    return hashlib.sha256(text.encode()).hexdigest() if text else None


def number(value):
    if re.fullmatch(r'\d+(?:[.,]\d+)?',value.strip()):
        return float(value.strip().replace(',','.'))
    return None


def side_from(text):
    matches=list(re.finditer(r'прав(?:ая|ой|ую|а|о)|лев(?:ая|ой|ую|а|о)|\bRD\b|\bRS\b',text,re.I))
    if not matches:
        return ''
    token=matches[-1][0].lower()
    return 'Правая' if token.startswith('прав') or token=='rd' else 'Левая'


def metric_from(text, default):
    labels=[('Паренхима',r'паренхим|\bпар\b'),('Лоханка',r'лохан|члс'),('Чашечки',r'чашеч'),
            ('Мочеточник',r'мочеточник'),('Остаточная моча',r'остаточ|после мочеиспуск'),
            ('Объём мочевого пузыря',r'объем|объём'),('Креатинин',r'креатинин'),('Мочевина',r'мочевин'),
            ('Гемоглобин',r'гемоглоб|\bHb\b'),('Белок',r'белок|белка'),('Рост',r'рост'),('Масса',r'масса|\bвес\b|\bМ='),
            ('Стенка',r'стенк'),('Клиренс',r'клиренс'),('Накопление препарата',r'накоплен'),('Перфузия',r'uptake|перфуз')]
    candidates=[]
    for label,pattern in labels:
        hits=list(re.finditer(pattern,text,re.I))
        if hits:candidates.append((hits[-1].start(),label))
    return max(candidates)[1] if candidates else default


def narrative(pages, day, ocr_pages):
    values=[];sections=[]
    for page_no,page in enumerate(pages,1):
        # Laboratory fields are already extracted by the layout parser.
        if 'Показатель' in page and 'Результат' in page:
            continue
        page_day=document_date(page) or day
        section='Описание исследования';context_date=page_day;date_source='Дата документа';context=''
        lines=page.splitlines();section_lines=[];section_start=1
        def flush():
            text='\n'.join(section_lines).strip()
            if text:sections.append({'page':page_no,'line':section_start,'section':section,'text':text})
        for index,line in enumerate(lines,1):
            clean=' '.join(line.split())
            if not clean:continue
            marker=SECTIONS.match(clean)
            if marker:
                flush();section_lines=[];section_start=index;section=marker[1]
                context='';context_date=page_day;date_source='Дата документа'
                if re.search('анамнез',section,re.I):context_date=None;date_source='Дата в анамнезе не указана'
            section_lines.append(line)
            if re.search(r'рекомендац|назначен|лечени',section,re.I):continue
            if re.search(r'Дата рождения|(?:^|\s)Адрес|№|ОГРН|телефон|[+]7\s*\(',clean,re.I):continue
            # Carry an explicit study date within a contiguous paragraph, never a DOB.
            dates=list(DATE_PATTERN.finditer(clean))
            if dates:
                value=iso_date(dates[0][1])
                if value:context_date=value;date_source='Дата в тексте'
            historical=bool(re.search('анамнез',section,re.I))
            fragments=[]
            for match in DIMENSIONS.finditer(clean):
                before=clean[:match.start()]
                side=side_from(context+' '+before)
                if not side or not re.search(r'почк|размер|паренхим|\bRD\b|\bRS\b',context+' '+before,re.I):continue
                # A printed dimension pair is retained as a pair, without assigning axes.
                fragments.append((match.start(),match.end(),metric_from(context+' '+before,'Размеры почки'),match[1],match[2] or '',side))
            occupied=[(start,end) for start,end,*_ in fragments]
            for match in MEASUREMENT.finditer(clean):
                if any(start<=match.start()<end for start,end in occupied):continue
                before=clean[:match.start()]
                metric=metric_from(context+' '+before,'Измерение из описания')
                side=side_from(context+' '+before) if re.search('почк|паренхим|лохан|чашеч|мочеточник',context+' '+before,re.I) else ''
                fragments.append((match.start(),match.end(),metric,match['value'].strip(),match['unit'],side))
            for start,end,metric,result,unit,side in sorted(fragments):
                local_dates=[match for match in dates if match.end()<start]
                local_day=iso_date(local_dates[-1][1]) if local_dates else context_date
                status='Проверить' if page_no in ocr_pages or not local_day or metric=='Измерение из описания' else 'Извлечено'
                values.append({'page':page_no,'line':index,'section':section,'label':metric,'result':result,'number':number(result),
                               'unit':unit,'reference':'','raw':clean,'flag':'','comment':'','side':side,'date':local_day,
                               'date_source':date_source,'origin':'Анамнез' if historical else 'Описание','status':status})
            context=(context+' '+clean)[-200:]
        flush()
    return values,sections


def analyze(extracted, filename):
    pages=extracted['pages'];ocr_pages=extracted['ocr_pages']
    names,births,cards=identity(pages)
    native_names,native_births,_=identity(extracted.get('native_pages',[text for i,text in enumerate(pages,1) if i not in ocr_pages]))
    day=document_date('\n'.join(pages))
    laboratory,pending=laboratory_rows(pages)
    for row in laboratory:
        row_day=document_date(pages[row['page']-1]) or day
        row.update(side='',date=row_day,date_source='Дата страницы' if row_day!=day else 'Дата документа',origin='Лабораторный бланк',status='Проверить' if row['page'] in ocr_pages else 'Извлечено')
    descriptive,sections=narrative(pages,day,ocr_pages)
    problems=[]
    if not any(text.strip() for text in pages):problems.append('В документе не обнаружен читаемый текст')
    for page,line,text in pending:
        if re.match(r'^(Общий|Исследование мочи методом|Анализ крови биохимический)',text,re.I):continue
        problems.append(f'Страница {page}, строка {line}: проверить строку таблицы — {text}')
    page_numbers=extracted.get('page_numbers',list(range(1,len(pages)+1)))
    for item in laboratory+descriptive+sections:item['page']=page_numbers[item['page']-1]
    return {'version':PARSER_VERSION,'names':names,'births':births,'cards':cards,'date':day,'page_numbers':page_numbers,
            'type':study_type('\n'.join(pages),filename),'observations':laboratory+descriptive,'sections':sections,
            'problems':problems,'identity_from_ocr':names!=native_names or births!=native_births,'pages':pages,'ocr_pages':ocr_pages}


def resolve(analyses, known_patients):
    """Return exact identities or review candidates. Folder names never invent identity."""
    keys=set((patient['name'],patient['birth']) for patient in known_patients)
    for item in analyses:
        a=item['analysis']
        if len(a['names'])==1 and len(a['births'])==1 and len(a['names'][0].split())==3 and not a['identity_from_ocr']:
            keys.add((a['names'][0],a['births'][0]))
    compact=defaultdict(set)
    for name,birth in keys:compact[(name.replace(' ',''),birth)].add((name,birth))
    votes=defaultdict(Counter)
    results={}
    for item in analyses:
        a=item['analysis'];key=None;reason='Недостаточно идентификаторов';candidates=[]
        if len(a['names'])>1 or len(a['births'])>1:reason='В документе несколько ФИО или дат рождения. Сверьте все страницы с оригиналом'
        if len(a['names'])==1 and len(a['births'])==1:
            options=compact[(a['names'][0].replace(' ',''),a['births'][0])]
            if len(options)==1:
                key=next(iter(options));reason='ФИО и дата рождения совпадают'
        if a['identity_from_ocr']:
            reason='Подтвердите ФИО и дату рождения, распознанные со скана';key=None
        if not key:
            candidates=[(name,birth) for name,birth in sorted(keys) if birth in a['births'] or name in a['names']]
        if key:votes[item['folder']][key]+=1
        results[item['id']]={'key':key,'reason':reason,'candidates':candidates}
    dominant={}
    for folder,counter in votes.items():
        key,count=counter.most_common(1)[0]
        if count>=5 and count/sum(counter.values())>=.9 and key[0].split()[0] in normalized(folder):dominant[folder]=key
    for item in analyses:
        result=results[item['id']];owner=dominant.get(item['folder'])
        if owner and result['key'] and owner!=result['key']:
            result['candidates']=[result['key'],owner];result['key']=None
            result['reason']='Пациент отличается от остальных документов в этой папке'
        elif owner and not result['key'] and owner not in result['candidates']:
            result['candidates'].append(owner)
    return results
