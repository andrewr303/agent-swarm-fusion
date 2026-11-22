"""ACP v1 stdio JSON-RPC client (Agent Client Protocol) for peer sessions.

    AcpClient   spawns the agent in its own process group, speaks newline-delimited JSON-RPC 2.0 over stdio,
                collects streamed `session/update` notifications until the `session/prompt` response arrives.
    connect()   policy-enforced factory: `peers.policy.route(peer, session=True)` decides the transport, a codex-model
                seat on a Claude host resolves to `andrewcode acp`, and a real `codex` / `codex-acp` spawn is refused
                unless the D2 gate is open (FUSION_ALLOW_CODEX_EXEC=1 or a codex host). Never `npx`.
    ask()       one-shot convenience: connect, start, new session, optional model/thinking, one prompt, close.

Security notes
- `permission="allow_read"` is ADVISORY for AndrewCode: AndrewCode has no sandbox (notes/g1-codex-peer-path.md s.6,
  judge J-09), so a read-classified tool call cannot be confined to reading. `allow_all` needs an explicit
  `allow_all=True` flag. `deny` is the default.
- We advertise NO fs/terminal client capabilities and answer any `fs/*` or `terminal/*` request with -32601.
- stderr of the agent is only kept as a short, redacted tail for error messages; nothing is logged or printed here.

Stdlib only; works on 3.11+.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from . import identity as _identity
from .policy import PeerError, PEERS, codex_exec_allowed, detect_host, is_codex_binary, require, route

PROTOCOL_VERSION = 1
PERMISSIONS = ("deny", "allow_read", "allow_all")
READ_KINDS = frozenset({"read", "search", "fetch"})
MAX_LINE = 8 * 1024 * 1024
MAX_TEXT = 10_000_000
MAX_PROMPT = 2_000_000
STDERR_TAIL = 4000
CANCEL_GRACE_S = 2.0
LAUNCHERS = frozenset({"npx", "npx.cmd", "bunx", "pnpx", "npm", "npm.cmd", "pnpm", "yarn"})
GATED_ACP = frozenset({"codex-acp", "codex-acp.exe", "codex-acp.cmd"})
THINKING_MAP = {"off": "off", "none": "off", "minimal": "low", "low": "low", "medium": "high", "high": "high",
                "xhigh": "max", "ultra": "max", "max": "max"}
TRANSPORT_ARGV = {"andrewcode-acp": ("andrewcode", ["acp"]),
                  "claude-acp": ("claude-agent-acp", []),
                  "codex-acp": ("codex-acp", [])}
_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|authorization|password|passwd|bearer)\s*[=:]?\s*)[^\s\"',;]+"
    r"|sk-[A-Za-z0-9_-]{8,}")


def _version() -> str:
    try:
        from ..vboard import VERSION
        return str(VERSION)
    except Exception:  # noqa: BLE001
        return "3.3.0+swarm"


class AcpError(PeerError):
    """codes (PeerError): peer_not_allowed, peer_absent, peer_failed, invalid_request, timeout, cancelled.
    `rpc_code` carries the JSON-RPC error code when the agent returned an error response."""

    def __init__(self, code: str, message: str = "", *, rpc_code: int | None = None):
        super().__init__(code, message)
        self.rpc_code = rpc_code


@dataclass
class AcpTurn:
    stop_reason: str
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    updates: int = 0
    elapsed_s: float = 0.0


def redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}[redacted]" if m.group(1) else "[redacted]", text or "")


def thinking_value(level: str | None) -> str | None:
    """ACP offers off|low|high|max; map xhigh/ultra/max -> max."""
    if level is None or str(level).strip() == "":
        return None
    key = str(level).strip().lower()
    if key not in THINKING_MAP:
        raise AcpError("invalid_request", f"unknown thinking level {level!r}")
    return THINKING_MAP[key]


def assert_spawnable(argv: Sequence[str], env: Mapping[str, str] | None = None) -> None:
    """Refuse launchers (npx...) and, without the D2 gate, any codex / codex-acp binary anywhere in argv."""
    if not argv:
        raise AcpError("invalid_request", "empty argv")
    for part in argv:
        base = os.path.basename(str(part)).lower()
        if base in LAUNCHERS:
            raise AcpError("peer_not_allowed", f"{base} is never used to launch peers (no on-demand installs)")
        if (is_codex_binary(part) or base in GATED_ACP) and not codex_exec_allowed(env):
            raise AcpError("peer_not_allowed",
                           "codex / codex-acp spawn requires FUSION_ALLOW_CODEX_EXEC=1 or a codex host")


def _kill_group(proc: subprocess.Popen, sig=signal.SIGKILL) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGKILL:  # pragma: no cover
            proc.kill()
        else:  # pragma: no cover
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass


class _Turn:
    """Mutable collector for one session/prompt."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.parts: list[str] = []
        self.size = 0
        self.calls: dict[str, dict] = {}
        self.order: list[str] = []
        self.updates = 0

    def add_text(self, text: str) -> None:
        if self.size < MAX_TEXT:
            self.parts.append(text[: MAX_TEXT - self.size])
            self.size += len(text)

    def add_call(self, update: Mapping) -> None:
        cid = str(update.get("toolCallId") or f"call-{len(self.order)}")
        cur = self.calls.get(cid)
        if cur is None:
            cur = {"toolCallId": cid}
            self.calls[cid] = cur
            self.order.append(cid)
        for key in ("title", "kind", "status", "rawInput", "rawOutput", "locations"):
            if key in update and update[key] is not None:
                cur[key] = update[key]


