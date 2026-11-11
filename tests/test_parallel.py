"""Spreading work over processes. Payloads are plain fictional numbers."""
import os
import threading
import unittest
from unittest.mock import patch

from patient_importer import parallel


def square(value):
    return value * value


class OrderedMapTests(unittest.TestCase):
    def run_map(self, values, **options):
        jobs = [(f'key{value}', value) for value in values]
        return list(parallel.ordered_map(square, jobs, total=len(jobs), **options))

    def test_results_keep_the_input_order(self):
        values = list(range(40))
        self.assertEqual(self.run_map(values, workers=3, minimum=1), [(f'key{value}', value * value) for value in values])

    def test_work_leaves_this_process(self):
        jobs = [(index, index) for index in range(6)]
        pids = {pid for _key, pid in parallel.ordered_map(parallel.process_id, jobs, total=6, workers=2, minimum=1)}
        self.assertNotIn(os.getpid(), pids)

    def test_few_jobs_stay_here(self):
        jobs = [(index, index) for index in range(3)]
        pids = {pid for _key, pid in parallel.ordered_map(parallel.process_id, jobs, total=3, workers=2)}
        self.assertEqual(pids, {os.getpid()})

    def test_a_pool_that_cannot_start_falls_back(self):
        with patch.object(parallel, 'ProcessPoolExecutor', side_effect=OSError('учебный отказ')):
            self.assertEqual(self.run_map([1, 2, 3], workers=2, minimum=1), [('key1', 1), ('key2', 4), ('key3', 9)])

    def test_a_dead_worker_is_replaced_by_this_process(self):
        class Broken:
            def result(self, timeout=None):
                raise parallel.BrokenProcessPool

        class Pool:
            def __init__(self, *args, **kwargs):
                pass

            def submit(self, function, payload):
                return Broken()

            def shutdown(self, **kwargs):
                pass

        with patch.object(parallel, 'ProcessPoolExecutor', Pool):
            self.assertEqual(self.run_map([2, 3], workers=2, minimum=1), [('key2', 4), ('key3', 9)])

    def test_a_pool_that_breaks_while_submitting_falls_back(self):
        class Pool:
            def __init__(self, *args, **kwargs):
                pass

            def submit(self, function, payload):
                raise parallel.BrokenProcessPool

            def shutdown(self, **kwargs):
                pass

        with patch.object(parallel, 'ProcessPoolExecutor', Pool):
            self.assertEqual(self.run_map([1, 2, 3], workers=2, minimum=1), [('key1', 1), ('key2', 4), ('key3', 9)])

    def test_stop_ends_the_run(self):
        cancel = threading.Event()
        jobs = [(index, index) for index in range(50)]
        seen = []
        with self.assertRaises(parallel.Cancelled):
            for key, _value in parallel.ordered_map(square, jobs, total=50, workers=2, minimum=1, cancel=cancel):
                seen.append(key)
                if key == 3:
                    cancel.set()
        self.assertEqual(seen, [0, 1, 2, 3])

    def test_one_core_is_left_free(self):
        with patch.object(parallel.os, 'cpu_count', return_value=4):
            self.assertEqual(parallel.worker_count(), 3)
        with patch.object(parallel.os, 'cpu_count', return_value=None):
            self.assertEqual(parallel.worker_count(), 1)


if __name__ == '__main__':
    unittest.main()
