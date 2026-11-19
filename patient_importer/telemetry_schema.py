"""The complete wire contract for optional aggregate diagnostics.

Every code is built from closed lists or from short ASCII patterns that cannot hold a word
from a document, a name or a path. The client accepts only codes of this contract. The
receivers keep what they know and count anything else as one «_unknown» code, never its text.
Both receivers (google_apps_script/Code.gs and telemetry_server) apply the same rules.
"""
from datetime import datetime, timezone
import re
from types import MappingProxyType

SCHEMA_VERSION = 2
# Versions 2.4.0–2.5.2 send schema 1. Their reports keep being accepted.
ACCEPTED_SCHEMAS = (1, 2)
MAX_COUNT = 100_000
MAX_REPORT_BYTES = 16384
COUNTERS_PATH = '/v1/counters'
MAX_CODE_LENGTH = 120
# Receivers: distinct versions per week, then «other»; distinct codes per week and group, then «_other».
MAX_VERSIONS_PER_WEEK = 12
MAX_CODES_PER_WEEK = MappingProxyType({'errors': 60, 'reading': 400})
DEFAULT_CODES_PER_WEEK = 200
OTHER_VERSION = 'other'
# Why a receiver refused a report. Counted per week; the report itself is never kept.
REFUSALS = ('format', 'too_large', 'content_type', 'rate_limited', 'busy', 'storage', 'full')
UNKNOWN = '_unknown'
OTHER = '_other'
_VERSION = re.compile(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}', re.ASCII)
_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}', re.ASCII)

# Study kinds as the parser names them, with a fixed ASCII name each.
KINDS = MappingProxyType({
    'ОАК': 'cbc', 'ОАМ': 'urine', 'Биохимия крови': 'biochemistry', 'Нечипоренко': 'nechiporenko',
    'Зимницкий': 'zimnitsky', 'Реберг': 'reberg', 'Белок мочи': 'urine_protein', 'Посев мочи': 'urine_culture',
    'Лабораторное исследование': 'lab_other', 'УЗИ': 'ultrasound', 'УЗДГ': 'doppler', 'МЦУГ': 'cystography',
    'Урография': 'urography', 'Сцинтиграфия': 'scintigraphy', 'МРТ': 'mri', 'Цистометрия': 'cystometry',
    'Осмотр или выписка': 'visit', 'Документ': 'unknown',
})
KIND_CODES = tuple(sorted(set(KINDS.values()) | {'other'}))
OUTCOMES = ('saved', 'saved_flagged', 'duplicate', 'identity_review', 'quality_review', 'error', 'skipped')
REASONS = (
    'missing_identity', 'ambiguous_identity', 'identity_from_ocr', 'legacy_parser',
    'type_mismatch', 'conflicting_type', 'unknown_type', 'conflicting_date', 'missing_date',
    'lab_no_results', 'missing_narrative_result', 'ocr_value', 'ocr_text', 'ambiguous_value',
    'missing_result', 'missing_unit', 'unknown_unit', 'unmapped_columns', 'unparsed_row',
    'extraction_issue', 'OCR_USED', 'OCR_DISAGREEMENT', 'OCR_ENGINE_UNAVAILABLE',
    'OCR_LOW_CONFIDENCE', 'OCR_EMPTY', 'OCR_FAILED', 'PDF_TEXT_UNREADABLE', 'PDF_PAGE_EMPTY',
    'EMBEDDED_IMAGE_UNREADABLE', 'EMBEDDED_IMAGE_UNSUPPORTED', 'SIGNATURE_IMAGE',
    'DOCX_COMPLEX_CONTENT', 'DOCX_EXTRA_TEXT', 'page_without_patient',
    'possible_duplicate', 'reprocess_conflict',
)
STAGES = ('intake', 'extraction', 'analysis', 'storage', 'export', 'export_locked', 'archive', 'review')
ERROR_STAGES = STAGES + ('window', 'import', 'update', 'startup')
COLUMNS = ('label', 'result', 'unit', 'reference', 'flag', 'comment', 'test_system', 'history', 'unknown')
MAX_HEADER_COLUMNS = 8
# Modules of the program. A failure is placed by module, function and line of its innermost frame here.
MODULES = (
    'analysis', 'app', 'clinical_sheets', 'diagnostic_events', 'diagnostics_config', 'diagnostics_ui',
    'extraction', 'gui',
    'help_view', 'intake', 'ocr', 'parallel', 'parsing', 'pdf_render', 'quality', 'quality_ui', 'reading',
    'review_queue', 'smoke', 'storage', 'support', 'telemetry', 'telemetry_schema', 'ui', 'ui_smoke',
    'updater', 'workbook', 'other',
)
EXCEPTIONS = (
    'ArithmeticError', 'AssertionError', 'AttributeError', 'BadZipFile', 'BrokenProcessPool', 'Cancelled',
    'ConnectionError', 'DatabaseError', 'EOFError', 'FileExistsError', 'FileNotFoundError', 'ImportError',
    'IndexError', 'IntegrityError', 'InvalidFileException', 'IsADirectoryError', 'JSONDecodeError', 'KeyError',
    'LockException', 'LookupError', 'MemoryError', 'ModuleNotFoundError', 'NameError', 'NotADirectoryError',
    'NotImplementedError', 'OSError', 'OperationalError', 'OverflowError', 'PdfReadError', 'PdfiumError',
    'PermissionError', 'ReadingFailed', 'RecursionError', 'ReviewCommittedError', 'RuntimeError',
    'StopIteration', 'SystemError', 'TclError', 'TimeoutError', 'TypeError', 'UnboundLocalError',
    'UnicodeDecodeError', 'UnicodeEncodeError', 'UnicodeError', 'UnidentifiedImageError', 'UpdateError',
    'ValueError', 'ZeroDivisionError', 'Other',
)
FUNCTION = r'[a-z_][a-z0-9_]{0,39}'
LINE = r'[1-9][0-9]{0,4}'

