"""Explicit source-to-result review. No confidence score substitutes for consent."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from . import ui
from .pdf_render import PDFIUM_LOCK
from .storage import ReviewCommittedError


def display_date(value):
    try:return datetime.strptime(value,'%Y-%m-%d').strftime('%d.%m.%Y')
    except (ValueError,TypeError):return value or ''


def parse_date(value):
    value=value.strip()
    if not value:return ''
    for pattern in ('%d.%m.%Y','%Y-%m-%d'):
        try:return datetime.strptime(value,pattern).date().isoformat()
        except ValueError:pass
    raise ValueError('Дата должна быть в формате ДД.ММ.ГГГГ.')


class SourcePreview(ttk.Frame):
    """Show the immutable original; label text-only fallbacks honestly."""
    CACHE_PAGES = 2
    CACHE_PIXELS = 8_000_000
    MAX_DIMENSION = 8192

    def __init__(self,parent,icons):
        super().__init__(parent)
        self.icons=icons;self.document=None;self.analysis={};self.pdf=None;self.image_path=None
        self.page=0;self.zoom=1.;self.highlight=None;self.page_count=1
        self.photos=OrderedDict();self.displayed=None;self.evidence={}
        bar=ttk.Frame(self);bar.pack(fill='x')
        self.title=ttk.Label(bar,text='Оригинал',font=(ui.FONT,12,'bold'));self.title.pack(side='left')
        ttk.Button(bar,text='Открыть файл',style='Link.TButton',command=self.open_original).pack(side='right')
        nav=ttk.Frame(self);nav.pack(fill='x',pady=(0,6))
        ttk.Button(nav,text='‹',width=2,command=lambda:self.turn(-1)).pack(side='left')
        self.page_label=ttk.Label(nav,text='');self.page_label.pack(side='left',padx=8)
        ttk.Button(nav,text='›',width=2,command=lambda:self.turn(1)).pack(side='left')
        ttk.Button(nav,text='−',width=2,command=lambda:self.resize(-.25)).pack(side='right')
        ttk.Button(nav,text='+',width=2,command=lambda:self.resize(.25)).pack(side='right')
        body=ttk.Frame(self);body.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(body,bg='#f1f4f8',highlightthickness=0)
        self.text=tk.Text(body,wrap='word',width=28,font=(ui.FONT,11),padx=12,pady=12,relief='flat',bg='white',fg=ui.INK)
        self.text.tag_configure('source',background='#fff0ba')
        self.scroll=ttk.Scrollbar(body,orient='vertical',command=self.canvas.yview);self.scroll.pack(side='right',fill='y')
        self.xscroll=ttk.Scrollbar(body,orient='horizontal',command=self.canvas.xview);self.xscroll.pack(side='bottom',fill='x')
        self.canvas.configure(yscrollcommand=self.scroll.set,xscrollcommand=self.xscroll.set)
        self.text.configure(yscrollcommand=self.scroll.set)
        self.resize_job=None;self.canvas.bind('<Configure>',self.schedule_render)
        self.bind('<Destroy>',self.destroy_preview)

    def destroy_preview(self,event):
        if event.widget is self:
            if self.resize_job:self.after_cancel(self.resize_job);self.resize_job=None
            self.close_source()
            self.photos.clear()

    def close_source(self):
        if self.pdf is not None:
            with PDFIUM_LOCK:
                self.pdf.close()
            self.pdf=None
        self.image_path=None

    def schedule_render(self,event=None):
        if self.resize_job:self.after_cancel(self.resize_job)
        def render():
            self.resize_job=None
            if self.document:self.render()
        self.resize_job=self.after(100,render) if event else self.after_idle(render)

    def load(self,document,analysis):
        if self.resize_job:self.after_cancel(self.resize_job);self.resize_job=None
        self.close_source()
        self.document=document;self.analysis=analysis;self.page=0;self.zoom=1.;self.highlight=None
        self.page_count=max(1,len(analysis.get('pages',[])))
        self.canvas.delete('all');self.photos.clear();self.displayed=None
        self.canvas.pack_forget();self.text.pack_forget()
        self.evidence={}
        for item in analysis.get('ocr_evidence',[]):
            if item.get('source_kind',item.get('source')) in {'page','image'}:
                self.evidence.setdefault((item.get('page'),item.get('line')),[]).append(item)
        path=Path(document['original'])
        try:
            if path.suffix.lower()=='.pdf':
                import pypdfium2 as pdfium
                with PDFIUM_LOCK:
                    self.pdf=pdfium.PdfDocument(path)
                    self.page_count=max(1,len(self.pdf))
            elif path.suffix.lower() in {'.png','.jpg','.jpeg','.tif','.tiff','.bmp'}:
                from PIL import Image
                with Image.open(path) as image:
                    self.page_count=getattr(image,'n_frames',1)
                self.image_path=path
        except Exception:self.close_source()
        if self.has_image():
            self.title.configure(text='Оригинал');self.canvas.pack(fill='both',expand=True)
            self.scroll.configure(command=self.canvas.yview);self.xscroll.pack(side='bottom',fill='x')
            self.schedule_render()
        else:
            self.show_text()

    def show_text(self):
        self.title.configure(text='Извлечённый текст · откройте оригинал')
        self.canvas.pack_forget();self.xscroll.pack_forget()
        self.text.pack(fill='both',expand=True);self.scroll.configure(command=self.text.yview)
        self.render()

    def has_image(self):return self.pdf is not None or self.image_path is not None

    def count(self):return self.page_count

    def turn(self,delta):self.page=max(0,min(self.count()-1,self.page+delta));self.highlight=None;self.render()
    def resize(self,delta):self.zoom=max(.5,min(3.,self.zoom+delta));self.render()

    def image_scale(self,width,height,target):
        # PDFium rounds both output dimensions up to full pixels.
        pixels=self.CACHE_PIXELS-2*self.MAX_DIMENSION-1
        return min(target/width,self.MAX_DIMENSION/max(width,height),(pixels/(width*height))**.5)

    def render_photo(self,target):
        from PIL import Image,ImageOps,ImageTk
        if self.pdf is not None:
            with PDFIUM_LOCK:
                page=self.pdf[self.page]
                try:
                    scale=min(4,self.image_scale(*page.get_size(),target))
                    bitmap=page.render(scale=scale)
                    try:
                        image=bitmap.to_pil().copy()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        else:
            with Image.open(self.image_path) as source:
                source.seek(self.page)
                ImageOps.exif_transpose(source,in_place=True)
                original=source if source.mode=='RGB' else source.convert('RGB')
                scale=self.image_scale(*original.size,target)
                size=tuple(max(1,int(dimension*scale)) for dimension in original.size)
                image=original.resize(size,Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image,master=self)

    def render(self):
        self.page_label.configure(text=f'{self.page+1} / {self.count()}')
        try:
            if not self.has_image():
                if self.displayed!=self.page:
                    pages=self.analysis.get('pages',[])
                    self.text.configure(state='normal');self.text.delete('1.0','end')
                    self.text.insert('1.0',pages[self.page] if self.page<len(pages) else '')
                    self.text.configure(state='disabled');self.displayed=self.page
            else:
                target=int(max(300,self.canvas.winfo_width()-24)*self.zoom)
                key=(self.page,target)
                if key!=self.displayed:
                    self.canvas.delete('all');self.displayed=None
                    photo=self.photos.get(key)
                    if photo is None:
                        try:photo=self.render_photo(target)
                        except Exception:
                            if self.image_path is None:raise
                            self.image_path=None;self.photos.clear()
                            self.page_count=max(1,len(self.analysis.get('pages',[])))
                            self.page=min(self.page,self.page_count-1)
                            self.show_text()
                            return
                        self.photos[key]=photo
                    self.photos.move_to_end(key)
                    while len(self.photos)>1 and (len(self.photos)>self.CACHE_PAGES or
                          sum(p.width()*p.height() for p in self.photos.values())>self.CACHE_PIXELS):
                        self.photos.popitem(last=False)
                    self.canvas.create_image(12,8,anchor='nw',image=photo)
                    self.canvas.configure(scrollregion=(0,0,photo.width()+24,photo.height()+16))
                    self.displayed=key
            self.draw_highlight()
        except Exception:self.page_label.configure(text='Откройте исходный файл')

    def draw_highlight(self):
        if not self.has_image():
            self.text.tag_remove('source','1.0','end')
            if self.highlight:
                line=self.highlight.get('line',1);self.text.tag_add('source',f'{line}.0',f'{line}.end');self.text.see(f'{line}.0')
            return
        self.canvas.delete('source-highlight')
        if not self.highlight or self.displayed is None:return
        items=self.evidence.get((self.page+1,self.highlight.get('line')),[])
        index=self.highlight.get('page_index')
        item=next((e for e in items if not index or e.get('page_index')==index),None)
        if not item:return
        width,height=item.get('image_size',(0,0));box=item.get('bbox',[])
        if width and height and len(box)==4:
            photo=self.photos[self.displayed]
            left,top,right,bottom=box;xs=photo.width()/width;ys=photo.height()/height
            self.canvas.create_rectangle(12+left*xs,8+top*ys,12+right*xs,8+bottom*ys,
                                         outline='#c67600',width=3,tags=('source-highlight',))
            self.canvas.yview_moveto(max(0,(top*ys-60)/(photo.height()+16)))

    def locate(self,observation):
        physical=max(1,observation.get('page') or 1)
        if self.has_image():page=min(self.count()-1,physical-1)
        else:
            numbers=self.analysis.get('page_numbers',[])
            page=min(self.count()-1,max(0,observation['page_index']-1)) if observation.get('page_index') else numbers.index(physical) if physical in numbers else min(self.count()-1,physical-1)
        self.highlight=observation
        if page==self.page and self.displayed is not None:
            try:self.draw_highlight()
            except Exception:self.page_label.configure(text='Откройте исходный файл')
        else:
            self.page=page
            if self.displayed is not None or not self.resize_job:self.render()

    def open_original(self):
        from .gui import open_path
        if self.document:open_path(self.document['original'])


class ObservationEditor:
    def __init__(self,parent,observation,on_save):
        self.original=deepcopy(observation);self.on_save=on_save;self.fields={}
        self.window=tk.Toplevel(parent);self.window.title('Строка исследования');self.window.configure(bg='white')
        self.window.geometry('640x660');self.window.minsize(540,490);self.window.transient(parent);self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        outer=ttk.Frame(self.window,padding=20);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Сверьте строку с оригиналом',style='Heading.TLabel').pack(anchor='w',pady=(0,8))
        self.save_button=ttk.Button(outer,text='Сохранить строку',style='Primary.TButton',command=self.save);self.save_button.pack(side='bottom',anchor='e',pady=(12,0))
        area=ui.ScrollArea(outer);area.pack(fill='both',expand=True)
        raw=observation.get('raw','')
        if raw:ttk.Label(area.body,text='Распознано: '+raw,wraplength=500,style='Muted.TLabel').pack(anchor='w',pady=(0,12))
        for key,label in [('label','Показатель'),('result','Результат — со всеми знаками'),('unit','Единица измерения'),('reference','Референс из бланка'),('date','Дата исследования'),('side','Сторона / орган'),('section','Раздел'),('comment','Комментарий')]:
            ttk.Label(area.body,text=label).pack(anchor='w')
            value=observation.get(key,'') or ''
            if key=='date':value=display_date(value)
            var=tk.StringVar(value=value);self.fields[key]=var
            ttk.Entry(area.body,textvariable=var).pack(fill='x',pady=(4,10))
        self.error=tk.StringVar();ttk.Label(area.body,textvariable=self.error,foreground='#b42318',wraplength=500).pack(anchor='w')

    def save(self):
        from .parsing import result_fields
        try:
            record=deepcopy(self.original)
            for key,var in self.fields.items():record[key]=var.get().strip()
            if not record['label'] or not record['result']:raise ValueError('Укажите показатель и результат. Пустые строки не записываются.')
            record['date']=parse_date(record['date'])
            record['date_edited']=record['date']!=(self.original.get('date') or '')
            record['number']=result_fields(record['result'])[1]
            record['status']='Проверено вручную';record['manual_edited']=True
            if not record.get('raw'):record['raw']='';record['origin']='Ручной ввод'
            self.on_save(record);self.close()
        except ValueError as exc:self.error.set(str(exc))

    def close(self):
        parent=self.window.master;self.window.destroy();parent.grab_set()


class QualityReviewWindow:
    def __init__(self,parent,workspace,on_change,icons,actions=None):
        self.workspace=workspace;self.on_change=on_change;self.icons=icons;self.selected=None;self.records={};self.observations=[];self.sections=[]
        self.window=tk.Toplevel(parent);self.window.title('Проверка данных');self.window.configure(bg='white')
        self.actions=actions or ui.WorkspaceActions(self.window);self.saving=False
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.geometry(f'{min(1240,parent.winfo_screenwidth()-50)}x{min(850,parent.winfo_screenheight()-130)}+25+25')
        self.window.minsize(900,650);self.window.transient(parent);self.window.grab_set()
        outer=ttk.Frame(self.window,padding=18);outer.pack(fill='both',expand=True)
        self.title=ttk.Label(outer,text='Сверьте данные с оригиналом',style='Heading.TLabel');self.title.pack(anchor='w')
        self.list=ttk.Treeview(outer,columns=('file','issues'),show='headings',height=2,selectmode='browse')
        self.list.heading('file',text='Документ');self.list.heading('issues',text='Требует проверки')
        self.list.column('file',width=340);self.list.column('issues',width=650)
        self.list.pack(fill='x',pady=(10,8));self.list.bind('<<TreeviewSelect>>',self.load)
        self.panes=ttk.Panedwindow(outer,orient='horizontal');self.panes.pack(fill='both',expand=True)
        self.preview=SourcePreview(self.panes,icons);right=ttk.Frame(self.panes,padding=(16,0,0,0))
        self.panes.add(self.preview,weight=1);self.panes.add(right,weight=1)
        bottom=ttk.Frame(right);bottom.pack(side='bottom',fill='x',pady=(8,0))
        self.verified=tk.BooleanVar(value=False)
        self.attest=ttk.Checkbutton(bottom,text='Все страницы и строки сверены',variable=self.verified,command=self.enable_confirm)
        self.attest.pack(anchor='w')
        self.confirm_button=ttk.Button(bottom,text='Подтвердить данные',style='Primary.TButton',command=self.confirm,state='disabled');self.confirm_button.pack(fill='x',pady=(8,0))
        self.editor_area=ui.ScrollArea(right);self.editor_area.pack(fill='both',expand=True)
        fields=self.editor_area.body
        self.issue_label=ttk.Label(fields,text='',wraplength=420,foreground='#895500');self.issue_label.pack(anchor='w',pady=(0,10))
        self.details_button=ttk.Button(fields,text='Все замечания',style='Link.TButton',command=self.show_issues)
        self.duplicate_button=ttk.Button(fields,text='Сравнить с возможным дублем',command=self.compare_duplicate)
        self.alternatives_button=ttk.Button(fields,text='Варианты распознавания',style='Link.TButton',command=self.show_alternatives)
        dates=ttk.Frame(fields);dates.pack(fill='x');dates.columnconfigure(1,weight=1)
        ttk.Label(dates,text='Дата').grid(row=0,column=0,sticky='w',padx=(0,10))
        self.date=tk.StringVar();self.study_type=tk.StringVar()
        for value in (self.date,self.study_type):value.trace_add('write',self.changed)
        date_entry=ttk.Entry(dates,textvariable=self.date,width=16);date_entry.grid(row=0,column=1,sticky='ew')
        ttk.Label(dates,text='Вид').grid(row=1,column=0,sticky='w',padx=(0,10),pady=6)
        study_entry=ttk.Entry(dates,textvariable=self.study_type);study_entry.grid(row=1,column=1,sticky='ew',pady=6)
        ttk.Label(fields,text='Показатели',font=(ui.FONT,12,'bold')).pack(anchor='w',pady=(8,4))
        self.rows=ttk.Treeview(fields,columns=('label','result','unit'),show='headings',height=5,selectmode='browse')
        for key,title,width in [('label','Показатель',190),('result','Результат',105),('unit','Ед.',75)]:
            self.rows.heading(key,text=title);self.rows.column(key,width=width,minwidth=50,stretch=True)
        self.rows.pack(fill='x');self.rows.bind('<<TreeviewSelect>>',self.locate);self.rows.bind('<Double-1>',lambda e:self.edit_row())
        buttons=ttk.Frame(fields);buttons.pack(fill='x',pady=(4,8))
        self.edit_button=ttk.Button(buttons,text='Исправить',command=self.edit_row);self.edit_button.pack(side='left')
        add_button=ttk.Button(buttons,text='+ Строка',style='Link.TButton',command=self.add_row);add_button.pack(side='left')
        remove_button=ttk.Button(buttons,text='Убрать',style='Link.TButton',command=self.remove_row);remove_button.pack(side='right')
        self.source_label=ttk.Label(fields,text='',wraplength=420,style='Muted.TLabel');self.source_label.pack(anchor='w',pady=(0,8))
        self.sections_button=ttk.Button(fields,text='Сверить текст заключения',command=self.edit_sections)
        self.sections_button.pack(anchor='w',pady=(0,8))
        ttk.Label(fields,text='Замечание / причина отсутствующих данных').pack(anchor='w')
        self.note=tk.Text(fields,height=3,wrap='word',font=(ui.FONT,11),relief='solid',borderwidth=1,padx=6,pady=6,bg='white',fg=ui.INK);self.note.pack(fill='x',pady=(4,6))
        self.controls=(self.attest,date_entry,study_entry,self.edit_button,add_button,remove_button,
                       self.sections_button,self.duplicate_button,self.details_button,self.alternatives_button,self.note)
        self.validation=tk.StringVar();ttk.Label(fields,textvariable=self.validation,foreground='#b42318',wraplength=420).pack(anchor='w')
        self.editor_area.canvas.bind('<Configure>',lambda event:[label.configure(wraplength=max(200,event.width-8)) for label in (self.issue_label,self.source_label)],add='+')
        self.success=ttk.Frame(outer,padding=40)
        ttk.Label(self.success,image=icons.get('check-circle','green',64)).pack(pady=15)
        ttk.Label(self.success,text='Данные проверены',style='Heading.TLabel').pack(pady=8)
        ttk.Label(self.success,text='Исправления и история сверки сохранены',style='Muted.TLabel').pack(pady=8)
        ttk.Button(self.success,text='Вернуться к результатам',style='Primary.TButton',command=self.close).pack(pady=20)
        self.fill()

    def enable_confirm(self):
        self.confirm_button.configure(state='normal' if self.selected and self.verified.get() and not self.saving else 'disabled')

    def set_saving(self,saving):
        self.saving=saving
        self.list.configure(selectmode='none' if saving else 'browse')
        for control in self.controls:control.configure(state='disabled' if saving else 'normal')
        self.confirm_button.configure(text='Сохраняем данные…' if saving else 'Подтвердить данные')
        self.enable_confirm()

    def close(self):
        if not self.actions.active:self.window.destroy()

    def changed(self,*args):self.verified.set(False);self.enable_confirm()

    def fill(self):
        self.selected=None;self.verified.set(False);self.enable_confirm()
        self.records={r['id']:r for r in self.workspace.quality_review_list()}
        for item in self.list.get_children():self.list.delete(item)
        for record in self.records.values():
            messages=[i['message'] for i in record['issues']]
            self.list.insert('','end',iid=record['id'],values=(record['filename'],'; '.join(messages)[:180]))
        if self.records:
            self.title.configure(text=f'Сверьте данные с оригиналом · осталось {len(self.records)}')
            self.list.selection_set(next(iter(self.records)));self.load()
        else:
            self.list.pack_forget();self.panes.pack_forget();self.title.pack_forget();self.success.pack(fill='both',expand=True)

    def load(self,event=None):
        selected=self.list.selection()
        if self.saving:
            if selected!=(self.selected,):self.list.selection_set(self.selected)
            return
        if not selected or selected[0] not in self.records or selected[0]==self.selected:return
        self.selected=selected[0];record=self.records[self.selected]
        self.observations=deepcopy(record['observations']);self.sections=deepcopy(record['analysis'].get('sections',[]));self.date.set(display_date(record['date']));self.study_type.set(record['type'])
        self.verified.set(False);self.enable_confirm();self.validation.set('');self.note.delete('1.0','end')
        from collections import Counter
        counts=Counter(i['code'] for i in record['issues']);messages={i['code']:i['message'] for i in reversed(record['issues'])}
        if 'OCR_USED' in messages:
            for code in ('ocr_value','ocr_text'):messages.pop(code,None)
        elif 'ocr_value' in messages:messages['ocr_value']='Сверьте результаты со скана.'
        self.issue_label.configure(text='\n'.join('• '+message+(f' (строк: {counts[code]})' if counts[code]>1 else '') for code,message in list(messages.items())[:3]))
        self.details_button.pack_forget();self.alternatives_button.pack_forget()
        if len(record['issues'])>3:self.details_button.pack(anchor='w',after=self.issue_label,pady=(0,6))
        if record['analysis'].get('ocr_alternatives'):self.alternatives_button.pack(anchor='w',after=self.issue_label,pady=(0,6))
        self.duplicate_button.pack_forget()
        if any(i['code']=='possible_duplicate' for i in record['issues']):self.duplicate_button.pack(fill='x',before=self.issue_label,pady=(0,8))
        self.preview.load(self.workspace.document(self.selected),record['analysis'])
        self.fill_rows();self.editor_area.canvas.yview_moveto(0)

    def fill_rows(self):
        self.rows.configure(height=min(5,max(2,len(self.observations))))
        for item in self.rows.get_children():self.rows.delete(item)
        for index,o in enumerate(self.observations):self.rows.insert('','end',iid=str(index),values=(o.get('label',''),o.get('result',''),o.get('unit','')))
        self.source_label.configure(text='' if self.observations else 'Числовых строк нет. Сверьте текст заключения и полноту документа.')
        if self.observations:self.rows.selection_set('0');self.locate()

    def locate(self,event=None):
        selected=self.rows.selection()
        if not selected:return
        o=self.observations[int(selected[0])];self.preview.locate(o)
        self.source_label.configure(text=f"Страница {o.get('page',1)}, строка {o.get('line','—')} · {o.get('raw','Ручной ввод')}")

    def edit_row(self):
        if self.saving:return
        selected=self.rows.selection()
        if not selected:return
        index=int(selected[0])
        def save(value):self.observations[index]=value;self.fill_rows();self.rows.selection_set(str(index));self.verified.set(False);self.enable_confirm()
        self.row_editor=ObservationEditor(self.window,self.observations[index],save)

    def add_row(self):
        if self.saving:return
        def save(value):self.observations.append(value);self.fill_rows();self.verified.set(False);self.enable_confirm()
        try:date=parse_date(self.date.get())
        except ValueError as exc:self.validation.set(str(exc));return
        self.row_editor=ObservationEditor(self.window,{'page':self.preview.page+1,'line':0,'date':date,'date_source':'Ручной ввод','origin':'Ручной ввод'},save)

    def remove_row(self):
        if self.saving:return
        selected=self.rows.selection()
        if selected and messagebox.askyesno('Убрать строку','Убрать ошибочно извлечённую строку? Предыдущий результат сохранится в истории сверки.',parent=self.window):
            self.observations.pop(int(selected[0]));self.fill_rows();self.verified.set(False);self.enable_confirm()

    def confirm(self):
        if self.actions.active or not self.selected or not self.verified.get():return
        try:
            date=parse_date(self.date.get());kind=self.study_type.get().strip();note=self.note.get('1.0','end').strip()
            from . import quality
            draft=dict(self.records[self.selected]['analysis'],date=date,type=kind,observations=self.observations)
            remaining={i['code'] for i in quality.assess(draft)['issues']}
            exceptions={'missing_date','unknown_type','missing_unit','unknown_unit','missing_result','lab_no_results','missing_narrative_result','unparsed_row','conflicting_date','conflicting_type','unmapped_columns'}
            if (not date or not kind or kind=='Документ' or not self.observations or remaining & exceptions) and not note:
                raise ValueError('Остались замечания к полноте данных. Дополните поля или укажите причину в замечании. Для текстового заключения отметьте, что оно сверено.')
        except ValueError as exc:self.validation.set(str(exc));self.editor_area.canvas.yview_moveto(1);return
        document_id=self.selected
        revision=self.records[document_id]['revision']
        observations,sections=deepcopy(self.observations),deepcopy(self.sections)

        def work():
            return self.workspace.confirm_quality(document_id,date=date,study_type=kind,observations=observations,
                sections=sections,note=note or 'Сверены все страницы и строки оригинала',expected_revision=revision)

        def completed(_value,error):
            self.set_saving(False)
            if isinstance(error,ValueError):
                self.validation.set(str(error));self.editor_area.canvas.yview_moveto(1)
                return
            if error is None or isinstance(error,ReviewCommittedError):
                self.on_change();self.fill()
                if error is not None:
                    messagebox.showwarning('Данные сохранены',str(error),parent=self.window)
            else:
                messagebox.showerror('Не удалось сохранить проверку',str(error),parent=self.window)

        if self.actions.run(work,completed):
            self.validation.set('')
            self.set_saving(True)

    def compare_duplicate(self):
        if self.actions.active or not self.selected:return
        record=self.records[self.selected]
        issue=next((i for i in record['issues'] if i['code']=='possible_duplicate'),{})
        target=next(iter(issue.get('related_ids',[])),None)
        if not target:
            messagebox.showinfo('Возможный дубль','Откройте «Все исследования» в меню «Папка данных», чтобы сравнить оригиналы. Сходство текста ещё не означает совпадение результатов.',parent=self.window);return
        self.duplicate_window=DuplicateWindow(self.window,self.workspace,record,target,self.icons,self.duplicate_done,actions=self.actions)

    def duplicate_done(self):self.on_change();self.fill()

    def edit_sections(self):
        if self.saving:return
        window=tk.Toplevel(self.window);self.sections_window=window;window.title('Текст исследования');window.configure(bg='white');window.geometry('900x680');window.transient(self.window);window.grab_set()
        outer=ttk.Frame(window,padding=18);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Сверьте текст с оригиналом',style='Heading.TLabel').pack(anchor='w',pady=(0,10))
        buttons=ttk.Frame(outer);buttons.pack(side='bottom',fill='x',pady=(10,0))
        area=ui.ScrollArea(outer);area.pack(fill='both',expand=True);editors=[];self.sections_editors=editors
        working=deepcopy(self.sections)
        def add_editor(section):
            ttk.Label(area.body,text=f"{section.get('section','Текст')} · страница {section.get('page',1)}",font=(ui.FONT,12,'bold')).pack(anchor='w',pady=(8,4))
            text=tk.Text(area.body,height=max(3,min(12,len(section.get('text','').splitlines())+1)),wrap='word',font=(ui.FONT,11),bg='white',fg=ui.INK,relief='solid',borderwidth=1,padx=8,pady=8)
            text.insert('1.0',section.get('text',''));text.pack(fill='x',pady=(0,10));editors.append(text)
        def add_section():
            section={'section':'Заключение','text':'','page':self.preview.page+1,'line':0}
            working.append(section);add_editor(section);area.canvas.yview_moveto(1)
        for section in working:add_editor(section)
        if not working:add_section()
        def close():window.destroy();self.window.grab_set()
        def save():
            for section,text in zip(working,editors):
                value=text.get('1.0','end-1c')
                if value!=section.get('text',''):section['text']=value;section['manual_edited']=True
            self.sections=working
            self.changed();close()
        ttk.Button(buttons,text='Отмена',command=close).pack(side='left')
        ttk.Button(buttons,text='+ Раздел',style='Link.TButton',command=add_section).pack(side='left',padx=8)
        self.sections_save_button=ttk.Button(buttons,text='Сохранить текст',style='Primary.TButton',command=save);self.sections_save_button.pack(side='right')
        window.protocol('WM_DELETE_WINDOW',close)

    def show_issues(self):
        record=self.records[self.selected]
        self.show_text_columns('Замечания к документу',[('Что сверить','\n\n'.join((f"Страница {i['page']}. " if i.get('page') else '')+i['message'] for i in record['issues']))])

    def show_alternatives(self):
        analysis=self.records[self.selected]['analysis']
        primary='\n\n'.join(f'Страница {i+1}\n{text}' for i,text in enumerate(analysis['pages']))
        alternatives='\n\n'.join(f"Страница {item.get('page','—')} · {item['engine']}\n{item['text']}" for item in analysis.get('ocr_alternatives',[]))
        self.show_text_columns('Варианты распознавания — сверьте с оригиналом',[('Использовано для черновика',primary),('Независимое чтение',alternatives)])

    def show_text_columns(self,title,columns):
        window=tk.Toplevel(self.window);window.title(title);window.configure(bg='white');window.geometry('1000x680');window.transient(self.window);window.grab_set()
        outer=ttk.Frame(window,padding=18);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text=title,font=(ui.FONT,14,'bold'),wraplength=900).pack(anchor='w',pady=(0,12))
        panes=ttk.Panedwindow(outer,orient='horizontal');panes.pack(fill='both',expand=True)
        for label,value in columns:
            frame=ttk.Frame(panes);panes.add(frame,weight=1)
            ttk.Label(frame,text=label,font=(ui.FONT,12,'bold')).pack(anchor='w')
            text=tk.Text(frame,wrap='word',width=35,font=(ui.FONT,11),relief='flat',padx=8,pady=8,bg='white',fg=ui.INK)
            scroll=ttk.Scrollbar(frame,command=text.yview);scroll.pack(side='right',fill='y');text.configure(yscrollcommand=scroll.set);text.pack(fill='both',expand=True)
            text.insert('1.0',value);text.configure(state='disabled')
        def close():window.destroy();self.window.grab_set()
        window.protocol('WM_DELETE_WINDOW',close);ttk.Button(outer,text='Вернуться к сверке',command=close).pack(anchor='e',pady=(10,0))


class DuplicateWindow:
    def __init__(self,parent,workspace,record,target,icons,on_done,actions=None):
        self.workspace=workspace;self.record=record;self.target=target;self.on_done=on_done
        self.window=tk.Toplevel(parent);self.window.title('Сравнение документов');self.window.configure(bg='white')
        self.actions=actions or ui.WorkspaceActions(self.window)
        self.window.geometry('1150x760');self.window.transient(parent);self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        outer=ttk.Frame(self.window,padding=18);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Одинаковые исследования или разные результаты?',style='Heading.TLabel').pack(anchor='w',pady=(0,12))
        buttons=ttk.Frame(outer);buttons.pack(side='bottom',fill='x',pady=(12,0))
        self.cancel_button=ttk.Button(buttons,text='Разные — продолжить сверку',command=self.close);self.cancel_button.pack(side='left')
        self.confirm_button=ttk.Button(buttons,text='Это повтор того же исследования',command=self.confirm);self.confirm_button.pack(side='right')
        panes=ttk.Panedwindow(outer,orient='horizontal');panes.pack(fill='both',expand=True)
        for docid in (record['id'],target):
            document=workspace.document(docid);preview=SourcePreview(panes,icons);panes.add(preview,weight=1);preview.load(document,json.loads(document['analysis']))

    def confirm(self):
        if self.actions.active:return
        document_id,target,revision=self.record['id'],self.target,self.record['revision']

        def work():
            return self.workspace.mark_duplicate(document_id,target,'Оригиналы сверены: повтор того же исследования',expected_revision=revision)

        def completed(_value,error):
            if error is None or isinstance(error,ReviewCommittedError):
                parent=self.window.master
                self.close();self.on_done()
                if error is not None:
                    messagebox.showwarning('Решение сохранено',str(error),parent=parent)
                return
            self.cancel_button.configure(state='normal')
            self.confirm_button.configure(state='normal',text='Это повтор того же исследования')
            messagebox.showerror('Не удалось отметить повтор',str(error),parent=self.window)

        if self.actions.run(work,completed):
            self.cancel_button.configure(state='disabled')
            self.confirm_button.configure(state='disabled',text='Сохраняем решение…')

    def close(self):
        if self.actions.active:return
        parent=self.window.master;self.window.destroy();parent.grab_set()


class HistoryWindow:
    def __init__(self,parent,workspace,on_change,icons,actions=None):
        self.workspace=workspace;self.on_change=on_change;self.icons=icons;self.candidate=None
        self.window=tk.Toplevel(parent);self.window.title('Все исследования');self.window.configure(bg='white')
        self.actions=actions or ui.WorkspaceActions(self.window)
        self.window.geometry('1040x700');self.window.minsize(850,600);self.window.transient(parent);self.window.grab_set();self.window.protocol('WM_DELETE_WINDOW',self.close)
        outer=ttk.Frame(self.window,padding=22);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Все исследования',style='Heading.TLabel').pack(anchor='w')
        self.status=tk.StringVar(value='Оригиналы и предыдущие результаты сохраняются.')
        ttk.Label(outer,textvariable=self.status,style='Muted.TLabel',wraplength=900).pack(anchor='w',pady=8)
        self.list=ttk.Treeview(outer,columns=('file','date','type','state'),show='headings',height=10,selectmode='browse')
        for key,label,width in [('file','Документ',330),('date','Дата',110),('type','Вид',180),('state','Состояние',190)]:self.list.heading(key,text=label);self.list.column(key,width=width)
        self.list.pack(fill='both',expand=True)
        buttons=ttk.Frame(outer);buttons.pack(fill='x',pady=(12,0))
        self.open_button=ttk.Button(buttons,text='Открыть оригинал',command=self.open_original);self.open_button.pack(side='left')
        self.reprocess_button=ttk.Button(buttons,text='Распознать заново',command=self.reprocess);self.reprocess_button.pack(side='right')
        self.fill()

    def fill(self):
        for item in self.list.get_children():self.list.delete(item)
        self.records={r['id']:r for r in self.workspace.document_list()}
        for r in self.records.values():self.list.insert('','end',iid=r['id'],values=(r['filename'],display_date(r.get('date','')),r.get('type',''),r.get('status','')))
        if self.records:self.list.selection_set(next(iter(self.records)))

    def open_original(self):
        from .gui import open_path
        selected=self.list.selection()
        if selected:open_path(self.workspace.document(selected[0])['original'])

    def reprocess(self):
        selected=self.list.selection()
        if not selected or self.candidate is not None or self.actions.active:return
        document_id=selected[0]

        def work():
            return self.workspace.prepare_reprocess(document_id)

        def completed(candidate,error):
            self.list.configure(selectmode='browse')
            self.open_button.configure(state='normal')
            self.reprocess_button.configure(state='normal')
            if error is not None:
                self.status.set('Повторное распознавание не завершено.')
                messagebox.showerror('Не удалось распознать',str(error),parent=self.window)
            else:
                self.status.set('Черновик готов. Сравните изменения перед применением.')
                self.show_candidate(candidate)

        if self.actions.run(work,completed):
            self.list.configure(selectmode='none')
            self.open_button.configure(state='disabled')
            self.reprocess_button.configure(state='disabled')
            self.status.set('Повторно читаем сохранённый оригинал… Это может занять несколько минут.')

    def show_candidate(self,candidate):
        self.candidate=candidate;win=tk.Toplevel(self.window);self.candidate_window=win;win.title('Результат повторного распознавания');win.configure(bg='white');win.geometry('1100x700');win.transient(self.window);win.grab_set();win.protocol('WM_DELETE_WINDOW',self.close_candidate)
        outer=ttk.Frame(win,padding=20);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='Сравните перед применением',style='Heading.TLabel').pack(anchor='w',pady=(0,10))
        info='Подтверждённые вручную поля сохранены. '+ '; '.join(candidate.get('protected_fields',[])) if candidate.get('protected_fields') else 'Предыдущий результат останется в истории. Новый результат пройдёт проверки.'
        ttk.Label(outer,text=info,style='Muted.TLabel',wraplength=820).pack(anchor='w',pady=(0,12))
        buttons=ttk.Frame(outer);buttons.pack(side='bottom',fill='x',pady=(12,0))
        self.cancel_button=ttk.Button(buttons,text='Оставить прежний результат',command=self.close_candidate);self.cancel_button.pack(side='left')
        self.apply_button=ttk.Button(buttons,text='Применить черновик',style='Primary.TButton',command=self.apply);self.apply_button.pack(side='right')
        panes=ttk.Panedwindow(outer,orient='horizontal');panes.pack(fill='both',expand=True)
        for title,a in [('Сейчас',candidate['previous_analysis']),('Новое чтение',candidate['proposed_analysis']),('Будет сохранено',candidate['analysis'])]:
            frame=ttk.Frame(panes);panes.add(frame,weight=1);ttk.Label(frame,text=title,font=(ui.FONT,13,'bold')).pack(anchor='w')
            text=tk.Text(frame,wrap='word',width=28,font=(ui.FONT,11),relief='flat',padx=8,pady=10,bg='white',fg=ui.INK)
            scroll=ttk.Scrollbar(frame,command=text.yview);scroll.pack(side='right',fill='y');text.configure(yscrollcommand=scroll.set);text.pack(fill='both',expand=True)
            text.insert('end',display_date(a.get('date',''))+' · '+a.get('type','')+'\n\n')
            for o in a.get('observations',[]):
                text.insert('end',f"{o.get('label','')}  {o.get('result','')} {o.get('unit','')}\n{display_date(o.get('date',''))} · {o.get('side','')}\n{o.get('section','')} · {o.get('origin','')}\nРеференс: {o.get('reference','')}\n\n")
            for section in a.get('sections',[]):text.insert('end',section.get('section','')+'\n'+section.get('text','')+'\n\n')
            for issue in a.get('quality',{}).get('issues',[]):text.insert('end','\n⚠ '+issue['message'])
            text.configure(state='disabled')

    def apply(self):
        if self.actions.active or self.candidate is None:return
        # No widget edits the candidate after it has been shown.
        candidate=self.candidate
        document_id=candidate['document_id']

        def work():
            return self.workspace.apply_reprocess(document_id,candidate)

        def completed(_value,error):
            if error is not None and not isinstance(error,ReviewCommittedError):
                self.apply_button.configure(state='normal',text='Применить черновик')
                self.cancel_button.configure(state='normal')
                messagebox.showerror('Не удалось применить',str(error),parent=self.candidate_window)
                return
            self.close_candidate();self.on_change();self.fill()
            if error is not None:
                self.status.set('Результат сохранён в базе.')
                messagebox.showwarning('Результат сохранён',str(error),parent=self.window)
            else:
                self.status.set('Результат сохранён. Документы с замечаниями доступны в «Проверить документы».')

        if self.actions.run(work,completed):
            self.apply_button.configure(state='disabled',text='Сохраняем результат…')
            self.cancel_button.configure(state='disabled')

    def close_candidate(self):
        if self.actions.active or self.candidate is None:return
        self.candidate_window.destroy();self.candidate=None;self.window.grab_set()

    def close(self):
        if self.actions.active:
            self.status.set('Дождитесь завершения операции. Исходные данные сохраняются.')
            return
        self.window.destroy()
