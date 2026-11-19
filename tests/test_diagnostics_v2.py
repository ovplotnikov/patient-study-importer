"""Diagnostics, second contract: delivery, versions, consent and every new group. Fictional data only."""
from concurrent.futures.process import BrokenProcessPool
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from patient_importer import gui, parallel, telemetry, ui
from patient_importer import telemetry_schema as schema
from patient_importer.diagnostics_ui import EXAMPLE_REPORT
from patient_importer.intake import stage_files
from patient_importer.smoke import SAMPLE
from patient_importer.storage import Workspace
from patient_importer.telemetry import Diagnostics, split_report

URL = 'https://metrics.example.com/v1/counters'
NAME = 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ'
PATH = r'C:\Users\doctor\Пациенты\ТЕСТОВ ТЕСТ ТЕСТОВИЧ\оак.pdf'
MESSAGE = 'Нет доступа к ' + PATH
LAB = ('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nДата выполнения исследования: 02.03.2025\n'
       'Общий анализ крови\nПоказатель  Результат  Единицы  Учебная колонка\nГемоглобин  120  г/л  x\n'
       'Лейкоциты 5 10^9/л\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.diag = Diagnostics(self.folder / 'diagnostics', URL, app_version='2.6.0')
        self.state = self.folder / 'diagnostics' / 'diagnostics.json'

    def enable(self, diag=None):
        self.assertTrue((diag or self.diag).set_consent(True))

    def queue(self):
        return json.loads(self.state.read_text(encoding='utf-8'))['queue']


class DeliveryTests(Base):
    def test_a_large_queue_goes_in_reports_within_the_size_limit(self):
        counters = {'errors': {f'window.ValueError.gui.function_{index}.{index + 1}': 1 for index in range(600)}}
        parts = split_report('2.6.0', counters, limit=4096)
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(len(body) <= 4096 for _, body in parts))
        merged = {}
        for part, body in parts:
            schema.validate_report(json.loads(body))
            merged.update(part['errors'])
        self.assertEqual(merged, counters['errors'])

    def test_versions_go_oldest_first_and_a_refusal_keeps_the_rest(self):
        older = Diagnostics(self.folder / 'diagnostics', URL, app_version='2.5.3')
        self.enable()
        self.assertTrue(older.record('formats', 'pdf', 2))
        self.assertTrue(self.diag.record('formats', 'docx', 1))
        sent = []

        def refuse_second(endpoint, body):
            sent.append(json.loads(body)['app_version'])
            return len(sent) == 1

        with patch('patient_importer.telemetry.post_report', side_effect=refuse_second):
            self.assertFalse(self.diag.send_if_due())
        self.assertEqual(sent, ['2.5.3', '2.6.0'])
        self.assertEqual(self.queue(), {'2.6.0': {'formats': {'docx': 1}}})
        with patch('patient_importer.telemetry.post_report', return_value=True) as post:
            self.assertFalse(self.diag.send_if_due())
            post.assert_not_called()

    def test_an_old_receiver_that_refuses_leaves_the_queue_for_tomorrow(self):
        self.enable()
        self.diag.record('review_outcomes', 'confirmed')
        with patch('patient_importer.telemetry.post_report', return_value=False), \
             patch('patient_importer.telemetry.time.time', return_value=1_900_000_000):
            self.assertFalse(self.diag.send_if_due())
        with patch('patient_importer.telemetry.post_report', return_value=True), \
             patch('patient_importer.telemetry.time.time', return_value=1_900_000_000 + telemetry.SEND_INTERVAL):
            self.assertTrue(self.diag.send_if_due())
        self.assertEqual(self.queue(), {})

    def test_the_queue_is_bounded_and_the_oldest_version_goes_first(self):
        self.enable()
        for minor in range(telemetry.MAX_QUEUED_VERSIONS + 2):
            Diagnostics(self.folder / 'diagnostics', URL, app_version=f'2.{minor}.0').record('formats', 'pdf')
        self.assertEqual(len(self.queue()), telemetry.MAX_QUEUED_VERSIONS)
        self.assertNotIn('2.0.0', self.queue())
        self.assertIn(f'2.{telemetry.MAX_QUEUED_VERSIONS + 1}.0', self.queue())
        big = {'errors': {f'window.ValueError.gui.f{index}.{index + 1}': 1 for index in range(4000)}}
        self.assertFalse(self.diag.record_batch(big, epoch=self.diag.consent_epoch()))
        self.assertLessEqual(self.state.stat().st_size, telemetry.MAX_STATE_BYTES)


