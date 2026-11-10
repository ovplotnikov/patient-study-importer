"""Review issues derived from extraction evidence and rules for each document kind."""
from hashlib import sha256
import json
import re

from .parsing import COMPARATOR, KNOWN_UNIT, LABORATORY_TYPES, RANGE, SCALAR, classify_pages, classify_study, document_dates

REVIEWED_PARSER_VERSION = '2.5.0'
UNITLESS_LABEL = re.compile(
    r'^(?:ph|рн|inr|мно|цветовой показатель|относительная плотность|удельный вес)(?:\b|$)|'
    r'\b(?:коэффициент|индекс|отношение|ph|уд[ае]льн\w+ вес\w*|относительн\w+ плотност\w*)\b',
    re.I,
)
MEASURED_LABEL = re.compile(
    r'гемоглобин|креатинин|мочевин|эритроцит|лейкоцит|тромбоцит|'
    r'натрий|калий|глюкоз|концентраци|толщин|размер|объ[её]м|масса|'
    r'гематокрит|\b(?:hb|hgb|wbc|rbc|plt)\b',
    re.I,
)
NUMERIC_RESULT = re.compile(rf'^(?:{COMPARATOR}\s*)?{SCALAR}(?:{RANGE}|(?:\s*[хx×*]\s*{SCALAR})+)$')
MISSING_VALUES = {'', '-', '—', '–', 'н/д', 'н.д.'}
PRINTED_DASHES = {'-', '—', '–'}
SEMI_QUANTITATIVE = re.compile(r'(?:\d\s*)?\+{1,4}')
URINE_FORMS = frozenset({'ОАМ', 'Нечипоренко'})
# Urine forms print sediment counts with no unit. There is nothing on the form to compare it with.
SEDIMENT_LABEL = re.compile(r'лейкоцит|эритроцит|эпители|цилиндр', re.I)
# Bookkeeping about how a page was read. It says nothing about the extracted values.
INFORMATIONAL_ISSUES = frozenset({'SIGNATURE_IMAGE'})


def has_narrative_result(sections):
    """A manually checked or recognized textual result can be complete without numbers."""
    for section in sections:
        text = str(section.get('text') or '').strip()
        if not text:
            continue
        if section.get('reviewed'):
            return True
        name = section.get('section', '')
        if name.lower() != 'описание исследования':
            body = re.sub(rf'^\s*{re.escape(name)}\s*:', '', text, count=1, flags=re.I)
            if re.search(r'[А-Яа-яЁёA-Za-z]', body):
                return True
            continue
        for line in text.splitlines():
            line = line.strip()
            if (not line or classify_study(line) or document_dates(line)
                    or re.match(r'^(?:пациент|фио|фамилия|имя|отчество|дата рождения|пол|возраст|№|врач|медицинская организация)(?:[: .]|$)', line, re.I)):
                continue
            # A title like «Описание:» without any text is not a result.
            if re.fullmatch(r'[А-Яа-яЁёA-Za-z ]+\s*:', line):
                continue
            if len(re.findall(r'[А-Яа-яЁёA-Za-z]{2,}', line)) >= 2:
                return True
    return False


