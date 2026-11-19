"""Turn what the program already knows about a document into diagnostics codes.

Only closed codes come out: the kind of form, why it was flagged, ranges of counts, the order
of named table columns. Nothing read from the document itself (a word, a value, a name) can.
"""
from collections import Counter

from . import parsing, quality, review_queue
from . import telemetry_schema as schema
from .analysis import FOLDER_REASON

LAB_ROW = 'Лабораторный бланк'
PERSON = 'Пациент подтверждён пользователем'


def header_layouts(pages):
    """The order of a lab table's columns, each by its fixed name or «unknown»."""
    layouts = set()
    for page in pages:
        for line in page.splitlines():
            if not parsing.laboratory_header(line):
                continue
            columns = []
            for part in parsing.split_columns(line):
                columns.extend(parsing.header_cells(part) or ['unknown'])
            code = schema.header_code(columns)
            if code:
                layouts.add(code)
    return layouts


def patient_source(record, parsed):
    if record.get('patient_id'):
        reason = record.get('reason') or ''
        if reason == FOLDER_REASON:
            return 'folder'
        return 'person' if reason.startswith(PERSON) else 'document'
    if parsed.get('identity_from_ocr'):
        return 'identity_from_ocr'
    if (record.get('reason') or '').startswith('После повторного'):
        return 'changed_identity'
    if len(parsed.get('names', ())) > 1 or len(parsed.get('births', ())) > 1:
        return 'ambiguous_identity'
    if not parsed.get('names') or not parsed.get('births'):
        return 'missing_identity'
    return 'conflict'


def reading_codes(record, parsed, outcome, extraction_issues=()):
    """Codes of the group «reading» for one newly saved document."""
    kind = schema.kind_code(parsed.get('type'))
    codes = Counter({f'outcome.{kind}.{outcome}': 1, 'patient.' + patient_source(record, parsed): 1})
    flags = quality.issues(parsed)
    for code in {issue.get('code') for issue in flags}:
        if code in schema.REASONS:
            codes[f'flag.{kind}.{code}'] = 1
    if parsed.get('type') in parsing.LABORATORY_TYPES:
        rows = sum(item.get('origin') == LAB_ROW for item in parsed.get('observations', ()))
        unread = quality.unparsed_rows(flags)
        codes['lab_rows.' + schema.count_bucket(rows, schema.ROWS)] = 1
        codes['unparsed.' + schema.share_bucket(unread, rows + unread)] = 1
        for layout in header_layouts(parsed.get('pages', ())):
            codes[layout] = 1
    if parsed.get('ocr_pages'):
        codes['scan_pages.' + schema.count_bucket(len(parsed['ocr_pages']), schema.PAGES)] = 1
        found = {entry.get('code') for entry in extraction_issues if isinstance(entry, dict)}
        reading = ('single_engine' if 'OCR_ENGINE_UNAVAILABLE' in found
                   else 'disagree' if 'OCR_DISAGREEMENT' in found else 'agree')
        codes['scan_reading.' + reading] = 1
    return {'reading': {code: count for code, count in codes.items() if schema.valid_code('reading', code)}}


def source(row):
    if row.get('from_ocr'):
        return 'scan'
    return 'table' if row.get('origin') == LAB_ROW else 'narrative'


FIELDS = {'result': 'result', 'unit': 'unit', 'reference': 'reference', 'label': 'label', 'date': 'date'}


def accuracy_codes(previous_rows, reviewed_rows, checked_ids=None, unparsed_lines=()):
    """What a person changed when confirming: rows kept as read, edited fields, accepted layouts."""
    before = {row.get('result_id'): row for row in previous_rows if row.get('result_id')}
    unread = {str(raw).strip() for raw in unparsed_lines if str(raw).strip()}
    codes = Counter()
    for row in reviewed_rows:
        old = before.get(row.get('result_id'))
        if old is not None:
            if checked_ids is not None and row['result_id'] not in checked_ids:
                continue
            origin = source(old)
            edited = [field for field in FIELDS
                      if str(old.get(field) or '').strip() != str(row.get(field) or '').strip()]
            codes[('rows_edited.' if edited else 'rows_unchanged.') + origin] += 1
            for field in edited:
                codes[f'field_edited.{FIELDS[field]}.{origin}'] += 1
        elif str(row.get('raw') or '').strip() in unread:
            proposed = review_queue.prefill_row(row['raw'])
            same = all(str(row.get(field) or '').strip() == str(proposed.get(field) or '').strip()
                       for field in ('label', 'result', 'unit', 'reference'))
            codes['layout.' + ('accepted' if same else 'edited')] += 1
    return {'accuracy': dict(codes)} if codes else {}


def change_basis(record):
    reason = record.get('reason') or ''
    if reason.startswith(PERSON + ' списком'):
        return 'suggestion'
    if record.get('decision') or reason.startswith(PERSON):
        return 'person'
    if reason == FOLDER_REASON:
        return 'folder'
    return 'document'
