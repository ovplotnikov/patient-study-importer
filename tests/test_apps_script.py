"""Exercise the Apps Script receiver in V8 with synthetic Sheets API responses."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from patient_importer.telemetry_schema import (
    COUNTERS_PATH, MAX_COUNT, MAX_REPORT_BYTES, METRICS, MIN_ATTEMPTS, SCHEMA_VERSION,
)

ROOT = Path(__file__).resolve().parents[1] / 'google_apps_script'
NODE = shutil.which('node')
VERSION = '2.4.0'
MARKER = 'SYNTHETIC_PRIVATE_MARKER'
ID = 'SyntheticSheetId_a1b2c3d4e5f6'
HEADER = ['app_version', 'metric', 'code', 'total']


HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let id = input.initialized ? input.id : null;
let held = false;
let rows = input.rows;
let created = [];
let writes = [];
let reads = [];
let locks = 0;
let releases = 0;
let propertyWrites = [];
let logs = [];
let grid;

function fieldPaths(mask) {
  const tokens = mask.match(/[A-Za-z]\w*|[(),]/g);
  assert.equal(tokens.join(''), mask);
  const leaves = [];
  let index = 0;
  function parse(prefix) {
    while (true) {
      const name = tokens[index++];
      assert.match(name, /^[A-Za-z]\w*$/);
      const path = prefix + name;
      if (tokens[index] === '(') {
        index++;
        parse(path + '.');
        assert.equal(tokens[index++], ')');
      } else {
        leaves.push(path);
      }
      if (tokens[index] !== ',') return;
      index++;
    }
  }
  parse('');
  assert.equal(index, tokens.length);
  return leaves.sort();
}

const context = vm.createContext({
  console: {log: (...args) => logs.push(args), error: (...args) => logs.push(args)},
  Logger: {log: (...args) => logs.push(args)},
  ContentService: {MimeType: {JSON: 'json'}, createTextOutput: body => ({
    setMimeType: mime => ({body, mime}),
  })},
  LockService: {getScriptLock: () => ({
    tryLock: timeout => {
      assert.equal(timeout, 250);
      locks++;
      if (input.busy) return false;
      assert.equal(held, false);
      held = true;
      return true;
    },
    releaseLock: () => {
      assert.equal(held, true);
      held = false;
      releases++;
    },
  })},
  PropertiesService: {getScriptProperties: () => ({
    getProperty: name => {
      assert.equal(name, 'SPREADSHEET_ID');
      assert.equal(held, true);
      return id;
    },
    setProperty: (name, value) => {
      assert.equal(name, 'SPREADSHEET_ID');
      assert.equal(held, true);
      propertyWrites.push([name, value]);
      id = value;
    },
  })},
  Sheets: {Spreadsheets: {
    create: (request, options) => {
      assert.equal(held, true);
      assert.equal(options.fields, 'spreadsheetId');
      created.push(request);
      return {spreadsheetId: input.id};
    },
    get: (spreadsheetId, options) => {
      assert.equal(held, true);
      assert.equal(spreadsheetId, input.id);
      assert.deepEqual(fieldPaths(options.fields), [
        'sheets.data.rowData.values.userEnteredValue',
        'sheets.data.startColumn',
        'sheets.data.startRow',
        'sheets.properties.gridProperties.columnCount',
        'sheets.properties.gridProperties.rowCount',
        'sheets.properties.sheetId',
        'sheets.properties.title',
      ]);
      if (input.error === 'get') throw Error(input.marker);
      reads.push(options);
      return {sheets: [{
        properties: {sheetId: 0, title: 'Счётчики', gridProperties: grid},
        data: [{rowData: rows.map(row => ({values: row.map(value => {
          if (value === null) return {};
          if (typeof value === 'object') return {userEnteredValue: value};
          return {userEnteredValue: typeof value === 'string' ? {stringValue: value} : {numberValue: value}};
        })}))}],
      }]};
    },
    batchUpdate: (request, spreadsheetId) => {
      assert.equal(held, true);
      assert.equal(spreadsheetId, input.id);
      if (input.error === 'write') throw Error(input.marker);
      assert.equal(request.requests.length, 1);
      const operation = request.requests[0].updateCells;
      assert.ok(['userEnteredValue', 'userEnteredValue,userEnteredFormat'].includes(operation.fields));
      assert.equal(operation.start.sheetId, 0);
      assert.equal(operation.start.rowIndex, 0);
      assert.equal(operation.start.columnIndex, 0);
      writes.push(request);
      rows = operation.rows.map(row => row.values.map(cell =>
        cell.userEnteredValue.stringValue ?? cell.userEnteredValue.numberValue));
    },
  }},
});
vm.runInContext(input.code, context, {filename: 'Code.gs'});
if (input.versions) {
  context.versions = input.versions;
  vm.runInContext('WIRE_SCHEMA.versions = versions', context);
}
grid = {rowCount: vm.runInContext('MAX_ROWS', context), columnCount: input.extraColumns ? 5 : 4};
const result = input.steps.map(step => {
  if (step.action === 'init') return vm.runInContext('initializeSpreadsheet()', context);
  context.event = step.event;
  return vm.runInContext(step.action === 'get' ? 'doGet(event)' : 'doPost(event)', context);
});
process.stdout.write(JSON.stringify({
  schema: vm.runInContext('WIRE_SCHEMA', context),
  created, writes, reads, locks, releases, propertyWrites, logs, rows, result,
}));
"""


