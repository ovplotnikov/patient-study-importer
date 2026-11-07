"""Deterministic extraction from Russian clinical document text."""
from datetime import datetime
import hashlib
import math
import re
import unicodedata

DATE = r'\d{2}\.\d{2}\.\d{4}'
SCALAR = r'[-+−]?\d+(?:[.,]\d+)?(?:[eE][-+−]?\d+)?'
POWER = r'\s*[×·⋅]\s*10\^[-+−]?\d+'
RANGE = rf'(?:\s*[-–]\s*{SCALAR})*'
COMPARATOR = r'(?:[<>]=?|[≤≥])'
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


DOCUMENT_DATE_LABELS = (
    r'Дата выполнения исследования', r'Результат исследования от', r'Выполнено',
    r'Дата и время проведения исследования', r'Дата и время посещения',
    r'Дата выполнения услуги', r'Дата, время исследования', r'Дата исследования', r'Дата проведения исследования',
)


def document_dates(text):
    """Collect explicitly labeled dates without substituting DOB or file timestamps."""
    dates = set()
    for label in DOCUMENT_DATE_LABELS:
        for match in re.finditer(rf'^\s*{label}[ :\t]*({DATE})', text, re.I | re.M):
            if value := iso_date(match[1]):
                dates.add(value)
    for match in re.finditer(r'^\s*Дата выполнения услуги:[ \t]*(\d{1,2})\s+(\w+)\s+(\d{4})', text, re.I | re.M):
        if match[2].lower() in MONTHS:
            value = iso_date(f'{int(match[1]):02}.{MONTHS.index(match[2].lower())+1:02}.{match[3]}')
            if value:
                dates.add(value)
    return sorted(dates)


def document_date(text):
    dates = document_dates(text)
    return dates[0] if len(dates) == 1 else None


LABORATORY_TYPES = frozenset({
    'Зимницкий', 'Нечипоренко', 'Реберг', 'ОАК', 'ОАМ', 'Биохимия крови',
    'Белок мочи', 'Посев мочи', 'Лабораторное исследование',
})
STUDY_HEADINGS = (
    ('Зимницкий', r'(?:проба|анализ мочи) по зимницк|проба зимницк|зимницкий\b'),
    ('Нечипоренко', r'(?:исследование мочи методом|анализ мочи по|проба по) нечипоренко|нечипоренко\b'),
    ('Реберг', r'(?:проба|тест)(?: по)? реберг|реберг\b'),
    ('ОАК', r'общий[\w ()-]*анализ крови|гематологическ|оак\b'),
    ('ОАМ', r'общий[\w ()-]*анализ мочи|общеклинические \(моча\)|физические св-ва мочи|оам\b'),
    ('Биохимия крови', r'биохими|анализ крови биохимический|(?:исследование|определение) уровня (?:креатинина|мочевины) в крови'),
    ('Белок мочи', r'(?:определение|исследование)(?: уровня)? белка в моче|белок мочи'),
    ('Посев мочи', r'(?:посев|бактериологическое исследование) мочи|микрофлора мочи'),
    ('УЗДГ', r'уздг\b|ультразвуковая допплерограф|допплерограф'),
    ('УЗИ', r'ультразвуковое исследован|узи\b'),
    ('МЦУГ', r'цистоуретрограф|микционная цистограф|мцуг\b'),
    ('Урография', r'экскреторн.*урограф|урография\b'),
    ('Сцинтиграфия', r'сцинтиграф|динамическая нефросцинтиграф'),
    ('МРТ', r'магнитно-резонанс|мрт\b'),
    ('Цистометрия', r'цистометр'),
    ('Осмотр или выписка', r'(?:выписной )?эпикриз|выписка\b|осмотр(?: врач[а-я]+)?\b|протокол осмотра'),
)
NARRATIVE_HEADING = re.compile(r'^(?:жалобы|анамнез(?: болезни| заболевания| жизни)?|рекомендации|назначения|лечение|заключение|диагноз|описание|результаты)\s*:', re.I)
COLUMN_LABELS = {
    'label': r'(?:показател[ьи]|наименование(?: показателя| исследования)?|параметр|тест|анализ)',
    'result': r'(?:результат(?: исследования)?|значение(?: показателя)?)',
    'unit': r'(?:единиц[аы](?: измерения)?|ед\.?\s*(?:изм\.?)?)',
    'reference': r'(?:референс(?:ные значения|ный интервал)?|реф\.? значения|норма|нормальные значения)',
    'flag': r'(?:флаг|отклонение)',
    'comment': r'(?:комментарий|примечание)',
}


