"""Exercise workspace action lifecycle through Tcl without a desktop window."""

from threading import get_ident
from time import monotonic, sleep
from tkinter import Tcl
import unittest
from unittest.mock import patch

from patient_importer.ui import WorkspaceActions


class WorkspaceActionTests(unittest.TestCase):
    def setUp(self):
        self.root = Tcl()
        self.owner_thread = get_ident()
        self.completed = []
        self.idle = []
        self.actions = WorkspaceActions(self.root, on_idle=lambda: self.idle.append(get_ident()))

    def finish(self):
        deadline = monotonic() + 3
        while self.actions.active:
            self.root.update()
            if monotonic() >= deadline:
                self.fail('Workspace action never reached completion')
            sleep(.005)
        self.assertEqual(self.idle, [self.owner_thread])

    def record(self, value, error):
        self.completed.append((value, error, get_ident()))

    def test_aborted_worker_reports_failure_and_releases_gate(self):
        def work():
            raise SystemExit(17)

        self.assertTrue(self.actions.run(work, self.record))
        self.finish()
        value, error, thread = self.completed[0]
        self.assertIsNone(value)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(thread, self.owner_thread)

    def test_worker_start_failure_is_presented_on_the_tk_thread(self):
        failure = RuntimeError('Synthetic thread creation failure')
        with patch('patient_importer.ui.Thread') as thread:
            thread.return_value.start.side_effect = failure
            self.assertTrue(self.actions.run(lambda: self.fail('Worker must not run'), self.record))
        self.finish()
        self.assertEqual(self.completed, [(None, failure, self.owner_thread)])

    def test_closing_skips_error_presentation_but_delivers_idle(self):
        self.actions.can_present = lambda: False

        def work():
            raise ValueError('Synthetic operation failure')

        self.assertTrue(self.actions.run(work, self.record))
        self.finish()
        self.assertEqual(self.completed, [])

    def test_completion_cannot_start_a_reentrant_operation(self):
        restarted = []

        def complete(value, error):
            self.record(value, error)
            restarted.append(self.actions.run(lambda: 'second operation', self.record))

        self.assertTrue(self.actions.run(get_ident, complete))
        self.finish()
        worker_thread, error, completion_thread = self.completed[0]
        self.assertNotEqual(worker_thread, self.owner_thread)
        self.assertIsNone(error)
        self.assertEqual(completion_thread, self.owner_thread)
        self.assertEqual(restarted, [False])


if __name__ == '__main__':
    unittest.main()
