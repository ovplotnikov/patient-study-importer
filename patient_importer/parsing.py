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
BIRTH_DATE_LABEL = r'Дата\s+рожд\w*'
MONTHS = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']


def normalized(value):
    return ' '.join(unicodedata.normalize('NFC', value).upper().replace('Ё', 'Е').split())


def spaced(phrase):
    """Some PDF fonts split words («Жало б ы:»). Match a phrase whatever spaces fall inside it."""
    return r'\s*'.join(re.escape(character) for character in phrase.replace(' ', ''))


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
        for match in re.finditer(rf'^[ \t]*Ф\.?\s*И\.?\s*О\.?\s*пациента[ \t]+({FULL_NAME})[ \t]*$', header, re.M | re.I):
            names.add(normalized(match[1]))
        for match in re.finditer(rf'(?:Сведения о пациенте\s+|ФИО(?: пациента)?, дата рождения(?:\(возраст\))?:?\s*)({FULL_NAME})(?=[\s,;]|$)', header, re.I):
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
        # An empty birth-date field must not borrow a date from the next labeled field.
        for m in re.finditer(rf'{BIRTH_DATE_LABEL}(?:(?!дата)[^\n\d]){{0,100}}({DATE})', header, re.I):
            if iso_date(m[1]):
                births.add(iso_date(m[1]))
        for line in header.splitlines():
            combined = re.search(rf'{NAME_LABEL}\s*:[ \t]*{FULL_NAME}\s*[,(][ \t]*({DATE})',line,re.I)
            if not combined and re.search(r'ФИО(?: пациента)?, дата рождения',line,re.I):
                combined = re.search(rf'\(({DATE})(?:\s*г\.?\s*р\.?)?\)|(?<!\d)({DATE})',line)
                combined = combined and (combined[1] or combined[2])
                if combined and iso_date(combined):
                    births.add(iso_date(combined))
                continue
            if combined and iso_date(combined[1]):
                births.add(iso_date(combined[1]))
        for m in re.finditer(r'(?:№\s*(?:мед\.\s*)?карты|№\s*мед\.\s*карты)\s*:\s*([\w/-]+)', header, re.I):
            cards.add(m[1])
    return sorted(names), sorted(births), sorted(cards)


DOCUMENT_DATE_LABELS = (
    r'Дата выполнения исследования', r'Результат исследования от', r'Выполнено',
    r'Дата и время проведения исследования', r'Дата и время посещения',
    r'Дата выполнения услуги', r'Дата, время исследования', r'Дата исследования', r'Дата проведения(?: исследования)?',
)


def document_dates(text):
    """Collect explicitly labeled dates without substituting DOB or file timestamps."""
    dates = set()
    for label in DOCUMENT_DATE_LABELS:
        # Layout text can print another field to the left, after a column gap.
        for match in re.finditer(rf'(?:^[ \t]*|[ \t]{{2,}}){label}[ :\t]*({DATE})', text, re.I | re.M):
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
    ('УЗИ', r'ультразвуковое исследован|протокол ультразвуков|технические особенности ультразвуков|узи\b'),
    ('МЦУГ', r'(?:микционная )?цистоуретрограф|микционная цистограф|мцуг\b'),
    ('Урография', r'(?:экскреторн|внутривенн).*урограф|урография\b'),
    ('Сцинтиграфия', r'сцинтиграф|динамическая нефросцинтиграф'),
    ('МРТ', r'магнитно-резонанс|мрт\b'),
    ('Цистометрия', r'цистометр'),
    ('Осмотр или выписка', r'(?:выписной )?эпикриз|выписка\b|осмотр(?: врач[а-я]+)?\b|протокол осмотра|[a-zа-я]?[\d.]*\s*при[её]м \(осмотр'),
)
TABLE_FRAGMENT_INDENT = 25
NARRATIVE_WORDS = ('жалобы', 'анамнез болезни', 'анамнез заболевания', 'анамнез жизни', 'анамнез',
                   'рекомендации', 'назначения', 'лечение', 'заключение', 'диагноз', 'описание', 'результаты')
