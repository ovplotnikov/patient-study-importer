"""Build an offline, single-file Windows application with its OCR dependencies."""
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import importlib.metadata
import hashlib
import json

root=Path(__file__).resolve().parents[1]
source=Path(r'C:\Program Files\Tesseract-OCR')
target=root/'vendor'/'tesseract'
if not (source/'tesseract.exe').exists():raise SystemExit('Install Tesseract OCR in the build machine first')
shutil.copytree(source,target,dirs_exist_ok=True)
for extra in target.glob('*.exe'):
    if extra.name.lower()!='tesseract.exe':extra.unlink()
for language in ['rus','eng']:
    url=f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{language}.traineddata'
    with urllib.request.urlopen(url,timeout=120) as response:
        (target/'tessdata'/f'{language}.traineddata').write_bytes(response.read())
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
                '--name','Исследования пациентов','--add-data',str(root/'vendor')+';vendor',
                '--add-data',str(root/'patient_importer'/'assets')+';patient_importer/assets',
                '--collect-all','tkinterdnd2','--collect-all','pypdfium2','--collect-all','pypdfium2_raw',str(root/'app.py')],cwd=root,check=True)
(root/'dist'/'Инструкция.txt').write_text('Откройте docs/Краткая инструкция.pdf или docs/guide.html.\nВ программе кнопка «Помощь» всегда открывает инструкцию текущей версии.\n',encoding='utf-8-sig')
docs_target=root/'dist'/'docs';docs_target.mkdir(exist_ok=True)
for name in ['Краткая инструкция.pdf','guide.html']:
    shutil.copy2(root/'build'/'guide'/name,docs_target/name)
for name in ['USER_GUIDE.md','CODEX_INSTALL.md','SUPPORTED_FORMS.md','PRIVACY.md']:
    shutil.copy2(root/'docs'/name,docs_target/name)
license_root=root/'dist'/'Лицензии';license_root.mkdir(exist_ok=True)
shutil.copy2(root/'LICENSE',license_root/'PatientStudyImporter-MIT.txt')
shutil.copy2(root/'patient_importer'/'assets'/'icons'/'LICENSE.txt',license_root/'Bootstrap-Icons-MIT.txt')
packages=['pypdf','openpyxl','python-docx','Pillow','pypdfium2','portalocker','lxml','et-xmlfile','typing_extensions','tkinterdnd2']
versions={}
for package in packages:
    try:distribution=importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError:continue
    versions[package]=distribution.version
    for file in distribution.files or []:
        if any(token in file.name.lower() for token in ['license','copying','notice']):
            location=distribution.locate_file(file)
            if location.is_file():
                destination=license_root/package/file.name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(location,destination)
for file in [Path(sys.base_prefix)/'LICENSE.txt',Path(sys.base_prefix)/'tcl'/'tcl8.6'/'license.terms',Path(sys.base_prefix)/'tcl'/'tk8.6'/'license.terms']:
    if file.exists():shutil.copy2(file,license_root/(file.parent.name+'_'+file.name))
with urllib.request.urlopen('https://raw.githubusercontent.com/tesseract-ocr/tesseract/main/LICENSE',timeout=60) as response:
    (license_root/'Tesseract-and-traineddata-Apache-2.0.txt').write_bytes(response.read())
(license_root/'Источники.txt').write_text('Tesseract: https://github.com/tesseract-ocr/tesseract\nWindows distribution: https://github.com/UB-Mannheim/tesseract\nLanguage data: https://github.com/tesseract-ocr/tessdata_fast\nPDFium: https://pdfium.googlesource.com/pdfium/\nPython: https://www.python.org/\n',encoding='utf-8')
(root/'dist'/'build-info.json').write_text(json.dumps({'python':sys.version,'dependencies':versions,'traineddata_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (target/'tessdata').glob('*.traineddata')}},indent=2),encoding='utf-8')
