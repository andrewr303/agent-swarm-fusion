#!/usr/bin/env bash
# fusion/install.sh — macOS / Linux / POSIX installer for Fusion, dependencies,
# and the AndrewCode, Claude Code, and Codex host plugins.
#
# Usage:
#   ./install.command                         # Finder double-clickable macOS installer
#   bash install.sh                           # full install: runtime + verify + all 3 hosts
#   bash install.sh --hosts codex             # install runtime + register Codex only
#   bash install.sh --skip-dependencies       # offline mode: reuse existing tools/CLIs
#   bash install.sh --no-register             # stage runtime + verify only
#   bash install.sh --dry-run                 # show intended actions; make no changes
#   bash install.sh --smoke                   # also run optional live panel smoke test
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for _dir in "/opt/homebrew/bin" "/usr/local/bin" "$HOME/.local/bin" "$HOME/.cargo/bin" "${ANDREWCODE_HOME:-$HOME/.andrewcode}/bin"; do
  if [ -d "$_dir" ]; then
    case ":${PATH:-}:" in
      *":$_dir:"*) ;;
      *) PATH="$_dir${PATH:+:$PATH}" ;;
    esac
  fi
done
export PATH

HOSTS=()
INSTALL_ROOT=""
ANDREWCODE_HOME=""
ANDREWCODE_SOURCE="${ANDREWCODE_SOURCE:-}"
PYTHON_BIN=""
BASH_BIN=""
CODEX_CMD=""
CLAUDE_CMD=""
ANDREWCODE_CMD=""
SCOPE="user"
PROJECT_DIR="$(pwd)"
SKIP_DEPS=0
SKIP_JQ=0
NO_PATH_UPDATE=0
NO_REGISTER=0
DRY_RUN=0
SMOKE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --hosts|-Hosts)
      shift
      IFS=',' read -r -a parts <<< "${1:-}"
      for p in "${parts[@]}"; do
        [ -n "$p" ] && HOSTS+=("$p")
      done
      ;;
    --install-root|-InstallRoot)
      shift; INSTALL_ROOT="${1:-}" ;;
    --andrewcode-home|-AndrewCodeHome)
      shift; ANDREWCODE_HOME="${1:-}" ;;
    --andrewcode-source|-AndrewCodeSource)
      shift; ANDREWCODE_SOURCE="${1:-}" ;;
    --python|-Python)
      shift; PYTHON_BIN="${1:-}" ;;
    --bash|--git-bash|-GitBash|-Bash)
      shift; BASH_BIN="${1:-}" ;;
    --codex-command|-CodexCommand)
      shift; CODEX_CMD="${1:-}" ;;
    --claude-command|-ClaudeCommand)
      shift; CLAUDE_CMD="${1:-}" ;;
    --andrewcode-command|-AndrewCodeCommand)
      shift; ANDREWCODE_CMD="${1:-}" ;;
    --scope|-Scope)
      shift; SCOPE="${1:-user}" ;;
    --project-directory|-ProjectDirectory)
      shift; PROJECT_DIR="${1:-$(pwd)}" ;;
    --skip-dependencies|-SkipDependencies)
      SKIP_DEPS=1 ;;
    --skip-jq|-SkipJq)
      SKIP_JQ=1 ;;
    --no-path-update|-NoPathUpdate)
      NO_PATH_UPDATE=1 ;;
    --no-register|-NoRegister)
      NO_REGISTER=1 ;;
    --dry-run|-DryRun)
      DRY_RUN=1 ;;
    --no-pause|-NoPause|-nopause)
      ;;
    --smoke|-Smoke)
      SMOKE=1 ;;
    -h|--help)
      sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown installer argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "${#HOSTS[@]}" -eq 0 ]; then
  HOSTS=("all")
fi

if [ "$DRY_RUN" -eq 0 ]; then
  chmod +x "$ROOT"/scripts/*.sh "$ROOT"/scripts/providers/*.sh "$ROOT"/tests/*.sh \
    "$ROOT"/install.sh "$ROOT"/*.command 2>/dev/null || true
fi

find_bootstrap_python() {
  if [ -n "$PYTHON_BIN" ]; then
    if [ -x "$PYTHON_BIN" ] || command -v "$PYTHON_BIN" >/dev/null 2>&1; then
      printf '%s\n' "$PYTHON_BIN"
      return 0
    fi
  fi
  local c
  for c in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys, sqlite3; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        command -v "$c"
        return 0
      fi
    fi
  done
  return 1
}

BOOTSTRAP_PY="$(find_bootstrap_python || true)"
if [ -z "$BOOTSTRAP_PY" ] && [ "$DRY_RUN" -eq 0 ] && [ "$SKIP_DEPS" -eq 0 ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing dependency: python"
    brew install python
    BOOTSTRAP_PY="$(find_bootstrap_python || true)"
  fi
fi

if [ -z "$BOOTSTRAP_PY" ]; then
  for c in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1; then
      BOOTSTRAP_PY="$(command -v "$c")"
      break
    fi
  done
fi

if [ -z "$BOOTSTRAP_PY" ]; then
  echo "Python 3.10+ with sqlite3 is required to run the Fusion installer." >&2
  exit 1
fi

ARGS=("$ROOT/scripts/fusion_install.py" "install" "--root" "$ROOT" "--scope" "$SCOPE" "--project-directory" "$PROJECT_DIR")
for h in "${HOSTS[@]}"; do
  ARGS+=("--hosts" "$h")
done
[ -n "$INSTALL_ROOT" ] && ARGS+=("--install-root" "$INSTALL_ROOT")
[ -n "$ANDREWCODE_HOME" ] && ARGS+=("--andrewcode-home" "$ANDREWCODE_HOME")
[ -n "$ANDREWCODE_SOURCE" ] && ARGS+=("--andrewcode-source" "$ANDREWCODE_SOURCE")
[ -n "$PYTHON_BIN" ] && ARGS+=("--python" "$PYTHON_BIN")
[ -n "$BASH_BIN" ] && ARGS+=("--bash" "$BASH_BIN")
[ -n "$CODEX_CMD" ] && ARGS+=("--codex-command" "$CODEX_CMD")
[ -n "$CLAUDE_CMD" ] && ARGS+=("--claude-command" "$CLAUDE_CMD")
[ -n "$ANDREWCODE_CMD" ] && ARGS+=("--andrewcode-command" "$ANDREWCODE_CMD")
[ "$SKIP_DEPS" -eq 1 ] && ARGS+=("--skip-dependencies")
[ "$SKIP_JQ" -eq 1 ] && ARGS+=("--skip-jq")
[ "$NO_PATH_UPDATE" -eq 1 ] && ARGS+=("--no-path-update")
[ "$NO_REGISTER" -eq 1 ] && ARGS+=("--no-register")
[ "$DRY_RUN" -eq 1 ] && ARGS+=("--dry-run")

"$BOOTSTRAP_PY" -B "${ARGS[@]}"
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ] && [ "$SMOKE" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  bash "$ROOT/tests/smoke-providers.sh" || true
fi

exit "$EXIT_CODE"
