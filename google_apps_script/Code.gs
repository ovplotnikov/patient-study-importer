// Closed wire schema. Python parity is checked in tests/test_apps_script.py.
const WIRE_SCHEMA = {
  "acceptedSchemas": [
    1,
    2
  ],
  "defaultCodesPerWeek": 200,
  "maxCodeLength": 120,
  "maxCodesPerWeek": {
    "errors": 60,
    "reading": 400
  },
  "maxCount": 100000,
  "maxReportBytes": 16384,
  "maxVersionsPerWeek": 12,
  "metrics": {
    "accuracy": [
      [
        [
          "rows_unchanged"
        ],
        [
          "narrative",
          "scan",
          "table"
        ]
      ],
      [
        [
          "rows_edited"
        ],
        [
          "narrative",
          "scan",
          "table"
        ]
      ],
      [
        [
          "field_edited"
        ],
        [
          "date",
          "label",
          "reference",
          "result",
          "unit"
        ],
        [
          "narrative",
          "scan",
          "table"
        ]
      ],
      [
        [
          "layout"
        ],
        [
          "accepted",
          "edited"
        ]
      ],
      [
        [
          "patient_changed"
        ],
        [
          "document",
          "folder",
          "person",
          "suggestion"
        ]
      ]
    ],
    "activity": [
      [
        [
          "active"
        ]
      ],
      [
        [
          "age"
        ],
        [
          "1_3m",
          "1_3w",
          "3m_plus",
          "week0"
        ]
      ],
      [
        [
          "os"
        ],
        [
          "other",
          "win10",
          "win11",
          "windows_other"
        ]
      ],
      [
        [
          "cores"
        ],
        [
          "17_plus",
          "1_2",
          "3_4",
          "5_8",
          "9_16"
        ]
      ],
      [
        [
          "memory"
        ],
        [
          "16_31",
          "32_plus",
          "4_7",
          "8_15",
          "lt4",
          "unknown"
        ]
      ],
      [
        [
          "scale"
        ],
        [
          "100",
          "125",
          "150",
          "175",
          "200_plus",
          "other"
        ]
      ],
      [
        [
          "documents"
        ],
        [
          "0",
          "100_499",
          "10_99",
          "1_9",
          "2000_plus",
          "500_1999"
        ]
      ],
      [
        [
          "patients"
        ],
        [
          "0",
          "10_49",
          "1_9",
          "200_plus",
          "50_199"
        ]
      ],
      [
        [
          "waiting"
        ],
        [
          "0",
          "10_49",
          "1_9",
          "50_plus"
        ]
      ],
      [
        [
          "flagged"
        ],
        [
          "0",
          "10_24",
          "1_9",
          "25_49",
          "50_plus"
        ]
      ]
    ],
    "edit_categories": [
      [
        [
          "conclusion",
          "identity",
          "observation",
          "other",
          "reference",
          "result",
          "study_date",
          "study_type",
          "unit"
        ]
      ]
    ],
    "errors": [
      [
        [
          "analysis",
          "archive",
          "export",
          "export_locked",
          "extraction",
          "import",
          "intake",
          "review",
          "startup",
          "storage",
          "update",
          "window"
        ],
        [
          "ArithmeticError",
          "AssertionError",
          "AttributeError",
          "BadZipFile",
          "BrokenProcessPool",
          "Cancelled",
          "ConnectionError",
          "DatabaseError",
          "EOFError",
          "FileExistsError",
          "FileNotFoundError",
          "ImportError",
          "IndexError",
          "IntegrityError",
          "InvalidFileException",
          "IsADirectoryError",
          "JSONDecodeError",
          "KeyError",
          "LockException",
          "LookupError",
          "MemoryError",
          "ModuleNotFoundError",
          "NameError",
          "NotADirectoryError",
          "NotImplementedError",
          "OSError",
          "OperationalError",
          "Other",
          "OverflowError",
          "PdfReadError",
          "PdfiumError",
          "PermissionError",
          "ReadingFailed",
          "RecursionError",
          "ReviewCommittedError",
          "RuntimeError",
          "StopIteration",
          "SystemError",
          "TclError",
          "TimeoutError",
          "TypeError",
          "UnboundLocalError",
          "UnicodeDecodeError",
          "UnicodeEncodeError",
          "UnicodeError",
          "UnidentifiedImageError",
          "UpdateError",
          "ValueError",
          "ZeroDivisionError"
        ],
        [
          "analysis",
          "app",
          "clinical_sheets",
          "diagnostic_events",
          "diagnostics_config",
          "diagnostics_ui",
          "extraction",
          "gui",
          "help_view",
          "intake",
          "ocr",
          "other",
          "parallel",
          "parsing",
          "pdf_render",
          "quality",
          "quality_ui",
          "reading",
          "review_queue",
          "smoke",
          "storage",
          "support",
          "telemetry",
          "telemetry_schema",
          "ui",
          "ui_smoke",
          "updater",
          "workbook"
        ],
        {
          "pattern": "[a-z_][a-z0-9_]{0,39}"
        },
        {
          "pattern": "[1-9][0-9]{0,4}"
        }
      ],
      [
        [
          "session_crash"
        ],
        [
          "idle",
          "processing"
        ]
      ],
      [
        [
          "worker_crash"
        ]
      ],
      [
        [
          "export_locked"
        ]
      ]
    ],
    "failure_stages": [
      [
        [
          "analysis",
          "archive",
          "export",
          "export_locked",
          "extraction",
          "intake",
          "review",
          "storage"
        ]
      ]
    ],
    "formats": [
      [
        [
          "docx",
          "image",
          "other",
          "pdf",
          "txt"
        ]
      ]
    ],
    "outcomes": [
      [
        [
          "duplicate",
          "error",
          "identity_review",
          "quality_review",
          "saved",
          "saved_flagged",
          "skipped"
        ]
      ]
    ],
    "path": [
      [
        [
          "add_drop",
          "add_files",
          "add_folder",
          "file_opened",
          "file_revealed",
          "import_done",
          "import_failed",
          "import_started",
          "import_stopped",
          "patient_candidate",
          "patient_draft_edited",
          "patient_draft_kept",
          "patient_list",
          "patient_suggested",
          "patient_typed",
          "patient_window",
          "quality_window",
          "reparse_accepted",
          "reparse_cancelled",
          "reparse_shown",
          "start",
          "table_opened",
          "update_accepted",
          "update_installed",
          "update_offered"
        ]
      ],
      [
        [
          "closed"
        ],
        [
          "patient",
          "quality"
        ],
        [
          "0",
          "1",
          "10_49",
          "2_4",
          "50_plus",
          "5_9"
        ]
      ],
      [
        [
          "update_failed"
        ],
        [
          "check",
          "download",
          "install"
        ]
      ]
    ],
    "reading": [
      [
        [
          "outcome"
        ],
        [
          "biochemistry",
          "cbc",
          "cystography",
          "cystometry",
          "doppler",
          "lab_other",
          "mri",
          "nechiporenko",
          "other",
          "reberg",
          "scintigraphy",
          "ultrasound",
          "unknown",
          "urine",
          "urine_culture",
          "urine_protein",
          "urography",
          "visit",
          "zimnitsky"
        ],
        [
          "duplicate",
          "error",
          "identity_review",
          "quality_review",
          "saved",
          "saved_flagged",
          "skipped"
        ]
      ],
      [
        [
          "flag"
        ],
        [
          "biochemistry",
          "cbc",
          "cystography",
          "cystometry",
          "doppler",
          "lab_other",
          "mri",
          "nechiporenko",
          "other",
          "reberg",
          "scintigraphy",
          "ultrasound",
          "unknown",
          "urine",
          "urine_culture",
          "urine_protein",
          "urography",
          "visit",
          "zimnitsky"
        ],
        [
          "DOCX_COMPLEX_CONTENT",
          "DOCX_EXTRA_TEXT",
          "EMBEDDED_IMAGE_UNREADABLE",
          "EMBEDDED_IMAGE_UNSUPPORTED",
          "OCR_DISAGREEMENT",
          "OCR_EMPTY",
          "OCR_ENGINE_UNAVAILABLE",
          "OCR_FAILED",
          "OCR_LOW_CONFIDENCE",
          "OCR_USED",
          "PDF_PAGE_EMPTY",
          "PDF_TEXT_UNREADABLE",
          "SIGNATURE_IMAGE",
          "ambiguous_identity",
          "ambiguous_value",
          "conflicting_date",
          "conflicting_type",
          "extraction_issue",
          "identity_from_ocr",
          "lab_no_results",
          "legacy_parser",
          "missing_date",
          "missing_identity",
          "missing_narrative_result",
          "missing_result",
          "missing_unit",
          "ocr_text",
          "ocr_value",
          "page_without_patient",
          "possible_duplicate",
          "reprocess_conflict",
          "type_mismatch",
          "unknown_type",
          "unknown_unit",
          "unmapped_columns",
          "unparsed_row"
        ]
      ],
      [
        [
          "lab_rows"
        ],
        [
          "0",
          "10_19",
          "1_4",
          "20_39",
          "40_plus",
          "5_9"
        ]
      ],
      [
        [
          "unparsed"
        ],
        [
          "0",
          "10_24",
          "1_9",
          "25_49",
          "50_plus"
        ]
      ],
      [
        [
          "header"
        ],
        {
          "pattern": "(?:label|result|unit|reference|flag|comment|test_system|history|unknown)(?:\\|(?:label|result|unit|reference|flag|comment|test_system|history|unknown)){0,7}"
        }
      ],
      [
        [
          "patient"
        ],
        [
          "ambiguous_identity",
          "changed_identity",
          "conflict",
          "document",
          "folder",
          "identity_from_ocr",
          "missing_identity",
          "person"
        ]
      ],
      [
        [
          "scan_pages"
        ],
        [
          "1",
          "11_plus",
          "2",
          "3_5",
          "6_10"
        ]
      ],
      [
        [
          "scan_reading"
        ],
        [
          "agree",
          "disagree",
          "single_engine"
        ]
      ]
    ],
    "review_outcomes": [
      [
        [
          "confirmed",
          "confirmed_incomplete",
          "deferred",
          "excluded",
          "reprocessed"
        ]
      ]
    ],
    "review_reasons": [
      [
        [
          "DOCX_COMPLEX_CONTENT",
          "DOCX_EXTRA_TEXT",
          "EMBEDDED_IMAGE_UNREADABLE",
          "EMBEDDED_IMAGE_UNSUPPORTED",
          "OCR_DISAGREEMENT",
          "OCR_EMPTY",
          "OCR_ENGINE_UNAVAILABLE",
          "OCR_FAILED",
          "OCR_LOW_CONFIDENCE",
          "OCR_USED",
          "PDF_PAGE_EMPTY",
          "PDF_TEXT_UNREADABLE",
          "SIGNATURE_IMAGE",
          "ambiguous_identity",
          "ambiguous_value",
          "conflicting_date",
          "conflicting_type",
          "extraction_issue",
          "identity_from_ocr",
          "lab_no_results",
          "legacy_parser",
          "missing_date",
          "missing_identity",
          "missing_narrative_result",
          "missing_result",
          "missing_unit",
          "ocr_text",
          "ocr_value",
          "page_without_patient",
          "possible_duplicate",
          "reprocess_conflict",
          "type_mismatch",
          "unknown_type",
          "unknown_unit",
          "unmapped_columns",
          "unparsed_row"
        ]
      ]
    ],
    "speed": [
      [
        [
          "export",
          "scan",
          "startup",
          "text"
        ],
        [
          "10_30s",
          "1_3m",
          "1_3s",
          "30_60s",
          "3_10s",
          "3m_plus",
          "lt1s"
        ]
      ]
    ]
  },
  "other": "_other",
  "otherVersion": "other",
  "pathInfo": "",
  "refusals": [
    "format",
    "too_large",
    "content_type",
    "rate_limited",
    "busy",
    "storage",
    "full"
  ],
  "schemaVersion": 2,
  "unknown": "_unknown"
};

