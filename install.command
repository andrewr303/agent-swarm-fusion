#!/usr/bin/env bash
# Fusion macOS double-clickable installer. Use --no-pause or -NoPause for unattended runs.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NO_PAUSE=0
for arg in "$@"; do
  case "$arg" in
    --no-pause|-NoPause|-nopause) NO_PAUSE=1 ;;
  esac
done
bash "$SCRIPT_DIR/install.sh" "$@"
EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  printf 'Fusion installation failed. See the diagnostics and installation report above.\n'
fi
if [ "$NO_PAUSE" -eq 0 ] && [ -t 0 ]; then
  read -r -p "Press Return to close..." _ || true
fi
exit "$EXIT_CODE"