class ConsentTests(Base):
    def legacy(self, consent):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({
            'schema_version': 1, 'consent_version': 1, 'consent': consent, 'endpoint': URL,
            'app_version': '2.5.2', 'last_attempt': 0, 'epoch': 4, 'counters': {'formats': {'pdf': 12}} if consent else {},
        }), encoding='utf-8')

    def test_consent_to_the_first_contract_is_asked_again(self):
        for consent in (True, False):
            with self.subTest(consent=consent):
                if self.state.parent.exists():
                    import shutil
                    shutil.rmtree(self.state.parent)
                self.legacy(consent)
                self.assertTrue(self.diag.should_prompt())
                self.assertFalse(self.diag.enabled)
                self.assertIsNone(self.diag.snapshot())
                self.assertEqual(self.queue(), {})
                self.assertFalse(self.diag.record('formats', 'pdf'))

    def test_new_consent_starts_a_new_epoch(self):
        self.legacy(True)
        self.assertTrue(self.diag.should_prompt())
        self.enable()
        self.assertEqual(self.diag.consent_epoch(), 6)
        self.assertEqual(telemetry.CONSENT_VERSION, 2)


class ActivityTests(Base):
    summary = {'documents': 120, 'patients': 12, 'total_review': 3, 'quality_review': 30}

    def test_one_activity_record_per_week_in_ranges(self):
        self.enable()
        with patch('patient_importer.telemetry.date') as clock:
            clock.today.return_value = date(2026, 11, 18)
            clock.fromisocalendar = date.fromisocalendar
            self.assertTrue(self.diag.note_start())
            self.assertTrue(self.diag.note_week(self.summary, scale=2.0))
            self.assertTrue(self.diag.note_week(self.summary, scale=2.0))
            activity = self.diag.snapshot()['counters']['activity']
            self.assertEqual(activity['active'], 1)
            for code in ('age.week0', 'documents.100_499', 'patients.10_49', 'waiting.1_9', 'flagged.25_49', 'scale.150'):
                self.assertEqual(activity[code], 1, code)
            self.assertEqual(sum(code.startswith('os.') for code in activity), 1)
            clock.today.return_value = date(2027, 1, 20)
            self.assertTrue(self.diag.note_week(self.summary))
        activity = self.diag.snapshot()['counters']['activity']
        self.assertEqual((activity['active'], activity['age.1_3m']), (2, 1))

    def test_week_and_ages_are_local_and_never_sent(self):
        self.assertEqual(telemetry.install_age('2026-W47', date(2026, 11, 18)), 'week0')
        self.assertEqual(telemetry.install_age('2026-W40', date(2026, 11, 18)), '1_3m')
        self.assertEqual(telemetry.install_age('2025-W40', date(2026, 11, 18)), '3m_plus')
        self.enable()
        self.diag.note_start()
        self.diag.note_week(self.summary)
        for report in self.diag.snapshot()['counters'].values():
            self.assertFalse(any('W' in code for code in report))

    def test_ranges(self):
        self.assertEqual(schema.scale_bucket(96 / 72), '100')
        self.assertEqual(schema.scale_bucket(96 / 72 * 1.25), '125')
        self.assertEqual(schema.scale_bucket(96 / 72 * 2.4), '200_plus')
        self.assertEqual(schema.memory_bucket(16 * 1024 ** 3), '16_31')
        self.assertEqual(schema.memory_bucket(None), 'unknown')
        self.assertEqual(schema.count_bucket(8, schema.CORES), '5_8')
        self.assertEqual(schema.share_bucket(0, 0), '0')
        self.assertEqual(schema.seconds_bucket(0.2), 'lt1s')
        self.assertEqual(schema.seconds_bucket(400), '3m_plus')


