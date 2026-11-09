"""Real Tk flows with synthetic inputs, used in both source and frozen Windows CI."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote


def run(root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['PATIENT_IMPORTER_NO_UPDATE_CHECK']='1'
    os.environ['PATIENT_IMPORTER_NO_DIAGNOSTICS_PROMPT']='1'
    settings=root/'isolated-settings';settings_dir=settings/'PatientStudyImporter';settings_dir.mkdir(parents=True,exist_ok=True)
    workspace=root/'gui-workspace';workspace.mkdir(exist_ok=True)
    os.environ['APPDATA']=str(settings);os.environ['LOCALAPPDATA']=str(settings)
    (settings_dir/'settings.json').write_text(json.dumps({'workspace':str(workspace)}))
    from tkinter import ttk,messagebox
    from .gui import Application, DOCUMENTS_PER_PAGE
    from .smoke import SAMPLE
    from . import __version__,updater
    from PIL import Image,ImageGrab,ImageDraw,ImageFont
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
        while app.working or app.workspace_actions.active or not app.events.empty():
            settle();time.sleep(.04)
            assert time.monotonic()<deadline,'UI operation timeout'
        settle()
    def check_background_save(method,invoke,while_pending):
        entered=threading.Event()
        release=threading.Event()
        original=getattr(app.workspace,method)
        calls=[]

        def held(*args,**kwargs):
            calls.append(threading.get_ident())
            entered.set()
            assert release.wait(15),'Save test did not release the worker'
            return original(*args,**kwargs)

        with patch.object(app.workspace,method,side_effect=held):
            invoke()
            try:
                deadline=time.monotonic()+15
                while not entered.is_set():
                    settle();time.sleep(.02)
                    assert time.monotonic()<deadline,'Save did not start'
                ticks=[]
                app.root.after(0,lambda:ticks.append(True))
                settle()
                assert ticks and len(calls)==1 and calls[0]!=threading.get_ident()
                while_pending()
            finally:release.set()
            finish()
        assert len(calls)==1
    def close_review(window):
        # Closing the review window rebuilds the workbook once for the whole session.
        window.close();finish()
        deadline=time.monotonic()+60
        while window.window.winfo_exists():
            settle();time.sleep(.04);assert time.monotonic()<deadline,'Workbook update did not finish'
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
    third=external/'Исследование.png'
    synthetic_text=SAMPLE.replace('71.5','<0,05')
    original=Image.new('RGB',(1650,1250),'white');draw=ImageDraw.Draw(original)
    font_path='C:/Windows/Fonts/consola.ttf' if os.name=='nt' else '/System/Library/Fonts/Menlo.ttc'
    font=ImageFont.truetype(font_path,26)
    draw.text((40,40),'УЧЕБНЫЙ БЛАНК · СИНТЕТИЧЕСКИЕ ДАННЫЕ',font=font,fill='#586779')
    for index,line in enumerate(synthetic_text.splitlines()):draw.text((40,120+index*60),line,font=font,fill='black')
    original.save(third)
    # This test exercises the review UI with a deterministic OCR error. Real
    # bundled OCR is measured separately by the self-test and benchmark.
    from . import extraction
    real_extract=extraction.extract
    def ui_extraction(path):
        if Path(path).suffix=='.png' and Path(path).read_bytes()==third.read_bytes():
            text=synthetic_text.replace('мкмоль/л','мкмоль/π')
            return {'pages':[text],'native_pages':[''],'page_numbers':[1],'ocr_pages':[1],
                    'format':'Изображение','ocr_evidence':[],'ocr_alternatives':[],
                    'extraction_issues':[{'code':'OCR_USED','message':'Сверьте распознанные со скана данные с оригиналом','page':1}]}
        return real_extract(path)
    ocr_patch=patch.object(extraction,'extract',side_effect=ui_extraction);ocr_patch.start()
    # Native DnD registration and Tcl-escaped names with spaces; use the real callback.
    if os.name=='nt':
        assert app.dnd
        data=app.root.tk.call('list',str(first),str(second),str(third))
        app.on_drop(SimpleNamespace(data=data))
    else:app.stage([first,second,third])
    finish();assert len(app.paths)==3
    visible(app.start_button);visible(app.add_button)
    capture('01-ready.png')
    app.root.geometry('850x620');settle();visible(app.start_button);visible(app.add_button)
    capture('small-window.png');app.root.geometry(f'{min(1100,app.root.winfo_screenwidth()-50)}x{min(800,app.root.winfo_screenheight()-130)}');settle()
    # The wheel must scroll when the pointer is over a row label, not just empty canvas.
    for index in range(15):app.row(f'Учебный файл {index}.txt','TXT')
    settle();target=app.content.body.winfo_children()[0].winfo_children()[0]
    target.event_generate('<MouseWheel>',delta=-120,x=5,y=5);settle()
    assert app.content.canvas.yview()[0]>0,'Wheel over a child label did not scroll'
    app.show_documents();settle()
    app.start_button.invoke();finish()
    summary=app.workspace.summary();assert summary['documents']==3 and summary['patients']==1 and summary['review']==1,summary
    assert app.screen=='result'
    capture('02-result.png')
    app.review();settle();review=app.review_window
    assert review.selected and review.candidates
    review.window.geometry('850x620');settle();visible(review.confirm_button);capture('review-small-window.png',review.window)
    review.window.geometry(f'{min(1240,app.root.winfo_screenwidth()-50)}x{min(850,app.root.winfo_screenheight()-130)}');settle()
    capture('03-review.png',review.window)
    visible(review.confirm_button)
    # Exercise actual PDF rendering, multipage navigation and zoom in the review pane.
    from pypdf import PdfWriter
    preview=root/'preview.pdf';writer=PdfWriter()
    writer.add_blank_page(width=595,height=842);writer.add_blank_page(width=595,height=842)
    writer.write(preview)
    review.show_preview({'original':str(preview)},{'pages':['Synthetic preview']});settle()
    assert review.preview_document is not None and review.preview_photo is not None
    review.turn_page(1);review.resize_preview(.25);settle()
    assert review.page==1 and review.zoom==1.25

    class UnrenderablePreview:
        def __init__(self):
            self.closed=False
            self.attempts=0

        def __getitem__(self,index):
            self.attempts+=1
            raise RuntimeError('Synthetic render failure')

        def close(self):
            self.closed=True

    cancelled=UnrenderablePreview()
    broken=UnrenderablePreview()
    with patch('pypdfium2.PdfDocument',side_effect=[cancelled,broken]):
        review.show_preview({'original':str(preview)},{'pages':['Synthetic replacement']})
        review.show_preview({'original':str(preview)},{'pages':['Synthetic replacement']})
        assert not review.canvas.find_all() and review.preview_photo is None
        settle()
        assert cancelled.closed and cancelled.attempts==0 and broken.attempts==1
        assert not review.canvas.find_all() and review.preview_photo is None
        assert review.page_label.get()=='Откройте оригинал'
    review.show_preview({'original':str(preview)},{'pages':['Synthetic preview']});settle()
    preview_handle=review.preview_document
    assert broken.closed and preview_handle is not None and review.preview_photo is not None
    review.use_candidate(*review.candidates[0])
    def while_patient_pending():
        assert review.saving and str(review.confirm_button['state'])=='disabled'
        review.confirm()
        review.close()
        assert review.window.winfo_exists()
        app.start();app.stage([first]);app.select(root/'blocked-workspace')
        assert not app.working and not (root/'blocked-workspace').exists()
    check_background_save('assign',review.confirm_button.invoke,while_patient_pending)
    assert review.selected is None and not review.records
    assert str(review.confirm_button['state'])=='disabled'
    with app.workspace.connect() as db:before=db.execute("SELECT count(*) FROM events WHERE kind='patient_confirmed'").fetchone()[0]
    review.confirm()
    with app.workspace.connect() as db:after=db.execute("SELECT count(*) FROM events WHERE kind='patient_confirmed'").fetchone()[0]
    assert before==after==1
    capture('04-confirmed.png',review.window)
    review.next_button.invoke();settle()
    assert preview_handle.raw is None
    assert app.workspace.summary()['review']==0
    assert app.workspace.summary()['quality_review']==1
    assert app.heading.cget('text')=='Нужно ваше внимание','Identity approval must not approve results'
    quality=app.review_window
    assert quality.selected and str(quality.confirm_button['state'])=='disabled'
    visible(quality.confirm_button);visible(quality.attest)
    capture('07-quality-review.png',quality.window)
    quality.window.geometry('900x650');settle();visible(quality.confirm_button);visible(quality.attest)
    capture('quality-small-window.png',quality.window)
    quality.window.geometry(f'{min(1240,app.root.winfo_screenwidth()-50)}x{min(850,app.root.winfo_screenheight()-130)}');settle()
    quality.rows.selection_set('0');quality.edit_button.invoke();settle()
    editor=quality.row_editor;assert editor.fields['result'].get()=='<0,05'
    editor.fields['unit'].set('мкмоль/л');capture('08-field-editor.png',editor.window)
    visible(editor.save_button);editor.save_button.invoke();settle()
    assert quality.observations[0]['unit']=='мкмоль/л' and quality.observations[0]['number'] is None
    quality.note.insert('1.0','Сверены все строки учебного оригинала; единица измерения исправлена.')
    quality.attest.invoke();settle();assert str(quality.confirm_button['state'])=='normal'
    def while_quality_pending():
        assert quality.saving and str(quality.confirm_button['state'])=='disabled'
        quality.confirm()
        quality.close()
        assert quality.window.winfo_exists()
    check_background_save('confirm_quality',quality.confirm_button.invoke,while_quality_pending)
    assert quality.selected is None and not quality.records
    assert app.workspace.summary()['total_review']==0
    capture('09-quality-finished.png',quality.window);close_review(quality)
    assert app.heading.cget('text')=='Документы сохранены'
    capture('05-finished.png')
    app.history();settle();history=app.history_window
    native_id=next(r['id'] for r in history.records.values() if r['filename']=='Анализ крови.txt')
    history.list.selection_set(native_id);history.reprocess_button.invoke()
    deadline=time.monotonic()+60
    while history.candidate is None:
        settle();time.sleep(.04);assert time.monotonic()<deadline,'Reprocess draft timeout'
    capture('10-history.png',history.candidate_window)
    visible(history.apply_button);history.apply_button.invoke();finish()
    assert app.workspace.summary()['documents']==3 and app.workspace.summary()['total_review']==0
    history.close();settle()
    app.show_documents();settle();assert str(app.start_button['state'])=='disabled'
    with app.workspace.connect() as db:app.workspace.touch(db)
    app.refresh();settle();assert str(app.excel_button['state'])=='disabled','Stale workbook must not look current'
    assert str(app.start_button['state'])=='normal','Refreshing an out-of-date workbook must enable save'
    app.start();finish();assert str(app.excel_button['state'])=='normal'
    assert all(p.exists() for p in [first,second,third]),'External originals changed'
    app.show_documents();app.stage([first]);finish();app.start();finish()
    assert app.workspace.summary()['documents']==3 and app.batch[0]['status']=='Повтор'
    invalid=external/'Повреждённый.pdf';invalid.write_bytes(b'%PDF-1.4\nsynthetic broken document')
    app.show_documents();app.stage([invalid]);finish();app.start();finish()
    assert app.workspace.summary()['errors']==1
    assert app.heading.cget('text')=='Нужно ваше внимание'
    app.problems();settle();capture('06-errors.png',app.problem_window.window)
    app.problem_window.retry(app.workspace.problems()[0]);settle()
    assert len(app.paths)==1,'Retry must restore the queue'
    app.start();finish();assert app.workspace.summary()['errors']==1
    # Stop during a real import, complete the current document, then resume.
    entered=threading.Event();release=threading.Event()
    original_add=app.workspace.add_file
    def held_add(*args,**kwargs):
        entered.set();assert release.wait(15),'Stop test did not release import'
        return original_add(*args,**kwargs)
    app.show_documents();app.stage([first,second]);finish()
    with patch.object(app.workspace,'add_file',side_effect=held_add):
        app.start()
        deadline=time.monotonic()+15
        while not entered.is_set():
            settle();time.sleep(.02);assert time.monotonic()<deadline
        app.stop_button.invoke();release.set();finish()
    assert len(app.batch)==1 and len(app.workspace.files()[0])==1
    app.show_documents();app.start();finish()
    assert not app.workspace.files()[0] and app.workspace.summary()['documents']==3
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
    # Explicit opt-in and withdrawal use isolated fake configuration, never the network.
    from . import diagnostics_config
    from .telemetry import Diagnostics
    from .diagnostics_ui import DiagnosticsWindow, SupportWindow
    support_email=diagnostics_config.SUPPORT_EMAIL or 'templates@example.org'
    diagnostics_example='https://script.google.com/macros/s/AKfycb_TEST_DIAGNOSTICS_ONLY_00000000000000000000/exec'
    test_diagnostics=Diagnostics(settings_dir/'test-diagnostics', endpoint=diagnostics_example)
    assert test_diagnostics.should_prompt() and test_diagnostics.mark_prompted()
    assert not test_diagnostics.enabled
    dialog=DiagnosticsWindow(app.root,test_diagnostics,diagnostics_example,first_run=True)
    settle();visible(dialog.allow_button);visible(dialog.decline_button)
    capture('11-diagnostics.png',dialog.window)
    dialog.allow_button.invoke()
    deadline=time.monotonic()+10
    while dialog.window.winfo_exists():
        settle();time.sleep(.03);assert time.monotonic()<deadline
    assert test_diagnostics.enabled
    test_diagnostics.record('formats','pdf',10)
    dialog=DiagnosticsWindow(app.root,test_diagnostics,diagnostics_example)
    dialog.decline_button.invoke()
    deadline=time.monotonic()+10
    while dialog.window.winfo_exists():
        settle();time.sleep(.03);assert time.monotonic()<deadline
    assert not test_diagnostics.enabled and test_diagnostics.snapshot() is None
    next_version=Diagnostics(settings_dir/'test-diagnostics',endpoint=diagnostics_example,app_version='2.5.0')
    assert not next_version.should_prompt() and not next_version.enabled
    dialog=SupportWindow(app.root,support_email);settle()
    visible(dialog.mail_button);visible(dialog.attest)
    assert str(dialog.mail_button['state'])=='disabled'
    capture('12-template-help.png',dialog.window)
    with patch('patient_importer.diagnostics_ui.webbrowser.open',return_value=True) as compose:
        dialog.compose();compose.assert_not_called()
        dialog.attest.invoke();dialog.mail_button.invoke()
        assert compose.call_count==1
        mailto=compose.call_args.args[0]
        assert unquote(mailto.split('?',1)[0])==f'mailto:{support_email}'
        assert 'attach' not in mailto
    dialog.window.destroy()
    app.root.destroy()
    # 144 DPI (150% at the usual 96-DPI baseline), fresh Tcl interpreter.
    dpi_workspace=root/'dpi-workspace';dpi_workspace.mkdir()
    (settings_dir/'settings.json').write_text(json.dumps({'workspace':str(dpi_workspace)}),encoding='utf8')
    app=Application(tk_scaling=2.0)
    app.root.report_callback_exception=lambda typ,value,tb:errors.append(str(value))
    settle();visible(app.start_button);visible(app.add_button)
    dialog=DiagnosticsWindow(app.root,test_diagnostics,diagnostics_example)
    settle();visible(dialog.allow_button);visible(dialog.decline_button)
    capture('diagnostics-150-percent.png',dialog.window);dialog.window.destroy()
    dialog=SupportWindow(app.root,support_email);settle()
    visible(dialog.mail_button);visible(dialog.attest)
    capture('template-help-150-percent.png',dialog.window);dialog.window.destroy()
    app.stage([first,third]);finish();app.start();finish();app.review();settle()
    capture('review-150-percent.png',app.review_window.window)
    visible(app.review_window.confirm_button)
    candidate=app.review_window.candidate_area.winfo_children()[1].winfo_children()[0]
    visible(candidate)
    assert candidate.winfo_rooty()+candidate.winfo_height()<=app.review_window.editor_area.canvas.winfo_rooty()+app.review_window.editor_area.canvas.winfo_height()
    candidate.invoke();settle();visible(app.review_window.name_entry)
    capture('review-fields-150-percent.png',app.review_window.window)
    app.review_window.use_candidate(*app.review_window.candidates[0]);app.review_window.confirm();finish()
    app.review_window.next_button.invoke();settle()
    capture('quality-150-percent.png',app.review_window.window)
    visible(app.review_window.confirm_button);visible(app.review_window.attest)
    app.review_window.window.destroy();app.show_documents();capture('main-150-percent.png')
    # Native text corrections and explicit duplicate review use the same UI APIs.
    app.select(root/'review-extras');settle()
    narrative=external/'Заключение.txt'
    narrative.write_text('Пациент: ТЕСТОВ ТЕСТ ТЕСТОВИЧ\nДата рождения: 01.01.2000\nУЗИ почек\nЗаключение: учебная ошибка распознавания.',encoding='utf8')
    app.stage([narrative]);finish();app.start();finish();app.review();settle()
    quality=app.review_window;quality.edit_sections();settle()
    quality.sections_editors[-1].delete('1.0','end');quality.sections_editors[-1].insert('1.0','Заключение: учебный текст сверён и исправлен.')
    quality.sections_save_button.invoke();settle()
    quality.date.set('02.03.2025');quality.note.insert('1.0','Текст заключения сверён; числовых показателей нет.')
    quality.attest.invoke();quality.confirm_button.invoke();finish()
    assert not quality.records
    with app.workspace.connect() as db:
        parsed=json.loads(db.execute('SELECT analysis FROM documents').fetchone()[0])
        assert parsed['sections'][-1]['text']=='Заключение: учебный текст сверён и исправлен.'
    close_review(quality);app.select(root/'duplicate-extras');settle()
    original_text=SAMPLE.replace('мкмоль/л','мкмоль/π')
    pair=[]
    for index in range(2):
        path=external/f'Сравнение {index}.txt';path.write_text(original_text.replace('Пациент: ','Пациент: '+' '*index),encoding='utf8');pair.append(path)
    app.stage(pair);finish();app.start();finish();app.review();settle()
    quality=app.review_window
    duplicate=next(r for r in quality.records.values() if any(i['code']=='possible_duplicate' for i in r['issues']))
    quality.list.selection_set(duplicate['id']);quality.load();quality.compare_duplicate();settle()
    assert quality.duplicate_window.window.winfo_exists()
    quality.duplicate_window.confirm();finish()
    assert app.workspace.summary()['documents']==1 and app.workspace.summary()['duplicates']==1
    quality.window.destroy()
    # Text documents go through the queue of stumbles and save themselves.
    app.select(root/'queue-extras');settle()
    queued=external/'Очередь.txt';queued.write_text(SAMPLE.replace('мкмоль/л','мкмоль/π'),encoding='utf8')
    app.stage([queued]);finish();app.start();finish();app.review();settle()
    quality=app.review_window
    assert not quality.full_mode and quality.current['kind']=='row'
    visible(quality.accept_button);capture('06-queue.png',quality.window)
    quality.window.geometry('900x650');settle();visible(quality.accept_button);visible(quality.whole_button)
    capture('queue-small-window.png',quality.window)
    quality.vars['unit'].set('мкмоль/л');quality.accept_button.invoke()
    deadline=time.monotonic()+60
    while quality.save_queue or quality.finishing or quality.session_saved or app.workspace_actions.active:
        settle();time.sleep(.04);assert time.monotonic()<deadline,'Queue save timeout'
    assert app.workspace.summary()['total_review']==0
    close_review(quality)
    # Documents of an earlier version are offered a recalculation that keeps the workbook usable.
    with app.workspace.connect() as db,db:
        rows=db.execute('SELECT id,analysis FROM documents').fetchall()
        for row in rows:
            old=json.loads(row['analysis']);old['version']='2.3.0';old['quality']={'issues':[],'confirmed':False}
            db.execute('UPDATE documents SET analysis=? WHERE id=?',(json.dumps(old,ensure_ascii=False),row['id']))
    app.refresh();settle();assert app.workspace.summary()['reparse']==1
    with patch.object(messagebox,'askyesno',return_value=True),patch.object(messagebox,'showinfo') as reported:
        app.reparse();finish()
    reported.assert_called_once();assert app.workspace.summary()['reparse']==0
    # Long queues retain their visible widgets across focus and expose every file.
    queue_root=root/'long-queue'
    incoming=queue_root/'Входящие';incoming.mkdir(parents=True)
    for index in range(1000):
        (incoming/f'Учебный документ {index:04d}.txt').write_text(SAMPLE,encoding='utf8')
    app.select(queue_root);settle()
    assert len(app.paths)==1000
    children=tuple(app.content.body.winfo_children())
    assert len(children)==DOCUMENTS_PER_PAGE*2
    visible(app.page_previous);visible(app.page_next)
    with patch.object(app.workspace,'summary',wraps=app.workspace.summary) as summary:
        app.show_documents()
        assert sum(not args and not kwargs for args,kwargs in summary.call_args_list)==1
    app.focus_refresh(SimpleNamespace(widget=app.root));settle()
    assert tuple(app.content.body.winfo_children())==children
    with patch.object(app.workspace,'summary',wraps=app.workspace.summary) as summary:
        app.page_next.invoke()
        summary.assert_not_called()
    settle()
    assert app.documents_page==1 and tuple(app.content.body.winfo_children())!=children
    app.turn_list_page(1000);settle()
    assert str(app.page_next['state'])=='disabled'
    assert app.page_range.cget('text').endswith('1000 из 1000')
    last=app.paths[-1]
    last_row=app.content.body.winfo_children()[-2]
    remove=next(child for child in last_row.winfo_children() if isinstance(child,ttk.Button))
    remove.invoke();settle()
    assert len(app.paths)==999 and not last.exists()
    assert (queue_root/'Отложено'/last.name).is_file()
    release=threading.Event()
    failed=threading.Event()
    completed=[]

    def fail_during_close():
        assert release.wait(15)
        failed.set()
        raise RuntimeError('Synthetic failure while closing')

    with patch.object(app.root,'destroy',wraps=app.root.destroy) as destroy:
        assert app.workspace_actions.run(fail_during_close,lambda result,error:completed.append((result,error)))
        try:
            app.close()
            assert app.closing and app.root.winfo_exists()
            destroy.assert_not_called()
        finally:release.set()
        deadline=time.monotonic()+15
        while not destroy.called:
            settle();time.sleep(.02)
            assert time.monotonic()<deadline,'Closing did not wait for the workspace action'
        destroy.assert_called_once()
    assert failed.is_set() and not completed
    ocr_patch.stop()
    result={'ok':True,'checks':['empty_state','picker_or_native_drop_callback','external_originals_preserved','same_patient_incremental_import','per_file_results','small_window_buttons','wheel_over_row','150_percent_layout','visible_candidates','pdf_preview_pages_zoom','confirmation_clears_selection','repeat_confirmation_blocked','consistent_result_state','quality_stays_pending_after_identity','explicit_quality_attestation','inequality_and_corrected_unit_preserved','quality_small_window_and_150_percent','reprocess_preview_apply_same_study','stale_workbook_requires_save','narrative_text_correction','manual_duplicate_comparison','deduplication','actionable_errors','retry_returns_to_queue','stop_and_resume','automatic_update_offer_decline','embedded_help' if getattr(sys,'frozen',False) else 'source_capture']}
    result['checks'].extend(['diagnostics_explicit_opt_in','diagnostics_withdrawal_clears_queue',
                             'diagnostics_refusal_survives_update','manual_template_draft_no_attachment',
                             'diagnostics_and_support_150_percent_layout','patient_save_keeps_tk_responsive',
                             'quality_save_keeps_tk_responsive','workspace_actions_block_conflicts',
                             'close_waits_for_workspace_action','identity_preview_clears_on_failure',
                             'long_queue_paging_focus_and_defer'])
    (root/'ui-test.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
