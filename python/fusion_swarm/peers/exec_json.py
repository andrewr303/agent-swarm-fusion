"""One-shot structured peer adapters (claude -p, andrewcode, codex exec, opencode run)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from . import identity as _identity
from .policy import PeerError, assert_exec_allowed, codex_exec_allowed, is_codex_binary, require, route

Runner = Callable[..., "tuple[int, str, str]"]

TOOLS = ("none", "read", "write")
CLAUDE_DENY = "Bash Edit Write MultiEdit NotebookEdit WebFetch"
OPENCODE_INLINE_MAX = 16000
MAX_TEXT = 10_000_000


@dataclass
class PeerRequest:
    prompt: str
    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    json_schema: dict | None = None
    tools: str = "none"
    timeout: float = 600
    session_id: str | None = None
    resume: bool = False
    name: str | None = None
    max_budget_usd: float | None = None
    env_extra: dict[str, str] = field(default_factory=dict)


@dataclass
class PeerResult:
    ok: bool
    status: str
    peer: str
    transport: str
    text: str = ""
    structured: dict | None = None
    model: str | None = None
    session_id: str | None = None
    usage: dict | None = None
    cost_usd: float | None = None
    rc: int = 0
    elapsed_s: float = 0.0
    raw_path: str | None = None
    error: str | None = None

    def to_dispatch(self, role: str = "") -> dict:
        usage = None
        if isinstance(self.usage, dict) or self.cost_usd is not None:
            u = self.usage if isinstance(self.usage, dict) else {}
            usage = {}
            inp, out = u.get("input_tokens"), u.get("output_tokens")
            if isinstance(inp, (int, float)):
                usage["input_tokens"] = inp
            if isinstance(out, (int, float)):
                usage["output_tokens"] = out
            total = u.get("total_tokens")
            if not isinstance(total, (int, float)) and "input_tokens" in usage and "output_tokens" in usage:
                total = usage["input_tokens"] + usage["output_tokens"]
            if isinstance(total, (int, float)):
                usage["total_tokens"] = total
            if self.cost_usd is not None:
                usage["cost_usd"] = self.cost_usd
        return {"ok": bool(self.ok), "output": self.text, "status": self.status, "provider": self.peer,
                "role": role, "rc": self.rc, "elapsed_s": self.elapsed_s, "model": self.model,
                "usage": usage, "session_id": self.session_id, "error": self.error}


# ---------------------------------------------------------------- helpers

def json_object(text: str) -> dict | None:
    """Extract one JSON object from model text (tolerates fences/prose)."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    try:
        val = json.loads(s)
        return val if isinstance(val, dict) else None
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.S)
    if m:
        try:
            val = json.loads(m.group(1))
            if isinstance(val, dict):
                return val
        except ValueError:
            pass
    dec = json.JSONDecoder()
    i = s.find("{")
    while i != -1:
        try:
            val, _ = dec.raw_decode(s[i:])
            if isinstance(val, dict):
                return val
        except ValueError:
            pass
        i = s.find("{", i + 1)
    return None


def _lines(raw: str):
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            yield ev


def render_prompt(transport: str, req: PeerRequest) -> str:
    """Prompt text actually sent (schema is appended where the CLI has no schema flag)."""
    if req.json_schema and transport in ("andrewcode-oneshot", "opencode-run"):
        return (req.prompt.rstrip() + "\n\nReply with ONLY one JSON object matching this JSON schema:\n"
                + json.dumps(req.json_schema, separators=(",", ":"), sort_keys=True))
    return req.prompt


def _sandbox(tools: str) -> str:
    return "workspace-write" if tools == "write" else "read-only"


# ---------------------------------------------------------------- argv

