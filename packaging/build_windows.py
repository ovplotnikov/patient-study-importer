"""Build an offline, single-file Windows application with its OCR dependencies."""
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import importlib.metadata
import hashlib
import json
from ocr_models import prepare_models
from windows_ocr_runtime import find_crt, audit_archive

root=Path(__file__).resolve().parents[1]
source=Path(r'C:\Program Files\Tesseract-OCR')
target=root/'vendor'/'tesseract'
if not (source/'tesseract.exe').exists():raise SystemExit('Install Tesseract OCR in the build machine first')
shutil.copytree(source,target,dirs_exist_ok=True)
for extra in target.glob('*.exe'):
    if extra.name.lower()!='tesseract.exe':extra.unlink()
ocr_weights = prepare_models(root/'vendor')
rapid_data = importlib.metadata.distribution('rapidocr').locate_file('rapidocr')
crt_revision, crt_files = find_crt()
crt_args = [arg for path in crt_files for arg in ('--add-binary', str(path)+';.')]
exe = root/'dist'/'Исследования пациентов.exe'
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--noupx','--onefile','--windowed',
                *crt_args,
                '--name','Исследования пациентов','--add-data',str(root/'vendor')+';vendor',
                '--add-data',str(root/'patient_importer'/'assets')+';patient_importer/assets',
                '--add-data',str(rapid_data/'config.yaml')+';rapidocr',
                '--add-data',str(rapid_data/'default_models.yaml')+';rapidocr',
                '--collect-binaries','onnxruntime','--copy-metadata','rapidocr',
                '--collect-all','tkinterdnd2','--collect-all','pypdfium2','--collect-all','pypdfium2_raw',str(root/'app.py')],cwd=root,check=True)
ocr_runtime = audit_archive(exe, crt_revision)
(root/'dist'/'Инструкция.txt').write_text('Откройте docs/Краткая инструкция.pdf или docs/guide.html.\nВ программе кнопка «Помощь» всегда открывает инструкцию текущей версии.\n',encoding='utf-8-sig')
docs_target=root/'dist'/'docs';docs_target.mkdir(exist_ok=True)
for name in ['Краткая инструкция.pdf','guide.html']:
    shutil.copy2(root/'build'/'guide'/name,docs_target/name)
for name in ['USER_GUIDE.md','CODEX_INSTALL.md','SUPPORTED_FORMS.md','PRIVACY.md']:
    shutil.copy2(root/'docs'/name,docs_target/name)
license_root=root/'dist'/'Лицензии';license_root.mkdir(exist_ok=True)
shutil.copy2(root/'LICENSE',license_root/'PatientStudyImporter-MIT.txt')
shutil.copy2(root/'patient_importer'/'assets'/'icons'/'LICENSE.txt',license_root/'Bootstrap-Icons-MIT.txt')
packages=['pypdf','openpyxl','python-docx','Pillow','pypdfium2','portalocker','lxml','et-xmlfile','typing_extensions','tkinterdnd2','rapidocr','onnxruntime','opencv-python-headless',
          'numpy','shapely','pyclipper','omegaconf','PyYAML','requests','colorlog','tqdm',
          'six','certifi','charset-normalizer','idna','urllib3','antlr4-python3-runtime',
          'coloredlogs','humanfriendly','flatbuffers','packaging','protobuf','sympy','mpmath']
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
with urllib.request.urlopen('https://raw.githubusercontent.com/tesseract-ocr/tesseract/5.5.3/LICENSE',timeout=60) as response:
    (license_root/'Tesseract-and-traineddata-Apache-2.0.txt').write_bytes(response.read())
with urllib.request.urlopen('https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/v3.7.0/LICENSE',timeout=60) as response:
    (license_root/'PaddleOCR-Apache-2.0.txt').write_bytes(response.read())
(license_root/'Microsoft-VC-Runtime.txt').write_text(
    'Microsoft Visual C++ x64 Runtime, application-local binaries from Visual Studio REDIST.\n'
    'https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution\n'
    'https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files\n',
    encoding='utf-8')
(license_root/'Источники.txt').write_text('Tesseract: https://github.com/tesseract-ocr/tesseract\nWindows distribution: https://github.com/UB-Mannheim/tesseract\nLanguage data: https://github.com/tesseract-ocr/tessdata_fast\nPaddleOCR: https://github.com/PaddlePaddle/PaddleOCR\nRapidOCR: https://github.com/RapidAI/RapidOCR\nONNX models: https://www.modelscope.cn/models/RapidAI/RapidOCR\nPDFium: https://pdfium.googlesource.com/pdfium/\nPython: https://www.python.org/\n',encoding='utf-8')
(root/'dist'/'build-info.json').write_text(json.dumps({'python':sys.version,'dependencies':versions,'ocr_windows_runtime':ocr_runtime,'ocr_weights_sha256':ocr_weights,'tesseract':subprocess.check_output([str(target/'tesseract.exe'),'--version'],text=True).splitlines()[0],'traineddata_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (target/'tessdata').glob('*.traineddata')}},indent=2),encoding='utf-8')
