"""Doctor-facing sheets: one row per study, one sheet per kind, and each patient over the years.

Values are carried over as printed. Nothing is converted between units and nothing is interpreted:
a value in an unexpected unit gets its own column instead of joining the usual one.
"""
from collections import Counter, defaultdict
from datetime import date
import hashlib
import re

COMMON = ['RowID', 'Пациент', 'Дата рождения', 'Дата исследования', 'Возраст, лет', 'Возраст, мес', 'Статус']
TAIL = ['Файл']
LAB_ORIGINS = frozenset({'Лабораторный бланк', 'Ручной ввод', 'Введено при сверке'})
OTHER_SHEET = 'Другие анализы'

COUNT_9 = ('10^9/л', '10^3/мкл', 'тыс/мкл')
COUNT_12 = ('10^12/л', '10^6/мкл', 'млн/мкл')
MICROMOL = ('мкмоль/л',)
MILLIMOL = ('ммоль/л',)
ENZYME = ('ед/л', 'u/l', 'ме/л')

# Order matters: the first match wins, so compound names come before the words inside them.
BLOOD_COUNT = (
    ('СОЭ (мм/ч)', r'\bсоэ\b|скорост\w+ оседания', ('мм/ч',)),
    ('HbA1c (%)', r'гликир|hba1c', ('%',)),
    ('MCHC (г/л)', r'\bmchc\b|средняя концентрация гемоглобина', ('г/л',)),
    ('MCH (пг)', r'\bmch\b|среднее содержание гемоглобина', ('пг', 'pg')),
    ('MCV (фл)', r'\bmcv\b|средний объ[её]м эритроцит', ('фл', 'fl')),
    ('RDW-SD (фл)', r'rdw-?\s*sd|-\s*sd\b', ('фл', 'fl')),
    ('RDW-CV (%)', r'rdw-?\s*cv|\brdw\b|ширина распределения эритроцитов', ('%',)),
    ('MPV (фл)', r'\bmpv\b|средний объ[её]м тромбоцит', ('фл', 'fl')),
    ('PDW', r'\bpdw\b|ширина распределения тромбоцитов', None),
    ('PCT (%)', r'\bpct\b|тромбокрит', ('%',)),
    ('Незрелые гранулоциты (%)', r'незрел', ('%',)),
    ('Незрелые гранулоциты (#)', r'незрел', COUNT_9),
    ('Гранулоциты (%)', r'гранулоцит|\bgra', ('%',)),
    ('Гранулоциты (#)', r'гранулоцит|\bgra', COUNT_9),
    ('Нейтрофилы палочкоядерные (%)', r'палочкоядерн', ('%',)),
    ('Нейтрофилы сегментоядерные (%)', r'сегментоядерн', ('%',)),
    ('Нейтрофилы (%)', r'нейтрофил|\bneu', ('%',)),
    ('Нейтрофилы (#)', r'нейтрофил|\bneu', COUNT_9),
    ('Лимфоциты (%)', r'лимфоцит|\blym', ('%',)),
    ('Лимфоциты (#)', r'лимфоцит|\blym', COUNT_9),
    ('Моноциты (%)', r'моноцит|\bmon', ('%',)),
    ('Моноциты (#)', r'моноцит|\bmon', COUNT_9),
    ('Эозинофилы (%)', r'эозинофил|\beos', ('%',)),
    ('Эозинофилы (#)', r'эозинофил|\beos', COUNT_9),
    ('Базофилы (%)', r'базофил|\bbas', ('%',)),
    ('Базофилы (#)', r'базофил|\bbas', COUNT_9),
    ('WBC (10^9/л)', r'лейкоцит|\bwbc\b', COUNT_9),
    ('RBC (10^12/л)', r'эритроцит|\brbc\b', COUNT_12),
    ('HGB (г/л)', r'гемоглобин|\bhgb\b|\bhb\b', ('г/л',)),
    ('HCT (%)', r'гематокрит|\bhct\b', ('%',)),
    ('PLT (10^9/л)', r'тромбоцит|\bplt\b', COUNT_9),
)
BIOCHEMISTRY = (
    ('Белок общий (г/л)', r'общ\w+ бел|бел\w+ общ', ('г/л',)),
    ('Альбумин (г/л)', r'\bальбумин', ('г/л',)),
    ('Мочевая кислота (мкмоль/л)', r'мочев\w+ кислот', MICROMOL),
    ('Мочевина (ммоль/л)', r'мочевин', MILLIMOL),
    ('Креатинин (мкмоль/л)', r'креатинин', MICROMOL),
    ('Билирубин непрямой (мкмоль/л)', r'непрям', MICROMOL),
    ('Билирубин прямой (мкмоль/л)', r'билирубин прям|прям\w+ билирубин', MICROMOL),
    ('Билирубин общий (мкмоль/л)', r'билирубин', MICROMOL),
    ('АЛТ (Ед/л)', r'аланин|\bалт\b|\balt\b', ENZYME),
    ('АСТ (Ед/л)', r'аспартат|\bаст\b|\bast\b', ENZYME),
    ('ГГТ (Ед/л)', r'гамма|\bггт\b|\bggt\b', ENZYME),
    ('ЩФ (Ед/л)', r'щелочн|\bщф\b', ENZYME),
    ('Глюкоза (ммоль/л)', r'глюкоз', MILLIMOL),
    ('СРБ (мг/л)', r'[cс]-?\s*реактивн|\bсрб\b', ('мг/л',)),
    ('Калий (ммоль/л)', r'\bкали[йя]\b', MILLIMOL),
    ('Натрий (ммоль/л)', r'\bнатри[йя]\b', MILLIMOL),
    ('Кальций ионизированный (ммоль/л)', r'ионизир', MILLIMOL),
    ('Кальций (ммоль/л)', r'кальци', MILLIMOL),
    ('Фосфор (ммоль/л)', r'фосфор|фосфат', MILLIMOL),
    ('Магний (ммоль/л)', r'магни', MILLIMOL),
    ('Хлор (ммоль/л)', r'\bхлор', MILLIMOL),
    ('Железо (мкмоль/л)', r'\bжелезо\b', MICROMOL),
    ('Холестерин не-ЛПВП (ммоль/л)', r'не-?\s*лпвп', MILLIMOL),
    ('ЛПВП (ммоль/л)', r'лпвп|высокой плотности', MILLIMOL),
    ('ЛПНП (ммоль/л)', r'лпнп|низкой плотности', MILLIMOL),
    ('Холестерин (ммоль/л)', r'холестерин', MILLIMOL),
    ('Триглицериды (ммоль/л)', r'триглицерид', MILLIMOL),
)
URINE = (
    ('Удельный вес', r'удельн|плотност', None),
    ('pH', r'\bph\b|\bрн\b|водородн', None),
    ('Белок (г/л)', r'белок|белка', ('г/л',)),
    ('Глюкоза', r'глюкоз', None),
    ('Билирубин', r'билирубин', None),
    ('Уробилиноген', r'уробилин', None),
    ('Кетоны', r'кетон', None),
    ('Нитриты', r'нитрит', None),
    ('Лейкоциты', r'лейкоцит', None),
    ('Эритроциты', r'эритроцит', None),
    ('Цилиндры', r'цилиндр', None),
    ('Эпителий плоский', r'плоск\w* эпител|эпител\w* плоск', None),
    ('Эпителий переходный', r'переходн\w* эпител|эпител\w* переходн', None),
    ('Эпителий почечный', r'почечн\w* эпител|эпител\w* почечн', None),
    ('Бактерии', r'бактери', None),
    ('Слизь', r'слиз', None),
    ('Соли', r'\bсоли\b|\bсолей\b|оксалат|урат', None),
    ('Цвет', r'\bцвет\b', None),
    ('Прозрачность', r'прозрачн', None),
)
NECHIPORENKO = (
    ('Лейкоциты', r'лейкоцит', None),
    ('Эритроциты', r'эритроцит', None),
    ('Цилиндры', r'цилиндр', None),
)
ZIMNITSKY = (
    ('Дневной диурез', r'дневн\w* диурез', None),
    ('Ночной диурез', r'ночн\w* диурез', None),
    ('Общий диурез', r'общ\w* диурез|суточн\w* диурез', None),
)
URINE_SAMPLE = re.compile(r'\bмоч[аеиу]\b|\bмочой\b|суточн')
LAB_SHEETS = {
    'ОАК': BLOOD_COUNT, 'ОАМ': URINE, 'Биохимия крови': BIOCHEMISTRY, 'Нечипоренко': NECHIPORENKO,
    'Зимницкий': ZIMNITSKY, 'Реберг': (), 'Белок мочи': (), 'Посев мочи': (), OTHER_SHEET: (),
}
ULTRASOUND_METRICS = {
    ('Размеры почки', 'Правая'): 'Почка правая', ('Размеры почки', 'Левая'): 'Почка левая',
    ('Паренхима', 'Правая'): 'Паренхима правая', ('Паренхима', 'Левая'): 'Паренхима левая',
    ('Лоханка', 'Правая'): 'Лоханка правая', ('Лоханка', 'Левая'): 'Лоханка левая',
    ('Чашечки', 'Правая'): 'Чашечки правые', ('Чашечки', 'Левая'): 'Чашечки левые',
    ('Мочеточник', 'Правая'): 'Мочеточник правый', ('Мочеточник', 'Левая'): 'Мочеточник левый',
    ('Объём', ''): 'Объём', ('Остаточная моча', ''): 'Остаточная моча',
}
ULTRASOUND_COLUMNS = tuple(dict.fromkeys(ULTRASOUND_METRICS.values()))
REPORT_SHEETS = {'УЗИ': 'УЗИ', 'УЗДГ': 'УЗДГ', 'МЦУГ': 'МЦУГ', 'Урография': 'Урография',
                 'Сцинтиграфия': 'Сцинтиграфия', 'МРТ': 'МРТ', 'Цистометрия': 'Цистометрия'}