# Fill-in forms print «Жалобы нет:» and «Анамнез жизни, родился…» with text right after the word.
HISTORY_START = rf"(?:{spaced('жалобы')}|{spaced('анамнез')})(?![а-яё])"
NARRATIVE_HEADING = re.compile(rf"^(?:(?:{'|'.join(spaced(word) for word in NARRATIVE_WORDS)})\s*:|{HISTORY_START})", re.I)
HISTORY_HEADING = re.compile(rf'^\s*{HISTORY_START}', re.I | re.M)
SERVICE_LINE = re.compile(r'^\s*услуга\s*:\s*(.+)$', re.I | re.M)
DATED_STUDY = re.compile(rf'\bот\s+{DATE}', re.I)
MENTIONED_STUDY = re.compile(rf'\bот\s+{DATE}\S*\s*[:,-]?\s*\S', re.I)
COLUMN_LABELS = {
    'label': r'(?:показател[ьи]|наименование(?: показателя| исследования)?|параметр|тест|анализ)',
    'result': r'(?:результат(?: исследования)?|значение(?: показателя)?)',
    'unit': r'(?:единиц[аы](?: измерения)?|ед\.?\s*(?:изм\.?)?)',
    'reference': r'(?:референс(?:ные значения|ный интервал)?|реф(?:\.|-ые)? значения|реф\.?\s*интервал|норма|нормальные значения)',
    'flag': r'(?:флаг|отклонение|откл\.?)',
    'comment': r'(?:комментарий|примечание)',
    'test_system': r'тест[- ]система',
    'history': DATE,  # Some forms print an earlier result in a column named by its date.
}
SPARSE_OPTIONAL_COLUMNS = frozenset({'unit', 'reference', 'flag', 'comment', 'test_system', 'history'})
FLAG_GLYPHS = '▲▼↑↓🡱🡳!'


def split_columns(line):
    return re.split(r'\s{2,}|\t+', line.strip())


def column_name(text):
    return next((name for name, pattern in COLUMN_LABELS.items() if re.fullmatch(pattern, text, re.I)), '')


def header_cells(part):
    """Name one header cell. Layout text can lose the gaps between its neighbours."""
    def split(words):
        for index in range(1, len(words) + 1):
            if name := column_name(' '.join(words[:index])):
                rest = split(words[index:]) if index < len(words) else []
                if rest is not None:
                    return [name, *rest]
        return None
    return split(re.sub(r'(?<=\.)(?=[А-ЯЁ])', ' ', part).split()) or ['']


def without_flag_column(columns):
    return columns[1:] if columns[:1] == ['flag'] else columns


def is_sparse_table(columns):
    """Full forms print label and result first and may leave the other cells empty."""
    columns = without_flag_column(columns)
    return len(columns) >= 4 and columns[:2] == ['label', 'result'] and set(columns[2:]) <= SPARSE_OPTIONAL_COLUMNS


def laboratory_header(line):
    """Read ordered header columns, with a conservative fallback for loose labels."""
    columns = [name for part in split_columns(line) for name in header_cells(part)]
    if 'label' in columns and 'result' in columns:
        return columns
    collapsed = ' '.join(line.lower().split())
    loose = re.fullmatch(r'показател[ьи] результат( единицы(?: измерения)?)?( референс(?:ные значения)?)?', collapsed)
    if loose:
        return ['label', 'result'] + (['unit'] if loose[1] else []) + (['reference'] if loose[2] else [])
    return None


def classify_title(heading):
    return next((label for label, pattern in STUDY_HEADINGS if re.match(pattern, heading)), '')


def classify_study(text):
    """Use explicit titles and table structure. Ignore references inside prose."""
    return read_study_titles(text)[0]


