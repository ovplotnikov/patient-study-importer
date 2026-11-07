#!/usr/bin/env python3
"""Compare analysis prototypes against the exact production results."""
import argparse
import inspect
import json
import platform
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter
from unittest.mock import patch

from patient_importer import analysis


ALPHABET = 'АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЫЭЮЯ'


def tag(index):
    prefix = ''
    while index >= len(ALPHABET):
        index, digit = divmod(index, len(ALPHABET))
        prefix = ALPHABET[digit] + prefix
    return ALPHABET[index] + prefix


def prototype(function, replacements):
    source = inspect.getsource(function)
    for original, replacement in replacements:
        if source.count(original) != 1:
            raise RuntimeError(f'Expected one matching code block in {function.__name__}')
        source = source.replace(original, replacement)
    namespace = dict(function.__globals__)
    exec(compile(source, f'<prototype {function.__name__}>', 'exec'), namespace)
    return namespace[function.__name__]


def cached_page_date():
    return prototype(analysis.analyze, [(
        "    for row in laboratory:\n        local = document_date(pages[row['page'] - 1])",
        "    dates_by_page = {}\n"
        "    for row in laboratory:\n"
        "        page_index = row['page'] - 1\n"
        "        if page_index not in dates_by_page:\n"
        "            dates_by_page[page_index] = document_date(pages[page_index])\n"
        "        local = dates_by_page[page_index]",
    )])


def indexed_candidates():
    return prototype(analysis.resolve, [(
        "    compact=defaultdict(set)\n"
        "    for name,birth in keys:compact[(name.replace(' ',''),birth)].add((name,birth))",
        "    compact = defaultdict(set)\n"
        "    by_name = defaultdict(set)\n"
        "    by_birth = defaultdict(set)\n"
        "    for name, birth in keys:\n"
        "        pair = (name, birth)\n"
        "        compact[(name.replace(' ', ''), birth)].add(pair)\n"
        "        by_name[name].add(pair)\n"
        "        by_birth[birth].add(pair)",
    ), (
        "            candidates=[(name,birth) for name,birth in sorted(keys) if birth in a['births'] or name in a['names']]",
        "            matching = set()\n"
        "            for name in a['names']:\n"
        "                matching.update(by_name.get(name, ()))\n"
        "            for birth in a['births']:\n"
        "                matching.update(by_birth.get(birth, ()))\n"
        "            candidates = sorted(matching)",
    )])


def lab_page(row_count, day='02.03.2025'):
    lines = ['Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ', 'Дата рождения: 01.01.2000']
    if day:
        lines.append(f'Дата выполнения исследования: {day}')
    lines.append('Показатель    Результат    Единицы    Референс')
    for i in range(row_count):
        lines.append(f'Вымышленный показатель {tag(i)}    {i + 1}.5    мкмоль/л    40 - 90')
    return '\n'.join(lines + ['Комментарий: Полностью вымышленный пример.']) + '\n'


def document(*pages):
    return {'pages': list(pages), 'native_pages': list(pages), 'ocr_pages': [], 'format': 'TXT'}


def identity_fixture(count):
    patients = [{'name': f'ТЕСТОВ{tag(i)} ТЕСТ ТЕСТОВИЧ',
                 'birth': (date(1970, 1, 1) + timedelta(days=i % 401)).isoformat()}
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


def compare(current, optimized, args, repeats):
    expected = current(*args)
    assert optimized(*args) == expected
    variants = [('current', current), ('prototype', optimized)]
    samples = {name: [] for name, _ in variants}
    for repeat in range(repeats):
        ordered = variants if repeat % 2 == 0 else reversed(variants)
        for name, function in ordered:
            started = perf_counter()
            actual = function(*args)
            samples[name].append(perf_counter() - started)
            assert actual == expected
    medians = {name: median(values) for name, values in samples.items()}
    return expected, {'seconds': medians, 'samples_seconds': samples,
                      'speedup': medians['current'] / medians['prototype'], 'equal': True}


def count_date_calls(function, args):
    counter = 0
    original = function.__globals__['document_date']

    def counted(text):
        nonlocal counter
        counter += 1
        return original(text)

    with patch.dict(function.__globals__, {'document_date': counted}):
        function(*args)
    return counter


def main(args):
    analyze_cached = cached_page_date()
    resolve_indexed = indexed_candidates()
    # Same behavior with missing dates and multiple pages carrying different dates.
    edge = document(lab_page(2, None), lab_page(3), lab_page(4, '05.04.2025'))
    assert analyze_cached(edge, 'synthetic.txt') == analysis.analyze(edge, 'synthetic.txt')
    rows = []
    for count in args.rows:
        arguments = (document(lab_page(count)), 'synthetic.txt')
        parsed, report = compare(analysis.analyze, analyze_cached, arguments, args.repeats)
        assert len(parsed['observations']) == count
        assert all(row['date'] == '2025-03-02' for row in parsed['observations'])
        report.update(kind='lab_page_date', laboratory_rows=count, pages=1, date_calls={
            'current': count_date_calls(analysis.analyze, arguments),
            'prototype': count_date_calls(analyze_cached, arguments),
        })
        rows.append(report)
        print(json.dumps(report), flush=True)
    for count in args.patients:
        resolved, report = compare(analysis.resolve, resolve_indexed, identity_fixture(count), args.repeats)
        unresolved = sum(result['key'] is None for result in resolved.values())
        assert unresolved == (count + 9) // 10
        report.update(kind='patient_candidates', patients=count, documents=len(resolved), unresolved=unresolved,
                      candidate_pairs=sum(len(result['candidates']) for result in resolved.values()))
        rows.append(report)
        print(json.dumps(report), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'platform': platform.platform(), 'python': platform.python_version(),
                                       'results': rows}, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', nargs='+', type=int, default=[20, 100, 500])
    parser.add_argument('--patients', nargs='+', type=int, default=[100, 1000, 3000])
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('analysis-results.json'))
    main(parser.parse_args())
