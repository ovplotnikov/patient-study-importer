import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'windows_ocr_runtime', Path(__file__).resolve().parents[1]/'packaging'/'windows_ocr_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class WindowsBundleLayoutTests(unittest.TestCase):
    def test_removes_only_duplicates_outside_application_dependency_closure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binaries = []
            for name in ('first.dll', 'transitive.dll', 'ocr-a.dll', 'ocr-b.dll', 'msvcp140.dll',
                         'different.dll', 'independent.dll'):
                vendor = root/'vendor'/name
                vendor.parent.mkdir(exist_ok=True)
                vendor.write_bytes(name.encode())
                source = vendor
                if name in {'different.dll', 'independent.dll'}:
                    source = root/name
                    source.write_bytes(b'different bytes' if name == 'different.dll' else vendor.read_bytes())
                binaries.extend([(runtime.TESSERACT_DIR + name, str(vendor), 'BINARY'),
                                 (name, str(source), 'BINARY')])
            app_extension = root/'module.pyd'
            app_extension.touch()
            binaries.append(('module.pyd', str(app_extension), 'EXTENSION'))
            imports = {
                'module.pyd': {'first.dll'},
                'first.dll': {'transitive.dll'},
                'transitive.dll': {'first.dll'},
                'ocr-a.dll': {'ocr-b.dll'},
                'ocr-b.dll': {'ocr-a.dll'},
            }
            with patch.object(runtime, 'pe_imports', side_effect=lambda path: imports.get(path.name, set())):
                filtered, report = runtime.deduplicate_tesseract(binaries)

        self.assertEqual(set(report['removed_root_dlls']), {'ocr-a.dll', 'ocr-b.dll'})
        self.assertEqual(set(report['retained_root_dlls']),
                         {'first.dll', 'transitive.dll', 'msvcp140.dll', 'independent.dll'})
        self.assertEqual({name for name, _, _ in filtered},
                         {name for name, _, _ in binaries} - {'ocr-a.dll', 'ocr-b.dll'})
