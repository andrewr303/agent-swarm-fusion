#!/usr/bin/env python3
"""Fake claude/andrewcode/codex/opencode for peers-core tests (stdlib only).

Install as `bin/<name>` (copy, mode 755). The behaviour depends on basename(argv[0]):
claude | andrewcode | codex | opencode. Env: FAKE_PEER_MODE (ok|hang|error), FAKE_PEER_LOG (append
one JSON line per call), FAKE_PEER_PIDFILE (hang mode writes the grandchild pid), FAKE_PEER_HELP
(override --help text).
"""
import json
import os
import subprocess
import sys
import time

NAME = os.path.basename(sys.argv[0]).replace(".py", "")
ARGV = sys.argv[1:]
MODE = os.environ.get("FAKE_PEER_MODE", "ok")

HELP = {
    "claude": "Usage: claude [options]\n  -p, --print\n  --output-format <f>\n  --json-schema <s>\n"
              "  --no-session-persistence\n  --max-budget-usd <n>\n  --permission-mode <m>\n  --model <m>\n",
    "andrewcode": "Usage: andrewcode\n  -m, --model\n  --thinking-effort <l>\n  --prompt-file <f>\n"
                  "  --disable-tools\n  --ephemeral\n  --output-format <f>\n  --auto\n",
    "codex": "Usage: codex\n  --no-daemon\n  exec\n  --json\n  --skip-git-repo-check\n  --ephemeral\n"
             "  --color <c>\n  --output-schema <f>\n  --sandbox <s>\n",
    "opencode": "Usage: opencode run\n  --format <f>\n  --dir <d>\n  --model <m>\n",
}


def log(extra=None):
    path = os.environ.get("FAKE_PEER_LOG")
    if not path:
        return
    rec = {"name": NAME, "argv": ARGV, "cwd": os.getcwd(),
           "env": {k: os.environ.get(k) for k in ("FUSION_AGENT_ID", "BOARD_PROFILE", "FUSION_PEER_DEPTH",
                                                  "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}}
    rec.update(extra or {})
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def opt(flag):
    return ARGV[ARGV.index(flag) + 1] if flag in ARGV else None


if "--help" in ARGV or "-h" in ARGV:
    log({"help": True})
    sys.stdout.write(os.environ.get("FAKE_PEER_HELP") or HELP.get(NAME, ""))
    sys.exit(0)

stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
if NAME == "andrewcode" and opt("--prompt-file"):
    stdin_text = open(opt("--prompt-file"), encoding="utf-8").read()
log({"stdin": stdin_text})

if MODE == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pidfile = os.environ.get("FAKE_PEER_PIDFILE")
    if pidfile:
        with open(pidfile, "w") as fh:
            fh.write(str(child.pid))
    time.sleep(60)
    sys.exit(0)

if MODE == "error":
    if NAME == "claude":
        print(json.dumps({"type": "result", "is_error": True, "subtype": "error_during_execution", "result": ""}))
    elif NAME == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": "t-err"}))
        print(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}))
    elif NAME == "andrewcode":
        print(json.dumps({"role": "error", "content": "SECRET-PROVIDER-PAYLOAD"}))
    sys.exit(1 if NAME != "claude" else 0)

if NAME == "claude":
    obj = {"type": "result", "subtype": "success", "is_error": False, "result": "claude says hi",
           "session_id": "sess-claude-1", "total_cost_usd": 0.0123,
           "usage": {"input_tokens": 10, "output_tokens": 5},
           "model_usage": {"claude-fake-1": {"inputTokens": 10}}}
    if "--json-schema" in ARGV:
        obj["structured_output"] = {"verdict": "ok"}
    print(json.dumps(obj))
elif NAME == "andrewcode":
    print(json.dumps({"role": "meta", "type": "system.version", "session_id": "sess-ac-1"}))
    print(json.dumps({"role": "assistant", "tool_calls": [{"id": "1", "name": "Read"}]}))
    print(json.dumps({"role": "tool", "content": "file text"}))
    print(json.dumps({"role": "assistant", "content": '{"verdict": "ok"}' if "JSON schema" in stdin_text
                      else "andrewcode says hi"}))
elif NAME == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "thr-codex-1"}))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex says hi"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}))
    last = opt("-o")
    if last:
        open(last, "w", encoding="utf-8").write("codex says hi")
elif NAME == "opencode":
    print(json.dumps({"type": "step_start", "sessionID": "ses-oc-1"}))
    print(json.dumps({"type": "text", "sessionID": "ses-oc-1", "part": {"messageID": "m1", "text": "old "}}))
    print(json.dumps({"type": "text", "sessionID": "ses-oc-1", "part": {"messageID": "m2", "text": "opencode "}}))
    print(json.dumps({"type": "text", "sessionID": "ses-oc-1", "part": {"messageID": "m2", "text": "says hi"}}))
sys.exit(0)
