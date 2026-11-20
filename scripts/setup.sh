#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_EXECUTABLE="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_EXECUTABLE" ]]; then
  for candidate in python3.13 python3.12 python3.11 /opt/homebrew/opt/python@3.13/bin/python3.13 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; assert (3, 11) <= sys.version_info < (3, 14)' 2>/dev/null; then
      PYTHON_EXECUTABLE="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON_EXECUTABLE" ]]; then
  echo 'Install Python 3.11–3.13, or set PYTHON_BIN to its executable.' >&2
  exit 1
fi
"$PYTHON_EXECUTABLE" -c 'import sys; assert (3, 11) <= sys.version_info < (3, 14), "Use Python 3.11–3.13"'
./scripts/npm.sh --version

"$PYTHON_EXECUTABLE" - <<'PY'
from pathlib import Path
from urllib.parse import quote
import os
import secrets

root_env = Path('.env')
if not root_env.exists():
    content = Path('.env.example').read_text()
    content = content.replace('replace-with-local-password', secrets.token_hex(24))
    content = content.replace('replace-with-another-local-password', secrets.token_hex(24))
    root_env.write_text(content)
    root_env.chmod(0o600)
backend_env = Path('backend/.env')
if not backend_env.exists():
    values = dict(line.split('=', 1) for line in root_env.read_text().splitlines() if line and not line.startswith('#'))
    values.update({key: os.environ[key] for key in values if key in os.environ})
    url = 'mysql+pymysql://{}:REDACTED-EXAMPLE-CREDENTIAL@127.0.0.1:{}/{}?charset=utf8mb4'.format(
        quote(values['MYSQL_USER'], safe=''), quote(values['MYSQL_PASSWORD'], safe=''),
        values['MYSQL_PORT'], quote(values['MYSQL_DATABASE'], safe=''))
    backend_env.write_text('DATABASE_URL=' + url + '\n')
    backend_env.chmod(0o600)
PY

if [[ ! -d backend/.venv ]]; then
  "$PYTHON_EXECUTABLE" -m venv backend/.venv
fi
backend/.venv/bin/python3 -m pip install -r backend/requirements.lock
backend/.venv/bin/python3 -m pip install --no-deps -e './backend[dev]'
backend/.venv/bin/python3 -m pip install --no-deps \
  -e ./libs/blissbiz_common \
  -e ./services/gateway -e ./services/identity -e ./services/store \
  -e ./services/catalog -e ./services/supply -e ./services/sales \
  -e ./services/billing -e ./services/agent
./scripts/npm.sh --prefix frontend ci
echo 'Setup complete. Run make db-up and make db-migrate, then make dev-all and make dev-frontend in separate terminals.'
