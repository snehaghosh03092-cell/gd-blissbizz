#!/bin/sh
set -eu
project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# Never use the legacy project's name or its persistent volumes here.
exec docker compose --project-name blissbiz-next \
  --env-file "$project_root/deploy/release.env" \
  -f "$project_root/deploy/compose.release.yaml" "$@"