def split_columns(line):
    return re.split(r'\s{2,}|\t+', line.strip())


def laboratory_header(line):
    """Read ordered header columns, with a conservative fallback for loose labels."""
    parts = split_columns(line)
    columns = [
        next((name for name, pattern in COLUMN_LABELS.items() if re.fullmatch(pattern, part, re.I)), '')
        for part in parts
    ]
    if 'label' in columns and 'result' in columns:
        return columns
    collapsed = ' '.join(line.lower().split())
    loose = re.fullmatch(r'показател[ьи] результат( единицы(?: измерения)?)?( референс(?:ные значения)?)?', collapsed)
    if loose:
        return ['label', 'result'] + (['unit'] if loose[1] else []) + (['reference'] if loose[2] else [])
    return None


def classify_study(text):
    """Use explicit titles and table structure. Ignore references inside prose."""
    matches = set()
    for line in text.splitlines():
        heading = ' '.join(line.lower().strip().split())
        if laboratory_header(line) or NARRATIVE_HEADING.match(heading):
            # A new page can carry another explicit title. Stop prose per page.
            break
        heading = re.sub(r'^(?:(?:наименование|вид|тип) исследования|исследование|услуга)\s*:\s*', '', heading)
        for label, pattern in STUDY_HEADINGS:
            if re.match(pattern, heading):
                matches.add(label)
                break
    return matches


def classify_pages(pages):
    candidates = set()
    laboratory = False
    narrative = False
    for page in pages:
        candidates.update(classify_study(page))
        laboratory |= any(laboratory_header(line) for line in page.splitlines())
        narrative |= bool(re.search(r'^\s*(?:жалобы|анамнез(?: заболевания| жизни)?)\s*:', page, re.I | re.M))
    ordered = sorted(candidates)
    if len(ordered) > 1:
        return 'Документ', ordered
    if ordered:
        return ordered[0], ordered
    if laboratory:
        return 'Лабораторное исследование', []
    if narrative:
        return 'Осмотр или выписка', []
    return 'Документ', []


def study_type(text, filename):
    return classify_pages([text])[0]


KNOWN_UNIT = re.compile(
    r'^(?:%|‰|фл|пг|fL|pg|сек|с|мин|ч|сут|ед(?:иниц)?\.?|ме|iu|балл(?:а|ов)?|'
    r'(?:мк|н|м|к|д|с)?(?:г|моль|л|м)|(?:m|u|n|k|d|c)?(?:g|mol|l|m)|'
    r'мм\s*рт\.?\s*ст\.?|клет(?:ок|ки)?|копий|кое|п\.\s*зр\.|в\s*п\.\s*зр\.|'
    r'(?:[×xх*]\s*)?10(?:(?:\^|\*)[0-9]+|[¹²³⁴⁵⁶⁷⁸⁹⁰]+))'
    r'(?:\s*(?:/|на )\s*(?:[0-9]+(?:[.,][0-9]+)?\s*)?'
    r'(?:мкл|мл|дл|л|ml|dl|ul|µl|μl|l|мин|ч|сут|с|мкг|мг|г|кг|g|мм|см|м|mm|cm|m|п\.\s*зр\.)[²³23]?)*$',
    re.I,
)


def numeric_value(value):
    """A numeric cell represents one finite scalar, with no inferred signs."""
    if not re.fullmatch(SCALAR, value.strip()):
        return None
    number = float(value.replace(',', '.').replace('−', '-'))
    if not math.isfinite(number):
        return None
    if number == 0 and re.search('[1-9]', re.split('[eE]', value)[0]):
        return None
    return number


def result_fields(tail):
    """Retain exact value and units. Inequalities are not converted to exact numbers."""
    reference = ''
    match = re.search(r'\(', tail)
    if match:
        reference = tail[match.start():].strip()
        tail = tail[:match.start()].strip()
    numeric = re.match(rf'^({COMPARATOR}\s*)?({SCALAR})({POWER}|{RANGE})(?=\s|$)', tail)
    if numeric:
        result = numeric[0]
        number = None if numeric[1] or numeric[3] else numeric_value(numeric[2])
        unit = tail[numeric.end():].strip()
        # Two separate numbers without a labeled reference column are ambiguous.
        extra_numbers = re.match(rf'^{SCALAR}(?:[ \t]+{SCALAR})*(?=\s|$)', unit)
        if extra_numbers:
            result += ' ' + extra_numbers[0]
            number = None
            unit = unit[extra_numbers.end():].strip()
        return result, number, unit, reference
    chunks = re.split(r'\s{2,}', tail.strip(), maxsplit=1)
    return chunks[0], None, chunks[1] if len(chunks)>1 else '', reference