def _build(transport: str, req: PeerRequest, binary: str, flags, prompt_file, schema_file, last_file,
           notes: list[str]) -> list[str]:
    if req.tools not in TOOLS:
        raise PeerError("invalid_request", f"tools must be one of {TOOLS}")
    argv: list[str] = [binary]

    def opt(flag: str, *vals: str) -> None:
        if flags is not None and flag.startswith("--") and flag not in flags:
            notes.append(f"dropped unsupported flag {flag}")
            return
        argv.append(flag)
        argv.extend(vals)

    session = bool(req.session_id or req.resume)
    if transport == "claude-print":
        argv += ["-p", "--output-format", "json"]
        if not session:
            opt("--no-session-persistence")
        if req.model:
            argv += ["--model", req.model]
        if req.json_schema:
            opt("--json-schema", json.dumps(req.json_schema, separators=(",", ":"), sort_keys=True))
        if req.max_budget_usd is not None:
            opt("--max-budget-usd", str(req.max_budget_usd))
        if req.resume and req.session_id:
            argv += ["--resume", req.session_id]
        elif req.session_id:
            argv += ["--session-id", req.session_id]
        if req.tools == "none":
            argv += ["--permission-mode", "dontAsk", "--disallowedTools", CLAUDE_DENY]
        elif req.tools == "read":
            argv += ["--permission-mode", "dontAsk", "--allowedTools", "Read,Grep,Glob"]
        else:
            argv += ["--permission-mode", "acceptEdits"]
    elif transport == "andrewcode-oneshot":
        if req.tools == "read":
            raise PeerError("invalid_request", "andrewcode has no sandbox; tools='read' cannot be guaranteed (J-09)")
        if not prompt_file:
            raise ValueError("andrewcode-oneshot requires prompt_file")
        if req.model:
            argv += ["-m", req.model]
        if req.effort:
            opt("--thinking-effort", req.effort)
        argv += ["--prompt-file", prompt_file, "--output-format", "stream-json"]
        if req.tools == "none":
            argv.append("--disable-tools")
        else:
            argv.append("--auto")
        if session:
            if req.session_id:
                argv += ["-S", req.session_id]
        else:
            opt("--ephemeral")
    elif transport == "codex-exec":
        if req.resume:
            raise PeerError("invalid_request", "codex-exec one-shot has no resume; use codex-acp")
        opt("--no-daemon")
        argv += ["exec", "--json"]
        opt("--skip-git-repo-check")
        opt("--ephemeral")
        opt("--color", "never")
        argv += ["-s", _sandbox(req.tools), "-C", req.cwd or os.getcwd()]
        if req.model:
            argv += ["-m", req.model]
        if req.effort:
            argv += ["-c", f'model_reasoning_effort="{req.effort}"']
        if req.json_schema and schema_file:
            opt("--output-schema", schema_file)
        if last_file:
            argv += ["-o", last_file]
        argv.append("-")
    elif transport == "opencode-run":
        argv.append("run")
        if req.model:
            argv += ["-m", req.model]
        argv += ["--format", "json", "--dir", req.cwd or os.getcwd()]
        text = render_prompt(transport, req)
        if len(text.encode("utf-8")) > OPENCODE_INLINE_MAX:
            if not prompt_file:
                raise ValueError("opencode-run needs prompt_file for prompts over 16000 bytes")
            argv.append(f"Read the task in {prompt_file} and follow it exactly.")
        else:
            argv.append(text)
    else:
        raise PeerError("invalid_request", f"unsupported one-shot transport {transport!r}")
    return argv


def build_argv(transport: str, req: PeerRequest, *, binary: str, flags: set[str] | None = None,
               prompt_file: str | None = None, schema_file: str | None = None,
               last_file: str | None = None) -> list[str]:
    return _build(transport, req, binary, flags, prompt_file, schema_file, last_file, [])


# ---------------------------------------------------------------- parsers

def _parse_claude(stdout: str) -> dict:
    data = None
    s = (stdout or "").strip()
    try:
        data = json.loads(s)
    except ValueError:
        data = None
        for ev in _lines(s):
            if ev.get("type") == "result":
                data = ev
    if isinstance(data, list):
        data = next((x for x in reversed(data) if isinstance(x, dict) and x.get("type") == "result"), None)
    if not isinstance(data, dict):
        return {"ok": False, "error": "unparseable claude output"}
    text = data.get("result")
    text = text if isinstance(text, str) else ""
    structured = data.get("structured_output")
    models = data.get("model_usage") or data.get("modelUsage")
    out = {
        "ok": not bool(data.get("is_error")),
        "text": text,
        "structured": structured if isinstance(structured, dict) else None,
        "session_id": data.get("session_id") if isinstance(data.get("session_id"), str) else None,
        "usage": data.get("usage") if isinstance(data.get("usage"), dict) else None,
        "cost_usd": data.get("total_cost_usd") if isinstance(data.get("total_cost_usd"), (int, float)) else None,
        "model": next(iter(models)) if isinstance(models, dict) and models else None,
    }
    if data.get("is_error"):
        out["error"] = f"claude reported an error ({data.get('subtype') or data.get('terminal_reason') or 'is_error'})"
    return out


