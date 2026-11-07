#!/usr/bin/env python3
"""Measure local diagnostics storage for two counters per synthetic document."""
import argparse
from collections import Counter
from contextlib import ExitStack
from functools import wraps
import hashlib
import json
import platform
from pathlib import Path
from statistics import median
import tempfile
from time import perf_counter
from unittest.mock import patch

from patient_importer import telemetry
from patient_importer.telemetry_schema import MAX_COUNT


TEST_ENDPOINT = 'https://diagnostics.example.org/v1/counters'
COUNTERS = (('formats', 'pdf'), ('outcomes', 'saved'))
VARIANTS = ('current', 'aggregate', 'off')


def run_case(directory, documents, variant):
    diagnostics = telemetry.Diagnostics(directory, endpoint=TEST_ENDPOINT)
    assert diagnostics.configured and diagnostics.set_consent(variant != 'off')
    state_path = directory / 'diagnostics.json'
    initial_state = state_path.read_bytes()
    assert diagnostics.snapshot() is None
    calls = Counter({name: 0 for name in ('record_calls', 'state_reads', 'state_writes', 'fsync_calls', 'atomic_replaces')})

    def track(owner, name, counter):
        function = getattr(owner, name)

        @wraps(function)
        def counted(*args, **kwargs):
            calls[counter] += 1
            return function(*args, **kwargs)

        return patch.object(owner, name, counted)

    with ExitStack() as stack:
        stack.enter_context(track(diagnostics, 'record', 'record_calls'))
        stack.enter_context(track(diagnostics, '_load', 'state_reads'))
        stack.enter_context(track(diagnostics, '_write', 'state_writes'))
        stack.enter_context(track(telemetry.os, 'fsync', 'fsync_calls'))
        stack.enter_context(track(telemetry.os, 'replace', 'atomic_replaces'))
        started = perf_counter()
        # The importer checks consent at the start of every document attempt.
        collect = [diagnostics.enabled for _ in range(documents)]
        checked = perf_counter()
        if variant == 'aggregate':
            count = sum(collect)
            if count:
                for metric, code in COUNTERS:
                    assert diagnostics.record(metric, code, count)
        else:
            for allowed in collect:
                if allowed:
                    for metric, code in COUNTERS:
                        assert diagnostics.record(metric, code)
        finished = perf_counter()

    snapshot = diagnostics.snapshot()
    state = state_path.read_bytes()
    writes = {'current': 2 * documents, 'aggregate': 2, 'off': 0}[variant]
    assert calls['record_calls'] == calls['state_writes'] == calls['atomic_replaces'] == writes
    assert calls['state_reads'] == documents + writes
    # Any platform-dependent flushes inside the file lock are counted as well.
    assert calls['fsync_calls'] >= writes
    if variant == 'off':
        assert not any(collect) and snapshot is None and state == initial_state
    else:
        assert all(collect)
        assert snapshot['counters'] == {'formats': {'pdf': documents}, 'outcomes': {'saved': documents}}
    return {'documents': documents, 'variant': variant,
            'seconds': {'consent_checks': checked - started, 'record_counters': finished - checked,
                        'total': finished - started},
            'counts': dict(calls), 'state_fsync_calls': writes, 'snapshot': snapshot,
            'state_bytes': len(state), 'state_sha256': hashlib.sha256(state).hexdigest()}


def main(args):
    if args.repeats < 1 or any(not 1 <= count <= MAX_COUNT for count in args.counts):
        raise ValueError('Counts and repeats must be positive; counters cannot exceed MAX_COUNT')
    rows = []
    summaries = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='diag-perf-') as folder, patch.object(
        telemetry, 'post_report', side_effect=AssertionError('Network is forbidden in this benchmark')
    ) as transport:
        root = Path(folder)
        for count in args.counts:
            snapshots = []
            states = set()
            results = {variant: [] for variant in VARIANTS}
            for repeat in range(args.repeats):
                offset = repeat % len(VARIANTS)
                for variant in VARIANTS[offset:] + VARIANTS[:offset]:
                    row = run_case(root / f'{count}-{repeat}-{variant}', count, variant)
                    row['repeat'] = repeat + 1
                    rows.append(row)
                    results[variant].append(row)
                    if variant != 'off':
                        snapshots.append(row['snapshot'])
                        states.add(row['state_sha256'])
                    print(json.dumps(row), flush=True)
            assert len(states) == 1 and all(snapshot == snapshots[0] for snapshot in snapshots)
            summaries.append({'documents': count, 'enabled_snapshot_and_state_equal': True,
                              'seconds': {variant: {key: median(row['seconds'][key] for row in runs)
                                                    for key in runs[0]['seconds']}
                                          for variant, runs in results.items()}})
        transport.assert_not_called()
    report = {'platform': platform.platform(), 'python': platform.python_version(), 'network_requests': 0,
              'summary': summaries, 'runs': rows}
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[100, 1000])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('diagnostics-results.json'))
    main(parser.parse_args())
