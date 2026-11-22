#!/usr/bin/env bash
# fusion/scripts/providers/grok.sh — panelist adapter for Grok Build (xAI)
#
# Usage: grok.sh <prompt_file> <out_file> [model] [effort]
# Verified against grok 1.0.92 help (--output-format plain|json|...; json used only when advertised):
#   grok --prompt-file <file> --output-format plain|json --effort <effort>
#        --always-approve --cwd <dir>
#   (--prompt-file is injection-safe: the prompt never touches the shell argv)
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
. "$DIR/_common.sh"

prompt_file="${1:?usage: grok.sh <prompt_file> <out_file> [model] [effort]}"
out_file="${2:?missing out_file}"
model="${3:-${FUSION_GROK_MODEL:-grok-4.5}}" # default grok-4.5 (set FUSION_GROK_MODEL="" for the CLI default)
# NOTE: the default grok-build model rejects reasoningEffort (verified live, HTTP 400).
# Effort is therefore OPT-IN — only sent when explicitly requested AND the model supports it.
effort="${4:-${FUSION_GROK_EFFORT:-}}"
timeout_s="${FUSION_GROK_TIMEOUT:-600}"

command -v grok >/dev/null 2>&1 || { echo "[fusion] grok not installed" >&2; exit 127; }
fusion_export_identity

# Feature-detect structured output (D3): json only when --help advertises it.
help_text="$(grok --help 2>&1 || true)"
out_fmt="plain"
fusion_help_has "$help_text" '--output-format' 'json' && out_fmt="json"

scratch="$(fusion_mk_scratch grok)"
trap 'fusion_rm_scratch "$scratch"' EXIT
: > "$out_file"

# MetaLoop expert worker: snapshot the repo into <scratch>/work so grok can READ
# the real codebase (proposal mode — edits land only in this disposable copy).
# Blind panel mode passes no repo → empty work dir. grok already takes the prompt
# via --prompt-file, so it is injection- and E2BIG-safe by construction.
work="$scratch/work"; mkdir -p "$work"
repo="$(fusion_worker_repo "${5:-}")"
if [ -n "$repo" ]; then
  if ! fusion_snapshot_repo "$repo" "$work" >/dev/null 2>&1; then
    [ -f "$work/.fusion-snapshot.txt" ] && cp "$work/.fusion-snapshot.txt" "${out_file}.snapshot" 2>/dev/null || true
    echo "[fusion] grok: repo snapshot failed or copied 0 files from non-empty repo ($repo)" >&2
    exit 1
  fi
  [ -f "$work/.fusion-snapshot.txt" ] && cp "$work/.fusion-snapshot.txt" "${out_file}.snapshot" 2>/dev/null || true
fi

win_work="$(fusion_winpath "$work")"
win_prompt="$(fusion_winpath "$prompt_file")"
log="$scratch/stream.log"

opt_args=()
[ -n "$model" ] && opt_args+=(-m "$model")
[ -n "$effort" ] && opt_args+=(--effort "$effort")

raw="$scratch/raw.out"
_run() {
  grok \
    --prompt-file "$win_prompt" \
    --output-format "$out_fmt" \
    --always-approve \
    --cwd "$win_work" \
    ${opt_args[@]+"${opt_args[@]}"} > "$raw" 2> "$log"
}

fusion_timeout "$timeout_s" _run
rc=$?
cp "$log" "${out_file}.log" 2>/dev/null || true  # preserve diagnostics past scratch cleanup
if [ -s "$raw" ]; then
  if [ "$out_fmt" = "json" ]; then
    fusion_json_out grok "$raw" "$out_file" "$model"
  else
    cp "$raw" "$out_file" 2>/dev/null || true
    fusion_write_meta "$out_file" "$model"
  fi
fi

if [ "$rc" -eq 124 ]; then
  echo "[fusion] grok timed out after ${timeout_s}s" >&2
  exit 124
fi
if [ "$rc" -ne 0 ] || [ ! -s "$out_file" ]; then
  fusion_fail "grok" "$rc" "$log"
  exit 1
fi
fusion_clean_output "$out_file"
exit 0
