#!/usr/bin/env bash
# Compatibility seat "codex": AndrewCode OAuth aliases, never codex.exe.
# Usage: codex.sh <prompt_file> <out_file> [model] [effort] [sandbox] [workspace]
# Runs a tools-disabled, ephemeral AndrewCode consult turn (--disable-tools
# --ephemeral) from an empty temp dir; the prompt must be self-contained. The
# sandbox argument cannot widen that: workspace-write is noted and ignored,
# danger-full-access is refused. Fails closed if the installed AndrewCode lacks
# the flags. A snapshot is context, not a filesystem sandbox.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_common.sh"
prompt_file="${1:?usage: codex.sh <prompt_file> <out_file> [model] [effort] [sandbox] [workspace]}"
out_file="${2:?missing out_file}"
model="${3:-${FUSION_CODEX_MODEL:-gpt-6.1-sol}}"
effort="${4:-${FUSION_CODEX_EFFORT:-xhigh}}"
sandbox="${5:-${FUSION_CODEX_SANDBOX:-workspace-write}}"
fusion_export_identity
# Keep the public positional sandbox/workspace contract; the workspace is not mounted.
echo "mode: isolated-tools-disabled" > "${out_file}.log" 2>/dev/null || true
"${FUSION_PYTHON:-python}" -B "$(fusion_winpath "$DIR/../../python/fusion_swarm/andrewcode_cli.py")" \
  "$(fusion_winpath "$prompt_file")" "$(fusion_winpath "$out_file")" \
  "$model" "$effort" --sandbox "$sandbox" --isolated
rc=$?
[ -s "${out_file}.raw" ] && fusion_json_out andrewcode "${out_file}.raw" "$out_file" "$model" --meta-only
exit "$rc"
