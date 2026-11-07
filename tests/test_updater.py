import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
import zipfile
import hashlib

from patient_importer import updater as u


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage = self.root / 'stage'; self.stage.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, extra=None, data_format=u.DATA_FORMAT):
        raw = b'MZ synthetic executable'
        manifest = {'format': 1, 'version': '2.2.0', 'data_format': data_format,
                    'exe_sha256': hashlib.sha256(raw).hexdigest()}
        path = self.root / 'update.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr(u.EXE, raw)
            z.writestr('update.json', json.dumps(manifest))
            if extra:
                z.writestr(*extra)
        return path

    def test_versions_do_not_downgrade_or_accept_prerelease(self):
        self.assertGreater(u.version('v2.10.0'), u.version('2.9.9'))
        for bad in ['2.1', '2.1.0-beta', '../../2.1.0', '02.1.0']:
            with self.assertRaises(u.UpdateError):u.version(bad)
        for tag in ['v2.1.0', 'v2.0.0']:
            with patch.object(u, 'read_small', return_value=json.dumps({'tag_name': tag}).encode()):
                self.assertIsNone(u.check_for_update('2.1.0'))

    def test_offline_check_raises_clear_error(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=OSError('offline')):
            with self.assertRaises(u.UpdateError):u.check_for_update()

    def test_offer_waits_for_processing_and_decline_keeps_app_idle(self):
        from patient_importer.gui import Application, messagebox
        release=u.Release('v2.2.0','','',1)
        app=SimpleNamespace(closing=False,pending_update=release,updating=False,
                            worker=Mock(),root=Mock(),offer_update=Mock())
        app.worker.is_alive.return_value=True
        with patch.object(messagebox,'askyesno',return_value=False) as question:
            Application.offer_update(app)
            question.assert_not_called()
            self.assertIs(app.pending_update,release)
            app.worker.is_alive.return_value=False
            Application.offer_update(app)
            question.assert_called_once()
        self.assertIsNone(app.pending_update)
        self.assertFalse(app.updating)

    def test_redirects_only_use_https_github_hosts(self):
        for url in ['http://github.com/a', 'https://github.com.evil.test/a', 'file:///tmp/a', 'https://user:pass@github.com/a']:
            self.assertFalse(u.trusted_url(url))
        self.assertTrue(u.trusted_url('https://release-assets.githubusercontent.com/a'))

    def test_archive_rejects_paths_and_unknown_payloads(self):
        for name in ['../outside.exe', '/absolute.exe', 'docs/../../bad', 'docs\\bad', 'Данные/registry.sqlite']:
            with self.assertRaises(u.UpdateError):u.validate_archive(self.archive((name, 'bad')), self.stage, 'v2.2.0')
        self.assertFalse((self.root / 'outside.exe').exists())

    def test_archive_rejects_symlinks_and_duplicates(self):
        link = zipfile.ZipInfo('docs/link');link.external_attr = (0o120777 << 16)
        with self.assertRaises(u.UpdateError):u.validate_archive(self.archive((link, '../bad')), self.stage, 'v2.2.0')
        with self.assertRaises(u.UpdateError):u.validate_archive(self.archive(('UPDATE.JSON', '{}')), self.stage, 'v2.2.0')

    def test_incompatible_data_format_is_not_installed(self):
        with self.assertRaises(u.UpdateError):u.validate_archive(self.archive(data_format='future-schema'), self.stage, 'v2.2.0')
        self.assertFalse((self.stage / 'candidate.exe').exists())

    def test_checked_download_and_cancel_do_not_touch_installation(self):
        raw = self.archive().read_bytes();checksum = hashlib.sha256(raw).hexdigest()
        release = u.Release('v2.2.0', 'archive', 'checksum', len(raw), 'sha256:' + checksum)
        with patch.object(u, 'read_small', return_value=f'{checksum}  {u.ARCHIVE}\n'.encode()), patch.object(u, 'open_url', return_value=io.BytesIO(raw)):
            stage = u.prepare_update(release, threading.Event(), root=self.root / 'cache')
        self.assertTrue((stage / 'candidate.exe').exists())
        cancel = threading.Event();cancel.set()
        with patch.object(u, 'read_small', return_value=f'{checksum}  {u.ARCHIVE}\n'.encode()), patch.object(u, 'open_url', return_value=io.BytesIO(raw)):
            with self.assertRaises(u.Cancelled):u.prepare_update(release, cancel, root=self.root / 'cancel')
        self.assertEqual(list((self.root / 'cancel').iterdir()), [])

    def test_corrupt_download_is_rejected_and_cleaned(self):
        raw = self.archive().read_bytes();checksum = '0' * 64
        release = u.Release('v2.2.0', 'archive', 'checksum', len(raw))
        with patch.object(u, 'read_small', return_value=f'{checksum}  {u.ARCHIVE}\n'.encode()), patch.object(u, 'open_url', return_value=io.BytesIO(raw)):
            with self.assertRaises(u.UpdateError):u.prepare_update(release, threading.Event(), root=self.root / 'cache')
        self.assertEqual(list((self.root / 'cache').iterdir()), [])

    def plan(self):
        target = self.root / 'installed' / u.EXE;target.parent.mkdir()
        target.write_bytes(b'MZ old program')
        (self.stage / 'candidate.exe').write_bytes(b'MZ new program')
        data = target.parent / 'Данные';data.mkdir()
        (data / 'registry.sqlite').write_bytes(b'synthetic database sentinel')
        (data / 'notes.xlsx').write_bytes(b'synthetic workbook sentinel')
        settings = target.parent / 'settings.json';settings.write_text('synthetic settings sentinel')
        plan = {'format': 1, 'target': str(target), 'old_sha256': u.digest(target),
                'new_sha256': u.digest(self.stage / 'candidate.exe'), 'version': '2.2.0',
                'archive_sha256': '0' * 64, 'test': True}
        p = self.stage / 'plan.json';p.write_text(json.dumps(plan))
        return p, target

    def probe(self, *args, **kwargs):
        (self.stage / 'probe.json').write_text(json.dumps({'version': '2.2.0'}))
        return type('Result', (), {'returncode': 0})()

    def test_install_changes_only_program_and_receipt_preserves_data(self):
        plan, target = self.plan()
        before = {p.relative_to(target.parent):p.read_bytes() for p in target.parent.rglob('*') if p.is_file() and p != target}
        with patch.object(u.subprocess, 'run', side_effect=self.probe):
            backup = u.apply_update(plan, launcher=lambda command: None)
        self.assertEqual(target.read_bytes(), b'MZ new program')
        self.assertEqual(backup.read_bytes(), b'MZ old program')
        for p, data in before.items():self.assertEqual((target.parent / p).read_bytes(), data)
        self.assertEqual(json.loads((target.parent / 'installation.json').read_text())['version'], 'v2.2.0')

    def test_restart_failure_rolls_back_executable_and_receipt(self):
        plan, target = self.plan()
        receipt = target.parent / 'installation.json';receipt.write_text('previous receipt')
        def fail(command):raise OSError('launch failed')
        with patch.object(u.subprocess, 'run', side_effect=self.probe):
            with self.assertRaises(OSError):u.apply_update(plan, launcher=fail)
        self.assertEqual(target.read_bytes(), b'MZ old program')
        self.assertEqual(receipt.read_text(), 'previous receipt')
        self.assertEqual((target.parent / 'Данные' / 'registry.sqlite').read_bytes(), b'synthetic database sentinel')

    def test_changed_installed_exe_is_not_overwritten(self):
        plan, target = self.plan();target.write_bytes(b'MZ changed elsewhere')
        with patch.object(u.subprocess, 'run', side_effect=self.probe):
            with self.assertRaises(u.UpdateError):u.apply_update(plan, launcher=lambda command: None)
        self.assertEqual(target.read_bytes(), b'MZ changed elsewhere')


if __name__ == '__main__':unittest.main()