const RECEPTION_SHEET = 'Приём';
const RECEPTION_HEADER = ['week', 'outcome', 'reason', 'total'];
const COUNTER_HEADER = ['week', 'app_version', 'metric', 'code', 'total'];
const WEEK_ROWS = 6000;
const RECEPTION_ROWS = 2000;
const PENDING_PROPERTY = 'PENDING_REFUSALS';
const MAX_PENDING_KEYS = 64;
const HEADER_FORMAT = {
  backgroundColor: {red: 0.11, green: 0.28, blue: 0.51},
  textFormat: {bold: true, foregroundColor: {red: 1, green: 1, blue: 1}},
};
const CONTENT_TYPE = /^application\/json(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?$/i;
const VERSION = /^(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}$/;
const NAME = /^[a-z][a-z0-9_]{0,31}$/;
const WEEK = /^[0-9]{4}-W[0-9]{2}$/;

// A week of this server's clock, «2026-W47». The program never sends a time.
function now_() {
  return new Date();
}

function weekOf_(date) {
  const day = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const weekday = day.getUTCDay() || 7;
  day.setUTCDate(day.getUTCDate() + 4 - weekday);
  const year = day.getUTCFullYear();
  const week = Math.ceil(((day - Date.UTC(year, 0, 1)) / 86400000 + 1) / 7);
  return year + '-W' + String(week).padStart(2, '0');
}

function doPost(event) {
  const week = weekOf_(now_());
  let lock;
  let locked = false;
  try {
    const parsed = parseReport_(event);
    lock = LockService.getScriptLock();
    locked = lock.tryLock(250);
    if (!locked) {
      notePending_(week, 'busy');
      return reply_(false);
    }
    const spreadsheetId = spreadsheet_();
    const index = sheetsIndex_(spreadsheetId);
    const reception = [];
    if (parsed.report) {
      const stored = addCounters_(spreadsheetId, index, week, parsed.report);
      reception.push([week, stored ? 'accepted' : 'refused',
        stored ? 'schema_' + parsed.report.schema_version : 'full']);
      countReception_(spreadsheetId, index, reception);
      return reply_(stored);
    }
    reception.push([week, 'refused', parsed.reason]);
    countReception_(spreadsheetId, index, reception);
    return reply_(false);
  } catch (_) {
    try { notePending_(week, 'storage'); } catch (__) { /* no diagnostic details */ }
    return reply_(false);
  } finally {
    if (locked) {
      try { lock.releaseLock(); } catch (_) { /* no diagnostic details */ }
    }
  }
}

function doGet(event) {
  return reply_(baseRoute_(event));
}

function reply_(ok) {
  return ContentService.createTextOutput(ok ? '{"ok":true}' : '{"ok":false}')
    .setMimeType(ContentService.MimeType.JSON);
}

function baseRoute_(event) {
  return Boolean(event
    && (event.pathInfo === undefined || event.pathInfo === null || event.pathInfo === WIRE_SCHEMA.pathInfo)
    && (event.queryString === null || event.queryString === ''));
}

function isObject_(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

const PATTERNS_ = {};
function partMatches_(part, value) {
  if (Array.isArray(part)) return part.includes(value);
  if (!PATTERNS_[part.pattern]) PATTERNS_[part.pattern] = new RegExp('^(?:' + part.pattern + ')$');
  return PATTERNS_[part.pattern].test(value);
}

function validCode_(metric, code) {
  if (typeof code !== 'string' || code.length < 1 || code.length > WIRE_SCHEMA.maxCodeLength) return false;
  const shapes = WIRE_SCHEMA.metrics[metric];
  const parts = code.split('.');
  return shapes.some(shape => shape.length === parts.length
    && shape.every((part, index) => partMatches_(part, parts[index])));
}

function validCount_(count) {
  return Number.isInteger(count) && count >= 1 && count <= WIRE_SCHEMA.maxCount;
}

// Same rules as receive_report in patient_importer/telemetry_schema.py.
function receiveReport_(value) {
  if (!isObject_(value) || Object.keys(value).length !== 3
      || !['schema_version', 'app_version', 'counters'].every(key => Object.prototype.hasOwnProperty.call(value, key))
      || !Number.isInteger(value.schema_version) || !WIRE_SCHEMA.acceptedSchemas.includes(value.schema_version)
      || typeof value.app_version !== 'string' || value.app_version.length > 14 || !VERSION.test(value.app_version)
      || !isObject_(value.counters) || Object.keys(value.counters).length === 0) return {reason: 'format'};
  const counters = {};
  for (const [metric, bucket] of Object.entries(value.counters)) {
    if (!isObject_(bucket) || Object.keys(bucket).length === 0) return {reason: 'format'};
    const known = NAME.test(metric) && Object.prototype.hasOwnProperty.call(WIRE_SCHEMA.metrics, metric);
    for (const [code, count] of Object.entries(bucket)) {
      if (!validCount_(count)) return {reason: 'format'};
      const group = known ? metric : WIRE_SCHEMA.unknown;
      const name = known && validCode_(metric, code) ? code : WIRE_SCHEMA.unknown;
      counters[group] = counters[group] || {};
      counters[group][name] = Math.min(WIRE_SCHEMA.maxCount, (counters[group][name] || 0) + count);
    }
  }
  return {report: {schema_version: value.schema_version, app_version: value.app_version, counters}};
}

function parseReport_(event) {
  if (!baseRoute_(event)) return {reason: 'format'};
  const data = event.postData;
  if (!data || typeof data.type !== 'string' || !CONTENT_TYPE.test(data.type)) return {reason: 'content_type'};
  if (Number.isInteger(event.contentLength) && event.contentLength > WIRE_SCHEMA.maxReportBytes) {
    return {reason: 'too_large'};
  }
  if (!Number.isInteger(event.contentLength) || event.contentLength !== data.length
      || event.contentLength < 1 || typeof data.contents !== 'string'
      || data.contents.length !== event.contentLength || /[^\x20-\x7e]/.test(data.contents)) return {reason: 'format'};
  let report;
  try {
    report = JSON.parse(data.contents);
  } catch (_) {
    return {reason: 'format'};
  }
  // The client sends ASCII JSON with sorted keys and no whitespace. Require
  // the same bytes so duplicate keys erased by JSON.parse cannot be accepted.
  if (data.contents !== canonicalJson_(report)) return {reason: 'format'};
  return receiveReport_(report);
}

function canonicalJson_(value) {
  if (!isObject_(value)) return JSON.stringify(value);
  return '{' + Object.keys(value).sort().map(key =>
    JSON.stringify(key) + ':' + canonicalJson_(value[key])).join(',') + '}';
}

// Run once from the owner’s script editor. The web app routes expose only
// doGet and doPost; setup is not part of the anonymous HTTP interface.
function initializeSpreadsheet() {
  let lock;
  let locked = false;
  try {
    lock = LockService.getScriptLock();
    locked = lock.tryLock(250);
    if (!locked) return false;
    const properties = PropertiesService.getScriptProperties();
    let spreadsheetId = properties.getProperty('SPREADSHEET_ID');
    let fresh = false;
    if (spreadsheetId === null) {
      fresh = true;
      const created = Sheets.Spreadsheets.create({
        properties: {title: 'Исследования пациентов — диагностика'},
        sheets: [{properties: {
          title: RECEPTION_SHEET,
          gridProperties: {rowCount: RECEPTION_ROWS, columnCount: RECEPTION_HEADER.length, frozenRowCount: 1},
        }}],
      }, {fields: 'spreadsheetId'});
      spreadsheetId = created.spreadsheetId;
      if (!validId_(spreadsheetId)) return false;
      properties.setProperty('SPREADSHEET_ID', spreadsheetId);
    }
    if (!validId_(spreadsheetId)) return false;
    // A spreadsheet of the first receiver keeps its sheet «Счётчики» as it is.
    const index = sheetsIndex_(spreadsheetId);
    const sheetId = ensureSheet_(spreadsheetId, index, RECEPTION_SHEET, RECEPTION_HEADER, RECEPTION_ROWS);
    if (fresh) writeRows_(spreadsheetId, sheetId, [RECEPTION_HEADER], true);
    return true;
  } catch (_) {
    return false;
  } finally {
    if (locked) {
      try { lock.releaseLock(); } catch (_) { /* no diagnostic details */ }
    }
  }
}

function validId_(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{20,100}$/.test(value);
}

function spreadsheet_() {
  const spreadsheetId = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (!validId_(spreadsheetId)) throw new Error();
  return spreadsheetId;
}

function sheetsIndex_(spreadsheetId) {
  const source = Sheets.Spreadsheets.get(spreadsheetId, {
    fields: 'sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))',
  });
  const index = new Map();
  for (const sheet of source.sheets || []) index.set(sheet.properties.title, sheet.properties);
  return index;
}

// One sheet per receiving week keeps every read and write small.
function ensureSheet_(spreadsheetId, index, title, header, rowCount) {
  const existing = index.get(title);
  if (existing) {
    const grid = existing.gridProperties || {};
    if (grid.rowCount !== rowCount || grid.columnCount !== header.length) throw new Error();
    return existing.sheetId;
  }
  const reply = Sheets.Spreadsheets.batchUpdate({requests: [{addSheet: {properties: {
    title, gridProperties: {rowCount, columnCount: header.length, frozenRowCount: 1},
  }}}]}, spreadsheetId);
  const properties = reply.replies[0].addSheet.properties;
  index.set(title, properties);
  writeRows_(spreadsheetId, properties.sheetId, [header], true);
  return properties.sheetId;
}

function storedRows_(spreadsheetId, title, header, rowCount) {
  const last = String.fromCharCode(64 + header.length);
  const source = Sheets.Spreadsheets.get(spreadsheetId, {
    ranges: ["'" + title + "'!A1:" + last + rowCount],
    fields: 'sheets(data(startRow,startColumn,rowData(values(userEnteredValue))))',
  });
  const sheets = source.sheets || [];
  const ranges = (sheets[0] && sheets[0].data) || [];
  const range = ranges[0] || {};
  if (sheets.length !== 1 || ranges.length > 1 || (range.startRow || 0) !== 0 || (range.startColumn || 0) !== 0) {
    throw new Error();
  }
  const rows = (range.rowData || []).map(row => (row.values || []).map(cell => {
    const value = cell.userEnteredValue;
    if (!value) return null;
    if (Object.keys(value).length !== 1) throw new Error();
    if (typeof value.stringValue === 'string') return value.stringValue;
    if (typeof value.numberValue === 'number') return value.numberValue;
    // Formula and boolean cells must never be copied into a write.
    throw new Error();
  }));
  while (rows.length && rows[rows.length - 1].every(value => value === null)) rows.pop();
  if (!rows.length || !header.every((name, column) => rows[0][column] === name)) throw new Error();
  return rows.slice(1);
}

function validTotal_(total) {
  return Number.isSafeInteger(total) && total >= 1;
}

function addCounters_(spreadsheetId, index, week, report) {
  const title = week;
  const sheetId = ensureSheet_(spreadsheetId, index, title, COUNTER_HEADER, WEEK_ROWS);
  const totals = new Map();
  const versions = new Set();
  const codes = new Map();
  for (const [rowWeek, version, metric, code, total] of storedRows_(spreadsheetId, title, COUNTER_HEADER, WEEK_ROWS)) {
    if (rowWeek !== week || typeof version !== 'string' || typeof metric !== 'string' || typeof code !== 'string'
        || !(VERSION.test(version) || version === WIRE_SCHEMA.otherVersion)
        || !(NAME.test(metric) || metric === WIRE_SCHEMA.unknown)
        || code.length > WIRE_SCHEMA.maxCodeLength || /[^\x21-\x7e]/.test(code) || !validTotal_(total)) {
      throw new Error();
    }
    const key = JSON.stringify([version, metric, code]);
    if (totals.has(key)) throw new Error();
    totals.set(key, total);
    versions.add(version);
    if (!codes.has(metric)) codes.set(metric, new Set());
    codes.get(metric).add(code);
  }
  versions.delete(WIRE_SCHEMA.otherVersion);
  let version = report.app_version;
  if (!versions.has(version) && versions.size >= WIRE_SCHEMA.maxVersionsPerWeek) version = WIRE_SCHEMA.otherVersion;
  for (const [metric, bucket] of Object.entries(report.counters)) {
    if (!codes.has(metric)) codes.set(metric, new Set());
    const known = codes.get(metric);
    const limit = Object.prototype.hasOwnProperty.call(WIRE_SCHEMA.maxCodesPerWeek, metric)
      ? WIRE_SCHEMA.maxCodesPerWeek[metric] : WIRE_SCHEMA.defaultCodesPerWeek;
    for (const [given, count] of Object.entries(bucket)) {
      let code = given;
      const counted = known.size - (known.has(WIRE_SCHEMA.other) ? 1 : 0);
      if (!known.has(code) && counted >= limit) code = WIRE_SCHEMA.other;
      known.add(code);
      const key = JSON.stringify([version, metric, code]);
      const previous = totals.get(key) || 0;
      totals.set(key, previous > Number.MAX_SAFE_INTEGER - count ? Number.MAX_SAFE_INTEGER : previous + count);
    }
  }
  if (totals.size + 1 > WEEK_ROWS) return false;
  const rows = [COUNTER_HEADER, ...Array.from(totals.keys()).sort()
    .map(key => [week, ...JSON.parse(key), totals.get(key)])];
  writeRows_(spreadsheetId, sheetId, rows);
  return true;
}

// Refusals seen while the lock was busy wait in a script property. Without the lock they
// can be undercounted; they are merged into «Приём» by the next request that holds it.
function notePending_(week, reason) {
  const properties = PropertiesService.getScriptProperties();
  let pending = {};
  try {
    pending = JSON.parse(properties.getProperty(PENDING_PROPERTY) || '{}');
  } catch (_) {
    pending = {};
  }
  if (!isObject_(pending)) pending = {};
  const key = week + '|' + reason;
  if (!(key in pending) && Object.keys(pending).length >= MAX_PENDING_KEYS) return;
  pending[key] = Math.min(WIRE_SCHEMA.maxCount, (Number.isInteger(pending[key]) ? pending[key] : 0) + 1);
  properties.setProperty(PENDING_PROPERTY, JSON.stringify(pending));
}

function countReception_(spreadsheetId, index, events) {
  const sheetId = ensureSheet_(spreadsheetId, index, RECEPTION_SHEET, RECEPTION_HEADER, RECEPTION_ROWS);
  const totals = new Map();
  for (const [week, outcome, reason, total] of storedRows_(spreadsheetId, RECEPTION_SHEET, RECEPTION_HEADER, RECEPTION_ROWS)) {
    if (typeof week !== 'string' || !WEEK.test(week) || !['accepted', 'refused'].includes(outcome)
        || typeof reason !== 'string' || !NAME.test(reason) || !validTotal_(total)) throw new Error();
    totals.set(JSON.stringify([week, outcome, reason]), total);
  }
  const properties = PropertiesService.getScriptProperties();
  let pending = {};
  try {
    pending = JSON.parse(properties.getProperty(PENDING_PROPERTY) || '{}');
  } catch (_) {
    pending = {};
  }
  const all = events.map(event => [...event, 1]);
  if (isObject_(pending)) {
    for (const [key, count] of Object.entries(pending)) {
      const [week, reason] = key.split('|');
      if (WEEK.test(week) && WIRE_SCHEMA.refusals.includes(reason) && validCount_(count)) all.push([week, 'refused', reason, count]);
    }
  }
  for (const [week, outcome, reason, count] of all) {
    const key = JSON.stringify([week, outcome, reason]);
    totals.set(key, Math.min(Number.MAX_SAFE_INTEGER, (totals.get(key) || 0) + count));
  }
  if (totals.size + 1 > RECEPTION_ROWS) throw new Error();
  writeRows_(spreadsheetId, sheetId, [RECEPTION_HEADER, ...Array.from(totals.keys()).sort()
    .map(key => [...JSON.parse(key), totals.get(key)])]);
  if (Object.keys(pending || {}).length) properties.deleteProperty(PENDING_PROPERTY);
}

function writeRows_(spreadsheetId, sheetId, rows, formatHeader = false) {
  Sheets.Spreadsheets.batchUpdate({requests: [{updateCells: {
    start: {sheetId, rowIndex: 0, columnIndex: 0},
    rows: rows.map(row => ({values: row.map(value => {
      const cell = {userEnteredValue: typeof value === 'string'
        ? {stringValue: value} : {numberValue: value}};
      if (formatHeader) cell.userEnteredFormat = HEADER_FORMAT;
      return cell;
    })})),
    fields: formatHeader ? 'userEnteredValue,userEnteredFormat' : 'userEnteredValue',
  }}]}, spreadsheetId);
}
