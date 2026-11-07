"""Synthetic application import timings; never opens a user workspace."""
import argparse
from collections import defaultdict
from datetime import date, timedelta
from functools import wraps
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time

sys.path.insert(0, str(Path.cwd()))
from patient_importer.storage import Workspace
from patient_importer.smoke import SAMPLE
from storage import peak_rss_mib

parser=argparse.ArgumentParser()
parser.add_argument('--counts', type=int, nargs='+', default=[10,100,500])
parser.add_argument('--output', type=Path, required=True)
args=parser.parse_args()
records=[]
stage_times=defaultdict(float)
stage_calls=defaultdict(int)
for name in ('add_file','resolve_patients','_export_workbook','finish_moves','backup','summary'):
    original=getattr(Workspace,name)
    def timed(function, label):
        @wraps(function)
        def wrapper(*a,**kw):
            start=time.perf_counter()
            try:return function(*a,**kw)
            finally:
                stage_times[label]+=time.perf_counter()-start
                stage_calls[label]+=1
        return wrapper
    setattr(Workspace,name,timed(original,name))

def sample(index):
    stamp=(date(2020,1,1)+timedelta(days=index)).strftime('%d.%m.%Y')
    return 'Биохимический анализ крови\n'+SAMPLE.replace('02.03.2025',stamp)

def run(workspace,label,n):
    stage_times.clear();stage_calls.clear()
    start=time.perf_counter()
    result=workspace.import_folder()
    elapsed=time.perf_counter()-start
    row={'documents_before':n,'scenario':label,'seconds':elapsed,
         'stage_seconds':dict(stage_times),'stage_calls':dict(stage_calls),
         'documents_after':result['documents'],'patients':result['patients'],
         'duplicates_total':result['duplicates'],'errors':result['errors'],
         'peak_rss_bytes':round(peak_rss_mib()*1024*1024)}
    assert result['errors']==0 and result['patients']==1
    records.append(row);print(json.dumps(row),flush=True)

for count in args.counts:
    with tempfile.TemporaryDirectory(prefix='psi-perf-synthetic-') as root:
        workspace=Workspace(root)
        incoming=workspace.root/'Входящие'
        for i in range(count):(incoming/f'synthetic-{i:05}.txt').write_text(sample(i),encoding='utf-8')
        run(workspace,'fresh_import',0)
        assert records[-1]['documents_after']==count
        (incoming/'one-new-study.txt').write_text(sample(count),encoding='utf-8')
        run(workspace,'append_one',count)
        assert records[-1]['documents_after']==count+1
        for i in range(count):(incoming/f'duplicate-{i:05}.txt').write_text(sample(i),encoding='utf-8')
        run(workspace,'all_duplicates',count+1)
        assert records[-1]['documents_after']==count+1
        assert records[-1]['duplicates_total']==count
        run(workspace,'empty_queue',count+1)

report={'platform':platform.platform(),'python':sys.version,'load_average':getattr(os,'getloadavg',lambda:None)(),
        'synthetic':True,'ocr':False,'records':records}
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
