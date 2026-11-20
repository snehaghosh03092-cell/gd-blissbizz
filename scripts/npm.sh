#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export npm_config_cache="${npm_config_cache:-$PROJECT_ROOT/.tools/npm-cache}"
if [[ -x "$PROJECT_ROOT/.tools/node/bin/node" ]]; then
  export PATH="$PROJECT_ROOT/.tools/node/bin:$PATH"
fi
exec npm "$@"
