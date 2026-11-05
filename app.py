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
    args=parser.parse_args()
    if args.ui_smoke_test:
        args.ui_smoke_test.mkdir(parents=True,exist_ok=True)
        from patient_importer.gui import Application
        application=Application()
        application.select(args.ui_smoke_test/'gui-workspace')
        application.root.update()
        assert application.root.winfo_exists()
        assert str(application.start_button['state'])=='normal'
        application.root.after(500,application.root.destroy)
        application.run()
        (args.ui_smoke_test/'ui-test.json').write_text(json.dumps({'ok':True,'window':'Исследования пациентов'}),encoding='utf-8')
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
    try:main()
    except Exception as exc:
        if len(sys.argv)>1:raise
        import tkinter as tk
        from tkinter import messagebox
        root=tk.Tk();root.withdraw();messagebox.showerror('Исследования пациентов',str(exc));root.destroy()
        raise SystemExit(1)