class AcpClient:
    def __init__(self, argv: list[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
                 timeout: float = 600, permission: str = "deny", allow_all: bool = False,
                 on_update: Callable[[dict], None] | None = None, cancel=None,
                 protocol_version: int = PROTOCOL_VERSION, policy_env: Mapping[str, str] | None = None):
        if permission not in PERMISSIONS:
            raise AcpError("invalid_request", f"permission must be one of {PERMISSIONS}")
        if permission == "allow_all" and not allow_all:
            raise AcpError("invalid_request", "permission allow_all requires the explicit allow_all=True flag")
        self.argv = [str(a) for a in argv]
        self.cwd = cwd
        self.env = dict(os.environ if env is None else env)
        self.policy_env = dict(self.env if policy_env is None else policy_env)
        self.timeout = float(timeout)
        self.permission = permission
        self.on_update = on_update
        self.cancel = cancel
        self.offer_version = int(protocol_version)
        self.default_model: str | None = None
        self.transport: str | None = None
        self.proc: subprocess.Popen | None = None
        self.agent_info: dict = {}
        self.permission_log: list[dict] = []
        self.ignored_notifications = 0
        self.bad_lines = 0
        self._q: "queue.Queue[tuple[str, dict | None]]" = queue.Queue()
        self._next_id = 0
        self._wlock = threading.Lock()
        self._closed = False
        self._close_lock = threading.Lock()
        self._turn: _Turn | None = None
        self._cancel_deadline: float | None = None
        self._threads: list[threading.Thread] = []
        self._stderr: collections.deque = collections.deque()
        self._stderr_size = 0
        self._unregister = None

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> dict:
        if self.proc is not None:
            raise AcpError("invalid_request", "client already started")
        assert_spawnable(self.argv, self.policy_env)
        kwargs: dict = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # pragma: no cover
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        except FileNotFoundError:
            raise AcpError("peer_absent", f"{os.path.basename(self.argv[0])} not found") from None
        except OSError as exc:
            raise AcpError("peer_failed", f"cannot spawn {os.path.basename(self.argv[0])}: {exc.strerror}") from None
        reg = getattr(self.cancel, "register", None)
        if callable(reg):
            try:
                self._unregister = reg(self.proc, None)
            except Exception:  # noqa: BLE001
                self._unregister = None
        for target, name in ((self._read_stdout, "acp-stdout"), (self._read_stderr, "acp-stderr")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        params = {"protocolVersion": self.offer_version,
                  "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                  "clientInfo": {"name": "fusion", "version": _version()}}
        result = self._request("initialize", params)
        got = result.get("protocolVersion")
        if got != PROTOCOL_VERSION:
            self.close()
            raise AcpError("peer_failed", f"agent negotiated ACP protocolVersion {got!r}; only v1 is supported")
        self.agent_info = {k: result.get(k) for k in ("agentInfo", "agentCapabilities", "authMethods")
                           if k in result}
        return result

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        proc = self.proc
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            _kill_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            _kill_group(proc, signal.SIGKILL)  # stragglers in the group, even after the leader exited
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
            for t in self._threads:
                t.join(timeout=1)
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except (OSError, ValueError):
                    pass
        if callable(self._unregister):
            try:
                self._unregister()
            except Exception:  # noqa: BLE001
                pass
        self._q.put(("eof", None))  # wake any waiter

    # ------------------------------------------------------------------ reader threads
    def _read_stdout(self) -> None:
        proc = self.proc
        assert proc is not None
        try:
            while True:
                line = proc.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    self.bad_lines += 1
                    while line and not line.endswith(b"\n"):  # drain the rest of the oversized line
                        line = proc.stdout.readline(MAX_LINE)
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    self.bad_lines += 1
                    continue
                if isinstance(msg, dict):
                    self._q.put(("msg", msg))
                else:
                    self.bad_lines += 1
        except (OSError, ValueError):
            pass
        finally:
            self._q.put(("eof", None))

    def _read_stderr(self) -> None:
        proc = self.proc
        assert proc is not None
        try:
            while True:
                chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr.append(chunk)
                self._stderr_size += len(chunk)
                while self._stderr_size > STDERR_TAIL and len(self._stderr) > 1:
                    self._stderr_size -= len(self._stderr.popleft())
        except (OSError, ValueError):
            pass

    def stderr_tail(self, limit: int = 300) -> str:
        data = b"".join(self._stderr).decode("utf-8", "replace").strip()
        return redact(data)[-limit:]

    # ------------------------------------------------------------------ wire
    def _send(self, obj: dict) -> None:
        if self._closed or self.proc is None or self.proc.stdin is None:
            raise AcpError("peer_failed", "ACP connection is closed")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                raise AcpError("peer_failed", "agent closed its stdin") from None

    def _request(self, method: str, params: dict, *, timeout: float | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self._await(rid, method, time.monotonic() + (self.timeout if timeout is None else timeout))

    def _await(self, rid: int, method: str, deadline: float) -> dict:
        while True:
            now = time.monotonic()
            if now >= deadline:
                self._timeout(method)
            if self._external_cancel(now):
                self.close()
                raise AcpError("cancelled", f"{method} cancelled")
            try:
                kind, msg = self._q.get(timeout=min(0.05, max(0.0, deadline - now)))
            except queue.Empty:
                continue
            if kind == "eof" or msg is None:
                detail = self.stderr_tail()
                rc = self.proc.poll() if self.proc else None
                self.close()
                raise AcpError("peer_failed", f"agent exited during {method}"
                                              + (f" (rc {rc})" if rc is not None else "")
                                              + (f": {detail}" if detail else ""))
            if "method" in msg:
                if "id" in msg:
                    self._server_request(msg)
                else:
                    self._notification(msg)
                continue
            if msg.get("id") != rid:
                continue  # stale/unknown response
            err = msg.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                raise AcpError("peer_failed", f"{method} failed: {code} {str(err.get('message', ''))[:300]}",
                               rpc_code=code if isinstance(code, int) else None)
            res = msg.get("result")
            return res if isinstance(res, dict) else {}

    def _timeout(self, method: str) -> None:
        turn = self._turn
        if turn is not None:
            try:
                self.cancel_turn(turn.session_id)
            except AcpError:
                pass
        self.close()
        raise AcpError("timeout", f"{method} timed out after {self.timeout:g}s")

    def _external_cancel(self, now: float) -> bool:
        """True when the caller's cancel object fired and the grace window (if any) is over."""
        if self.cancel is None or not getattr(self.cancel, "is_set", lambda: False)():
            return False
        turn = self._turn
        if turn is None:
            return True
        if self._cancel_deadline is None:
            try:
                self.cancel_turn(turn.session_id)
            except AcpError:
                return True
            self._cancel_deadline = now + CANCEL_GRACE_S
            return False
        return now >= self._cancel_deadline

    # ------------------------------------------------------------------ inbound traffic
    def _notification(self, msg: dict) -> None:
        if msg.get("method") != "session/update":
            self.ignored_notifications += 1
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        turn = self._turn
        if turn is None or params.get("sessionId") != turn.session_id:
            self.ignored_notifications += 1
            return
        turn.updates += 1
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                turn.add_text(content["text"])
        elif kind in ("tool_call", "tool_call_update"):
            turn.add_call(update)
        if self.on_update is not None:
            try:
                self.on_update(update)
            except Exception:  # noqa: BLE001 - a bad callback never breaks the protocol loop
                pass

    def _server_request(self, msg: dict) -> None:
        rid, method = msg.get("id"), str(msg.get("method"))
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "session/request_permission":
            outcome = self._decide_permission(params)
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})
            return
        # fs/* and terminal/* (we advertise none of them) and anything unknown
        self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}})

    def _decide_permission(self, params: Mapping) -> dict:
        call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        kind = str(call.get("kind") or "")
        options = [o for o in (params.get("options") or []) if isinstance(o, dict)]
        allow = next((o for o in options if str(o.get("kind", "")).startswith("allow") and o.get("optionId")), None)
        selected = None
        if self.permission == "allow_all":
            selected = allow
        elif self.permission == "allow_read" and kind in READ_KINDS:
            selected = allow
        self.permission_log.append({"tool_kind": kind, "title": str(call.get("title") or "")[:80],
                                    "decision": "allow" if selected else "deny"})
        if selected is None:
            return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": selected["optionId"]}

    # ------------------------------------------------------------------ session API
    def new_session(self, cwd: str, *, mcp_servers: list | None = None) -> dict:
        return self._request("session/new", {"cwd": os.path.abspath(cwd), "mcpServers": list(mcp_servers or [])})

    def set_config(self, session_id: str, config_id: str, value: str) -> dict:
        return self._request("session/set_config_option",
                             {"sessionId": session_id, "configId": config_id, "value": value})

    def prompt(self, session_id: str, text: str) -> AcpTurn:
        if not isinstance(text, str) or not text.strip():
            raise AcpError("invalid_request", "prompt is required")
        if len(text) > MAX_PROMPT:
            raise AcpError("invalid_request", "prompt too large")
        if self._turn is not None:
            raise AcpError("invalid_request", "a prompt is already running on this client")
        started = time.monotonic()
        turn = _Turn(session_id)
        self._turn = turn
        self._cancel_deadline = None
        try:
            result = self._request("session/prompt", {"sessionId": session_id,
                                                      "prompt": [{"type": "text", "text": text}]})
        finally:
            self._turn = None
        calls = [turn.calls[c] for c in turn.order]
        return AcpTurn(stop_reason=str(result.get("stopReason") or ""), text="".join(turn.parts), tool_calls=calls,
                       updates=turn.updates, elapsed_s=round(time.monotonic() - started, 3))

    def cancel_turn(self, session_id: str) -> None:
        self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})


