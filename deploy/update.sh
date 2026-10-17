#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo "Install Python 3 on the Docker host first." >&2; exit 1; }
exec python3 "$DEPLOY_DIR/update.py" "$@"
