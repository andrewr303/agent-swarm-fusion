#!/usr/bin/env python3
"""Fake ACP v1 agent for peers-acp tests (stdlib only, newline-delimited JSON-RPC over stdio).

Env:
  FAKE_ACP_MODE  comma-separated flags:
      v2            answer initialize with protocolVersion 2 regardless of the request
      perm_read     send session/request_permission (tool kind "read") during the prompt
      perm_edit     send session/request_permission (tool kind "edit") during the prompt
      fs            send fs/read_text_file + terminal/create requests; report the error codes in the text
      cancel        stream one chunk, then wait for session/cancel and answer stopReason "cancelled"
      hang          never answer session/prompt
      spawn_child   spawn a grandchild (sleep 60) at start and write its pid to FAKE_ACP_PIDFILE
      exit_early    print a fake secret to stderr and exit right after reading initialize
      badline       print a non-JSON line before the initialize response
  FAKE_ACP_LOG   append one JSON line per received message ({"recv": msg}) plus a startup {"argv", "env"} record
Behaviour by default: initialize -> protocolVersion min(asked, 1); session/new -> sessionId sess-1;
session/set_config_option -> {}; session/prompt streams 3 agent_message_chunk updates ("Hello", ", ", "world") plus
one tool_call (kind read) and a tool_call_update, then answers {"stopReason": "end_turn"}.
"""
import json
import os
import subprocess
import sys
import time

MODES = {m for m in os.environ.get("FAKE_ACP_MODE", "").split(",") if m}
LOG = os.environ.get("FAKE_ACP_LOG")


def log(rec):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def recv():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    msg = json.loads(line)
    log({"recv": msg})
    return msg


def update(sid, body):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": body}})


def chunk(sid, text):
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


def request(rid, method, params):
    """Send a request to the client and return its response (answering nothing else in between)."""
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        msg = recv()
        if msg.get("id") == rid and "method" not in msg:
            return msg


log({"argv": sys.argv[1:], "env": {k: os.environ.get(k) for k in ("FUSION_AGENT_ID", "BOARD_PROFILE",
                                                                 "FUSION_PEER_DEPTH", "FUSION_HOST",
                                                                 "CLAUDECODE")}})

if "spawn_child" in MODES:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pidfile = os.environ.get("FAKE_ACP_PIDFILE")
    if pidfile:
        with open(pidfile, "w") as fh:
            fh.write(str(child.pid))

SID = "sess-1"
seen_cancel = False

while True:
    msg = recv()
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        if "exit_early" in MODES:
            sys.stderr.write("fatal: api_key=sk-FAKESECRET12345 token=hunter2value\n")
            sys.stderr.flush()
            sys.exit(3)
        if "badline" in MODES:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        asked = params.get("protocolVersion", 1)
        version = 2 if "v2" in MODES else min(int(asked), 1)
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version, "agentCapabilities": {"loadSession": False},
            "agentInfo": {"name": "fake-acp", "version": "0"}, "authMethods": []}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "sessionId": SID, "modes": {"currentModeId": "default", "availableModes": []},
            "configOptions": [{"id": "model", "type": "select"}, {"id": "thinking", "type": "select"}]}})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": SID, "update": {"sessionUpdate": "available_commands_update", "availableCommands": []}}})
    elif method == "session/set_config_option":
        send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": []}})
    elif method == "session/cancel":
        seen_cancel = True
    elif method == "session/prompt":
        sid = params.get("sessionId", SID)
        if "hang" in MODES:
            chunk(sid, "starting")
            time.sleep(60)
            continue
        if "cancel" in MODES:
            chunk(sid, "working")
            while not seen_cancel:
                m2 = recv()
                if m2.get("method") == "session/cancel":
                    seen_cancel = True
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
            seen_cancel = False
            continue
        for piece in ("Hello", ", ", "world"):
            chunk(sid, piece)
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read README", "kind": "read",
                     "status": "pending"})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
        update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
        extra = ""
        for flag, kind in (("perm_read", "read"), ("perm_edit", "edit")):
            if flag in MODES:
                resp = request(900, "session/request_permission", {
                    "sessionId": sid,
                    "toolCall": {"toolCallId": "t2", "title": f"{kind} thing", "kind": kind, "status": "pending"},
                    "options": [{"optionId": "reject-1", "name": "Reject", "kind": "reject_once"},
                                {"optionId": "allow-1", "name": "Allow", "kind": "allow_once"}]})
                outcome = (resp.get("result") or {}).get("outcome") or {}
                extra += f" perm_{kind}={outcome.get('optionId') or outcome.get('outcome')}"
        if "fs" in MODES:
            r1 = request(901, "fs/read_text_file", {"sessionId": sid, "path": "/etc/hostname"})
            r2 = request(902, "terminal/create", {"sessionId": sid, "command": "ls"})
            extra += f" fs_err={(r1.get('error') or {}).get('code')} term_err={(r2.get('error') or {}).get('code')}"
        if extra:
            chunk(sid, extra)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