AGES = ('week0', '1_3w', '1_3m', '3m_plus')
SYSTEMS = ('win10', 'win11', 'windows_other', 'other')
CORES = ('1_2', '3_4', '5_8', '9_16', '17_plus')
MEMORY = ('lt4', '4_7', '8_15', '16_31', '32_plus', 'unknown')
SCALES = ('100', '125', '150', '175', '200_plus', 'other')
DOCUMENTS = ('0', '1_9', '10_99', '100_499', '500_1999', '2000_plus')
PATIENTS = ('0', '1_9', '10_49', '50_199', '200_plus')
WAITING = ('0', '1_9', '10_49', '50_plus')
SHARES = ('0', '1_9', '10_24', '25_49', '50_plus')
ROWS = ('0', '1_4', '5_9', '10_19', '20_39', '40_plus')
PAGES = ('1', '2', '3_5', '6_10', '11_plus')
REMAINING = ('0', '1', '2_4', '5_9', '10_49', '50_plus')
SECONDS = ('lt1s', '1_3s', '3_10s', '10_30s', '30_60s', '1_3m', '3m_plus')
EVENTS = (
    'start', 'add_files', 'add_folder', 'add_drop', 'import_started', 'import_done', 'import_stopped',
    'import_failed', 'table_opened', 'patient_window', 'quality_window', 'patient_typed',
    'patient_candidate', 'patient_list', 'patient_draft_kept', 'patient_draft_edited', 'file_opened',
    'file_revealed', 'patient_suggested', 'reparse_shown', 'reparse_accepted', 'reparse_cancelled', 'update_offered',
    'update_accepted', 'update_installed',
)
UPDATE_STAGES = ('check', 'download', 'install')
PATIENT_SOURCES = ('document', 'folder', 'person', 'missing_identity', 'ambiguous_identity',
                   'identity_from_ocr', 'changed_identity', 'conflict')
SOURCES = ('table', 'narrative', 'scan')
FIELDS = ('result', 'unit', 'reference', 'label', 'date')
BASES = ('document', 'folder', 'suggestion', 'person')
TIMINGS = ('text', 'scan', 'export', 'startup')


def _pattern(expression):
    return {'pattern': expression}


HEADER = _pattern('(?:{0})(?:\\|(?:{0})){{0,{1}}}'.format('|'.join(COLUMNS), MAX_HEADER_COLUMNS - 1))

