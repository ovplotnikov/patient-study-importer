"""Inspect a published PyInstaller bundle and time its hidden-Tk startup probe.

Archive inspection works on macOS and Windows. EXE timing requires Windows.
The first measured process is identified separately; OS file caches are retained.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter, sleep


def archive_sizes(executable):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    entries = {name.replace('\\', '/').lower(): name for name in archive.toc}
    pairs = []
    for normal, vendor_name in entries.items():
        components = normal.split('/')
        if len(components) != 3 or components[:2] != ['vendor', 'tesseract']:
            continue
        dll = components[-1]
        if not dll.endswith('.dll') or dll not in entries:
            continue
        root_name = entries[dll]
        vendor = archive.toc[vendor_name]
        root = archive.toc[root_name]
        if vendor[2] != root[2] or vendor[-1] != 'b' or root[-1] != 'b':
            continue
        vendor_hash = sha256(archive.extract(vendor_name)).digest()
        root_hash = sha256(archive.extract(root_name)).digest()
        if vendor_hash == root_hash:
            pairs.append({
                'dll': dll, 'root_archive_bytes': root[1],
                'vendor_archive_bytes': vendor[1], 'expanded_bytes': vendor[2],
            })
    binaries = [entry for entry in archive.toc.values() if entry[-1] == 'b']
    return {
        'exe_bytes': executable.stat().st_size,
        'binary_and_data_entries': len(binaries),
        'binary_and_data_expanded_bytes': sum(entry[2] for entry in binaries),
        'duplicate_dll_pairs': len(pairs),
        'duplicate_root_archive_bytes': sum(pair['root_archive_bytes'] for pair in pairs),
        'duplicate_vendor_archive_bytes': sum(pair['vendor_archive_bytes'] for pair in pairs),
        'duplicate_expanded_bytes_one_copy': sum(pair['expanded_bytes'] for pair in pairs),
        'largest_duplicate_dlls': sorted(pairs, key=lambda pair: -pair['expanded_bytes'])[:10],
    }


def time_probe(command, workdir, repeat, timeout):
    samples = []
    with tempfile.TemporaryDirectory(prefix='patient-startup-') as temp:
        folder = Path(temp)
        env = os.environ.copy()
        env.update({
            'APPDATA': str(folder / 'settings'),
            'PATIENT_IMPORTER_NO_UPDATE_CHECK': '1',
            'PATIENT_IMPORTER_NO_DIAGNOSTICS_PROMPT': '1',
        })
        for index in range(repeat):
            ready = folder / f'ready-{index}.json'
            start = perf_counter()
            process = subprocess.Popen([*command, '--update-probe', str(ready)], cwd=workdir, env=env)
            ready_at = None
            version = None
            try:
                while perf_counter() - start < timeout:
                    if ready.is_file():
                        try:
                            version = json.loads(ready.read_text(encoding='utf-8'))['version']
                        except (OSError, ValueError, KeyError):
                            # The marker is written directly; a read can race with the write.
                            pass
                        else:
                            ready_at = perf_counter()
                            break
                    if process.poll() is not None:
                        break
                    sleep(0.005)
                code = process.wait(timeout=max(0.01, timeout - (perf_counter() - start)))
                finished = perf_counter()
                if code == 0 and ready_at is None and ready.is_file():
                    # The process can exit between the file check and poll().
                    version = json.loads(ready.read_text(encoding='utf-8'))['version']
                    ready_at = finished
                if code != 0 or ready_at is None:
                    raise RuntimeError(f'Probe failed (exit code {code}, ready={ready_at is not None})')
                samples.append({
                    'version': version, 'ready_ms': round((ready_at - start) * 1000, 3),
                    'exit_ms': round((finished - start) * 1000, 3),
                })
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    return {
        'first_process': samples[0],
        'median_ready_ms': round(median(sample['ready_ms'] for sample in samples), 3),
        'median_exit_ms': round(median(sample['exit_ms'] for sample in samples), 3),
        'samples': samples,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--source', type=Path, help='Also time a checkout\'s app.py using this Python')
    parser.add_argument('--archive-only', action='store_true')
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.repeat <= 0 or args.timeout <= 0:
        parser.error('repeat and timeout must be positive')
    if not args.archive_only and os.name != 'nt':
        parser.error('EXE startup measurement requires Windows; use --archive-only here')
    executable = args.exe.resolve(strict=True)
    report = {'platform': platform.platform(), 'python': platform.python_version()}
    if not args.archive_only:
        report['frozen_probe'] = time_probe([str(executable)], executable.parent, args.repeat, args.timeout)
        if args.source:
            entrypoint = args.source.resolve(strict=True)
            report['source_probe'] = time_probe(
                [sys.executable, str(entrypoint)], entrypoint.parent, args.repeat, args.timeout)
    report['archive'] = archive_sizes(executable)
    payload = json.dumps(report, indent=2) + '\n'
    print(payload, end='')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
