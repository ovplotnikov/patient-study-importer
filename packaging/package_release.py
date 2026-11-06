"""Package only distributable files, retaining Russian names inside the ZIP."""
import hashlib
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(sys.argv[1] if len(sys.argv) > 1 else 'dist')
files = [root / name for name in (
    'Исследования пациентов.exe', 'Инструкция.txt', 'SHA256.txt', 'build-info.json'
)]
licenses = root / 'Лицензии'
if not licenses.is_dir() or any(not path.is_file() for path in files):
    raise SystemExit('Missing release files or license directory')
files += sorted(path for path in licenses.rglob('*') if path.is_file())
archive = root / 'PatientStudyImporter-Windows.zip'
with ZipFile(archive, 'w', ZIP_DEFLATED) as output:
    for path in files:
        output.write(path, path.relative_to(root))
with ZipFile(archive) as output:
    if output.testzip() is not None:
        raise SystemExit('Release archive failed verification')
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(root / 'PatientStudyImporter-Windows.sha256').write_text(
    f'{digest}  {archive.name}\n', encoding='ascii'
)
print(f'Packaged {len(files)} files: {archive.name}')
