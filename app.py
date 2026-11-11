import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--import-folder',type=Path)
    parser.add_argument('--self-test',type=Path)
    parser.add_argument('--ui-smoke-test',type=Path)
    parser.add_argument('--apply-update',type=Path)
    parser.add_argument('--update-probe',type=Path)
    parser.add_argument('--update-lock-probe',type=Path)
    args=parser.parse_args()
    if args.apply_update:
        from patient_importer.updater import run_helper
        run_helper(args.apply_update)
        return
    if args.update_probe:
        from patient_importer import __version__
        from patient_importer.gui import Application
        import tkinter as tk
        window=tk.Tk();window.withdraw();window.update_idletasks();window.destroy()
        args.update_probe.write_text(json.dumps({'version':__version__}),encoding='utf8')
        return
    if args.update_lock_probe:
        import time
        args.update_lock_probe.mkdir(parents=True,exist_ok=True)
        (args.update_lock_probe/'ready').write_text('ready')
        deadline=time.monotonic()+90
        while not (args.update_lock_probe/'exit').exists() and time.monotonic()<deadline:time.sleep(.1)
        return
    if args.ui_smoke_test:
        from patient_importer.ui_smoke import run
        run(args.ui_smoke_test)
        return
    if args.self_test:
        from patient_importer.smoke import smoke_test
        result=smoke_test(args.self_test)
        (args.self_test/'self-test.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return
    if args.import_folder:
        from patient_importer.storage import Workspace
        print(json.dumps(Workspace(args.import_folder).import_folder(),ensure_ascii=False))
        return
    from patient_importer.gui import Application
    Application().run()


if __name__=='__main__':
    # Reading runs in worker processes. In the packaged program they start this same
    # executable, which has to become a worker instead of opening a second window.
    import multiprocessing
    multiprocessing.freeze_support()
    try:main()
    except Exception as exc:
        if any(flag in sys.argv for flag in ['--apply-update','--update-probe','--update-lock-probe']):
            raise SystemExit(1)
        if len(sys.argv)>1:
            if getattr(sys,'frozen',False):
                import traceback
                target=Path(sys.argv[-1]);target.mkdir(parents=True,exist_ok=True)
                (target/'failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
                raise SystemExit(1)
            raise
        import tkinter as tk
        from tkinter import messagebox
        root=tk.Tk();root.withdraw();messagebox.showerror('Исследования пациентов',str(exc));root.destroy()
        raise SystemExit(1)
