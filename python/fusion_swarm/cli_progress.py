"""Forward bounded real CLI chat messages as framed ``FUSION_CHAT_V1`` packets.

Packets carry only supported bounded chat fields (assistant/tool messages);
meta/auth fields in the raw stream are never forwarded.
"""
from __future__ import annotations

import json
import os
import sys

PREFIX = "FUSION_CHAT_V1 "


def clipped(text, limit):
    """Keep the last ``limit`` UTF-16 code units of ``str(text)``."""
    return str(text).encode("utf-16-le", errors="replace")[-limit * 2:].decode("utf-16-le", errors="ignore")


def _meta(meta=None):
    meta = meta or {}
    env = os.environ
    return {"id": meta.get("id") or env.get("FUSION_PROGRESS_ID", ""),
            "role": meta.get("role") or env.get("FUSION_PROGRESS_ROLE", "worker"),
            "model": meta.get("model") or env.get("FUSION_PROGRESS_MODEL", ""),
            "effort": meta.get("effort") or env.get("FUSION_PROGRESS_EFFORT", "")}


def emit(kind, _stream=None, _meta_override=None, **fields):
    packet = {**_meta(_meta_override), "kind": kind, **fields}
    print(PREFIX + json.dumps(packet, ensure_ascii=True), file=_stream or sys.stdout, flush=True)


def clean_message(raw):
    role = raw.get("role")
    if role not in ("assistant", "tool"):
        return None
    content = raw.get("content")
    message = {"role": role, "content": clipped(content, 12000) if isinstance(content, str) else ""}
    if role == "assistant":
        calls = []
        for call in (raw.get("tool_calls") or [])[:8]:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            function = call["function"]
            calls.append({"id": clipped(call.get("id", ""), 100), "name": clipped(function.get("name", ""), 100),
                          "arguments": clipped(function.get("arguments", ""), 4000)})
        message["toolCalls"] = calls
    else:
        message["toolCallId"] = str(raw.get("tool_call_id", ""))[:100]
        if raw.get("is_error") is True:
            message["isError"] = True
    if len(json.dumps(message, ensure_ascii=True)) > 60000:
        message["content"] = clipped(message["content"], 2000)
        for call in message.get("toolCalls", []):
            call["arguments"] = clipped(call["arguments"], 300)
    return message


def relay(line, stream=None, meta=None):
    """Emit one packet for a raw stream-json line; return the cleaned message or None."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    message = clean_message(raw)
    if message is None:
        return None
    emit("message", _stream=stream if stream is not None else sys.stderr, _meta_override=meta, message=message)
    return message


def main():
    # The raw stream stays in the adapter's owned scratch directory. Only
    # supported bounded chat fields are forwarded; meta/auth fields are omitted.
    with open(sys.argv[1], "w", encoding="utf-8") as raw_file, open(sys.argv[2], "w", encoding="utf-8") as text_file:
        for line in sys.stdin:
            raw_file.write(line)
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            message = clean_message(raw)
            if message is None:
                continue
            emit("message", message=message)
            if message["role"] == "assistant" and message["content"]:
                text_file.write(message["content"] + "\n")


if __name__ == "__main__":
    main()