def laboratory_row(line, columns):
    parts = split_columns(line)
    if len(parts) == len(columns):
        values = dict(zip(columns, parts))
        if len(values) != len(columns):
            return None
        label, tail = values['label'], values['result']
        tail = re.sub(r'^[▲▼↑↓\s]+', '', tail)
        result, number, inline_unit, inline_reference = result_fields(tail)
        # A discrepancy between columns and the inline reading must be reviewed.
        unit = values.get('unit', inline_unit)
        reference = values.get('reference', inline_reference)
        if (inline_unit and unit != inline_unit) or (inline_reference and reference != inline_reference):
            return None
        comment = values.get('comment', '')
        flags = values.get('flag', '')
    elif columns in (['label', 'result'], ['label', 'result', 'unit']) and len(parts) >= 2:
        # In layout text, a result may share a column gap with its units.
        # The legacy header also omits the optional reference in parentheses.
        label, tail = re.split(r'\s{2,}|\t+', line.strip(), maxsplit=1)
        tail = re.sub(r'^[▲▼↑↓\s]+', '', tail)
        result, number, unit, reference = result_fields(tail)
        unit_parts = split_columns(unit)
        unit, comment = unit_parts[0], ' '.join(unit_parts[1:])
        flags = ''
    else:
        return None
    # With a missing gap, the measured value can become part of the label and
    # a reference range can slide into the result column. Standalone numbers
    # are ambiguous here, unlike identifiers such as Т3 and B12.
    label_tail = re.search(rf'\s{SCALAR}(?:\s+(?P<unit>.+))?$', label)
    if (label_tail and re.match(rf'^(?:{COMPARATOR}\s*)?{SCALAR}', result)
            and (not label_tail['unit'] or KNOWN_UNIT.fullmatch(label_tail['unit']))):
        return None
    if (not re.search(r'[А-Яа-яЁёA-Za-z]', label)
            or re.match(r'^(?:№|номер(?: документа| исследования)?|дата|референс\w*|норма|границы нормы)(?:[: .]|$)', label, re.I)
            or not result.strip()
            or re.fullmatch(DATE, result)
            or KNOWN_UNIT.fullmatch(result)
            or re.fullmatch(r'[-+−]?\d+(?:[.,]\d+)?', unit)):
        return None
    if not flags:
        flags = ''.join(re.findall(r'[▲▼↑↓]', line))
    return {
        'label': re.sub(r'^[▲▼↑↓\s]+', '', label),
        'result': result, 'number': number, 'unit': unit, 'reference': reference,
        'flag': flags, 'comment': comment, 'unit_column': 'unit' in columns,
        'unmapped_columns': '' in columns,
    }


def laboratory_rows(pages):
    rows, pending = [], []
    for page_number, page in enumerate(pages, 1):
        columns, section = None, ''
        for line_number, line in enumerate(page.splitlines(), 1):
            if header := laboratory_header(line):
                columns = header
                continue
            if columns is None or not line.strip():
                continue
            if re.search(r'^\s*(?:Комментарий:|Подпись|Исполнители:|ДОКУМЕНТ ПОДПИСАН|СВЕДЕНИЯ О СЕРТИФИКАТЕ|Заключение:)', line, re.I):
                columns = None
                continue
            if re.match(r'\s*\d{2}\.\d{2}\.\d{4}', line):
                columns = None
                continue
            if re.match(r'^\s*Дата(?:[ :]|$)', line):
                columns = None
                pending.append([page_number, line_number, line.strip()])
                continue
            raw = line.strip()
            if re.match(r'^(?:Общий.*анализ (?:крови|мочи)|Исследование мочи методом|Анализ крови биохимический)\b', raw, re.I):
                section = raw
                continue
            if item := laboratory_row(line, columns):
                rows.append({'page': page_number, 'line': line_number, 'section': section, 'raw': raw, **item})
                continue
            previous = rows[-1] if rows and rows[-1]['page'] == page_number else None
            if (previous and previous['reference'].count('(') > previous['reference'].count(')')
                    and len(line) - len(line.lstrip()) > 25 and re.fullmatch(r'[\d\s.,;<>≤≥+−–-]*\)', raw)):
                previous['reference'] += ' ' + raw
                previous['raw'] += '\n' + raw
            else:
                # An unparsed result may look like a continuation but contain a
                # different analyte or value. Preserve it as a review item.
                pending.append([page_number, line_number, raw])
    return rows, pending