class PathTests(Base):
    def test_actions_are_noted_in_memory_and_written_as_one_group(self):
        self.enable()
        with patch.object(self.diag, '_write', wraps=self.diag._write) as write:
            for code in ('start', 'add_folder', 'import_started', 'import_done', 'closed.patient.2_4'):
                self.assertTrue(self.diag.note('path', code))
            write.assert_not_called()
            self.diag.flush()
            write.assert_called_once()
        self.assertEqual(self.diag.snapshot()['counters']['path']['closed.patient.2_4'], 1)

    def test_actions_after_withdrawal_are_dropped(self):
        self.enable()
        self.assertTrue(self.diag.note('path', 'start'))
        self.assertTrue(self.diag.set_consent(False))
        self.diag.flush()
        self.assertIsNone(self.diag.snapshot())
        self.assertFalse(self.diag.note('path', 'start'))

    def test_a_session_that_did_not_end_is_a_crash_with_its_phase(self):
        self.enable()
        self.assertTrue(self.diag.note_start())
        self.assertTrue(self.diag.note_session('processing'))
        later = Diagnostics(self.folder / 'diagnostics', URL, app_version='2.6.1')
        self.assertTrue(later.note_start())
        counters = later.snapshot()['counters']
        self.assertEqual(counters['errors'], {'session_crash.processing': 1})
        self.assertEqual(counters['path'], {'start': 1, 'update_installed': 1})
        self.assertTrue(later.note_session(None))
        self.assertTrue(later.note_start())
        self.assertEqual(later.snapshot()['counters']['errors'], {'session_crash.processing': 1})


class FailureTests(unittest.TestCase):
    def test_place_comes_from_the_code_and_never_from_the_message(self):
        try:
            schema.bucket(MESSAGE, (1,), ('a', 'b'))
        except TypeError as exc:
            code = schema.failure_code('import', exc)
        self.assertRegex(code, r'^import\.TypeError\.telemetry_schema\.bucket\.[0-9]+$')
        self.assertTrue(schema.valid_code('errors', code))
        self.assertNotIn('Users', code)

    def test_unknown_exception_class_and_frames_outside_the_program(self):
        class PrivateError(Exception):
            pass
        try:
            raise PrivateError(NAME)
        except PrivateError as exc:
            self.assertEqual(schema.failure_code('window', exc), 'window.Other.other.other.1')
        self.assertEqual(schema.failure_code('nonsense', ValueError())[:7], 'window.')

    def test_a_dead_worker_is_counted_once(self):
        class Broken:
            def submit(self, *args):
                raise BrokenProcessPool()

            def shutdown(self, **kwargs):
                pass

        told = []
        with patch.object(parallel, '_start', return_value=Broken()):
            result = list(parallel.ordered_map(str, [(1, 1), (2, 2)], total=2, workers=2, minimum=1,
                                               on_broken=lambda: told.append(1)))
        self.assertEqual(result, [(1, '1'), (2, '2')])
        self.assertEqual(told, [1])

    def test_window_failure_is_recorded_and_explained(self):
        owner = Mock()
        try:
            schema.bucket(None, (1,), ('a', 'b'))
        except TypeError as exc:
            error = exc
        with patch.object(gui.messagebox, 'showerror') as shown:
            gui.Application.window_failure(owner, TypeError, error, error.__traceback__)
        code = owner.diagnostics.note.call_args.args
        self.assertEqual(code[0], 'errors')
        self.assertTrue(code[1].startswith('window.TypeError.telemetry_schema.bucket.'))
        shown.assert_called_once()


