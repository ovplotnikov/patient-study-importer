"""Build an offline, single-file Windows application with its OCR dependencies."""
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

root=Path(__file__).resolve().parents[1]
source=Path(r'C:\Program Files\Tesseract-OCR')
target=root/'vendor'/'tesseract'
if not (source/'tesseract.exe').exists():raise SystemExit('Install Tesseract OCR in the build machine first')
shutil.copytree(source,target,dirs_exist_ok=True)
for language in ['rus','eng']:
    url=f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{language}.traineddata'
    with urllib.request.urlopen(url,timeout=120) as response:
        (target/'tessdata'/f'{language}.traineddata').write_bytes(response.read())
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
                '--name','Исследования пациентов','--add-data',str(root/'vendor')+';vendor',
                '--collect-all','pypdfium2','--collect-all','pypdfium2_raw',str(root/'app.py')],cwd=root,check=True)
