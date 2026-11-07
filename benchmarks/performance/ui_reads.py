"""Measure storage reads reached from Tk callbacks without starting Tk.

Run with PYTHONPATH pointing to the repository. Data are generated in an
automatically removed temporary workspace. stdout contains only timings and sizes.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import platform
from statistics import median
import tempfile
from time import perf_counter

from patient_importer import quality
from patient_importer.storage import Workspace


def create_payload():
    lines = [
        'Учебный лабораторный бланк. Все сведения вымышлены.',
        'Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ',
        'Дата рождения: 01.01.2000. Дата исследования: 02.03.2025.',
    ]
    observations = []
    for index in range(30):
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
    extracted = json.dumps({'pages': pages, 'ocr_pages': [1], 'ocr_evidence': evidence}, ensure_ascii=False)
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
    review = json.dumps(analysis, ensure_ascii=False)
    return extracted, verified, review


def seed(workspace, count, payload):
    extracted, verified, review = payload
    with workspace.connect() as db:
        db.execute('INSERT INTO patients VALUES (?,?,?)',
                   ('TEST-PATIENT', 'ТЕСТОВ ТЕСТ ТЕСТОВИЧ', '2000-01-01'))
        db.executemany(
            '''INSERT INTO documents
               (id,hash,filename,folder,original,extracted,analysis,patient_id,created)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            ((f'TEST-{index:06d}', sha256(str(index).encode()).hexdigest(),
              f'учебный-{index:06d}.pdf', '', 'synthetic-unused.pdf', extracted,
              review if index % 10 == 0 else verified, 'TEST-PATIENT', '2025-03-02T10:00:00')
             for index in range(count)),
        )
        db.executemany(
            '''INSERT INTO arrivals (source,hash,document_id,category,destination,status,created)
               VALUES (?,?,?,?,?,'done',?)''',
            ((f'учебный-{index:06d}.pdf', sha256(str(index).encode()).hexdigest(),
              f'TEST-{index:06d}', 'Нужно уточнить' if index % 10 == 0 else 'Обработано',
              f'архив/учебный-{index:06d}.pdf', '2025-03-02T10:00:00')
             for index in range(count)),
        )


def batch_status(record):
    parsed = json.loads(record['analysis'])
    if parsed.get('duplicate_of'):
        return 'Повтор'
    if not record['patient_id']:
        return 'Уточнить пациента'
    if quality.needs_review(parsed):
        return 'Проверить данные'
    if parsed.get('quality', {}).get('incomplete'):
        return 'Сверено с замечаниями'
    return 'Добавлен'


def individual_batch(workspace, ids):
    return [batch_status(workspace.document(document_id)) for document_id in ids]


def grouped_batch(workspace, ids):
    statuses = {}
    with workspace.connect() as db:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            query = ('SELECT id, patient_id, analysis FROM documents WHERE id IN ('
                     + ','.join('?' for _ in chunk) + ')')
            for record in db.execute(query, chunk):
                statuses[record['id']] = batch_status(record)
    return [statuses[document_id] for document_id in ids]


def timed(call, repeat):
    samples = []
    result = None
    for _ in range(repeat):
        result = None
        begin = perf_counter()
        result = call()
        samples.append((perf_counter() - begin) * 1000)
    return {'median_ms': round(median(samples), 3), 'samples_ms': [round(t, 3) for t in samples]}, result


def benchmark(count, repeat, batch_count):
    payload = create_payload()
    with tempfile.TemporaryDirectory(prefix='patient-ui-reads-') as temp:
        workspace = Workspace(Path(temp) / 'synthetic')
        seed(workspace, count, payload)
        ids = [f'TEST-{index:06d}' for index in range(min(count, batch_count))]
        result = {
            'documents': count, 'batch_documents': len(ids), 'quality_review_expected': (count + 9) // 10,
            'database_bytes': workspace.database.stat().st_size,
            'analysis_bytes': len(payload[1].encode()), 'extracted_bytes': len(payload[0].encode()),
        }
        result['documents_view_one_summary'], single = timed(workspace.summary, repeat)
        result['documents_view_two_summaries'], double = timed(
            lambda: (workspace.summary(), workspace.summary()), repeat)
        assert single == double[0] == double[1]
        assert single['documents'] == count and single['quality_review'] == result['quality_review_expected']
        result['result_view_quality_list'], review = timed(workspace.quality_review_list, repeat)
        assert len(review) == result['quality_review_expected']
        del review
        result['history_document_list'], history = timed(workspace.document_list, repeat)
        assert len(history) == count
        del history
        result['result_view_individual_batch'], individual = timed(lambda: individual_batch(workspace, ids), repeat)
        result['result_view_grouped_batch'], grouped = timed(lambda: grouped_batch(workspace, ids), repeat)
        assert individual == grouped
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[100, 1000, 5000])
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--batch-count', type=int, default=500)
    parser.add_argument('--output', type=Path, help='Also save the JSON lines to this file')
    args = parser.parse_args()
    if min(args.counts) <= 0 or args.repeat <= 0 or args.batch_count <= 0:
        parser.error('counts, repeat, and batch-count must be positive')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text('', encoding='utf-8')
    for count in args.counts:
        record = benchmark(count, args.repeat, args.batch_count)
        record['platform'] = platform.platform()
        record['python'] = platform.python_version()
        line = json.dumps(record)
        print(line, flush=True)
        if args.output:
            with args.output.open('a', encoding='utf-8') as output:
                output.write(line + '\n')
