"""Exercise the Apps Script receiver in V8 with synthetic Sheets API responses."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from patient_importer.telemetry_schema import (
    MAX_CODES_PER_WEEK, MAX_COUNT, MAX_REPORT_BYTES, MAX_VERSIONS_PER_WEEK, receive_report, wire_schema,
)

ROOT = Path(__file__).resolve().parents[1] / 'google_apps_script'
NODE = shutil.which('node')
VERSION = '2.4.0'
MARKER = 'SYNTHETIC_PRIVATE_MARKER'
ID = 'SyntheticSheetId_a1b2c3d4e5f6'
NOW = '2026-11-18T12:00:00Z'
WEEK = '2026-W47'
COUNTERS = ['week', 'app_version', 'metric', 'code', 'total']
RECEPTION = ['week', 'outcome', 'reason', 'total']
LEGACY_HEADER = ['app_version', 'metric', 'code', 'total']


HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let properties = Object.assign({}, input.properties || {});
if (input.initialized) properties.SPREADSHEET_ID = input.id;
let held = false;
let nextSheet = 10;
const sheets = new Map();
for (const [title, sheet] of Object.entries(input.sheets || {})) sheets.set(title, sheet);
let created = [];
let writes = 0;
let reads = 0;
let locks = 0;
let releases = 0;
let logs = [];

function cellsOf(rows) {
  return rows.map(row => ({values: row.map(value => {
    if (value === null) return {};
    if (typeof value === 'object') return {userEnteredValue: value};
    return {userEnteredValue: typeof value === 'string' ? {stringValue: value} : {numberValue: value}};
  })}));
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
    getProperty: name => (Object.prototype.hasOwnProperty.call(properties, name) ? properties[name] : null),
    setProperty: (name, value) => {
      if (name === 'SPREADSHEET_ID') assert.equal(held, true);
      properties[name] = value;
    },
    deleteProperty: name => {
      assert.equal(held, true);
      delete properties[name];
    },
  })},
  Sheets: {Spreadsheets: {
    create: (request, options) => {
      assert.equal(held, true);
      assert.equal(options.fields, 'spreadsheetId');
      created.push(request);
      for (const sheet of request.sheets) {
        sheets.set(sheet.properties.title, {sheetId: nextSheet++, grid: sheet.properties.gridProperties, rows: []});
      }
      return {spreadsheetId: input.id};
    },
    get: (spreadsheetId, options) => {
      assert.equal(held, true);
      assert.equal(spreadsheetId, input.id);
      if (input.error === 'get') throw Error(input.marker);
      reads++;
      if (options.ranges) {
        assert.equal(options.ranges.length, 1);
        const title = /^'(.*)'!A1:[A-Z][0-9]+$/.exec(options.ranges[0])[1];
        const sheet = sheets.get(title);
        assert.ok(sheet);
        return {sheets: [{data: [{rowData: cellsOf(sheet.rows)}]}]};
      }
      return {sheets: Array.from(sheets.entries()).map(([title, sheet]) => ({properties: {
        sheetId: sheet.sheetId, title, gridProperties: {rowCount: sheet.grid.rowCount, columnCount: sheet.grid.columnCount},
      }}))};
    },
    batchUpdate: (request, spreadsheetId) => {
      assert.equal(held, true);
      assert.equal(spreadsheetId, input.id);
      if (input.error === 'write') throw Error(input.marker);
      assert.equal(request.requests.length, 1);
      const change = request.requests[0];
      if (change.addSheet) {
        const properties = Object.assign({sheetId: nextSheet++}, change.addSheet.properties);
        sheets.set(properties.title, {sheetId: properties.sheetId, grid: properties.gridProperties, rows: []});
        return {replies: [{addSheet: {properties}}]};
      }
      const operation = change.updateCells;
      assert.ok(['userEnteredValue', 'userEnteredValue,userEnteredFormat'].includes(operation.fields));
      assert.equal(operation.start.rowIndex, 0);
      assert.equal(operation.start.columnIndex, 0);
      const target = Array.from(sheets.values()).find(sheet => sheet.sheetId === operation.start.sheetId);
      assert.ok(target);
      assert.ok(operation.rows.length <= target.grid.rowCount);
      writes++;
      target.rows = operation.rows.map(row => row.values.map(cell =>
        cell.userEnteredValue.stringValue ?? cell.userEnteredValue.numberValue));
      return {};
    },
  }},
});
vm.runInContext(input.code, context, {filename: 'Code.gs'});
let clock = input.now;
context.now_ = () => new Date(clock);
const result = input.steps.map(step => {
  if (step.now) clock = step.now;
  if (step.action === 'init') return vm.runInContext('initializeSpreadsheet()', context);
  if (step.action === 'receive') {
    context.value = step.value;
    return vm.runInContext('receiveReport_(value)', context);
  }
  if (step.action === 'week') {
    context.moment = step.moment;
    return vm.runInContext('weekOf_(new Date(moment))', context);
  }
  context.event = step.event;
  return vm.runInContext(step.action === 'get' ? 'doGet(event)' : 'doPost(event)', context);
});
process.stdout.write(JSON.stringify({
  schema: vm.runInContext('WIRE_SCHEMA', context),
  sheets: Object.fromEntries(Array.from(sheets.entries()).map(([title, sheet]) => [title, sheet.rows])),
  created, writes, reads, locks, releases, properties, logs, result,
}));
"""