def read_study_titles(text):
    """Return the study titles of one page and whether prose started on it."""
    matches = set()

    def read_heading(heading):
        heading = re.sub(r'^(?:(?:наименование|вид|тип) исследования|исследование|услуга)\s*:\s*', '', heading)
        for label, pattern in STUDY_HEADINGS:
            if re.match(pattern, heading):
                matches.add(label)
                break

    # «Услуга:» names the study wherever the form prints it, even below the diagnosis.
    for service in SERVICE_LINE.finditer(text):
        name = ' '.join(service[1].lower().split())
        for label, pattern in STUDY_HEADINGS:
            if re.search(pattern, name):
                matches.add(label)
                break

    lines = text.splitlines()
    # One dated title can be this form's own. Several in a row are a list of earlier results.
    cited_results = sum(1 for line in lines if DATED_STUDY.search(line) and classify_title(' '.join(line.lower().split()))) > 1
    for index, line in enumerate(lines):
        heading = ' '.join(line.lower().strip().split())
        if laboratory_header(line):
            # Full forms print the study title on the first line under the header.
            following = next((candidate for candidate in lines[index + 1:] if candidate.strip()), '')
            if is_table_fragment(following.strip()):
                read_heading(' '.join(following.lower().split()))
            # A new page can carry another explicit title. Stop prose per page.
            break
        if NARRATIVE_HEADING.match(heading):
            return matches, True
        # «ОАМ от 01.02.2020: белок отр.» cites an earlier result. It is not this form's title.
        if not MENTIONED_STUDY.search(heading) and not (cited_results and DATED_STUDY.search(heading)):
            read_heading(heading)
    return matches, False


def opens_new_document(page):
    """A page with its own patient or study date is not a continuation of the prose before it."""
    return bool(identity([page])[0] or document_dates(page))


def classify_pages(pages):
    candidates = set()
    laboratory = False
    narrative = False
    inside_prose = False
    separate_report = False
    for page in pages:
        has_table = any(laboratory_header(line) for line in page.splitlines())
        laboratory |= has_table
        narrative |= bool(HISTORY_HEADING.search(page))
        # Lists of earlier results continue on later pages and look like titles.
        if inside_prose and not has_table and not opens_new_document(page):
            continue
        titles, prose = read_study_titles(page)
        candidates.update(titles)
        separate_report |= inside_prose and bool(titles)
        inside_prose = prose
    ordered = sorted(candidates)
    # A clinical summary with no result table quotes many studies. It is still one summary.
    if len(ordered) > 1 and narrative and not laboratory and not separate_report:
        return 'Осмотр или выписка', ['Осмотр или выписка']
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
    r'мм\.?\s*рт\.?\s*ст\.?|мосм|клет(?:ок|ки)?|копий|кое|п[./]\s*зр\.?|в\s*п[./]\s*зр\.?|ph|'
    r'1(?=\s*/)|'
    r'(?:[×xх*]\s*)?10(?:(?:\^|\*)[0-9]+|[¹²³⁴⁵⁶⁷⁸⁹⁰]+|[EeЕе][0-9]+))'
    r'(?:\s*(?:/|на )\s*(?:[0-9]+(?:[.,][0-9]+)?\s*)?'
    r'(?:мкл|мл|дл|л|ml|dl|ul|µl|μl|l|мин|ч|сут|с|мкг|мг|г|кг|g|мм|см|м|mm|cm|m|п[./]\s*зр\.?|поле\s*зр(?:ения|\.)?)[²³23]?)*$',
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


REFERENCE_CELL = re.compile(rf'(?:{COMPARATOR}\s*{SCALAR}|{SCALAR}\s*[-–]\s*{SCALAR})')
SEDIMENT_LABEL = re.compile(r'лейкоцит|эритроцит|эпители|цилиндр|бактери', re.I)


