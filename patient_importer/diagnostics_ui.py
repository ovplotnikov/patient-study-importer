"""Small opt-in and contribution windows; neither receives clinical content."""
import tkinter as tk
from tkinter import ttk, messagebox
import queue
import threading
import webbrowser
from urllib.parse import urlsplit

from . import ui
from .support import SUPPORT_STEPS, SUPPORT_CAUTION, support_mailto


class DiagnosticsWindow:
    def __init__(self, parent, diagnostics, endpoint, *, first_run=False, on_change=lambda: None):
        self.diagnostics = diagnostics
        self.on_change = on_change
        self.window = tk.Toplevel(parent)
        self.window.title('Добровольная диагностика')
        self.window.geometry(f'640x{min(680, parent.winfo_screenheight()-120)}')
        self.window.minsize(520, 480)
        self.window.transient(parent)
        self.window.grab_set()
        outer = ttk.Frame(self.window, padding=22)
        outer.pack(fill='both', expand=True)
        actions = ttk.Frame(outer)
        actions.pack(side='bottom', fill='x', pady=(18, 0))
        body = ui.ScrollArea(outer)
        body.pack(fill='both', expand=True)
        self.status = tk.StringVar(value='Включена' if diagnostics.enabled else 'Выключена')
        self.labels = []
        def label(text, style='TLabel'):
            item = ttk.Label(body.body, text=text, style=style, wraplength=550)
            item.pack(anchor='w', fill='x', pady=(0, 12))
            self.labels.append(item)
        label('Помочь улучшить программу?', 'Heading.TLabel')
        label('Разрешить отправку технических счётчиков? '
              'Программа работает при любом выборе.')
        label('Что отправляется', 'Heading.TLabel')
        label('Версия программы, число обработок по форматам, коды ошибок, '
              'число сверок и категории исправлений.')
        label('Что остаётся на компьютере', 'Heading.TLabel')
        label('Бланки, изображения, ФИО, результаты, имена файлов, пути, заметки и журналы. '
              'В отчёте нет идентификатора пользователя.')
        label('Отправка в фоне, не чаще раза в сутки. '
              'Изменить выбор: «Помощь → Диагностика».')
        if urlsplit(endpoint).hostname == 'script.google.com':
            label('Получатель: разработчик программы. Счётчики — в закрытой Google-таблице. '
                  'Google видит IP и служебные сведения о соединениях и работе обработчика.', 'Muted.TLabel')
        elif endpoint:
            label('Получатель: ' + urlsplit(endpoint).netloc + '. Сервер видит IP соединения.', 'Muted.TLabel')
        else:
            label('Приёмник диагностики в этой сборке ещё не подключён. '
                  'Сбор и отправка выключены.', 'Muted.TLabel')
        label('Выбор сохраняется после обновлений. При отключении очередь удаляется. '
              'Уже начатый запрос может завершиться.', 'Muted.TLabel')
        ttk.Label(actions, textvariable=self.status, style='Muted.TLabel').pack(side='left')
        self.decline_button = ttk.Button(actions, text=('Не отправлять' if first_run else 'Выключить')
                                        if diagnostics.configured else 'Закрыть',
                                        command=(lambda: self.choose(False)) if diagnostics.configured else self.window.destroy)
        self.decline_button.pack(side='right', padx=(8, 0))
        self.allow_button = ttk.Button(actions, text='Разрешить', style='Primary.TButton',
                                      command=lambda: self.choose(True),
                                      state='normal' if diagnostics.configured else 'disabled')
        self.allow_button.pack(side='right')
        self.window.bind('<Configure>', lambda event: self.resize(event))
        self.window.protocol('WM_DELETE_WINDOW', self.window.destroy)

    def resize(self, event):
        if event.widget is self.window:
            for label in self.labels:
                label.configure(wraplength=max(360, event.width-70))

    def choose(self, enabled):
        self.allow_button.configure(state='disabled');self.decline_button.configure(state='disabled')
        self.status.set('Сохраняем выбор…')
        self.window.protocol('WM_DELETE_WINDOW', lambda: None)
        result = queue.Queue()
        def save():
            try:result.put(self.diagnostics.set_consent(enabled))
            except Exception:result.put(False)
        threading.Thread(target=save, daemon=True).start()
        def poll():
            try:saved = result.get_nowait()
            except queue.Empty:
                self.window.after(50, poll);return
            if saved:
                self.on_change();self.window.destroy();return
            self.window.protocol('WM_DELETE_WINDOW', self.window.destroy)
            self.status.set('Включена' if self.diagnostics.enabled else 'Выключена')
            self.allow_button.configure(state='normal' if self.diagnostics.configured else 'disabled')
            self.decline_button.configure(state='normal')
            messagebox.showerror('Выбор не сохранён',
                                 'Не удалось сохранить настройку. Повторите действие.', parent=self.window)
        self.window.after(50, poll)


class SupportWindow:
    def __init__(self, parent, address):
        self.address = address
        self.window = tk.Toplevel(parent)
        self.window.title('Помочь с новым бланком')
        self.window.geometry(f'650x{min(680, parent.winfo_screenheight()-120)}')
        self.window.minsize(540, 480)
        self.window.transient(parent)
        self.window.grab_set()
        outer = ttk.Frame(self.window, padding=22)
        outer.pack(fill='both', expand=True)
        actions = ttk.Frame(outer)
        actions.pack(side='bottom', fill='x', pady=(16, 0))
        self.checked = tk.BooleanVar(value=False)
        self.attest = ttk.Checkbutton(actions, text='Подготовлен пустой или вымышленный пример',
                                     variable=self.checked, command=self.update_button)
        self.attest.pack(anchor='w', pady=(0, 12))
        self.mail_button = ttk.Button(actions, text='Открыть письмо', command=self.compose,
                                      style='Primary.TButton', state='disabled')
        self.mail_button.pack(side='right')
        ttk.Button(actions, text='Закрыть', command=self.window.destroy).pack(side='right', padx=10)
        body = ui.ScrollArea(outer)
        body.pack(fill='both', expand=True)
        self.labels = []
        for index, text in enumerate(('Помогите добавить поддержку формы',) + SUPPORT_STEPS + (SUPPORT_CAUTION,)):
            item = ttk.Label(body.body, text=(f'{index}. ' if 1 <= index <= 3 else '') + text,
                             style='Heading.TLabel' if index == 0 else 'TLabel', wraplength=570)
            item.pack(anchor='w', fill='x', pady=(0, 18))
            self.labels.append(item)
        try:
            support_mailto(address)
            self.address_valid = True
        except ValueError:
            self.address_valid = False
        text = ('Адрес: ' + address) if self.address_valid else 'Адрес поддержки в этой сборке ещё не настроен.'
        ttk.Label(body.body, text=text, style='Muted.TLabel', wraplength=550).pack(anchor='w')
        self.window.bind('<Configure>', self.resize)

    def resize(self, event):
        if event.widget is self.window:
            for label in self.labels:
                label.configure(wraplength=max(380, event.width-70))

    def update_button(self):
        self.mail_button.configure(state='normal' if self.checked.get() and self.address_valid else 'disabled')

    def compose(self):
        if not self.checked.get() or not self.address_valid:
            return
        # No document, screenshot, workspace or free text is accepted here.
        try:
            opened = webbrowser.open(support_mailto(self.address))
            if not opened:
                raise OSError()
        except (OSError, ValueError):
            messagebox.showinfo('Откройте почту самостоятельно',
                                'Отправьте подготовленный учебный пример на ' + self.address +
                                '. Программа не отправляла письмо.', parent=self.window)
