"""Measure the actual Tk queue callback on an isolated Windows desktop.

The withdrawn window measures Tk widget construction and idle layout work.
The resulting timings exclude painting the visible window and importing files.
"""
import argparse
import json
import os
from pathlib import Path
import platform
from statistics import median
import tempfile
from time import perf_counter
from unittest.mock import patch


def measure_queue(count, repeat):
    from patient_importer import diagnostics_config
    from patient_importer.gui import Application

    show_documents = Application.show_documents
    select = Application.select
    samples = []
    callback_errors = []

    def measure(app):
        start = perf_counter()
        show_documents(app)
        callback_done = perf_counter()
        app.root.update_idletasks()
        finished = perf_counter()
        samples.append({
            'callback_ms': round((callback_done - start) * 1000, 3),
            'idle_ms': round((finished - callback_done) * 1000, 3),
            'total_ms': round((finished - start) * 1000, 3),
        })
        assert len(app.paths) == count
        assert len(app.content.body.winfo_children()) == count * 2
        assert not callback_errors, callback_errors

    def first_show(app):
        app.root.withdraw()
        app.root.report_callback_exception = lambda typ, value, traceback: callback_errors.append(typ.__name__)
        measure(app)

    def fail_dialog(*args, **kwargs):
        raise AssertionError('Queue startup opened an error dialog')

    with tempfile.TemporaryDirectory(prefix='patient-tk-queue-') as temp:
        root = Path(temp).resolve()
        workspace = root / 'synthetic'
        incoming = workspace / 'Входящие'
        incoming.mkdir(parents=True)
        for index in range(count):
            (incoming / f'Учебный документ {index:05d}.txt').write_text(
                'Синтетический текст для замера очереди.', encoding='utf-8')
        appdata = root / 'settings'
        settings = appdata / 'PatientStudyImporter'
        settings.mkdir(parents=True)
        (settings / 'settings.json').write_text(json.dumps({'workspace': str(workspace)}), encoding='utf-8')
        environment = {
            'APPDATA': str(appdata), 'LOCALAPPDATA': str(appdata),
            'PATIENT_IMPORTER_NO_UPDATE_CHECK': '1', 'PATIENT_IMPORTER_NO_DIAGNOSTICS_PROMPT': '1',
        }

        def select_synthetic(app, path):
            if Path(path).resolve() != workspace.resolve():
                raise AssertionError('Queue benchmark must use its synthetic workspace')
            select(app, path)

        with (
            patch.dict(os.environ, environment),
            patch.object(diagnostics_config, 'TELEMETRY_URL', ''),
            patch.object(Application, 'select', select_synthetic),
            patch.object(Application, 'show_documents', first_show),
            patch('patient_importer.gui.messagebox.showerror', side_effect=fail_dialog),
        ):
            app = Application.__new__(Application)
            try:
                app.__init__()
                assert len(samples) == 1
                assert app.root.state() == 'withdrawn'
                tk_version = app.root.tk.call('info', 'patchlevel')
                for _ in range(repeat):
                    measure(app)
            finally:
                if hasattr(app, 'root'):
                    app.root.destroy()
    refreshes = samples[1:]
    return {
        'queued_files': count, 'tk': tk_version, 'initial': samples[0],
        'unchanged_refresh_samples': refreshes,
        'unchanged_refresh_median_ms': round(median(sample['total_ms'] for sample in refreshes), 3),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[10, 100, 1000])
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This benchmark requires Windows Tk')
    if min(args.counts) <= 0 or args.repeat <= 0:
        parser.error('counts and repeat must be positive')
    result = {
        'platform': platform.platform(), 'python': platform.python_version(),
        'window_withdrawn': True, 'cases': [measure_queue(count, args.repeat) for count in args.counts],
    }
    payload = json.dumps(result, indent=2) + '\n'
    print(payload, end='')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
