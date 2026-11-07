// Closed wire schema. Python parity is checked in tests/test_apps_script.py.
const WIRE_SCHEMA = {
  "maxCount": 100000,
  "maxReportBytes": 8192,
  "metrics": {
    "edit_categories": [
      "conclusion",
      "identity",
      "observation",
      "other",
      "reference",
      "result",
      "study_date",
      "study_type",
      "unit"
    ],
    "failure_stages": [
      "analysis",
      "archive",
      "export",
      "export_locked",
      "extraction",
      "intake",
      "review",
      "storage"
    ],
    "formats": [
      "docx",
      "image",
      "other",
      "pdf",
      "txt"
    ],
    "outcomes": [
      "duplicate",
      "error",
      "identity_review",
      "quality_review",
      "saved",
      "skipped"
    ],
    "review_outcomes": [
      "confirmed",
      "confirmed_incomplete",
      "deferred",
      "excluded",
      "reprocessed"
    ],
    "review_reasons": [
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
      "type_mismatch",
      "unknown_type",
      "unknown_unit",
      "unmapped_columns",
      "unparsed_row"
    ]
  },
  "minAttempts": 10,
  "pathInfo": "",
  "schemaVersion": 1,
  "versions": [
    "2.4.0"
  ]
};

const SHEET_NAME = 'Счётчики';
const HEADER = ['app_version', 'metric', 'code', 'total'];
const MAX_ROWS = 1024;
const MAX_VERSIONS = 8;
const HEADER_FORMAT = {
  backgroundColor: {red: 0.11, green: 0.28, blue: 0.51},
  textFormat: {bold: true, foregroundColor: {red: 1, green: 1, blue: 1}},
};
const CONTENT_TYPE = /^application\/json(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?$/i;

function doPost(event) {
  let lock;
  let locked = false;
  try {
    const report = parseReport_(event);
    if (!report) return reply_(false);
    lock = LockService.getScriptLock();
    locked = lock.tryLock(250);
    if (!locked) return reply_(false);
    addCounters_(report);
    return reply_(true);
  } catch (_) {
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

function knownCode_(metric, code) {
  return typeof metric === 'string' && typeof code === 'string'
    && Object.prototype.hasOwnProperty.call(WIRE_SCHEMA.metrics, metric)
    && WIRE_SCHEMA.metrics[metric].includes(code);
}

function validVersions_() {
  const versions = WIRE_SCHEMA.versions;
  return Array.isArray(versions) && versions.length > 0 && versions.length <= MAX_VERSIONS
    && new Set(versions).size === versions.length
    && versions.every(version => typeof version === 'string'
      && /^(?:0|[1-9][0-9]{0,3})(?:\.(?:0|[1-9][0-9]{0,3})){2}$/.test(version))
    && versions.length * Object.values(WIRE_SCHEMA.metrics)
      .reduce((total, codes) => total + codes.length, 0) < MAX_ROWS;
}

function parseReport_(event) {
  if (!validVersions_() || !baseRoute_(event)) return null;
  const data = event.postData;
  if (!data || typeof data.type !== 'string' || !CONTENT_TYPE.test(data.type)
      || !Number.isInteger(event.contentLength) || event.contentLength !== data.length
      || event.contentLength < 1 || event.contentLength > WIRE_SCHEMA.maxReportBytes
      || typeof data.contents !== 'string' || data.contents.length !== event.contentLength
      || /[^\x20-\x7e]/.test(data.contents)) return null;
  const report = JSON.parse(data.contents);
  if (!isObject_(report) || Object.keys(report).length !== 3
      || report.schema_version !== WIRE_SCHEMA.schemaVersion
      || !WIRE_SCHEMA.versions.includes(report.app_version) || !isObject_(report.counters)) return null;
  for (const [metric, bucket] of Object.entries(report.counters)) {
    if (!isObject_(bucket) || Object.keys(bucket).length === 0) return null;
    for (const [code, count] of Object.entries(bucket)) {
      if (!knownCode_(metric, code) || !Number.isInteger(count)
          || count < 1 || count > WIRE_SCHEMA.maxCount) return null;
    }
  }
  const formats = report.counters.formats || {};
  if (Object.values(formats).reduce((total, count) => total + count, 0)
      < WIRE_SCHEMA.minAttempts) return null;

  // The client sends ASCII JSON with sorted keys and no whitespace. Require
  // the same bytes so duplicate keys erased by JSON.parse cannot be accepted.
  return data.contents === canonicalJson_(report) ? report : null;
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
    if (!validVersions_()) return false;
    const properties = PropertiesService.getScriptProperties();
    let spreadsheetId = properties.getProperty('SPREADSHEET_ID');
    if (spreadsheetId === null) {
      const created = Sheets.Spreadsheets.create({
        properties: {title: 'Исследования пациентов — диагностика'},
        sheets: [{properties: {
          sheetId: 0, title: SHEET_NAME,
          gridProperties: {rowCount: MAX_ROWS, columnCount: HEADER.length, frozenRowCount: 1},
        }}],
      }, {fields: 'spreadsheetId'});
      spreadsheetId = created.spreadsheetId;
      if (!validId_(spreadsheetId)) return false;
      properties.setProperty('SPREADSHEET_ID', spreadsheetId);
    }
    if (!validId_(spreadsheetId)) return false;
    const rows = storedRows_(spreadsheetId);
    if (rows.length) return HEADER.every((title, index) => title === rows[0][index]);
    writeRows_(spreadsheetId, [HEADER], true);
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

function storedRows_(spreadsheetId) {
  const source = Sheets.Spreadsheets.get(spreadsheetId, {
    ranges: ["'" + SHEET_NAME + "'!A1:D" + MAX_ROWS],
    fields: 'sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),data(startRow,startColumn,rowData(values(userEnteredValue))))',
  });
  const sheets = source.sheets || [];
  const sheet = sheets[0];
  const properties = sheet && sheet.properties;
  const grid = properties && properties.gridProperties;
  if (sheets.length !== 1 || !grid || properties.sheetId !== 0 || properties.title !== SHEET_NAME
      || grid.rowCount !== MAX_ROWS || grid.columnCount !== HEADER.length) throw new Error();
  const ranges = sheet.data || [];
  const range = ranges[0] || {};
  if (ranges.length > 1 || (range.startRow || 0) !== 0 || (range.startColumn || 0) !== 0) throw new Error();
  const rows = range.rowData || [];
  if (rows.length > MAX_ROWS) throw new Error();
  const values = rows.map(row => (row.values || []).map(cell => {
    const value = cell.userEnteredValue;
    if (!value) return null;
    if (Object.keys(value).length !== 1) throw new Error();
    if (typeof value.stringValue === 'string') return value.stringValue;
    if (typeof value.numberValue === 'number') return value.numberValue;
    // Formula and boolean cells must never be copied into a write.
    throw new Error();
  }));
  while (values.length && values[values.length - 1].every(value => value === null)) values.pop();
  return values;
}

function addCounters_(report) {
  const spreadsheetId = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (!validId_(spreadsheetId)) throw new Error();
  const previousRows = storedRows_(spreadsheetId);
  const totals = new Map();
  if (previousRows.length) {
    const [header, ...rows] = previousRows;
    if (!HEADER.every((title, index) => title === header[index])) throw new Error();
    for (const [version, metric, code, total] of rows) {
      if (!WIRE_SCHEMA.versions.includes(version) || !knownCode_(metric, code)
          || !Number.isSafeInteger(total) || total < 1) throw new Error();
      const key = version + '|' + metric + '|' + code;
      if (totals.has(key)) throw new Error();
      totals.set(key, total);
    }
  }

  for (const [metric, bucket] of Object.entries(report.counters)) {
    for (const [code, count] of Object.entries(bucket)) {
      const key = report.app_version + '|' + metric + '|' + code;
      const previous = totals.get(key) || 0;
      totals.set(key, previous > Number.MAX_SAFE_INTEGER - count
        ? Number.MAX_SAFE_INTEGER : previous + count);
    }
  }

  const rows = [HEADER, ...Array.from(totals.keys()).sort()
    .map(key => [...key.split('|'), totals.get(key)])];
  writeRows_(spreadsheetId, rows);
}

function writeRows_(spreadsheetId, rows, formatHeader = false) {
  Sheets.Spreadsheets.batchUpdate({requests: [{updateCells: {
    start: {sheetId: 0, rowIndex: 0, columnIndex: 0},
    rows: rows.map(row => ({values: row.map(value => {
      const cell = {userEnteredValue: typeof value === 'string'
        ? {stringValue: value} : {numberValue: value}};
      if (formatHeader) cell.userEnteredFormat = HEADER_FORMAT;
      return cell;
    })})),
    fields: formatHeader ? 'userEnteredValue,userEnteredFormat' : 'userEnteredValue',
  }}]}, spreadsheetId);
}
