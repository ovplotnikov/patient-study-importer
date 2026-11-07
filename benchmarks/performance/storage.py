#!/usr/bin/env python3
"""Compare production storage paths with in-process performance prototypes."""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from datetime import datetime
from functools import lru_cache, wraps
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch
from uuid import UUID
from zipfile import ZipFile

from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook

from patient_importer import analysis, extraction, storage, workbook


VARIANTS = ('current', 'index', 'catalog', 'styles', 'combined')
STAMP = '2025-03-03T12:00:00+00:00'
ALPHABET = 'АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЫЭЮЯ'
LABELS = ('Креатинин', 'Глюкоза', 'Мочевина', 'Альбумин', 'Белок общий', 'Холестерин',
          'Кальций', 'Натрий', 'Калий', 'Хлор')


def synthetic_document(index):
    patient = index // 5
    patient_tag = ALPHABET[patient // len(ALPHABET)] + ALPHABET[patient % len(ALPHABET)]
    rows = [f'Пациент: ТЕСТОВ{patient_tag} ТЕСТ ТЕСТОВИЧ', 'Дата рождения: 01.01.2000',
            'Дата выполнения исследования: 02.03.2025',
            'Показатель    Результат    Единицы    Референс']
    for offset, label in enumerate(LABELS):
        rows.append(f'{label}    {index + offset}.5    мкмоль/л    40 - 90')
    rows.append('Комментарий: Полностью вымышленный пример.')
    return '\n'.join(rows) + '\n'


def seed(root, count):
    workspace = storage.Workspace(root)
    incoming = root / 'Входящие'
    for index in range(count):
        (incoming / f'пример-{index:05d}.txt').write_text(synthetic_document(index), encoding='utf-8')
    result = workspace.import_folder()
    assert result['documents'] == count and result['patients'] == (count + 4) // 5, result['batch'][:3]
    assert result['pending_moves'] == 0 and result['quality_review'] == 0, result['batch'][:3]
    book = load_workbook(root / 'Исследования.xlsx')
    sheet = book['Показатели']
    assert sheet.max_row == count * len(LABELS) + 1, sheet.max_row
    notes = next(cell.column for cell in sheet[1] if cell.value == 'Заметки')
    sheet.cell(2, notes, 'Синтетическая заметка')
    sheet.cell(1, notes + 1, 'Моя колонка')
    sheet.cell(2, notes + 1, '=1+2')
    book.create_sheet('Мои записи')['B2'] = 'Сохранить лист'
    book.save(root / 'Исследования.xlsx')
    book.close()


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2025, 3, 3, 12, tzinfo=tz)


def instrument(stack, workspace, variant):
    elapsed = defaultdict(float)
    counts = Counter()

    def timed(owner, attr, key):
        original = getattr(owner, attr)

        @wraps(original)
        def wrapped(*args, **kwargs):
            started = perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                counts[key] += 1
                elapsed[key] += perf_counter() - started

        stack.enter_context(patch.object(owner, attr, wrapped))

    for method in ('backup', 'files', 'add_file', 'resolve_patients', 'finish_moves', 'summary'):
        timed(workspace, method, method)
    for method in ('rows_for', 'write_sheet', 'load_workbook', 'export_workbook'):
        timed(workbook, method, method)
    timed(Workbook, 'save', 'workbook_save')
    timed(storage, 'sha256', 'sha256')
    timed(storage.shutil, 'copyfileobj', 'file_copy_stream')
    timed(os, 'fsync', 'fsync')
    timed(os, 'scandir', 'directory_scan_open')
    timed(extraction, 'extract', 'extract')
    timed(analysis, 'analyze', 'analyze')

    original_connect = workspace.connect

    def count_statement(sql):
        counts['sqlite_statements'] += 1
        if sql.startswith('SELECT * FROM arrivals WHERE document_id='):
            counts['arrival_queries'] += 1

    @contextmanager
    def counted_connect():
        with original_connect() as db:
            db.set_trace_callback(count_statement)
            yield db

    stack.enter_context(patch.object(workspace, 'connect', counted_connect))

    original_rows_for = workbook.rows_for
    original_glob = Path.glob
    catalog = None

    def rows_with_catalog(*args, **kwargs):
        nonlocal catalog
        started = perf_counter()
        grouped = defaultdict(list)
        for path in workspace.originals.iterdir():
            stem, separator, _ = path.name.partition('.')
            if separator:
                grouped[os.path.normcase(stem)].append(path)
        catalog = grouped
        counts['original_catalog_builds'] += 1
        counts['original_catalog_entries'] += sum(map(len, grouped.values()))
        elapsed['original_catalog'] += perf_counter() - started
        try:
            return original_rows_for(*args, **kwargs)
        finally:
            catalog = None

    def original_lookup(directory, pattern, *args, **kwargs):
        if directory == workspace.originals and re.fullmatch(r'[0-9a-f]{64}\.\*', pattern):
            counts['original_lookups'] += 1
            if catalog is not None:
                return iter(catalog.get(os.path.normcase(pattern[:-2]), ()))
            counts['original_globs'] += 1
        return original_glob(directory, pattern, *args, **kwargs)

    stack.enter_context(patch.object(Path, 'glob', original_lookup))
    if variant in ('catalog', 'combined'):
        stack.enter_context(patch.object(workbook, 'rows_for', rows_with_catalog))
    if variant in ('styles', 'combined'):
        for constructor in ('Alignment', 'Font', 'PatternFill'):
            factory = lru_cache(maxsize=None)(getattr(workbook, constructor))
            stack.enter_context(patch.object(workbook, constructor, factory))
    return elapsed, counts


