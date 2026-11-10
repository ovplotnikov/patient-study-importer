"""Compare two production source trees on generated OCR documents and shared weights."""

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch


SCRIPT = Path(__file__).resolve()
CASES = ('clean.png', 'rotation.png', 'small_type.png', 'skew.png', 'scan.pdf', 'repeated.pdf')
WARM_REPEATS = 3


def fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def create_fixtures(root):
    from PIL import Image
    from pypdf import PdfWriter
    from benchmark_ocr import synthetic_form
    from patient_importer.smoke import make_mixed_pdf

    if os.name == 'nt':
        fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        arial, verdana = 'arial.ttf', 'verdana.ttf'
    else:
        fonts = Path('/System/Library/Fonts/Supplemental')
        arial, verdana = 'Arial.ttf', 'Verdana.ttf'
    form = synthetic_form(str(fonts / arial), 0, False)[0]
    other = synthetic_form(str(fonts / verdana), 2, True)[0]
    small = Image.new('RGB', other.size, 'white')
    small.paste(other.resize((850, 540), Image.Resampling.LANCZOS), (120, 80))
    rotation = form.transpose(Image.Transpose.ROTATE_90)
    skew = other.rotate(3.5, resample=Image.Resampling.BICUBIC, fillcolor='white', expand=True)
    for name, image in zip(CASES[:4], (form, rotation, small, skew)):
        image.save(root / name)
    scan = root / 'scan.pdf'
    form.save(scan, 'PDF', resolution=180)
    mixed = root / 'mixed.pdf'
    make_mixed_pdf(scan, mixed)
    with closing(PdfWriter()) as writer:
        writer.append(mixed, pages=[0] * 6)
        writer.write(root / 'repeated.pdf')


def check_weights(vendor, executable):
    from ocr_models import RAPID_ONNX, TESS_FAST

    models = {vendor / 'rapidocr' / Path(name).name: expected for name, expected in RAPID_ONNX.items()}
    # Production Tesseract uses the tessdata folder next to its executable.
    models.update({executable.parent / 'tessdata' / name: expected for name, expected in TESS_FAST.items()})
    for path, expected in models.items():
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Missing or mismatched release OCR weight: {path.name}')
    return {path.name: expected for path, expected in models.items()}


def peak_rss():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak_working_set', 'working_set', 'peak_paged_pool',
                                                    'paged_pool', 'peak_nonpaged_pool', 'nonpaged_pool',
                                                    'pagefile', 'peak_pagefile')]

        counters = ProcessMemoryCounters()
        counters.size = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.argtypes = ()
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        memory = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
        memory.restype = wintypes.BOOL
        if not memory(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.size):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'worker_peak_mib': round(counters.peak_working_set / (1 << 20), 2),
                'tesseract_child_peak_mib': None}
    import resource

    scale = 1 << (20 if sys.platform == 'darwin' else 10)
    return {'worker_peak_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 2),
            'tesseract_child_peak_mib': round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / scale, 2)}


def read_page(extraction, path):
    """Check Page parity outside the timed runs, using the same input raster."""
    from PIL import Image

    if path.suffix == '.png':
        with Image.open(path) as image:
            return extraction.recognize_image(image)
    if path.name == 'repeated.pdf':
        from pypdf import PdfReader

        return extraction.recognize_embedded(PdfReader(path).pages[0].images[0].data)
    import pypdfium2 as pdfium

    with closing(pdfium.PdfDocument(path)) as document, closing(document[0]) as page:
        width, height = page.get_size()
        scale = min(extraction.PDF_RENDER_SCALE, (extraction.MAX_RENDER_PIXELS / (width * height)) ** .5)
        with closing(page.render(scale=scale)) as bitmap:
            image = bitmap.to_pil().copy()
    return extraction.recognize_image(image)


