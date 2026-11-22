"""`python -m fusion_swarm.peers` CLI: policy | route | ask | acp | handoff | whoami.

Exit codes: 0 ok, 1 peer/board failure, 2 policy or usage error. Output is JSON on stdout; errors are one JSON object
`{"error": {"code", "message"}}` on stderr. Never prints secrets.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import threading
from typing import Mapping, Sequence

from . import acp as _acp
from . import exec_json as _exec
from . import handoff as _handoff
from . import identity as _identity
from . import policy as _policy
from .policy import PeerError

POLICY_CODES = frozenset({"peer_not_allowed", "invalid_request", "depth_exceeded"})


def _dump(obj, out) -> None:
    out.write(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def _route_doc(rt: _policy.Route) -> dict:
    return dataclasses.asdict(rt)


def _safe_route(peer: str, session: bool, env) -> dict:
    try:
        return _route_doc(_policy.route(peer, session=session, env=env))
    except PeerError as exc:
        return {"peer": peer, "allowed": False, "error": exc.code, "reason": exc.message}


def cmd_policy(args, env, out, err) -> int:
    table = {p: {"oneshot": _safe_route(p, False, env), "session": _safe_route(p, True, env)}
             for p in _policy.PEERS}
    _dump({"host": _policy.detect_host(env), "codex_exec_allowed": _policy.codex_exec_allowed(env),
           "claude_spawn_allowed": _policy.claude_spawn_allowed(env), "routes": table}, out)
    return 0


def cmd_route(args, env, out, err) -> int:
    rt = _policy.route(args.peer, session=args.session, env=env)
    _dump(_route_doc(rt), out)
    return 0 if rt.allowed else 2


def _prompt_arg(args) -> str:
    if args.prompt is not None and args.prompt_file:
        raise PeerError("invalid_request", "use --prompt or --prompt-file, not both")
    if args.prompt_file:
        try:
            with open(args.prompt_file, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise PeerError("invalid_request", f"cannot read prompt file: {exc.strerror}") from None
    if args.prompt is None:
        raise PeerError("invalid_request", "--prompt or --prompt-file is required")
    return args.prompt


def cmd_ask(args, env, out, err) -> int:
    schema = None
    if args.json_schema:
        try:
            with open(args.json_schema, encoding="utf-8") as fh:
                schema = json.load(fh)
        except (OSError, ValueError) as exc:
            raise PeerError("invalid_request", f"cannot read --json-schema: {exc}") from None
    req = _exec.PeerRequest(prompt=_prompt_arg(args), model=args.model, effort=args.effort, cwd=args.cwd,
                            json_schema=schema, tools=args.tools, timeout=args.timeout, name=args.as_)
    res = _exec.run(args.peer, req, env=env)
    _dump(dataclasses.asdict(res), out)
    return 0 if res.ok else 1


def cmd_acp(args, env, out, err) -> int:
    if args.permission == "allow_all" and not args.yes_allow_all:
        raise PeerError("invalid_request", "--permission allow_all needs --yes-allow-all")
    res = _acp.ask(args.target, _prompt_arg(args), model=args.model, thinking=args.thinking, cwd=args.cwd,
                   permission=args.permission, allow_all=args.yes_allow_all, timeout=args.timeout, env=env,
                   name=args.as_)
    _dump(res, out)
    return 0 if res["ok"] else 1


def _read_text(path: str | None, inline: str | None, what: str) -> str:
    if path and inline is not None:
        raise PeerError("invalid_request", f"use --{what} or --{what}-file, not both")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise PeerError("invalid_request", f"cannot read {what} file: {exc.strerror}") from None
    if inline is None:
        raise PeerError("invalid_request", f"--{what} or --{what}-file is required")
    return inline


def _command_handler(cmd: Sequence[str], env: Mapping[str, str], timeout: float, name: str):
    def run(task: dict) -> str:
        text = str(task.get("text") or "")
        child = _identity.child_env(env, name, task_id=str(task.get("id")))
        _acp.assert_spawnable(cmd, env)
        kwargs: dict = {"start_new_session": True} if os.name == "posix" else {}
        proc = subprocess.Popen(list(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=child, **kwargs)
        timer = threading.Timer(timeout, _acp._kill_group, args=(proc,))
        timer.start()
        try:
            out_b, _ = proc.communicate(text.encode("utf-8"))
        finally:
            timer.cancel()
            _acp._kill_group(proc)
        if proc.returncode != 0:
            raise RuntimeError(f"handler exited {proc.returncode}")
        return out_b.decode("utf-8", "replace")

    return run


def cmd_handoff(args, env, out, err) -> int:
    port = _handoff.open_board(args.board, as_=args.as_, env=env)
    try:
        if args.hcmd == "submit":
            brief = _read_text(args.brief_file, args.brief, "brief")
            payload = None
            if args.payload_file:
                try:
                    with open(args.payload_file, encoding="utf-8") as fh:
                        payload = json.load(fh)
                except (OSError, ValueError) as exc:
                    raise PeerError("invalid_request", f"cannot read --payload-file: {exc}") from None
            job = _handoff.submit(port, args.channel, brief, to=args.to, payload=payload, idempotency_key=args.key)
            _dump(job.to_dict(), out)
            return 0
        if args.hcmd == "status":
            _dump(_handoff.status(port, args.job).to_dict(), out)
            return 0
        if args.hcmd == "wait":
            job = _handoff.wait(port, args.job, timeout=args.timeout)
            _dump(job.to_dict(), out)
            return 0 if job.state == "completed" else 1
        if args.hcmd == "get":
            text = _handoff.get_result(port, args.job)
            if text is None:
                err.write(json.dumps({"error": {"code": "not_ready", "message": "job is not finished"}}) + "\n")
                return 1
            out.write(text if text.endswith("\n") or not text else text + "\n")
            return 0
        if args.hcmd == "serve":
            cmd = list(args.cmd[1:]) if args.cmd and args.cmd[0] == "--" else list(args.cmd)
            if not cmd:
                raise PeerError("invalid_request", "serve needs a command after --")
            _acp.assert_spawnable(cmd, env)
            name = args.as_ or _identity.resolve_name(env) or "handoff"
            handler = _command_handler(cmd, env, args.handler_timeout, name)
            served, rc = 0, 0
            while args.max == 0 or served < args.max:
                job = _handoff.serve_one(port, args.channel, handler, lease=args.lease, wait_s=args.wait)
                if job is None:
                    break
                served += 1
                _dump(job.to_dict(), out)
                if job.state != "completed":
                    rc = 1
            return rc
        raise PeerError("invalid_request", f"unknown handoff command {args.hcmd!r}")
    finally:
        port.close()


def cmd_whoami(args, env, out, err) -> int:
    doc = {"name": _identity.resolve_name(env), "host": _policy.detect_host(env),
           "peer_depth": env.get("FUSION_PEER_DEPTH", "0"),
           "max_depth": env.get("FUSION_PEER_MAX_DEPTH", str(_identity.DEFAULT_MAX_DEPTH))}
    if args.board:
        port = _handoff.open_board(args.board, as_=args.as_, env=env)
        try:
            doc["board"] = port.whoami()
        finally:
            port.close()
    _dump(doc, out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m fusion_swarm.peers", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("policy", help="route table for this environment (JSON)").set_defaults(fn=cmd_policy)
    p = sub.add_parser("route", help="one route")
    p.add_argument("peer")
    p.add_argument("--session", action="store_true")
    p.set_defaults(fn=cmd_route)

    p = sub.add_parser("ask", help="one-shot structured peer call (prints PeerResult JSON)")
    p.add_argument("peer")
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--cwd")
    p.add_argument("--tools", choices=_exec.TOOLS, default="none")
    p.add_argument("--json-schema")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--as", dest="as_")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("acp", help="one ACP v1 session turn (andrewcode|claude|codex)")
    p.add_argument("target", choices=("andrewcode", "claude", "codex"))
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--model")
    p.add_argument("--thinking")
    p.add_argument("--cwd")
    p.add_argument("--permission", choices=_acp.PERMISSIONS, default="deny",
                   help="allow_read is advisory for andrewcode (no sandbox)")
    p.add_argument("--yes-allow-all", action="store_true", help="explicit confirmation required for allow_all")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--as", dest="as_")
    p.set_defaults(fn=cmd_acp)

    p = sub.add_parser("handoff", help="durable handoff over the board")
    p.add_argument("--board", help="board spec (default env FUSION_VBOARD): local, local:<db>, http://..., vercel")
    p.add_argument("--as", dest="as_")
    hs = p.add_subparsers(dest="hcmd", required=True)
    q = hs.add_parser("submit")
    q.add_argument("--channel", required=True)
    q.add_argument("--brief")
    q.add_argument("--brief-file")
    q.add_argument("--payload-file")
    q.add_argument("--key", required=True)
    q.add_argument("--to")
    q = hs.add_parser("status")
    q.add_argument("job")
    q = hs.add_parser("wait")
    q.add_argument("job")
    q.add_argument("--timeout", type=float, default=1800)
    q = hs.add_parser("get")
    q.add_argument("job")
    q = hs.add_parser("serve")
    q.add_argument("--channel", required=True)
    q.add_argument("--lease", type=float, default=300)
    q.add_argument("--wait", type=float, default=0)
    q.add_argument("--max", type=int, default=1, help="tasks to serve (0 = until none is ready)")
    q.add_argument("--handler-timeout", type=float, default=600)
    q.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_handoff)

    p = sub.add_parser("whoami")
    p.add_argument("--board")
    p.add_argument("--as", dest="as_")
    p.set_defaults(fn=cmd_whoami)
    return ap


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    env = os.environ if env is None else env
    try:
        with contextlib.redirect_stderr(err):
            args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        return int(args.fn(args, env, out, err))
    except PeerError as exc:
        err.write(json.dumps({"error": {"code": exc.code, "message": exc.message}}) + "\n")
        return 2 if exc.code in POLICY_CODES else 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    sys.exit(main())
