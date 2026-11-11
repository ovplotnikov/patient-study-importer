"""Guided, local desktop workflow: documents -> processing -> results."""
from datetime import datetime
import functools
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import textwrap
import tkinter as tk
from tkinter import filedialog,messagebox,ttk
import traceback
from .pdf_render import PDFIUM_LOCK
from .storage import ReviewCommittedError,Workspace
from . import __version__,review_queue,updater,intake,parallel,ui,diagnostics_config
from .telemetry import Diagnostics


DOCUMENTS_PER_PAGE = 25


def open_path(path):
    if os.name=='nt':os.startfile(str(path))
    elif sys.platform=='darwin':subprocess.Popen(['open',str(path)])
    else:subprocess.Popen(['xdg-open',str(path)])


class Application:
    def __init__(self,tk_scaling=None):
        # The tested Windows bundle always includes TkDnD. Source builds on other
        # platforms may use an incompatible Tk version; the picker remains usable.
        self.dnd=False
        if os.name=='nt':
            from tkinterdnd2 import TkinterDnD
            self.root=TkinterDnD.Tk();self.dnd=True
        else:self.root=tk.Tk()
        if tk_scaling is not None:self.root.tk.call('tk','scaling',tk_scaling)
        self.root.title('Исследования пациентов')
        self.root.geometry(f'{min(1100,self.root.winfo_screenwidth()-50)}x{min(800,self.root.winfo_screenheight()-130)}+20+20')
        self.root.minsize(850,620);ui.setup(self.root);self.icons=ui.Icons(self.root)
        self.events=queue.Queue();self.worker=None;self.cancel=threading.Event();self.closing=False
        self.updating=False;self.update_cancel=threading.Event();self.pending_update=None;self.update_prompt_active=False
        self.working=False;self.reparsing=False;self.screen='documents';self.batch=[];self.paths=[]
        self.workspace_actions=ui.WorkspaceActions(self.root,can_start=self.can_start_workspace_action,
                                                   can_present=lambda:not self.closing,
                                                   on_idle=self.after_workspace_action)
        self.documents_page=0;self.result_page=0;self.skipped_files=0
        self.rendered_queue=None;self.focus_job=None;self.list_summary=None
        self.result_needs_form_help=False
        self.workspace=None;self.review_window=None;self.problem_window=None;self.history_window=None
        self.settings=Path(os.environ.get('APPDATA',str(Path.home()/'.config')))/'PatientStudyImporter'/'settings.json'
        self.diagnostics=Diagnostics(self.settings.parent/'diagnostics',endpoint=diagnostics_config.TELEMETRY_URL)
        self.diagnostics_window=None;self.support_window=None;self.diagnostics_worker=None
        self.frame=ttk.Frame(self.root,padding=(28,16));self.frame.pack(fill='both',expand=True)
        header=ttk.Frame(self.frame);header.pack(fill='x',pady=(0,12))
        ttk.Label(header,text='Исследования пациентов',font=(ui.FONT,18,'bold')).pack(side='left')
        self.help_button=ttk.Button(header,text=' Помощь',image=self.icons.get('question-circle'),compound='left',style='Link.TButton',command=self.details)
        self.help_button.pack(side='right')
        self.steps=ttk.Frame(self.frame);self.steps.pack(fill='x',pady=(0,12))
        self.step_labels=[]
        for index,(title,icon) in enumerate([('Документы','file-earmark'),('Обработка','gear'),('Результат','table')]):
            self.steps.columnconfigure(index*2,weight=1)
            label=tk.Label(self.steps,text='  '+title,font=(ui.FONT,13,'bold'),bg='white',fg=ui.MUTED,padx=12,pady=10,compound='left')
            label.grid(row=0,column=index*2,sticky='ew');self.step_labels.append((label,icon))
            if index<2:ttk.Separator(self.steps,orient='horizontal').grid(row=0,column=index*2+1,sticky='ew',padx=12);self.steps.columnconfigure(index*2+1,weight=1)
        # Bottom controls stay visible even while the central document list scrolls.
        footer=ttk.Frame(self.frame);footer.pack(side='bottom',fill='x',pady=(8,0))
        ttk.Label(footer,text='На этом компьютере',image=self.icons.get('pc-display','muted',18),compound='left',style='Muted.TLabel',font=(ui.FONT,10)).pack(side='left')
        self.choose_button=ttk.Button(footer,text='Папка данных',style='Link.TButton',command=self.folder_menu);self.choose_button.pack(side='left',padx=10)
        ttk.Label(footer,text=f'Версия {__version__}',style='Muted.TLabel',font=(ui.FONT,10)).pack(side='right')
        self.actions=ttk.Frame(self.frame);self.actions.pack(side='bottom',fill='x',pady=(10,0))
        self.excel_button=ttk.Button(self.actions,text=' Открыть таблицу',image=self.icons.get('table'),compound='left',command=self.open_workbook);self.excel_button.pack(side='left')
        self.start_button=ttk.Button(self.actions,text='Обработать',style='Primary.TButton',command=self.start);self.start_button.pack(side='right')
        self.stop_button=ttk.Button(self.actions,text='Остановить',command=self.stop)
        self.review_button=ttk.Button(self.actions,text='Проверить документы',style='Primary.TButton',command=self.review)
        self.new_button=ttk.Button(self.actions,text='Добавить ещё',command=self.show_documents)
        self.phase=tk.StringVar();self.current=tk.StringVar();self.metrics=tk.StringVar();self.folder_var=tk.StringVar()
        self.heading=ttk.Label(self.frame,text='Документы для обработки',style='Title.TLabel');self.heading.pack(anchor='w')
        self.phase_label=ttk.Label(self.frame,textvariable=self.phase,style='Muted.TLabel',wraplength=780);self.phase_label.pack(anchor='w',pady=(4,6))
        self.progress=ttk.Progressbar(self.frame,mode='determinate')
        self.current_label=ttk.Label(self.frame,textvariable=self.current,wraplength=780,style='Muted.TLabel')
        self.pagination=ttk.Frame(self.frame)
        self.page_range=ttk.Label(self.pagination,style='Muted.TLabel')
        self.page_range.pack(side='left')
        self.page_next=ttk.Button(self.pagination,text='Дальше ›',style='Link.TButton',
                                  command=lambda:self.turn_list_page(1))
        self.page_previous=ttk.Button(self.pagination,text='‹ Назад',style='Link.TButton',
                                      command=lambda:self.turn_list_page(-1))
        self.page_next.pack(side='right')
        self.page_previous.pack(side='right',padx=(0,8))
        self.drop=ttk.Frame(self.frame,padding=(16,10),style='Drop.TFrame')
        self.drop.pack(side='bottom',fill='x',pady=(8,0))
        self.drop_label=ttk.Label(self.drop,text='Перетащите файлы сюда' if self.dnd else 'Добавьте файлы с компьютера',image=self.icons.get('download'),compound='left',style='Drop.TLabel');self.drop_label.pack(side='left')
        self.add_button=ttk.Button(self.drop,text='Добавить файлы',command=self.add_files);self.add_button.pack(side='right')
        self.add_folder_button=ttk.Button(self.drop,text='Папку',style='Link.TButton',command=self.add_folder);self.add_folder_button.pack(side='right',padx=8)
        self.content=ui.ScrollArea(self.frame);self.content.pack(fill='both',expand=True)
        if self.dnd:
            from tkinterdnd2 import DND_FILES
            for widget in [self.root,self.drop,self.drop_label,self.content.canvas,self.content.body]:
                widget.drop_target_register(DND_FILES);widget.dnd_bind('<<Drop>>',self.on_drop)
        default=Path.home()/'Documents'/'Исследования пациентов'
        portable=Path(sys.executable).parent/'Данные'
        if getattr(sys,'frozen',False) and portable.is_dir():default=portable
        if self.settings.exists():
            try:
                saved=Path(json.loads(self.settings.read_text(encoding='utf8'))['workspace'])
                if saved.is_dir():default=saved
            except (OSError,ValueError,KeyError):pass
        self.select(default)
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.after(100,self.poll)
        self.root.bind('<FocusIn>',self.focus_refresh)
        if not os.environ.get('PATIENT_IMPORTER_NO_UPDATE_CHECK'):self.root.after(2000,self.auto_update_check)
        if not os.environ.get('PATIENT_IMPORTER_NO_DIAGNOSTICS_PROMPT'):
            self.root.after(6000,self.offer_diagnostics)
        self.root.after(60000,self.diagnostics_tick)

    def focus_refresh(self,event):
        if (event.widget is self.root and not self.workspace_busy() and self.screen=='documents'
                and self.focus_job is None):
            self.focus_job=self.root.after_idle(self.refresh_focused_documents)

    def refresh_focused_documents(self):
        self.focus_job=None
        if self.screen=='documents':self.show_documents()

    def can_start_workspace_action(self):
        return not (self.working or self.updating or self.update_prompt_active or self.closing)

    def workspace_busy(self):
        return self.workspace_actions.active or not self.can_start_workspace_action()

    def workspace_window_open(self):
        return any(window and window.window.winfo_exists()
                   for window in (self.review_window,self.history_window,self.problem_window))

    def after_workspace_action(self):
        if self.closing:self.root.destroy()

    def set_step(self,index):
        for n,(label,icon) in enumerate(self.step_labels):
            label.configure(bg=ui.PALE if n==index else 'white',fg=ui.BLUE if n==index else ui.MUTED,image=self.icons.get(icon,'blue' if n==index else 'muted'))

    def busy(self,value):
        self.working=value
        for button in [self.start_button,self.choose_button,self.review_button,self.add_button,self.add_folder_button,self.new_button,self.excel_button]:
            button.config(state='disabled' if value else 'normal')
        self.stop_button.config(state='normal' if value else 'disabled')
        if value:
            self.page_previous.configure(state='disabled')
            self.page_next.configure(state='disabled')

    def select(self,path):
        if self.workspace_busy() or self.workspace_window_open():return
        try:
            workspace=Workspace(path,diagnostics=self.diagnostics)
            self.settings.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.settings.with_suffix('.tmp');temporary.write_text(json.dumps({'workspace':str(workspace.root)},ensure_ascii=False),encoding='utf8');temporary.replace(self.settings)
            self.workspace=workspace;self.folder_var.set(str(workspace.root));self.batch=[]
            self.documents_page=0;self.result_page=0;self.rendered_queue=None
            self.show_documents()
        except Exception as exc:messagebox.showerror('Не удалось открыть папку',str(exc),parent=self.root)

    def folder_menu(self):
        if self.workspace_busy():return
        menu=tk.Menu(self.root,tearoff=False)
        menu.add_command(label='Открыть папку данных',command=lambda:self.open_folder(''))
        menu.add_command(label='Открыть «Входящие»',command=lambda:self.open_folder('Входящие'))
        menu.add_command(label='Открыть отложенные файлы',command=lambda:self.open_folder('Отложено'))
        menu.add_command(label='Все исследования…',command=self.history)
        menu.add_separator();menu.add_command(label='Выбрать другую папку…',command=self.choose)
        menu.tk_popup(self.choose_button.winfo_rootx(),self.choose_button.winfo_rooty())

    def choose(self):
        if self.workspace_busy() or self.workspace_window_open():return
        selected=filedialog.askdirectory(title='Рабочая папка с базой и таблицей',initialdir=self.folder_var.get(),parent=self.root)
        if not selected:return
        found=intake.documents_in_folder(selected)
        if found and not messagebox.askyesno('Переместить документы?',
                f'В этой папке есть документы: {found}. При обработке программа перенесёт их в свои папки внутри выбранной.\n\n'
                'Лучше выбрать пустую папку, а документы добавить кнопкой. Всё равно использовать эту папку?',
                default='no',icon='warning',parent=self.root):return
        self.select(selected)

    def row(self,name,subtitle,icon=None,action=None,action_text=None):
        row=ttk.Frame(self.content.body,padding=(4,8));row.pack(fill='x')
        ttk.Label(row,image=icon or self.icons.file(name)).pack(side='left',padx=(4,18))
        if action:
            button=ttk.Button(row,command=action,style='Link.TButton',**({'text':action_text} if action_text else {'image':self.icons.get('x-lg','muted',20)}))
            button.pack(side='right',padx=(8,4))
            if not action_text:button.bind('<Enter>',lambda e:self.current.set('Убрать из очереди — копия останется в папке «Отложено»'))
        text=ttk.Frame(row);text.pack(side='left',fill='x',expand=True)
        label=ttk.Label(text,text=name,font=(ui.FONT,13,'bold'),wraplength=650);label.pack(anchor='w')
        label.bind('<Configure>',lambda e:label.configure(wraplength=max(200,e.width)))
        ttk.Label(text,text=subtitle,style='Muted.TLabel',wraplength=650).pack(anchor='w',pady=(4,0))
        ttk.Separator(self.content.body).pack(fill='x')

    def visible_list_page(self,items,page):
        page=max(0,min(page,(len(items)-1)//DOCUMENTS_PER_PAGE))
        start=page*DOCUMENTS_PER_PAGE
        end=min(len(items),start+DOCUMENTS_PER_PAGE)
        if len(items)>DOCUMENTS_PER_PAGE:
            self.page_range.configure(text=f'{start+1}–{end} из {len(items)}')
            self.page_previous.configure(state='normal' if page else 'disabled')
            self.page_next.configure(state='normal' if end<len(items) else 'disabled')
            self.pagination.pack(fill='x',before=self.content,pady=(0,6))
        else:self.pagination.pack_forget()
        return page,items[start:end]

    def turn_list_page(self,delta):
        if self.workspace_busy():return
        if self.screen=='documents':
            self.documents_page+=delta
            self.show_documents(rescan=False)
        elif self.screen=='result':
            self.result_page+=delta
            self.show_result(self.list_summary,refresh_form_help=False)

    def refresh(self,summary=None):
        if not self.workspace or self.workspace_busy():return
        if summary is None:summary=self.workspace.summary()
        self.list_summary=summary
        self.metrics.set(f"В таблице · Документы: {summary['documents']} · Пациенты: {summary['patients']}")
        self.excel_button.config(state='normal' if Path(summary['workbook']).exists() and not summary.get('pending_export') else 'disabled')
        self.review_button.config(state='normal' if summary['total_review'] else 'disabled')
        if self.screen=='result':self.show_result(summary)
        elif self.screen=='documents':
            self.start_button.configure(
                text=f'Обработать · {len(self.paths)}' if self.paths else 'Сохранить таблицу',
                state='normal' if self.paths or summary['pending_moves'] or summary.get('pending_export') else 'disabled')
        return summary

    def show_documents(self,*,rescan=True):
        if self.workspace_busy() or not self.workspace:return
        self.screen='documents';self.set_step(0);self.excel_button.configure(style='TButton',image=self.icons.get('table'))
        self.progress.pack_forget();self.current_label.pack_forget()
        for b in [self.stop_button,self.review_button,self.new_button]:b.pack_forget()
        self.start_button.pack(side='right');self.drop.pack(side='bottom',fill='x',pady=(8,0),before=self.content)
        if rescan:
            self.paths,skipped=self.workspace.files()
            self.skipped_files=len(skipped)
        self.heading.configure(text='Документы для обработки' if self.paths else 'Добавьте исследования')
        phase=f'Файлов в очереди: {len(self.paths)}' if self.paths else 'PDF, Word, фотографии и сканы'
        if self.skipped_files:phase+=f' · Других файлов: {self.skipped_files}'
        self.phase.set(phase)
        summary=self.refresh(None if rescan else self.list_summary)
        self.documents_page,visible=self.visible_list_page(self.paths,self.documents_page)
        attention=bool(summary['total_review'] or summary['errors'] or summary['pending_moves'] or summary.get('pending_export'))
        rendered=(tuple(visible),attention,summary['total_review'],summary['errors'],summary.get('reparse',0))
        if rendered==self.rendered_queue:return
        self.content.clear()
        self.rendered_queue=rendered
        if not self.paths:
            empty=ttk.Frame(self.content.body,padding=(20,32));empty.pack(fill='x')
            ttk.Label(empty,image=self.icons.get('file-earmark','blue',64)).pack(pady=12)
            ttk.Label(empty,text='Ваши документы появятся здесь',style='Heading.TLabel').pack(pady=8)
            ttk.Label(empty,text='Выберите файлы или целую папку',style='Muted.TLabel').pack()
        for path in visible:
            self.row(path.name,path.suffix.upper().lstrip('.') or 'Документ',action=lambda p=path:self.remove_file(p))
        self.reparse_row(summary)
        if attention:
            self.row('Требуют внимания',f"Проверить документы: {summary['total_review']} · Ошибки: {summary['errors']}",self.icons.get('exclamation-triangle','amber',32),self.show_result,'Посмотреть')

    def reparse_row(self,summary):
        if summary.get('reparse'):
            self.row('Программа стала лучше читать бланки',f"Можно обновить документы: {summary['reparse']} · Подтверждённые вручную не изменятся",
                     self.icons.get('exclamation-triangle','amber',32),self.reparse,'Обновить')

    def reparse(self):
        if not self.workspace or self.workspace_busy() or self.workspace_window_open():return
        count=self.workspace.summary().get('reparse',0)
        if not count:return
        if not messagebox.askyesno('Обновить разбор',
                f'Пересчитать документы по новым правилам: {count}?\n\nОригиналы и документы, подтверждённые вручную, не изменятся. '
                'Перед пересчётом будет сделана резервная копия, прежний результат останется в истории. '
                'Закройте таблицу в Excel.',parent=self.root):return

        def completed(report,error):
            self.reparsing=False;self.busy(False);self.stop_button.pack_forget()
            if self.closing:return
            self.rendered_queue=None
            if self.screen=='documents':self.show_documents(rescan=False)
            else:self.refresh()
            if isinstance(error,parallel.Cancelled):
                self.phase.set('Обновление остановлено. Документы не изменились.')
                return
            if isinstance(error,ReviewCommittedError):messagebox.showwarning('Документы обновлены',str(error),parent=self.root)
            elif error is not None:messagebox.showerror('Не удалось обновить разбор',str(error),parent=self.root)
            else:
                lines=[f"Обновлено документов: {report['updated']}",f"Строк в лабораторных таблицах: было {report['rows_before']}, стало {report['rows_after']}"]
                if report['kept']:lines.append(f"Оставлено без изменений: {report['kept']}. Новый разбор нашёл в них меньше строк")
                if report['reviewed']:lines.append(f"Подтверждённые вручную не изменялись: {report['reviewed']}")
                messagebox.showinfo('Разбор обновлён','\n'.join(lines),parent=self.root)

        self.cancel.clear()
        events=self.events
        work=functools.partial(self.workspace.reparse_unconfirmed,
                               progress=lambda **state:events.put(('progress',state)),cancel=self.cancel)
        if self.workspace_actions.run(work,completed):self.show_reparse_progress(count)

    def show_reparse_progress(self,count):
        """The same progress view as processing, so a long run is visibly alive."""
        self.reparsing=True;self.busy(True)
        self.heading.configure(text='Обновляем разбор документов')
        self.phase.set('Подготовка');self.current.set('')
        self.content.clear();self.rendered_queue=None;self.drop.pack_forget();self.pagination.pack_forget()
        for button in [self.start_button,self.review_button,self.new_button]:button.pack_forget()
        self.stop_button.configure(text='Остановить');self.stop_button.pack(side='right')
        self.progress.pack(fill='x',before=self.content,pady=(8,12))
        self.progress['maximum']=max(1,count);self.progress['value']=0
        self.current_label.pack(anchor='w',before=self.content,pady=(0,12))
        ttk.Label(self.content.body,text='Можно остановить в любой момент до сохранения таблицы: документы останутся как были.',
                  style='Muted.TLabel',wraplength=760).pack(anchor='w',pady=(4,0))

    def add_files(self):
        if self.workspace_busy() or self.workspace_window_open():return
        files=filedialog.askopenfilenames(title='Добавьте исследования',parent=self.root,filetypes=[('Бланки исследований','*.pdf *.docx *.txt *.jpg *.jpeg *.png *.tif *.tiff *.bmp'),('Все файлы','*')])
        if files:self.stage(files)

    def add_folder(self):
        if self.workspace_busy() or self.workspace_window_open():return
        folder=filedialog.askdirectory(title='Папка с новыми исследованиями',parent=self.root)
        if folder:self.stage([folder])

    def on_drop(self,event):
        if self.workspace_busy() or self.workspace_window_open():return 'none'
        self.stage(self.root.tk.splitlist(event.data));return 'copy'

    def stage(self,paths):
        if not self.workspace or self.workspace_busy() or self.workspace_window_open():return
        self.cancel.clear();self.busy(True);self.phase.set('Добавляем файлы…')
        workspace=self.workspace
        def work():
            try:
                result=intake.stage_files(workspace,paths,lambda name:self.events.put(('staging',name)),self.cancel)
                if result['errors']:workspace._diagnostic('failure_stages','intake',len(result['errors']))
                self.events.put(('staged',result))
            except Exception as exc:
                workspace._diagnostic('failure_stages','intake')
                self.events.put(('error',str(exc)))
        self.worker=threading.Thread(target=work,daemon=False);self.worker.start()

    def remove_file(self,path):
        if self.workspace_busy() or self.workspace_window_open():return
        try:
            intake.defer_file(self.workspace,path);self.show_documents()
            self.phase.set('Файл убран из очереди. Копия сохранена в «Отложено».')
        except Exception as exc:messagebox.showerror('Не удалось убрать файл',str(exc),parent=self.root)

    def start(self):
        if not self.workspace or self.workspace_busy() or self.workspace_window_open():return
        self.cancel.clear();self.busy(True);self.screen='processing';self.set_step(1)
        self.heading.configure(text='Обрабатываем документы');self.phase.set('Подготовка');self.current.set('')
        self.content.clear();self.rendered_queue=None;self.drop.pack_forget();self.pagination.pack_forget()
        for b in [self.start_button,self.review_button,self.new_button]:b.pack_forget()
        self.stop_button.configure(text='Остановить');self.stop_button.pack(side='right')
        self.progress.pack(fill='x',before=self.content,pady=(8,12));self.progress['value']=0
        self.current_label.pack(anchor='w',before=self.content,pady=(0,12))
        workspace=self.workspace
        def work():
            try:self.events.put(('finished',workspace.import_folder(progress=lambda **state:self.events.put(('progress',state)),cancel=self.cancel)))
            except Exception as exc:
                try:
                    with (workspace.base/'last-error.txt').open('w',encoding='utf8') as log:traceback.print_exc(file=log)
                except OSError:pass
                self.events.put(('error',str(exc)))
        self.worker=threading.Thread(target=work,daemon=False);self.worker.start()

    def show_result(self,summary=None,*,refresh_form_help=True):
        if self.workspace_busy() or not self.workspace:return
        self.screen='result';self.set_step(2);self.content.clear();self.rendered_queue=None;self.drop.pack_forget()
        self.progress.pack_forget();self.current_label.pack_forget()
        for b in [self.start_button,self.stop_button,self.review_button,self.new_button]:b.pack_forget()
        if summary is None:summary=self.workspace.summary()
        self.list_summary=summary
        if refresh_form_help:
            self.result_needs_form_help=bool(summary['errors'] or (
                summary['quality_review'] and self.workspace.has_serious_review_issue()))
        problems=summary['errors']+summary['pending_moves']
        self.result_page,visible=self.visible_list_page(self.batch,self.result_page)
        self.heading.configure(text='Нужно ваше внимание' if summary['total_review'] or problems or summary.get('pending_export') else 'Документы сохранены')
        self.phase.set(f"В таблице · Документы: {summary['documents']} · Пациенты: {summary['patients']}")
        if summary['total_review']:
            self.excel_button.configure(style='TButton',image=self.icons.get('table'))
            self.review_button.configure(text=f"Проверить документы · {summary['total_review']}",state='normal');self.review_button.pack(side='right')
            self.new_button.pack(side='right',padx=10)
        else:
            self.new_button.pack(side='right');self.excel_button.configure(style='Primary.TButton',image=self.icons.get('table','white'))
        self.excel_button.configure(state='normal' if Path(summary['workbook']).exists() and not summary.get('pending_export') else 'disabled')
        self.reparse_row(summary)
        if summary['review']:self.row('Уточните пациента',f"Документов: {summary['review']}",self.icons.get('exclamation-triangle','amber',32),self.review,'Проверить')
        if summary['quality_review']:self.row('Сверьте данные с оригиналом',f"Документов: {summary['quality_review']} · Показатели пока не подтверждены",self.icons.get('exclamation-triangle','amber',32),self.review,'Проверить')
        if summary.get('incomplete'):self.row('Сверено с замечаниями',f"Документов: {summary['incomplete']} · Причины сохранены в таблице",self.icons.get('exclamation-triangle','amber',32),self.history,'Посмотреть')
        if summary.get('pending_export'):self.row('Таблица ожидает сохранения','Закройте Excel и повторите сохранение',self.icons.get('exclamation-triangle','amber',32),self.start,'Сохранить')
        if problems:self.row('Не все файлы обработаны','Откройте список, чтобы исправить ошибки',self.icons.get('exclamation-triangle','red',32),self.problems,'Разобрать')
        if self.result_needs_form_help:
            self.row('Бланк прочитан не полностью','Можно помочь добавить поддержку этого формата',
                     self.icons.get('question-circle','muted',32),self.help_with_form,'Как помочь')
        if self.batch:
            from . import quality

            ttk.Label(self.content.body,text='Последняя обработка',font=(ui.FONT,13,'bold')).pack(anchor='w',pady=(20,6))
            changing={'Добавлен','Уточнить пациента','Проверить данные'}
            documents=self.workspace.documents_by_ids(
                item['document_id'] for item in visible if item['status'] in changing)
            for item in visible:
                status=item['status']
                document=documents.get(item.get('document_id'))
                if status in changing and document is not None:
                    try:parsed=json.loads(document['analysis'])
                    except ValueError:pass
                    else:
                        status='Повтор' if parsed.get('duplicate_of') else 'Уточнить пациента' if not document['patient_id'] else 'Проверить данные' if quality.needs_review(parsed) else 'Сверено с замечаниями' if parsed.get('quality',{}).get('incomplete') else 'Добавлен'
                icon,color={'Добавлен':('check-circle','green'),'Повтор':('files','muted'),'Ошибка':('exclamation-triangle','red'),'Уточнить пациента':('exclamation-triangle','amber'),'Проверить данные':('exclamation-triangle','amber'),'Сверено с замечаниями':('exclamation-triangle','amber')}[status]
                subtitle={'Добавлен':'Добавлен в таблицу','Повтор':'Повтор — уже есть в таблице','Ошибка':'Не удалось прочитать','Уточнить пациента':'Нужно уточнить пациента','Проверить данные':'Черновик · сверьте данные с оригиналом','Сверено с замечаниями':'Сверено с замечаниями · причины сохранены'}[status]
                self.row(item['file'],subtitle,self.icons.get(icon,color,32))
        elif not summary['total_review'] and not problems and not summary.get('pending_export'):self.row('Всё готово','Можно открыть таблицу или добавить новые исследования',self.icons.get('check-circle','green',48))

    def problems(self):
        if self.workspace_busy() or self.workspace_window_open():return
        self.problem_window=ProblemsWindow(self.root,self.workspace,self.icons,self.after_retry)

    def after_retry(self):self.show_documents()

    def stop(self):
        if self.updating:
            self.update_cancel.set();self.phase.set('Отменяем загрузку обновления…');return
        self.cancel.set();self.stop_button.config(state='disabled')
        self.phase.set('Останавливаем. Документы не изменятся.' if self.reparsing else 'Сохраним результат после текущего файла.')

    def poll(self):
        try:
            while True:
                kind,data=self.events.get_nowait()
                if kind=='staging':self.phase.set('Добавляем: '+data)
                elif kind=='staged':
                    self.busy(False)
                    if self.closing:self.root.destroy();return
                    self.show_documents()
                    if data['errors'] or data['skipped']:
                        message=f"Добавлено: {len(data['added'])}. Других форматов: {len(data['skipped'])}. Ошибок: {len(data['errors'])}."
                        if data['errors']:message+='\n\n'+'\n'.join(item['file']+': '+item['reason'] for item in data['errors'][:8])
                        messagebox.showwarning('Не все файлы добавлены',message,parent=self.root)
                # A late report from a finished run must not overwrite its outcome.
                elif kind=='progress' and self.working:
                    if 'phase' in data:self.phase.set(data['phase'])
                    if 'current' in data:self.current.set(data['current'])
                    if 'total' in data:self.progress['maximum']=max(1,data['total'])
                    if 'done' in data:self.progress['value']=data['done']
                    if self.reparsing and 'done' in data:self.phase.set(f"Обновляем разбор документов · {data['done']} из {int(self.progress['maximum'])}")
                    if data.get('stoppable') is False:self.stop_button.config(state='disabled')
                elif kind=='finished':
                    self.batch=data.get('batch',[]);self.result_page=0;self.busy(False)
                    if self.closing:self.root.destroy();return
                    self.show_result(data)
                    self.send_diagnostics()
                    if self.cancel.is_set():self.phase.set(self.phase.get()+f" · Остановлено. Осталось: {data.get('remaining',0)}")
                    if data.get('skipped_files'):self.phase.set(self.phase.get()+f" · Других форматов: {data['skipped_files']}")
                elif kind=='error':
                    self.busy(False)
                    if self.closing:self.root.destroy();return
                    self.show_documents();self.phase.set('Обработка не завершена. Данные сохранены.')
                    messagebox.showerror('Не удалось завершить',data,parent=self.root)
                elif kind=='update_available':self.pending_update=data;self.root.after(0,self.offer_update)
                elif kind=='update_progress':self.progress['value']=data;self.phase.set(f'Скачиваем обновление — {data}%')
                elif kind=='update_error':
                    cancelled=self.update_cancel.is_set();self.finish_update_attempt('Обновление отменено.' if cancelled else data)
                    if self.closing:return
                    if not cancelled:messagebox.showerror('Обновление не установлено',data,parent=self.root)
                elif kind=='update_ready':
                    if self.update_cancel.is_set():
                        import shutil
                        shutil.rmtree(data,ignore_errors=True);self.finish_update_attempt('Обновление отменено.')
                        if self.closing:return
                    else:
                        try:updater.launch_install(data,sys.executable)
                        except Exception:
                            self.finish_update_attempt('Не удалось начать обновление. Текущая программа и данные сохранены.')
                        else:self.root.destroy();return
        except queue.Empty:pass
        self.root.after(100,self.poll)

    def auto_update_check(self):
        if os.name!='nt' or not getattr(sys,'frozen',False):return
        def check():
            try:
                updater.cleanup_cache();release=updater.check_for_update()
                if release:self.events.put(('update_available',release))
            except Exception:pass
        threading.Thread(target=check,daemon=True).start()

    def diagnostics_tick(self):
        if self.closing:return
        self.send_diagnostics()
        self.root.after(60000,self.diagnostics_tick)

    def send_diagnostics(self):
        if not self.diagnostics.enabled or self.closing:return
        if self.diagnostics_worker and self.diagnostics_worker.is_alive():return
        self.diagnostics_worker=threading.Thread(target=self.diagnostics.send_if_due,daemon=True)
        self.diagnostics_worker.start()

    def offer_diagnostics(self):
        if self.closing or not self.diagnostics.should_prompt():return
        # Native/Tcl dialogs do not always have a Python Widget wrapper.
        if (self.working or self.updating or self.update_prompt_active
                or self.root.tk.call('grab','current')):
            self.root.after(3000,self.offer_diagnostics);return
        if self.diagnostics.mark_prompted():self.show_diagnostics(first_run=True)

    def show_diagnostics(self,first_run=False):
        if self.diagnostics_window and self.diagnostics_window.window.winfo_exists():
            self.diagnostics_window.window.lift();return
        from .diagnostics_ui import DiagnosticsWindow
        self.diagnostics_window=DiagnosticsWindow(
            self.root,self.diagnostics,diagnostics_config.TELEMETRY_URL,
            first_run=first_run,on_change=self.send_diagnostics)

    def help_with_form(self):
        if self.support_window and self.support_window.window.winfo_exists():
            self.support_window.window.lift();return
        from .diagnostics_ui import SupportWindow
        self.support_window=SupportWindow(self.root,diagnostics_config.SUPPORT_EMAIL)

    def offer_update(self):
        if self.closing or not self.pending_update:return
        windows=[getattr(self,name,None) for name in ('review_window','problem_window','history_window','diagnostics_window','support_window')]
        if (self.worker and self.worker.is_alive()) or self.updating or any(w and w.window.winfo_exists() for w in windows):
            self.root.after(2000,self.offer_update);return
        release=self.pending_update;self.pending_update=None
        self.update_prompt_active=True
        try:
            accepted=messagebox.askyesno('Доступно обновление',f'Доступна версия {release.tag.lstrip("v")} (установлена {__version__}).\n\nСкачать, установить и перезапустить программу сейчас?\nИсследования, таблица и выбранная рабочая папка сохранятся.',parent=self.root)
        finally:self.update_prompt_active=False
        if not accepted:return
        self.updating=True;self.update_cancel.clear();self.busy(True)
        self.start_button.pack_forget();self.review_button.pack_forget();self.new_button.pack_forget()
        self.stop_button.config(text='Отменить обновление');self.stop_button.pack(side='right')
        self.progress.pack(fill='x',before=self.content,pady=10);self.progress['maximum']=100;self.progress['value']=0
        self.phase.set('Скачиваем обновление…')
        def download():
            try:
                stage=updater.prepare_update(release,self.update_cancel,lambda value:self.events.put(('update_progress',value)))
                self.events.put(('update_ready',stage))
            except Exception as exc:
                message=str(exc) if isinstance(exc,updater.UpdateError) else 'Не удалось скачать обновление. Программа и данные сохранены.'
                self.events.put(('update_error',message))
        threading.Thread(target=download,daemon=False).start()

    def finish_update_attempt(self,message):
        self.updating=False;self.busy(False);self.stop_button.config(text='Остановить');self.show_documents();self.phase.set(message)
        if self.closing:self.root.destroy()

    def open_folder(self,name):
        if self.workspace:open_path(self.workspace.root/name)

    def open_workbook(self):
        if not self.workspace or self.workspace_busy():return
        if self.workspace.summary().get('pending_export'):
            self.show_documents();self.phase.set('Сначала сохраните актуальную таблицу. Закройте Excel и нажмите «Сохранить таблицу».');return
        path=self.workspace.root/'Исследования.xlsx'
        if path.exists():
            try:open_path(path)
            except OSError:messagebox.showerror('Не удалось открыть таблицу','Откройте Исследования.xlsx в папке данных с помощью Excel или другой программы для таблиц.',parent=self.root)

    def details(self):
        from .help_view import open_help
        menu=tk.Menu(self.root,tearoff=False)
        menu.add_command(label='Инструкция',command=lambda:open_help(self.root))
        menu.add_command(label='Диагностика…',command=self.show_diagnostics)
        menu.add_command(label='Помочь с новым бланком…',command=self.help_with_form)
        menu.tk_popup(self.help_button.winfo_rootx(),self.help_button.winfo_rooty()+self.help_button.winfo_height())

    def review(self):
        if not self.workspace or self.workspace_busy() or self.workspace_window_open():return
        if self.workspace.summary()['review']:
            self.review_window=ReviewWindow(self.root,self.workspace,self.refresh,self.icons,
                                            on_next=self.review,actions=self.workspace_actions)
        else:
            from .quality_ui import QualityReviewWindow
            self.review_window=QualityReviewWindow(self.root,self.workspace,self.refresh,self.icons,
                                                   actions=self.workspace_actions)

    def history(self):
        if self.workspace and not self.workspace_busy() and not self.workspace_window_open():
            from .quality_ui import HistoryWindow
            self.history_window=HistoryWindow(self.root,self.workspace,self.refresh,self.icons,
                                              actions=self.workspace_actions)

    def close(self):
        if self.workspace_actions.active or self.workspace_actions.presenting:
            self.closing=True
            if self.reparsing:self.cancel.set()
            self.phase.set('Закроем программу после завершения текущего действия…')
        elif self.updating:
            if messagebox.askyesno('Скачивается обновление','Отменить загрузку и закрыть программу?',parent=self.root):self.closing=True;self.update_cancel.set()
        elif self.working:
            if messagebox.askyesno('Идёт обработка','Завершить текущий файл, сохранить результат и закрыть программу?',parent=self.root):self.closing=True;self.stop()
        else:self.root.destroy()

    def run(self):self.root.mainloop()


class ReviewWindow:
    def __init__(self,parent,workspace,on_change,icons=None,on_next=None,actions=None):
        self.workspace=workspace;self.on_change=on_change;self.selected=None
        self.on_next=on_next
        self.icons=icons or ui.Icons(parent);self.preview_document=None;self.page=0;self.zoom=1.0
        self.preview_image=None;self.preview_photo=None;self.preview_render_job=None
        self.window=tk.Toplevel(parent);self.window.title('Уточнение пациента');self.window.configure(bg='white')
        self.actions=actions or ui.WorkspaceActions(self.window)
        self.saving=False
        self.window.geometry(f'{min(1240,parent.winfo_screenwidth()-50)}x{min(850,parent.winfo_screenheight()-130)}+25+25')
        self.window.minsize(850,620);self.window.transient(parent);self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.bind('<Return>',lambda event:self.confirm())
        self.window.bind('<Destroy>',self.destroy_preview,add='+')
        outer=ttk.Frame(self.window,padding=22);outer.pack(fill='both',expand=True)
        self.title=ttk.Label(outer,text='Кому принадлежит документ?',style='Heading.TLabel');self.title.pack(anchor='w',pady=(0,12))
        self.bulk_button=ttk.Button(outer,text='',style='Primary.TButton',command=self.open_bulk)
        self.window.bind('<Configure>',lambda e:self.title.configure(wraplength=max(500,e.width-44)) if e.widget is self.window else None)
        self.list_frame=ttk.Frame(outer);list_frame=self.list_frame;list_frame.pack(fill='x')
        self.list=ttk.Treeview(list_frame,columns=('file','reason'),show='headings',height=3,selectmode='browse')
        self.list.heading('file',text='Документ');self.list.heading('reason',text='Что проверить')
        self.list.column('file',width=330);self.list.column('reason',width=600)
        list_scroll=ttk.Scrollbar(list_frame,orient='vertical',command=self.list.yview)
        self.list.configure(yscrollcommand=list_scroll.set)
        list_scroll.pack(side='right',fill='y');self.list.pack(side='left',fill='x',expand=True);self.list.bind('<<TreeviewSelect>>',self.load)
        self.panes=ttk.Panedwindow(outer,orient='horizontal');self.panes.pack(fill='both',expand=True,pady=(18,0))
        self.left=ttk.Frame(self.panes);self.right=ttk.Frame(self.panes,padding=(20,0,0,0))
        self.panes.add(self.left,weight=3);self.panes.add(self.right,weight=2)
        toolbar=ttk.Frame(self.left);toolbar.pack(fill='x')
        self.preview_title=ttk.Label(toolbar,text='Документ',font=(ui.FONT,12,'bold'));self.preview_title.pack(side='left')
        self.original_button=ttk.Button(toolbar,text='Оригинал',style='Link.TButton',command=self.open_original);self.original_button.pack(side='right')
        self.page_label=tk.StringVar();self.preview_nav=ttk.Frame(self.left)
        self.preview_nav.pack(fill='x',pady=8)
        ttk.Button(self.preview_nav,image=self.icons.get('arrow-left','blue',18),command=lambda:self.turn_page(-1),width=3).pack(side='left')
        ttk.Label(self.preview_nav,textvariable=self.page_label).pack(side='left',padx=10)
        ttk.Button(self.preview_nav,image=self.icons.get('arrow-right','blue',18),command=lambda:self.turn_page(1),width=3).pack(side='left')
        ttk.Button(self.preview_nav,image=self.icons.get('zoom-in','blue',18),command=lambda:self.resize_preview(0.25),width=3).pack(side='right')
        ttk.Button(self.preview_nav,image=self.icons.get('zoom-out','blue',18),command=lambda:self.resize_preview(-0.25),width=3).pack(side='right')
        self.preview_area=ttk.Frame(self.left);self.preview_area.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(self.preview_area,bg='#f1f4f8',highlightthickness=0)
        scroll=ttk.Scrollbar(self.preview_area,orient='vertical',command=self.canvas.yview);scroll.pack(side='right',fill='y')
        xscroll=ttk.Scrollbar(self.preview_area,orient='horizontal',command=self.canvas.xview);xscroll.pack(side='bottom',fill='x')
        self.canvas.configure(yscrollcommand=scroll.set,xscrollcommand=xscroll.set)
        self.text=tk.Text(self.preview_area,wrap='word',font=(ui.FONT,12),padx=12,pady=12,relief='flat',width=35)
        self.text.configure(yscrollcommand=scroll.set)
        self.text_scroll=scroll
        self.confirm_button=ttk.Button(self.right,text='Подтвердить пациента',style='Primary.TButton',command=self.confirm)
        self.confirm_button.pack(side='bottom',fill='x',pady=(10,0))
        self.editor_area=ui.ScrollArea(self.right);self.editor_area.pack(fill='both',expand=True)
        self.candidate_area=ttk.Frame(self.editor_area.body);self.candidate_area.pack(fill='x')
        self.candidates=[];self.candidate_buttons=[]
        fields=ttk.Frame(self.editor_area.body);fields.pack(fill='x',pady=12)
        ttk.Label(fields,text='ФИО полностью').pack(anchor='w')
        self.name=tk.StringVar();self.birth=tk.StringVar()
        self.name_entry=ttk.Entry(fields,textvariable=self.name);self.name_entry.pack(fill='x',pady=(6,12))
        ttk.Label(fields,text='Дата рождения').pack(anchor='w')
        self.birth_entry=ttk.Entry(fields,textvariable=self.birth);self.birth_entry.pack(fill='x',pady=(6,4))
        ttk.Label(fields,text='ДД.ММ.ГГГГ',style='Muted.TLabel',font=(ui.FONT,10)).pack(anchor='w')
        self.validation=tk.StringVar();ttk.Label(fields,textvariable=self.validation,foreground='#b42318',wraplength=330).pack(anchor='w',pady=6)
        for entry in (self.name_entry,self.birth_entry):
            entry.bind('<FocusIn>',lambda e:self.editor_area.canvas.yview_moveto(1))
        self.success=ttk.Frame(outer,padding=40)
        ttk.Label(self.success,image=self.icons.get('check-circle','green',64)).pack(pady=15)
        ttk.Label(self.success,text='Пациенты уточнены',style='Heading.TLabel').pack(pady=8)
        self.success_message=ttk.Label(self.success,text='Изменения сохранены в таблице',style='Muted.TLabel');self.success_message.pack(pady=8)
        self.next_button=ttk.Button(self.success,text='Вернуться к результатам',style='Primary.TButton',command=self.close);self.next_button.pack(pady=20)
        self.fill()

    def close(self):
        if self.saving:
            self.validation.set('Дождитесь сохранения решения.')
            return
        self.window.destroy()

    def destroy_preview(self,event):
        if event.widget is self.window:
            self.close_preview()
            self.preview_photo=None

    def close_preview(self):
        if self.preview_render_job is not None:
            self.window.after_cancel(self.preview_render_job)
            self.preview_render_job=None
        if self.preview_document is not None:
            with PDFIUM_LOCK:self.preview_document.close()
            self.preview_document=None
        self.preview_image=None

    def clear_preview_canvas(self):
        self.canvas.delete('all')
        self.canvas.configure(scrollregion=(0,0,0,0))
        self.preview_photo=None

    def fill(self,summary=None):
        self.selected=None;self.name.set('');self.birth.set('');self.validation.set('')
        self.confirm_button.configure(state='disabled');self.original_button.configure(state='disabled')
        self.records={row['id']:row for row in self.workspace.review_list()}
        self.groups=review_queue.patient_groups(self.records.values())
        count=sum(len(group['documents']) for group in self.groups)
        self.bulk_button.pack_forget()
        if count>1:
            self.bulk_button.configure(text=f'Подтвердить найденных пациентов списком · документов: {count}')
            self.bulk_button.pack(anchor='w',before=self.list_frame,pady=(0,12))
        self.list.configure(height=min(2,max(1,len(self.records))))
        for item in self.list.get_children():self.list.delete(item)
        for record in self.records.values():self.list.insert('','end',iid=record['id'],values=(record['filename'],ui.reason_text(record)))
        if self.records:
            self.list.selection_set(next(iter(self.records)));self.load()
            self.title.configure(text=f'Кому принадлежит документ?  Осталось: {len(self.records)}')
        else:
            if self.on_next and (summary if summary is not None else self.workspace.summary())['quality_review']:
                self.success_message.configure(text='Теперь сверьте результаты исследований с оригиналами')
                self.next_button.configure(text='Проверить данные',command=self.next_review)
            self.list_frame.pack_forget();self.panes.pack_forget();self.title.pack_forget();self.bulk_button.pack_forget();self.success.pack(fill='both',expand=True)

    def open_bulk(self):
        if self.saving or not self.groups:return
        self.bulk_dialog=BulkPatientDialog(self.window,self.groups,self.save_bulk)

    def save_bulk(self,assignments,dialog):
        workspace=self.workspace

        def completed(summary,error):
            self.set_saving(False)
            dialog.close()
            if isinstance(error,ReviewCommittedError):
                messagebox.showwarning('Пациенты подтверждены',str(error),parent=self.window)
            elif error is not None:
                messagebox.showerror('Не удалось подтвердить',str(error),parent=self.window)
            self.on_change()
            self.fill(summary if error is None else None)

        if self.actions.run(lambda:workspace.assign_many(assignments),completed):
            self.set_saving(True)
            return True
        return False

    def next_review(self):
        if self.saving:return
        parent=self.window.master;self.close();parent.after_idle(self.on_next)

    def load(self,event=None):
        selection=self.list.selection()
        if self.saving:
            if selection!=(self.selected,):self.list.selection_set(self.selected)
            return
        if not selection or selection[0] not in self.records:return
        if selection[0]==self.selected:return
        self.selected=selection[0];record=self.records[self.selected]
        document=self.workspace.document(self.selected);analysis=json.loads(document['analysis'])
        suggestion=review_queue.patient_suggestion(record)
        found=suggestion[0] if suggestion else (None,None)
        name=analysis['names'][0] if len(analysis['names'])==1 else found[0]
        birth=analysis['births'][0] if len(analysis['births'])==1 else found[1]
        self.name.set(name or '')
        self.birth.set(datetime.strptime(birth,'%Y-%m-%d').strftime('%d.%m.%Y') if birth else '')
        self.validation.set('');self.confirm_button.configure(state='normal');self.original_button.configure(state='normal')
        for child in self.candidate_area.winfo_children():child.destroy()
        self.candidate_buttons=[]
        self.editor_area.canvas.yview_moveto(0);self.candidates=record['candidates']
        ttk.Label(self.candidate_area,text='Возможные совпадения' if self.candidates else 'Введите данные из оригинала',font=(ui.FONT,12,'bold')).pack(anchor='w',pady=(0,10))
        for name,birth in self.candidates:
            day=datetime.strptime(birth,'%Y-%m-%d').strftime('%d.%m.%Y')
            frame=ttk.Frame(self.candidate_area,padding=(0,4));frame.pack(fill='x')
            button=ttk.Button(frame,text='\n'.join(textwrap.wrap(name,24))+'\n'+day,
                              command=lambda n=name,b=birth:self.use_candidate(n,b))
            button.pack(fill='x')
            self.candidate_buttons.append(button)
            basis=[]
            if birth in record['births']:basis.append('дата рождения')
            if name in record['names']:basis.append('ФИО')
            ttk.Label(frame,text='Совпадает: '+', '.join(basis) if basis else 'Предложен по соседним документам — проверьте',style='Muted.TLabel',wraplength=330).pack(anchor='w',pady=(4,8))
        self.show_preview(document,analysis)

    def use_candidate(self,name,birth):
        if self.saving:return
        self.name.set(name);self.birth.set(datetime.strptime(birth,'%Y-%m-%d').strftime('%d.%m.%Y'));self.validation.set('')
        self.editor_area.canvas.yview_moveto(1)

    def show_preview(self,document,analysis):
        from PIL import Image
        self.close_preview()
        self.clear_preview_canvas()
        self.page=0;self.zoom=1.0;path=Path(document['original'])
        self.page_label.set('')
        self.canvas.pack_forget();self.text.pack_forget();self.preview_nav.pack_forget()
        self.text.configure(state='normal');self.text.delete('1.0','end')
        self.text.configure(state='disabled')
        try:
            if path.suffix.lower()=='.pdf':
                import pypdfium2 as pdfium
                with PDFIUM_LOCK:self.preview_document=pdfium.PdfDocument(path)
            elif path.suffix.lower() in {'.png','.jpg','.jpeg','.tif','.tiff','.bmp'}:
                with Image.open(path) as image:self.preview_image=image.convert('RGB')
            else:raise ValueError('text preview')
            self.preview_title.configure(text='Оригинал документа');self.preview_nav.pack(fill='x',before=self.preview_area,pady=8)
            self.canvas.pack(fill='both',expand=True);self.text_scroll.configure(command=self.canvas.yview)
            self.preview_render_job=self.window.after_idle(self.render_scheduled_preview)
        except Exception:
            self.close_preview()
            self.preview_title.configure(text='Извлечённый текст')
            self.text.configure(state='normal');self.text.insert('1.0','\n\n'.join(analysis['pages']))
            self.text.configure(state='disabled')
            self.text.pack(fill='both',expand=True);self.text_scroll.configure(command=self.text.yview)

    def render_scheduled_preview(self):
        self.preview_render_job=None
        self.render_preview()

    def render_preview(self):
        from PIL import Image,ImageTk
        try:
            width=max(300,self.canvas.winfo_width()-24)
            if self.preview_document is not None:
                with PDFIUM_LOCK:
                    page=self.preview_document[self.page]
                    try:
                        bitmap=page.render(scale=min(3,width/page.get_width()*self.zoom))
                        try:image=bitmap.to_pil().copy()
                        finally:bitmap.close()
                    finally:page.close()
                    count=len(self.preview_document)
            else:
                image=self.preview_image.copy();count=1
                image=image.resize((int(width*self.zoom),max(1,int(image.height*width*self.zoom/image.width))),Image.Resampling.LANCZOS)
            self.preview_photo=ImageTk.PhotoImage(image,master=self.window)
            self.canvas.delete('all');self.canvas.create_image(12,8,anchor='nw',image=self.preview_photo)
            self.canvas.configure(scrollregion=(0,0,image.width+24,image.height+16));self.canvas.yview_moveto(0)
            self.page_label.set(f'{self.page+1} / {count}')
        except Exception:
            self.clear_preview_canvas()
            self.page_label.set('Откройте оригинал')

    def turn_page(self,delta):
        if self.preview_document is not None:
            with PDFIUM_LOCK:self.page=max(0,min(len(self.preview_document)-1,self.page+delta))
            self.render_preview()

    def resize_preview(self,delta):self.zoom=max(.5,min(2.5,self.zoom+delta));self.render_preview()

    def open_original(self):
        if self.selected:open_path(self.workspace.document(self.selected)['original'])

    def set_saving(self,saving):
        self.saving=saving
        state='disabled' if saving else 'normal'
        self.list.configure(selectmode='none' if saving else 'browse')
        for button in [self.confirm_button,self.name_entry,self.birth_entry,*self.candidate_buttons]:
            button.configure(state=state)
        self.confirm_button.configure(text='Сохраняем…' if saving else 'Подтвердить пациента')
        self.validation.set('Сохраняем решение и таблицу…' if saving else '')

    def confirmation_error(self,error):
        self.validation.set('Проверьте ФИО и дату (ДД.ММ.ГГГГ).\n'+str(error))
        if not self.name.get().strip():self.name_entry.focus_set()
        else:self.birth_entry.focus_set()

    def complete_confirmation(self,summary,error):
        self.set_saving(False)
        if isinstance(error,ValueError):
            self.confirmation_error(error)
            return
        if error is not None:
            messagebox.showerror('Не удалось сохранить таблицу',str(error),parent=self.window)
        self.on_change()
        self.fill(summary)

    def confirm(self):
        if self.saving or not self.selected or self.selected not in self.records:return
        try:
            birth=datetime.strptime(self.birth.get().strip(),'%d.%m.%Y').date()
            if birth>datetime.now().date():raise ValueError('Дата рождения не может быть в будущем.')
        except ValueError as exc:
            self.confirmation_error(exc)
            return
        workspace,document_id,name=self.workspace,self.selected,self.name.get()
        birth_iso=birth.isoformat()
        if self.actions.run(lambda:workspace.assign(document_id,name,birth_iso),self.complete_confirmation):
            self.set_saving(True)


class BulkPatientDialog:
    """One decision for every document of a patient. Weak suggestions start unselected."""

    def __init__(self,parent,groups,on_save):
        self.groups=groups;self.on_save=on_save
        self.window=tk.Toplevel(parent);self.window.title('Подтвердить пациентов списком');self.window.configure(bg='white')
        self.window.geometry('860x520');self.window.minsize(640,380);self.window.transient(parent);self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        outer=ttk.Frame(self.window,padding=20);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Подтвердите пациентов, найденных в документах',style='Heading.TLabel').pack(anchor='w')
        ttk.Label(outer,text='Выделенные строки будут подтверждены. Документы без ФИО и даты рождения предложены по соседним документам папки: выделите их, только если уверены.',style='Muted.TLabel',wraplength=800).pack(anchor='w',pady=(6,12))
        buttons=ttk.Frame(outer);buttons.pack(side='bottom',fill='x',pady=(12,0))
        self.save_button=ttk.Button(buttons,text='',style='Primary.TButton',command=self.save);self.save_button.pack(side='right')
        ttk.Button(buttons,text='Отмена',command=self.close).pack(side='left')
        area=ttk.Frame(outer);area.pack(fill='both',expand=True)
        self.list=ttk.Treeview(area,columns=('name','birth','count','basis'),show='headings',selectmode='extended')
        for key,title,width in [('name','Пациент',380),('birth','Дата рождения',120),('count','Документов',90),('basis','Найдено по',190)]:
            self.list.heading(key,text=title);self.list.column(key,width=width,stretch=key=='name')
        scroll=ttk.Scrollbar(area,orient='vertical',command=self.list.yview);self.list.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.list.pack(side='left',fill='both',expand=True)
        strong=[]
        for index,group in enumerate(groups):
            day=datetime.strptime(group['birth'],'%Y-%m-%d').strftime('%d.%m.%Y')
            self.list.insert('','end',iid=str(index),values=(group['name'],day,len(group['documents']),group['basis']))
            if group['basis']!=review_queue.FOLDER_BASIS:strong.append(str(index))
        self.list.bind('<<TreeviewSelect>>',self.update_button)
        self.list.selection_set(strong)
        self.update_button()

    def chosen(self):
        return [self.groups[int(item)] for item in self.list.selection()]

    def update_button(self,event=None):
        count=sum(len(group['documents']) for group in self.chosen())
        self.save_button.configure(text=f'Подтвердить · документов: {count}',state='normal' if count else 'disabled')

    def save(self):
        assignments=[(document,group['name'],group['birth']) for group in self.chosen() for document in group['documents']]
        if assignments and self.on_save(assignments,self):
            self.save_button.configure(state='disabled',text='Сохраняем…')

    def close(self):
        if self.window.winfo_exists():
            parent=self.window.master;self.window.destroy();parent.grab_set()


class ProblemsWindow:
    def __init__(self,parent,workspace,icons,on_retry):
        self.workspace=workspace;self.on_retry=on_retry
        self.window=tk.Toplevel(parent);self.window.title('Файлы, требующие внимания');self.window.configure(bg='white')
        self.window.geometry('850x580');self.window.transient(parent);self.window.grab_set()
        outer=ttk.Frame(self.window,padding=22);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Разберём проблемные файлы',style='Heading.TLabel').pack(anchor='w',pady=(0,16))
        self.area=ui.ScrollArea(outer);self.area.pack(fill='both',expand=True)
        for item in workspace.problems():
            row=ttk.Frame(self.area.body,padding=(0,12));row.pack(fill='x')
            ttk.Label(row,text=Path(item['source']).name,font=(ui.FONT,13,'bold')).pack(anchor='w')
            ttk.Label(row,text=ui.file_error(item['error']) if item['category']=='Ошибки' else 'Данные сохранены, но файл ещё не перемещён. Закройте открытые документы и повторите обработку.',wraplength=730,style='Muted.TLabel').pack(anchor='w',pady=8)
            buttons=ttk.Frame(row);buttons.pack(fill='x')
            path=Path(item['destination']) if item['destination'] and Path(item['destination']).exists() else Path(item['source'])
            ttk.Button(buttons,text='Открыть файл',command=lambda p=path:open_path(p)).pack(side='left')
            ttk.Button(buttons,text='Подробнее',style='Link.TButton',command=lambda i=item:messagebox.showinfo('Подробности',i['error'] or 'Файл ожидает перемещения.',parent=self.window)).pack(side='left',padx=8)
            ttk.Button(buttons,text='Повторить',command=lambda i=item:self.retry(i)).pack(side='right')
            ttk.Separator(self.area.body).pack(fill='x')
        ttk.Button(outer,text='Закрыть',command=self.window.destroy).pack(anchor='e',pady=(14,0))
    def retry(self,item):
        try:
            if item['category']=='Ошибки':intake.retry_file(self.workspace,item['id'])
            self.window.destroy();self.on_retry()
        except Exception as exc:messagebox.showerror('Не удалось повторить',str(exc),parent=self.window)
