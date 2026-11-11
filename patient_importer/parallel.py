"""Spread pure CPU work over processor cores, keeping input order.

Python runs one thread of its own code at a time, so only separate processes use
more than one core. Anything that fails to start a process pool falls back to
this process: slower, never wrong.
"""
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing
import os

# A few documents are faster here than starting worker processes.
MINIMUM_FOR_POOL = 24
MAXIMUM_WORKERS = 8


class Cancelled(Exception):
    """The person stopped the run."""


def worker_count():
    # One core stays free for the window and the system.
    return max(1, min((os.cpu_count() or 1) - 1, MAXIMUM_WORKERS))


def process_id(_payload):
    """Lets the packaged self-test see that work really left this process."""
    return os.getpid()


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled


def ordered_map(function, jobs, *, total, workers=None, cancel=None, minimum=MINIMUM_FOR_POOL):
    """Yield (key, function(payload)) for (key, payload) jobs in their order.

    `function` must be a module-level function of plain data. Keys stay in this
    process, so they may be anything. Raises Cancelled when `cancel` is set.
    """
    workers = worker_count() if workers is None else workers
    jobs = iter(jobs)
    executor = _start(workers) if workers > 1 and total >= minimum else None
    if executor is not None:
        pending = deque()
        try:
            for key, payload in jobs:
                _check(cancel)
                try:
                    future = executor.submit(function, payload)
                except BrokenProcessPool:
                    # The rest is read here, below.
                    pending.append((key, payload, None))
                    break
                pending.append((key, payload, future))
                # A bounded window keeps memory flat however many documents there are.
                while len(pending) >= workers * 4:
                    yield _take(pending, function, cancel)
            while pending:
                yield _take(pending, function, cancel)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    for key, payload in jobs:
        _check(cancel)
        yield key, function(payload)


def _start(workers):
    try:
        return ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context('spawn'))
    except (OSError, ValueError, NotImplementedError):
        return None


def _take(pending, function, cancel):
    key, payload, future = pending.popleft()
    while future is not None:
        _check(cancel)
        try:
            return key, future.result(timeout=.2)
        except TimeoutError:
            continue
        except BrokenProcessPool:
            # A worker died, for example when memory ran out. Finish here.
            break
    return key, function(payload)
