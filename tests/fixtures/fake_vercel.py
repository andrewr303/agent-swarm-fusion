#!/usr/bin/env python3
"""Fake `vercel` CLI for tests: emulates `vercel board <verb> --json --as X` over a LocalEngineAdapter.

Never touches the network or the real CLI. Every invocation is one process, like the real thing, so claim proofs
travel through the shared claim cache (XDG_CONFIG_HOME) exactly as the stock CLI does.

Environment:
  FAKE_VERCEL_DB           sqlite path of the emulated board (required for board verbs)
  FAKE_VERCEL_STYLE        compact (default; standalone `vb`: one stderr line, empty stdout) |
                           pretty66 (vercel 66.2.0: pretty-printed on BOTH streams, extra cliVersion,
                           invocationId and feedback keys, no next[].argv)
  FAKE_VERCEL_LOG          append one JSON line {"argv": [...]} per invocation
  FAKE_VERCEL_INJECT       path of a JSON file {"reason", "times", "status"?, "retryAfter"?, "exit"?}; while
                           times > 0 the next board verbs fail with that reason (times is decremented)
  FAKE_VERCEL_VERSION      text printed by `--version` (default `Vercel CLI 66.2.0`)
  FAKE_VERCEL_HELP_EXTRA   appended to `board tasks --help` (changes the help hash)
  FAKE_VERCEL_BATCH_MAX    batch size limit (default 15, the hosted limit)

Hosted-board rules emulated: a bare-integer `--lease` is exit 2 `invalid_arguments`; leases are clamped to 300 s;
channel subtrees (`tasks work/`) are `not_supported`; conditional needs are an unknown option (exit 2).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm.vboard.model import REASONS, VBoardError, parse_iso  # noqa: E402
from fusion_swarm.vboard.port import LocalEngineAdapter  # noqa: E402

VALUE_FLAGS = {"--as", "--url", "--name", "--scope", "--label", "--needs", "--for", "--lease", "--idempotency-key",
               "--after", "--before", "--limit", "-n", "--by", "--text", "--re", "--state", "--wait", "--timeout",
               "--from", "--kind", "--file", "--if-revision", "--unset", "--if", "--if-unset", "--mentions",
               "--avatar", "--result"}
MULTI = {"--label", "--needs", "--from", "--unset", "--if", "--if-unset"}
BOOL = {"--json", "--no-sync", "--claim", "--ready", "--remove", "--pool", "--none", "--all", "--sync", "--ensure",
        "--private"}
KNOWN = VALUE_FLAGS | BOOL
VERSION = "66.2.0"
LEASE = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
TIMEOUT = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class Usage(Exception):
    def __init__(self, reason: str, message: str):
        self.reason, self.message = reason, message


def parse(tokens: list[str]) -> tuple[list[str], dict]:
    pos: list[str] = []
    flags: dict = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            pos += tokens[i + 1:]
            break
        if tok.startswith("-") and len(tok) > 1 and not tok[1:].isdigit():
            name, eq, val = tok.partition("=")
            if name not in KNOWN:
                raise Usage("unknown_option", f"unknown option {name}")
            if name in VALUE_FLAGS:
                if not eq:
                    if name == "--mentions" and (i + 1 >= len(tokens) or tokens[i + 1].startswith("-")):
                        val = "me"
                    else:
                        i += 1
                        if i >= len(tokens):
                            raise Usage("missing_arguments", f"{name} needs a value")
                        val = tokens[i]
                if name in MULTI:
                    flags.setdefault(name, []).append(val)
                else:
                    flags[name] = val
            else:
                flags[name] = True
        else:
            pos.append(tok)
        i += 1
    return pos, flags


def seconds(text: str, flag: str, *, strict: bool) -> float:
    m = (LEASE if strict else TIMEOUT).match(text or "")
    if not m:
        raise Usage("invalid_arguments", f"{flag} takes a duration like 30s or 10m, and {text} isn't one.")
    return float(m.group(1)) * UNIT[m.group(2)]


def emit_failure(err: VBoardError, style: str) -> int:
    doc = err.to_failure()
    cls = REASONS.get(err.code)
    rc = 2 if (cls is not None and cls.cls == "usage") or err.code in ("invalid_arguments", "unknown_option") else 1
    if style == "pretty66":
        doc = dict(doc)
        doc["next"] = [{"command": n.get("command"), "when": n.get("when")} for n in doc.get("next", [])]
        doc["cliVersion"] = VERSION
        doc["invocationId"] = uuid.uuid4().hex
        doc["feedback"] = {"message": "Something wrong?", "command": "vercel feedback"}
        text = json.dumps(doc, indent=2)
        print(text)
        print(text, file=sys.stderr)
    else:
        print(json.dumps(doc), file=sys.stderr)
    return rc


def inject(style: str) -> int | None:
    path = os.environ.get("FAKE_VERCEL_INJECT")
    if not path or not os.path.exists(path):
        return None
    spec = json.loads(Path(path).read_text())
    if int(spec.get("times", 0)) <= 0:
        return None
    spec["times"] = int(spec["times"]) - 1
    Path(path).write_text(json.dumps(spec))
    err = VBoardError(spec["reason"], spec.get("message", spec["reason"]), status=spec.get("status"),
                      retry_after=spec.get("retryAfter"))
    rc = emit_failure(err, style)
    return int(spec.get("exit", rc))


def help_text(verb: list[str]) -> str:
    if verb[:2] == ["board", "tasks"]:
        return ("vercel board tasks [channel] [--state S] [--label k=v]\n  tasks create <channel> --file plan.json "
                "--idempotency-key K --needs ID --claim --lease D\n  tasks claim <id> | --ready <channel> --wait D "
                "--lease D\n" + os.environ.get("FAKE_VERCEL_HELP_EXTRA", ""))
    return "vercel board <whoami|channels|read|post|reply|edit|delete|react|label|wait|tasks> [--idempotency-key K]\n"


def lease_of(flags: dict) -> float | None:
    if "--lease" not in flags:
        return None
    return min(300.0, seconds(flags["--lease"], "--lease", strict=True))


def run(pos: list[str], flags: dict, port: LocalEngineAdapter) -> dict | None:
    verb = pos[0] if pos else ""
    rest = pos[1:]
    labels = {}
    for item in flags.get("--label", []):
        k, _, v = item.partition("=")
        labels[k] = v
    if verb == "whoami":
        return port.whoami()
    if verb == "channels":
        return port.channels(rest[0] if rest else None)
    if verb == "read":
        return port.read(rest[0] if rest else None, after=flags.get("--after"), before=flags.get("--before"),
                         limit=int(flags["--limit"]) if "--limit" in flags else None,
                         label=flags.get("--label", []), by=flags.get("--by"), mentions=flags.get("--mentions"),
                         text=flags.get("--text"))
    if verb in ("post", "reply"):
        target, text = rest[0], rest[1] if len(rest) > 1 else ""
        fn = port.reply if verb == "reply" else port.post
        return fn(target, text, labels=labels or None, idempotency_key=flags.get("--idempotency-key"),
                  after=flags.get("--after"), no_sync=bool(flags.get("--no-sync")))
    if verb == "edit":
        return port.edit(rest[0], rest[1], if_revision=int(flags["--if-revision"]) if "--if-revision" in flags
                         else None)
    if verb in ("delete", "rm", "del"):
        return port.delete(rest[0], if_revision=int(flags["--if-revision"]) if "--if-revision" in flags else None)
    if verb == "react":
        return port.react(rest[0], rest[1], remove=bool(flags.get("--remove")))
    if verb == "label":
        sets = {}
        for item in rest[1:]:
            k, _, v = item.partition("=")
            sets[k] = v
        cond = {}
        for item in flags.get("--if", []):
            k, _, v = item.partition("=")
            cond[k] = v
        return port.label(rest[0], set=sets or None, unset=flags.get("--unset", []), if_=cond or None,
                          if_unset=flags.get("--if-unset", []))
    if verb == "wait":
        timeout = seconds(flags.get("--timeout", "30m"), "--timeout", strict=False)
        ev = port.wait(rest[0] if rest else None, after=flags.get("--after"), timeout=timeout,
                       label=flags.get("--label", []), by=flags.get("--by"), re=flags.get("--re"),
                       mentions=flags.get("--mentions"), text=flags.get("--text"))
        if ev is None:
            raise VBoardError("timeout", f"Nothing matched within {flags.get('--timeout', '30m')}.")
        return {"event": ev}
    if verb == "tasks":
        return tasks_verb(rest, flags, port, labels)
    raise Usage("unknown_command", f"unknown command {verb!r}")


def tasks_verb(rest: list[str], flags: dict, port: LocalEngineAdapter, labels: dict) -> dict:
    sub = rest[0] if rest else ""
    args = rest[1:]
    if sub in ("create",):
        if "--needs-if-failed" in flags:
            raise Usage("unknown_option", "unknown option --needs-if-failed")
        if "--file" in flags:
            plan = json.loads(Path(flags["--file"]).read_text())
            limit = int(os.environ.get("FAKE_VERCEL_BATCH_MAX", "15"))
            if len(plan) > limit:
                raise VBoardError("batch_too_large", f"a batch holds at most {limit} tasks")
            return port.store.create_tasks(port.caller, args[0], plan, client_id=flags.get("--idempotency-key")) | {
                "outcome": "created"}
        target = args[0]
        if len(args) == 1 and re.match(r"^(#?\d+|msg_\S+)$", target):
            return port.task_adopt(target, labels=labels or None, needs=flags.get("--needs", []),
                                   for_=flags.get("--for"), claim=bool(flags.get("--claim")), lease=lease_of(flags),
                                   idempotency_key=flags.get("--idempotency-key"))
        return port.task_create(target, args[1], labels=labels or None, needs=flags.get("--needs", []),
                                for_=flags.get("--for"), claim=bool(flags.get("--claim")), lease=lease_of(flags),
                                idempotency_key=flags.get("--idempotency-key"),
                                no_sync=bool(flags.get("--no-sync")))
    if sub == "claim":
        if flags.get("--ready"):
            return claim_ready(args[0], flags, port, labels)
        return port.task_claim(args[0], lease=lease_of(flags))
    if sub == "renew":
        return port.task_renew(args[0], lease=lease_of(flags))
    if sub == "release":
        return port.task_release(args[0])
    if sub == "complete":
        return port.task_complete(args[0], args[1] if len(args) > 1 else None)
    if sub == "fail":
        return port.task_fail(args[0], args[1])
    if sub == "block":
        return port.task_block(args[0], args[1], kind=flags.get("--kind"))
    if sub == "unblock":
        return port.task_unblock(args[0])
    if sub == "reopen":
        return port.task_reopen(args[0])
    if sub == "assign":
        return port.task_assign(args[0], None if flags.get("--pool") else (args[1] if len(args) > 1 else None))
    if sub == "needs":
        return port.task_needs(args[0], [] if flags.get("--none") else args[1:])
    if sub == "cancel":
        return port.task_cancel(args[0], args[1])
    # list
    if sub.endswith("/"):
        raise VBoardError("not_supported", "the hosted board does not read channel subtrees")
    return port.tasks(sub or None, state=flags.get("--state", "open"), label=flags.get("--label", []),
                      by=flags.get("--by"), for_=flags.get("--for"), after=flags.get("--after"),
                      limit=int(flags["--limit"]) if "--limit" in flags else None)


def claim_ready(channel: str, flags: dict, port: LocalEngineAdapter, labels: dict) -> dict:
    """Stock semantics: assigned-to-caller first, then oldest ready. No priority or defer handling."""
    if channel.endswith("/"):
        raise VBoardError("not_supported", "the hosted board does not read channel subtrees")
    wait = seconds(flags["--wait"], "--wait", strict=True) if "--wait" in flags else 0.0
    lease = lease_of(flags)
    me = port.whoami()
    owners = {me["principal"], *flags.get("--from", [])}
    deadline = time.monotonic() + wait
    while True:
        page = port.tasks(channel, state="ready", label=[f"{k}={v}" for k, v in labels.items()], limit=500)
        cands = []
        for i, summ in enumerate(page["tasks"]):
            msg = summ["message"]
            task = msg["task"]
            if task.get("for") and task["for"] != me["actor"]:
                continue
            if task.get("owner") not in owners:
                continue
            cands.append((0 if task.get("for") else 1, i, msg["id"]))
        for _a, _b, tid in sorted(cands):
            try:
                return port.task_claim(tid, lease=lease)
            except VBoardError as exc:
                if exc.code in ("task_claimed", "task_assigned", "task_finished", "task_waiting", "task_blocked"):
                    continue
                raise
        if time.monotonic() >= deadline:
            raise VBoardError("timeout" if wait else "nothing_ready",
                              "Nothing became ready in time." if wait else "Nothing is ready to claim.")
        time.sleep(0.05)


def main(argv: list[str]) -> int:
    style = os.environ.get("FAKE_VERCEL_STYLE", "compact")
    log = os.environ.get("FAKE_VERCEL_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv}) + "\n")
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_VERCEL_VERSION", f"Vercel CLI {VERSION}"))
        return 0
    if argv[:1] != ["board"]:
        print("fake vercel: only `board` and `--version`", file=sys.stderr)
        return 2
    if "--help" in argv[1:]:
        print(help_text([a for a in argv if a != "--help"]))
        return 0
    rc = inject(style)
    if rc is not None:
        return rc
    try:
        pos, flags = parse(argv[1:])
        db = os.environ.get("FAKE_VERCEL_DB")
        if not db:
            raise VBoardError("unexpected_error", "FAKE_VERCEL_DB is not set")
        port = LocalEngineAdapter(db, as_=flags.get("--as"), claim_cache=True)
        try:
            doc = run(pos, flags, port)
        finally:
            port.close()
        print(json.dumps(doc, ensure_ascii=False))
        return 0
    except Usage as exc:
        err = VBoardError(exc.reason, exc.message, status=400)
        doc = err.to_failure()
        doc["data"]["retryable"] = False
        if style == "pretty66":
            text = json.dumps({**doc, "cliVersion": VERSION, "invocationId": uuid.uuid4().hex,
                               "feedback": {"message": "Something wrong?", "command": "vercel feedback"}}, indent=2)
            print(text)
            print(text, file=sys.stderr)
        else:
            print(json.dumps(doc), file=sys.stderr)
        return 2
    except VBoardError as exc:
        return emit_failure(exc, style)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
