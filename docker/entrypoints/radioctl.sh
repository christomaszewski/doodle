#!/usr/bin/env bash
# Client for the running supervisor, which owns every change to the radio.
#   radioctl.sh status          status JSON on stdout
#   radioctl.sh set <value>     standby|low|medium|high|max|off|<dBm>|startup|unchanged
set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/supervisor.py" "$@"
