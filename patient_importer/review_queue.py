"""What the review window asks about, one stumble at a time. Tk-free so it can be tested alone."""
from collections import Counter
import re

from .parsing import COMPARATOR, FLAG_GLYPHS, LABORATORY_TYPES, SCALAR, STUDY_HEADINGS, split_columns

# These need the whole document in front of the reader: a scan is checked line by line,
# and a form with no results has nothing to step through.
FULL_REVIEW_CODES = frozenset({
    'ocr_value', 'ocr_text', 'OCR_USED', 'OCR_DISAGREEMENT', 'OCR_LOW_CONFIDENCE', 'OCR_ENGINE_UNAVAILABLE',
    'OCR_EMPTY', 'OCR_FAILED', 'lab_no_results', 'missing_narrative_result', 'possible_duplicate',
    'reprocess_conflict', 'legacy_parser',
})
ROW_TITLES = {
    'missing_unit': 'Нет единицы измерения', 'unknown_unit': 'Незнакомая единица измерения',
    'missing_result': 'Нет результата', 'ambiguous_value': 'Проверьте значение',
    'unmapped_columns': 'Неясные столбцы таблицы',
}
DATE_CODES = frozenset({'missing_date', 'conflicting_date'})
TYPE_CODES = frozenset({'unknown_type', 'conflicting_type', 'type_mismatch'})
UNIT_CODES = frozenset({'unknown_unit', 'missing_unit'})
STUDY_TYPES = tuple(sorted({label for label, _pattern in STUDY_HEADINGS} | LABORATORY_TYPES))
VALUE_START = re.compile(rf'^(?:{COMPARATOR}\s*)?{SCALAR}')
INLINE_ROW = re.compile(rf'^(?P<label>.*?[^\s\d.,])\s+(?P<result>(?:{COMPARATOR}\s*)?{SCALAR})\s*(?P<unit>\S*)$')


def needs_full_review(record):
    return any(issue['code'] in FULL_REVIEW_CODES for issue in record['issues'])


def page_line(analysis, page_index, line):
    pages = analysis.get('pages', [])
    if page_index and 1 <= page_index <= len(pages) and line:
        lines = pages[page_index - 1].splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()
    return ''


def stumbles(record):
    """Merge a document's issues into the questions a person answers, in reading order."""
    found = {}
    analysis = record['analysis']
    observations = record['observations']
    for issue in record['issues']:
        code = issue['code']
        index = issue.get('observation_index')
        if index is not None and index < len(observations):
            row = observations[index]
            key = 'row:' + row['result_id']
            item = found.setdefault(key, {
                'key': key, 'kind': 'row', 'result_id': row['result_id'], 'page': row.get('page'),
                'page_index': row.get('page_index'), 'line': row.get('line'), 'raw': row.get('raw', ''),
                'label': row.get('label', ''), 'title': ROW_TITLES.get(code, 'Проверьте строку'),
                'codes': [], 'messages': [],
            })
        elif code == 'unparsed_row':
            page_index = issue.get('page_index')
            line = issue.get('line')
            key = f"line:{issue.get('page')}:{page_index}:{line}"
            item = found.setdefault(key, {
                'key': key, 'kind': 'unparsed', 'page': issue.get('page'), 'page_index': page_index,
                'line': line, 'raw': page_line(analysis, page_index, line),
                'title': 'Строка не разобрана', 'label': '', 'codes': [], 'messages': [],
            })
        elif code in DATE_CODES:
            item = found.setdefault('date', {
                'key': 'date', 'kind': 'date', 'page': 1, 'title': 'Укажите дату исследования',
                'candidates': analysis.get('date_candidates', []), 'label': '', 'codes': [], 'messages': [],
            })
        elif code in TYPE_CODES:
            item = found.setdefault('type', {
                'key': 'type', 'kind': 'type', 'page': 1, 'title': 'Укажите вид исследования',
                'candidates': analysis.get('type_candidates', []), 'label': '', 'codes': [], 'messages': [],
            })
        else:
            key = f"info:{code}:{issue.get('page')}"
            item = found.setdefault(key, {
                'key': key, 'kind': 'info', 'page': issue.get('page') or 1, 'page_index': issue.get('page_index'),
                'line': issue.get('line'), 'title': 'Проверьте документ', 'label': '', 'codes': [], 'messages': [],
            })
        item['codes'].append(code)
        if issue.get('message') and issue['message'] not in item['messages']:
            item['messages'].append(issue['message'])

    def reading_order(item):
        first = {'date': 0, 'type': 1}.get(item['kind'], 2)
        return first, item.get('page_index') or item.get('page') or 0, item.get('line') or 0

    return sorted(found.values(), key=reading_order)


