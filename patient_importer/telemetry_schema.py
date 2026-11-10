"""The complete wire contract for optional aggregate diagnostics."""
import re
from types import MappingProxyType

SCHEMA_VERSION = 1
MIN_ATTEMPTS = 10
MAX_COUNT = 100_000
MAX_REPORT_BYTES = 8192
COUNTERS_PATH = '/v1/counters'
_VERSION = re.compile(r'(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}', re.ASCII)

METRICS = MappingProxyType({
    'formats': frozenset({'pdf', 'docx', 'txt', 'image', 'other'}),
    'outcomes': frozenset({'saved', 'duplicate', 'identity_review', 'quality_review', 'error', 'skipped'}),
    'review_reasons': frozenset({
        'missing_identity', 'ambiguous_identity', 'identity_from_ocr', 'legacy_parser',
        'type_mismatch', 'conflicting_type', 'unknown_type', 'conflicting_date', 'missing_date',
        'lab_no_results', 'missing_narrative_result', 'ocr_value', 'ocr_text', 'ambiguous_value',
        'missing_result', 'missing_unit', 'unknown_unit', 'unmapped_columns', 'unparsed_row',
        'extraction_issue', 'OCR_USED', 'OCR_DISAGREEMENT', 'OCR_ENGINE_UNAVAILABLE',
        'OCR_LOW_CONFIDENCE', 'OCR_EMPTY', 'OCR_FAILED', 'PDF_TEXT_UNREADABLE', 'PDF_PAGE_EMPTY',
        'EMBEDDED_IMAGE_UNREADABLE', 'EMBEDDED_IMAGE_UNSUPPORTED', 'SIGNATURE_IMAGE',
        'DOCX_COMPLEX_CONTENT', 'DOCX_EXTRA_TEXT', 'page_without_patient',
    }),
    'review_outcomes': frozenset({'confirmed', 'confirmed_incomplete', 'reprocessed', 'excluded', 'deferred'}),
    'edit_categories': frozenset({
        'identity', 'study_type', 'study_date', 'result', 'unit', 'reference', 'observation', 'conclusion', 'other',
    }),
    'failure_stages': frozenset({'intake', 'extraction', 'analysis', 'storage', 'export', 'export_locked', 'archive', 'review'}),
})


def valid_version(value):
    return type(value) is str and len(value) <= 14 and _VERSION.fullmatch(value) is not None


def validate_counters(value):
    """Return fresh dictionaries containing only values from the closed schema."""
    if type(value) is not dict or not value.keys() <= METRICS.keys():
        raise ValueError('Invalid diagnostics counters')
    clean = {}
    for metric, counters in value.items():
        if type(metric) is not str or type(counters) is not dict or not counters or not counters.keys() <= METRICS[metric]:
            raise ValueError('Invalid diagnostics bucket')
        bucket = {}
        for code, count in counters.items():
            if type(code) is not str or type(count) is not int or not 1 <= count <= MAX_COUNT:
                raise ValueError('Invalid diagnostics count')
            bucket[code] = count
        clean[metric] = bucket
    return clean


def validate_report(value):
    if (type(value) is not dict or value.keys() != {'schema_version', 'app_version', 'counters'}
            or type(value['schema_version']) is not int or value['schema_version'] != SCHEMA_VERSION
            or not valid_version(value['app_version'])):
        raise ValueError('Invalid diagnostics report')
    counters = validate_counters(value['counters'])
    if not counters:
        raise ValueError('Empty diagnostics report')
    return {'schema_version': SCHEMA_VERSION, 'app_version': value['app_version'], 'counters': counters}
