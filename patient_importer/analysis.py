"""Evidence-preserving structured extraction and conservative identity matching."""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import re
import unicodedata

from .parsing import (
    COMPARATOR, LABORATORY_TYPES, POWER, RANGE, SCALAR, classify_pages, document_date,
    document_dates, identity, iso_date, laboratory_header, laboratory_rows,
    normalized, numeric_value, spaced,
)
from .quality import REVIEWED_PARSER_VERSION, assess

# Changing this offers existing unconfirmed documents a recalculation.
PARSER_VERSION=REVIEWED_PARSER_VERSION
SECTIONS=re.compile(r'^(Жалобы|Анамнез(?: заболевания| жизни)?|Объективный статус|Объективно|Диагноз[^:]*|Клинический диагноз|Заключение[^:]*|Рекомендации[^:]*|Лечение|Назначения|Проведенные обследования|Результаты[^:]*|Описание[^:]*|Осмотр)\s*:',re.I)
NUMBER = rf'(?:{COMPARATOR}\s*)?{SCALAR}(?:{POWER}|{RANGE})'
NUMBER_START = r'(?<![\w.,<>≤≥+−–^×·⋅-])'
UNITS = r'(?:мкмоль/л|ммоль/л|мл/мин(?:/1[.,]73\s*м[²2])?|мм\s*рт\.?\s*ст\.?|г/л|г/сут|мг/л|мг/сут|мл|мм/ч|мм|см|кг|%|10\^?9/л)'
MEASUREMENT = re.compile(rf'{NUMBER_START}(?P<value>{NUMBER})\s*(?P<unit>{UNITS})(?![\w/])', re.I)
DIMENSIONS = re.compile(rf'{NUMBER_START}({SCALAR}(?:\s*[xх×*]\s*{SCALAR}){{1,2}})(?![\d.,^])\s*(мм|см)?', re.I)

DATE_PATTERN=re.compile(r'\b(\d{2}\.\d{2}\.\d{4})\b')


def content_fingerprint(pages):
    # Every character/number remains part of the fingerprint; only whitespace changes.
    text=' '.join(unicodedata.normalize('NFC',' '.join(pages)).split())
    return hashlib.sha256(text.encode()).hexdigest() if text else None


SIDE = re.compile(r'\b(?:с?прав|с?лев)(?:ая|ый|ой|ую|ом|ое|а|о)\b|\bRD\b|\bRS\b', re.I)
KIDNEY_METRICS = frozenset({'Паренхима', 'Лоханка', 'Чашечки', 'Мочеточник', 'Размеры почки'})
METRIC_LABELS = (
    ('Паренхима', r'\bпаренхим|\bпар\b'), ('Лоханка', r'\bлохан|\bчлс\b'), ('Чашечки', r'\bчашеч'),
    ('Мочеточник', r'\bмочеточник'), ('Остаточная моча', r'\bостаточ|после мочеиспуск'),
    ('Объём', r'\bоб[ъь][её]м(?!н)'), ('Креатинин', r'\bкреатинин'), ('Мочевина', r'\bмочевин'),
    ('Гемоглобин', r'\bгемоглоб|\bHb\b'), ('Белок', r'\bбел(?:ок|ка)\b'), ('Рост', r'\bрост\b'),
    ('Масса', r'\bмасса\b|\bвес\b|\bМ='), ('Стенка', r'\bстенк'), ('Клиренс', r'\bклиренс'),
    ('Накопление препарата', r'\bнакоплен'), ('Перфузия', r'\buptake\b|\bперфуз'),
)
# A dot ends a clause after a full word. Shorter words before a dot are abbreviations («толщ.»).
CLAUSE_END = re.compile(r'(?:(?<=[А-Яа-яЁёA-Za-z]{5})\.|[!?;])\s*(?=\S)|,\s+')
COMPARISON = re.compile(r'сравнен|предыдущ|прошл|ранее', re.I)
UNNAMED_METRIC = 'Измерение из описания'
SPACED_SECTIONS = tuple(
    (name, re.compile(spaced(name) + (r'(?![а-яё])' if name.startswith(('Жалобы', 'Анамнез')) else r'\s*:'), re.I))
    for name in ('Жалобы', 'Анамнез заболевания', 'Анамнез жизни', 'Анамнез', 'Объективный статус',
                 'Диагноз', 'Заключение', 'Рекомендации', 'Лечение', 'Назначения')
)


def side_from(text):
    matches = list(SIDE.finditer(text))
    if not matches:
        return ''
    token = matches[-1][0].lower()
    return 'Правая' if 'прав' in token or token == 'rd' else 'Левая'


def current_clause(text):
    return CLAUSE_END.split(text)[-1]