def queue_label(stumble):
    place = f"с.{stumble['page']} · " if stumble.get('page') and stumble['kind'] in {'row', 'unparsed', 'info'} else ''
    name = stumble.get('label') or ''
    return place + stumble['title'] + (f': {name}' if name else '')


def prefill_row(raw):
    """Lay an unread line out by content. The person checks every field before accepting."""
    fields = {'label': '', 'result': '', 'unit': '', 'reference': ''}
    cells = [cell for cell in split_columns(raw) if cell.strip(FLAG_GLYPHS + ' ')]
    remaining = []
    for cell in cells:
        if cell.startswith('(') and not fields['reference'] and remaining:
            fields['reference'] = cell
        else:
            remaining.append(cell)
    if len(remaining) == 1:
        inline = INLINE_ROW.match(remaining[0])
        if inline:
            fields.update(label=inline['label'], result=inline['result'], unit=inline['unit'])
        else:
            fields['label'] = remaining[0]
    elif remaining:
        value_at = next((index for index, cell in enumerate(remaining) if index and VALUE_START.match(cell)), 1)
        fields['label'] = ' '.join(remaining[:value_at])
        fields['result'] = remaining[value_at]
        if value_at + 1 < len(remaining):
            fields['unit'] = remaining[value_at + 1]
    return fields


def visual_lines(rects, page_height):
    """Group PDF text rectangles into lines, top to bottom, in coordinates from the top edge."""
    lines = []
    for text, (left, bottom, right, top) in sorted(rects, key=lambda item: -(item[1][1] + item[1][3])):
        middle = (top + bottom) / 2
        height = max(top - bottom, 1)
        if lines and abs(lines[-1]['middle'] - middle) <= .6 * height:
            lines[-1]['parts'].append((left, right, text))
            lines[-1]['bottom'] = min(lines[-1]['bottom'], bottom)
            lines[-1]['top'] = max(lines[-1]['top'], top)
        else:
            lines.append({'middle': middle, 'bottom': bottom, 'top': top, 'parts': [(left, right, text)]})
    result = []
    for line in lines:
        parts = sorted(line['parts'], key=lambda part: part[0])
        result.append({
            'text': ' '.join(part[2] for part in parts),
            'box': (parts[0][0], page_height - line['top'], max(part[1] for part in parts), page_height - line['bottom']),
        })
    return result


def tokens(text):
    return Counter(re.findall(r'[а-яёa-z]+|\d+(?:[.,]\d+)?', text.lower()))


def find_line(lines, raw, line=None, total=None):
    """Find a parsed line on the rendered page. Identical rows are told apart by position."""
    wanted = tokens((raw or '').split('\n')[0])
    size = sum(wanted.values())
    if not size or not lines:
        return None
    scored = []
    for index, candidate in enumerate(lines):
        common = sum((wanted & tokens(candidate['text'])).values())
        if common >= min(2, size) and common / size >= .7:
            scored.append((common / size, index))
    if not scored:
        return None
    best = max(score for score, _index in scored)
    matches = [index for score, index in scored if best - score < 1e-6]
    if line and total:
        expected = (line - 1) / max(total - 1, 1)
        matches.sort(key=lambda index: abs(index / max(len(lines) - 1, 1) - expected))
    return lines[matches[0]]


NAME_BASIS, BIRTH_BASIS, FOLDER_BASIS = 'ФИО', 'дата рождения', 'соседние документы'


def patient_suggestion(record):
    """The one patient a document points to, with the reason. None when a person has to look.

    A document naming someone and giving a birth date reached review because something
    conflicts, and identity read from a scan needs its own check.
    """
    names, births = record.get('names', []), record.get('births', [])
    candidates = [tuple(candidate) for candidate in record.get('candidates', [])]
    if record.get('identity_from_ocr') or record.get('reason', '').startswith('После повторного'):
        return None
    if len(names) > 1 or len(births) > 1 or (names and births):
        return None
    if names:
        matching = [candidate for candidate in candidates if candidate[0] == names[0]]
        basis = NAME_BASIS
    elif births:
        matching = [candidate for candidate in candidates if candidate[1] == births[0]]
        basis = BIRTH_BASIS
    else:
        matching, basis = candidates, FOLDER_BASIS
    return (matching[0], basis) if len(matching) == 1 else None


def patient_groups(records):
    """Group confident suggestions, so one decision can cover every document of a patient."""
    groups = {}
    for record in records:
        suggestion = patient_suggestion(record)
        if suggestion:
            (name, birth), basis = suggestion
            groups.setdefault((name, birth, basis), []).append(record['id'])
    return [{'name': name, 'birth': birth, 'basis': basis, 'documents': documents}
            for (name, birth, basis), documents in sorted(groups.items())]
