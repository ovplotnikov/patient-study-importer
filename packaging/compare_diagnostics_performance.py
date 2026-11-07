#!/usr/bin/env python3
"""Compare production diagnostics batching using only synthetic local counters."""
import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch


SCRIPT = Path(__file__).resolve()
CURRENT = SCRIPT.parents[1]
TEST_ENDPOINT = 'https://diagnostics.example.org/v1/counters'
TEST_VERSION = '2.4.0'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def measured_call(calls, owner, name, key):
    original = getattr(owner, name)

    def wrapped(*args, **kwargs):
        calls[key] += 1
        return original(*args, **kwargs)

    return patch.object(owner, name, wrapped)


def measure(variant, attempts):
    from patient_importer import storage, telemetry

    require(Path(storage.__file__).resolve().parent == (Path.cwd() / 'patient_importer').resolve(),
            'Worker imported the wrong checkout')
    with tempfile.TemporaryDirectory(prefix='psi-diag-perf-') as temporary:
        root = Path(temporary)
        diagnostics = telemetry.Diagnostics(root / 'diagnostics', TEST_ENDPOINT, app_version=TEST_VERSION)
        require(diagnostics.set_consent(True) and diagnostics.snapshot() is None, 'Could not initialize consent')
        workspace = storage.Workspace(root / 'synthetic', diagnostics=diagnostics)
        if variant == 'baseline':
            capture, consent_key, write_method = workspace._diagnostics_enabled, 'collect', 'record'
        else:
            capture, consent_key, write_method = workspace._diagnostic_epoch, 'epoch', 'record_batch'
        calls = Counter(dict.fromkeys(('record_calls', 'state_reads', 'state_writes', 'fsync_calls',
                                       'atomic_replaces'), 0))
        with ExitStack() as stack:
            transport = stack.enter_context(patch.object(
                telemetry, 'post_report', side_effect=AssertionError('Network is forbidden in this benchmark')))
            for owner, name, key in ((diagnostics, write_method, 'record_calls'),
                                     (diagnostics, '_load', 'state_reads'),
                                     (diagnostics, '_write', 'state_writes'),
                                     (telemetry.os, 'fsync', 'fsync_calls'),
                                     (telemetry.os, 'replace', 'atomic_replaces')):
                stack.enter_context(measured_call(calls, owner, name, key))
            started = perf_counter()
            with workspace._diagnostic_batch() as batch:
                for _ in range(attempts):
                    batch.append({'format': 'pdf', 'outcome': 'saved', consent_key: capture()})
                captured = perf_counter()
            finished = perf_counter()
            transport.assert_not_called()

        expected = {'schema_version': 1, 'app_version': TEST_VERSION,
                    'counters': {'formats': {'pdf': attempts}, 'outcomes': {'saved': attempts}}}
        snapshot = diagnostics.snapshot()
        require(snapshot == expected, 'Diagnostics payload differs from the expected wire contract')
        writes = attempts * 2 if variant == 'baseline' else 1
        require(calls['record_calls'] == calls['state_writes'] == calls['atomic_replaces'] == writes,
                'Unexpected diagnostics state write count')
        require(calls['state_reads'] == attempts + writes and calls['fsync_calls'] >= writes,
                'Unexpected diagnostics reads or disk flush count')
        fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode('ascii')).hexdigest()
    return {'seconds': {'consent_checks': captured - started, 'record_counters': finished - captured,
                        'total': finished - started},
            'counts': dict(calls), 'payload_sha256': fingerprint}


def compare(args):
    repos = {'baseline': args.baseline.resolve(), 'current': CURRENT}
    require(repos['baseline'] != CURRENT and (repos['baseline'] / 'patient_importer').is_dir(),
            'Use a separate v2.4.0 checkout as --baseline')
    require(args.repeats > 0 and all(1 <= count <= 100000 for count in args.counts),
            'Use positive repeats and counts within the diagnostics schema limit')
    results = []
    for count in args.counts:
        samples = {variant: [] for variant in repos}
        for repeat in range(args.repeats):
            order = repos if repeat % 2 == 0 else reversed(repos)
            for variant in order:
                repo = repos[variant]
                run = subprocess.run([sys.executable, str(SCRIPT), '--worker', variant, str(count)],
                                     cwd=repo, capture_output=True, encoding='utf-8', timeout=300,
                                     env={**os.environ, 'PYTHONPATH': str(repo), 'PYTHONUTF8': '1'})
                require(run.returncode == 0, f'{variant} diagnostics worker failed:\n{run.stderr}')
                samples[variant].append(json.loads(run.stdout))
        hashes = {row['payload_sha256'] for runs in samples.values() for row in runs}
        require(len(hashes) == 1, f'Diagnostics payload mismatch at {count} attempts')
        medians = {variant: median(row['seconds']['total'] for row in runs) for variant, runs in samples.items()}
        speedup = medians['baseline'] / medians['current']
        results.append({'attempts': count, 'median_seconds': medians, 'speedup': speedup,
                        'payload_parity': True, 'payload_sha256': hashes.pop(), 'samples': samples})
        print(f'Diagnostics {count}: {medians["baseline"]:.4f}s -> {medians["current"]:.4f}s ({speedup:.2f}x), '
              f'{count * 2} -> 1 state writes, payload parity verified', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'schema_version': 1, 'network_requests': 0, 'repeats': args.repeats,
                                       'results': results}, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--worker']:
        _, _, variant, attempts = sys.argv
        print(json.dumps(measure(variant, int(attempts))))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--baseline', required=True, type=Path)
        parser.add_argument('--output', required=True, type=Path)
        parser.add_argument('--counts', type=int, nargs='+', default=[100, 1000])
        parser.add_argument('--repeats', type=int, default=3)
        compare(parser.parse_args())