SUMMARY_GROUPS = (
    ('ОАМ', 'Дата ОАМ (посл.)', ('Удельный вес', 'pH', 'Белок (г/л)', 'Эритроциты', 'Лейкоциты', 'Бактерии', 'Нитриты')),
    ('Нечипоренко', 'Дата Нечипоренко (посл.)', ('Лейкоциты', 'Эритроциты')),
    ('Зимницкий', 'Дата Зимницкого (посл.)', ('Дневной диурез', 'Ночной диурез', 'Общий диурез')),
    ('ОАК', 'Дата ОАК (посл.)', ('HGB (г/л)', 'WBC (10^9/л)', 'PLT (10^9/л)')),
    ('Биохимия крови', 'Дата биохимии (посл.)', ('Креатинин (мкмоль/л)', 'Мочевина (ммоль/л)', 'Белок общий (г/л)')),
    ('УЗИ', 'Дата УЗИ (посл.)', ('Почка правая', 'Почка левая', 'Заключение')),
    ('МЦУГ', 'Дата МЦУГ (посл.)', ('Заключение',)),
    ('Урография', 'Дата урографии (посл.)', ('Заключение',)),
    ('Сцинтиграфия', 'Дата сцинтиграфии (посл.)', ('Заключение',)),
    ('МРТ', 'Дата МРТ (посл.)', ('Заключение',)),
)
DRAFT_STATUSES = frozenset({'Проверить данные', 'Уточнить пациента'})