def sparse_fields(cells, columns):
    """Read a full-form row by cell content. Cells follow the header order but may be empty."""
    label, tail, *rest = cells
    tail = tail.lstrip(FLAG_GLYPHS + ' ')
    result, number, inline_unit, inline_reference = result_fields(tail)
    unit, reference, comments = '', '', []
    for cell in rest:
        if not reference and not comments and (cell.startswith('(') or REFERENCE_CELL.fullmatch(cell)):
            reference = cell
        elif not unit and not reference and not comments and 'unit' in columns:
            bracket = cell.find('(')
            unit, reference = (cell[:bracket].strip(), cell[bracket:].strip()) if bracket > 0 else (cell, '')
        else:
            comments.append(cell)
    # A discrepancy between a cell and the inline reading must be reviewed.
    if (inline_unit and unit and unit != inline_unit) or (inline_reference and reference and reference != inline_reference):
        return None
    # With an empty result cell the printed norm slides into its place. Cells carry no position here.
    suspect = bool('reference' in columns and not (reference or inline_reference)
                   and REFERENCE_CELL.fullmatch(tail.strip()) and not SEDIMENT_LABEL.search(label))
    return label, result, number, unit or inline_unit, reference or inline_reference, ' '.join(comments), suspect


def laboratory_row(line, columns):
    parts = split_columns(line)
    suspect_reference = False
    sparse = is_sparse_table(columns)
    if sparse or columns[:1] == ['flag']:
        # Arrows are marks, whether in their own column or printed in a gap.
        parts = [part for part in parts if part.strip(FLAG_GLYPHS)]
        if not parts:
            return None
        columns = without_flag_column(columns)
    cells = parts
    if sparse and 2 <= len(cells) <= len(columns):
        fields = sparse_fields(cells, columns)
        if fields is None:
            return None
        label, result, number, unit, reference, comment, suspect_reference = fields
        flags = ''
    elif len(parts) == len(columns):
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
        flags = ''.join(re.findall(f'[{FLAG_GLYPHS}]', line))
    return {
        'label': label.lstrip(FLAG_GLYPHS + ' '),
        'result': result, 'number': number, 'unit': unit, 'reference': reference,
        'flag': flags, 'comment': comment, 'unit_column': 'unit' in columns,
        'unmapped_columns': '' in columns, 'suspect_reference': suspect_reference,
    }


def is_table_fragment(raw):
    """A wrapped cell or a table title: one cell of words without a standalone value."""
    return (len(split_columns(raw)) == 1 and bool(re.search(r'[А-Яа-яЁёA-Za-z]', raw)) and not raw.endswith(':')
            and not re.search(rf'(?:^|\s){SCALAR}(?:\s|$)', raw))


def join_wrapped(text, continuation):
    return text[:-1] + continuation if text.endswith('-') else f'{text} {continuation}'


def is_measured_row(item):
    """Outside the printed table only a number with a unit or a range counts as a result."""
    return bool(re.match(rf'^(?:{COMPARATOR}\s*)?{SCALAR}', item['result']) and (item['unit'] or item['reference']))


