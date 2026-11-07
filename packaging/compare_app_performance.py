#!/usr/bin/env python3
"""Compare two production checkouts on disposable, synthetic Windows workloads."""
import argparse
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
import random
import shutil
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch
from uuid import UUID
from zipfile import ZipFile


SCRIPT = Path(__file__).resolve()
CURRENT = SCRIPT.parents[1]
ALPHABET = 'АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЫЭЮЯ'
LABELS = ('Креатинин', 'Глюкоза', 'Мочевина', 'Альбумин', 'Белок общий', 'Холестерин',
          'Кальций', 'Натрий', 'Калий', 'Хлор')
STAMP = '2025-03-03T12:00:00+00:00'
WORKBOOK = 'Исследования.xlsx'
NOTE = 'Синтетическая заметка'
CUSTOM_COLUMN = 'Моя колонка'
CUSTOM_SHEET = 'Мои записи'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def tag(index):
    suffix = ''
    while index >= len(ALPHABET):
        index, digit = divmod(index, len(ALPHABET))
        suffix = ALPHABET[digit] + suffix
    return ALPHABET[index] + suffix


def patient_name(index):
    return f'ТЕСТОВ{tag(index)} ТЕСТ ТЕСТОВИЧ'


def lab_page(patient, labels, start):
    lines = [f'Пациент: {patient}', 'Дата рождения: 01.01.2000',
             'Дата выполнения исследования: 02.03.2025',
             'Показатель    Результат    Единицы    Референс']
    for offset, label in enumerate(labels):
        lines.append(f'{label}    {start + offset}.5    мкмоль/л    40 - 90')
    return '\n'.join(lines + ['Комментарий: Полностью вымышленный пример.']) + '\n'


def study(index):
    return lab_page(patient_name(index // 5), LABELS, index)


def workbook_fingerprint(path):
    # Archive member timestamps are immaterial. openpyxl also writes a fresh
    # modification timestamp into docProps/core.xml on each save.
    digest = hashlib.sha256()
    with ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if name != 'docProps/core.xml':
                digest.update(name.encode('utf-8') + b'\0')
                digest.update(hashlib.sha256(archive.read(name)).digest())
    return digest.hexdigest()


def check_import(result, count, duplicates):
    expected = {'documents': count, 'patients': (count + 4) // 5, 'duplicates': duplicates,
                'pending_moves': 0, 'errors': 0, 'review': 0, 'quality_review': 0, 'remaining': 0}
    require(all(result[key] == value for key, value in expected.items()),
            'Unexpected synthetic import counters')
    require(not result['pending_export'], 'Synthetic workbook export did not complete')
    return expected


def seed(storage, root, count):
    from openpyxl import load_workbook

    workspace = storage.Workspace(root)
    incoming = root / 'Входящие'
    for index in range(count):
        (incoming / f'пример-{index:05d}.txt').write_text(study(index), encoding='utf-8')
    check_import(workspace.import_folder(), count, 0)
    book = load_workbook(root / WORKBOOK)
    try:
        sheet = book['Показатели']
        require(sheet.max_row == count * len(LABELS) + 1, 'Unexpected fixture workbook size')
        notes = next(cell.column for cell in sheet[1] if cell.value == 'Заметки')
        sheet.cell(2, notes, NOTE)
        sheet.cell(1, notes + 1, CUSTOM_COLUMN)
        sheet.cell(2, notes + 1, '=1+2')
        book.create_sheet(CUSTOM_SHEET)['B2'] = 'Сохранить лист'
        book.save(root / WORKBOOK)
    finally:
        book.close()
    return {'documents': count}


def check_workbook(path, count, note_result_id):
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True)
    try:
        sheet = book['Показатели']
        require(sheet.max_row == count * len(LABELS) + 1, 'Unexpected final workbook size')
        header = next(sheet.values)
        id_column = header.index('ResultID')
        note_column = header.index('Заметки')
        formula_column = header.index(CUSTOM_COLUMN)
        for row in sheet.iter_rows(min_row=2):
            if row[id_column].value == note_result_id:
                require(row[note_column].value == NOTE, 'Custom note changed')
                require(row[formula_column].value == '=1+2' and row[formula_column].data_type == 'f',
                        'Custom formula changed')
                break
        else:
            raise RuntimeError('Workbook lost the row containing the custom note')
        require(book[CUSTOM_SHEET]['B2'].value == 'Сохранить лист', 'Custom sheet changed')
    finally:
        book.close()


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2025, 3, 3, 12, tzinfo=tz)