def row_id(*pieces):
    return hashlib.sha256('|'.join(map(str, pieces)).encode()).hexdigest()[:22]


def normalized_unit(unit):
    """Compare units across forms: letters that look alike and spellings of a power of ten."""
    text = ''.join(str(unit or '').lower().split()).replace('ё', 'е').replace('μ', 'мк').replace('µ', 'мк')
    text = text.replace('×', '').replace('х10', '10').replace('x10', '10').replace('*10', '10')
    text = re.sub(r'^10(?:[\^*]|[eе])(\d+)', r'10^\1', text)
    return re.sub(r'^тыс\.?/', 'тыс/', re.sub(r'^млн\.?/', 'млн/', text)).rstrip('.')


def column_for(label, unit, analytes):
    """Pick the usual column. A value in another unit does not join it, however similar the name."""
    text = ' '.join(str(label).lower().replace('ё', 'е').split())
    # Creatinine or protein in urine is another measurement than in blood.
    if analytes is BIOCHEMISTRY and URINE_SAMPLE.search(text):
        return None
    unit_key = normalized_unit(unit)
    # The first name that fits decides the analyte. Neutrophils in % and in counts share a name.
    pattern = next((candidate for _column, candidate, _units in analytes if re.search(candidate.replace('ё', 'е'), text)), None)
    if pattern is None:
        return None
    same_analyte = [(column, units) for column, candidate, units in analytes if candidate == pattern]
    if not unit_key:
        # With no printed unit the column is known only if the analyte has just one.
        return same_analyte[0][0] if len(same_analyte) == 1 else None
    return next((column for column, units in same_analyte if units is None or unit_key in units), None)


def extra_column(label, unit):
    name = ' '.join(str(label).split())
    return f'{name} ({unit})' if unit else name