# ---------------------------------------------------------------------------------------------- connect / ask
def connect(target: str, *, env: Mapping[str, str] | None = None, cwd: str | None = None, name: str | None = None,
            start: bool = False, **kw) -> AcpClient:
    """Policy-enforced factory. `target` is a peer (andrewcode|claude|codex); returns an UNSTARTED client unless
    `start=True`. The route comes from `policy.route(peer, session=True)`:

        andrewcode          -> `andrewcode acp`
        codex (default)     -> `andrewcode acp` (model gpt-6.1-sol; the codex-model seat), `codex-acp` only when the
                               D2 gate is open AND it is on PATH
        claude              -> `claude-agent-acp` (never npx), only when `claude_spawn_allowed`
    """
    if target not in PEERS or target == "opencode":
        raise AcpError("invalid_request", f"{target!r} has no ACP transport (andrewcode|claude|codex)")
    parent = dict(os.environ if env is None else env)
    rt = require(route(target, session=True, env=parent))
    binary, extra = TRANSPORT_ARGV[rt.transport]
    resolved = shutil.which(binary, path=parent.get("PATH"))
    if resolved is None:
        raise AcpError("peer_absent", f"{binary} not found on PATH")
    argv = [resolved, *extra]
    assert_spawnable(argv, parent)
    ident = name or _identity.agent_name(parent) or f"{target}-peer"
    child = _identity.child_env(parent, ident)
    host = detect_host(parent)
    if host != "unknown":
        child["FUSION_HOST"] = host  # stripped Claude markers must not turn the child into a "codex host"
        child.pop("FUSION_HOST_EXPLICIT", None)
    client = AcpClient(argv, cwd=cwd, env=child, policy_env=parent, **kw)
    client.default_model = rt.default_model
    client.transport = rt.transport
    if start:
        client.start()
    return client


