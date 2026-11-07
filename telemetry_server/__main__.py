"""Start the loopback diagnostics receiver."""
import argparse
import os
from pathlib import Path
import sqlite3

from .receiver import CounterServer, CounterStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--allow-version', action='append', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        with CounterServer(args.port, CounterStore(args.database), args.allow_version) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(1, 'Не удалось запустить приёмник диагностики. Проверьте конфигурацию, каталог и порт.\n')


if __name__ == '__main__':
    raise SystemExit(main())