def workbook_fingerprint(path):
    # Compare every worksheet, cell, formula, style, relationship, and table.
    # openpyxl writes a fresh archive-level modified timestamp when saving.
    digest = hashlib.sha256()
    with ZipFile(path) as book:
        for name in sorted(set(book.namelist()) - {'docProps/core.xml'}):
            digest.update(name.encode())
            digest.update(hashlib.sha256(book.read(name)).digest())
    return digest.hexdigest()


def peak_rss_mib():
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('page_fault_count', wintypes.DWORD)] + [
                (field, ctypes.c_size_t) for field in (
                    'peak_working_set', 'working_set', 'peak_paged_pool', 'paged_pool',
                    'peak_nonpaged_pool', 'nonpaged_pool', 'pagefile', 'peak_pagefile')]

        counters = ProcessMemoryCounters()
        counters.size = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess
        process.restype = wintypes.HANDLE
        memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        memory_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
        memory_info.restype = wintypes.BOOL
        if not memory_info(process(), ctypes.byref(counters), counters.size):
            raise ctypes.WinError()
        return counters.peak_working_set / (1024 ** 2)
    try:
        import resource
    except ImportError:
        return None
    scale = 1024 ** 2 if sys.platform == 'darwin' else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale


def check_workbook(path, count):
    book = load_workbook(path, read_only=True)
    try:
        sheet = book['Показатели']
        assert sheet.max_row == count * len(LABELS) + 1, sheet.max_row
        header = next(sheet.values)
        note_column = header.index('Заметки')
        custom_column = header.index('Моя колонка')
        # A single new study can shift the first original study by ten rows.
        assert any(row[note_column] == 'Синтетическая заметка' and row[custom_column] == '=1+2'
                   for row in sheet.iter_rows(min_row=2, max_row=2 + len(LABELS), values_only=True))
        assert book['Мои записи']['B2'].value == 'Сохранить лист'
    finally:
        book.close()


def run_case(root, count, variant):
    workspace = storage.Workspace(root)
    incoming = root / 'Входящие'
    (incoming / 'добавленный.txt').write_text(synthetic_document(count), encoding='utf-8')
    (incoming / 'точная-копия.txt').write_text(synthetic_document(0), encoding='utf-8')
    (incoming / 'копия-пробел.txt').write_text(synthetic_document(0).replace('Пациент: ', 'Пациент:  '), encoding='utf-8')
    with workspace.connect() as db:
        if variant in ('index', 'combined'):
            db.execute('CREATE INDEX IF NOT EXISTS perf_arrivals_document ON arrivals(document_id)')
        plan = [row[3] for row in db.execute(
            'EXPLAIN QUERY PLAN SELECT * FROM arrivals WHERE document_id=? ORDER BY id', ('S-example',))]
    rng = random.Random(39)
    with ExitStack() as stack:
        elapsed, counters = instrument(stack, workspace, variant)
        stack.enter_context(patch.object(storage.uuid, 'uuid4', lambda: UUID(int=rng.getrandbits(128), version=4)))
        stack.enter_context(patch.object(storage, 'now', lambda: STAMP))
        stack.enter_context(patch.object(workbook, 'datetime', FixedDatetime))
        start = perf_counter()
        result = workspace.import_folder()
        elapsed['import_total'] = perf_counter() - start
    assert (result['documents'], result['duplicates'], result['pending_moves'], result['errors']) == (count + 1, 2, 0, 0), result['batch']
    rss = peak_rss_mib()
    start = perf_counter()
    workspace.summary()
    elapsed['single_summary'] = perf_counter() - start
    check_workbook(root / 'Исследования.xlsx', count + 1)
    with workspace.connect() as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    report = {'documents_before': count, 'variant': variant, 'seconds': dict(elapsed), 'counts': dict(counters),
              'peak_rss_mib': rss, 'arrival_query_plan': plan,
              'workbook_fingerprint': workbook_fingerprint(root / 'Исследования.xlsx'),
              'verified': ['workbook_rows', 'notes', 'custom_formula', 'custom_sheet', 'duplicates', 'sqlite_integrity']}
    print(json.dumps(report))


def orchestrate(args):
    output = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='storage-synthetic-', dir=args.output.parent) as folder:
        root = Path(folder)
        for size in args.sizes:
            fixture = root / f'fixture-{size}'
            seed(fixture, size)
            hashes = set()
            for repeat in range(args.repeats):
                variants = args.variants if repeat % 2 == 0 else reversed(args.variants)
                for variant in variants:
                    case = root / f'case-{size}-{repeat}-{variant}'
                    shutil.copytree(fixture, case)
                    result = subprocess.run([sys.executable, __file__, '--case', str(case), '--size', str(size),
                                             '--variant', variant], capture_output=True, encoding='utf-8',
                                            env={**os.environ, 'PYTHONUTF8': '1'})
                    if result.returncode:
                        raise RuntimeError(result.stderr or result.stdout)
                    row = json.loads(result.stdout)
                    row['repeat'] = repeat + 1
                    output.append(row)
                    hashes.add(row['workbook_fingerprint'])
                    print(json.dumps(row), flush=True)
                    shutil.rmtree(case)
            assert len(hashes) == 1, f'Generated Excel content differs for {size} documents: {hashes}'
            shutil.rmtree(fixture)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', '--sizes', dest='sizes', type=int, nargs='+', default=[100, 1000])
    parser.add_argument('--variants', choices=VARIANTS, nargs='+', default=['current', 'combined'])
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('results.json'))
    parser.add_argument('--case', type=Path)
    parser.add_argument('--size', type=int)
    parser.add_argument('--variant', choices=VARIANTS)
    options = parser.parse_args()
    if options.case:
        run_case(options.case, options.size, options.variant)
    else:
        orchestrate(options)
