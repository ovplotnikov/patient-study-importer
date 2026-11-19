"""Write the diagnostics contract into google_apps_script/Code.gs: python packaging/wire_schema.py"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from patient_importer.telemetry_schema import wire_schema  # noqa: E402

START = '// Closed wire schema. Python parity is checked in tests/test_apps_script.py.\nconst WIRE_SCHEMA = '
END = ';\n'


def block():
    return START + json.dumps(wire_schema(), ensure_ascii=False, indent=2, sort_keys=True) + END


def main():
    path = ROOT / 'google_apps_script' / 'Code.gs'
    code = path.read_text(encoding='utf8')
    begin = code.index(START)
    finish = code.index(END, begin + len(START)) + len(END)
    path.write_text(code[:begin] + block() + code[finish:], encoding='utf8')


if __name__ == '__main__':
    main()