class ClientRefusesTests(Base):
    def test_names_paths_and_messages_are_refused_in_every_field(self):
        self.enable()
        attempts = [
            ('path', NAME), ('path', PATH), ('path', 'start.' + NAME), ('activity', 'os.' + PATH),
            ('reading', 'header.label|' + NAME), ('reading', 'outcome.' + NAME + '.saved'),
            ('errors', 'window.ValueError.gui.' + MESSAGE + '.1'), ('errors', 'window.ValueError.gui.Load.1'),
            ('errors', 'window.ValueError.' + PATH + '.load.1'), ('errors', 'window.ValueError.gui.load.0'),
            ('speed', 'scan.' + MESSAGE), (NAME, 'start'), ('accuracy', 'field_edited.' + NAME + '.table'),
        ]
        for metric, code in attempts:
            with self.subTest(metric=metric, code=code):
                self.assertFalse(self.diag.note(metric, code))
                self.assertFalse(self.diag.record(metric, code))
        self.diag.flush()
        self.assertIsNone(self.diag.snapshot())
        self.assertNotIn(NAME, self.state.read_text(encoding='utf-8'))

    def test_the_example_in_the_window_is_a_valid_report(self):
        self.assertEqual(schema.validate_report(EXAMPLE_REPORT)['counters'], EXAMPLE_REPORT['counters'])