def worker(vendor, executable, fixtures):
    from patient_importer import __version__, analysis, extraction

    if Path(extraction.__file__).resolve().parents[1] != Path.cwd().resolve():
        raise RuntimeError('Worker imported the wrong checkout')

    def resources(name):
        if name in {'tesseract/tesseract', 'tesseract/tesseract.exe'}:
            return executable
        return vendor / name

    def measure(name):
        started = perf_counter()
        data = extraction.extract(fixtures / name)
        extracted = perf_counter()
        parsed = analysis.analyze(data, name)
        finished = perf_counter()
        expected_pages = 6 if name == 'repeated.pdf' else 1
        evidence = data['ocr_evidence']
        if (len(data['ocr_pages']) != expected_pages or not evidence or
                any(item['verification_engine'] != 'tesseract-fast' for item in evidence) or
                parsed['quality']['confirmed']):
            raise RuntimeError(f'Expected complete dual OCR with manual review: {name}')
        return {'seconds': finished - started, 'extraction_seconds': extracted - started,
                'analysis_seconds': finished - extracted, 'ocr_pages': expected_pages,
                'extracted': fingerprint(data), 'analysis': fingerprint(parsed),
                'quality': fingerprint(parsed['quality'])}

    with patch.object(extraction, 'resource_path', side_effect=resources):
        cold = measure(CASES[0])
        cold_memory = peak_rss()
        warm = {name: [measure(name)] for name in CASES}
        for offset in range(1, WARM_REPEATS):
            for name in CASES[offset:] + CASES[:offset]:
                warm[name].append(measure(name))
        warm_memory = peak_rss()
        pages = {}
        for name in CASES:
            page = read_page(extraction, fixtures / name)
            if page.unavailable or page.verification_engine != 'tesseract-fast':
                raise RuntimeError(f'Expected a complete two-engine Page result: {name}')
            pages[name] = fingerprint(asdict(page))
    return {'version': __version__, 'cold': cold, 'cold_memory': cold_memory,
            'warm': warm, 'warm_memory': warm_memory, 'pages': pages}


def run_checkout(checkout, vendor, executable, fixtures):
    command = [sys.executable, str(SCRIPT), '--worker', str(fixtures),
               '--vendor', str(vendor), '--tesseract', str(executable)]
    process = subprocess.run(command, cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout), 'PYTHONUTF8': '1'},
                             capture_output=True, encoding='utf-8')
    if process.returncode:
        raise RuntimeError(f'OCR worker failed:\n{process.stderr[-3000:]}')
    return json.loads(process.stdout)


def differences(baseline, current):
    """OCR output must match the baseline. Parsing rules may change, but must stay repeatable."""
    mismatches = []
    if baseline['cold']['extracted'] != current['cold']['extracted']:
        mismatches.append('cold/extracted')
    for name in CASES:
        if baseline['pages'][name] != current['pages'][name]:
            mismatches.append(f'{name}/Page')
        if len({run['extracted'] for run in baseline['warm'][name] + current['warm'][name]}) != 1:
            mismatches.append(f'{name}/warm/extracted')
        for label, result in (('baseline', baseline), ('current', current)):
            for key in ('analysis', 'quality'):
                if len({run[key] for run in result['warm'][name]}) != 1:
                    mismatches.append(f'{name}/warm/{key}/{label}')
    return mismatches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, help='Checkout of 2.4.0')
    parser.add_argument('--vendor', required=True, type=Path)
    parser.add_argument('--tesseract', required=True, type=Path, help='Runtime with the pinned tessdata next to it')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    vendor, executable = args.vendor.resolve(strict=True), args.tesseract.resolve(strict=True)
    if args.worker:
        print(json.dumps(worker(vendor, executable, args.worker)))
        return
    if not args.baseline or not args.output:
        parser.error('--baseline and --output are required')
    baseline_path = args.baseline.resolve(strict=True)
    current_path = SCRIPT.parents[1]
    weights = check_weights(vendor, executable)
    with tempfile.TemporaryDirectory(prefix='patient-ocr-comparison-') as directory:
        fixtures = Path(directory)
        create_fixtures(fixtures)
        baseline = run_checkout(baseline_path, vendor, executable, fixtures)
        current = run_checkout(current_path, vendor, executable, fixtures)
    mismatches = differences(baseline, current)
    medians = {name: {label: statistics.median(run['seconds'] for run in result['warm'][name])
                      for label, result in [('baseline', baseline), ('current', current)]}
               for name in CASES}
    report = {'synthetic': True, 'platform': platform.platform(), 'python': platform.python_version(),
              'tesseract': subprocess.check_output([str(executable), '--version'],
                                                  encoding='utf-8').splitlines()[0],
              'weights_sha256': weights, 'warm_repeats': WARM_REPEATS,
              'cold_scope': 'First OCR in a fresh Python process, including model load. OS caches retained.',
              'memory_scope': 'Process peaks reported separately. No Tesseract child peak on Windows.',
              'baseline': baseline, 'current': current, 'warm_median_seconds': medians,
              'full_result_parity': not mismatches, 'mismatches': mismatches,
              'parity_scope': 'OCR text and pages against the baseline. Analysis and quality only between repeats.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'full_result_parity': not mismatches, 'warm_median_seconds': medians}))
    if mismatches:
        raise SystemExit(f'OCR output differs: {", ".join(mismatches)}')


if __name__ == '__main__':
    main()
