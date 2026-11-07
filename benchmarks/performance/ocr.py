"""Isolated performance benchmark using only generated Russian forms."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import asdict
import hashlib
import io
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


REPO = Path.cwd().resolve()
sys.path[:0] = [str(REPO), str(REPO / 'packaging')]

from PIL import Image
from patient_importer import analysis, extraction, ocr
from patient_importer.smoke import make_mixed_pdf
from benchmark_ocr import synthetic_form
from ocr_models import RAPID_ONNX, TESS_FAST
from pypdf import PdfWriter


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(data):
    return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode())


def tesseract_pipe(image, executable, tessdata, engine, psm=3):
    stream = io.BytesIO()
    image.save(stream, 'PNG', compress_level=1)
    command = [str(executable), 'stdin', 'stdout', '--tessdata-dir',
               str(tessdata), '-l', 'rus+eng', '--psm', str(psm),
               '-c', 'tessedit_create_tsv=1']
    try:
        result = subprocess.run(command, input=stream.getvalue(), capture_output=True,
                                timeout=180, creationflags=0x08000000 if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Не удалось запустить распознавание страницы скана') from exc
    if result.returncode:
        raise RuntimeError('Не удалось распознать страницу скана')
    words = []
    for row in csv.DictReader(io.StringIO(result.stdout.decode('utf-8', 'replace')), delimiter='\t'):
        if row.get('level') != '5' or not row.get('text', '').strip():
            continue
        left, top, width, height = (int(row[key]) for key in ('left', 'top', 'width', 'height'))
        conf = float(row['conf'])
        words.append((row['text'], (left, top, left + width, top + height),
                      min(1., conf / 100) if conf >= 0 else None))
    return ocr.assemble_page(words, engine, image.size)


class Benchmark:
    def __init__(self, mode, vendor, executable):
        self.mode = mode
        self.vendor = vendor
        self.executable = executable
        self.tessdata = vendor / 'tesseract' / 'tessdata'
        self.rapid_page = extraction.rapid_page
        self.recognize_image = extraction.recognize_image
        self.tesseract_page = extraction.tesseract_page if mode != 'overlap_pipe' else tesseract_pipe
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.page_results = []
        self.timings = {}

    def resource_path(self, name):
        if name in {'tesseract/tesseract', 'tesseract/tesseract.exe'}:
            return self.executable
        return self.vendor / name

    def time_call(self, key, function, *args, **kwargs):
        start = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            self.timings.setdefault(key, []).append(time.perf_counter() - start)

    def rapid(self, image, *args, **kwargs):
        if self.mode != 'baseline':
            self.future = self.worker.submit(self.time_call, 'tesseract', self.tesseract_page,
                                             image.copy(), self.executable, self.tessdata,
                                             'tesseract-fast')
        return self.time_call('rapid', self.rapid_page, image, *args, **kwargs)

    def tesseract(self, image, executable, tessdata, *args, **kwargs):
        if self.future is not None:
            future, self.future = self.future, None
            return future.result()
        return self.time_call('tesseract', self.tesseract_page, image, self.executable,
                              self.tessdata, *args, **kwargs)

    def recognize(self, image):
        page = self.recognize_image(image)
        self.page_results.append(asdict(page))
        return page

    def run(self, source):
        self.page_results = []
        self.timings = {}
        start = time.perf_counter()
        data = extraction.extract(source)
        parsed = analysis.analyze(data, source.name)
        wall = time.perf_counter() - start
        assert self.page_results and all(page['verification_engine'] == 'tesseract-fast'
                                         and not page['unavailable'] for page in self.page_results)
        assert parsed['quality']['confirmed'] is False
        return {'seconds': wall, 'stages_seconds': self.timings, 'page': fingerprint(self.page_results),
                'extracted': fingerprint(data), 'analysis': fingerprint(parsed), 'pages': len(self.page_results)}


def fixtures(root):
    fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' if os.name == 'nt' else Path('/System/Library/Fonts/Supplemental')
    form = synthetic_form(str(fonts / 'arial.ttf'), 0, False)[0]
    other = synthetic_form(str(fonts / 'verdana.ttf'), 2, True)[0]
    small = Image.new('RGB', other.size, 'white')
    small.paste(other.resize((850, 540), Image.Resampling.LANCZOS), (120, 80))
    paths = []
    for name, image in [('clean', form), ('rotation', form.transpose(Image.Transpose.ROTATE_90)),
                        ('small_type', small),
                        ('skew', other.rotate(3.5, resample=Image.Resampling.BICUBIC,
                                              fillcolor='white', expand=True))]:
        file = root / (name + '.png')
        image.save(file)
        paths.append(file)
    scan = root / 'scan.pdf'
    form.save(scan, 'PDF', resolution=180)
    paths.append(scan)
    return paths


def check_weights(vendor):
    for name, expected in RAPID_ONNX.items():
        file = vendor / 'rapidocr' / Path(name).name
        assert sha256(file.read_bytes()) == expected, file.name
    for name, expected in TESS_FAST.items():
        file = vendor / 'tesseract' / 'tessdata' / name
        assert sha256(file.read_bytes()) == expected, file.name


def rss_mib(*, children=False):
    if os.name == 'nt':
        if children:
            return None
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('page_fault_count', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak_working_set', 'working_set', 'peak_paged_pool',
                                                     'paged_pool', 'peak_nonpaged_pool', 'nonpaged_pool',
                                                     'pagefile', 'peak_pagefile')]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_memory_info = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        get_memory_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
        get_memory_info.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not get_memory_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return round(counters.peak_working_set / (1 << 20), 1)
    import resource

    kind = resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF
    unit = 1 << (20 if sys.platform == 'darwin' else 10)
    return round(resource.getrusage(kind).ru_maxrss / unit, 1)


def worker(args):
    benchmark = Benchmark(args.worker, args.vendor, args.tesseract)
    try:
        with tempfile.TemporaryDirectory(prefix='synthetic-ocr-', dir=Path(__file__).parent) as directory:
            paths = fixtures(Path(directory))
            with patch.object(extraction, 'resource_path', side_effect=benchmark.resource_path), \
                 patch.object(extraction, 'rapid_page', side_effect=benchmark.rapid), \
                 patch.object(extraction, 'tesseract_page', side_effect=benchmark.tesseract), \
                 patch.object(extraction, 'recognize_image', side_effect=benchmark.recognize):
                cold = benchmark.run(paths[0])
                cold_rss = rss_mib()
                warm = {path.stem: [] for path in paths}
                for iteration in range(args.repeats):
                    for path in paths[iteration % len(paths):] + paths[:iteration % len(paths)]:
                        warm[path.stem].append(benchmark.run(path))
                return {'mode': args.worker, 'cold': cold, 'warm': warm,
                        'cold_self_peak_rss_mib': cold_rss,
                        'self_peak_rss_mib': rss_mib(),
                        'children_reported_peak_rss_mib': rss_mib(children=True)}
    finally:
        benchmark.worker.shutdown(wait=True)


def compare(reference, candidate):
    for key in ('page', 'extracted', 'analysis'):
        assert reference['cold'][key] == candidate['cold'][key], ('cold', candidate['mode'], key)
        for name, measurements in reference['warm'].items():
            values = [item[key] for item in measurements + candidate['warm'][name]]
            assert len(set(values)) == 1, (name, candidate['mode'], key)


def repeated_embedded(args):
    """Memoize exact successful OCR outputs only within one PDF read."""
    with tempfile.TemporaryDirectory(prefix='synthetic-pdf-', dir=Path(__file__).parent) as directory:
        root = Path(directory)
        scan = fixtures(root)[-1]
        mixed = root / 'mixed.pdf'
        make_mixed_pdf(scan, mixed)
        repeated = root / 'repeated.pdf'
        writer = PdfWriter()
        writer.append(mixed, pages=[0] * 6)
        writer.write(repeated)
        writer.close()
        timings = {}
        recognize_embedded = extraction.recognize_embedded
        memo = {}

        def memoize(data):
            # Prototype is scoped to one synthetic PDF. A product cache also needs explicit size bounds.
            if data not in memo:
                result = recognize_embedded(data)
                if result.unavailable or not result.text.strip():
                    return result
                memo[data] = result
            return memo[data]

        benchmark = Benchmark('baseline', args.vendor, args.tesseract)
        with patch.object(extraction, 'resource_path', side_effect=benchmark.resource_path), \
             patch.object(extraction, 'tesseract_page', side_effect=benchmark.tesseract):
            extraction.extract(mixed)  # Warm model for a fair pair of timings.
            start = time.perf_counter()
            baseline = extraction.extract(repeated)
            timings['baseline_seconds'] = time.perf_counter() - start
            with patch.object(extraction, 'recognize_embedded', side_effect=memoize):
                start = time.perf_counter()
                candidate = extraction.extract(repeated)
                timings['candidate_seconds'] = time.perf_counter() - start
        assert baseline == candidate
        assert analysis.analyze(baseline, repeated.name) == analysis.analyze(candidate, repeated.name)
        assert len(memo) == 1 and baseline['ocr_pages'] == list(range(7, 13))
        return {**timings, 'embedded_occurrences': 6, 'unique_images': len(memo),
                'extraction_and_analysis_identical': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor', type=Path, default=REPO / 'vendor')
    parser.add_argument('--tesseract', type=Path)
    parser.add_argument('--worker', choices=('baseline', 'overlap', 'overlap_pipe'))
    parser.add_argument('--repeats', default=2, type=int)
    parser.add_argument('--modes', nargs='+', choices=('baseline', 'overlap', 'overlap_pipe'),
                        default=['baseline', 'overlap', 'overlap_pipe'])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--skip-embedded', action='store_true')
    args = parser.parse_args()
    args.vendor = args.vendor.resolve()
    executable_name = 'tesseract.exe' if os.name == 'nt' else 'tesseract'
    args.tesseract = (args.tesseract or args.vendor / 'tesseract' / executable_name).resolve()
    if not args.tesseract.is_file():
        parser.error('Tesseract executable missing. Specify --tesseract.')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    if not args.output or args.modes[0] != 'baseline' or args.repeats < 1:
        parser.error('Provide --output, start with baseline and use at least one warm repeat.')
    check_weights(args.vendor)
    results = {'platform': platform.platform(), 'python': platform.python_version(),
               'dependencies': {name: metadata.version(name) for name in
                                ['rapidocr', 'onnxruntime', 'opencv-python-headless', 'Pillow', 'pypdfium2']},
               'tesseract': subprocess.check_output([str(args.tesseract), '--version'],
                                                    text=True).splitlines()[0],
               'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
               'warm_repeats': args.repeats, 'runs': []}
    for mode in args.modes:
        result = json.loads(subprocess.check_output([sys.executable, __file__, '--worker', mode,
                                                     '--repeats', str(args.repeats),
                                                     '--vendor', str(args.vendor),
                                                     '--tesseract', str(args.tesseract)]))
        compare(results['runs'][0] if results['runs'] else result, result)
        results['runs'].append(result)
        case_medians = {name: round(statistics.median(item['seconds'] for item in case), 4)
                        for name, case in result['warm'].items()}
        print(mode, 'cold', round(result['cold']['seconds'], 4), 'warm median by case', case_medians,
              'warm suite', round(sum(case_medians.values()), 4),
              'self peak MiB', result['self_peak_rss_mib'],
              'child rusage peak MiB', result['children_reported_peak_rss_mib'], flush=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    if not args.skip_embedded:
        results['repeated_embedded'] = repeated_embedded(args)
        print('Repeated embedded image:', results['repeated_embedded'], flush=True)
    results['complete_page_extraction_and_analysis_match'] = True
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
