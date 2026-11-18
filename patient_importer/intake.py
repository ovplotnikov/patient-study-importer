"""Safe staging for file-picker/drop input. External originals are never moved."""
import hashlib
from pathlib import Path
import os
import shutil
import uuid
from .extraction import supported
from .storage import MANAGED, sha256


def available_path(folder, name):
    path=folder/name
    stem,suffix=path.stem,path.suffix
    index=2
    while path.exists():
        path=folder/f'{stem} ({index}){suffix}';index+=1
    return path


# Leaves room for the longest service folder name the file is later filed under.
STAGED_PATH_LIMIT=220
FOLDER_NAME_LIMIT=65
# A shortened name keeps its start and gets «~» and a hash of the whole: different names stay different.
HASH_LENGTH=8
COLLAPSED_START=24
SHORT_STEM=40
# Room kept for a file name when deciding whether a chain of folders still fits.
FILE_ROOM=60


def fingerprint(*pieces):
    return hashlib.sha256('\x00'.join(pieces).encode('utf-8')).hexdigest()[:HASH_LENGTH]


def shortened(name, limit, key=None):
    """At most `limit` characters. Two names cut to the same start still differ by their hash tail."""
    if len(name)<=limit:return name
    return name[:limit-HASH_LENGTH-1].rstrip(' .')+'~'+fingerprint(key if key is not None else name)


def safe_folder(name):
    """A source folder name that the queue can hold: bounded, and never one of the program's own."""
    name=shortened(name.rstrip(' .'),FOLDER_NAME_LIMIT)
    return '_'+name if name in MANAGED or name=='Входящие' or name.startswith('.') else name


def collapsed_folder(parts, key):
    return parts[-1][:COLLAPSED_START].rstrip(' .')+'~'+fingerprint(*key)


def fitted_filename(name, room):
    """A file name that fits the room left after its folder; folders are never given up for it."""
    if len(name)<=room:return name
    path=Path(name)
    stem_room=max(1,min(SHORT_STEM,room-len(path.suffix)-HASH_LENGTH-1))
    return path.stem[:stem_room].rstrip(' .')+'_'+fingerprint(name)+path.suffix


def staging_location(incoming, folders, filename):
    """(folder, file name) in the queue. Matching reads the folder a file lies in directly, so:

    files of one source folder always share one queue folder, files of different source folders
    never do, and the folder a file lies in is never dropped. A chain too long for the path
    becomes one folder named by the start of the innermost name and a hash of the whole chain.
    The file name is shortened only after that.
    """
    parts=[part for part in map(safe_folder,folders) if part]
    folder=incoming.joinpath(*parts)
    # The choice depends on the folders alone, so every file of a folder gets the same one.
    if parts and len(str(folder))+1+FILE_ROOM>STAGED_PATH_LIMIT:
        folder=incoming/collapsed_folder(parts,folders)
    return folder,fitted_filename(filename,STAGED_PATH_LIMIT-len(str(folder))-1)


def staging_folder(incoming, folders, filename):
    return staging_location(incoming,folders,filename)[0]


def prune_empty(folder, stop):
    while stop in folder.parents:
        try:folder.rmdir()
        except OSError:return
        folder=folder.parent


def collect(paths, workspace):
    """Bound traversal to explicit inputs, excluding registries and symlinks."""
    for candidate,_folders in collect_with_folders(paths,workspace):yield candidate


def collect_with_folders(paths, workspace):
    """Every input file with its folders: the added folder and those below it, or a single file's own folder."""
    seen=set()
    for value in paths:
        path=Path(value).expanduser()
        if path.is_symlink():continue
        path=path.resolve()
        if path.is_dir():
            if (path/'База'/'registry.sqlite').exists():
                raise ValueError('Это рабочая папка программы. Откройте её через «Папка данных», чтобы не импортировать архив повторно.')
            candidates=[]
            # Service names mean something only inside the program's own folder.
            reserved=MANAGED if path==workspace.root or workspace.root in path.parents else frozenset()
            for parent,dirs,names in os.walk(path,followlinks=False):
                dirs[:]=[d for d in dirs if not d.startswith('.') and d not in reserved and not (Path(parent)/d).is_symlink() and not (Path(parent)/d/'База'/'registry.sqlite').exists()]
                folders=(path.name,)+Path(parent).relative_to(path).parts
                candidates.extend((Path(parent)/name,folders) for name in sorted(names))
        else:candidates=[(path,(path.parent.name,))]
        for candidate,folders in candidates:
            if candidate.is_symlink() or candidate.name.startswith(('.','~$')):continue
            candidate=candidate.resolve()
            if candidate not in seen:
                seen.add(candidate);yield candidate,folders


