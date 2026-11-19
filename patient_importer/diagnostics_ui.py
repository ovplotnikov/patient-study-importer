"""Small opt-in and contribution windows; neither receives clinical content."""
import json
import tkinter as tk
from tkinter import ttk, messagebox
import queue
import threading
import webbrowser
from urllib.parse import urlsplit

from . import ui
from .support import SUPPORT_STEPS, SUPPORT_CAUTION, support_mailto

# What the doctor agrees to, in plain words. docs/TELEMETRY.md lists every code.
SENT = (
    'Версия программы и суммы по заранее заданным кодам: сколько файлов каких форматов обработано '
    'и чем закончилось, причины пометок по видам бланков, сколько строк извлечено (диапазонами), '
    'порядок столбцов в шапке лабораторной таблицы условными названиями («показатель», «результат», «неизвестный»).',
    'Раз в неделю — запись об активности без идентификатора: давность установки, Windows 10 или 11, '
    'число ядер, память и масштаб экрана диапазонами, сколько документов и пациентов в базе, '
    'сколько ждут пациента и какая доля с пометками — тоже диапазонами.',
    'Действия в окнах по закрытому списку: добавление файлов, начало и конец обработки, открытие таблицы, '
    'окон пациента и сверки, способ подтверждения пациента, «Открыть файл», «Показать в папке», обновления.',
    'Что исправлено при сверке: сколько строк подтверждено без правок и какие поля исправлены '
    '(результат, единица, референс, название, дата) — без самих значений.',
    'При сбое — вид ошибки и место в коде программы (модуль, функция, номер строки), без текста ошибки. '
    'Отдельно — что прошлый сеанс завершился аварийно.',
    'Скорость диапазонами: обработка документа и скана, сохранение таблицы, запуск.',
)
NOT_SENT = ('ФИО, даты рождения, результаты и тексты документов, имена файлов и папок, пути, текст ошибок, '
            'заметки и журналы. В сообщении нет идентификатора установки, компьютера или пользователя, '
            'и программа не отправляет время: неделю получения ставит приёмник по своим часам.')
EXAMPLE_REPORT = {
    'schema_version': 2, 'app_version': '2.6.0',
    'counters': {
        'activity': {'active': 1, 'age.1_3m': 1, 'os.win11': 1, 'documents.100_499': 1},
        'path': {'add_folder': 1, 'import_done': 1, 'table_opened': 2},
        'reading': {'outcome.cbc.saved': 4, 'flag.ultrasound.missing_date': 1},
        'errors': {'window.KeyError.quality_ui.load.512': 1},
    },
}


class DiagnosticsWindow:
    def __init__(self, parent, diagnostics, endpoint, *, first_run=False, on_change=lambda: None):
        self.diagnostics = diagnostics
        self.on_change = on_change
        self.window = tk.Toplevel(parent)
        self.window.title('Добровольная диагностика')
        self.window.minsize(520, 480)
        ui.place_window(self.window, 640, 680)
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
        label('Разрешить отправку технических счётчиков? Программа работает при любом выборе.')
        label('Если вы уже отвечали раньше: состав диагностики изменился, поэтому программа спрашивает снова.',
              'Muted.TLabel')
        label('Что отправляется', 'Heading.TLabel')
        for line in SENT:
            label('• ' + line)
        label('Что не отправляется', 'Heading.TLabel')
        label(NOT_SENT)
        label('Пример сообщения (вымышленный)', 'Heading.TLabel')
        example = tk.Text(body.body, height=9, wrap='word', font=('Courier New', 10), relief='flat',
                          background='#f1f4f8', padx=8, pady=6)
        example.insert('1.0', json.dumps(EXAMPLE_REPORT, ensure_ascii=True, indent=1, sort_keys=True))
        example.configure(state='disabled')
        example.pack(anchor='w', fill='x', pady=(0, 12))
        self.example = example
        label('Отправка в фоне, раз в сутки: уходит всё накопленное. При обновлении программы '
              'накопленное не стирается и уходит с пометкой той версии, где набрано. '
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
        self.window.minsize(540, 480)
        ui.place_window(self.window, 650, 680)
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
