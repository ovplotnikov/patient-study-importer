"""Recalculating documents already in the registry. All data is fictional."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from patient_importer import analysis, gui
from patient_importer.storage import ReviewCommittedError, Workspace

FORM = ('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\n'
        'Дата выполнения исследования: 02.03.2025\nОбщий анализ крови\nПоказатель  Результат  Единицы\n'
        'Гемоглобин  120  г/л\nЭритроциты  4,5  г/л\n')
OTHER = FORM.replace('02.03.2025', '03.03.2025')
LAB = 'Лабораторный бланк'


class ReparseTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.workspace = Workspace(self.folder / 'case')

    def add(self, name='a.txt', text=FORM):
        (self.workspace.root / 'Входящие' / name).write_text(text, encoding='utf-8')
        self.workspace.import_folder()

    def documents(self):
        with self.workspace.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM documents ORDER BY filename')]

    def analysis_of(self, index=0):
        return json.loads(self.documents()[index]['analysis'])

    def age(self, index=0, **changes):
        """Rewrite a stored document the way an earlier version left it."""
        document = self.documents()[index]
        parsed = json.loads(document['analysis'])
        parsed.update(version='2.3.0', **changes)
        with self.workspace.connect() as db, db:
            db.execute('UPDATE documents SET analysis=? WHERE id=?', (json.dumps(parsed, ensure_ascii=False), document['id']))
        return document

    def broken(self, index=0):
        return self.age(index, observations=[])

    def history(self):
        with self.workspace.connect() as db:
            return [row[0] for row in db.execute('SELECT action FROM study_history')]

    def test_lost_rows_come_back(self):
        self.add()
        self.broken()
        self.assertEqual(self.workspace.summary()['reparse'], 1)
        report = self.workspace.reparse_unconfirmed()
        self.assertEqual((report['updated'], report['rows_before'], report['rows_after']), (1, 0, 2))
        parsed = self.analysis_of()
        self.assertEqual([row['label'] for row in parsed['observations']], ['Гемоглобин', 'Эритроциты'])
        self.assertEqual(parsed['version'], analysis.PARSER_VERSION)
        self.assertEqual(self.workspace.summary()['reparse'], 0)

    def test_workbook_gets_the_rows(self):
        self.add()
        self.broken()
        self.workspace.reparse_unconfirmed()
        book = load_workbook(self.workspace.root / 'Исследования.xlsx', read_only=True)
        values = {cell for row in book['Показатели'].iter_rows(values_only=True) for cell in row}
        self.assertIn('Гемоглобин', values)

    def test_previous_reading_goes_to_history(self):
        self.add()
        self.broken()
        self.workspace.reparse_unconfirmed()
        self.assertEqual(self.history(), ['reparsed'])

    def test_original_and_stored_text_do_not_change(self):
        self.add()
        document = self.broken()
        self.workspace.reparse_unconfirmed()
        after = self.documents()[0]
        self.assertEqual((after['extracted'], after['hash'], after['fingerprint']),
                         (document['extracted'], document['hash'], document['fingerprint']))

    def test_ocr_is_not_run_again(self):
        self.add()
        self.broken()
        with patch('patient_importer.extraction.extract') as extract:
            self.workspace.reparse_unconfirmed()
        extract.assert_not_called()

    def test_second_run_changes_nothing(self):
        self.add()
        self.broken()
        self.workspace.reparse_unconfirmed()
        report = self.workspace.reparse_unconfirmed()
        self.assertEqual(report['updated'], 0)
        self.assertEqual(self.history(), ['reparsed'])

    def test_confirmed_document_is_not_touched(self):
        self.add(text=FORM.replace('120  г/л', '120'))
        record = self.workspace.quality_review_list()[0]
        self.workspace.confirm_quality(record['id'], note='Учебная сверка')
        document = self.age(observations=[])
        self.assertEqual(self.workspace.summary()['reparse'], 0)
        report = self.workspace.reparse_unconfirmed()
        self.assertEqual((report['updated'], report['reviewed']), (0, 1))
        self.assertEqual(self.documents()[0]['analysis'], json.dumps(json.loads(self.documents()[0]['analysis']), ensure_ascii=False))
        self.assertEqual(json.loads(self.documents()[0]['analysis'])['observations'], [])
        self.assertEqual(self.documents()[0]['id'], document['id'])

    def test_duplicate_is_not_touched(self):
        self.add()
        self.age(duplicate_of='S-учебный')
        self.assertEqual(self.workspace.summary()['reparse'], 0)
        self.assertEqual(self.workspace.reparse_unconfirmed()['updated'], 0)

    def test_fewer_table_rows_keep_the_old_reading_and_stop_asking(self):
        self.add()
        rows = self.analysis_of()['observations']
        extra = dict(rows[0], label='Учебная прежняя строка', result_id='R-old')
        self.age(observations=[*rows, extra])
        report = self.workspace.reparse_unconfirmed()
        self.assertEqual((report['updated'], report['kept']), (0, 1))
        self.assertEqual(len(self.analysis_of()['observations']), 3)
        self.assertEqual(self.workspace.summary()['reparse'], 0)
        self.assertEqual(self.history(), [])

    def test_unreadable_stored_text_keeps_the_old_reading(self):
        self.add()
        document = self.broken()
        with self.workspace.connect() as db, db:
            db.execute('UPDATE documents SET extracted=? WHERE id=?', ('{"учебный": 1}', document['id']))
        report = self.workspace.reparse_unconfirmed()
        self.assertEqual((report['updated'], report['kept']), (0, 1))

    def test_one_bad_document_does_not_stop_the_others(self):
        self.add('a.txt')
        self.add('b.txt', OTHER)
        first = self.broken(0)
        self.broken(1)
        with self.workspace.connect() as db, db:
            db.execute('UPDATE documents SET extracted=? WHERE id=?', ('не json', first['id']))
        report = self.workspace.reparse_unconfirmed()
        self.assertEqual((report['updated'], report['kept']), (1, 1))

    def test_backup_is_made_first(self):
        self.add()
        self.broken()
        order = []
        with patch.object(self.workspace, 'backup', side_effect=lambda db: order.append('backup')), \
                patch.object(self.workspace, '_save_study_history', side_effect=lambda *a, **k: order.append('write')):
            self.workspace.reparse_unconfirmed()
        self.assertEqual(order[:2], ['backup', 'write'])

    def test_open_workbook_stops_before_any_change(self):
        self.add()
        document = self.broken()
        (self.workspace.root / '~$Исследования.xlsx').write_text('lock')
        with self.assertRaises(RuntimeError):
            self.workspace.reparse_unconfirmed()
        self.assertEqual(self.documents()[0]['analysis'], self.documents()[0]['analysis'])
        self.assertEqual(json.loads(self.documents()[0]['analysis'])['version'], '2.3.0')
        self.assertEqual(self.documents()[0]['id'], document['id'])

    def test_failure_during_the_run_leaves_every_document_as_it_was(self):
        self.add('a.txt')
        self.add('b.txt', OTHER)
        self.broken(0)
        self.broken(1)
        with patch.object(self.workspace, 'resolve_patients', side_effect=OSError('Учебный сбой')):
            with self.assertRaises(OSError):
                self.workspace.reparse_unconfirmed()
        self.assertEqual({json.loads(item['analysis'])['version'] for item in self.documents()}, {'2.3.0'})
        self.assertEqual(self.history(), [])

    def test_workbook_rebuilt_once(self):
        self.add('a.txt')
        self.add('b.txt', OTHER)
        self.broken(0)
        self.broken(1)
        with patch('patient_importer.workbook.export_workbook') as export:
            self.workspace.reparse_unconfirmed()
        export.assert_called_once()

    def test_patient_chosen_by_hand_stays(self):
        text = FORM.replace('Дата рождения: 01.01.2000\n', '')
        self.add('a.txt', text)
        document = self.documents()[0]
        self.workspace.assign(document['id'], 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ', '2001-02-03')
        patient = self.documents()[0]['patient_id']
        self.broken()
        self.workspace.reparse_unconfirmed()
        self.assertEqual(self.documents()[0]['patient_id'], patient)

    def test_document_without_a_patient_gets_one_from_the_better_reading(self):
        text = FORM.replace('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000',
                            'ФИО пациента: ТЕСТОВ ТЕСТ ТЕСТОВИЧ (01.01.2000)')
        self.add('a.txt', text)
        self.age(names=[], births=[])
        with self.workspace.connect() as db, db:
            db.execute('UPDATE documents SET patient_id=NULL')
        self.workspace.reparse_unconfirmed()
        self.assertTrue(self.documents()[0]['patient_id'])

    def test_changed_identity_goes_back_to_a_person(self):
        self.add()
        self.broken()
        changed = json.loads(self.documents()[0]['extracted'])
        changed['pages'][0] = changed['pages'][0].replace('ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'ПРИМЕРОВ ПРИМЕР ПРИМЕРОВИЧ')
        with self.workspace.connect() as db, db:
            db.execute('UPDATE documents SET extracted=?', (json.dumps(changed, ensure_ascii=False),))
        self.workspace.reparse_unconfirmed()
        self.assertIsNone(self.documents()[0]['patient_id'])

    def test_row_identifiers_survive_so_notes_stay_attached(self):
        self.add()
        with self.workspace.connect() as db:
            before = self.workspace._with_result_ids(self.documents()[0]['id'], self.analysis_of())
        ids = {row['label']: row['result_id'] for row in before['observations']}
        self.age(observations=before['observations'])
        self.workspace.reparse_unconfirmed()
        self.assertEqual({row['label']: row['result_id'] for row in self.analysis_of()['observations']}, ids)

    def test_possible_duplicate_question_survives(self):
        self.add()
        document = self.documents()[0]
        parsed = json.loads(document['analysis'])
        parsed['quality']['issues'].append({'id': 'possible_duplicate', 'code': 'possible_duplicate', 'message': 'м',
                                            'severity': 'review', 'related_ids': ['S-учебный']})
        parsed['version'] = '2.3.0'
        with self.workspace.connect() as db, db:
            db.execute('UPDATE documents SET analysis=?', (json.dumps(parsed, ensure_ascii=False),))
        self.workspace.reparse_unconfirmed()
        self.assertIn('possible_duplicate', {issue['code'] for issue in self.analysis_of()['quality']['issues']})

    def test_new_import_is_not_offered(self):
        self.add()
        self.assertEqual(self.workspace.summary()['reparse'], 0)


class MainWindowTests(unittest.TestCase):
    def app(self, count=3):
        workspace = Mock()
        workspace.summary.return_value = {'reparse': count}
        actions = Mock()
        actions.run.return_value = True
        return SimpleNamespace(workspace=workspace, workspace_actions=actions, workspace_busy=lambda: False,
                               workspace_window_open=lambda: False, root=Mock(), current=Mock(), screen='result',
                               refresh=Mock(), show_documents=Mock(), rendered_queue=None)

    def test_nothing_starts_without_consent(self):
        app = self.app()
        with patch.object(gui.messagebox, 'askyesno', return_value=False):
            gui.Application.reparse(app)
        app.workspace_actions.run.assert_not_called()

    def test_consent_starts_the_run_in_the_background(self):
        app = self.app()
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            gui.Application.reparse(app)
        self.assertIs(app.workspace_actions.run.call_args.args[0], app.workspace.reparse_unconfirmed)

    def test_nothing_to_do_asks_nothing(self):
        app = self.app(0)
        with patch.object(gui.messagebox, 'askyesno') as ask:
            gui.Application.reparse(app)
        ask.assert_not_called()

    def run_to_end(self, report, error):
        app = self.app()
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            gui.Application.reparse(app)
        completed = app.workspace_actions.run.call_args.args[1]
        with patch.object(gui.messagebox, 'showinfo') as info, patch.object(gui.messagebox, 'showwarning') as warning, \
                patch.object(gui.messagebox, 'showerror') as failure:
            completed(report, error)
        return app, info, warning, failure

    def test_report_says_what_happened(self):
        report = {'updated': 5, 'kept': 1, 'reviewed': 2, 'rows_before': 0, 'rows_after': 40}
        app, info, _warning, _failure = self.run_to_end(report, None)
        text = info.call_args.args[1]
        self.assertIn('Обновлено документов: 5', text)
        self.assertIn('было 0, стало 40', text)
        app.refresh.assert_called_once_with()

    def test_locked_workbook_after_commit_is_a_warning(self):
        _app, info, warning, failure = self.run_to_end(None, ReviewCommittedError('Учебное'))
        warning.assert_called_once()
        info.assert_not_called()
        failure.assert_not_called()

    def test_failure_is_shown(self):
        _app, info, _warning, failure = self.run_to_end(None, RuntimeError('Учебный сбой'))
        failure.assert_called_once()
        info.assert_not_called()


if __name__ == '__main__':
    unittest.main()
