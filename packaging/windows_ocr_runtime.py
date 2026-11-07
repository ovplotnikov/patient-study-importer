"""Build-time Windows OCR dependency selection and onefile archive audit."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess

# Imports of the win_amd64 onnxruntime 1.23.2 DLL and CPython 3.12 extension.
CRT_NAMES = ('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
TESSERACT_DIR = 'vendor/tesseract/'
RUNTIME_FILES = (*CRT_NAMES, 'tcl86t.dll', 'tk86t.dll',
                 'onnxruntime/capi/onnxruntime.dll',
                 'onnxruntime/capi/onnxruntime_providers_shared.dll')
# Names known to be required by the app, including DLLs loaded at runtime.
PROTECTED_DLLS = {PureWindowsPath(name).name for name in RUNTIME_FILES}


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


def archive_path(name):
    return PureWindowsPath(name).as_posix().lower()


def file_digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def pe_imports(path):
    """Import names, including delayed loads and forwarded exports."""
    import pefile

    with pefile.PE(str(path), fast_load=True) as pe:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY[f'IMAGE_DIRECTORY_ENTRY_{kind}']
                         for kind in ('IMPORT', 'DELAY_IMPORT', 'EXPORT')],
            forwarded_exports_only=True, import_dllnames_only=True,
        )
        names = {entry.dll.decode('ascii').lower()
                 for kind in ('IMPORT', 'DELAY_IMPORT')
                 for entry in getattr(pe, f'DIRECTORY_ENTRY_{kind}', ())}
        for symbol in getattr(getattr(pe, 'DIRECTORY_ENTRY_EXPORT', None), 'symbols', ()):
            if symbol.forwarder:
                module = symbol.forwarder.decode('ascii').rsplit('.', 1)[0].lower()
                names.add(module if module.endswith('.dll') else module + '.dll')
    return names


def deduplicate_tesseract(binaries):
    """Drop proven redundant root copies after PyInstaller dependency analysis.

    Windows loads the bundled Tesseract executable's sibling DLLs before the
    inherited SetDllDirectoryW(_MEIPASS) directory. Other binaries can require
    the root copies, so retain the full linked closure of those consumers.
    """
    entries = {archive_path(name): Path(source) for name, source, _ in binaries}
    pairs, keep, candidates = {}, {}, set()
    for name, source in entries.items():
        if '/' in name or not name.endswith('.dll'):
            continue
        vendored = entries.get(TESSERACT_DIR + name)
        if vendored is None:
            continue
        digest = file_digest(vendored)
        if file_digest(source) != digest:
            continue
        pairs[name] = {'sha256': digest, 'expanded_bytes': vendored.stat().st_size}
        if name in PROTECTED_DLLS or name.startswith(('msvcp', 'vcruntime', 'concrt', 'ucrtbase')):
            keep[name] = 'application runtime'
        elif source.resolve() != vendored.resolve():
            keep[name] = 'independent build input'
        else:
            candidates.add(name)

    if candidates:
        pending = [name for name in entries if not name.startswith(TESSERACT_DIR)
                   and name not in candidates and name.endswith(('.dll', '.pyd', '.exe'))]
        while pending:
            consumer = pending.pop()
            for dependency in sorted(pe_imports(entries[consumer]) & candidates):
                if dependency not in keep:
                    keep[dependency] = f'linked by {consumer}'
                    pending.append(dependency)

    removed = candidates.difference(keep)
    layout = {
        'removed_root_dlls': {name: pairs[name] for name in sorted(removed)},
        'retained_root_dlls': {name: {**pairs[name], 'reason': keep[name]} for name in sorted(keep)},
    }
    return [item for item in binaries if archive_path(item[0]) not in removed], layout


def audit_archive(exe, revision, *, tesseract, crt_files, weights, layout):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(exe))
    entries = {archive_path(name): name for name in archive.toc}
    if len(entries) != len(archive.toc):
        raise SystemExit('Windows archive has case-insensitive path collisions')
    expected = {path.name.lower(): file_digest(path) for path in crt_files}
    for path in tesseract.iterdir():
        if path.is_file() and (path.suffix.lower() == '.dll' or path.name.lower() == 'tesseract.exe'):
            expected[TESSERACT_DIR + path.name.lower()] = file_digest(path)
    for path in (tesseract/'tessdata').glob('*.traineddata'):
        expected[TESSERACT_DIR + 'tessdata/' + path.name.lower()] = file_digest(path)
    for name, digest in weights.items():
        prefix = TESSERACT_DIR + 'tessdata/' if name.endswith('.traineddata') else 'vendor/rapidocr/'
        expected[prefix + name.lower()] = digest
    for name, pair in layout['retained_root_dlls'].items():
        expected[name] = pair['sha256']
    for name, pair in layout['removed_root_dlls'].items():
        if name in entries or expected.get(TESSERACT_DIR + name) != pair['sha256']:
            raise SystemExit(f'Invalid deduplicated Tesseract pair in EXE: {name}')

    required = (*RUNTIME_FILES, TESSERACT_DIR + 'tesseract.exe',
                TESSERACT_DIR + 'tessdata/rus.traineddata', TESSERACT_DIR + 'tessdata/eng.traineddata')
    required = sorted(set(required).union(expected))
    missing = [name for name in required if name not in entries]
    if missing:
        raise SystemExit(f'Missing local OCR runtime in EXE: {", ".join(missing)}')
    hashes = {name: hashlib.sha256(archive.extract(entries[name])).hexdigest() for name in required}
    for name, digest in expected.items():
        if hashes[name] != digest:
            raise SystemExit(f'Unexpected OCR dependency checksum in EXE: {name}')
    for name in hashes:
        if name.startswith(TESSERACT_DIR) and name.endswith('.dll'):
            root_name = PureWindowsPath(name).name
            if root_name in entries and root_name not in layout['retained_root_dlls']:
                if hashlib.sha256(archive.extract(entries[root_name])).hexdigest() == hashes[name]:
                    raise SystemExit(f'Unaccounted duplicate Tesseract DLL in EXE: {root_name}')
    return {
        'msvc_redist_revision': revision,
        'packaged_sha256': {PureWindowsPath(name).name: hashes[name] for name in RUNTIME_FILES},
        'tesseract_layout': {
            **layout,
            'vendor_sha256': {name: digest for name, digest in hashes.items() if name.startswith(TESSERACT_DIR)},
            'expanded_bytes_removed': sum(pair['expanded_bytes'] for pair in layout['removed_root_dlls'].values()),
            'exe_bytes': Path(exe).stat().st_size,
            'expanded_bytes': sum(entry[2] for entry in archive.toc.values() if entry[4] in ('b', 'x')),
        },
    }