def metric_from(text, default):
    """Name a value only from whole words in its own clause, never from a passing mention."""
    clause = current_clause(text)
    candidates = []
    for label, pattern in METRIC_LABELS:
        hits = list(re.finditer(pattern, clause, re.I))
        # A label followed by an earlier value must not label the next unknown metric.
        if hits and not re.search(r'\d', clause[hits[-1].end():]):
            candidates.append((hits[-1].start(), label))
    return max(candidates)[1] if candidates else default


def section_name(clean):
    marker = SECTIONS.match(clean)
    if marker:
        return marker[1]
    return next((name for name, pattern in SPACED_SECTIONS if pattern.match(clean)), None)


def narrative(pages, day, ocr_pages, kind, table_pages=frozenset()):
    # An unnamed number in a known prose report stays in its text. On a lab or unknown
    # form it may be a table row that was not read, so it goes to review.
    keep_unnamed=kind in LABORATORY_TYPES or kind=='Документ'
    values=[];sections=[]
    for page_no,page in enumerate(pages,1):
        # Laboratory fields are already extracted by the layout parser.
        if any(laboratory_header(line) for line in page.splitlines()):
            continue
        page_day=document_date(page) or day
        section='Описание исследования';section_date=page_day;section_source='Дата документа';context='';anchor=None
        lines=page.splitlines();section_lines=[];section_start=1
        def flush():
            text='\n'.join(section_lines).strip()
            if text:sections.append({'page':page_no,'line':section_start,'section':section,'text':text})
        for index,line in enumerate(lines,1):
            clean=' '.join(line.split())
            if not clean:continue
            heading=section_name(clean)
            if heading:
                flush();section_lines=[];section_start=index;section=heading
                context='';section_date=page_day;section_source='Дата документа';anchor=None
                if re.search('анамнез',section,re.I):section_date=None;section_source='Дата в анамнезе не указана'
            section_lines.append(line)
            if page_no in table_pages or re.search(r'рекомендац|назначен|лечени',section,re.I):continue
            if re.search(r'Дата рождения|(?:^|\s)Адрес|№|ОГРН|телефон|[+]7\s*\(',clean,re.I):continue
            # A date applies until its sentence ends, including lines the sentence wraps onto.
            # A date of comparison belongs to the earlier study, never to these values.
            dates=[match for match in DATE_PATTERN.finditer(clean) if iso_date(match[1]) and not COMPARISON.search(clean[:match.start()])]
            line_day=iso_date(dates[0][1]) if dates else None
            historical=bool(re.search('анамнез',section,re.I))
            fragments=[]
            for match in DIMENSIONS.finditer(clean):
                before=clean[:match.start()]
                side=side_from(context+' '+before)
                if not side or not re.search(r'почк|размер|паренхим|\bRD\b|\bRS\b',context+' '+before,re.I):continue
                # A printed dimension pair is retained as a pair, without assigning axes.
                metric=metric_from(context+' '+before,'Размеры почки')
                if metric not in {'Паренхима','Лоханка','Чашечки','Мочеточник'}:metric='Размеры почки'
                fragments.append((match.start(),match.end(),metric,match[1],match[2] or '',side))
            occupied=[(start,end) for start,end,*_ in fragments]
            for match in MEASUREMENT.finditer(clean):
                if any(start<=match.start()<end for start,end in occupied):continue
                before=clean[:match.start()]
                metric=metric_from(context+' '+before,UNNAMED_METRIC)
                if metric==UNNAMED_METRIC and not keep_unnamed:continue
                side=side_from(context+' '+before) if metric in KIDNEY_METRICS else ''
                fragments.append((match.start(),match.end(),metric,match['value'].strip(),match['unit'],side))
            for start,end,metric,result,unit,side in sorted(fragments):
                local_dates=[match for match in dates if match.end()<start]
                local_day=iso_date(local_dates[-1][1]) if local_dates else line_day or anchor or section_date
                # A number from the history with no date cannot be placed in time. It stays in the text.
                if historical and not local_day:continue
                date_source='Дата в тексте' if line_day or anchor else section_source
                status='Проверить' if page_no in ocr_pages or not local_day or metric==UNNAMED_METRIC else 'Извлечено'
                values.append({'page':page_no,'line':index,'section':section,'label':metric,'result':result,'number':numeric_value(result),
                               'unit':unit,'reference':'','raw':clean,'flag':'','comment':'','side':side,'date':local_day,
                               'date_source':date_source,'origin':'Анамнез' if historical else 'Описание',
                               'status':status,'from_ocr':page_no in ocr_pages})
            context=(context+' '+clean)[-200:]
            anchor=None if re.search(r'[.!?;]$',clean) else (iso_date(dates[-1][1]) if dates else anchor)
        flush()
    return values,sections


def pages_without_patient(pages, names):
    """Pages that open their own study but name nobody, in a file that names a patient elsewhere."""
    if len(pages) < 2 or not names:
        return []
    found = []
    for number, page in enumerate(pages, 1):
        starts_study = document_dates(page) or any(laboratory_header(line) for line in page.splitlines())
        if starts_study and not identity([page])[0]:
            found.append(number)
    return found


