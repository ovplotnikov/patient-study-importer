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
        from tkinter import messagebox
        def fail_dialog(title,message,**kwargs):raise RuntimeError(title+': '+message)
        messagebox.showerror=fail_dialog
        from patient_importer.gui import Application
        application=Application()
        application.select(args.ui_smoke_test/'gui-workspace')
        application.root.update()
        from tkinter import ttk
        def verify_buttons(widget,window):
            if isinstance(widget,ttk.Button):
                assert widget.winfo_ismapped() and widget.winfo_height()>=widget.winfo_reqheight(),widget.cget('text')
                assert widget.winfo_rooty()+widget.winfo_height()<=window.winfo_rooty()+window.winfo_height(),widget.cget('text')
            for child in widget.winfo_children():verify_buttons(child,window)
        verify_buttons(application.root,application.root)
        assert application.root.winfo_exists()
        assert str(application.start_button['state'])=='normal'
        from patient_importer.smoke import SAMPLE
        import time
        (application.workspace.root/'Входящие'/'gui-test.txt').write_text(SAMPLE,encoding='utf-8')
        application.start_button.invoke()
        deadline=time.monotonic()+30
        while application.worker.is_alive() or not application.events.empty():
            application.root.update();time.sleep(.05)
            assert time.monotonic()<deadline,'GUI import timed out'
        assert application.workspace.summary()['documents']==1
        assert 'Готово' in application.phase.get(),application.phase.get()
        try:
            from PIL import ImageGrab
            application.root.update()
            x,y=application.root.winfo_rootx(),application.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+application.root.winfo_width(),y+application.root.winfo_height())).save(args.ui_smoke_test/'ui.png')
        except Exception:pass
        (application.workspace.root/'Входящие'/'без ФИО.txt').write_text('Дата рождения: 01.01.2000\nЗаключение: тестовый текст.',encoding='utf-8')
        application.start_button.invoke()
        deadline=time.monotonic()+30
        while application.worker.is_alive() or not application.events.empty():
            application.root.update();time.sleep(.05)
            assert time.monotonic()<deadline,'Second GUI import timed out'
        application.review_button.invoke();application.root.update()
        review=application.review_window
        verify_buttons(review.window,review.window)
        assert review.selected
        try:
            x,y=review.window.winfo_rootx(),review.window.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+review.window.winfo_width(),y+review.window.winfo_height())).save(args.ui_smoke_test/'review-ui.png')
        except Exception:pass
        review.name.set('ТЕСТОВ ТЕСТ ТЕСТОВИЧ');review.birth.set('01.01.2000');review.confirm_button.invoke();application.root.update()
        assert application.workspace.summary()['review']==0
        review.window.destroy()
        application.root.destroy()
        (args.ui_smoke_test/'ui-test.json').write_text(json.dumps({'ok':True,'checks':['window_creation','folder_selection','process_button','background_import','completion_state','review_window','confirmation_button','buttons_fully_visible']}),encoding='utf-8')
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
