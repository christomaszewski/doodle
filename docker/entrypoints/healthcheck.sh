#!/usr/bin/env bash
# Healthy = radio reachable and at its target, and diagnostics publishing. Read-only.
set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/supervisor.py" health
