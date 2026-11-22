#!/usr/bin/env bash
# Install Fusion runtime/dependencies and register the all host selection.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/install.sh" --hosts all "$@"
