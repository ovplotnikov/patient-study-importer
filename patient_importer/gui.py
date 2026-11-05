"""One-window desktop workflow, with patient confirmation inside the app."""
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog,messagebox,ttk
from tkinter import font as tkfont
import traceback

from .storage import Workspace


def open_path(path):
    if os.name=='nt':os.startfile(str(path))
    elif sys.platform=='darwin':subprocess.Popen(['open',str(path)])
    else:subprocess.Popen(['xdg-open',str(path)])


class Application:
    def __init__(self):
        self.root=tk.Tk();self.root.title('Исследования пациентов');self.root.geometry(f"{min(980,self.root.winfo_screenwidth()-60)}x{min(720,self.root.winfo_screenheight()-100)}+20+20");self.root.minsize(820,600)
        tkfont.nametofont('TkDefaultFont').configure(family='Segoe UI' if os.name=='nt' else 'Helvetica',size=11)
        style=ttk.Style();style.configure('Title.TLabel',font=('Segoe UI',22,'bold'));style.configure('Action.TButton',padding=(20,13));style.configure('TButton',padding=(10,7))
        self.events=queue.Queue();self.worker=None;self.cancel=threading.Event();self.closing=False
        self.settings=Path(os.environ.get('APPDATA',str(Path.home()/'.config')))/'PatientStudyImporter'/'settings.json'
        self.workspace=None
        frame=ttk.Frame(self.root,padding=20);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='Исследования пациентов',style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame,text='Добавьте файлы в рабочую папку и нажмите «Обработать».',wraplength=870).pack(anchor='w',pady=(8,22))
        folder=ttk.Frame(frame);folder.pack(fill='x')
        self.folder_var=tk.StringVar();ttk.Entry(folder,textvariable=self.folder_var,state='readonly').pack(side='left',fill='x',expand=True,padx=(0,10))
        self.choose_button=ttk.Button(folder,text='Выбрать папку…',command=self.choose);self.choose_button.pack(side='right')
        actions=ttk.Frame(frame);actions.pack(fill='x',pady=12)
        self.start_button=ttk.Button(actions,text='Обработать',style='Action.TButton',command=self.start);self.start_button.pack(side='left')
        self.stop_button=ttk.Button(actions,text='Остановить',command=self.stop,state='disabled');self.stop_button.pack(side='left',padx=10)
        ttk.Button(actions,text='Открыть «Входящие»',command=lambda:self.open_folder('Входящие')).pack(side='right')
        self.phase=tk.StringVar(value='Выберите папку с исследованиями или используйте предложенную ниже.')
        ttk.Label(frame,textvariable=self.phase,wraplength=870).pack(anchor='w',pady=(8,8))
        self.progress=ttk.Progressbar(frame,mode='determinate');self.progress.pack(fill='x')
        self.current=tk.StringVar();ttk.Label(frame,textvariable=self.current,wraplength=870).pack(anchor='w',pady=(6,12))
        self.metrics=tk.StringVar();ttk.Label(frame,textvariable=self.metrics,font=('Segoe UI',12),wraplength=870).pack(anchor='w',pady=(5,18))
        results=ttk.Frame(frame);results.pack(fill='x')
        self.excel_button=ttk.Button(results,text='Открыть таблицу',command=self.open_workbook);self.excel_button.pack(side='left')
        self.review_button=ttk.Button(results,text='Уточнить пациентов',command=self.review);self.review_button.pack(side='left',padx=10)
        ttk.Button(results,text='Открыть рабочую папку',command=lambda:self.open_folder('')).pack(side='left')
        ttk.Separator(frame).pack(fill='x',pady=16)
        help_text=('Файлы из корня и папки «Входящие» обрабатываются вместе с подпапками.\n\n'
                   '«Обработано» — данные сохранены. «Нужно уточнить» — выберите пациента в программе. '
                   '«Дубли» — повторные файлы. «Ошибки» — файлы, которые не удалось прочитать.\n\n'
                   'Новые исследования добавляются в ту же таблицу. Колонка «Заметки», ваши дополнительные колонки и отдельные листы сохраняются.')
        ttk.Label(frame,text=help_text,wraplength=870,justify='left').pack(anchor='w')
        bottom=ttk.Frame(frame);bottom.pack(side='bottom',fill='x',pady=(15,0))
        ttk.Button(bottom,text='Сведения и журнал',command=self.details).pack(side='left')
        ttk.Label(bottom,text='Локальная обработка · версия 2.0',foreground='#65727c').pack(side='right')
        default=Path.home()/'Documents'/'Исследования пациентов'
        portable=Path(sys.executable).parent/'Данные'
        if getattr(sys,'frozen',False) and portable.is_dir():default=portable
        if self.settings.exists():
            try:
                saved=Path(json.loads(self.settings.read_text(encoding='utf-8'))['workspace'])
                if saved.is_dir():default=saved
            except (OSError,ValueError,KeyError):pass
        self.select(default)
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.after(100,self.poll)

    def select(self,path):
        try:
            self.workspace=Workspace(path);self.folder_var.set(str(self.workspace.root))
            self.settings.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.settings.with_suffix('.tmp');temporary.write_text(json.dumps({'workspace':str(self.workspace.root)},ensure_ascii=False),encoding='utf-8');temporary.replace(self.settings)
            self.refresh()
        except Exception as exc:messagebox.showerror('Не удалось открыть папку',str(exc),parent=self.root)

    def choose(self):
        selected=filedialog.askdirectory(title='Выберите рабочую папку с исследованиями',initialdir=self.folder_var.get(),parent=self.root)
        if selected:self.select(selected)

    def refresh(self):
        if not self.workspace:return
        summary=self.workspace.summary()
        self.metrics.set(f"Документов — {summary['documents']}     Пациентов — {summary['patients']}\nУточнить пациента — {summary['review']}     Повторов — {summary['duplicates']}     Ошибок — {summary['errors']}")

    def busy(self,value):
        for button in [self.start_button,self.choose_button,self.review_button]:button.config(state='disabled' if value else 'normal')
        self.stop_button.config(state='normal' if value else 'disabled')

    def start(self):
        if not self.workspace or self.worker and self.worker.is_alive():return
        self.cancel.clear();self.busy(True);self.phase.set('Подготовка');self.current.set('');self.progress['value']=0
        workspace=self.workspace
        def work():
            try:
                summary=workspace.import_folder(progress=lambda **state:self.events.put(('progress',state)),cancel=self.cancel)
                self.events.put(('finished',summary))
            except Exception as exc:
                try:
                    with (workspace.base/'last-error.txt').open('w',encoding='utf-8') as log:traceback.print_exc(file=log)
                except OSError:pass
                self.events.put(('error',str(exc)))
        self.worker=threading.Thread(target=work,daemon=False);self.worker.start()

    def stop(self):
        self.cancel.set();self.phase.set('Остановимся после текущего файла и сохраним результат.');self.stop_button.config(state='disabled')

    def poll(self):
        try:
            while True:
                kind,data=self.events.get_nowait()
                if kind=='progress':
                    if 'phase' in data:self.phase.set(data['phase'])
                    if 'current' in data:self.current.set(data['current'])
                    if 'total' in data:self.progress['maximum']=max(1,data['total'])
                    if 'done' in data:self.progress['value']=data['done']
                    if data.get('skipped'):self.current.set(f"Пропущены файлы других форматов — {data['skipped']}")
                elif kind=='finished':
                    self.busy(False);self.refresh();self.current.set('')
                    self.phase.set(('Остановлено. ' if self.cancel.is_set() else 'Готово. ')+f"В таблице {data['documents']} документов. Уточнить пациента — {data['review']}.")
                    if data['pending_moves']:messagebox.showwarning('Файлы сохранены',f"Осталось переместить файлов — {data['pending_moves']}. Закройте открытые документы и нажмите «Обработать» ещё раз.",parent=self.root)
                    if self.closing:self.root.destroy();return
                elif kind=='error':
                    self.busy(False);self.refresh();self.phase.set('Обработка прервана. Сохранённые данные доступны при следующем запуске.')
                    messagebox.showerror('Не удалось завершить обработку',data,parent=self.root)
                    if self.closing:self.root.destroy();return
        except queue.Empty:pass
        self.root.after(100,self.poll)

    def open_folder(self,name):
        if self.workspace:open_path(self.workspace.root/name)

    def open_workbook(self):
        if not self.workspace:return
        path=self.workspace.root/'Исследования.xlsx'
        if path.exists():open_path(path)
        else:messagebox.showinfo('Таблица', 'Сначала нажмите «Обработать».',parent=self.root)

    def details(self):
        messagebox.showinfo('Как работает программа','PDF, Word DOCX, TXT, JPG, PNG, TIFF и BMP обрабатываются локально.\n\nПациент определяется по полному ФИО и дате рождения. Спорные случаи уточняются через кнопку «Уточнить пациентов». Числа из описаний сохраняются с контекстом и статусом проверки.\n\nИсходники, база и журнал находятся в рабочей папке. При повторной обработке одинаковые файлы не создают новые исследования.\n\nАвтоматические колонки обновляет программа. Для собственных записей используйте «Заметки» и дополнительные колонки.\n\nПеред обработкой закройте таблицу в Excel.',parent=self.root)

    def review(self):
        if not self.workspace:return
        self.review_window=ReviewWindow(self.root,self.workspace,self.refresh)

    def close(self):
        if self.worker and self.worker.is_alive():
            if messagebox.askyesno('Идёт обработка','Остановить после текущего файла, сохранить результат и закрыть программу?',parent=self.root):
                self.closing=True;self.stop()
        else:self.root.destroy()

    def run(self):self.root.mainloop()