def sheet_for_lab(kind, labels):
    if kind in LAB_SHEETS:
        return kind
    text = ' '.join(labels).lower()
    if 'порци' in text:
        return 'Зимницкий'
    if 'реберг' in text or 'клиренс' in text:
        return 'Реберг'
    return OTHER_SHEET


def cell_value(item):
    return item['number'] if item.get('number') is not None else item.get('result', '')


def age(birth, day):
    """Completed years and months on the study date, or blanks if a date is missing."""
    try:
        born, seen = date.fromisoformat(birth), date.fromisoformat(day)
    except (TypeError, ValueError):
        return '', ''
    if seen < born:
        return '', ''
    months = (seen.year - born.year) * 12 + seen.month - born.month - (seen.day < born.day)
    return months // 12, months


def conclusion_of(study):
    return '\n'.join(section['text'].strip() for section in study['sections']
                     if section.get('section', '').lower().startswith('заключение') and section.get('text', '').strip())


def put(values, column, value):
    """A repeated column gets a numbered neighbour. A second reading never overwrites the first."""
    if column not in values:
        values[column] = value
        return
    if values[column] == value:
        return
    number = 2
    while f'{column} ·{number}' in values:
        number += 1
    values[f'{column} ·{number}'] = value


def base_row(study, sheet, day):
    return {'sheet': sheet, 'study': study, 'date': day, 'values': {}, 'units': {}}


def lab_rows(study):
    rows = {}
    usable = [item for item in study['observations'] if item.get('origin') in LAB_ORIGINS]
    sheet = sheet_for_lab(study['kind'], [str(item.get('label', '')) for item in usable])
    for item in usable:
        # One file can carry several visits, each with its own date.
        day = item.get('date') or study['date']
        row = rows.setdefault(day, base_row(study, sheet, day))
        column = column_for(item.get('label', ''), item.get('unit', ''), LAB_SHEETS[sheet])
        if column is None:
            column = extra_column(item.get('label', ''), item.get('unit', ''))
            if item.get('unit'):
                row['units'][column] = item['unit']
        put(row['values'], column, cell_value(item))
    return list(rows.values())


def report_rows(study):
    sheet = REPORT_SHEETS[study['kind']]
    rows = {study['date']: base_row(study, sheet, study['date'])}
    if sheet == 'УЗИ':
        for item in study['observations']:
            column = ULTRASOUND_METRICS.get((item.get('label'), item.get('side', '')))
            if column and item.get('origin') == 'Описание':
                # A report can quote an earlier measurement with its own date.
                day = item.get('date') or study['date']
                row = rows.setdefault(day, base_row(study, sheet, day))
                text = ' '.join(filter(None, (str(item.get('result', '')), item.get('unit', ''))))
                row['values'][column] = f"{row['values'][column]}; {text}" if column in row['values'] else text
    conclusion = conclusion_of(study)
    if conclusion:
        rows[study['date']]['values']['Заключение'] = conclusion
    return [row for row in rows.values() if row['values']]


def fixed_columns(sheet):
    if sheet == 'УЗИ':
        return list(ULTRASOUND_COLUMNS) + ['Заключение']
    if sheet in REPORT_SHEETS.values():
        return ['Заключение']
    return [column for column, _pattern, _units in LAB_SHEETS[sheet]]


def identity_cells(row, sheet):
    study = row['study']
    years, months = age(study['birth'], row['date'])
    number = row['number']
    # No date in the identifier: correcting a date must not detach the notes on the row.
    return [row_id('clinical', sheet, study['id'], number), study['name'], study['birth'],
            row['date'], years, months, study['status']]


def merge_unitless_columns(rows, fixed):
    """Join «X» into «X (мл)» when the unit was only left blank on some forms."""
    columns = {column for row in rows for column in row['values'] if column not in fixed}
    units = {column: unit for row in rows for column, unit in row['units'].items()}
    for column in sorted(columns):
        # Only a real unit counts: «Креатинин (моча)» is a different test, not «Креатинин» with a unit.
        partners = [other for other in columns if other in units and other == f'{column} ({units[other]})']
        if len(partners) != 1:
            continue
        for row in rows:
            if column in row['values']:
                put(row['values'], partners[0], row['values'].pop(column))


def unique_columns(columns, taken):
    """Excel tables reject duplicate or very long headers."""
    used = {name.lower() for name in taken}
    result = {}
    for column in columns:
        name = column[:200]
        number = 2
        while name.lower() in used:
            name = f'{column[:195]} ·{number}'
            number += 1
        used.add(name.lower())
        result[column] = name
    return result


def by_patient_and_date(row):
    return row['study']['name'], row['date'] or '9999', row['study']['id']


