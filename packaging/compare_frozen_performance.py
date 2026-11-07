"""Measure the existing offline, hidden-Tk startup probe in two Windows EXEs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
import tempfile
from time import perf_counter, sleep

from windows_ocr_runtime import archive_path, file_digest, TESSERACT_DIR


def read_marker(path):
    try:
        version = json.loads(path.read_text(encoding='utf-8'))['version']
    except (OSError, ValueError, KeyError, TypeError):
        return None  # A poll can see the marker while the process is writing it.
    return version if isinstance(version, str) and version else None


def probe(executable, directory, timeout=120):
    directory.mkdir()
    marker = directory / 'ready.json'
    env = os.environ.copy()
    env.update(APPDATA=str(directory / 'appdata'), LOCALAPPDATA=str(directory / 'localappdata'),
               PATIENT_IMPORTER_NO_UPDATE_CHECK='1', PATIENT_IMPORTER_NO_DIAGNOSTICS_PROMPT='1')
    start = perf_counter()
    process = subprocess.Popen([str(executable), '--update-probe', str(marker)], cwd=directory, env=env)
    ready_at = None
    try:
        while perf_counter() - start < timeout:
            version = read_marker(marker)
            if version is not None:
                ready_at = perf_counter()
                break
            if process.poll() is not None:
                break
            sleep(0.005)
        code = process.wait(timeout=max(0.01, timeout - (perf_counter() - start)))
        finished = perf_counter()
        if ready_at is None:
            version = read_marker(marker)
            ready_at = finished if version is not None else None
        if code != 0 or ready_at is None:
            raise RuntimeError(f'Startup probe failed (exit={code}, marker={ready_at is not None})')
        return {'version': version, 'ready_ms': round((ready_at - start) * 1000, 3),
                'exit_ms': round((finished - start) * 1000, 3)}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def archive_report(executable):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    entries = {archive_path(name): name for name in archive.toc}
    duplicates = []
    for path, name in entries.items():
        dll = path.removeprefix(TESSERACT_DIR)
        if path == dll or '/' in dll or not dll.endswith('.dll') or dll not in entries:
            continue
        root = entries[dll]
        if (archive.toc[name][2] == archive.toc[root][2]
                and hashlib.sha256(archive.extract(name)).digest() == hashlib.sha256(archive.extract(root)).digest()):
            duplicates.append(dll)
    return {'exe_bytes': executable.stat().st_size, 'sha256': file_digest(executable),
            'expanded_binary_data_bytes': sum(e[2] for e in archive.toc.values() if e[4] in ('b', 'x')),
            'identical_root_tesseract_dlls': sorted(duplicates)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-exe', type=Path, required=True)
    parser.add_argument('--candidate-exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Frozen startup measurement requires Windows')
    exes = {'baseline': args.baseline_exe.resolve(strict=True),
            'candidate': args.candidate_exe.resolve(strict=True)}
    samples = {name: [] for name in exes}
    with tempfile.TemporaryDirectory(prefix='patient-frozen-startup-') as temp:
        for index in range(4):
            order = exes if index % 2 == 0 else reversed(exes)
            for name in order:
                samples[name].append(probe(exes[name], Path(temp) / f'{name}-{index}'))
    if any(sample['version'] != '2.4.0' for sample in samples['baseline']):
        raise RuntimeError('Baseline is not the published application version 2.4.0')
    report = {'platform': platform.platform(), 'python': platform.python_version(),
              'probe': '--update-probe', 'ready_marker': 'GUI imported and hidden Tk created and destroyed',
              'file_cache_reset': False, 'order': 'alternate baseline/candidate across rounds'}
    # Inspect and hash only after timing to avoid pre-reading the EXEs here.
    for name, executable in exes.items():
        first, *repeats = samples[name]
        report[name] = {'first_process': first, 'repeats': repeats,
                        'median_repeat_ready_ms': median(s['ready_ms'] for s in repeats),
                        'median_repeat_exit_ms': median(s['exit_ms'] for s in repeats),
                        'archive': archive_report(executable)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