# Every group is a list of shapes. A shape is a list of parts joined by «.» in a code;
# a part is a closed list or an ASCII pattern.
METRICS = MappingProxyType({
    # Schema 1 groups, kept as they were. Versions up to 2.5.2 send only these.
    'formats': [[['pdf', 'docx', 'txt', 'image', 'other']]],
    'outcomes': [[list(OUTCOMES)]],
    'review_reasons': [[list(REASONS)]],
    'review_outcomes': [[['confirmed', 'confirmed_incomplete', 'reprocessed', 'excluded', 'deferred']]],
    'edit_categories': [[['identity', 'study_type', 'study_date', 'result', 'unit', 'reference',
                          'observation', 'conclusion', 'other']]],
    'failure_stages': [[list(STAGES)]],
    # Schema 2.
    'activity': [[['active']], [['age'], list(AGES)], [['os'], list(SYSTEMS)], [['cores'], list(CORES)],
                 [['memory'], list(MEMORY)], [['scale'], list(SCALES)], [['documents'], list(DOCUMENTS)],
                 [['patients'], list(PATIENTS)], [['waiting'], list(WAITING)], [['flagged'], list(SHARES)]],
    'path': [[list(EVENTS)], [['closed'], ['patient', 'quality'], list(REMAINING)],
             [['update_failed'], list(UPDATE_STAGES)]],
    'reading': [[['outcome'], list(KIND_CODES), list(OUTCOMES)], [['flag'], list(KIND_CODES), list(REASONS)],
                [['lab_rows'], list(ROWS)], [['unparsed'], list(SHARES)], [['header'], HEADER],
                [['patient'], list(PATIENT_SOURCES)], [['scan_pages'], list(PAGES)],
                [['scan_reading'], ['agree', 'disagree', 'single_engine']]],
    'accuracy': [[['rows_unchanged'], list(SOURCES)], [['rows_edited'], list(SOURCES)],
                 [['field_edited'], list(FIELDS), list(SOURCES)], [['layout'], ['accepted', 'edited']],
                 [['patient_changed'], list(BASES)]],
    'errors': [[list(ERROR_STAGES), list(EXCEPTIONS), list(MODULES), _pattern(FUNCTION), _pattern(LINE)],
               [['session_crash'], ['idle', 'processing']], [['worker_crash']], [['export_locked']]],
    'speed': [[list(TIMINGS), list(SECONDS)]],
})


def _compile(part):
    if isinstance(part, dict):
        expression = re.compile(part['pattern'], re.ASCII)
        return lambda value: expression.fullmatch(value) is not None
    allowed = frozenset(part)
    return allowed.__contains__


_CHECKS = {metric: [[_compile(part) for part in shape] for shape in shapes] for metric, shapes in METRICS.items()}


def valid_code(metric, code):
    if type(metric) is not str or type(code) is not str or not 0 < len(code) <= MAX_CODE_LENGTH:
        return False
    shapes = _CHECKS.get(metric)
    if shapes is None:
        return False
    parts = code.split('.')
    return any(len(parts) == len(shape) and all(check(part) for check, part in zip(shape, parts))
               for shape in shapes)


def valid_version(value):
    return type(value) is str and len(value) <= 14 and _VERSION.fullmatch(value) is not None


def valid_count(value):
    return type(value) is int and 1 <= value <= MAX_COUNT


def validate_counters(value):
    """Client side: fresh dictionaries with only codes of this contract, or ValueError."""
    if type(value) is not dict:
        raise ValueError('Invalid diagnostics counters')
    clean = {}
    for metric, counters in value.items():
        if type(metric) is not str or metric not in METRICS or type(counters) is not dict or not counters:
            raise ValueError('Invalid diagnostics bucket')
        bucket = {}
        for code, count in counters.items():
            if not valid_code(metric, code) or not valid_count(count):
                raise ValueError('Invalid diagnostics count')
            bucket[code] = count
        clean[metric] = bucket
    return clean


def validate_report(value):
    """Client side: exactly what this version sends."""
    if (type(value) is not dict or value.keys() != {'schema_version', 'app_version', 'counters'}
            or type(value['schema_version']) is not int or value['schema_version'] != SCHEMA_VERSION
            or not valid_version(value['app_version'])):
        raise ValueError('Invalid diagnostics report')
    counters = validate_counters(value['counters'])
    if not counters:
        raise ValueError('Empty diagnostics report')
    return {'schema_version': SCHEMA_VERSION, 'app_version': value['app_version'], 'counters': counters}


def receive_report(value):
    """Receiver side: (report, None) with unknown codes folded into «_unknown», or (None, reason).

    The structure, the version and every count must be right, or the whole report is refused.
    A group or a code nobody knows is counted, but its text is never kept.
    """
    if (type(value) is not dict or value.keys() != {'schema_version', 'app_version', 'counters'}
            or type(value['schema_version']) is not int or value['schema_version'] not in ACCEPTED_SCHEMAS
            or not valid_version(value['app_version']) or type(value['counters']) is not dict
            or not value['counters']):
        return None, 'format'
    counters = {}
    for metric, bucket in value['counters'].items():
        if type(bucket) is not dict or not bucket:
            return None, 'format'
        known = type(metric) is str and _NAME.fullmatch(metric) is not None and metric in METRICS
        for code, count in bucket.items():
            if not valid_count(count):
                return None, 'format'
            if known and valid_code(metric, code):
                group, name = metric, code
            elif known:
                group, name = metric, UNKNOWN
            else:
                group, name = UNKNOWN, UNKNOWN
            target = counters.setdefault(group, {})
            target[name] = min(MAX_COUNT, target.get(name, 0) + count)
    return {'schema_version': value['schema_version'], 'app_version': value['app_version'],
            'counters': counters}, None


