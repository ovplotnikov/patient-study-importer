"""PyInstaller onefile layout; called by build_windows.py."""
import importlib.metadata
import json
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, copy_metadata

sys.path.insert(0, SPECPATH)
from windows_ocr_runtime import deduplicate_tesseract

root = Path(SPECPATH).parent
layout_file = Path(sys.argv[1])
rapid_data = importlib.metadata.distribution('rapidocr').locate_file('rapidocr')
datas = [
    (str(root/'vendor'), 'vendor'),
    (str(root/'patient_importer'/'assets'), 'patient_importer/assets'),
    (str(rapid_data/'config.yaml'), 'rapidocr'),
    (str(rapid_data/'default_models.yaml'), 'rapidocr'),
    *copy_metadata('rapidocr'),
]
binaries = [(path, '.') for path in sys.argv[2:]] + collect_dynamic_libs('onnxruntime')
hiddenimports = []
for package in ('tkinterdnd2', 'pypdfium2', 'pypdfium2_raw'):
    package_data, package_binaries, package_imports = collect_all(package)
    datas.extend(package_data)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_imports)

analysis = Analysis([str(root/'app.py')], binaries=binaries, datas=datas, hiddenimports=hiddenimports)
analysis.binaries, layout = deduplicate_tesseract(analysis.binaries)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
          name='Исследования пациентов', console=False, strip=False, upx=False)
layout_file.write_text(json.dumps(layout, indent=2), encoding='utf-8')
