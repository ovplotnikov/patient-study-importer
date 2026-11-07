"""Transactional registry, incremental imports and recoverable file moves."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import threading
import uuid

import portalocker

from . import analysis, extraction, quality

MANAGED={'Обработано','Нужно уточнить','Дубли','Ошибки','База','Резервные копии','Отложено'}


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
                CREATE TABLE IF NOT EXISTS study_history (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    created TEXT NOT NULL, action TEXT NOT NULL, analysis TEXT NOT NULL,
                    extracted TEXT NOT NULL, proposal TEXT, note TEXT NOT NULL);
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
            dirs[:]=[d for d in dirs if not d.startswith('.') and d not in MANAGED and not (Path(parent)/d).is_symlink() and not (Path(parent)/d/'База'/'registry.sqlite').exists()]
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
        previous=db.execute('''SELECT arrivals.* FROM arrivals JOIN documents ON arrivals.document_id=documents.id
                               WHERE arrivals.source=? AND arrivals.hash=? AND documents.hash=?''',
                            (str(path), digest, digest)).fetchone()
        if previous and previous['document_id']:
            if previous['status']=='done':
                db.execute("UPDATE arrivals SET status='pending',category='Дубли',destination=NULL,error='' WHERE id=?",(previous['id'],))
            else:db.execute("UPDATE arrivals SET error='' WHERE id=?",(previous['id'],))
            return 'Повтор',previous['document_id']
        match=db.execute('SELECT id FROM documents WHERE hash=?',(digest,)).fetchone()
        duplicate=bool(match)
        if match:
            document_id=match['id']
        else:
            original=self.preserve(path,digest)
            extracted=extraction.extract(original)
            parsed=analysis.analyze(extracted,path.name)
            if not any(page.strip() for page in extracted['pages']):raise ValueError('В файле не обнаружен читаемый текст')
            fingerprint=analysis.content_fingerprint(extracted['pages'])
            matches = db.execute('SELECT * FROM documents WHERE fingerprint=? ORDER BY created,id', (fingerprint,)).fetchall() if fingerprint else []
            possible = []
            for other in matches:
                previous_analysis = self._load_analysis(other)
                if previous_analysis.get('duplicate_of'):
                    continue
                # Only complete, plain text can prove equivalence after whitespace
                # normalization. A PDF/DOCX or OCR result can omit visible content.
                previous_extracted = json.loads(other['extracted'])
                if self._same_complete_text(parsed, extracted, previous_analysis, previous_extracted):
                    document_id = other['id']
                    duplicate = True
                    break
                possible.append(other['id'])
            if not duplicate:
                if possible:
                    parsed['quality']['issues'].append({
                        'id': 'possible_duplicate', 'code': 'possible_duplicate',
                        'message': 'Текст совпадает с другим исследованием. Сравните оригиналы.',
                        'severity': 'review', 'related_ids': possible,
                    })
                document_id='S-'+uuid.uuid4().hex[:16]
                db.execute('INSERT INTO documents(id,hash,fingerprint,filename,folder,original,extracted,analysis,created) VALUES (?,?,?,?,?,?,?,?,?)',
                           (document_id,digest,fingerprint,path.name,str(path.parent.relative_to(self.root)),str(original),json.dumps(extracted,ensure_ascii=False),json.dumps(parsed,ensure_ascii=False),now()))
        category='Дубли' if duplicate else 'Обработано'
        db.execute('INSERT INTO arrivals(source,hash,document_id,category,created) VALUES (?,?,?,?,?) ON CONFLICT(source,hash) DO UPDATE SET document_id=excluded.document_id,category=excluded.category,destination=NULL,status=\'pending\',error=\'\'',
                   (str(path),digest,document_id,category,now()))
        self.touch(db)
        return 'Повтор' if duplicate else 'Добавлен',document_id

    @staticmethod
    def _load_analysis(record):
        parsed = json.loads(record['analysis'])
        if 'quality' not in parsed:
            parsed['quality'] = quality.assess(parsed, json.loads(record['extracted']))
        return parsed

    @staticmethod
    def _same_complete_text(left, left_source, right, right_source):
        if any(source.get('format') != 'TXT' or source.get('ocr_pages') or source.get('extraction_issues')
               for source in (left_source, right_source)):
            return False
        if any(quality.needs_review(parsed) or parsed['quality'].get('issues')
               or parsed['quality'].get('confirmed') or parsed['quality'].get('protected') for parsed in (left, right)):
            return False
        def fields(parsed):
            return [tuple(item.get(key) for key in ('label','result','unit','side','date','reference'))
                    for item in parsed['observations']]
        # Identical text alone can have different row/column boundaries.
        return (bool(left['observations'] or left['sections'])
                and all(left[key] == right[key] for key in ('names','births','type','date'))
                and fields(left) == fields(right))

    @staticmethod
    def _category(record, parsed):
        if parsed.get('duplicate_of'):
            return 'Дубли'
        if not record['patient_id'] or quality.needs_review(parsed):
            return 'Нужно уточнить'
        return 'Обработано'

    def _schedule_category(self, db, document_id, category):
        for arrival in db.execute(
                "SELECT * FROM arrivals WHERE document_id=? AND category NOT IN ('Дубли','Ошибки') AND category!=?",
                (document_id, category)).fetchall():
            source = arrival['destination'] if arrival['status'] == 'done' else arrival['source']
            # The previous move may have copied the file before an interruption.
            if arrival['destination'] and not Path(source).exists() and Path(arrival['destination']).is_file():
                source = arrival['destination']
            db.execute("UPDATE arrivals SET source=?,destination=NULL,category=?,status='pending' WHERE id=?",
                       (source, category, arrival['id']))

    def resolve_patients(self,db):
        records=[dict(row) for row in db.execute('SELECT * FROM documents')]
        for record in records:record['analysis']=self._load_analysis(record)
        active = [record for record in records if not record['analysis'].get('duplicate_of')]
        known=[dict(row) for row in db.execute('SELECT * FROM patients')]
        resolutions=analysis.resolve(active,known)
        for record in active:
            if not record['decision']:
                result=resolutions[record['id']];patient_id=None
                if record['analysis'].get('reprocess_identity_changed'):
                    candidates = result['candidates'] + ([result['key']] if result['key'] else [])
                    result = {'key': None, 'candidates': candidates,
                              'reason': 'После повторного чтения сведения о пациенте изменились. Сверьте оригинал.'}
                if result['key']:
                    name,birth=result['key']
                    existing=db.execute('SELECT id FROM patients WHERE name=? AND birth=?',(name,birth)).fetchone()
                    patient_id=existing['id'] if existing else 'P-'+uuid.uuid4().hex[:12]
                    db.execute('INSERT OR IGNORE INTO patients VALUES (?,?,?)',(patient_id,name,birth))
                if patient_id != record['patient_id']:
                    self.touch(db)
                db.execute('UPDATE documents SET patient_id=?,reason=?,candidates=? WHERE id=?',
                           (patient_id,result['reason'],json.dumps(result['candidates'],ensure_ascii=False),record['id']))
                record['patient_id'] = patient_id
            self._schedule_category(db, record['id'], self._category(record, record['analysis']))

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
            paths,skipped=self.files();batch=[];progress(total=len(paths),done=0,phase='Чтение файлов',skipped=len(skipped))
            for index,path in enumerate(paths,1):
                if cancel.is_set():break
                progress(done=index-1,current=path.name)
                try:
                    with db:
                        result,document_id=self.add_file(db,path)
                        self.event(db,'import',json.dumps({'source':str(path),'result':result,'document':document_id},ensure_ascii=False))
                    batch.append({'file':path.name,'source':str(path),'status':result,'document_id':document_id})
                except Exception as exc:
                    try:digest=sha256(path)
                    except OSError:digest='unreadable-'+hashlib.sha256(str(path).encode()).hexdigest()
                    with db:
                        db.execute("INSERT INTO arrivals(source,hash,category,error,created) VALUES (?,?,?,?,?) ON CONFLICT(source,hash) DO UPDATE SET error=excluded.error,status='pending'",(str(path),digest,'Ошибки',str(exc),now()))
                        self.touch(db)
                    batch.append({'file':path.name,'source':str(path),'status':'Ошибка','reason':str(exc)})
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
            result['skipped_files']=len(skipped)
            for item in batch:
                if item['status']=='Добавлен':
                    record=db.execute('SELECT * FROM documents WHERE id=?',(item['document_id'],)).fetchone()
                    if not record['patient_id']:
                        item['status']='Уточнить пациента'
                    elif quality.needs_review(self._load_analysis(record)):
                        item['status']='Проверить данные'
            result['batch']=batch
            result['remaining']=max(0,len(paths)-len(batch))
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
        records = [(row, self._load_analysis(row)) for row in db.execute('SELECT * FROM documents')]
        active = [(row, parsed) for row, parsed in records if not parsed.get('duplicate_of')]
        identity = {row['id'] for row, _ in active if not row['patient_id']}
        uncertain = {row['id'] for row, parsed in active if quality.needs_review(parsed)}
        exported = db.execute("SELECT value FROM meta WHERE key='exported_revision'").fetchone()
        pending_export = bool(records) and (
            not exported or exported[0] != self._revision(db) or not (self.root/'Исследования.xlsx').is_file()
        )
        incomplete = sum(1 for _, parsed in active if parsed['quality'].get('confirmed') and parsed['quality'].get('incomplete'))
        return {
            'pending_export': pending_export,
            'documents': len(active),
            'patients': len({row['patient_id'] for row, _ in active if row['patient_id']}),
            'review': len(identity),
            'quality_review': len(uncertain),
            'total_review': len(identity | uncertain),
            'incomplete': incomplete,
            'duplicates':db.execute("SELECT count(*) FROM arrivals WHERE category='Дубли'").fetchone()[0],
            'errors':db.execute("SELECT count(*) FROM arrivals WHERE category='Ошибки'").fetchone()[0],
            'pending_moves':db.execute("SELECT count(*) FROM arrivals WHERE status='pending'").fetchone()[0],
            'workbook':str(self.root/'Исследования.xlsx'),
        }

    def problems(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM arrivals WHERE category='Ошибки' OR status='pending' ORDER BY id")]

    def review_list(self):
        with self.connect() as db:
            result=[]
            for row in db.execute('SELECT * FROM documents WHERE patient_id IS NULL ORDER BY created'):
                a=self._load_analysis(row)
                if a.get('duplicate_of'):continue
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
            record=db.execute('SELECT * FROM documents WHERE id=?',(document_id,)).fetchone()
            if not record:raise ValueError('Документ не найден')
            if record['patient_id']:raise ValueError('Пациент уже подтверждён. Обновите список документов.')
            with db:
                existing=db.execute('SELECT id FROM patients WHERE name=? AND birth=?',(name,birth)).fetchone()
                patient_id=existing['id'] if existing else 'P-'+uuid.uuid4().hex[:12]
                db.execute('INSERT OR IGNORE INTO patients VALUES (?,?,?)',(patient_id,name,birth))
                db.execute('UPDATE documents SET patient_id=?,decision=?,reason=? WHERE id=?',(patient_id,now(),'Пациент подтверждён пользователем',document_id))
                self._schedule_category(db, document_id, self._category({'patient_id': patient_id}, self._load_analysis(record)))
                self.touch(db);self.event(db,'patient_confirmed',document_id)
            export_workbook(self,db)
            self.finish_moves(db,lambda **kw:None)
            self.backup(db)
        return self.summary()

    @staticmethod
    def _revision(db):
        return db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]

    def _checked_document(self, db, document_id, expected_revision=None):
        if expected_revision is not None and str(expected_revision) != self._revision(db):
            raise ValueError('Данные изменились. Откройте документ для сверки снова.')
        record = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        if record is None:
            raise ValueError('Документ не найден')
        return record

    @staticmethod
    def _with_result_ids(document_id, parsed):
        from .workbook import stable_id
        for index, item in enumerate(parsed['observations']):
            item.setdefault('result_id', stable_id(document_id, item['page'], item['line'], index))
        for index, item in enumerate(parsed['sections']):
            if 'section_id' not in item:
                item['section_id'] = stable_id(document_id, 'section', index, 0)
                item['legacy_section_index'] = index
        return parsed

    def quality_review_list(self):
        with self.connect() as db:
            revision = self._revision(db)
            pending = []
            for row in db.execute('SELECT * FROM documents ORDER BY created,id'):
                parsed = self._load_analysis(row)
                issues = quality.issues(parsed)
                if issues and not parsed.get('duplicate_of'):
                    self._with_result_ids(row['id'], parsed)
                    pending.append({
                        'id': row['id'], 'filename': row['filename'], 'original': row['original'],
                        'patient_id': row['patient_id'], 'revision': revision, 'issues': issues,
                        'type': parsed['type'], 'date': parsed['date'], 'observations': parsed['observations'],
                        'analysis': parsed,
                    })
            return pending

    def _save_study_history(self, db, record, action, note, proposal=None):
        db.execute('INSERT INTO study_history(document_id,created,action,analysis,extracted,proposal,note) VALUES (?,?,?,?,?,?,?)',
                   (record['id'], now(), action, record['analysis'], record['extracted'],
                    json.dumps(proposal, ensure_ascii=False) if proposal is not None else None, note))

    def _preflight_workbook(self):
        # Most export failures happen when Excel has the workbook open. Check
        # before recording a new decision so the user can close it and retry.
        if (self.root/'~$Исследования.xlsx').exists():
            raise RuntimeError('Закройте таблицу «Исследования» в Excel и повторите действие.')

    def _finish_review_change(self, db):
        from .workbook import export_workbook
        try:
            export_workbook(self, db)
        except Exception as exc:
            raise RuntimeError('Решение сохранено в базе. Закройте Excel и нажмите «Обработать», чтобы обновить таблицу. '+str(exc)) from exc
        self.finish_moves(db, lambda **state: None)
        self.backup(db)
        return self.summary(db)

    @staticmethod
    def _review_date(value):
        if not value:
            return None
        for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except (ValueError, TypeError):
                pass
        raise ValueError('Укажите дату в формате ДД.ММ.ГГГГ.')

    def _reviewed_observations(self, parsed, observations, previous_date):
        from .parsing import numeric_value
        previous = {item['result_id']: item for item in parsed['observations']}
        if observations is None:
            observations = parsed['observations']
        checked = []
        seen = set()
        for edit in observations:
            result_id = edit.get('result_id')
            if result_id and (result_id not in previous or result_id in seen):
                raise ValueError('Строка показателя изменилась. Откройте документ снова.')
            if result_id:
                seen.add(result_id)
            item = dict(previous.get(result_id, {}))
            for key in ('label', 'result', 'unit', 'side', 'reference', 'section', 'comment'):
                if key in edit:
                    item[key] = str(edit[key] or '').strip()
            if not item.get('label'):
                raise ValueError('У каждого показателя должно быть название.')
            result = item.get('result', '')
            item['result'] = result
            if item.get('number') is None or result != previous.get(result_id, {}).get('result'):
                item['number'] = numeric_value(result)
            old_row_date = item.get('date')
            item['date'] = self._review_date(edit.get('date', item.get('date', parsed['date'])))
            if item.get('date_source') == 'Дата документа' and item['date'] == previous_date and not edit.get('date_edited'):
                item['date'] = parsed['date']
            elif item['date'] != old_row_date or edit.get('date_edited'):
                item['date_source'] = 'Указано при сверке'
            item.setdefault('date_source', 'Указано при сверке')
            item.setdefault('origin', 'Введено при сверке')
            for key in ('section', 'raw', 'flag', 'side', 'unit', 'reference'):
                item.setdefault(key, '')
            if not result_id:
                page = edit.get('page', 1)
                if not re.fullmatch(r'[1-9]\d*', str(page)) or int(page) > max(parsed.get('page_numbers') or [1]):
                    raise ValueError('Укажите страницу в пределах исходного документа.')
                item['page'] = int(page)
                item['line'] = 0
            if 'comment' in edit and item['comment'] != previous.get(result_id, {}).get('comment'):
                item['review_comment'] = item['comment']
            item.setdefault('line', 0)
            item['result_id'] = result_id or 'R-'+uuid.uuid4().hex[:20]
            item['status'] = 'Проверено'
            checked.append(item)
        return checked

    def _reviewed_sections(self, parsed, edits):
        previous = parsed['sections']
        if edits is None:
            return previous
        if len(edits) < len(previous):
            raise ValueError('Откройте заключения повторно. Не все исходные разделы доступны.')
        checked = []
        for index, edit in enumerate(edits):
            if index < len(previous):
                item = dict(previous[index])
                if edit.get('section_id') and edit['section_id'] != item['section_id']:
                    raise ValueError('Порядок заключений изменился. Откройте документ снова.')
            else:
                page = edit.get('page', 1)
                if not re.fullmatch(r'[1-9]\d*', str(page)) or int(page) > max(parsed.get('page_numbers') or [1]):
                    raise ValueError('Укажите страницу заключения в пределах исходного документа.')
                item = {'section_id': 'T-'+uuid.uuid4().hex[:20], 'page': int(page), 'line': 0}
            revised_text = str(edit.get('text', item.get('text', '')) or '').strip()
            revised_name = str(edit.get('section', item.get('section', 'Заключение')) or '').strip() or 'Заключение'
            if revised_text != str(item.get('text', '')).strip():
                item['reviewed'] = True
            item['text'], item['section'] = revised_text, revised_name
            checked.append(item)
        return checked

    def confirm_quality(self, document_id, *, date=None, study_type=None, observations=None, sections=None,
                        note='', expected_revision=None):
        note = note.strip()
        if not note:
            raise ValueError('Укажите результат сверки с оригиналом.')
        with self.lock(), self.connect() as db:
            record = self._checked_document(db, document_id, expected_revision)
            parsed = self._with_result_ids(document_id, self._load_analysis(record))
            if parsed.get('duplicate_of') or not quality.needs_review(parsed):
                raise ValueError('Сверка уже завершена. Обновите список документов.')
            self._preflight_workbook()
            previous_date = parsed['date']
            if date is not None:
                parsed['date'] = self._review_date(date)
            if study_type is not None:
                parsed['type'] = study_type.strip() or 'Документ'
            parsed['observations'] = self._reviewed_observations(parsed, observations, previous_date)
            parsed['sections'] = self._reviewed_sections(parsed, sections)
            findings = quality.assess(parsed, json.loads(record['extracted']))['issues']
            incomplete = [issue for issue in findings if issue['code'] in {
                'unknown_type', 'missing_date', 'lab_no_results', 'missing_narrative_result',
                'missing_result', 'missing_unit', 'unparsed_row', 'unknown_unit',
                'unmapped_columns', 'OCR_EMPTY', 'OCR_FAILED',
                'PDF_TEXT_UNREADABLE', 'EMBEDDED_IMAGE_UNREADABLE', 'EMBEDDED_IMAGE_UNSUPPORTED',
            }]
            incomplete.extend(issue for issue in findings if (
                (issue['code'] == 'conflicting_date' and date is None)
                or (issue['code'] in {'conflicting_type','type_mismatch'} and study_type is None)
            ))
            if incomplete and note == 'Сверены все страницы и строки оригинала':
                raise ValueError('Укажите, как устранены замечания или почему данные остаются неполными.')
            for issue in incomplete:
                index = issue.get('observation_index')
                if index is not None:
                    parsed['observations'][index]['status'] = 'Сверено · неполные данные'
            parsed['quality'].update(confirmed=True, protected=True, confirmed_at=now(), note=note,
                                     incomplete=bool(incomplete), accepted_missing=incomplete)
            self.backup(db)
            with db:
                self._save_study_history(db, record, 'quality_confirmed', note)
                db.execute('UPDATE documents SET analysis=? WHERE id=?',
                           (json.dumps(parsed, ensure_ascii=False), document_id))
                self._schedule_category(db, document_id, self._category(record, parsed))
                self.touch(db)
                self.event(db, 'quality_confirmed', document_id)
            return self._finish_review_change(db)

    def mark_duplicate(self, document_id, target_id, note, expected_revision=None):
        note = note.strip()
        if not note or document_id == target_id:
            raise ValueError('Укажите другое исследование и основание для подтверждения дубля.')
        with self.lock(), self.connect() as db:
            record = self._checked_document(db, document_id, expected_revision)
            target = self._checked_document(db, target_id)
            parsed = self._load_analysis(record)
            related = {other for issue in parsed['quality']['issues'] if issue['code'] == 'possible_duplicate'
                       for other in issue.get('related_ids', [])}
            if parsed.get('duplicate_of') or target_id not in related or self._load_analysis(target).get('duplicate_of'):
                raise ValueError('Обновите список возможных дублей.')
            if not record['patient_id'] or record['patient_id'] != target['patient_id']:
                raise ValueError('Для обоих исследований сначала подтвердите одного и того же пациента.')
            self._preflight_workbook()
            parsed['duplicate_of'] = target_id
            parsed['quality'].update(confirmed=True, confirmed_at=now(), note=note)
            self.backup(db)
            with db:
                self._save_study_history(db, record, 'duplicate_confirmed', note)
                db.execute('UPDATE documents SET analysis=? WHERE id=?',
                           (json.dumps(parsed, ensure_ascii=False), document_id))
                self._schedule_category(db, document_id, 'Дубли')
                self.touch(db)
                self.event(db, 'duplicate_confirmed', json.dumps({'id': document_id, 'target': target_id}))
            return self._finish_review_change(db)

    @staticmethod
    def _observation_key(item):
        return tuple(item.get(key) or '' for key in ('label', 'date', 'unit', 'side', 'origin', 'section'))

    def _preserve_reprocess_fields(self, document_id, previous, candidate):
        from collections import Counter
        from copy import deepcopy
        self._with_result_ids(document_id, previous)
        old = previous['observations']
        revised = deepcopy(candidate)
        protected = []
        if previous['quality'].get('confirmed') or previous['quality'].get('protected'):
            revised['quality']['protected'] = True
            for key, label in (('type', 'Вид исследования'), ('date', 'Дата исследования')):
                if previous[key] != revised[key]:
                    protected.append(label)
                revised[key] = previous[key]
            fields = ('label', 'result', 'unit', 'side', 'date', 'reference')
            if [[row.get(key) for key in fields] for row in old] != [
                    [row.get(key) for key in fields] for row in revised['observations']]:
                protected.append('Ранее сверенные показатели')
            revised['observations'] = deepcopy(old)
            previous_sections = [(item['section'], item['text']) for item in previous['sections']]
            new_sections = [(item['section'], item['text']) for item in revised['sections']]
            if previous_sections != new_sections:
                protected.append('Ранее сверенные заключения')
            revised['sections'] = deepcopy(previous['sections'])
            # Candidate issue indices refer to the new OCR rows. Once protected
            # rows replace those rows, keep the page and drop the stale index.
            for issue in revised['quality']['issues']:
                issue.pop('observation_index', None)
            if protected:
                revised['quality']['issues'].append({
                    'id': 'reprocess_conflict', 'code': 'reprocess_conflict', 'severity': 'review',
                    'message': 'Новое чтение расходится с ранее сверенными полями. Сохранены прежние значения.',
                })
            elif not revised['quality']['issues'] and previous['quality'].get('confirmed'):
                revised['quality'] = deepcopy(previous['quality'])
        else:
            old_counts = Counter(map(self._observation_key, old))
            new_counts = Counter(map(self._observation_key, revised['observations']))
            old_ids = {self._observation_key(item): item['result_id'] for item in old}
            for item in revised['observations']:
                key = self._observation_key(item)
                item['result_id'] = old_ids[key] if old_counts[key] == new_counts[key] == 1 else 'R-'+uuid.uuid4().hex[:20]
            # Keep notes on unchanged text. Changed sections receive new IDs,
            # and their old notes are retained in the workbook archive.
            def section_key(item):
                return item['section'], item['text']
            old_sections = previous['sections']
            new_sections = revised['sections']
            old_section_counts = Counter(map(section_key, old_sections))
            new_section_counts = Counter(map(section_key, new_sections))
            matching = {section_key(item): item for item in old_sections}
            for item in new_sections:
                key = section_key(item)
                if old_section_counts[key] == new_section_counts[key] == 1:
                    source = matching[key]
                    for field in ('section_id', 'legacy_section_index'):
                        if field in source:
                            item[field] = source[field]
                else:
                    item['section_id'] = 'T-'+uuid.uuid4().hex[:20]
        if not previous['quality'].get('confirmed'):
            # Reprocessing one source does not decide whether two sources are copies.
            for issue in quality.issues(previous):
                if issue['code'] == 'possible_duplicate':
                    revised['quality']['issues'].append(deepcopy(issue))
        return revised, protected

    def prepare_reprocess(self, document_id):
        with self.lock(), self.connect() as db:
            record = dict(self._checked_document(db, document_id))
            revision = self._revision(db)
        previous = self._load_analysis(record)
        if previous.get('duplicate_of'):
            raise ValueError('Повторно обработайте основное исследование.')
        original = Path(record['original'])
        if not original.is_file() or sha256(original) != record['hash']:
            raise ValueError('Сохранённый оригинал отсутствует или изменился.')
        extracted = extraction.extract(original)
        if sha256(original) != record['hash']:
            raise ValueError('Сохранённый оригинал изменился во время чтения.')
        if not any(text.strip() for text in extracted['pages']):
            raise ValueError('При повторном чтении не найден читаемый текст. Прежние данные сохранены.')
        proposed = analysis.analyze(extracted, record['filename'])
        revised, protected = self._preserve_reprocess_fields(document_id, previous, proposed)
        return {
            'document_id': document_id, 'revision': revision, 'hash': record['hash'],
            'analysis': revised, 'extracted': extracted, 'previous_analysis': previous,
            'proposed_analysis': proposed, 'protected_fields': protected,
        }

    def apply_reprocess(self, document_id, candidate):
        if candidate.get('document_id') != document_id or 'revision' not in candidate:
            raise ValueError('Сначала откройте результат повторного распознавания.')
        with self.lock(), self.connect() as db:
            record = self._checked_document(db, document_id, candidate['revision'])
            original = Path(record['original'])
            if record['hash'] != candidate['hash'] or not original.is_file() or sha256(original) != record['hash']:
                raise ValueError('Сохранённый оригинал отсутствует или изменился.')
            previous = self._load_analysis(record)
            if previous.get('duplicate_of'):
                raise ValueError('Повторно обработайте основное исследование.')
            self._preflight_workbook()
            self.backup(db)
            extracted = candidate['extracted']
            proposed = analysis.analyze(extracted, record['filename'])
            parsed, _ = self._preserve_reprocess_fields(document_id, previous, proposed)
            if previous.get('reprocess_identity_changed'):
                parsed['reprocess_identity_changed'] = True
            if not record['decision'] and record['patient_id']:
                owner = db.execute('SELECT name,birth FROM patients WHERE id=?', (record['patient_id'],)).fetchone()
                if parsed['names'] != [owner['name']] or parsed['births'] != [owner['birth']]:
                    parsed['reprocess_identity_changed'] = True
            with db:
                self._save_study_history(db, record, 'reprocessed', '', proposal=proposed)
                db.execute('UPDATE documents SET extracted=?,analysis=?,fingerprint=? WHERE id=?',
                           (json.dumps(extracted, ensure_ascii=False), json.dumps(parsed, ensure_ascii=False),
                            analysis.content_fingerprint(extracted['pages']), document_id))
                self.touch(db)
                self.resolve_patients(db)
                self.event(db, 'reprocessed', document_id)
            return self._finish_review_change(db)

    def document_list(self):
        """Study list for selecting an original to reprocess."""
        with self.connect() as db:
            result = []
            for row in db.execute('''SELECT documents.*, patients.name AS patient_name FROM documents
                                     LEFT JOIN patients ON documents.patient_id=patients.id
                                     ORDER BY documents.created DESC,documents.id'''):
                parsed = self._load_analysis(row)
                if parsed.get('duplicate_of'):
                    continue
                if not row['patient_id']:
                    status = 'Уточнить пациента'
                elif quality.needs_review(parsed):
                    status = 'Проверить данные'
                elif parsed['quality'].get('confirmed'):
                    status = 'Сверено с замечаниями' if parsed['quality'].get('incomplete') else 'Проверено'
                else:
                    status = 'Сохранено'
                result.append({'id': row['id'], 'filename': row['filename'], 'date': parsed['date'],
                               'type': parsed['type'], 'patient_name': row['patient_name'] or '', 'status': status})
            return result