def _parse_andrewcode(stdout: str) -> dict:
    final, session, failed = "", None, False
    for ev in _lines(stdout):
        role, kind = ev.get("role"), ev.get("type")
        if role == "error" or kind in ("error", "turn.failed", "turn.error"):
            failed = True
        if role in ("meta", "system") or (isinstance(kind, str) and kind.startswith("system")):
            for key in ("session_id", "sessionId", "session"):
                if isinstance(ev.get(key), str):
                    session = ev[key]
        if role == "assistant" and not ev.get("tool_calls"):
            content = ev.get("content")
            if isinstance(content, str) and content.strip():
                final = content
            elif isinstance(content, list):
                joined = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
                if joined.strip():
                    final = joined
    out = {"ok": bool(final.strip()) and not failed, "text": final, "session_id": session}
    if failed:
        out["error"] = "andrewcode stream reported an error or failed turn"  # never echo provider payloads
    elif not final.strip():
        out["error"] = "andrewcode produced no final assistant message"
    return out


def _parse_codex(stdout: str, last_file_text: str | None) -> dict:
    session, usage, text, failed, err = None, None, "", False, None
    for ev in _lines(stdout):
        kind = ev.get("type")
        if kind == "thread.started" and isinstance(ev.get("thread_id"), str):
            session = ev["thread_id"]
        elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        elif kind == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                text = item["text"]
        elif kind in ("turn.failed", "error"):
            failed = True
            e = ev.get("error")
            msg = e.get("message") if isinstance(e, dict) else ev.get("message")
            err = f"codex {kind}: {str(msg)[:200]}" if msg else f"codex {kind}"
    if not text and last_file_text and last_file_text.strip():
        text = last_file_text.strip()
    out = {"ok": bool(text.strip()) and not failed, "text": text, "session_id": session, "usage": usage}
    if err:
        out["error"] = err
    elif not text.strip():
        out["error"] = "codex produced no agent_message"
    return out


def _opencode_text_events(events):
    parts = []  # (message_id, text)
    session = None
    for ev in events:
        if isinstance(ev.get("sessionID"), str):
            session = ev["sessionID"]
        if ev.get("type") != "text":
            continue
        part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
        text = part.get("text")
        if isinstance(text, str):
            parts.append((part.get("messageID"), text))
        if isinstance(part.get("sessionID"), str):
            session = part["sessionID"]
    return parts, session


def _parse_opencode(stdout: str) -> dict:
    s = (stdout or "").strip()
    events = list(_lines(s))
    if not events:
        try:
            data = json.loads(s)
            events = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else (
                [data] if isinstance(data, dict) else [])
        except ValueError:
            events = []
    parts, session = _opencode_text_events(events)
    text = ""
    if parts:
        last_id = parts[-1][0]
        chosen = [t for mid, t in parts if mid == last_id] if last_id else [t for _, t in parts]
        text = "".join(chosen)
    out = {"ok": bool(text.strip()), "text": text, "session_id": session}
    if not text.strip():
        out["error"] = "opencode produced no text parts"
    return out


def parse(transport: str, stdout: str, *, last_file_text: str | None = None) -> dict:
    if transport == "claude-print":
        return _parse_claude(stdout)
    if transport == "andrewcode-oneshot":
        return _parse_andrewcode(stdout)
    if transport == "codex-exec":
        return _parse_codex(stdout, last_file_text)
    if transport == "opencode-run":
        return _parse_opencode(stdout)
    raise PeerError("invalid_request", f"cannot parse transport {transport!r}")


# ---------------------------------------------------------------- feature flags

_FLAG_RE = re.compile(r"(?<![\w-])(--[a-zA-Z][a-zA-Z0-9-]*)")


def _fusion_home(env: Mapping[str, str]) -> Path:
    return Path(env.get("FUSION_HOME") or (Path.home() / ".fusion"))