def _failure(target: str, transport: str | None, status: str, error: str, elapsed: float, **extra) -> dict:
    out = {"ok": False, "status": status, "peer": target, "transport": transport, "text": "", "stop_reason": None,
           "tool_calls": [], "updates": 0, "model": None, "session_id": None, "elapsed_s": round(elapsed, 3),
           "error": error, "warnings": [], "permission_decisions": []}
    out.update(extra)
    return out


def ask(target: str, prompt: str, *, model: str | None = None, thinking: str | None = None, cwd: str | None = None,
        permission: str = "deny", allow_all: bool = False, timeout: float = 600, env: Mapping[str, str] | None = None,
        name: str | None = None, cancel=None, on_update: Callable[[dict], None] | None = None) -> dict:
    """One session, one prompt. Policy errors (peer_not_allowed, invalid_request, depth_exceeded) raise PeerError;
    runtime failures (timeout, cancelled, agent died, protocol error) come back as an `ok: False` dict."""
    started = time.monotonic()
    think = thinking_value(thinking)
    client = connect(target, env=env, name=name, timeout=timeout, permission=permission, allow_all=allow_all,
                     on_update=on_update, cancel=cancel)
    transport = client.transport
    warnings: list[str] = []
    if transport == "andrewcode-acp" and permission != "deny":
        warnings.append("ACP permission policy is advisory for AndrewCode: it has no sandbox, so a read-classified "
                        "tool call is not confined to reading")
    tmp = None
    try:
        work = cwd
        if work is None:
            tmp = tempfile.mkdtemp(prefix="fusion-acp-")
            work = tmp
        try:
            client.start()
            session = client.new_session(work)
            sid = str(session.get("sessionId") or "")
            if not sid:
                raise AcpError("peer_failed", "agent returned no sessionId")
            use_model = model or client.default_model
            if use_model:
                client.set_config(sid, "model", use_model)
            if think:
                client.set_config(sid, "thinking", think)
            turn = client.prompt(sid, prompt)
        except AcpError as exc:
            if exc.code in ("timeout", "cancelled", "peer_failed", "peer_absent"):
                status = {"peer_failed": "error", "peer_absent": "absent"}.get(exc.code, exc.code)
                return _failure(target, transport, status, str(exc), time.monotonic() - started,
                                warnings=warnings, permission_decisions=list(client.permission_log))
            raise
        ok = turn.stop_reason == "end_turn" and bool(turn.text.strip())
        status = "returned" if ok else ("cancelled" if turn.stop_reason == "cancelled" else "error")
        err = None if ok else (f"stopReason={turn.stop_reason or 'none'}" + ("" if turn.text.strip() else ", no text"))
        return {"ok": ok, "status": status, "peer": target, "transport": transport, "text": turn.text,
                "stop_reason": turn.stop_reason, "tool_calls": turn.tool_calls, "updates": turn.updates,
                "model": use_model, "session_id": sid, "elapsed_s": round(time.monotonic() - started, 3),
                "error": err, "warnings": warnings, "permission_decisions": list(client.permission_log)}
    finally:
        client.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