class WorkspaceGroupsTests(Base):
    def setUp(self):
        super().setUp()
        self.enable()
        self.workspace = Workspace(self.folder / 'рабочая', diagnostics=self.diag)

    def add(self, name, text, extracted=None):
        (self.workspace.root / 'Входящие' / name).write_text(text, encoding='utf-8')
        if extracted is None:
            return self.workspace.import_folder()
        with patch('patient_importer.extraction.extract', return_value=extracted):
            return self.workspace.import_folder()

    def counters(self):
        self.diag.flush()
        return self.diag.snapshot()['counters']

    def test_reading_of_a_lab_form(self):
        self.add('оак.txt', LAB)
        reading = self.counters()['reading']
        self.assertEqual(reading['outcome.cbc.saved_flagged'], 1)
        self.assertEqual(reading['flag.cbc.unparsed_row'], 1)
        self.assertEqual(reading['lab_rows.1_4'], 1)
        self.assertEqual(reading['unparsed.50_plus'], 1)
        self.assertEqual(reading['header.label|result|unit|unknown'], 1)
        self.assertEqual(reading['patient.document'], 1)
        self.assertNotIn('Учебная', json.dumps(reading, ensure_ascii=False))

    def test_reading_of_a_scan_and_speed(self):
        extracted = {'pages': [SAMPLE], 'native_pages': [SAMPLE], 'ocr_pages': [1], 'format': 'PDF',
                     'extraction_issues': [{'code': 'OCR_DISAGREEMENT', 'page': 1}]}
        self.add('скан.pdf', '%PDF-учебный', extracted)
        counters = self.counters()
        self.assertEqual(counters['reading']['scan_pages.1'], 1)
        self.assertEqual(counters['reading']['scan_reading.disagree'], 1)
        self.assertEqual(sum(count for code, count in counters['speed'].items() if code.startswith('scan.')), 1)
        self.assertEqual(sum(count for code, count in counters['speed'].items() if code.startswith('export.')), 1)

    def test_patient_sources_and_a_person_s_answer(self):
        folder = self.folder / 'архив' / 'Папка'
        folder.mkdir(parents=True)
        (folder / 'a.txt').write_text('Дата выполнения исследования: 05.03.2025\nЗаключение: учебное.\n', encoding='utf-8')
        stage_files(self.workspace, [folder])
        self.workspace.import_folder()
        self.assertEqual(self.counters()['reading']['patient.missing_identity'], 1)
        document = self.workspace.review_list()[0]['id']
        self.workspace.assign(document, NAME, '2000-01-01')
        self.assertEqual(self.counters()['reading']['patient.person'], 1)

    def test_accuracy_of_a_confirmation(self):
        self.add('оак.txt', LAB.replace('  Учебная колонка', '').replace('120  г/л  x', '120'))
        record = self.workspace.quality_review_list()[0]
        rows = record['observations']
        rows[0]['unit'] = 'г/л'
        unread = 'Лейкоциты 5 10^9/л'
        rows.append({'label': 'Лейкоциты', 'result': '5', 'unit': '10^9/л', 'raw': unread, 'page': 1})
        self.workspace.confirm_quality(record['id'], observations=rows, note='Учебная сверка.',
                                       expected_revision=record['revision'],
                                       resolved_lines=[(1, 1, 7)])
        accuracy = self.counters()['accuracy']
        self.assertEqual(accuracy['rows_edited.table'], 1)
        self.assertEqual(accuracy['field_edited.unit.table'], 1)
        self.assertEqual(accuracy['layout.accepted'], 1)
        self.assertNotIn('Лейкоциты', json.dumps(accuracy, ensure_ascii=False))

    def test_patient_change_after_an_automatic_assignment(self):
        self.add('оак.txt', LAB)
        document = self.workspace.document_list()[0]['id']
        self.workspace.change_patient(document, 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
        self.assertEqual(self.counters()['accuracy']['patient_changed.document'], 1)

    def test_excel_open_is_its_own_failure(self):
        self.add('оак.txt', LAB)
        (self.workspace.root / '~$Исследования.xlsx').write_text('lock', encoding='utf-8')
        with self.assertRaises(RuntimeError), self.workspace.connect() as db:
            self.workspace._export_workbook(db)
        self.assertEqual(self.counters()['errors']['export_locked'], 1)


class WindowActionsTests(Base):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f'Tk display unavailable: {exc}') from exc
        cls.root.withdraw()
        ui.setup(cls.root)
        cls.addClassCleanup(cls.root.destroy)

    def setUp(self):
        super().setUp()
        self.enable()
        self.workspace = Workspace(self.folder / 'рабочая', diagnostics=self.diag)
        self.addCleanup(self.finish_widgets)

    def finish_widgets(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.root.update()

    def open(self, folder_name):
        folder = self.folder / 'архив' / folder_name
        folder.mkdir(parents=True)
        for index in range(2):
            (folder / f'{index}.txt').write_text(f'Дата выполнения исследования: 05.03.2025\nЗаключение: {folder_name} {index}.\n',
                                                 encoding='utf-8')
        stage_files(self.workspace, [folder])
        self.workspace.import_folder()
        window = gui.ReviewWindow(self.root, self.workspace, Mock(), ui.Icons(self.root), actions=ui.WorkspaceActions(self.root))
        self.root.update()
        return window

    def settle(self, window):
        deadline = time.monotonic() + 15
        while window.saving:
            self.root.update()
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        self.root.update()

    def path(self):
        self.diag.flush()
        return self.diag.snapshot()['counters']['path']

    def test_draft_kept_or_edited_and_typed(self):
        window = self.open('Тестов Тест Тестович 01.01.2000')
        window.confirm();self.settle(window)
        self.assertEqual(self.path()['patient_draft_kept'], 1)

    def test_draft_edited(self):
        window = self.open('Тестов Тест Тестович 01.01.2000')
        window.birth.set('02.01.2000')
        window.confirm();self.settle(window)
        self.assertEqual(self.path()['patient_draft_edited'], 1)

    def test_typed_and_closed_with_rows_left(self):
        window = self.open('Папка')
        window.name.set(NAME);window.birth.set('01.01.2000')
        window.confirm();self.settle(window)
        self.assertEqual(self.path()['patient_typed'], 1)
        window = self.open('Другая папка')
        with patch.object(gui, 'open_path'), patch.object(gui, 'reveal_path'):
            window.original_button.invoke();window.reveal_button.invoke()
        window.close()
        path = self.path()
        self.assertEqual((path['file_opened'], path['file_revealed'], path['closed.patient.2_4']), (1, 1, 1))


if __name__ == '__main__':
    unittest.main()


class DocumentationTests(unittest.TestCase):
    def test_every_code_of_the_contract_is_listed_for_the_doctor(self):
        text = (Path(__file__).resolve().parents[1] / 'docs' / 'TELEMETRY.md').read_text(encoding='utf-8')
        for metric, shapes in schema.METRICS.items():
            self.assertIn(f'`{metric}`', text)
            for shape in shapes:
                for part in shape:
                    for value in ([] if isinstance(part, dict) else part):
                        # A prefix such as «age» is shown with its ranges: `age.<возраст>`.
                        self.assertTrue(f'`{value}`' in text or f'`{value}.' in text, (metric, value))
        for value in (*schema.REFUSALS, schema.FUNCTION, schema.LINE):
            self.assertIn(f'`{value}`', text)
