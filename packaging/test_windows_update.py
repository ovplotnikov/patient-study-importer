"""Actual frozen helper: a locked EXE, restart, and preservation of synthetic data."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patient_importer import updater as u, __version__

root = Path(__file__).resolve().parents[1]
exe = root / 'dist' / u.EXE
subprocess.run([sys.executable, str(root / 'packaging/package_release.py'), str(root / 'dist')], check=True)
with tempfile.TemporaryDirectory(prefix='patient-update-ci-') as tmp:
    folder = Path(tmp)
    install = folder / 'Программа с пробелами';install.mkdir()
    target = install / u.EXE
    # Test the actual updater already installed by users, not another copy of this build.
    legacy_url='https://github.com/ovplotnikov/patient-study-importer/releases/download/v2.1.0/'
    legacy=folder/'legacy.zip'
    with urllib.request.urlopen(legacy_url+u.ARCHIVE,timeout=120) as response,legacy.open('wb') as output:
        shutil.copyfileobj(response,output)
    with urllib.request.urlopen(legacy_url+'PatientStudyImporter-Windows.sha256',timeout=30) as response:
        expected=response.read().decode().split()[0]
    assert u.digest(legacy)==expected,'Legacy release checksum mismatch'
    with zipfile.ZipFile(legacy) as bundle:target.write_bytes(bundle.read(u.EXE))
    data = install / 'Данные';data.mkdir()
    (data / 'registry.sqlite').write_bytes(b'synthetic database, do not change')
    (data / 'notes.xlsx').write_bytes(b'synthetic workbook, do not change')
    settings = folder / 'settings.json';settings.write_text('synthetic user settings')
    before = {p:u.digest(p) for p in [data/'registry.sqlite', data/'notes.xlsx', settings]}
    stage = folder / 'stage';stage.mkdir()
    manifest = u.validate_archive(root/'dist'/u.ARCHIVE, stage, 'v'+__version__)
    manifest['archive_sha256'] = u.digest(root/'dist'/u.ARCHIVE)
    (stage/'manifest.json').write_text(json.dumps(manifest))
    lock_dir = folder / 'lock';lock_dir.mkdir()
    old = subprocess.Popen([str(target), '--update-lock-probe', str(lock_dir)], env=u.detached_environment())
    helper = None
    try:
        deadline = time.monotonic()+60
        while not (lock_dir/'ready').exists():
            assert old.poll() is None and time.monotonic()<deadline, 'Old program did not start'
            time.sleep(.2)
        with patch.object(sys, 'executable', str(target)):
            helper = u.launch_install(stage, target, test=True)
        deadline = time.monotonic()+90
        while not (stage/'probe.json').exists():
            assert helper.poll() is None and time.monotonic()<deadline, 'Candidate probe failed'
            time.sleep(.2)
        time.sleep(2)
        assert not (stage/'result.json').exists(), 'Installer failed to wait for the running EXE'
        assert target.exists()
        (lock_dir/'exit').write_text('exit');old.wait(timeout=30)
        helper.wait(timeout=150)
        result = json.loads((stage/'result.json').read_text())
        assert result['ok'], result
        deadline = time.monotonic()+60
        while not (stage/'restart-probe.json').exists():
            assert time.monotonic()<deadline, 'Updated program did not restart'
            time.sleep(.2)
        assert json.loads((stage/'restart-probe.json').read_text())['version']==__version__
        assert u.digest(target)==u.digest(exe)
        assert target.with_name(target.stem+'.previous.exe').is_file()
        assert all(u.digest(p)==value for p,value in before.items()), 'User data changed'
        assert json.loads((install/'installation.json').read_text())['version']=='v'+__version__
        # Allow the restarted one-file parent to release its files before TemporaryDirectory cleanup.
        time.sleep(3)
    finally:
        (lock_dir/'exit').write_text('exit')
        if old.poll() is None:old.wait(timeout=100)
        if helper is not None and helper.poll() is None:helper.wait(timeout=180)
(root/'dist'/'update-test.json').write_text(json.dumps({'ok': True, 'checks': [
    'locked_exe_wait', 'candidate_health_probe', 'frozen_helper', 'atomic_replacement',
    'previous_exe_backup', 'frozen_restart', 'workspace_and_settings_unchanged']}), encoding='utf8')
(root/'dist'/u.ARCHIVE).unlink()
(root/'dist'/'PatientStudyImporter-Windows.sha256').unlink()
print('Frozen Windows update test passed')