def report(count=10):
    return {'schema_version': SCHEMA_VERSION, 'app_version': VERSION, 'counters': {
        'formats': {'pdf': count}, 'outcomes': {'saved': count},
    }}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def post(body=None, *, path=COUNTERS_PATH.removeprefix('/'), query=None, mime='application/json'):
    body = canonical(report()) if body is None else body
    length = len(body.encode('utf8'))
    return {'action': 'post', 'event': {
        'pathInfo': path, 'queryString': query, 'contentLength': length,
        'postData': {'type': mime, 'length': length, 'contents': body},
    }}


class ManifestTests(unittest.TestCase):
    def test_minimal_scope_and_logging_settings(self):
        manifest = json.loads((ROOT / 'appsscript.json').read_text(encoding='utf8'))
        self.assertEqual(manifest['exceptionLogging'], 'NONE')
        self.assertEqual(manifest['oauthScopes'], ['https://www.googleapis.com/auth/drive.file'])
        self.assertEqual(manifest['dependencies']['enabledAdvancedServices'], [
            {'userSymbol': 'Sheets', 'version': 'v4', 'serviceId': 'sheets'},
        ])
        self.assertEqual(manifest['webapp'], {'executeAs': 'USER_DEPLOYING', 'access': 'ANYONE_ANONYMOUS'})
        code = (ROOT / 'Code.gs').read_text(encoding='utf8')
        for forbidden in ('DriveApp', 'SpreadsheetApp', 'UrlFetchApp', 'Logger.', 'console.', 'Session.',
                          'ScriptApp.getOAuthToken'):
            self.assertNotIn(forbidden, code)


