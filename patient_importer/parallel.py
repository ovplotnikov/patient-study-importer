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


# Marks a job with nothing to compute.
DONE = object()


class Cancelled(Exception):
    """The person stopped the run."""


def available_memory():
    """Free physical memory in bytes, or None when the system does not say."""
    if os.name == 'nt':
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ('total', 'available', 'total_page', 'available_page',
                                                        'total_virtual', 'available_virtual', 'extended')]

        status = Status()
        status.length = ctypes.sizeof(Status)
        try:
            return status.available if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
        except (AttributeError, OSError):
            return None
    try:
        return os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (AttributeError, OSError, ValueError):
        return None


def worker_count(memory_each=None):
    """One core stays free for the window. Heavy work also leaves part of the free memory."""
    count = max(1, min((os.cpu_count() or 1) - 1, MAXIMUM_WORKERS))
    free = available_memory() if memory_each else None
    if free:
        count = min(count, max(1, int(free * .6) // memory_each))
    return count


def process_id(_payload):
    """Lets the packaged self-test see that work really left this process."""
    return os.getpid()


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled


def ordered_map(function, jobs, *, total, workers=None, cancel=None, minimum=None):
    """Yield (key, function(payload)) for (key, payload) jobs in their order.

    `function` must be a module-level function of plain data. Keys stay in this
    process, so they may be anything. A payload of None has nothing to compute and
    yields None. Raises Cancelled when `cancel` is set.
    """
    workers = worker_count() if workers is None else workers
    minimum = MINIMUM_FOR_POOL if minimum is None else minimum
    jobs = iter(jobs)
    executor = _start(workers) if workers > 1 and total >= minimum else None
    if executor is not None:
        pending = deque()
        try:
            for key, payload in jobs:
                _check(cancel)
                if payload is None:
                    pending.append((key, None, DONE))
                    continue
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
        yield key, None if payload is None else function(payload)


def _start(workers):
    try:
        return ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context('spawn'))
    except (OSError, ValueError, NotImplementedError):
        return None


def _take(pending, function, cancel):
    key, payload, future = pending.popleft()
    if future is DONE:
        return key, None
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
