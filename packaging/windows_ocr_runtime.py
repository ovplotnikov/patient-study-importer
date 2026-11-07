"""Collect app-local Visual C++ runtime from the build machine's REDIST kit."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess

# Imports of the win_amd64 onnxruntime 1.23.2 DLL and CPython 3.12 extension.
CRT_NAMES = ('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')


def find_crt():
    installer = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))
    vswhere = installer/'Microsoft Visual Studio'/'Installer'/'vswhere.exe'
    installations = json.loads(subprocess.check_output(
        [str(vswhere), '-products', '*', '-format', 'json', '-utf8'], encoding='utf-8'
    ))
    candidates = []
    for item in installations:
        redist = Path(item['installationPath'])/'VC'/'Redist'/'MSVC'
        for directory in redist.glob('*/x64/Microsoft.VC*.CRT'):
            revision = directory.parent.parent.name
            if not all(part.isdigit() for part in revision.split('.')):
                continue
            files = [directory/name for name in CRT_NAMES]
            if all(path.is_file() for path in files):
                candidates.append((tuple(int(part) for part in revision.split('.')), revision, files))
    if not candidates:
        raise SystemExit('Visual Studio x64 C++ redistributable files are required to build standalone OCR')
    _, revision, files = max(candidates, key=lambda item: item[0])
    return revision, files


def audit_archive(exe, revision):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(exe))
    entries = {PureWindowsPath(name).name.lower(): name for name in archive.toc}
    required = (*CRT_NAMES, 'onnxruntime.dll', 'onnxruntime_providers_shared.dll')
    missing = [name for name in required if name not in entries]
    if missing:
        raise SystemExit(f'Missing local OCR runtime in EXE: {", ".join(missing)}')
    return {'msvc_redist_revision': revision,
            'packaged_sha256': {name: hashlib.sha256(archive.extract(entries[name])).hexdigest()
                                for name in required}}
