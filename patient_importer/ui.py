"""Shared desktop presentation primitives and bundled library icons."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

BLUE='#075bbe'; INK='#17212f'; MUTED='#586779'; LINE='#dbe2ea'; PALE='#eef5ff'
FONT='Segoe UI'
ASSETS=Path(__file__).resolve().parent/'assets'


def setup(root):
    global FONT
    import os
    FONT='Segoe UI' if os.name=='nt' else 'Helvetica'
    root.configure(bg='white')
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('.',font=(FONT,12),background='white',foreground=INK)
    style.configure('TFrame',background='white')
    style.configure('Drop.TFrame',background=PALE,bordercolor='#a9c5e9',relief='solid',borderwidth=1)
    style.configure('Drop.TLabel',background=PALE,foreground=MUTED)
    style.configure('TLabel',background='white',foreground=INK)
    style.configure('Muted.TLabel',foreground=MUTED)
    style.configure('Title.TLabel',font=(FONT,22,'bold'))
    style.configure('Heading.TLabel',font=(FONT,18,'bold'))
    style.configure('TButton',padding=(14,10),background='white',bordercolor=LINE,focuscolor=BLUE,relief='solid',borderwidth=1,lightcolor=LINE,darkcolor=LINE)
    style.map('TButton',background=[('active',PALE)],foreground=[('disabled','#8793a1')])
    style.configure('Primary.TButton',background=BLUE,foreground='white',lightcolor=BLUE,darkcolor=BLUE,font=(FONT,13,'bold'),bordercolor=BLUE,padding=(20,13))
    style.map('Primary.TButton',background=[('disabled','#d8e3ef'),('active','#004992')],foreground=[('disabled','#657487'),('!disabled','white')])
    style.configure('Link.TButton',borderwidth=0,relief='flat',bordercolor='white',foreground=BLUE,padding=(8,8))
    style.configure('TEntry',padding=8,fieldbackground='white',bordercolor=LINE)
    style.map('TEntry',bordercolor=[('focus',BLUE)])
    style.configure('Horizontal.TProgressbar',background=BLUE,troughcolor=PALE,borderwidth=0,thickness=8)
    style.configure('Treeview',rowheight=38,fieldbackground='white',background='white',borderwidth=0)
    style.configure('Treeview.Heading',font=(FONT,11,'bold'),background=PALE,padding=8)
    style.map('Treeview',background=[('selected',PALE)],foreground=[('selected',INK)])


class Icons:
    def __init__(self,root):self.root=root;self.cache={}
    def get(self,name,color='blue',size=24):
        key=(name,color,size)
        if key not in self.cache:
            path=ASSETS/'icons'/f'{name}-{color}.png'
            self.cache[key]=ImageTk.PhotoImage(Image.open(path).resize((size,size),Image.Resampling.LANCZOS),master=self.root)
        return self.cache[key]
    def file(self,name):
        suffix=Path(name).suffix.lower()
        if suffix=='.pdf':return self.get('file-earmark-pdf','red',38)
        if suffix=='.docx':return self.get('file-earmark-word','blue',38)
        if suffix in {'.png','.jpg','.jpeg','.bmp','.tif','.tiff'}:return self.get('file-earmark-image','green',38)
        return self.get('file-earmark','muted',38)


class ScrollArea(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.canvas=tk.Canvas(self,bg='white',highlightthickness=0)
        scrollbar=ttk.Scrollbar(self,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y');self.canvas.pack(side='left',fill='both',expand=True)
        self.body=ttk.Frame(self.canvas)
        self.item=self.canvas.create_window(0,0,anchor='nw',window=self.body)
        self.body.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self.item,width=e.width))
        self.canvas.bind('<Enter>',self._bind_wheel)
        self.canvas.bind('<Leave>',lambda e:self.canvas.unbind_all('<MouseWheel>'))
        self.bind('<Destroy>',lambda e:self.canvas.unbind_all('<MouseWheel>') if e.widget is self else None)
    def _bind_wheel(self,event):
        self.canvas.bind_all('<MouseWheel>',lambda e:self.canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
    def clear(self):
        for child in self.body.winfo_children():child.destroy()
        self.canvas.yview_moveto(0)


def reason_text(record):
    reason=record['reason']
    if reason=='Недостаточно идентификаторов':
        missing=[]
        if not record['names']:missing.append('ФИО')
        if not record['births']:missing.append('дата рождения')
        return {'ФИО':'Не найдено ФИО','дата рождения':'Не найдена дата рождения'}.get(', '.join(missing),'Не найдены ФИО и дата рождения') if missing else 'Проверьте ФИО и дату рождения'
    return reason


def file_error(message):
    lower=message.lower()
    if 'excel' in lower:return 'Закройте таблицу в Excel и повторите обработку.'
    if any(word in lower for word in ['pdf','eof','stream','startxref']):return 'Не удалось прочитать PDF. Откройте исходник и сохраните новую копию.'
    if 'permission' in lower or 'доступ' in lower:return 'Нет доступа к файлу. Закройте его в другой программе и повторите попытку.'
    if 'текст' in lower:return 'Не удалось прочитать текст. Проверьте качество и ориентацию скана.'
    return 'Не удалось прочитать файл. Проверьте, открывается ли он, и попробуйте снова.'