def measure_import(storage, workbook, root, count):
    from openpyxl import load_workbook

    path = root / WORKBOOK
    before = load_workbook(path, read_only=True)
    try:
        note_result_id = before['Показатели']['A2'].value
    finally:
        before.close()

    workspace = storage.Workspace(root)
    incoming = root / 'Входящие'
    (incoming / 'добавленный.txt').write_text(study(count), encoding='utf-8')
    (incoming / 'точная-копия.txt').write_text(study(0), encoding='utf-8')
    (incoming / 'копия-пробел.txt').write_text(study(0).replace('Пациент: ', 'Пациент:  '), encoding='utf-8')
    rng = random.Random(39)

    def fixed_uuid():
        return UUID(int=rng.getrandbits(128), version=4)

    # Stabilize IDs and user-visible timestamps only. Both timed calls execute
    # the entire production import, including backup, archive moves and XLSX.
    with (patch.object(storage.uuid, 'uuid4', fixed_uuid),
          patch.object(storage, 'now', lambda: STAMP),
          patch.object(workbook, 'datetime', FixedDatetime)):
        start = perf_counter()
        result = workspace.import_folder()
        elapsed = perf_counter() - start
    counters = check_import(result, count + 1, 2)
    check_workbook(path, count + 1, note_result_id)
    with workspace.connect() as db:
        require([row[0] for row in db.execute('PRAGMA integrity_check')] == ['ok'],
                'Synthetic registry integrity check failed')
        require(db.execute('PRAGMA foreign_key_check').fetchone() is None,
                'Synthetic registry foreign key check failed')
    return {'seconds': elapsed, 'fingerprint': workbook_fingerprint(path), 'counts': counters}


def identity_fixture(count):
    patients = [{'name': patient_name(i), 'birth': (date(1970, 1, 1) + timedelta(days=i % 401)).isoformat()}
                for i in range(count)]
    records = []
    for index, patient in enumerate(patients):
        names, births = [patient['name']], [patient['birth']]
        from_ocr = False
        if index % 10 == 0:
            case = index // 10 % 5
            if case == 0:
                births.clear()
            elif case == 1:
                names.clear()
            elif case == 2:
                from_ocr = True
            elif case == 3:
                other = patients[(index + 1) % count]
                names.append(other['name'])
                births.append(other['birth'])
            else:
                names.clear()
                births.clear()
        records.append({'id': str(index), 'folder': f'Синтетический набор {index // 50}',
                        'analysis': {'names': names, 'births': births, 'identity_from_ocr': from_ocr}})
    return records, patients


