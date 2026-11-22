#!/usr/bin/env bash
# AndrewCode worker compatibility adapter. Prompts are UTF-8 files, not argv text.
# Usage: andrewcode.sh <prompt_file> <out_file> [model] [effort] [workspace]
# The old snapshot-only write guarantee cannot be enforced by the current CLI;
# refuse before launch. FUSION_ANDREWCODE_AUTO does not bypass this safety gate.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$DIR/_common.sh"
prompt_file="${1:?usage: andrewcode.sh <prompt_file> <out_file> [model] [effort] [workspace]}"
out_file="${2:?missing out_file}"
model="${3:-${FUSION_ANDREWCODE_MODEL:-meta/muse-spark-1.3-contributor}}"
effort="${4:-${FUSION_ANDREWCODE_EFFORT:-xhigh}}"
fusion_export_identity
: > "${out_file}.raw"
"${FUSION_PYTHON:-python}" -B "$(fusion_winpath "$DIR/../../python/fusion_swarm/andrewcode_cli.py")" \
  "$(fusion_winpath "$prompt_file")" "$(fusion_winpath "$out_file")" \
  "$model" "$effort" --sandbox snapshot-only
rc=$?
[ -s "${out_file}.raw" ] && fusion_json_out andrewcode "${out_file}.raw" "$out_file" "$model" --meta-only
exit "$rc"