class ReviewWindow:
    def __init__(self,parent,workspace,on_change):
        self.workspace=workspace;self.on_change=on_change;self.selected=None
        self.window=tk.Toplevel(parent);self.window.title('Уточнить пациентов');self.window.geometry(f"{min(1100,parent.winfo_screenwidth()-60)}x{min(760,parent.winfo_screenheight()-100)}+20+20");self.window.transient(parent);self.window.grab_set()
        outer=ttk.Frame(self.window,padding=18);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Проверьте оригинал и подтвердите, кому принадлежит документ.',wraplength=1000).pack(anchor='w',pady=(0,12))
        list_frame=ttk.Frame(outer);list_frame.pack(fill='x')
        self.list=ttk.Treeview(list_frame,columns=('file','reason'),show='headings',height=8)
        self.list.heading('file',text='Документ');self.list.heading('reason',text='Причина')
        self.list.column('file',width=300);self.list.column('reason',width=540);self.list.pack(side='left',fill='x',expand=True)
        scroll=ttk.Scrollbar(list_frame,orient='vertical',command=self.list.yview);scroll.pack(side='right',fill='y');self.list.configure(yscrollcommand=scroll.set)
        self.list.bind('<<TreeviewSelect>>',self.load)
        fields=ttk.Frame(outer);fields.pack(fill='x',pady=14)
        ttk.Label(fields,text='ФИО полностью').grid(row=0,column=0,sticky='w')
        ttk.Label(fields,text='Дата рождения · ДД.ММ.ГГГГ').grid(row=0,column=1,sticky='w',padx=10)
        self.name=tk.StringVar();self.birth=tk.StringVar()
        fields.columnconfigure(0,weight=1)
        ttk.Entry(fields,textvariable=self.name,width=43).grid(row=1,column=0,sticky='ew')
        ttk.Entry(fields,textvariable=self.birth,width=21).grid(row=1,column=1,padx=10)
        self.confirm_button=ttk.Button(fields,text='Подтвердить пациента',command=self.confirm);self.confirm_button.grid(row=1,column=2)
        candidate_row=ttk.Frame(outer);candidate_row.pack(fill='x')
        ttk.Label(candidate_row,text='Возможное совпадение').pack(side='left')
        self.candidates=ttk.Combobox(candidate_row,state='readonly',width=75);self.candidates.pack(side='left',padx=10)
        self.candidates.bind('<<ComboboxSelected>>',self.use_candidate)
        ttk.Button(outer,text='Открыть оригинал',command=self.open_original).pack(anchor='w',pady=10)
        self.text=tk.Text(outer,wrap='word',height=18);self.text.pack(fill='both',expand=True)
        self.fill()

    def fill(self):
        self.records={row['id']:row for row in self.workspace.review_list()}
        for item in self.list.get_children():self.list.delete(item)
        for item in self.records.values():self.list.insert('', 'end',iid=item['id'],values=(item['filename'],item['reason']))
        if self.records:self.list.selection_set(next(iter(self.records)))
        else:self.text.delete('1.0','end');self.text.insert('1.0','Все пациенты уточнены.')

    def load(self,event=None):
        selection=self.list.selection()
        if not selection:return
        self.selected=selection[0];record=self.records[self.selected]
        document=self.workspace.document(self.selected);a=json.loads(document['analysis'])
        self.name.set(a['names'][0] if len(a['names'])==1 else '')
        self.birth.set(datetime.strptime(a['births'][0],'%Y-%m-%d').strftime('%d.%m.%Y') if len(a['births'])==1 else '')
        self.candidate_values=record['candidates'];self.candidates['values']=[f"{name} · {datetime.strptime(birth,'%Y-%m-%d').strftime('%d.%m.%Y')}" for name,birth in self.candidate_values];self.candidates.set('')
        self.text.delete('1.0','end');self.text.insert('1.0','\n\n'.join(a['pages'])[:100000])

    def use_candidate(self,event=None):
        index=self.candidates.current()
        if index>=0:
            name,birth=self.candidate_values[index];self.name.set(name);self.birth.set(datetime.strptime(birth,'%Y-%m-%d').strftime('%d.%m.%Y'))

    def open_original(self):
        if self.selected:open_path(self.workspace.document(self.selected)['original'])

    def confirm(self):
        if not self.selected:return
        try:
            birth=datetime.strptime(self.birth.get().strip(),'%d.%m.%Y').date().isoformat()
            self.workspace.assign(self.selected,self.name.get(),birth)
            self.on_change();self.fill()
        except ValueError as exc:messagebox.showerror('Проверьте данные','Укажите полное ФИО и дату рождения в формате ДД.ММ.ГГГГ.\n'+str(exc),parent=self.window)
        except Exception as exc:messagebox.showerror('Не удалось обновить таблицу',str(exc),parent=self.window)