def study_sheets(studies):
    """Build every sheet with data, each as (headers, rows, date headers)."""
    grouped = defaultdict(list)
    for study in studies:
        if not study['patient_id']:
            continue
        rows = lab_rows(study) if study['kind'] not in REPORT_SHEETS else report_rows(study)
        for number, row in enumerate(rows):
            row['number'] = number
            grouped[row['sheet']].append(row)
    sheets = {}
    for sheet, rows in grouped.items():
        fixed = fixed_columns(sheet)
        merge_unitless_columns(rows, fixed)
        seen = Counter(column for row in rows for column in row['values'] if column not in fixed)
        extras = [column for column, _count in seen.most_common()]
        names = unique_columns(extras, [*COMMON, *fixed, *TAIL, 'Заметки'])
        columns = fixed + extras
        rows.sort(key=by_patient_and_date)
        table = [identity_cells(row, sheet) + [row['values'].get(column) for column in columns] + [row['study']['filename']]
                 for row in rows]
        headers = COMMON + fixed + [names[column] for column in extras] + TAIL
        sheets[sheet] = (headers, table, ('Дата рождения', 'Дата исследования'))
    return sheets, grouped


def summary_sheet(grouped):
    """One row per patient: the latest study of each kind. Earlier ones stay on their own sheets."""
    latest = {}
    patients = {}
    for sheet, rows in grouped.items():
        for row in rows:
            study = row['study']
            patients[study['patient_id']] = study
            if row['date'] and row['date'] >= latest.get((study['patient_id'], sheet), {'date': ''})['date']:
                latest[study['patient_id'], sheet] = row
    headers = ['RowID', 'Пациент', 'Дата рождения', 'Есть несверенные данные']
    dates = ['Дата рождения']
    labels = []
    for sheet, date_header, columns in SUMMARY_GROUPS:
        headers.append(date_header)
        dates.append(date_header)
        labels.append((sheet, None))
        for column in columns:
            headers.append(f'{sheet}: {column}')
            labels.append((sheet, column))
    table = []
    for patient_id, study in sorted(patients.items(), key=lambda pair: pair[1]['name']):
        used = [latest[patient_id, sheet] for sheet, _date, _columns in SUMMARY_GROUPS if (patient_id, sheet) in latest]
        draft = any(row['study']['status'] in DRAFT_STATUSES for row in used)
        cells = [row_id('summary', patient_id), study['name'], study['birth'], 'Да' if draft else 'Нет']
        for sheet, column in labels:
            row = latest.get((patient_id, sheet))
            cells.append(None if row is None else row['date'] if column is None else row['values'].get(column))
        table.append(cells)
    return headers, table, tuple(dates)


def dynamics_sheet(grouped):
    """Each numeric analyte of each patient across years, when it was measured on at least two dates."""
    series = defaultdict(list)
    for sheet, rows in grouped.items():
        if sheet in REPORT_SHEETS.values():
            continue
        for row in rows:
            if not row['date']:
                continue
            for column, value in row['values'].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    series[row['study']['patient_id'], row['study']['name'], sheet, column].append((row['date'], value))
    usable = {key: sorted(points) for key, points in series.items() if len({day for day, _value in points}) >= 2}
    years = sorted({day[:4] for points in usable.values() for day, _value in points})
    headers = ['RowID', 'Пациент', 'Вид анализа', 'Показатель', 'Измерений', 'Первая дата', 'Первое значение',
               'Последняя дата', 'Последнее значение'] + years
    table = []
    for (patient_id, name, sheet, column), points in sorted(usable.items(), key=lambda pair: pair[0][1:]):
        by_year = {day[:4]: value for day, value in points}  # Sorted, so the last of a year stays.
        table.append([row_id('dynamics', patient_id, sheet, column), name, sheet, column, len(points), points[0][0],
                      points[0][1], points[-1][0], points[-1][1]] + [by_year.get(year) for year in years])
    return headers, table, ('Первая дата', 'Последняя дата')


SHEET_ORDER = ('Свод', 'Динамика', *LAB_SHEETS, *REPORT_SHEETS.values())


def build(studies):
    """Return the sheets in the order a doctor reads them."""
    sheets, grouped = study_sheets(studies)
    ordered = {'Свод': summary_sheet(grouped), 'Динамика': dynamics_sheet(grouped)}
    for name in SHEET_ORDER[2:]:
        if name in sheets:
            ordered[name] = sheets[name]
    return ordered