def measure_analysis(analysis, kind, count):
    if kind == 'analysis':
        labels = [f'Вымышленный показатель {tag(i)}' for i in range(count)]
        page = lab_page(patient_name(0), labels, 1)
        function = analysis.analyze
        args = ({'pages': [page], 'native_pages': [page], 'ocr_pages': [], 'format': 'TXT'}, 'synthetic.txt')
    else:
        function = analysis.resolve
        args = identity_fixture(count)

    start = perf_counter()
    result = function(*args)
    elapsed = perf_counter() - start
    serialized = json.dumps(result, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if kind == 'analysis':
        observations = result['observations']
        require(len(observations) == count and all(row['date'] == '2025-03-02' for row in observations),
                'Synthetic laboratory rows or dates changed')
        counts = {'observations': len(observations)}
    else:
        unresolved = sum(entry['key'] is None for entry in result.values())
        require(len(result) == count and unresolved == (count + 9) // 10, 'Unexpected identity resolution counts')
        counts = {'documents': len(result), 'unresolved': unresolved,
                  'candidate_pairs': sum(len(entry['candidates']) for entry in result.values())}
    return {'seconds': elapsed, 'fingerprint': hashlib.sha256(serialized).hexdigest(), 'counts': counts}


def worker(kind, size, folder):
    import patient_importer
    from patient_importer import analysis, storage, workbook

    require(Path(patient_importer.__file__).resolve().parent == (Path.cwd() / 'patient_importer').resolve(),
            'Worker imported the wrong checkout')
    if kind == 'seed':
        result = seed(storage, folder, size)
    elif kind == 'import':
        result = measure_import(storage, workbook, folder, size)
    elif kind in {'analysis', 'candidates'}:
        result = measure_analysis(analysis, kind, size)
    else:
        raise RuntimeError('Unknown synthetic worker kind')
    return {'version': patient_importer.__version__, **result}


def run_worker(repo, kind, size, folder=None):
    command = [sys.executable, str(SCRIPT), '--worker', kind, str(size)]
    if folder is not None:
        command.append(str(folder))
    process = subprocess.run(command, cwd=repo, capture_output=True, encoding='utf-8', timeout=300,
                             env={**os.environ, 'PYTHONPATH': str(repo), 'PYTHONUTF8': '1', 'PYTHONHASHSEED': '0'})
    require(process.returncode == 0, f'Synthetic {kind} worker failed ({process.returncode}):\n{process.stderr}')
    return json.loads(process.stdout)


def paired_result(kind, size, samples):
    fingerprint = samples['baseline'][0]['fingerprint']
    counts = samples['baseline'][0]['counts']
    versions = {}
    timings = {}
    for variant, rows in samples.items():
        require(all(row['fingerprint'] == fingerprint and row['counts'] == counts for row in rows),
                f'Baseline and current {kind} outputs differ at size {size} ({variant})')
        versions[variant] = rows[0]['version']
        timings[variant] = [row['seconds'] for row in rows]
    medians = {variant: median(values) for variant, values in timings.items()}
    speedup = medians['baseline'] / medians['current']
    print(f'{kind} {size}: {medians["baseline"]:.4f}s -> {medians["current"]:.4f}s ({speedup:.2f}x), parity verified',
          flush=True)
    return {'kind': kind, 'size': size, 'versions': versions, 'samples_seconds': timings, 'median_seconds': medians,
            'speedup': speedup, 'fingerprint': fingerprint, 'counts': counts, 'parity': True}


def compare(args):
    repos = {'baseline': args.baseline.resolve(), 'current': CURRENT}
    require(repos['baseline'] != CURRENT, 'Use a separate checkout as --baseline')
    require((repos['baseline'] / 'patient_importer' / '__init__.py').is_file(), 'Invalid baseline checkout')
    results = []
    with tempfile.TemporaryDirectory(prefix='psi-perf-') as temporary:
        root = Path(temporary).resolve()
        for size in args.sizes:
            fixture = root / 'fixture'
            run_worker(repos['baseline'], 'seed', size, folder=fixture)
            samples = {variant: [] for variant in repos}
            case = root / 'case'
            for repeat in range(args.repeats):
                order = repos if repeat % 2 == 0 else reversed(repos)
                for variant in order:
                    shutil.copytree(fixture, case)
                    samples[variant].append(run_worker(repos[variant], 'import', size, folder=case))
                    shutil.rmtree(case)
            results.append(paired_result('import', size, samples))
            shutil.rmtree(fixture)
        for kind, sizes in [('analysis', args.rows), ('candidates', args.patients)]:
            for size in sizes:
                samples = {variant: [] for variant in repos}
                for repeat in range(args.repeats):
                    order = repos if repeat % 2 == 0 else reversed(repos)
                    for variant in order:
                        samples[variant].append(run_worker(repos[variant], kind, size))
                results.append(paired_result(kind, size, samples))
    report = {'schema_version': 1, 'python': sys.version.split()[0], 'platform': sys.platform,
              'repeats': args.repeats, 'results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


def positive_int(raw):
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError('expected a positive integer')
    return value


if __name__ == '__main__':
    if sys.argv[1:2] == ['--worker']:
        _, _, kind, size, *folders = sys.argv
        folder = Path(folders[0]) if folders else None
        print(json.dumps(worker(kind, int(size), folder)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--baseline', required=True, type=Path)
        parser.add_argument('--output', required=True, type=Path)
        parser.add_argument('--sizes', nargs='+', type=positive_int, default=[100, 1000])
        parser.add_argument('--rows', nargs='+', type=positive_int, default=[20, 100])
        parser.add_argument('--patients', nargs='+', type=positive_int, default=[1000, 3000])
        parser.add_argument('--repeats', type=positive_int, default=3)
        compare(parser.parse_args())