def laboratory_rows(pages):
    rows, pending = [], []
    carried = None
    for page_number, page in enumerate(pages, 1):
        # A table can run onto a page that does not repeat its header. Until a real
        # row confirms that, the text there is not treated as part of the table.
        columns, cautious, section, comment_at, last_row_line, label_at = carried, carried is not None, '', None, 0, 0
        held = []
        page_lines = page.splitlines()
        for line_number, line in enumerate(page_lines, 1):
            line = line.replace('\x00', '!')  # Some PDF fonts export the deviation mark as a null.
            if header := laboratory_header(line):
                columns = header
                cautious = False
                position = line.lower().find('комментарий')
                comment_at = position if position >= 0 else None
                last_row_line = 0
                continue
            if columns is None or not line.strip():
                continue
            if re.search(r'^\s*Комментарий:', line, re.I):
                # Results can continue under a comment printed in the middle of the table.
                cautious = True
                continue
            if re.search(r'^\s*(?:Подпись|Исполнители:|ДОКУМЕНТ ПОДПИСАН|СВЕДЕНИЯ О СЕРТИФИКАТЕ|Заключение:)', line, re.I):
                columns = None
                continue
            if re.match(r'\s*\d{2}\.\d{2}\.\d{4}', line):
                columns = None
                continue
            if re.match(r'^\s*Дата(?:[ :]|$)', line):
                columns = None
                if not cautious:
                    pending.append([page_number, line_number, line.strip()])
                continue
            raw = line.strip()
            if re.match(r'^(?:Общий.*анализ (?:крови|мочи)|Исследование мочи методом|Анализ крови биохимический)\b', raw, re.I):
                section = raw
                continue
            previous = rows[-1] if rows and rows[-1]['page'] == page_number else None
            item = laboratory_row(line, columns)
            if cautious:
                if not item or not is_measured_row(item):
                    if item:
                        held.append([page_number, line_number, raw])
                    continue
                # The table does go on here, so the rows passed over must not vanish.
                pending.extend(held)
                held = []
                cautious = False
            if item:
                if (is_sparse_table(columns) and previous and line_number == last_row_line + 1
                        and previous['result'].upper() == 'НЕ' and item['result'].upper().startswith('ОБНАРУЖ')):
                    # A two-word result wrapped together with its label.
                    previous['label'] += ' ' + item['label']
                    previous['result'] += ' ' + item['result']
                    previous['raw'] += '\n' + raw
                    last_row_line = line_number
                    continue
                rows.append({'page': page_number, 'line': line_number, 'section': section, 'raw': raw, **item})
                last_row_line = line_number
                label_at = line.find(item['label'])
                continue
            if (previous and previous['reference'].count('(') > previous['reference'].count(')')
                    and len(line) - len(line.lstrip()) > 25 and re.fullmatch(r'[\d\s.,;<>≤≥+−–-]*\)', raw)):
                previous['reference'] += ' ' + raw
                previous['raw'] += '\n' + raw
                last_row_line = line_number
                continue
            indent = len(line) - len(line.lstrip())
            adjacent = previous is not None and line_number == last_row_line + 1
            if is_sparse_table(columns) and raw.endswith(':') and not re.search(r'\d', raw):
                # With rows right below it, this is a group title. A lone note stays for review.
                following = next((candidate for candidate in page_lines[line_number:] if candidate.strip()), '')
                if laboratory_row(following, columns):
                    section = raw[:-1]
                    continue
            if is_sparse_table(columns) and is_table_fragment(raw):
                if adjacent and previous['reference'].count('(') > previous['reference'].count(')'):
                    previous['reference'] = join_wrapped(previous['reference'], raw)
                    previous['raw'] += '\n' + raw
                    last_row_line = line_number
                    continue
                if adjacent and previous['result'].endswith('-') and re.search(r'[А-Яа-яЁёA-Za-z]', previous['result']):
                    previous['result'] = join_wrapped(previous['result'], raw)
                    previous['raw'] += '\n' + raw
                    last_row_line = line_number
                    continue
                if comment_at is not None and indent >= comment_at - 2 and adjacent:
                    previous['comment'] = f"{previous['comment']} {raw}".strip()
                    previous['raw'] += '\n' + raw
                    last_row_line = line_number
                    continue
                # A wrapped label keeps the label's margin. Centered group titles do not.
                if indent > TABLE_FRAGMENT_INDENT or last_row_line == 0 or indent > label_at + 5:
                    section = raw
                    continue
                if adjacent:
                    previous['label'] += ' ' + raw
                    previous['raw'] += '\n' + raw
                    last_row_line = line_number
                    continue
            # An unparsed result may look like a continuation but contain a
            # different analyte or value. Preserve it as a review item.
            pending.append([page_number, line_number, raw])
        carried = None if cautious else columns
    return rows, pending
