"""Compare the real Windows Tk focus refresh on two source checkouts."""

import argparse
import json
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch


def descendants(parent):
    children = list(parent.winfo_children())
    for child in children:
        yield child
        yield from descendants(child)


def run_case(count, repeat):
    from tkinter import ttk

    from patient_importer import __version__, diagnostics_config, gui

    assert Path(gui.__file__).resolve().parents[1] == Path.cwd().resolve()
    application = gui.Application
    select = application.select
    show_documents = application.show_documents
    initial = {}
    callback_errors = []

    def measure(app, action):
        summary = app.workspace.summary
        summary_calls = 0

        def counted(*args, **kwargs):
            nonlocal summary_calls
            # A public summary() call can recurse with an explicit DB connection.
            if not args and not kwargs:
                summary_calls += 1
            return summary(*args, **kwargs)

        with patch.object(app.workspace, 'summary', counted):
            started = perf_counter()
            action()
            app.root.update_idletasks()
            elapsed = perf_counter() - started
        assert not callback_errors, callback_errors
        return {'milliseconds': round(elapsed * 1000, 3), 'summary_calls': summary_calls}

    def first_show(app):
        app.root.withdraw()
        app.root.report_callback_exception = lambda typ, _value, _traceback: callback_errors.append(typ.__name__)
        initial.update(measure(app, lambda:show_documents(app)))

    def fail_dialog(*args, **kwargs):
        raise AssertionError('Queue benchmark opened an error dialog')

    with tempfile.TemporaryDirectory(prefix='patient-compare-tk-') as temp:
        root = Path(temp).resolve()
        workspace = root / 'synthetic'
        incoming = workspace / 'Входящие'
        incoming.mkdir(parents=True)
        names = {f'Учебный документ {index:05d}.txt' for index in range(count)}
        for name in sorted(names):
            (incoming / name).write_text('Синтетический текст для измерения очереди.', encoding='utf-8')
        appdata = root / 'settings'
        settings = appdata / 'PatientStudyImporter'
        settings.mkdir(parents=True)
        (settings / 'settings.json').write_text(json.dumps({'workspace': str(workspace)}), encoding='utf-8')
        env = {
            'APPDATA': str(appdata), 'LOCALAPPDATA': str(appdata),
            'PATIENT_IMPORTER_NO_UPDATE_CHECK': '1', 'PATIENT_IMPORTER_NO_DIAGNOSTICS_PROMPT': '1',
        }

        def select_synthetic(app, path):
            assert Path(path).resolve() == workspace, 'Only the temporary synthetic workspace is allowed'
            select(app, path)

        with (
            patch.dict(os.environ, env),
            patch.object(diagnostics_config, 'TELEMETRY_URL', ''),
            patch.object(application, 'select', select_synthetic),
            patch.object(gui.messagebox, 'showerror', side_effect=fail_dialog),
        ):
            app = application.__new__(application)
            try:
                with patch.object(application, 'show_documents', first_show):
                    app.__init__()
                assert app.root.state() == 'withdrawn'
                assert {path.name for path in app.paths} == names
                tk_version = app.root.tk.call('info', 'patchlevel')
                body = app.content.body
                visible = min(count, getattr(gui, 'DOCUMENTS_PER_PAGE', count))
                children = body.winfo_children()
                assert len(children) == 2 * visible
                initial['rows'] = visible
                initial['widgets'] = sum(1 for _ in descendants(body))
                samples = []
                for _ in range(repeat):
                    before = set(descendants(body))
                    event = SimpleNamespace(widget=app.root)
                    sample = measure(app, lambda:app.focus_refresh(event))
                    after = set(descendants(body))
                    assert len(body.winfo_children()) == 2 * visible
                    assert sample['summary_calls'] > 0, 'Focus refresh did not run'
                    sample['rows'] = visible
                    sample['widgets'] = len(after)
                    sample['retained_widgets'] = len(before & after)
                    samples.append(sample)

                def visible_names():
                    return {str(widget.cget('text')) for widget in descendants(body)
                            if isinstance(widget, ttk.Label) and str(widget.cget('text')) in names}

                reachable = visible_names()
                next_page = getattr(app, 'page_next', None)
                if next_page is not None:
                    for _ in range(1, (count + visible - 1) // visible):
                        assert str(next_page.cget('state')) == 'normal'
                        next_page.invoke()
                        app.root.update_idletasks()
                        reachable.update(visible_names())
                assert reachable == names, 'Some synthetic files are unreachable in the visible list'
                assert not callback_errors, callback_errors
            finally:
                if hasattr(app, 'root'):
                    app.root.destroy()
    return {
        'version': __version__, 'queued_files': count, 'tk': tk_version, 'initial': initial,
        'focus_refresh_samples': samples,
        'focus_refresh_median_ms': round(median(sample['milliseconds'] for sample in samples), 3),
        'all_files_reachable': True,
    }


def compare(baseline, current, counts, repeat):
    results = {'baseline': [], 'current': []}
    script = Path(__file__).resolve()
    checkouts = [('baseline', baseline), ('current', current)]
    for index, count in enumerate(counts):
        order = checkouts if index % 2 == 0 else reversed(checkouts)
        for label, checkout in order:
            result = subprocess.run(
                [sys.executable, str(script), '--case', str(count), '--repeat', str(repeat)],
                cwd=checkout, env={**os.environ, 'PYTHONPATH': str(checkout), 'PYTHONUTF8': '1'},
                capture_output=True, encoding='utf-8', timeout=120,
            )
            if result.returncode:
                raise RuntimeError(f'{label} Tk worker failed for {count} files:\n{result.stderr}')
            row = json.loads(result.stdout)
            results[label].append(row)
            print(label, count, row['focus_refresh_median_ms'], 'ms', flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--counts', type=int, nargs='+', default=[10, 100, 1000])
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--case', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This benchmark requires Windows Tk')
    if min(args.counts) <= 0 or args.repeat <= 0 or (args.case is not None and args.case <= 0):
        parser.error('counts and repeat must be positive')
    if args.case is not None:
        print(json.dumps(run_case(args.case, args.repeat)))
        return
    if args.baseline is None or args.output is None:
        parser.error('Specify --baseline and --output')
    baseline = args.baseline.resolve(strict=True)
    current = Path(__file__).resolve().parents[1]
    if baseline == current or not (baseline / 'patient_importer' / 'gui.py').is_file():
        parser.error('--baseline must point to a separate baseline checkout')
    results = compare(baseline, current, args.counts, args.repeat)
    output = {
        'platform': platform.platform(), 'python': platform.python_version(), 'window_withdrawn': True,
        'synthetic': True, 'repeat': args.repeat, **results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