def week_of(moment=None):
    """ISO week of the receiver's clock, «2026-W47». The program never sends a time."""
    year, week, _ = (moment or datetime.now(timezone.utc)).isocalendar()
    return f'{year}-W{week:02d}'


def wire_schema():
    """What google_apps_script/Code.gs embeds as WIRE_SCHEMA. Checked by tests."""
    return {
        'schemaVersion': SCHEMA_VERSION, 'acceptedSchemas': list(ACCEPTED_SCHEMAS), 'pathInfo': '',
        'maxReportBytes': MAX_REPORT_BYTES, 'maxCount': MAX_COUNT, 'maxCodeLength': MAX_CODE_LENGTH,
        'maxVersionsPerWeek': MAX_VERSIONS_PER_WEEK, 'maxCodesPerWeek': dict(MAX_CODES_PER_WEEK),
        'defaultCodesPerWeek': DEFAULT_CODES_PER_WEEK,
        'otherVersion': OTHER_VERSION, 'unknown': UNKNOWN, 'other': OTHER, 'refusals': list(REFUSALS),
        'metrics': {metric: [[part if isinstance(part, dict) else sorted(part) for part in shape]
                             for shape in shapes] for metric, shapes in METRICS.items()},
    }


# Ranges. Each turns a number into one of the closed labels above.

def bucket(value, edges, labels):
    """edges are the lower bounds of labels[1:]; a value below edges[0] gets labels[0]."""
    for edge, label in zip(reversed(edges), reversed(labels[1:])):
        if value >= edge:
            return label
    return labels[0]


def count_bucket(value, labels):
    edges = {DOCUMENTS: (1, 10, 100, 500, 2000), PATIENTS: (1, 10, 50, 200), WAITING: (1, 10, 50),
             ROWS: (1, 5, 10, 20, 40), PAGES: (2, 3, 6, 11), REMAINING: (1, 2, 5, 10, 50),
             CORES: (3, 5, 9, 17)}[labels]
    return bucket(value, edges, labels)


def share_bucket(part, whole):
    if not whole or part <= 0:
        return '0'
    return bucket(100 * part / whole, (1, 10, 25, 50), SHARES)


def seconds_bucket(seconds):
    return bucket(seconds, (1, 3, 10, 30, 60, 180), SECONDS)


def memory_bucket(size):
    if not size:
        return 'unknown'
    return bucket(size / 1024 ** 3, (3.5, 7.5, 15.5, 31.5), MEMORY[:-1])


def scale_bucket(factor):
    """Windows display scale from Tk's pixels per point (1.333 at 100 %)."""
    if not factor or factor <= 0:
        return 'other'
    percent = factor / (96 / 72) * 100
    for label, centre in (('100', 100), ('125', 125), ('150', 150), ('175', 175)):
        if abs(percent - centre) < 12.5:
            return label
    return '200_plus' if percent >= 187.5 else 'other'


def kind_code(kind):
    return KINDS.get(kind, 'other')


def header_code(columns):
    names = [column if column in COLUMNS else 'unknown' for column in columns][:MAX_HEADER_COLUMNS]
    return 'header.' + '|'.join(names) if names else None


def failure_code(stage, exc):
    """«stage.Exception.module.function.line» of the innermost frame in the program.

    Only names from the code are used: never the message of the exception or a value.
    """
    if stage not in ERROR_STAGES:
        stage = 'window'
    name = type(exc).__name__
    kind = name if name in EXCEPTIONS else 'Other'
    module, function, line = 'other', 'other', 1
    traceback = getattr(exc, '__traceback__', None)
    while traceback is not None:
        frame = traceback.tb_frame
        owner = str(frame.f_globals.get('__name__', ''))
        if owner == 'patient_importer' or owner.startswith('patient_importer.'):
            short = owner.rpartition('.')[2]
            module = short if short in MODULES else 'other'
            function = frame.f_code.co_name.strip('<>').lower()
            line = traceback.tb_lineno
        traceback = traceback.tb_next
    if not re.fullmatch(FUNCTION, function, re.ASCII):
        function = 'other'
    if not re.fullmatch(LINE, str(line), re.ASCII):
        line = 1
    return f'{stage}.{kind}.{module}.{function}.{line}'
