#!/usr/bin/env python3
"""Measure production summary and status reads in two checkouts using a synthetic registry."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter


SCRIPT = Path(__file__).resolve()
CURRENT = SCRIPT.parents[1]
OBSERVATIONS = 30
STAMP = '2025-03-02T10:00:00'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return sha256(encode(value).encode('utf-8')).hexdigest()


def create_payload():
    # Same 30 observations and 63 lines/evidence entries as the original
    # performance audit's benchmarks/performance/ui_reads.py fixture.
    lines = [
        'Учебный лабораторный бланк. Все сведения вымышлены.',
        'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ',
        'Дата рождения: 01.01.2000. Дата исследования: 02.03.2025.',
    ]
    observations = []
    for index in range(OBSERVATIONS):
        label = f'Учебный показатель {index + 1:02d}'
        result = str(index + 50)
        source_line = f'{label}   {result}   мкмоль/л   40–90'
        lines.append(source_line)
        observations.append({
            'label': label, 'result': result, 'number': float(result),
            'unit': 'мкмоль/л', 'reference': '40–90',
            'page': 1, 'page_index': 1, 'line': len(lines), 'source_line': source_line,
        })
    lines.extend(f'Учебная строка {index:02d}. Условие подготовки и примечание к образцу.'
                 for index in range(30))
    pages = ['\n'.join(lines)]
    evidence = [
        {'page': 1, 'page_index': 1, 'line': index, 'text': line,
         'bbox': [40, 20 * index, 720, 20 * index + 18], 'image_size': [1240, 1754],
         'confidence': 0.98, 'engine': 'synthetic', 'source_kind': 'page'}
        for index, line in enumerate(lines, 1)
    ]
    extracted = {'pages': pages, 'ocr_pages': [1], 'ocr_evidence': evidence}
    analysis = {
        'type': 'Лабораторные исследования', 'date': '2025-03-02',
        'names': ['ТЕСТОВ ТЕСТ ТЕСТОВИЧ'], 'births': ['2000-01-01'],
        'pages': pages, 'page_numbers': [1], 'ocr_pages': [1],
        'observations': observations, 'sections': [], 'ocr_evidence': evidence,
        'quality': {'confirmed': True, 'issues': []},
    }
    verified = json.dumps(analysis, ensure_ascii=False)
    analysis['quality'] = {
        'confirmed': False,
        'issues': [{'code': 'OCR_USED', 'message': 'Сверьте результаты со скана.', 'page': 1}],
    }
    return json.dumps(extracted, ensure_ascii=False), verified, json.dumps(analysis, ensure_ascii=False)


def seed(workspace, count):
    extracted, verified, review = create_payload()
    rows = []
    for index in range(count):
        ident = f'TEST-{index:06d}'
        digest = sha256(str(index).encode()).hexdigest()
        filename = f'учебный-{index:06d}.pdf'
        needs_review = index % 10 == 0
        rows.append((ident, digest, filename, needs_review))
    with workspace.connect() as db:
        db.execute('INSERT INTO patients VALUES (?,?,?)',
                   ('TEST-PATIENT', 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01'))
        db.executemany(
            '''INSERT INTO documents
               (id,hash,filename,folder,original,extracted,analysis,patient_id,created)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            ((ident, digest, filename, '', 'synthetic-unused.pdf', extracted,
              review if needs_review else verified, 'TEST-PATIENT', STAMP)
             for ident, digest, filename, needs_review in rows),
        )
        db.executemany(
            '''INSERT INTO arrivals (source,hash,document_id,category,destination,status,created)
               VALUES (?,?,?,?,?,'done',?)''',
            ((filename, digest, ident, 'Нужно уточнить' if needs_review else 'Обработано',
              f'архив/{filename}', STAMP)
             for ident, digest, filename, needs_review in rows),
        )
    return {'documents': count, 'observations_per_document': OBSERVATIONS, 'evidence_per_document': 63,
            'database_bytes': workspace.database.stat().st_size}


def status(record, needs_review):
    """Apply the same status precedence as Application.show_result."""
    parsed = json.loads(record['analysis'])
    if parsed.get('duplicate_of'):
        return 'duplicate'
    if not record['patient_id']:
        return 'identity_review'
    if needs_review(parsed):
        return 'quality_review'
    if parsed.get('quality', {}).get('incomplete'):
        return 'incomplete'
    return 'saved'


def measure(call, repeats, normalize):
    samples = []
    expected = None
    result = None
    for _ in range(repeats):
        start = perf_counter()
        result = call()
        samples.append(perf_counter() - start)
        result = normalize(result)
        digest = fingerprint(result)
        if expected is None:
            expected = digest
        elif digest != expected:
            raise RuntimeError('Repeated reads returned different results')
    return {'samples_ms': [round(seconds * 1000, 3) for seconds in samples],
            'median_ms': round(median(samples) * 1000, 3), 'fingerprint': expected}, result


def worker(mode, directory, count, batch_count, repeats):
    import patient_importer
    from patient_importer.quality import needs_review
    from patient_importer.storage import Workspace

    if Path(patient_importer.__file__).resolve().parent != Path.cwd().resolve() / 'patient_importer':
        raise RuntimeError('Worker imported the wrong checkout')
    workspace = Workspace(directory)
    if mode == 'seed':
        return seed(workspace, count)

    ids = [f'TEST-{index:06d}' for index in range(min(count, batch_count))]
    if mode == 'baseline':
        def batch_statuses():
            return [status(workspace.document(ident), needs_review) for ident in ids]
    else:
        def batch_statuses():
            documents = workspace.documents_by_ids(ids)
            return [status(documents[ident], needs_review) for ident in ids]

    summary, counters = measure(workspace.summary, repeats,
                                lambda result: {key: value for key, value in result.items() if key != 'workbook'})
    batch, statuses = measure(batch_statuses, repeats, lambda result: result)
    if (counters['documents'] != count or counters['quality_review'] != (count + 9) // 10
            or statuses.count('quality_review') != (len(ids) + 9) // 10
            or statuses.count('saved') + statuses.count('quality_review') != len(ids)):
        raise RuntimeError('Unexpected counts in synthetic registry')
    return {'summary': {**summary, 'counts': counters},
            'statuses': {**batch, 'counts': dict(Counter(statuses))}}


def run_worker(checkout, mode, directory, args):
    env = {**os.environ, 'PYTHONPATH': str(checkout), 'PYTHONUTF8': '1'}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), '--worker', mode, str(directory),
         str(args.count), str(args.batch_count), str(args.repeats)],
        cwd=checkout, env=env, capture_output=True, encoding='utf-8', timeout=300,
    )
    if result.returncode:
        raise RuntimeError(f'Synthetic {mode} worker failed ({result.returncode}):\n{result.stderr}')
    return json.loads(result.stdout)


def compare(args):
    baseline = args.baseline.resolve()
    if baseline == CURRENT or not (baseline / 'patient_importer' / '__init__.py').is_file():
        raise ValueError('Use a separate patient-study-importer checkout as --baseline')
    with TemporaryDirectory(prefix='psi-registry-benchmark-') as temporary:
        folder = Path(temporary)
        fixture = folder / 'fixture'
        fixture_counts = run_worker(baseline, 'seed', fixture, args)
        runs = {}
        for name, checkout in [('baseline', baseline), ('current', CURRENT)]:
            workspace = folder / 'workspace'
            shutil.copytree(fixture, workspace)
            runs[name] = run_worker(checkout, name, workspace, args)
            shutil.rmtree(workspace)

    comparisons = {}
    for operation in ('summary', 'statuses'):
        old, new = (runs[name][operation] for name in ('baseline', 'current'))
        if old['fingerprint'] != new['fingerprint'] or old['counts'] != new['counts']:
            raise RuntimeError(f'{operation} results differ between checkouts')
        comparisons[operation] = {
            'parity': True,
            'fingerprint': old['fingerprint'],
            'counts': old['counts'],
            'samples_ms': {name: results[operation]['samples_ms'] for name, results in runs.items()},
            'median_ms': {name: results[operation]['median_ms'] for name, results in runs.items()},
            'speedup': round(old['median_ms'] / new['median_ms'], 3),
        }
    report = {'schema_version': 1, 'python': sys.version.split()[0], 'platform': sys.platform,
              'fixture': fixture_counts, 'batch_documents': min(args.count, args.batch_count),
              'repeats': args.repeats, 'results': comparisons}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--worker']:
        _, _, mode, directory, count, batch_count, repeats = sys.argv
        print(json.dumps(worker(mode, Path(directory), int(count), int(batch_count), int(repeats))))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--baseline', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        parser.add_argument('--count', type=int, default=5000)
        parser.add_argument('--batch-count', type=int, default=500)
        parser.add_argument('--repeats', type=int, default=3)
        args = parser.parse_args()
        if min(args.count, args.batch_count, args.repeats) < 1:
            parser.error('count, batch-count, and repeats must be positive')
        compare(args)
