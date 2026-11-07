"""Opt-in installation after an automatic check; never opens the clinical workspace."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from . import __version__

REPOSITORY = 'ovplotnikov/patient-study-importer'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
ARCHIVE = 'PatientStudyImporter-Windows.zip'
EXE = 'Исследования пациентов.exe'
DATA_FORMAT = 'registry-v2'
MAX_DOWNLOAD = 300 * 1024 * 1024
MAX_EXE = 400 * 1024 * 1024


class UpdateError(Exception):
    pass


class Cancelled(UpdateError):
    pass


def version(value):
    match = re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value)
    if not match:
        raise UpdateError('Непонятный номер версии обновления.')
    return tuple(int(x) for x in match.groups())


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def trusted_url(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == 'https' and parsed.hostname in
            {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
            and not parsed.username and not parsed.password and parsed.port in (None, 443))


class GithubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_url(newurl):
            raise UpdateError('Сервер перенаправил загрузку на неизвестный адрес.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    if not trusted_url(url):
        raise UpdateError('Недопустимый адрес обновления.')
    request = urllib.request.Request(url, headers={'User-Agent': f'PatientStudyImporter/{__version__}'})
    try:
        return urllib.request.build_opener(GithubRedirect()).open(request, timeout=20)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError('Не удалось связаться с GitHub. Проверьте интернет и попробуйте при следующем запуске.') from exc


def read_small(url, limit):
    with open_url(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError('Слишком большой ответ сервера обновлений.')
    return data


@dataclass(frozen=True)
class Release:
    tag: str
    archive_url: str
    checksum_url: str
    size: int
    api_digest: str = ''


def check_for_update(current=__version__):
    try:
        data = json.loads(read_small(API_URL, 1024 * 1024))
        if data.get('draft') or data.get('prerelease'):
            return None
        tag = data['tag_name']
        if version(tag) <= version(current):
            return None
        assets = {a['name']: a for a in data['assets'] if a.get('state') == 'uploaded'}
        archive, checksum = assets[ARCHIVE], assets['PatientStudyImporter-Windows.sha256']
        prefix = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
        if any(not a['browser_download_url'].startswith(prefix) for a in (archive, checksum)):
            raise UpdateError('Выпуск содержит постороннюю ссылку.')
        size = archive['size']
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
            raise UpdateError('Недопустимый размер обновления.')
        return Release(tag, archive['browser_download_url'], checksum['browser_download_url'], size,
                       archive.get('digest') or '')
    except (KeyError, ValueError, TypeError) as exc:
        raise UpdateError('Описание обновления повреждено. Текущая программа продолжит работать.') from exc


def cache_root():
    return Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'PatientStudyImporter' / 'Updates'


def cleanup_cache():
    root = cache_root()
    if not root.exists():
        return
    for path in root.iterdir():
        try:
            if (not path.is_symlink() and re.fullmatch(r'update-[0-9a-f]{32}', path.name)
                    and (path / 'owned-cache').is_file() and time.time() - path.stat().st_mtime > 7 * 86400):
                shutil.rmtree(path)
        except OSError:
            pass


def validate_archive(archive, stage, tag):
    with zipfile.ZipFile(archive) as bundle:
        seen = set()
        total = 0
        for item in bundle.infolist():
            name = item.filename
            parts = PurePosixPath(name).parts
            key = name.rstrip('/').casefold()
            if (not parts or name.startswith('/') or '\\' in name or ':' in name or '..' in parts
                    or key in seen or stat.S_ISLNK(item.external_attr >> 16)):
                raise UpdateError('Небезопасная структура архива обновления.')
            seen.add(key)
            allowed = (name in {EXE, 'update.json', 'Инструкция.txt', 'SHA256.txt', 'build-info.json'}
                       or parts[0] in {'docs', 'Лицензии'})
            if not allowed:
                raise UpdateError('В обновлении есть неизвестные файлы.')
            total += item.file_size
            if total > 600 * 1024 * 1024:
                raise UpdateError('Распакованное обновление слишком большое.')
        if bundle.getinfo('update.json').file_size > 32768:
            raise UpdateError('Повреждён паспорт обновления.')
        manifest = json.loads(bundle.read('update.json'))
        if (manifest.get('format') != 1 or version(manifest['version']) != version(tag)
                or manifest.get('data_format') != DATA_FORMAT):
            raise UpdateError('Эта версия требует отдельной инструкции обновления. Автоматическая установка отменена.')
        expected = manifest['exe_sha256']
        if not re.fullmatch('[0-9a-f]{64}', expected) or not 0 < bundle.getinfo(EXE).file_size <= MAX_EXE:
            raise UpdateError('Неверный паспорт программы.')
        candidate = stage / 'candidate.exe'
        with bundle.open(EXE) as source, candidate.open('wb') as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
        with candidate.open('rb') as source:
            header = source.read(2)
        if digest(candidate) != expected or header != b'MZ':
            raise UpdateError('Проверка файла программы не пройдена.')
        return manifest


def prepare_update(release, cancel, progress=lambda value: None, root=None):
    root = Path(root) if root is not None else cache_root()
    root.mkdir(parents=True, exist_ok=True)
    stage = root / ('update-' + uuid.uuid4().hex)
    stage.mkdir()
    (stage / 'owned-cache').write_text('PatientStudyImporter', encoding='ascii')
    try:
        checksum = read_small(release.checksum_url, 4096).decode('ascii').strip()
        match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?PatientStudyImporter-Windows\.zip', checksum)
        if not match:
            raise UpdateError('Повреждена контрольная сумма обновления.')
        expected = match[1].lower()
        if release.api_digest and release.api_digest != 'sha256:' + expected:
            raise UpdateError('Контрольные суммы выпуска не совпадают.')
        archive = stage / ARCHIVE
        received = 0
        started = time.monotonic()
        with open_url(release.archive_url) as response, archive.open('wb') as output:
            while True:
                if cancel.is_set():
                    raise Cancelled('Обновление отменено.')
                if time.monotonic() - started > 15 * 60:
                    raise UpdateError('Загрузка заняла слишком много времени. Попробуйте позже.')
                block = response.read(1024 * 1024)
                if not block:
                    break
                received += len(block)
                if received > release.size or received > MAX_DOWNLOAD:
                    raise UpdateError('Размер загрузки не соответствует выпуску.')
                output.write(block)
                progress(min(99, received * 100 // release.size))
        if received != release.size or digest(archive) != expected:
            raise UpdateError('Загрузка повреждена. Установленная программа не изменена.')
        if cancel.is_set():
            raise Cancelled('Обновление отменено.')
        manifest = validate_archive(archive, stage, release.tag)
        manifest['archive_sha256'] = expected
        (stage / 'manifest.json').write_text(json.dumps(manifest), encoding='utf8')
        archive.unlink()
        progress(100)
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def detached_environment():
    env = dict(os.environ)
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return env


def launch_install(stage, target, test=False):
    stage, target = Path(stage).resolve(), Path(target).resolve()
    if target.suffix.lower() != '.exe' or not target.is_file():
        raise UpdateError('Не найден установленный EXE.')
    # Check write access before closing the user's application.
    with tempfile.NamedTemporaryFile(prefix='.patient-update-write-', dir=target.parent):
        pass
    manifest = json.loads((stage / 'manifest.json').read_text(encoding='utf8'))
    plan = {'format': 1, 'target': str(target), 'old_sha256': digest(target),
            'new_sha256': manifest['exe_sha256'], 'version': manifest['version'],
            'archive_sha256': manifest['archive_sha256'], 'test': test}
    plan_path = stage / 'plan.json'
    plan_path.write_text(json.dumps(plan), encoding='utf8')
    helper = stage / 'updater.exe'
    shutil.copy2(sys.executable, helper)
    return subprocess.Popen([str(helper), '--apply-update', str(plan_path)], env=detached_environment(),
                            creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_update(plan_path, progress=lambda text: None, launcher=None, timeout=90):
    """Replace one EXE transactionally. No workspace or settings are read or modified."""
    import portalocker
    plan_path = Path(plan_path).resolve()
    stage = plan_path.parent
    plan = json.loads(plan_path.read_text(encoding='utf8'))
    target = Path(plan['target'])
    candidate = stage / 'candidate.exe'
    if (plan.get('format') != 1 or not target.is_absolute() or target.suffix.lower() != '.exe'
            or target.resolve() == Path(sys.executable).resolve()):
        raise UpdateError('Повреждено задание установки.')
    version(plan['version'])
    if digest(candidate) != plan['new_sha256']:
        raise UpdateError('Загруженная программа изменилась после проверки.')
    progress('Проверяем запуск новой версии…')
    probe = stage / 'probe.json'
    result = subprocess.run([str(candidate), '--update-probe', str(probe)], env=detached_environment(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=60)
    if result.returncode or not probe.is_file() or json.loads(probe.read_text())['version'] != plan['version']:
        raise UpdateError('Новая версия не смогла запуститься. Прежняя программа сохранена.')
    backup = target.with_name(target.stem + '.previous.exe')
    temporary = target.with_name('.patient-update-' + uuid.uuid4().hex + '.exe')
    backup_temporary = target.with_name('.patient-backup-' + uuid.uuid4().hex + '.exe')
    receipt = target.parent / 'installation.json'
    receipt_before = receipt.read_bytes() if receipt.exists() else None
    installed = False
    launcher = launcher or (lambda command: subprocess.Popen(command, env=detached_environment(),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        with portalocker.Lock(str(target.parent / '.patient-update.lock'), timeout=0):
            if digest(target) != plan['old_sha256']:
                raise UpdateError('Установленная программа уже изменилась. Запустите её и проверьте обновления снова.')
            shutil.copy2(candidate, temporary)
            with temporary.open('rb+') as source:
                os.fsync(source.fileno())
            if digest(temporary) != plan['new_sha256']:
                raise UpdateError('Не удалось записать новую программу.')
            shutil.copy2(target, backup_temporary)
            with backup_temporary.open('rb+') as source:
                os.fsync(source.fileno())
            if digest(backup_temporary) != plan['old_sha256']:
                raise UpdateError('Не удалось сохранить прежнюю версию.')
            os.replace(backup_temporary, backup)
            progress('Ожидаем закрытия программы…')
            deadline = time.monotonic() + timeout
            while True:
                try:
                    os.replace(temporary, target)
                    installed = True
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise UpdateError('Программа ещё открыта или папка недоступна. Закройте остальные окна и попробуйте снова.')
                    time.sleep(.3)
            progress('Устанавливаем обновление…')
            record = {'repository': REPOSITORY, 'version': 'v' + plan['version'].lstrip('v'),
                      'archive_sha256': plan['archive_sha256'], 'exe_sha256': plan['new_sha256'],
                      'installed_at': datetime.now(timezone.utc).isoformat()}
            receipt_tmp = stage / 'installation.json'
            receipt_tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
            # Copy receipt through the target volume before atomic rename.
            receipt_local = target.parent / ('.patient-receipt-' + uuid.uuid4().hex + '.json')
            try:
                shutil.copy2(receipt_tmp, receipt_local)
                os.replace(receipt_local, receipt)
            finally:
                receipt_local.unlink(missing_ok=True)
            progress('Запускаем программу…')
            command = [str(target)]
            if plan.get('test'):
                command += ['--update-probe', str(stage / 'restart-probe.json')]
            (stage / 'result.json').write_text(json.dumps({'ok': True, 'version': plan['version']}), encoding='utf8')
            process = launcher(command)
            if plan.get('test') and process is not None and process.wait(timeout=60) != 0:
                raise UpdateError('Обновлённая программа не прошла проверку перезапуска.')
    except Exception:
        if installed:
            try:
                os.replace(backup, target)
                if receipt_before is None:
                    receipt.unlink(missing_ok=True)
                else:
                    receipt.write_bytes(receipt_before)
            except OSError as rollback_error:
                raise UpdateError('Восстановление EXE не завершено. Закройте программу и восстановите сохранённый файл .previous.exe в папке программы. Рабочие данные не изменялись установщиком.') from rollback_error
        raise
    finally:
        temporary.unlink(missing_ok=True)
        backup_temporary.unlink(missing_ok=True)
    return backup


def run_helper(plan_path):
    """Separate frozen process survives the old PyInstaller process exiting."""
    import tkinter as tk
    from tkinter import messagebox, ttk
    import queue
    events = queue.Queue()
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_text(encoding='utf8'))
    window = tk.Tk()
    window.title('Обновление программы')
    window.geometry('500x145')
    window.resizable(False, False)
    status = tk.StringVar(value='Подготавливаем обновление…')
    ttk.Label(window, textvariable=status, padding=20, wraplength=460).pack()
    ttk.Label(window, text='Исследования и таблица остаются на месте.').pack()
    window.protocol('WM_DELETE_WINDOW', lambda: None)
    if plan.get('test'):
        window.withdraw()
    def work():
        try:
            apply_update(plan_path, lambda text: events.put(('progress', text)))
            events.put(('done', None))
        except Exception as exc:
            message = str(exc) if isinstance(exc, UpdateError) else 'Не удалось заменить программу. Прежний EXE сохранён; данные не изменялись.'
            try:
                (plan_path.parent / 'result.json').write_text(json.dumps({'ok': False, 'error': message}), encoding='utf8')
            except OSError:
                pass
            events.put(('error', message))
    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == 'progress':
                    status.set(value)
                else:
                    if kind == 'error' and not plan.get('test'):
                        messagebox.showerror('Обновление не установлено', value, parent=window)
                    window.destroy()
                    return
        except queue.Empty:
            pass
        window.after(100, poll)
    threading.Thread(target=work, daemon=False).start()
    window.after(100, poll)
    window.mainloop()
