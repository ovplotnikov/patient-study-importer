"""Explicit source-to-result review. No confidence score substitutes for consent."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from . import review_queue, ui
from .pdf_render import PDFIUM_LOCK
from .analysis import UNNAMED_METRIC
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
        self.photos=OrderedDict();self.displayed=None;self.evidence={};self.line_cache={}
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
        self.evidence={};self.line_cache={}
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

    def visual_lines(self,page):
        """Read where each text line sits on a PDF page. Rotated pages get no frame."""
        cached=self.line_cache.get(page)
        if cached is not None:return cached
        lines,width,height=[],1,1
        try:
            with PDFIUM_LOCK:
                handle=self.pdf[page];textpage=None
                try:
                    width,height=handle.get_size()
                    if not handle.get_rotation():
                        textpage=handle.get_textpage()
                        rects=[textpage.get_rect(index) for index in range(textpage.count_rects())]
                        lines=review_queue.visual_lines([(textpage.get_text_bounded(*rect),rect) for rect in rects],height)
                finally:
                    if textpage is not None:textpage.close()
                    handle.close()
        except Exception:lines=[]
        self.line_cache[page]=(lines,width,height)
        return self.line_cache[page]

    def show_box(self,left,top,right,bottom,xs,ys):
        photo=self.photos[self.displayed]
        self.canvas.create_rectangle(8+left*xs,5+top*ys,16+right*xs,11+bottom*ys,
                                     outline='#c67600',width=3,tags=('source-highlight',))
        # Keep the frame in the upper part of the view, with the rows above it visible.
        self.canvas.yview_moveto(max(0,(top*ys-.35*self.canvas.winfo_height())/(photo.height()+16)))

    def draw_highlight(self):
        if not self.has_image():
            self.text.tag_remove('source','1.0','end')
            if self.highlight and self.highlight.get('line'):
                line=self.highlight['line'];self.text.tag_add('source',f'{line}.0',f'{line}.end');self.text.see(f'{line}.0')
            return
        self.canvas.delete('source-highlight')
        if not self.highlight or self.displayed is None:return
        photo=self.photos[self.displayed]
        items=self.evidence.get((self.page+1,self.highlight.get('line')),[])
        index=self.highlight.get('page_index')
        item=next((e for e in items if not index or e.get('page_index')==index),None)
        if item:
            width,height=item.get('image_size',(0,0));box=item.get('bbox',[])
            if width and height and len(box)==4:
                self.show_box(*box,photo.width()/width,photo.height()/height)
            return
        if self.pdf is None or not self.highlight.get('raw'):
            self.canvas.yview_moveto(0);return
        lines,width,height=self.visual_lines(self.page)
        pages=self.analysis.get('pages',[])
        position=(index or self.page+1)-1
        total=len(pages[position].splitlines()) if 0<=position<len(pages) else None
        found=review_queue.find_line(lines,self.highlight['raw'],self.highlight.get('line'),total)
        if found:self.show_box(*found['box'],photo.width()/width,photo.height()/height)
        elif self.highlight.get('line') and total:self.canvas.yview_moveto(max(0,self.highlight['line']/total-.2))
        else:self.canvas.yview_moveto(0)

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
    """A queue of the places where reading stumbled. Scans keep the full line-by-line check."""
    AUTO_NOTE='Сверены отмеченные замечания с оригиналом'

    def __init__(self,parent,workspace,on_change,icons,actions=None):
        self.workspace=workspace;self.on_change=on_change;self.icons=icons;self.selected=None;self.records={};self.observations=[];self.sections=[]
        self.selected_item=None;self.items={};self.drafts={};self.found={};self.current=None;self.full_mode=True;self.similar=[]
        self.save_queue=[];self.pending={};self.session_saved=False;self.backed_up=False;self.revision=None
        self.finishing=False;self.after_finish=None;self.close_requested=False
        self.window=tk.Toplevel(parent);self.window.title('Проверка данных');self.window.configure(bg='white')
        self.actions=actions or ui.WorkspaceActions(self.window);self.saving=False
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.geometry(f'{min(1240,parent.winfo_screenwidth()-50)}x{min(850,parent.winfo_screenheight()-130)}+25+25')
        self.window.minsize(900,650);self.window.transient(parent);self.window.grab_set()
        outer=ttk.Frame(self.window,padding=18);outer.pack(fill='both',expand=True)
        self.head=ttk.Frame(outer);self.head.pack(fill='x')
        self.title=ttk.Label(self.head,text='Сверьте данные с оригиналом',style='Heading.TLabel');self.title.pack(side='left')
        self.progress=ttk.Label(self.head,text='',style='Muted.TLabel');self.progress.pack(side='right')
        self.panes=ttk.Panedwindow(outer,orient='horizontal');self.panes.pack(fill='both',expand=True,pady=(10,0))
        queue=ttk.Frame(self.panes,width=270);self.panes.add(queue,weight=0)
        self.list=ttk.Treeview(queue,show='tree',selectmode='browse')
        self.list.column('#0',width=262,stretch=True)
        self.list.tag_configure('document',font=(ui.FONT,11,'bold'))
        queue_scroll=ttk.Scrollbar(queue,orient='vertical',command=self.list.yview);self.list.configure(yscrollcommand=queue_scroll.set)
        queue_scroll.pack(side='right',fill='y');self.list.pack(side='left',fill='both',expand=True)
        self.list.bind('<<TreeviewSelect>>',self.load)
        self.center=ttk.Frame(self.panes,padding=(12,0,0,0));self.panes.add(self.center,weight=1)
        self.center.columnconfigure(0,weight=1);self.center.rowconfigure(0,weight=1)
        self.preview=SourcePreview(self.center,icons);self.preview.grid(row=0,column=0,sticky='nsew')
        self.build_focus_card()
        self.build_full_panel(icons)
        self.success=ttk.Frame(outer,padding=40)
        ttk.Label(self.success,image=icons.get('check-circle','green',64)).pack(pady=15)
        ttk.Label(self.success,text='Данные проверены',style='Heading.TLabel').pack(pady=8)
        self.success_text=ttk.Label(self.success,text='Исправления и история сверки сохранены',style='Muted.TLabel');self.success_text.pack(pady=8)
        ttk.Button(self.success,text='Вернуться к результатам',style='Primary.TButton',command=self.close).pack(pady=20)
        self.window.bind('<Return>',self.accept)
        self.window.bind('<Escape>',self.skip)
        self.fill()

    def build_focus_card(self):
        card=self.focus_card=ttk.Frame(self.center,padding=(0,12,0,0));card.grid(row=1,column=0,sticky='ew')
        card.columnconfigure(0,weight=1)
        card.bind('<Configure>',lambda event:self.focus_detail.configure(wraplength=max(300,event.width-8)))
        top=ttk.Frame(card);top.grid(row=0,column=0,sticky='ew')
        self.focus_title=ttk.Label(top,text='',font=(ui.FONT,14,'bold'),foreground='#895500');self.focus_title.pack(side='left')
        self.focus_place=ttk.Label(top,text='',style='Muted.TLabel');self.focus_place.pack(side='right')
        self.focus_detail=ttk.Label(card,text='',style='Muted.TLabel',wraplength=860);self.focus_detail.grid(row=1,column=0,sticky='w',pady=(2,8))
        self.vars={key:tk.StringVar() for key in ('label','result','unit','reference')}
        self.row_fields=ttk.Frame(card);self.row_fields.grid(row=2,column=0,sticky='ew')
        self.field_entries={}
        for column,(key,title,weight) in enumerate([('label','Показатель',4),('result','Результат',2),('unit','Единица',2),('reference','Референс',3)]):
            self.row_fields.columnconfigure(column,weight=weight,uniform='fields')
            ttk.Label(self.row_fields,text=title).grid(row=0,column=column,sticky='w',padx=(0,8))
            entry=ttk.Entry(self.row_fields,textvariable=self.vars[key]);entry.grid(row=1,column=column,sticky='ew',padx=(0,8),pady=(3,0))
            self.field_entries[key]=entry
        self.date_fields=ttk.Frame(card);self.date_fields.grid(row=2,column=0,sticky='ew')
        ttk.Label(self.date_fields,text='Дата исследования').pack(side='left',padx=(0,10))
        self.focus_date=tk.StringVar();self.date_entry=ttk.Entry(self.date_fields,textvariable=self.focus_date,width=16);self.date_entry.pack(side='left')
        self.date_choices=ttk.Frame(self.date_fields);self.date_choices.pack(side='left',padx=10)
        self.type_fields=ttk.Frame(card);self.type_fields.grid(row=2,column=0,sticky='ew')
        ttk.Label(self.type_fields,text='Вид исследования').pack(side='left',padx=(0,10))
        self.focus_type=tk.StringVar();self.type_entry=ttk.Combobox(self.type_fields,textvariable=self.focus_type,width=36);self.type_entry.pack(side='left')
        self.bulk=tk.BooleanVar(value=False);self.bulk_check=ttk.Checkbutton(card,variable=self.bulk);self.bulk_check.grid(row=3,column=0,sticky='w',pady=(8,0))
        self.focus_error=tk.StringVar();ttk.Label(card,textvariable=self.focus_error,foreground='#b42318',wraplength=860).grid(row=4,column=0,sticky='w')
        actions=ttk.Frame(card);actions.grid(row=5,column=0,sticky='ew',pady=(8,0))
        self.accept_button=ttk.Button(actions,text='Принять и дальше',style='Primary.TButton',command=self.accept);self.accept_button.pack(side='left')
        self.skip_button=ttk.Button(actions,text='Пропустить',style='Link.TButton',command=self.skip);self.skip_button.pack(side='left',padx=(8,0))
        self.whole_button=ttk.Button(actions,text='Весь документ',style='Link.TButton',command=self.show_full);self.whole_button.pack(side='right')
        self.dismiss_button=ttk.Button(actions,text='Убрать строку',style='Link.TButton',command=self.dismiss);self.dismiss_button.pack(side='right')

    def build_full_panel(self,icons):
        right=self.full_panel=ttk.Frame(self.center,padding=(16,0,0,0));right.grid(row=0,column=1,rowspan=2,sticky='nsew')
        bottom=ttk.Frame(right);bottom.pack(side='bottom',fill='x',pady=(8,0))
        self.verified=tk.BooleanVar(value=False)
        self.attest=ttk.Checkbutton(bottom,text='Все страницы и строки сверены',variable=self.verified,command=self.enable_confirm)
        self.attest.pack(anchor='w')
        self.confirm_button=ttk.Button(bottom,text='Подтвердить данные',style='Primary.TButton',command=self.confirm,state='disabled');self.confirm_button.pack(fill='x',pady=(8,0))
        self.editor_area=ui.ScrollArea(right);self.editor_area.pack(fill='both',expand=True)
        fields=self.editor_area.body
        self.back_button=ttk.Button(fields,text='‹ К замечаниям',style='Link.TButton',command=self.back_to_queue)
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
        self.rows.tag_configure('problem',background='#fff0ba')
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

    def enable_confirm(self):
        self.confirm_button.configure(state='normal' if self.selected and self.verified.get() and not self.saving else 'disabled')

    def set_saving(self,saving):
        self.saving=saving
        self.list.configure(selectmode='none' if saving else 'browse')
        for control in self.controls:control.configure(state='disabled' if saving else 'normal')
        self.confirm_button.configure(text='Сохраняем данные…' if saving else 'Подтвердить данные')
        self.enable_confirm()

    def close(self):
        if self.finishing:
            self.after_finish=self.window.destroy;return
        if self.actions.active or self.save_queue:
            if self.save_queue:self.close_requested=True
            self.progress.configure(text='Дождитесь завершения сохранения…')
            return
        if self.session_saved:self.finish_session(self.window.destroy)
        elif not self.finishing:self.window.destroy()

    def changed(self,*args):self.verified.set(False);self.enable_confirm()

    # Queue

    def draft(self,document_id):
        if document_id not in self.drafts:
            record=self.records[document_id]
            self.drafts[document_id]={
                'observations':deepcopy(record['observations']),'sections':deepcopy(record['analysis'].get('sections',[])),
                'date':display_date(record['date']),'type':record['type'],'done':set(),'checked':set(),'resolved':[],
            }
        return self.drafts[document_id]

    def stash(self):
        if self.selected in self.drafts:
            draft=self.drafts[self.selected]
            draft['date']=self.date.get();draft['type']=self.study_type.get();draft['sections']=self.sections

    def open_stumbles(self,document_id):
        if document_id not in self.found:self.found[document_id]=review_queue.stumbles(self.records[document_id])
        done=self.draft(document_id)['done']
        return [item for item in self.found[document_id] if item['key'] not in done]

    def fill(self):
        self.stash()
        self.selected=None;self.selected_item=None;self.current=None;self.verified.set(False);self.enable_confirm()
        self.records={r['id']:r for r in self.workspace.quality_review_list()}
        self.found={}
        self.revision=next((r['revision'] for r in self.records.values()),self.revision)
        self.fill_tree()
        if self.records:self.select_item(next(iter(self.list.get_children(''))))
        else:self.finish_queue()

    def fill_tree(self):
        for item in self.list.get_children(''):self.list.delete(item)
        self.items={};number=0
        for document_id,record in self.records.items():
            self.list.insert('','end',iid=document_id,text=record['filename'],tags=('document',))
            if not review_queue.needs_full_review(record):
                for stumble in self.open_stumbles(document_id):
                    number+=1;item=f'stumble-{number}';self.items[item]=(document_id,stumble['key'])
                    self.list.insert(document_id,'end',iid=item,text=review_queue.queue_label(stumble))
        self.update_progress()

    def update_progress(self):
        documents=len(self.records)
        if documents:self.title.configure(text='Сверьте данные с оригиналом')
        remaining=len(self.items)
        whole=sum(1 for record in self.records.values() if review_queue.needs_full_review(record))
        parts=[f'Документов: {documents}']
        if remaining:parts.append(f'замечаний: {remaining}')
        if whole:parts.append(f'сканов и документов для полной сверки: {whole}')
        self.progress.configure(text=' · '.join(parts) if documents else '')

    def leaves(self):
        result=[]
        for document_id in self.list.get_children(''):
            result.extend(self.list.get_children(document_id) or (document_id,))
        return result

    def select_item(self,item):
        self.list.selection_set(item);self.list.see(item);self.load()

    def load(self,event=None):
        selected=self.list.selection()
        if self.saving:
            if selected!=(self.selected_item,):self.list.selection_set(self.selected_item)
            return
        if not selected or selected[0]==self.selected_item:return
        item=selected[0];document_id,key=self.items.get(item,(item,None))
        if document_id not in self.records:return
        children=self.list.get_children(document_id)
        if key is None and children:
            item=children[0];document_id,key=self.items[item]
            self.list.selection_set(item)
        self.selected_item=item
        if document_id!=self.selected:self.open_document(document_id)
        self.list.item(document_id,open=True)
        if key is None:self.show_full()
        else:self.show_stumble(key)

    def open_document(self,document_id):
        self.stash()
        self.selected=document_id;record=self.records[document_id];draft=self.draft(document_id)
        self.observations=draft['observations'];self.sections=draft['sections'];self.date.set(draft['date']);self.study_type.set(draft['type'])
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
        self.preview.load(self.workspace.document(document_id),record['analysis'])
        self.fill_rows();self.editor_area.canvas.yview_moveto(0)

    def show_full(self):
        self.full_mode=True;self.focus_card.grid_remove()
        self.center.columnconfigure(1,weight=1,minsize=380);self.full_panel.grid()
        self.back_button.pack_forget()
        if self.selected and self.list.get_children(self.selected):self.back_button.pack(anchor='w',before=self.issue_label)
        self.fill_rows()

    def back_to_queue(self):
        try:parse_date(self.date.get())
        except ValueError as error:
            self.validation.set(str(error));return
        children=self.list.get_children(self.selected) if self.selected else ()
        if children:
            self.selected_item=None;self.select_item(children[0])

    # One stumble

    def show_stumble(self,key):
        draft=self.draft(self.selected)
        stumble=next(item for item in self.found[self.selected] if item['key']==key)
        self.current=stumble;self.full_mode=False;self.focus_error.set('')
        self.full_panel.grid_remove();self.center.columnconfigure(1,weight=0,minsize=0);self.focus_card.grid()
        self.focus_title.configure(text=stumble['title'])
        place=[]
        if stumble.get('page') and stumble['kind']!='date' and stumble['kind']!='type':place.append(f"Страница {stumble['page']}")
        if stumble.get('line') and stumble['kind'] in {'row','unparsed'}:place.append(f"строка {stumble['line']}")
        self.focus_place.configure(text=', '.join(place))
        raw=' '.join(stumble.get('raw','').split())
        detail=f'В тексте бланка: {raw}' if raw else ' '.join(stumble['messages'])
        if raw and len(set(stumble['codes']))>1:detail=' '.join(stumble['messages'])+' '+detail
        self.focus_detail.configure(text=detail)
        for frame in (self.row_fields,self.date_fields,self.type_fields):frame.grid_remove()
        self.dismiss_button.pack_forget();self.bulk.set(False);self.bulk_check.grid_remove();self.similar=[]
        target=None
        if stumble['kind'] in {'row','unparsed'}:
            self.row_fields.grid()
            if stumble['kind']=='row':
                row=self.row_by_id(draft,stumble['result_id'])
                for name,var in self.vars.items():var.set(str(row.get(name,'') or ''))
                self.dismiss_button.configure(text='Убрать строку')
                self.similar=self.similar_stumbles(stumble,row)
                if self.similar:
                    self.bulk_check.configure(text=f'Применить ко всем таким строкам: ещё {len(self.similar)}');self.bulk_check.grid()
                target=self.field_entries['unit'] if set(stumble['codes'])&review_queue.UNIT_CODES else self.field_entries['result']
            else:
                for name,value in review_queue.prefill_row(stumble['raw']).items():self.vars[name].set(value)
                self.dismiss_button.configure(text='Это не результат')
                target=self.field_entries['label']
            self.dismiss_button.pack(side='right')
        elif stumble['kind']=='date':
            self.date_fields.grid();self.focus_date.set(draft['date'] or (display_date(stumble['candidates'][0]) if len(stumble['candidates'])==1 else ''))
            for child in self.date_choices.winfo_children():child.destroy()
            for value in stumble['candidates']:
                text=display_date(value)
                ttk.Button(self.date_choices,text=text,style='Link.TButton',command=lambda text=text:self.focus_date.set(text)).pack(side='left')
            self.dismiss_button.configure(text='В бланке нет даты');self.dismiss_button.pack(side='right')
            target=self.date_entry
        elif stumble['kind']=='type':
            self.type_fields.grid()
            self.type_entry.configure(values=tuple(stumble['candidates'])+tuple(t for t in review_queue.STUDY_TYPES if t not in stumble['candidates']))
            self.focus_type.set('' if draft['type'] in ('Документ','',None) else draft['type'])
            self.dismiss_button.configure(text='Вид не определить');self.dismiss_button.pack(side='right')
            target=self.type_entry
        if target is not None:
            target.focus_set()
            if isinstance(target,ttk.Entry) and not isinstance(target,ttk.Combobox):target.select_range(0,'end')
        self.preview.locate({'page':stumble.get('page') or 1,'page_index':stumble.get('page_index'),'line':stumble.get('line'),'raw':stumble.get('raw','')})

    @staticmethod
    def row_by_id(draft,result_id):
        return next(row for row in draft['observations'] if row.get('result_id')==result_id)

    def similar_stumbles(self,stumble,row):
        """Other unit questions with the same answer. Only read-from-text documents qualify."""
        codes=set(stumble['codes'])
        if len(codes)!=1 or not codes<=review_queue.UNIT_CODES:return []
        field='unit' if codes=={'unknown_unit'} else 'label'
        found=[]
        for document_id,record in self.records.items():
            if review_queue.needs_full_review(record):continue
            draft=self.draft(document_id)
            for other in self.open_stumbles(document_id):
                if other['key']==stumble['key'] or other['kind']!='row' or set(other['codes'])!=codes:continue
                if str(self.row_by_id(draft,other['result_id']).get(field,'') or '')==str(row.get(field,'') or ''):
                    found.append((document_id,other))
        return found

    def read_row_fields(self):
        values={name:var.get().strip() for name,var in self.vars.items()}
        if not values['label'] or not values['result']:raise ValueError('Укажите показатель и результат.')
        return values

    def accept(self,event=None):
        if self.full_mode or self.current is None or self.saving:return
        document_id=self.selected;stumble=self.current;draft=self.draft(document_id)
        try:
            if stumble['kind']=='row':self.accept_row(document_id,draft,stumble)
            elif stumble['kind']=='unparsed':self.accept_unparsed(draft,stumble)
            elif stumble['kind']=='date':self.accept_date(document_id,draft)
            elif stumble['kind']=='type':
                if not self.focus_type.get().strip():raise ValueError('Выберите вид или нажмите «Вид не определить».')
                draft['type']=self.focus_type.get().strip();self.study_type.set(draft['type'])
        except ValueError as error:
            self.focus_error.set(str(error));return 'break'
        self.resolve(document_id,stumble)
        return 'break'

    def accept_row(self,document_id,draft,stumble):
        from .parsing import result_fields
        values=self.read_row_fields();row=self.row_by_id(draft,stumble['result_id'])
        unit_changed=values['unit']!=str(row.get('unit','') or '')
        if any(values[name]!=str(row.get(name,'') or '') for name in values):
            row.update(values);row['number']=result_fields(values['result'])[1];row['manual_edited']=True
        draft['checked'].add(stumble['result_id'])
        if self.bulk.get():
            for other_document,other in self.similar:
                other_draft=self.draft(other_document)
                if unit_changed:self.row_by_id(other_draft,other['result_id'])['unit']=values['unit']
                other_draft['checked'].add(other['result_id'])
                self.resolve(other_document,other,move=False)

    def accept_unparsed(self,draft,stumble):
        from .parsing import result_fields
        values=self.read_row_fields()
        try:date=parse_date(draft['date'])
        except ValueError:date=''
        draft['observations'].append({**values,'number':result_fields(values['result'])[1],'page':stumble.get('page') or 1,
            'page_index':stumble.get('page_index'),'line':stumble.get('line') or 0,'raw':stumble['raw'],'date':date,
            'date_source':'Указано при сверке','origin':'Введено при сверке','section':''})
        draft['resolved'].append((stumble.get('page'),stumble.get('page_index'),stumble.get('line')))

    def accept_date(self,document_id,draft):
        value=parse_date(self.focus_date.get())
        if not value:raise ValueError('Укажите дату или нажмите «В бланке нет даты».')
        draft['date']=display_date(value);self.date.set(draft['date'])
        # Numbers in the text were flagged only because the document had no date.
        for other in self.open_stumbles(document_id):
            if other['kind']=='row' and set(other['codes'])=={'ambiguous_value'}:
                row=self.row_by_id(draft,other['result_id'])
                if not row.get('date') and row.get('origin')!='Лабораторный бланк' and row.get('label')!=UNNAMED_METRIC:
                    self.resolve(document_id,other,move=False)

    def dismiss(self):
        if self.full_mode or self.current is None or self.saving:return
        document_id=self.selected;stumble=self.current;draft=self.draft(document_id)
        if stumble['kind']=='row':
            row=self.row_by_id(draft,stumble['result_id'])
            if not messagebox.askyesno('Убрать строку','Убрать ошибочно извлечённую строку? Предыдущий результат сохранится в истории сверки.',parent=self.window):return
            draft['observations'].remove(row)
        elif stumble['kind']=='unparsed':draft['resolved'].append((stumble.get('page'),stumble.get('page_index'),stumble.get('line')))
        self.resolve(document_id,stumble)

    def skip(self,event=None):
        if self.full_mode or self.current is None or self.saving:return
        leaves=self.leaves()
        if len(leaves)>1 and self.selected_item in leaves:
            self.select_item(leaves[(leaves.index(self.selected_item)+1)%len(leaves)])

    def resolve(self,document_id,stumble,move=True):
        """Close one stumble, save the document when none remain, and show the next."""
        following=None
        if move:
            leaves=self.leaves()
            position=leaves.index(self.selected_item) if self.selected_item in leaves else -1
            following=next(iter(leaves[position+1:]+leaves[:max(position,0)]),None)
        draft=self.draft(document_id);draft['done'].add(stumble['key'])
        item=next((leaf for leaf,value in self.items.items() if value==(document_id,stumble['key'])),None)
        if item:
            del self.items[item];self.list.delete(item)
        if not self.open_stumbles(document_id):self.enqueue(document_id)
        self.update_progress()
        if move:
            self.current=None
            if following and self.list.exists(following):
                self.selected_item=None;self.select_item(following)
            elif self.list.get_children(''):
                self.selected_item=None;self.select_item(self.leaves()[0])
            else:self.finish_queue()

    # Saving

    def enqueue(self,document_id):
        if document_id==self.selected:
            self.stash();self.selected=None;self.selected_item=None
        self.pending[document_id]=self.records.pop(document_id)
        if self.list.exists(document_id):self.list.delete(document_id)
        self.save_queue.append(document_id)
        self.pump_saves()

    def pump_saves(self):
        if not self.save_queue:
            if self.close_requested:self.close_requested=False;self.close()
            elif not self.records:self.finish_queue()
            return
        if self.actions.active or self.actions.presenting:
            self.window.after(30,self.pump_saves);return
        document_id=self.save_queue[0];draft=self.drafts[document_id]
        try:date=parse_date(draft['date'])
        except ValueError as error:
            # A mistyped date must never be saved as «no date».
            self.save_queue.pop(0);self.restore(document_id,error);self.window.after_idle(self.pump_saves);return
        request=dict(date=date,study_type=draft['type'],observations=deepcopy(draft['observations']),sections=deepcopy(draft['sections']),note=self.AUTO_NOTE,
                     checked_ids=set(draft['checked']),resolved_lines=list(draft['resolved']),expected_revision=self.revision,
                     defer_export=True,skip_backup=self.backed_up)

        def work():
            return self.workspace.confirm_quality(document_id,**request)

        def completed(value,error):
            if not self.window.winfo_exists():return
            self.save_queue.pop(0)
            if error is None or isinstance(error,ReviewCommittedError):
                self.document_saved(document_id,value);self.pending.pop(document_id,None);self.drafts.pop(document_id,None);self.update_progress()
            else:self.restore(document_id,error)
            self.window.after_idle(self.pump_saves)

        if not self.actions.run(work,completed):self.window.after(30,self.pump_saves)

    def document_saved(self,document_id,value):
        self.session_saved=True;self.backed_up=True
        if isinstance(value,dict) and value.get('revision'):self.revision=value['revision']

    def restore(self,document_id,error):
        """Put a document whose save failed back in front of the reader, with the edits intact."""
        self.records[document_id]=self.pending.pop(document_id)
        self.fill_tree()
        self.selected_item=None;self.select_item(document_id)
        if isinstance(error,ValueError):self.validation.set(str(error))
        else:messagebox.showerror('Не удалось сохранить проверку',str(error),parent=self.window)

    def finish_queue(self):
        """Show the end of the queue. The workbook is rebuilt once, after the last save."""
        if self.records or self.save_queue:return
        self.selected=None;self.selected_item=None;self.current=None
        self.panes.pack_forget();self.head.pack_forget();self.success.pack(fill='both',expand=True)
        self.finish_session()

    def finish_session(self,then=None):
        if then:self.after_finish=then
        if not self.session_saved:
            callback,self.after_finish=self.after_finish,None
            if callback:callback()
            return
        if self.finishing:return
        self.finishing=True
        self.progress.configure(text='Обновляем таблицу Excel…');self.success_text.configure(text='Обновляем таблицу Excel…')

        def work():
            return self.workspace.finish_review_session()

        def completed(_value,error):
            self.finishing=False;self.session_saved=False
            self.on_change()
            if not self.window.winfo_exists():return
            self.success_text.configure(text='Исправления и история сверки сохранены')
            self.progress.configure(text='')
            if isinstance(error,ReviewCommittedError):messagebox.showwarning('Данные сохранены',str(error),parent=self.window)
            elif error is not None:messagebox.showerror('Не удалось обновить таблицу',str(error),parent=self.window)
            callback,self.after_finish=self.after_finish,None
            if callback:callback()

        def start():
            if not self.window.winfo_exists():return
            if not self.actions.run(work,completed):self.window.after(30,start)

        start()

    # Whole document

    def fill_rows(self):
        self.rows.configure(height=min(12,max(3,len(self.observations))))
        for item in self.rows.get_children():self.rows.delete(item)
        problems=set()
        if self.selected in self.records:
            problems={item['result_id'] for item in self.open_stumbles(self.selected) if item['kind']=='row'}
        if self.selected in self.records and review_queue.needs_full_review(self.records[self.selected]):
            problems|={self.observations[i['observation_index']].get('result_id') for i in self.records[self.selected]['issues']
                       if i.get('observation_index') is not None and i['observation_index']<len(self.observations)}
        first=None
        for index,o in enumerate(self.observations):
            flagged=o.get('result_id') in problems
            self.rows.insert('','end',iid=str(index),values=(o.get('label',''),o.get('result',''),o.get('unit','')),tags=('problem',) if flagged else ())
            if flagged and first is None:first=index
        self.source_label.configure(text='' if self.observations else 'Числовых строк нет. Сверьте текст заключения и полноту документа.')
        if self.observations:
            item=str(first or 0);self.rows.selection_set(item);self.rows.see(item);self.locate()

    def locate(self,event=None):
        selected=self.rows.selection()
        if not selected or not self.full_mode:return
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
            self.observations.pop(int(selected[0]));self.forget_removed_rows();self.fill_rows();self.verified.set(False);self.enable_confirm()

    def forget_removed_rows(self):
        """A removed row has nothing left to ask. The document stays open in front of the reader."""
        draft=self.draft(self.selected)
        present={row.get('result_id') for row in draft['observations']}
        for stumble in self.open_stumbles(self.selected):
            if stumble['kind']=='row' and stumble['result_id'] not in present:
                draft['done'].add(stumble['key'])
                item=next((leaf for leaf,value in self.items.items() if value==(self.selected,stumble['key'])),None)
                if item:
                    del self.items[item];self.list.delete(item)
        self.update_progress()

    def confirm(self):
        if self.saving or not self.selected or not self.verified.get():return
        if self.actions.active or self.save_queue:
            self.window.after(150,self.confirm);return
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
        revision=self.revision
        observations,sections=deepcopy(self.observations),deepcopy(self.sections)
        resolved=list(self.draft(document_id)['resolved']);skip_backup=self.backed_up

        def work():
            return self.workspace.confirm_quality(document_id,date=date,study_type=kind,observations=observations,
                sections=sections,note=note or 'Сверены все страницы и строки оригинала',expected_revision=revision,
                resolved_lines=resolved,defer_export=True,skip_backup=skip_backup)

        def completed(value,error):
            self.set_saving(False)
            if isinstance(error,ValueError):
                self.validation.set(str(error));self.editor_area.canvas.yview_moveto(1)
                return
            if error is None or isinstance(error,ReviewCommittedError):
                self.document_saved(document_id,value)
                self.close_document(document_id)
                if error is not None:
                    messagebox.showwarning('Данные сохранены',str(error),parent=self.window)
            else:
                messagebox.showerror('Не удалось сохранить проверку',str(error),parent=self.window)

        if self.actions.run(work,completed):
            self.validation.set('')
            self.set_saving(True)

    def close_document(self,document_id):
        self.selected=None;self.selected_item=None;self.current=None
        self.records.pop(document_id,None);self.drafts.pop(document_id,None)
        for item,value in list(self.items.items()):
            if value[0]==document_id:del self.items[item]
        if self.list.exists(document_id):self.list.delete(document_id)
        self.verified.set(False);self.enable_confirm();self.update_progress()
        if self.list.get_children(''):self.select_item(self.leaves()[0])
        else:self.finish_queue()

    def compare_duplicate(self):
        if self.actions.active or not self.selected:return
        record=self.records[self.selected]
        issue=next((i for i in record['issues'] if i['code']=='possible_duplicate'),{})
        target=next(iter(issue.get('related_ids',[])),None)
        if not target:
            messagebox.showinfo('Возможный дубль','Откройте «Все исследования» в меню «Папка данных», чтобы сравнить оригиналы. Сходство текста ещё не означает совпадение результатов.',parent=self.window);return
        self.duplicate_window=DuplicateWindow(self.window,self.workspace,{**record,'revision':self.revision},target,self.icons,self.duplicate_done,actions=self.actions)

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


class PatientDialog:
    """Correct the owner of one study. Nothing is saved until the person presses the button."""

    def __init__(self,parent,record,on_save):
        self.record=record;self.on_save=on_save
        self.window=tk.Toplevel(parent);self.window.title('Сменить пациента');self.window.configure(bg='white')
        self.window.geometry('520x330');self.window.transient(parent);self.window.grab_set()
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        outer=ttk.Frame(self.window,padding=20);outer.pack(fill='both',expand=True)
        ttk.Label(outer,text=record.get('filename',''),style='Heading.TLabel',wraplength=470).pack(anchor='w')
        ttk.Label(outer,text='Сейчас: '+(record.get('patient_name') or 'пациент не назначен'),style='Muted.TLabel').pack(anchor='w',pady=(4,12))
        self.name=tk.StringVar();self.birth=tk.StringVar();self.error=tk.StringVar()
        ttk.Label(outer,text='Фамилия, имя и отчество').pack(anchor='w')
        name_entry=ttk.Entry(outer,textvariable=self.name);name_entry.pack(fill='x',pady=(4,10));name_entry.focus_set()
        ttk.Label(outer,text='Дата рождения, ДД.ММ.ГГГГ').pack(anchor='w')
        ttk.Entry(outer,textvariable=self.birth,width=16).pack(anchor='w',pady=(4,8))
        ttk.Label(outer,textvariable=self.error,foreground='#b42318',wraplength=470).pack(anchor='w')
        self.save_button=ttk.Button(outer,text='Сменить пациента',style='Primary.TButton',command=self.save);self.save_button.pack(side='bottom',anchor='e')

    def save(self):
        try:
            birth=parse_date(self.birth.get())
            if not birth or not self.name.get().strip():raise ValueError('Укажите фамилию, имя и дату рождения.')
        except ValueError as exc:
            self.error.set(str(exc));return
        if self.on_save(self.record['id'],self.name.get().strip(),birth,self):
            self.save_button.configure(state='disabled',text='Сохраняем…')

    def finish(self,error):
        if error is None or isinstance(error,ReviewCommittedError):
            self.close();return
        self.save_button.configure(state='normal',text='Сменить пациента');self.error.set(str(error))

    def close(self):
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
        self.change_button=ttk.Button(buttons,text='Сменить пациента',command=self.change_patient);self.change_button.pack(side='right',padx=8)
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

    def change_patient(self):
        selected=self.list.selection()
        if not selected or self.actions.active:return
        self.patient_dialog=PatientDialog(self.window,self.records[selected[0]],self.save_patient)

    def save_patient(self,document_id,name,birth,dialog):
        def work():
            return self.workspace.change_patient(document_id,name,birth)

        def completed(_value,error):
            dialog.finish(error)
            if error is None or isinstance(error,ReviewCommittedError):
                self.on_change();self.fill()
                self.status.set('Пациент изменён. Прежний пациент записан в журнал сверки.')
                if error is not None:messagebox.showwarning('Пациент изменён',str(error),parent=self.window)

        return self.actions.run(work,completed)

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