def documents_in_folder(path, limit=1000):
    """Count documents an import would move out of a folder that is not yet a workspace."""
    root=Path(path).expanduser()
    if (root/'База'/'registry.sqlite').exists():return 0
    count=0
    for parent,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if not d.startswith('.') and not (Path(parent)/d).is_symlink()]
        for name in names:
            item=Path(parent)/name
            if name.startswith(('.','~$')) or item.is_symlink() or item.parent.name=='Входящие':continue
            try:known=supported(item)
            except OSError:known=True
            count+=known
            if count>=limit:return count
    return count


def stage_files(workspace, paths, progress=lambda name:None, cancel=None):
    result={'added':[], 'skipped':[], 'errors':[]}
    incoming=workspace.root/'Входящие'
    with workspace.lock():
        for source,folders in collect_with_folders(paths,workspace):
            if cancel is not None and cancel.is_set():break
            progress(source.name)
            try:
                if not supported(source):result['skipped'].append(source.name);continue
                # Existing unprocessed files already belong to the queue.
                relative=None
                try:relative=source.relative_to(workspace.root)
                except ValueError:pass
                if relative and not any(part in MANAGED for part in relative.parts[:-1]):
                    result['added'].append(str(source));continue
                folder,name=staging_location(incoming,folders,source.name)
                folder.mkdir(parents=True,exist_ok=True)
                waiting=folder/name
                # Adding the same folder twice must not double the queue.
                if waiting.is_file() and waiting.stat().st_size==source.stat().st_size and sha256(waiting)==sha256(source):
                    workspace.remember_source(sha256(source),source)
                    result['added'].append(str(waiting));continue
                target=available_path(folder,name)
                temporary=incoming/('.intake-'+uuid.uuid4().hex+'.part')
                try:
                    before=sha256(source)
                    with source.open('rb') as src,temporary.open('xb') as dst:
                        shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
                    if sha256(temporary)!=before or sha256(source)!=before:
                        raise OSError('Файл изменился во время копирования. Добавьте его ещё раз.')
                    os.replace(temporary,target)
                    workspace.remember_source(before,source)
                finally:
                    temporary.unlink(missing_ok=True)
                    prune_empty(folder,incoming)
                result['added'].append(str(target))
            except (OSError,ValueError) as exc:result['errors'].append({'file':source.name,'reason':str(exc)})
    return result


def defer_file(workspace, path):
    """Remove from the automatic queue without deleting its retained copy."""
    path=Path(path).resolve()
    with workspace.lock():
        paths,_=workspace.files()
        if path not in paths:raise ValueError('Файл уже отсутствует в очереди. Обновите список.')
        target=available_path(workspace.root/'Отложено',path.name)
        shutil.move(str(path),str(target))
        return target


def retry_file(workspace, arrival_id):
    """Requeue an error under its original DB source key, so success clears it."""
    with workspace.lock(),workspace.connect() as db:
        row=db.execute("SELECT * FROM arrivals WHERE id=? AND category='Ошибки'",(arrival_id,)).fetchone()
        if not row:raise ValueError('Этот файл уже обработан. Обновите список.')
        source=Path(row['source'])
        archived=Path(row['destination']) if row['destination'] else source
        if row['status']=='done':
            if source.exists():
                raise ValueError('Во входной папке уже есть файл с таким именем. Сначала обработайте его.')
            if not archived.is_file():raise ValueError('Файл не найден. Добавьте исходник ещё раз.')
            source.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(archived,source)
        elif not source.is_file():raise ValueError('Файл не найден. Добавьте исходник ещё раз.')
        # An edited replacement belongs to this retry, not a second unresolved
        # error forever. The former bytes remain in the preserved original store.
        digest=sha256(source)
        if digest!=row['hash']:
            existing=db.execute('SELECT id FROM arrivals WHERE source=? AND hash=?',(str(source),digest)).fetchone()
            if existing:raise ValueError('Эта версия уже есть в очереди или архиве. Обработайте очередь.')
            db.execute('UPDATE arrivals SET hash=? WHERE id=?',(digest,row['id']))
        return source