def analyze_stored(job):
    """Read a stored extraction again. Runs in a worker process, so it takes and returns plain data.

    None means the stored text cannot be read, and the previous reading stays.
    """
    extracted, filename = job
    try:
        return analyze(json.loads(extracted), filename)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def analyze(extracted, filename):
    pages = extracted['pages']
    ocr_pages = extracted['ocr_pages']
    names, births, cards = identity(pages)
    native = extracted.get('native_pages', [text for i, text in enumerate(pages, 1) if i not in ocr_pages])
    native_names, native_births, _ = identity(native)
    date_candidates = document_dates('\n'.join(pages))
    day = date_candidates[0] if len(date_candidates) == 1 else None
    kind, type_candidates = classify_pages(pages)
    laboratory, pending = laboratory_rows(pages)
    dates_by_page = {}
    for row in laboratory:
        page_index = row['page'] - 1
        if page_index not in dates_by_page:
            dates_by_page[page_index] = document_date(pages[page_index])
        local = dates_by_page[page_index]
        row_day = local or day
        from_ocr = row['page'] in ocr_pages
        row.update(
            side='', date=row_day, date_source='Дата страницы' if local and local != day else 'Дата документа',
            origin='Лабораторный бланк', status='Проверить' if from_ocr or row.get('suspect_reference') else 'Извлечено',
            from_ocr=from_ocr,
        )
    descriptive, sections = narrative(pages, day, ocr_pages, kind, {row['page'] for row in laboratory})
    page_numbers = extracted.get('page_numbers', list(range(1, len(pages) + 1)))
    problems = []
    if not any(text.strip() for text in pages):
        problems.append('В документе не обнаружен читаемый текст')
    table_issues = []
    for page, line, raw in pending:
        physical_page = page_numbers[page - 1]
        table_issues.append({'page': physical_page, 'page_index': page, 'line': line})
        problems.append(f'Страница {physical_page}, строка {line}: проверить строку таблицы — {raw}')
    for item in laboratory + descriptive + sections:
        item['page_index'] = item['page']
        item['page'] = page_numbers[item['page_index'] - 1]
    result = {
        'version': PARSER_VERSION, 'names': names, 'births': births, 'cards': cards, 'date': day,
        'date_candidates': date_candidates, 'page_numbers': page_numbers, 'type': kind,
        'type_candidates': type_candidates, 'observations': laboratory + descriptive,
        'sections': sections, 'problems': problems, 'table_issues': table_issues,
        'identity_from_ocr': names != native_names or births != native_births,
        'pages_without_patient': pages_without_patient(pages, names),
        'pages': pages, 'ocr_pages': ocr_pages,
        'ocr_evidence': extracted.get('ocr_evidence', []),
        'ocr_alternatives': extracted.get('ocr_alternatives', []),
        'extraction_issues': extracted.get('extraction_issues', []),
    }
    result['quality'] = assess(result, extracted)
    return result


def resolve(analyses, known_patients):
    """Return exact identities or review candidates. Folder names never invent identity."""
    keys=set((patient['name'],patient['birth']) for patient in known_patients)
    confirmed=set(keys)
    for item in analyses:
        a=item['analysis']
        if len(a['names'])==1 and len(a['births'])==1 and len(a['names'][0].split())==3 and not a['identity_from_ocr']:
            keys.add((a['names'][0],a['births'][0]))
    compact = defaultdict(set)
    by_name = defaultdict(set)
    by_birth = defaultdict(set)
    for name, birth in keys:
        pair = (name, birth)
        compact[(name.replace(' ', ''), birth)].add(pair)
        by_name[name].add(pair)
        by_birth[birth].add(pair)
    votes=defaultdict(Counter)
    results={}
    for item in analyses:
        a=item['analysis'];key=None;reason='Недостаточно идентификаторов';candidates=[]
        if len(a['names'])>1 or len(a['births'])>1:reason='В документе несколько ФИО или дат рождения. Сверьте все страницы с оригиналом'
        if len(a['names'])==1 and len(a['births'])==1:
            options=compact[(a['names'][0].replace(' ',''),a['births'][0])]
            if len(options)==1:
                key=next(iter(options));reason='ФИО и дата рождения совпадают'
                # A new birth date for a known name is a typo as often as a namesake.
                if key not in confirmed and len(by_name[key[0]])>1:
                    key=None;reason='Такое ФИО уже есть с другой датой рождения. Сверьте оригинал'
        if a['identity_from_ocr']:
            reason='Подтвердите ФИО и дату рождения, распознанные со скана';key=None
        if not key:
            matching = set()
            for name in a['names']:
                matching.update(by_name.get(name, ()))
            for birth in a['births']:
                matching.update(by_birth.get(birth, ()))
            candidates = sorted(matching)
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
