#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v
python3 - <<'PY'
from pathlib import Path
for folder in ('ado_board_sync', 'tests', 'scripts'):
    for path in Path(folder).glob('*.py'):
        compile(path.read_text(encoding='utf-8'), str(path), 'exec')
PY
