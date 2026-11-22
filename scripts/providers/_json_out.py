#!/usr/bin/env python3
"""Extract final text and run metadata from provider CLI structured output.

Usage:
  _json_out.py <kind> <raw_file> <out_text_file> [--model M] [--meta-only]
  _json_out.py meta <out_file> <model> [session_id] [usage_json]

kind is one of: opencode grok agy claude andrewcode.
Writes <out_text_file> (final text) and <out_text_file>.meta.json:
  {"model", "session_id", "usage", "cost_usd", "kind"[, "parse_error": true]}
On parse failure the raw text is copied to the output and parse_error is set.
Stdlib only; never reads or echoes environment variables or config files.
"""
import json
import os
import sys

KINDS = ("opencode", "grok", "agy", "claude", "andrewcode")
TEXT_KEYS = ("result", "text", "output", "response", "content", "message")


def _atomic_write(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _events(raw):
    """Whole-document JSON (object or list) or JSON lines -> list of dicts."""
    s = raw.strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except ValueError:
        pass
    out = []
    for line in s.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _walk(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _first_str(events, keys):
    for ev in reversed(events):
        for d in _walk(ev):
            for k in keys:
                if isinstance(d.get(k), str) and d[k]:
                    return d[k]
    return None


def _usage(events):
    for ev in reversed(events):
        for d in _walk(ev):
            if isinstance(d.get("usage"), dict):
                return d["usage"]
            if isinstance(d.get("tokens"), dict):
                return d["tokens"]
    return None


def _cost(events):
    for ev in reversed(events):
        for key in ("total_cost_usd", "cost_usd", "cost"):
            if isinstance(ev.get(key), (int, float)) and not isinstance(ev.get(key), bool):
                return ev[key]
        for d in _walk(ev):
            if isinstance(d.get("cost"), (int, float)) and not isinstance(d.get("cost"), bool):
                return d["cost"]
    return None


def _session(events):
    return _first_str(events, ("session_id", "sessionId", "sessionID", "session"))


def _model(events):
    return _first_str(events, ("modelID", "model_id", "model"))


def parse_opencode(raw):
    events = _events(raw)
    parts, last_id = [], None
    for ev in events:
        part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
        if ev.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append((part.get("messageID"), part["text"]))
    if parts:
        last_id = parts[-1][0]
        text = "".join(t for mid, t in parts if mid == last_id) if last_id else "".join(t for _, t in parts)
    else:  # tolerate shape drift: concatenate any "text" strings
        text = "".join(d["text"] for ev in events for d in _walk(ev) if isinstance(d.get("text"), str))
    return text, {"model": _model(events), "session_id": _session(events),
                  "usage": _usage(events), "cost_usd": _cost(events)}


def parse_generic(raw):
    events = _events(raw)
    if not events:
        raise ValueError("no json")
    text = None
    for ev in reversed(events):
        for k in TEXT_KEYS:
            if isinstance(ev.get(k), str) and ev[k].strip():
                text = ev[k]
                break
        if text is not None:
            break
    if text is None:
        raise ValueError("no text")
    return text, {"model": _model(events), "session_id": _session(events),
                  "usage": _usage(events), "cost_usd": _cost(events)}


def parse_claude(raw):
    events = _events(raw)
    data = next((e for e in reversed(events) if e.get("type") == "result"), None) or (events[-1] if events else None)
    if not isinstance(data, dict) or not isinstance(data.get("result"), str):
        raise ValueError("no result")
    models = data.get("model_usage") or data.get("modelUsage")
    return data["result"], {
        "model": next(iter(models)) if isinstance(models, dict) and models else None,
        "session_id": data.get("session_id") if isinstance(data.get("session_id"), str) else None,
        "usage": data.get("usage") if isinstance(data.get("usage"), dict) else None,
        "cost_usd": data.get("total_cost_usd") if isinstance(data.get("total_cost_usd"), (int, float)) else None,
    }


def parse_andrewcode(raw):
    events = _events(raw)
    final = ""
    for ev in events:
        if ev.get("role") == "assistant" and not ev.get("tool_calls"):
            c = ev.get("content")
            if isinstance(c, str) and c.strip():
                final = c
            elif isinstance(c, list):
                j = "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
                if j.strip():
                    final = j
    if not final.strip():
        raise ValueError("no final assistant message")
    return final, {"model": None, "session_id": _session(events), "usage": _usage(events), "cost_usd": _cost(events)}


PARSERS = {"opencode": parse_opencode, "grok": parse_generic, "agy": parse_generic,
           "claude": parse_claude, "andrewcode": parse_andrewcode}


def write_meta(out_file, model, session=None, usage=None, cost=None, kind="adapter", parse_error=False):
    meta = {"model": model or None, "session_id": session or None, "usage": usage,
            "cost_usd": cost, "kind": kind}
    if parse_error:
        meta["parse_error"] = True
    _atomic_write(out_file + ".meta.json", json.dumps(meta, sort_keys=True) + "\n")


def extract(kind, raw_file, out_file, requested_model=None, meta_only=False):
    with open(raw_file, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    try:
        text, meta = PARSERS[kind](raw)
        if not text.strip():
            raise ValueError("empty text")
        parse_error = False
    except (ValueError, KeyError, TypeError):
        text, meta, parse_error = raw, {}, True
    if not meta.get("model"):
        meta["model"] = requested_model or None
    if not meta_only:
        _atomic_write(out_file, text)
    write_meta(out_file, meta.get("model"), meta.get("session_id"), meta.get("usage"),
               meta.get("cost_usd"), kind, parse_error)
    return 0


def main(argv):
    if len(argv) >= 3 and argv[0] == "meta":
        usage = None
        if len(argv) > 4 and argv[4]:
            try:
                usage = json.loads(argv[4])
            except ValueError:
                usage = None
        write_meta(argv[1], argv[2] if len(argv) > 2 else None, argv[3] if len(argv) > 3 else None, usage)
        return 0
    if len(argv) < 3 or argv[0] not in KINDS:
        print(__doc__, file=sys.stderr)
        return 2
    model, meta_only, rest = None, False, argv[3:]
    i = 0
    while i < len(rest):
        if rest[i] == "--model" and i + 1 < len(rest):
            model = rest[i + 1]
            i += 2
        elif rest[i] == "--meta-only":
            meta_only = True
            i += 1
        else:
            i += 1
    try:
        return extract(argv[0], argv[1], argv[2], model, meta_only)
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
