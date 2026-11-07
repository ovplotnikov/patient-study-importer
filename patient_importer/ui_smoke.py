"""Real Tk flows with synthetic inputs, used in both source and frozen Windows CI."""
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


def run(root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['PATIENT_IMPORTER_NO_UPDATE_CHECK']='1'
    settings=root/'isolated-settings';settings_dir=settings/'PatientStudyImporter';settings_dir.mkdir(parents=True,exist_ok=True)
    workspace=root/'gui-workspace';workspace.mkdir(exist_ok=True)
    os.environ['APPDATA']=str(settings);os.environ['LOCALAPPDATA']=str(settings)
    (settings_dir/'settings.json').write_text(json.dumps({'workspace':str(workspace)}))
    from tkinter import ttk,messagebox
    from .gui import Application
    from .smoke import SAMPLE
    from . import __version__,updater
    from PIL import Image,ImageGrab
    from docx import Document
    app=Application()
    errors=[]
    app.root.report_callback_exception=lambda typ,value,tb:errors.append(str(value))
    def fail_dialog(title,message,**kwargs):raise AssertionError(title+': '+message)
    messagebox.showerror=fail_dialog
    def settle():
        app.root.update_idletasks();app.root.update()
        assert not errors,errors
    def finish():
        deadline=time.monotonic()+60
        while app.working or not app.events.empty():
            settle();time.sleep(.04)
            assert time.monotonic()<deadline,'UI operation timeout'
        settle()
    def visible(button):
        window=button.winfo_toplevel()
        assert button.winfo_ismapped(),str(button)
        assert button.winfo_rooty()+button.winfo_height()<=window.winfo_rooty()+window.winfo_height(),button.cget('text')
        assert button.winfo_rootx()+button.winfo_width()<=window.winfo_rootx()+window.winfo_width(),button.cget('text')
    def capture(name,window=None):
        window=window or app.root
        window.lift();window.focus_force();settle()
        # Exercise queued native paint events before taking a real desktop capture.
        for _ in range(5):settle();time.sleep(.05)
        x,y=window.winfo_rootx(),window.winfo_rooty()
        ImageGrab.grab(bbox=(x,y,x+window.winfo_width(),y+window.winfo_height())).save(root/name)
    assert str(app.start_button['state'])=='disabled'
    capture('00-empty.png')
    external=root/'external files';external.mkdir(exist_ok=True)
    first=external/'Анализ крови.txt';first.write_text(SAMPLE,encoding='utf8')
    second=external/'УЗИ почек.docx';doc=Document();doc.add_paragraph(SAMPLE.replace('02.03.2025','03.03.2025'));doc.save(second)
    third=external/'Исследование.txt';third.write_text('Дата рождения: 01.01.2000\nЗаключение: учебный пример. Настоящих данных нет.',encoding='utf8')
    # Native DnD registration and Tcl-escaped names with spaces; use the real callback.
    if os.name=='nt':
        assert app.dnd
        data=app.root.tk.call('list',str(first),str(second),str(third))
        from types import SimpleNamespace
        app.on_drop(SimpleNamespace(data=data))
    else:app.stage([first,second,third])
    finish();assert len(app.paths)==3
    visible(app.start_button);visible(app.add_button)
    capture('01-ready.png')
    app.root.geometry('850x620');settle();visible(app.start_button);visible(app.add_button)
    capture('small-window.png');app.root.geometry('1100x800');settle()
    app.start_button.invoke();finish()
    summary=app.workspace.summary();assert summary['documents']==3 and summary['patients']==1 and summary['review']==1,summary
    assert app.screen=='result'
    capture('02-result.png')
    app.review();settle();review=app.review_window
    assert review.selected and review.candidates
    visible(review.confirm_button)
    capture('03-review.png',review.window)
    # Exercise actual PDF rendering, multipage navigation and zoom in the review pane.
    from pypdf import PdfWriter
    preview=root/'preview.pdf';writer=PdfWriter()
    writer.add_blank_page(width=595,height=842);writer.add_blank_page(width=595,height=842)
    writer.write(preview)
    review.show_preview({'original':str(preview)},{'pages':['Synthetic preview']});settle()
    assert review.preview_document is not None and hasattr(review,'preview_photo')
    review.turn_page(1);review.resize_preview(.25);settle()
    assert review.page==1 and review.zoom==1.25
    review.use_candidate(*review.candidates[0]);review.confirm_button.invoke();settle()
    assert review.selected is None and not review.records
    assert str(review.confirm_button['state'])=='disabled'
    with app.workspace.connect() as db:before=db.execute("SELECT count(*) FROM events WHERE kind='patient_confirmed'").fetchone()[0]
    review.confirm()
    with app.workspace.connect() as db:after=db.execute("SELECT count(*) FROM events WHERE kind='patient_confirmed'").fetchone()[0]
    assert before==after==1
    capture('04-confirmed.png',review.window)
    review.close();settle()
    assert app.workspace.summary()['review']==0
    assert app.heading.cget('text')=='Документы сохранены'
    capture('05-finished.png')
    assert all(p.exists() for p in [first,second,third]),'External originals changed'
    app.show_documents();app.stage([first]);finish();app.start();finish()
    assert app.workspace.summary()['documents']==3 and app.batch[0]['status']=='Повтор'
    invalid=external/'Повреждённый.pdf';invalid.write_bytes(b'%PDF-1.4\nsynthetic broken document')
    app.show_documents();app.stage([invalid]);finish();app.start();finish()
    assert app.workspace.summary()['errors']==1
    assert app.heading.cget('text')=='Нужно ваше внимание'
    app.problems();settle();capture('06-errors.png',app.problem_window.window);app.problem_window.window.destroy()
    # Verify update offer is deferred and declining leaves data and workflow alone.
    offered=[];future=updater.Release('v99.0.0','','',1)
    with patch.object(messagebox,'askyesno',side_effect=lambda *a,**k:offered.append(True) or False):
        if os.name=='nt' and getattr(sys,'frozen',False):
            with patch.object(updater,'check_for_update',return_value=future),patch.object(updater,'cleanup_cache'):
                app.auto_update_check()
                deadline=time.monotonic()+10
                while not offered:
                    settle();time.sleep(.05)
                    assert time.monotonic()<deadline,'Startup update check did not offer update'
        else:app.pending_update=future;app.offer_update()
    assert offered and not app.updating
    if getattr(sys,'frozen',False):
        from .help_view import help_path
        help_file=help_path();html=help_file.read_text(encoding='utf8')
        assert __version__ in html and 'data:image/png;base64,' in html
    app.root.destroy()
    result={'ok':True,'checks':['empty_state','picker_or_native_drop_callback','external_originals_preserved','same_patient_incremental_import','per_file_results','small_window_buttons','visible_candidates','pdf_preview_pages_zoom','confirmation_clears_selection','repeat_confirmation_blocked','consistent_result_state','deduplication','actionable_errors','automatic_update_offer_decline','embedded_help' if getattr(sys,'frozen',False) else 'source_capture']}
    (root/'ui-test.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