def assess(analysis, extracted=None):
    """Build review tasks without altering extracted values or existing assignments."""
    findings = []
    seen = set()

    def add(code, message, *, page=None, page_index=None, line=None, observation_index=None):
        location = (code, page, page_index, line, observation_index)
        if location in seen:
            return
        seen.add(location)
        issue_id = 'Q-' + sha256(json.dumps(location, ensure_ascii=False).encode()).hexdigest()[:20]
        issue = {'id': issue_id, 'code': code, 'message': message, 'severity': 'review'}
        for key, value in (('page', page), ('page_index', page_index), ('line', line),
                           ('observation_index', observation_index)):
            if value is not None:
                issue[key] = value
        findings.append(issue)

    parser_version = analysis.get('version')
    if parser_version and parser_version != REVIEWED_PARSER_VERSION:
        add('legacy_parser', 'Документ обработан прежней версией. Сверьте оригинал или распознайте заново.')

    pages = analysis.get('pages', [])
    dates = analysis.get('date_candidates')
    if dates is None:
        dates = document_dates('\n'.join(pages))
    candidates = analysis.get('type_candidates')
    kind = analysis.get('type')
    if candidates is None:
        inferred_kind, candidates = classify_pages(pages)
        if kind and kind != 'Документ' and inferred_kind != kind and len(candidates) == 1:
            add('type_mismatch', f'Проверьте вид бланка: «{kind}» или «{inferred_kind}».')
    if len(candidates) > 1:
        add('conflicting_type', f'В бланке несколько видов: {", ".join(candidates)}.')
    elif kind in ('Документ', '', None):
        add('unknown_type', 'Не удалось определить вид бланка.')
    if len(dates) > 1:
        add('conflicting_date', 'В бланке указано несколько дат исследования.')
    elif not analysis.get('date'):
        add('missing_date', 'Дата исследования не найдена.')

    observations = analysis.get('observations', [])
    if kind in LABORATORY_TYPES and not any(
        item.get('origin') in {'Лабораторный бланк', 'Ручной ввод', 'Введено при сверке'}
        and str(item.get('result') or '').strip().lower() not in MISSING_VALUES
        for item in observations
    ):
        add('lab_no_results', 'Не удалось извлечь результаты лабораторного бланка.')
    if kind not in LABORATORY_TYPES and kind not in ('Документ', '', None):
        if not observations and not has_narrative_result(analysis.get('sections', [])):
            add('missing_narrative_result', 'Не удалось найти описание или заключение исследования.')

    page_numbers = analysis.get('page_numbers') or list(range(1, len(pages) + 1))
    for index in analysis.get('pages_without_patient', []):
        add('page_without_patient', 'На странице своё исследование, но нет ФИО. Проверьте, что это тот же пациент.',
            page=page_numbers[index - 1], page_index=index)
    ocr_indexes = set(analysis.get('ocr_pages', []))
    has_native_text = {page_numbers[i] for i, text in enumerate(pages) if text.strip() and i + 1 not in ocr_indexes}
    ocr_read_text = any(1 <= i <= len(pages) and pages[i - 1].strip() for i in ocr_indexes)
    ocr_pages = {page_numbers[i - 1] for i in analysis.get('ocr_pages', []) if 1 <= i <= len(page_numbers)}
    found_ocr_value = False
    for index, item in enumerate(observations):
        position = {
            'page': item.get('page'), 'page_index': item.get('page_index'),
            'line': item.get('line'), 'observation_index': index,
        }
        value = str(item.get('result') or '').strip()
        label = item.get('label', '')
        unit = str(item.get('unit') or '').strip()
        from_ocr = item.get('from_ocr', item.get('status') == 'Проверить' and item.get('page') in ocr_pages)
        if from_ocr:
            found_ocr_value = True
            add('ocr_value', f'Сверьте с оригиналом результат «{label}» со скана.', **position)
        elif item.get('status') == 'Проверить':
            add('ambiguous_value', f'Сверьте с оригиналом результат «{label}».', **position)
        if value in PRINTED_DASHES and item.get('origin') == 'Лабораторный бланк':
            pass  # The table cell is carried over exactly as printed.
        elif value.lower() in MISSING_VALUES:
            add('missing_result', f'Не найден результат «{label}».', **position)
        elif NUMERIC_RESULT.fullmatch(value):
            if re.fullmatch(SCALAR, value) and item.get('number') is None:
                add('ambiguous_value', f'Проверьте запись числа «{label}».', **position)
            if unit.lower() in MISSING_VALUES:
                sediment = kind in URINE_FORMS and SEDIMENT_LABEL.search(label)
                if not sediment and not UNITLESS_LABEL.search(label) and (item.get('unit_column') or MEASURED_LABEL.search(label)):
                    add('missing_unit', f'Не найдена единица измерения «{label}».', **position)
            elif not KNOWN_UNIT.fullmatch(unit):
                add('unknown_unit', f'Проверьте единицу измерения «{label}».', **position)
        elif re.match(r'^[<>≤≥+−-]?\s*\d', value) and not SEMI_QUANTITATIVE.fullmatch(value):
            add('ambiguous_value', f'Проверьте, какое значение относится к «{label}».', **position)
        if item.get('unmapped_columns'):
            add('unmapped_columns', 'Не удалось определить назначение некоторых столбцов.', **position)
    if not found_ocr_value and any(
        1 <= i <= len(pages) and pages[i - 1].strip() for i in analysis.get('ocr_pages', [])
    ):
        add('ocr_text', 'Сверьте текст или поля, распознанные со скана.')

    if 'table_issues' in analysis:
        for issue in analysis['table_issues']:
            add('unparsed_row', 'Не удалось разобрать строку таблицы.',
                page=issue['page'], page_index=issue.get('page_index'), line=issue['line'])
    else:
        for message in analysis.get('problems', []):
            match = re.match(r'Страница (\d+), строка (\d+):', message)
            if match:
                page, line = map(int, match.groups())
                physical_page = page_numbers[page - 1] if 1 <= page <= len(page_numbers) else page
                add('unparsed_row', 'Не удалось разобрать строку таблицы.', page=physical_page, line=line)

    extraction_issues = (extracted or {}).get('extraction_issues', analysis.get('extraction_issues', []))
    for entry in extraction_issues:
        if isinstance(entry, dict):
            code = entry.get('code')
            if code in INFORMATIONAL_ISSUES:
                continue
            # An image with no text beside a readable page is a logo or a stamp.
            if code == 'OCR_EMPTY' and entry.get('page') in has_native_text:
                continue
            # Text that OCR read gets its own review item. Otherwise OCR repeated the text layer.
            if code == 'OCR_USED' and not ocr_read_text:
                continue
            add(entry.get('code') or 'extraction_issue',
                entry.get('message') or 'Не удалось полностью прочитать источник.',
                page=entry.get('page'), page_index=entry.get('page_index'), line=entry.get('line'))
        elif isinstance(entry, str):
            add('extraction_issue', entry)

    return {'issues': findings, 'confirmed': False}


def issues(analysis):
    quality = analysis.get('quality')
    if not isinstance(quality, dict):
        quality = assess(analysis)
    if quality.get('confirmed') is True:
        return []
    return quality.get('issues', [])


def needs_review(analysis):
    return bool(issues(analysis))