def _resolve(binary: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(binary, path=env.get("PATH"))


def _run_help(argv: list[str], env, runner: Runner | None) -> str:
    try:
        if runner is None:
            rc, out, err = default_runner(argv, cwd=None, env=dict(env), input=None, timeout=20, cancel=None)
        else:
            rc, out, err = runner(argv, cwd=None, env=dict(env), input=None, timeout=20, cancel=None)
    except (PeerError, OSError):
        return ""
    return (out or "") + "\n" + (err or "")


def feature_flags(binary: str, *, runner: Runner | None = None, env: Mapping[str, str] | None = None) -> set[str]:
    """`--long` options the CLI advertises. Cached by binary identity -> sha256(help) in $FUSION_HOME/cache."""
    e = os.environ if env is None else env
    if is_codex_binary(binary) and not codex_exec_allowed(e):
        raise PeerError("peer_not_allowed", "codex help probe requires FUSION_ALLOW_CODEX_EXEC=1 or a codex host")
    path = _resolve(binary, e)
    ident = None
    if path:
        try:
            st = os.stat(path)
            ident = f"{os.path.realpath(path)}|{st.st_size}|{st.st_mtime_ns}"
        except OSError:
            ident = None
    cache_file = _fusion_home(e) / "cache" / "cli-help.json"
    cache: dict = {}
    try:
        cache = json.loads(cache_file.read_text("utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    idents, hashes = cache.setdefault("by_binary", {}), cache.setdefault("by_help", {})
    if ident and ident in idents and idents[ident] in hashes:
        return set(hashes[idents[ident]])
    texts = [_run_help([binary, "--help"], e, runner)]
    if binary and os.path.basename(binary).lower().startswith("codex"):
        texts.append(_run_help([binary, "exec", "--help"], e, runner))
    help_text = "\n".join(texts)
    digest = hashlib.sha256(help_text.encode("utf-8", "replace")).hexdigest()
    found = set(_FLAG_RE.findall(help_text))
    if help_text.strip():
        hashes[digest] = sorted(found)
        if ident:
            idents[ident] = digest
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, sort_keys=True), "utf-8")
            os.replace(tmp, cache_file)
        except OSError:
            pass
    return found


# ---------------------------------------------------------------- default runner

def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def default_runner(argv, *, cwd, env, input, timeout, cancel):
    """Spawn in its own process group; kill the whole group on timeout/cancel."""
    assert_exec_allowed(argv[0], env)  # gate right before Popen
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    unregister = None
    reg = getattr(cancel, "register", None)
    if callable(reg):
        try:
            unregister = reg(proc, None)
        except Exception:
            unregister = None
    bufs: dict[str, bytes] = {"out": b"", "err": b""}

    def pump(stream, key):
        try:
            data = stream.read()
            bufs[key] = data[:MAX_TEXT]
        except (OSError, ValueError):
            pass

    def feed():
        try:
            proc.stdin.write(input.encode("utf-8"))
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    if input is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout if timeout else None
    outcome = None
    try:
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                outcome = "cancelled"
                break
            if deadline is not None and time.monotonic() >= deadline:
                outcome = "timeout"
                break
    finally:
        if outcome:
            _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            _kill_group(proc)
        for t in threads:
            t.join(timeout=2)
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass
        if callable(unregister):
            try:
                unregister()
            except Exception:
                pass
    if outcome:
        raise PeerError(outcome, f"peer process {outcome}")
    return proc.returncode, bufs["out"].decode("utf-8", "replace"), bufs["err"].decode("utf-8", "replace")


# ---------------------------------------------------------------- run

def _fail(peer, transport, status, error, rc=0, elapsed=0.0, model=None) -> PeerResult:
    return PeerResult(ok=False, status=status, peer=peer, transport=transport, text="", structured=None,
                      model=model, session_id=None, usage=None, cost_usd=None, rc=rc, elapsed_s=elapsed,
                      raw_path=None, error=error)


def run(peer: str, req: PeerRequest, *, env: Mapping[str, str] | None = None, runner: Runner | None = None,
        cancel=None, probe_flags: bool | None = None) -> PeerResult:
    base = dict(os.environ if env is None else env)
    rt = require(route(peer, env=base))
    transport = rt.transport
    if transport not in ("claude-print", "andrewcode-oneshot", "codex-exec", "opencode-run"):
        raise PeerError("invalid_request", f"{transport} is not a one-shot transport")
    if not isinstance(req.prompt, str) or not req.prompt.strip():
        raise PeerError("invalid_request", "prompt is required")
    if len(req.prompt) > MAX_TEXT:
        raise PeerError("invalid_request", "prompt too large")
    name = req.name or _identity.agent_name(base) or f"{rt.peer}-peer"
    child = _identity.child_env(base, name)
    child.update({k: str(v) for k, v in (req.env_extra or {}).items() if not str(k).startswith("board.")})
    model = req.model or rt.default_model
    req = PeerRequest(**{**req.__dict__, "model": model})
    flags = None
    if probe_flags is None:
        probe_flags = runner is None
    if probe_flags:
        flags = feature_flags(rt.binary, runner=runner, env=base) or None
    started = time.monotonic()
    tmp = tempfile.mkdtemp(prefix="fusion-peer-")
    try:
        cwd = req.cwd
        if cwd is None and (transport == "andrewcode-oneshot" and req.tools == "none"):
            cwd = tmp
        prompt = render_prompt(transport, req)
        prompt_file = schema_file = last_file = None
        task_file = None
        if transport == "andrewcode-oneshot":
            prompt_file = os.path.join(tmp, "prompt.md")
            Path(prompt_file).write_text(prompt, "utf-8")
        if transport == "codex-exec":
            last_file = os.path.join(tmp, "last.txt")
            if req.json_schema:
                schema_file = os.path.join(tmp, "schema.json")
                Path(schema_file).write_text(json.dumps(req.json_schema), "utf-8")
            req = PeerRequest(**{**req.__dict__, "cwd": req.cwd or tmp})
            cwd = req.cwd
        if transport == "opencode-run":
            cwd = req.cwd or tmp
            req = PeerRequest(**{**req.__dict__, "cwd": cwd})
            if len(prompt.encode("utf-8")) > OPENCODE_INLINE_MAX:
                task_file = os.path.join(cwd, ".fusion-task.md")
                Path(task_file).write_text(prompt, "utf-8")
                prompt_file = task_file
        notes: list[str] = []
        argv = _build(transport, req, rt.binary, flags, prompt_file, schema_file, last_file, notes)
        stdin = prompt if transport in ("claude-print", "codex-exec") else None
        assert_exec_allowed(argv[0], base)  # defence in depth before any runner/Popen
        call = runner or default_runner
        try:
            rc, out, _err = call(argv, cwd=cwd, env=child, input=stdin, timeout=req.timeout, cancel=cancel)
        except PeerError as exc:
            if exc.code in ("timeout", "cancelled"):
                status = exc.code
                return _fail(peer, transport, status, "; ".join(notes + [status]) or status,
                             elapsed=round(time.monotonic() - started, 3), model=model)
            raise
        except FileNotFoundError:
            return _fail(peer, transport, "absent", f"{rt.binary} not found on PATH", rc=127,
                         elapsed=round(time.monotonic() - started, 3), model=model)
        elapsed = round(time.monotonic() - started, 3)
        if rc == 127:
            return _fail(peer, transport, "absent", f"{rt.binary} not found", rc=rc, elapsed=elapsed, model=model)
        last_text = None
        if last_file and os.path.exists(last_file):
            try:
                last_text = Path(last_file).read_text("utf-8")
            except OSError:
                last_text = None
        try:
            parsed = parse(transport, out, last_file_text=last_text)
        except (PeerError, ValueError) as exc:
            parsed = {"ok": False, "error": f"unparseable output: {exc}"}
        text = parsed.get("text") or ""
        structured = parsed.get("structured")
        if structured is None and req.json_schema and text:
            structured = json_object(text)
        ok = bool(parsed.get("ok")) and rc == 0 and bool(text.strip())
        status = "returned" if ok else "error"
        errors = list(notes)
        if parsed.get("error"):
            errors.append(parsed["error"])
        if rc != 0:
            errors.append(f"exit code {rc}")
        raw_path = None
        if base.get("FUSION_PEERS_KEEP_RAW") == "1":
            try:
                d = _fusion_home(base) / "peers" / "raw"
                d.mkdir(parents=True, exist_ok=True)
                p = d / f"{int(time.time() * 1000)}-{peer}.jsonl"
                p.write_text(out, "utf-8")
                raw_path = str(p)
            except OSError:
                raw_path = None
        return PeerResult(ok=ok, status=status, peer=peer, transport=transport, text=text, structured=structured,
                          model=parsed.get("model") or model, session_id=parsed.get("session_id"),
                          usage=parsed.get("usage"), cost_usd=parsed.get("cost_usd"), rc=rc, elapsed_s=elapsed,
                          raw_path=raw_path, error="; ".join(errors) or None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def as_dispatch_fn(*, env=None, runner=None, default_tools: str = "none") -> Callable:
    """0.7-compatible dispatch function; never raises for provider failure."""

    def dispatch(provider: str, prompt: str, *, role: str = "", cwd: str | None = None,
                 timeout: float | None = 300, model: str = "", effort: str = "", cancel=None) -> dict:
        if not isinstance(provider, str) or not isinstance(prompt, str):
            raise TypeError("provider and prompt must be strings")
        req = PeerRequest(prompt=prompt, model=model or None, effort=effort or None, cwd=cwd,
                          tools=default_tools, timeout=timeout or 300)
        try:
            res = run(provider, req, env=env, runner=runner, cancel=cancel)
        except PeerError as exc:
            status = {"peer_not_allowed": "blocked", "peer_absent": "absent", "timeout": "timeout",
                      "cancelled": "cancelled"}.get(exc.code, "error")
            if exc.code == "invalid_request" and provider not in ("claude", "codex", "andrewcode", "opencode"):
                status = "absent"
            res = _fail(provider, "", status, str(exc))
        return res.to_dispatch(role)

    return dispatch