@unittest.skipUnless(NODE, 'Node.js required for the Apps Script V8 tests')
class AppsScriptTests(unittest.TestCase):
    def run_script(self, steps, **options):
        data = {
            'code': (ROOT / 'Code.gs').read_text(encoding='utf8'),
            'steps': steps, 'id': ID, 'marker': MARKER, 'rows': [], 'initialized': True,
        }
        data.update(options)
        result = subprocess.run([NODE, '-e', HARNESS], input=json.dumps(data), encoding='utf-8',
                                capture_output=True, timeout=10, check=True)
        self.assertEqual(result.stderr, '')
        self.assertNotIn(MARKER, result.stdout)
        state = json.loads(result.stdout)
        self.assertEqual(state['logs'], [])
        return state

    def test_wire_schema_matches_python(self):
        result = self.run_script([])
        self.assertEqual(result['schema'], {
            'schemaVersion': SCHEMA_VERSION,
            'pathInfo': COUNTERS_PATH.removeprefix('/'),
            'maxReportBytes': MAX_REPORT_BYTES,
            'maxCount': MAX_COUNT,
            'minAttempts': MIN_ATTEMPTS,
            'versions': [VERSION],
            'metrics': {key: sorted(codes) for key, codes in METRICS.items()},
        })

    def test_owner_setup_is_idempotent_and_uses_only_one_sheet(self):
        result = self.run_script([{'action': 'init'}, {'action': 'init'}], initialized=False)
        self.assertEqual(result['result'], [True, True])
        self.assertEqual(len(result['created']), 1)
        properties, = [sheet['properties'] for sheet in result['created'][0]['sheets']]
        self.assertEqual(properties['title'], 'Счётчики')
        self.assertEqual(properties['gridProperties']['frozenRowCount'], 1)
        self.assertEqual(properties['gridProperties']['columnCount'], 4)
        self.assertEqual(properties['gridProperties']['rowCount'], 1024)
        self.assertEqual(result['propertyWrites'], [['SPREADSHEET_ID', ID]])
        self.assertEqual(result['releases'], 2)
        self.assertEqual(result['rows'], [HEADER])
        header_write, = result['writes']
        header_cells = header_write['requests'][0]['updateCells']['rows'][0]['values']
        self.assertTrue(all(cell['userEnteredFormat']['textFormat']['bold'] for cell in header_cells))

    def test_later_version_can_share_existing_grid_with_previous_version(self):
        future = '2.5.0'
        body = canonical({**report(), 'app_version': future})
        result = self.run_script([post(body)], versions=[VERSION, future], rows=[
            HEADER, [VERSION, 'formats', 'pdf', 12],
        ])
        self.assertEqual(result['result'][0]['body'], '{"ok":true}')
        self.assertEqual(result['reads'][0]['ranges'], ["'Счётчики'!A1:D1024"])
        self.assertEqual(result['rows'], [HEADER, [VERSION, 'formats', 'pdf', 12],
                                          [future, 'formats', 'pdf', 10], [future, 'outcomes', 'saved', 10]])
        self.assertEqual(result['created'], [])

    def test_posts_merge_totals_and_health_does_not_read_sheets(self):
        result = self.run_script([
            post(), post(canonical(report(12)), mime='application/json; charset=UTF-8'),
            {'action': 'get', 'event': {'pathInfo': 'healthz', 'queryString': None}},
            {'action': 'get', 'event': {'pathInfo': 'v1/counters', 'queryString': None}},
        ])
        self.assertEqual(result['result'], [
            {'body': '{"ok":true}', 'mime': 'json'},
            {'body': '{"ok":true}', 'mime': 'json'},
            {'body': '{"ok":true}', 'mime': 'json'},
            {'body': '{"ok":false}', 'mime': 'json'},
        ])
        self.assertEqual(result['rows'], [HEADER, [VERSION, 'formats', 'pdf', 22],
                                          [VERSION, 'outcomes', 'saved', 22]])
        self.assertEqual(len(result['reads']), 2)
        self.assertEqual(result['releases'], 2)
        self.assertEqual(result['propertyWrites'], [])

    def test_invalid_payloads_are_rejected_before_google_storage(self):
        good = canonical(report())
        wrong_length = post()
        wrong_length['event']['postData']['length'] -= 1
        cases = [
            post(canonical({**report(), 'filename': MARKER})),
            post(canonical({**report(), 'app_version': '9.12.43'})),
            post(canonical({**report(), 'counters': {'formats': {'pdf': 10, MARKER: 1}}})),
            post(canonical(report(9))),
            post(canonical(report(True))),
            post(canonical(report(MAX_COUNT + 1))),
            post(canonical(report(10.5))),
            post(canonical(report(10.0))),
            post(good.replace('"pdf":10', '"pdf":10,"pdf":10')),
            post(good.replace('"app_version":', '"app_version":"2.4.0","app_version":')),
            post(good.replace('"pdf":10', '"pdf":1e1')),
            post(json.dumps(report(), indent=2)),
            post(MARKER * 1000), post('☠'),
            post(query=MARKER), post(path='/' + COUNTERS_PATH.removeprefix('/')),
            post(mime='multipart/form-data; boundary=' + MARKER),
            wrong_length,
        ]
        result = self.run_script(cases)
        self.assertEqual(result['result'], [{'body': '{"ok":false}', 'mime': 'json'}] * len(cases))
        self.assertEqual(result['locks'], 0)
        self.assertEqual(result['reads'], [])
        self.assertEqual(result['writes'], [])

    def test_lock_busy_configuration_and_api_failure_are_generic(self):
        for options in ({'busy': True}, {'initialized': False}, {'error': 'get'}, {'error': 'write'},
                        {'extraColumns': True}):
            with self.subTest(options=options):
                result = self.run_script([post()], **options)
                self.assertEqual(result['result'], [{'body': '{"ok":false}', 'mime': 'json'}])
                self.assertEqual(result['writes'], [])
                self.assertEqual(result['releases'], 0 if options.get('busy') else 1)

    def test_refuses_existing_formula_and_saturates_safe_integer(self):
        formula = [VERSION, 'formats', 'pdf', {'formulaValue': '=1+1'}]
        result = self.run_script([post()], rows=[HEADER, formula])
        self.assertEqual(result['result'][0]['body'], '{"ok":false}')
        self.assertEqual(result['writes'], [])
        result = self.run_script([post()], rows=[HEADER, [VERSION, 'formats', 'pdf', 2**53 - 6]])
        self.assertEqual(result['result'][0]['body'], '{"ok":true}')
        self.assertEqual(result['rows'][1][-1], 2**53 - 1)
