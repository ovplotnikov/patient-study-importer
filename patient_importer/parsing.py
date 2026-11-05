"""Deterministic extraction from Russian clinical document text."""
import collections
from datetime import datetime
import hashlib
import re
import unicodedata

DATE = r'\d{2}\.\d{2}\.\d{4}'
WORD = r'[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)*'
FULL_NAME = rf'{WORD}\s+{WORD}\s+{WORD}'
NAME_LABEL = r'(?:Пациент(?:ка)?|Ф\.?\s*И\.?\s*О\.?\s*(?:пациента)?|Фамилия, имя, отчество(?: \(при наличии\))?)'
MONTHS = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']


def normalized(value):
    return ' '.join(unicodedata.normalize('NFC', value).upper().replace('Ё', 'Е').split())


def identifier(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:20]


def iso_date(value):
    try:
        return datetime.strptime(value, '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def identity(pages):
    """Use patient header labels, never doctor names or names inside narrative."""
    names, births, cards = set(), set(), set()
    for page in pages:
        # Standalone signature pages identify the signer, not the patient.
        certificate=bool(re.search(r'сертификат|электронн.{0,20}подпис',page,re.I|re.S))
        clinical=bool(re.search(r'пациент|дата\s+рождения|показател|результат|фамилия|заключени|диагноз',page,re.I))
        if certificate and not clinical:continue
        header = '\n'.join(line for line in page.splitlines() if line.strip())
        header = '\n'.join(header.splitlines()[:45])
        for match in re.finditer(rf'^[ \t]*{NAME_LABEL}[ \t]*:[ \t]*([^\n]*)', header, re.M | re.I):
            fragment = re.split(r'[,;()]|[ \t]{8,}|\b(?:Дата|Срочная|Возраст|Пол)\b|\d',match[1],maxsplit=1,flags=re.I)[0].strip()
            if re.fullmatch(rf'{WORD}(?:[ \t]+{WORD}){{2,5}}',fragment):
                names.add(normalized(fragment))
        for match in re.finditer(rf'(?:Сведения о пациенте|ФИО, дата рождения\(возраст\):)\s+({FULL_NAME})(?=[\s,;]|$)', header, re.I):
            names.add(normalized(match[1]))
        gender_name = re.search(rf'^\s*({FULL_NAME})\s*\((?:Жен|Муж)\)', header, re.M | re.I)
        if gender_name:
            names.add(normalized(gender_name[1]))
        pieces = []
        for label in ['Фамилия', 'Имя', r'Отчество(?: \(при наличии\))?']:
            m = re.search(rf'^\s*{label}[ \t]+({WORD})[ \t]*$', header, re.M | re.I)
            pieces.append(m[1] if m else '')
        if all(pieces):
            names.add(normalized(' '.join(pieces)))
        for m in re.finditer(rf'Дата рождения[^\n\d]{{0,100}}({DATE})', header, re.I):
            if iso_date(m[1]):
                births.add(iso_date(m[1]))
        for line in header.splitlines():
            combined = re.search(rf'Пациент\s*:[ \t]*{FULL_NAME},[ \t]*({DATE})',line,re.I)
            if not combined and re.search(r'ФИО, дата рождения',line,re.I):
                combined = re.search(rf'\(({DATE})\)',line)
            if combined and iso_date(combined[1]):
                births.add(iso_date(combined[1]))
        for m in re.finditer(r'(?:№\s*(?:мед\.\s*)?карты|№\s*мед\.\s*карты)\s*:\s*([\w/-]+)', header, re.I):
            cards.add(m[1])
    return sorted(names), sorted(births), sorted(cards)


def document_date(text):
    labels = [r'Дата выполнения исследования', r'Результат исследования от', r'Выполнено',
              r'Дата и время проведения исследования', r'Дата и время посещения', r'Дата выполнения услуги',
              r'Дата, время исследования']
    for label in labels:
        m = re.search(rf'{label}[ :\t]*({DATE})', text, re.I)
        if m:
            return iso_date(m[1])
    m = re.search(r'Дата выполнения услуги:\s*(\d{1,2})\s+(\w+)\s+(\d{4})', text, re.I)
    if m and m[2].lower() in MONTHS:
        return iso_date(f'{int(m[1]):02}.{MONTHS.index(m[2].lower())+1:02}.{m[3]}')
    return None


def study_type(text, filename):
    head = text[:3000].lower()
    choices = [
        ('Зимницкий', r'зимницк'), ('Нечипоренко', r'нечипоренко'),
        ('Реберг', r'реберг'), ('ОАК', r'общий.*анализ крови|гематолог'),
        ('ОАМ', r'общий.*анализ мочи|общеклинические \(моча\)|физические св-ва мочи'),
        ('Биохимия крови', r'биохими|уровня (?:креатинина|мочевины) в крови'),
        ('Белок мочи', r'белка в моче|белок мочи'),
        ('Посев мочи', r'микрофлор|бактериолог'),
        ('УЗДГ', r'допплер|уздг|почечных артерий'),
        ('УЗИ', r'ультразвуков|узи'), ('МЦУГ', r'цистоуретрограф|микционная цистограф'),
        ('Урография', r'экскреторн.*урограф'), ('Сцинтиграфия', r'сцинтиграф'),
        ('МРТ', r'магнитно-резонанс'), ('Цистометрия', r'цистометр'),
    ]
    if 'Показатель' not in text and re.search(r'жалобы|анамнез|выписной эпикриз', head):
        return 'Осмотр или выписка'
    for label, pattern in choices:
        if re.search(pattern, head):
            return label
    return 'Документ'


def result_fields(tail):
    """Retain exact value and units. Inequalities are not converted to exact numbers."""
    reference = ''
    match = re.search(r'\(', tail)
    if match:
        reference = tail[match.start():].strip()
        tail = tail[:match.start()].strip()
    numeric = re.match(r'^([<>≤≥]=?\s*)?([-+]?\d+(?:[.,]\d+)?)(?=\s|$)', tail)
    if numeric:
        result = numeric[0]
        number = None if numeric[1] else float(numeric[2].replace(',', '.'))
        unit = tail[numeric.end():].strip()
        return result, number, unit, reference
    chunks = re.split(r'\s{2,}', tail.strip(), maxsplit=1)
    return chunks[0], None, chunks[1] if len(chunks)>1 else '', reference


def laboratory_rows(pages):
    rows, pending = [], []
    for page_number, page in enumerate(pages, 1):
        active, section = False, ''
        last_line = 0
        for line_number, line in enumerate(page.splitlines(), 1):
            if 'Показатель' in line and 'Результат' in line:
                active = True
                continue
            if not active or not line.strip():
                continue
            if re.search(r'Комментарий:|Подпись|Исполнители:|ДОКУМЕНТ ПОДПИСАН|СВЕДЕНИЯ О СЕРТИФИКАТЕ', line, re.I):
                active = False
                continue
            if re.match(r'\s*\d{2}\.\d{2}\.\d{4}', line):
                active = False
                continue
            parts = re.split(r'\s{2,}', line.strip(), maxsplit=1)
            indent = len(line) - len(line.lstrip())
            if parts[0] == 'Дата':
                active = False
                pending.append([page_number,line_number,line.strip()])
                continue
            if len(parts) == 2 and re.search(r'[А-Яа-яA-Za-z]', parts[0]):
                tail = parts[1]
                flags = ''.join(re.findall(r'[▲▼↑↓]',parts[0]+' '+tail))
                tail = re.sub(r'^[▲▼↑↓\s]+','',tail)
                label = re.sub(r'^[▲▼↑↓\s]+','',parts[0])
                result, number, unit, reference = result_fields(tail)
                units = re.split(r'\s{2,}',unit,maxsplit=1)
                unit = units[0]
                comment = units[1] if len(units)>1 else ''
                if rows and rows[-1]['page']==page_number and line_number==last_line+1 and rows[-1]['result'].upper()=='НЕ' and result.upper().startswith('ОБНАРУЖЕН'):
                    rows[-1]['label'] += ' '+label
                    rows[-1]['result'] += ' '+result
                    rows[-1]['raw'] += '\n'+line.strip()
                    last_line = line_number
                    continue
                rows.append({'page': page_number, 'line': line_number, 'section': section,
                             'label': label, 'result': result, 'number': number,
                             'unit': unit, 'reference': reference, 'raw': line.strip(), 'flag':flags,'comment':comment})
                last_line = line_number
            elif rows and rows[-1]['page']==page_number and rows[-1]['reference'].count('(')>rows[-1]['reference'].count(')') and indent>25:
                rows[-1]['reference'] += ' '+line.strip()
                rows[-1]['raw'] += '\n'+line.strip()
            elif indent > 25:
                section = line.strip()
            elif rows and rows[-1]['page'] == page_number and line_number == last_line+1:
                rows[-1]['label'] += ' ' + line.strip()
                rows[-1]['raw'] += '\n' + line.strip()
                last_line = line_number
            else:
                pending.append([page_number, line_number, line.strip()])
    return rows, pending

