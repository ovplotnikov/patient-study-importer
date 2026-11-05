"""Transactional registry, incremental imports and recoverable file moves."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import uuid

import portalocker

from . import analysis, extraction

MANAGED={'Обработано','Нужно уточнить','Дубли','Ошибки','База','Резервные копии'}


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Workspace:
    def __init__(self, root):
        self.root=Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        for name in MANAGED|{'Входящие'}:(self.root/name).mkdir(exist_ok=True)
        self.base=self.root/'База'
        self.originals=self.base/'Оригиналы';self.originals.mkdir(exist_ok=True)
        self.database=self.base/'registry.sqlite'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS patients (id TEXT PRIMARY KEY,name TEXT NOT NULL,birth TEXT NOT NULL,UNIQUE(name,birth));
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,hash TEXT UNIQUE NOT NULL,fingerprint TEXT,filename TEXT NOT NULL,
                    folder TEXT NOT NULL,original TEXT NOT NULL,extracted TEXT NOT NULL,analysis TEXT NOT NULL,
                    patient_id TEXT REFERENCES patients(id),decision TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
                    candidates TEXT NOT NULL DEFAULT '[]',created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS content_index ON documents(fingerprint);
                CREATE TABLE IF NOT EXISTS arrivals (
                    id INTEGER PRIMARY KEY,source TEXT NOT NULL,hash TEXT NOT NULL,document_id TEXT REFERENCES documents(id),
                    category TEXT NOT NULL,destination TEXT,status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL,UNIQUE(source,hash));
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,time TEXT NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('workspace',str(uuid.uuid4())))
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('revision','0'))
        with self.lock(),self.connect() as db:
            previous=db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            if previous and previous[0]!=str(self.root):
                old=previous[0].replace('\\','/').rstrip('/')+'/'
                def relocate(value):
                    if not value:return value
                    normalized=value.replace('\\','/')
                    if normalized.startswith(old):return str(self.root.joinpath(*normalized[len(old):].split('/')))
                    return value
                for row in db.execute('SELECT id,original FROM documents').fetchall():
                    db.execute('UPDATE documents SET original=? WHERE id=?',(relocate(row['original']),row['id']))
                for row in db.execute('SELECT id,source,destination FROM arrivals').fetchall():
                    db.execute('UPDATE arrivals SET source=?,destination=? WHERE id=?',(relocate(row['source']),relocate(row['destination']),row['id']))
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('root',str(self.root)))

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.database,timeout=20)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def lock(self):
        try:
            with portalocker.Lock(str(self.base/'работа.lock'),timeout=0):yield
        except portalocker.exceptions.LockException as exc:
            raise RuntimeError('Эта папка уже обрабатывается другой копией программы') from exc

    def event(self,db,kind,detail):
        db.execute('INSERT INTO events(time,kind,detail) VALUES (?,?,?)',(now(),kind,detail))

    def touch(self,db):
        db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def files(self):
        paths=[];skipped=[]
        for parent,dirs,files in os.walk(self.root,followlinks=False):
            dirs[:]=[d for d in dirs if not d.startswith('.') and not (Path(parent)==self.root and d in MANAGED) and not (Path(parent)/d).is_symlink()]
            for name in files:
                path=Path(parent)/name
                if name.startswith(('.','~$')) or path.is_symlink():continue
                if name=='Исследования.xlsx' and Path(parent)==self.root:continue
                try:
                    (paths if extraction.supported(path) else skipped).append(path)
                except OSError:
                    paths.append(path)
        return sorted(paths),skipped

    def preserve(self,path,digest):
        with path.open('rb') as source:pdf=source.read(5)==b'%PDF-'
        extension='.pdf' if pdf else path.suffix.lower()
        target=self.originals/(digest+extension)
        if target.exists():
            if sha256(target)!=digest:raise RuntimeError('Нарушена целостность сохранённого оригинала')
            return target
        temporary=target.with_suffix(target.suffix+'.part')
        # This hash-addressed partial belongs to an interrupted import under our lock.
        temporary.unlink(missing_ok=True)
        try:
            with path.open('rb') as source,temporary.open('xb') as destination:
                shutil.copyfileobj(source,destination)
                destination.flush();os.fsync(destination.fileno())
            if sha256(temporary)!=digest:raise RuntimeError('Файл изменился во время чтения. Повторите обработку')
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def add_file(self,db,path):
        digest=sha256(path)
        previous=db.execute('SELECT * FROM arrivals WHERE source=? AND hash=?',(str(path),digest)).fetchone()
        if previous and previous['document_id']:
            if previous['status']=='done':
                db.execute("UPDATE arrivals SET status='pending',category='Дубли',destination=NULL,error='' WHERE id=?",(previous['id'],))
            else:db.execute("UPDATE arrivals SET error='' WHERE id=?",(previous['id'],))
            return 'Повтор',previous['document_id']
        match=db.execute('SELECT id FROM documents WHERE hash=?',(digest,)).fetchone()
        duplicate=bool(match)
        if not match:
            match=db.execute('SELECT document_id AS id FROM arrivals WHERE hash=? AND document_id IS NOT NULL LIMIT 1',(digest,)).fetchone()
            duplicate=bool(match)
        if match:
            document_id=match['id']
        else:
            original=self.preserve(path,digest)
            extracted=extraction.extract(original)
            parsed=analysis.analyze(extracted,path.name)
            if not any(page.strip() for page in extracted['pages']):raise ValueError('В файле не обнаружен читаемый текст')
            fingerprint=analysis.content_fingerprint(extracted['pages'])
            match=db.execute('SELECT id FROM documents WHERE fingerprint=?',(fingerprint,)).fetchone() if fingerprint else None
            if match:
                document_id=match['id'];duplicate=True
            else:
                document_id='S-'+uuid.uuid4().hex[:16]
                db.execute('INSERT INTO documents(id,hash,fingerprint,filename,folder,original,extracted,analysis,created) VALUES (?,?,?,?,?,?,?,?,?)',
                           (document_id,digest,fingerprint,path.name,str(path.parent.relative_to(self.root)),str(original),json.dumps(extracted,ensure_ascii=False),json.dumps(parsed,ensure_ascii=False),now()))
        category='Дубли' if duplicate else 'Обработано'
        db.execute('INSERT INTO arrivals(source,hash,document_id,category,created) VALUES (?,?,?,?,?) ON CONFLICT(source,hash) DO UPDATE SET document_id=excluded.document_id,category=excluded.category,destination=NULL,status=\'pending\',error=\'\'',
                   (str(path),digest,document_id,category,now()))
        self.touch(db)
        return 'Повтор' if duplicate else 'Добавлен',document_id

    def resolve_patients(self,db):
        records=[dict(row) for row in db.execute('SELECT * FROM documents')]
        for record in records:record['analysis']=json.loads(record['analysis'])
        known=[dict(row) for row in db.execute('SELECT * FROM patients')]
        resolutions=analysis.resolve(records,known)
        for record in records:
            if record['decision']:continue
            result=resolutions[record['id']];patient_id=None
            if result['key']:
                name,birth=result['key']
                existing=db.execute('SELECT id FROM patients WHERE name=? AND birth=?',(name,birth)).fetchone()
                patient_id=existing['id'] if existing else 'P-'+uuid.uuid4().hex[:12]
                db.execute('INSERT OR IGNORE INTO patients VALUES (?,?,?)',(patient_id,name,birth))
            db.execute('UPDATE documents SET patient_id=?,reason=?,candidates=? WHERE id=?',
                       (patient_id,result['reason'],json.dumps(result['candidates'],ensure_ascii=False),record['id']))
        db.execute("UPDATE arrivals SET category=CASE WHEN (SELECT patient_id FROM documents WHERE id=document_id) IS NULL THEN 'Нужно уточнить' ELSE 'Обработано' END WHERE document_id IS NOT NULL AND category!='Дубли' AND status='pending'")

    def destination(self,arrival):
        relative=Path(arrival['source']).relative_to(self.root)
        parent=relative.parent
        if parent.parts and parent.parts[0] in MANAGED|{'Входящие'}:parent=Path(*parent.parts[1:])
        # Preserve patient subfolders, while bounding long names on Windows.
        parts=[part[:65] for part in parent.parts]
        filename=Path(arrival['source']).name
        target=self.root/arrival['category']/Path(*parts)/filename
        if len(str(target))>230:
            target=self.root/arrival['category']/(Path(filename).stem[:70]+'_'+arrival['hash'][:12]+Path(filename).suffix)
        if target.exists() and sha256(target)!=arrival['hash']:
            target=target.with_name(target.stem+'_'+arrival['hash'][:12]+target.suffix)
        return target

    def finish_moves(self,db,progress):
        for row in db.execute("SELECT * FROM arrivals WHERE status='pending' ORDER BY id").fetchall():
            source=Path(row['source']);target=Path(row['destination']) if row['destination'] else self.destination(row)
            with db:db.execute('UPDATE arrivals SET destination=? WHERE id=?',(str(target),row['id']))
            try:
                target.parent.mkdir(parents=True,exist_ok=True)
                if source.exists() and sha256(source)!=row['hash']:
                    raise RuntimeError('Исходный файл изменён. Новую версию обработаем при следующем запуске')
                if not target.exists():
                    if not source.exists():raise RuntimeError('Исходный файл не найден')
                    temporary=target.with_name('.'+target.name+'.part')
                    # A stale temporary file belongs to this recorded move and can be rebuilt.
                    with source.open('rb') as src,temporary.open('wb') as dst:
                        shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
                    if sha256(temporary)!=row['hash']:raise RuntimeError('Не удалось проверить копию файла')
                    os.replace(temporary,target)
                if sha256(target)!=row['hash']:raise RuntimeError('В архиве уже есть другой файл с таким именем')
                if source.exists() and source!=target:source.unlink()
                with db:
                    db.execute("UPDATE arrivals SET status='done',error='' WHERE id=?",(row['id'],))
                    self.event(db,'moved',json.dumps({'arrival':row['id'],'destination':str(target)},ensure_ascii=False))
            except OSError as exc:
                with db:db.execute('UPDATE arrivals SET error=? WHERE id=?',('Не удалось переместить файл. Возможно, он открыт: '+str(exc),row['id']))
            except RuntimeError as exc:
                with db:db.execute('UPDATE arrivals SET error=? WHERE id=?',(str(exc),row['id']))

    def import_folder(self,progress=lambda **state:None,cancel=None):
        from .workbook import export_workbook
        cancel=cancel or threading.Event()
        with self.lock(),self.connect() as db:
            self.backup(db)
            paths,skipped=self.files();progress(total=len(paths),done=0,phase='Чтение файлов',skipped=len(skipped))
            for index,path in enumerate(paths,1):
                if cancel.is_set():break
                progress(done=index-1,current=path.name)
                try:
                    with db:
                        result,document_id=self.add_file(db,path)
                        self.event(db,'import',json.dumps({'source':str(path),'result':result,'document':document_id},ensure_ascii=False))
                except Exception as exc:
                    try:digest=sha256(path)
                    except OSError:digest='unreadable-'+hashlib.sha256(str(path).encode()).hexdigest()
                    with db:
                        db.execute("INSERT INTO arrivals(source,hash,category,error,created) VALUES (?,?,?,?,?) ON CONFLICT(source,hash) DO UPDATE SET error=excluded.error,status='pending'",(str(path),digest,'Ошибки',str(exc),now()))
                        self.touch(db)
                progress(done=index)
            progress(phase='Сопоставление пациентов',current='')
            with db:self.resolve_patients(db)
            progress(phase='Сохранение таблицы')
            export_workbook(self,db)
            progress(phase='Перенос файлов')
            self.finish_moves(db,progress)
            self.backup(db)
            # Path metadata in the workbook describes the durable planned destinations.
            result=self.summary(db)
            progress(phase='Остановлено' if cancel.is_set() else 'Готово',summary=result)
            return result

    def backup(self,db):
        if not db.execute('SELECT 1 FROM documents LIMIT 1').fetchone():return
        folder=self.root/'Резервные копии'
        target=folder/('База_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.sqlite')
        temporary=target.with_suffix('.part')
        try:
            snapshot=sqlite3.connect(temporary)
            try:db.backup(snapshot)
            finally:snapshot.close()
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
        for old in sorted(folder.glob('База_*.sqlite'))[:-20]:old.unlink()

    def summary(self,db=None):
        if db is None:
            with self.connect() as connection:return self.summary(connection)
        return {
            'documents':db.execute('SELECT count(*) FROM documents').fetchone()[0],
            'patients':db.execute('SELECT count(DISTINCT patient_id) FROM documents WHERE patient_id IS NOT NULL').fetchone()[0],
            'review':db.execute('SELECT count(*) FROM documents WHERE patient_id IS NULL').fetchone()[0],
            'duplicates':db.execute("SELECT count(*) FROM arrivals WHERE category='Дубли'").fetchone()[0],
            'errors':db.execute("SELECT count(*) FROM arrivals WHERE category='Ошибки'").fetchone()[0],
            'pending_moves':db.execute("SELECT count(*) FROM arrivals WHERE status='pending'").fetchone()[0],
            'workbook':str(self.root/'Исследования.xlsx'),
        }

    def review_list(self):
        with self.connect() as db:
            result=[]
            for row in db.execute('SELECT * FROM documents WHERE patient_id IS NULL ORDER BY created'):
                a=json.loads(row['analysis'])
                result.append({'id':row['id'],'filename':row['filename'],'reason':row['reason'],'names':a['names'],'births':a['births'],'candidates':json.loads(row['candidates'])})
            return result

    def document(self,document_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM documents WHERE id=?',(document_id,)).fetchone()
            if not row:raise ValueError('Документ не найден')
            return dict(row)

    def assign(self,document_id,name,birth):
        from .parsing import FULL_NAME,normalized
        from .workbook import export_workbook
        import re
        name=normalized(name)
        if not re.fullmatch(FULL_NAME,name):raise ValueError('Укажите фамилию, имя и отчество полностью')
        datetime.strptime(birth,'%Y-%m-%d')
        with self.lock(),self.connect() as db:
            self.backup(db)
            if not db.execute('SELECT 1 FROM documents WHERE id=?',(document_id,)).fetchone():raise ValueError('Документ не найден')
            with db:
                existing=db.execute('SELECT id FROM patients WHERE name=? AND birth=?',(name,birth)).fetchone()
                patient_id=existing['id'] if existing else 'P-'+uuid.uuid4().hex[:12]
                db.execute('INSERT OR IGNORE INTO patients VALUES (?,?,?)',(patient_id,name,birth))
                db.execute('UPDATE documents SET patient_id=?,decision=?,reason=? WHERE id=?',(patient_id,now(),'Пациент подтверждён пользователем',document_id))
                # Schedule a second move from the review folder to the processed folder.
                for arrival in db.execute("SELECT * FROM arrivals WHERE document_id=? AND category='Нужно уточнить'",(document_id,)).fetchall():
                    source=arrival['destination'] if arrival['status']=='done' else arrival['source']
                    db.execute("UPDATE arrivals SET source=?,destination=NULL,category='Обработано',status='pending' WHERE id=?",(source,arrival['id']))
                self.touch(db);self.event(db,'patient_confirmed',document_id)
            export_workbook(self,db)
            self.finish_moves(db,lambda **kw:None)
            self.backup(db)
        return self.summary()
