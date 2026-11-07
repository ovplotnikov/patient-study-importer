"""Versioned offline help travels inside the executable, including screenshots."""
import os
from pathlib import Path
import webbrowser
from tkinter import messagebox
from . import __version__


def help_path():
    source=Path(__file__).resolve().parent/'assets'/'help'/'guide.html'
    if not source.is_file():raise FileNotFoundError('Справка ещё не собрана. Выполните packaging/build_guide.py при сборке программы.')
    root=Path(os.environ.get('LOCALAPPDATA',os.environ.get('APPDATA',str(Path.home()/'.cache'))))/'PatientStudyImporter'/'help'/__version__
    root.mkdir(parents=True,exist_ok=True)
    target=root/'guide.html';temporary=root/'guide.tmp'
    temporary.write_bytes(source.read_bytes());temporary.replace(target)
    return target


def open_help(parent):
    try:webbrowser.open(help_path().as_uri())
    except OSError as exc:messagebox.showerror('Не удалось открыть помощь',str(exc),parent=parent)