def report(count=10, version=VERSION, schema=1, counters=None):
    return {'schema_version': schema, 'app_version': version,
            'counters': counters or {'formats': {'pdf': count}, 'outcomes': {'saved': count}}}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def post(body=None, *, path='', query=None, mime='application/json', now=None):
    body = canonical(report()) if body is None else body
    length = len(body.encode('utf8'))
    step = {'action': 'post', 'event': {
        'pathInfo': path, 'queryString': query, 'contentLength': length,
        'postData': {'type': mime, 'length': length, 'contents': body},
    }}
    if now:
        step['now'] = now
    return step


OK = {'body': '{"ok":true}', 'mime': 'json'}
REFUSED = {'body': '{"ok":false}', 'mime': 'json'}


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
                          'ScriptApp.getOAuthToken', 'getRemoteAddress', 'headers'):
            self.assertNotIn(forbidden, code)


@unittest.skipUnless(NODE, 'Node.js required for the Apps Script V8 tests')
class AppsScriptTests(unittest.TestCase):
    def run_script(self, steps, **options):
        data = {
            'code': (ROOT / 'Code.gs').read_text(encoding='utf8'),
            'steps': steps, 'id': ID, 'marker': MARKER, 'initialized': True, 'now': NOW,
            'sheets': {'Приём': {'sheetId': 1, 'grid': {'rowCount': 2000, 'columnCount': 4}, 'rows': [RECEPTION]}},
        }
        data.update(options)
        result = subprocess.run([NODE, '-e', HARNESS], input=json.dumps(data), encoding='utf-8',
                                capture_output=True, timeout=20, check=True)
        self.assertEqual(result.stderr, '')
        self.assertNotIn(MARKER, result.stdout)
        state = json.loads(result.stdout)
        self.assertEqual(state['logs'], [])
        return state

    @staticmethod
    def counters(state, week=WEEK):
        return [row[1:] for row in state['sheets'][week][1:]]

    @staticmethod
    def reception(state):
        return {(row[0], row[1], row[2]): row[3] for row in state['sheets']['Приём'][1:]}

    def test_wire_schema_matches_python(self):
        self.assertEqual(self.run_script([])['schema'], json.loads(json.dumps(wire_schema())))

    def test_owner_setup_is_idempotent_and_keeps_the_first_sheet(self):
        result = self.run_script([{'action': 'init'}, {'action': 'init'}], initialized=False, sheets={})
        self.assertEqual(result['result'], [True, True])
        self.assertEqual(len(result['created']), 1)
        self.assertEqual(result['properties']['SPREADSHEET_ID'], ID)
        self.assertEqual(result['sheets'], {'Приём': [RECEPTION]})
        legacy = {'Счётчики': {'sheetId': 0, 'grid': {'rowCount': 1024, 'columnCount': 4},
                               'rows': [LEGACY_HEADER, [VERSION, 'formats', 'pdf', 12]]}}
        result = self.run_script([{'action': 'init'}], sheets=legacy)
        self.assertEqual(result['sheets'], {'Счётчики': [LEGACY_HEADER, [VERSION, 'formats', 'pdf', 12]],
                                            'Приём': [RECEPTION]})

    def test_schema_one_of_any_version_is_accepted_without_a_threshold(self):
        result = self.run_script([post(), post(canonical(report(1, '2.5.2'))), post(canonical(report(3, '3.1.0')))])
        self.assertEqual(result['result'], [OK] * 3)
        self.assertEqual(result['sheets'][WEEK][0], COUNTERS)
        self.assertEqual(self.counters(result), [
            ['2.4.0', 'formats', 'pdf', 10], ['2.4.0', 'outcomes', 'saved', 10],
            ['2.5.2', 'formats', 'pdf', 1], ['2.5.2', 'outcomes', 'saved', 1],
            ['3.1.0', 'formats', 'pdf', 3], ['3.1.0', 'outcomes', 'saved', 3],
        ])
        self.assertEqual(self.reception(result), {(WEEK, 'accepted', 'schema_1'): 3})

    def test_schema_two_and_unknown_codes_keep_no_text(self):
        path = 'C:/Users/' + MARKER + '/file.pdf'
        body = canonical(report(schema=2, version='2.6.0', counters={
            'formats': {'pdf': 3, MARKER: 2}, 'errors': {'window.' + path: 1, 'worker_crash': 1},
            'activity': {'active': 1, 'os.win11': 1}, 'secret_' + MARKER.lower(): {'x': 4}}))
        result = self.run_script([post(body)])
        self.assertEqual(result['result'], [OK])
        self.assertEqual(self.counters(result), [
            ['2.6.0', '_unknown', '_unknown', 4], ['2.6.0', 'activity', 'active', 1],
            ['2.6.0', 'activity', 'os.win11', 1], ['2.6.0', 'errors', '_unknown', 1],
            ['2.6.0', 'errors', 'worker_crash', 1], ['2.6.0', 'formats', '_unknown', 2],
            ['2.6.0', 'formats', 'pdf', 3],
        ])
        self.assertNotIn(MARKER.lower(), json.dumps(result['sheets']))
        self.assertEqual(self.reception(result), {(WEEK, 'accepted', 'schema_2'): 1})

    def test_name_path_or_exception_text_never_reach_storage(self):
        attempts = [
            canonical(report(schema=2, counters={'errors': {'window.ValueError.gui.load.1': 1}}))
            .replace('load', 'Тестов'),
            canonical(report(schema=2, version='C:/Users/x/1.pdf')),
            canonical(report(schema=2, counters={'errors': {'Нет файла C:\\Тестов.pdf': 1}})),
            canonical({**report(schema=2), 'message': 'ValueError: ' + MARKER}),
        ]
        result = self.run_script([post(body) for body in attempts])
        self.assertEqual(result['result'], [REFUSED, REFUSED, REFUSED, REFUSED])
        self.assertNotIn(WEEK, result['sheets'])
        self.assertEqual(self.reception(result), {(WEEK, 'refused', 'format'): 4})
        self.assertNotIn('Тестов', json.dumps(result['sheets'], ensure_ascii=False))

    def test_refusals_are_counted_by_reason_and_week(self):
        good = canonical(report())
        wrong_length = post()
        wrong_length['event']['postData']['length'] -= 1
        large = post('{' + ' ' * (MAX_REPORT_BYTES + 1) + '}')
        cases = [
            post(canonical({**report(), 'filename': MARKER})),
            post(canonical(report(True))), post(canonical(report(MAX_COUNT + 1))), post(canonical(report(10.5))),
            post(good.replace('"pdf":10', '"pdf":10,"pdf":10')), post(good.replace('"pdf":10', '"pdf":1e1')),
            post(json.dumps(report(), indent=2)), post(MARKER * 10), post('☠'),
            post(query=MARKER), post(path='v1/counters'), post(path=0), wrong_length,
            post(canonical(report(schema=3))),
        ]
        result = self.run_script(cases + [large, post(mime='multipart/form-data; boundary=' + MARKER),
                                          post(now='2026-11-24T00:00:00Z')])
        self.assertEqual(result['result'], [REFUSED] * (len(cases) + 2) + [OK])
        self.assertEqual(self.reception(result), {
            (WEEK, 'refused', 'format'): len(cases), (WEEK, 'refused', 'too_large'): 1,
            (WEEK, 'refused', 'content_type'): 1, ('2026-W48', 'accepted', 'schema_1'): 1,
        })
        self.assertEqual(list(result['sheets']), ['Приём', '2026-W48'])

    def test_busy_lock_is_counted_later(self):
        busy = self.run_script([post()], busy=True)
        self.assertEqual(busy['result'], [REFUSED])
        self.assertEqual(busy['writes'], 0)
        self.assertEqual(json.loads(busy['properties']['PENDING_REFUSALS']), {WEEK + '|busy': 1})
        result = self.run_script([post()], properties=busy['properties'])
        self.assertEqual(self.reception(result), {(WEEK, 'accepted', 'schema_1'): 1, (WEEK, 'refused', 'busy'): 1})
        self.assertNotIn('PENDING_REFUSALS', result['properties'])

    def test_each_week_has_its_own_sheet(self):
        result = self.run_script([post(), post(now='2026-12-31T23:00:00Z'), post(now='2027-01-04T01:00:00Z')])
        self.assertEqual(list(result['sheets']), ['Приём', WEEK, '2026-W53', '2027-W01'])
        self.assertEqual(self.counters(result, '2027-W01'), [[VERSION, 'formats', 'pdf', 10],
                                                             [VERSION, 'outcomes', 'saved', 10]])

    def test_iso_weeks_match_python(self):
        from datetime import datetime
        from patient_importer.telemetry_schema import week_of
        moments = ['2026-01-01T00:00:00Z', '2026-12-28T12:00:00Z', '2027-01-03T23:59:59Z', '2024-12-30T00:00:00Z',
                   '2020-12-31T12:00:00Z', '2021-01-04T00:00:00Z']
        result = self.run_script([{'action': 'week', 'moment': moment} for moment in moments])
        self.assertEqual(result['result'], [week_of(datetime.fromisoformat(moment.replace('Z', '+00:00')))
                                            for moment in moments])

    def test_versions_and_places_per_week_are_bounded(self):
        steps = [post(canonical(report(1, f'2.{minor}.0'))) for minor in range(MAX_VERSIONS_PER_WEEK + 2)]
        places = {f'window.ValueError.gui.function_{index}.1': 1 for index in range(MAX_CODES_PER_WEEK['errors'] + 5)}
        steps.append(post(canonical(report(schema=2, version='2.0.0', counters={'errors': places}))))
        result = self.run_script(steps)
        rows = self.counters(result)
        self.assertEqual(len({row[0] for row in rows}), MAX_VERSIONS_PER_WEEK + 1)
        self.assertIn('other', {row[0] for row in rows})
        errors = [row[2] for row in rows if row[1] == 'errors']
        self.assertEqual(len(errors), MAX_CODES_PER_WEEK['errors'] + 1)
        self.assertIn('_other', errors)

    def test_same_rules_as_the_python_receiver(self):
        values = [
            report(), report(schema=2, counters={'path': {'start': 2, 'closed.patient.2_4': 1}}),
            report(schema=2, counters={'reading': {'header.label|result|unit|unknown': 1, 'header.' + MARKER: 1}}),
            report(schema=2, counters={'errors': {'import.OSError.storage.add_file.288': 1}, 'Bad Name': {'x': 1}}),
            report(schema=2, counters={'formats': {}}), report(schema=2, counters={}),
            report(schema=2, counters={'formats': {'pdf': 0}}), report(schema=2, version='02.1.0'),
            {**report(), 'extra': 1}, [], 'text', report(schema=2, counters={'speed': {'scan.3_10s': MAX_COUNT}}),
        ]
        result = self.run_script([{'action': 'receive', 'value': value} for value in values])
        expected = []
        for value in values:
            accepted, reason = receive_report(value)
            expected.append({'report': accepted} if accepted else {'reason': reason})
        self.assertEqual(result['result'], expected)

    def test_health_does_not_read_sheets(self):
        result = self.run_script([
            {'action': 'get', 'event': {'queryString': None}},
            {'action': 'get', 'event': {'pathInfo': '', 'queryString': None}},
            {'action': 'get', 'event': {'pathInfo': 'healthz', 'queryString': None}},
            {'action': 'get', 'event': {'pathInfo': '', 'queryString': 'bad=1'}},
        ])
        self.assertEqual(result['result'], [OK, OK, REFUSED, REFUSED])
        self.assertEqual((result['reads'], result['locks']), (0, 0))

    def test_storage_failures_are_generic(self):
        for options in ({'initialized': False}, {'error': 'get'}, {'error': 'write'}):
            with self.subTest(options=options):
                result = self.run_script([post()], **options)
                self.assertEqual(result['result'], [REFUSED])
                self.assertEqual(result['releases'], 1)
                self.assertEqual(json.loads(result['properties']['PENDING_REFUSALS']), {WEEK + '|storage': 1})

    def test_refuses_existing_formula_and_saturates_safe_integer(self):
        sheets = {'Приём': {'sheetId': 1, 'grid': {'rowCount': 2000, 'columnCount': 4}, 'rows': [RECEPTION]},
                  WEEK: {'sheetId': 2, 'grid': {'rowCount': 6000, 'columnCount': 5},
                         'rows': [COUNTERS, [WEEK, VERSION, 'formats', 'pdf', {'formulaValue': '=1+1'}]]}}
        result = self.run_script([post()], sheets=sheets)
        self.assertEqual(result['result'], [REFUSED])
        sheets[WEEK]['rows'][1][-1] = 2**53 - 6
        result = self.run_script([post()], sheets=sheets)
        self.assertEqual(result['result'], [OK])
        self.assertEqual(self.counters(result)[0][-1], 2**53 - 1)
