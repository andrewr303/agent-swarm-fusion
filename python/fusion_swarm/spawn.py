"""Nested AndrewCode spawn — real processes, injectable runner, fail-closed budget.

Hive children are REAL AndrewCode processes, not simulated roles. This module is
the only spawn path for nested workers:

    node C:/dev/andrew-code/apps/kimi-code/dist/main.mjs -m <alias>
         --thinking-effort <exact-effort> --prompt-file <UTF8-file> --output-format stream-json

Honesty rules (same as the rest of fusion_swarm):

  * Real dispatch only. Tests inject a ``runner`` callable; they never require
    andrewcode to be installed and they never make live paid CLI calls.
  * Absent ≠ agreement. A missing binary is ``status="absent"`` (rc 127), a
    timeout is ``status="timeout"`` (rc 124). Callers must not treat either as
    a vote.
  * Stdlib only. Git Bash is resolved the same way as ``adapter._find_bash``
    (never WSL's System32 bash).
  * ``SpawnLimits`` are enforced BEFORE any subprocess. Refusal raises
    :class:`SpawnError` (fail-closed).

Every prompt is written as UTF-8 beside its output; argv never carries the
body. Tool-enabled implementation workers use Node directly. This path is NOT
sandboxed, tools-disabled or ephemeral, and must not be used for reviews that
require those controls. Restricted compatibility adapters fail closed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

from .andrewcode_cli import ENTRY as DEFAULT_ANDREWCODE

FUSION_ROOT = Path(__file__).resolve().parents[2]          # .../fusion
PYTHON_ROOT = Path(__file__).resolve().parents[1]          # .../fusion/python
ANDREWCODE_SH = FUSION_ROOT / "scripts" / "providers" / "andrewcode.sh"

# Legacy threshold retained for callers/tests; all prompt sizes now use files.
PROMPT_INLINE_MAX = 4000

# Env keys injected into every nested process (hive contract).
ENV_BOARD = "FUSION_BOARD"
ENV_AGENT_ID = "FUSION_AGENT_ID"
ENV_PARENT_ID = "FUSION_PARENT_ID"
ENV_SWARM_ID = "FUSION_SWARM_ID"
ENV_PYTHONPATH = "PYTHONPATH"


# --- identities (sibling slice). Fallback keeps this module independently testable.

try:
    from .identities import AgentIdentity as _AgentIdentity
    from .identities import SpawnLimits as _ImportedSpawnLimits
except Exception:  # pragma: no cover - identities slice may land in parallel
    _AgentIdentity = None
    _ImportedSpawnLimits = None


class SpawnError(ValueError):
    """Fail-closed spawn refusal (budget, depth, missing dispatch, …)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


if _ImportedSpawnLimits is not None:
    SpawnLimits = _ImportedSpawnLimits
else:  # pragma: no cover - used only when identities.py is absent
    @dataclass
    class SpawnLimits:
        max_depth: int = 2
        max_children_per_agent: int = 4
        max_agents: int = 24

        def can_spawn(self, parent, current_total, current_children):
            if current_total >= self.max_agents:
                return False, f"max_agents={self.max_agents}"
            if current_children >= self.max_children_per_agent:
                return False, (
                    f"max_children_per_agent={self.max_children_per_agent}"
                )
            depth = _field(parent, "depth", 0)
            max_depth = _field(parent, "max_depth", self.max_depth) or self.max_depth
            if int(depth or 0) >= int(max_depth or 0):
                return False, f"max_depth={max_depth}"
            return True, "ok"


@dataclass
class SpawnHandle:
    """Result of one spawn attempt. ``status`` mirrors adapter.Reply taxonomy."""
    agent_id: str
    provider: str
    model: str
    pid: int
    out_path: str
    log_path: str
    status: str          # returned | absent | timeout | error
    returncode: Optional[int] = None

    def dict(self) -> dict:
        return asdict(self)


# --- helpers -----------------------------------------------------------------

def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute from an AgentIdentity, a dict, or a namespace."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fs(p) -> str:
    """Forward-slash a path so Git Bash handles it in redirections/args."""
    return str(p).replace("\\", "/")


def _find_bash() -> str:
    """Resolve GIT BASH explicitly. Copied from adapter._find_bash: on Windows
    a bare 'bash' resolves to WSL (System32\\bash.exe), whose Linux PATH can't
    see the Windows CLIs — so we must pin Git Bash."""
    cand = os.environ.get("FUSION_BASH")
    if cand and Path(cand).exists():
        return cand
    for p in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(p).exists():
            return p
    w = shutil.which("bash")
    if w and "system32" not in w.lower():
        return w
    return "bash"


def _andrewcode_binary() -> str:
    """Resolve Node directly. Legacy FUSION_ANDREWCODE may name Node only.

    A .cmd/standalone CLI override is refused by the exact command builder.
    """
    return (os.environ.get("FUSION_ANDREWCODE") or shutil.which("node") or "node").strip()


def _as_identity(obj: Any, *, fallback_role: str = "worker") -> Any:
    """Coerce a dict/namespace into AgentIdentity when the identities
    module is present. Hive always passes AgentIdentity; tests may pass a
    SimpleNamespace or dict with the same fields."""
    if obj is None:
        return None
    if _AgentIdentity is not None and isinstance(obj, _AgentIdentity):
        return obj
    if _AgentIdentity is None:
        return obj
    src = obj if isinstance(obj, dict) else None
    data = {
        "id": str(_field(obj, "id", "parent") or (src or {}).get("id") or "parent"),
        "display_name": str(
            _field(obj, "display_name", None)
            or _field(obj, "id", "parent")
            or "parent"
        ),
        "role": _field(obj, "role", fallback_role) or fallback_role,
        "provider": str(_field(obj, "provider", "andrewcode") or "andrewcode"),
        "model": str(_field(obj, "model", "unknown") or "unknown"),
        "parent_id": _field(obj, "parent_id", None),
        "lineage": _field(obj, "lineage", "") or "",
        "depth": int(_field(obj, "depth", 0) or 0),
        "spawn_budget": int(_field(obj, "spawn_budget", 0) or 0),
        "max_depth": int(_field(obj, "max_depth", 2) or 2),
        "status": _field(obj, "status", "online") or "online",
    }
    if data["role"] not in ("architect", "operator", "captain", "worker", "child"):
        data["role"] = fallback_role
    try:
        return _AgentIdentity(**{
            k: data[k] for k in (
                "id", "display_name", "role", "provider", "model",
                "parent_id", "lineage", "depth", "spawn_budget",
                "max_depth", "status",
            ) if k in data
        })
    except Exception:
        # Identity validation is fail-closed in identities.py; if the
        # *spawned* object is illegal we still refuse at the budget gate
        # rather than crashing with IdentityError for a dict test fixture.
        return obj


def _spawn_parent(identity: Any) -> Any:
    """Parent passed to ``SpawnLimits.can_spawn``: one level above the
    identity being created. A depth-2 child with max_depth=2 is allowed
    (parent depth 1); a depth-3 child is not (parent depth 2 >= 2)."""
    depth = int(_field(identity, "depth", 0) or 0)
    parent_depth = max(0, depth - 1)
    max_depth = int(_field(identity, "max_depth", 2) or 2)
    parent_id = _field(identity, "parent_id", None) or "parent"
    lineage = _field(identity, "lineage", "") or ""
    parent_lineage = (
        lineage.rsplit("/", 1)[0] if lineage and "/" in lineage else str(parent_id)
    )
    parent_role = "architect" if parent_depth == 0 else (
        "captain" if parent_depth == 1 else "worker"
    )
    raw = {
        "id": str(parent_id),
        "display_name": str(parent_id),
        "role": parent_role,
        "provider": str(_field(identity, "provider", "andrewcode") or "andrewcode"),
        "model": str(_field(identity, "model", "unknown") or "unknown"),
        "parent_id": None if parent_depth == 0 else "architect",
        "lineage": parent_lineage,
        "depth": parent_depth,
        "spawn_budget": int(_field(identity, "spawn_budget", 1) or 1),
        "max_depth": max_depth,
        "status": "online",
    }
    return _as_identity(raw, fallback_role=parent_role)


def _enforce_limits(
    identity: Any,
    limits: Optional[SpawnLimits],
    current_total: int,
    current_children: int,
    parent: Any = None,
) -> None:
    """Fail-closed pre-spawn gate. ``SpawnLimits.can_spawn`` requires an
    AgentIdentity parent; we coerce dicts/namespaces so tests and hive
    both work. Extra child-depth check covers callers that pass the child
    rather than the parent."""
    limits = limits or SpawnLimits()
    parent = parent if parent is not None else _spawn_parent(identity)
    parent = _as_identity(parent, fallback_role="captain")
    ok, reason = limits.can_spawn(parent, current_total, current_children)
    if not ok:
        raise SpawnError(reason or "spawn refused by SpawnLimits")
    child_depth = int(_field(identity, "depth", 0) or 0)
    max_depth = int(getattr(limits, "max_depth", 2) or 2)
    ident_max = _field(identity, "max_depth", None)
    if ident_max is not None:
        max_depth = min(max_depth, int(ident_max))
    if child_depth > max_depth:
        raise SpawnError(f"depth={child_depth} exceeds max_depth={max_depth}")


def _pythonpath(fusion_pythonpath: Optional[str]) -> str:
    extra = fusion_pythonpath if fusion_pythonpath is not None else str(PYTHON_ROOT)
    existing = os.environ.get(ENV_PYTHONPATH, "")
    parts: list[str] = []
    for p in (extra, existing):
        if not p:
            continue
        for chunk in str(p).split(os.pathsep):
            if chunk and chunk not in parts:
                parts.append(chunk)
    return os.pathsep.join(parts)


def _inject_env(
    identity: Any,
    board_path,
    extra_env: Optional[dict],
    fusion_pythonpath: Optional[str],
    swarm_id: Optional[str],
) -> dict[str, str]:
    env = os.environ.copy()
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    agent_id = str(_field(identity, "id", "") or "")
    parent_id = _field(identity, "parent_id", None)
    sid = (
        swarm_id
        or (extra_env or {}).get(ENV_SWARM_ID)
        or env.get(ENV_SWARM_ID)
        or Path(str(board_path)).stem
    )
    # Required hive keys win over extra_env so a caller cannot accidentally
    # drop the identity of the process they just spawned.
    env[ENV_BOARD] = _fs(board_path)
    env[ENV_AGENT_ID] = agent_id
    env[ENV_PARENT_ID] = "" if parent_id is None else str(parent_id)
    env[ENV_SWARM_ID] = str(sid)
    env[ENV_PYTHONPATH] = _pythonpath(fusion_pythonpath)
    return env


def _paths(identity: Any, workdir, out_path, log_path) -> tuple[str, str]:
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    agent_id = str(_field(identity, "id", "agent") or "agent")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in agent_id)
    out = Path(out_path) if out_path else wd / f"{safe}.out"
    log = Path(log_path) if log_path else wd / f"{safe}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    return str(out), str(log)


def _status_for(returncode: Optional[int], timed_out: bool, absent: bool) -> str:
    if timed_out:
        return "timeout"
    if absent or returncode == 127:
        return "absent"
    if returncode == 124:
        return "timeout"
    if returncode == 0:
        return "returned"
    return "error"


def _result_ns(result: Any) -> Any:
    if result is None:
        return SimpleNamespace(pid=0, returncode=1, timed_out=False, absent=False)
    if isinstance(result, SpawnHandle):
        return result
    return result


def _handle_from_result(
    identity: Any,
    out_path: str,
    log_path: str,
    result: Any,
) -> SpawnHandle:
    if isinstance(result, SpawnHandle):
        return result
    ns = _result_ns(result)
    timed_out = bool(getattr(ns, "timed_out", False) or getattr(ns, "status", "") == "timeout")
    absent = bool(getattr(ns, "absent", False) or getattr(ns, "status", "") == "absent")
    rc = getattr(ns, "returncode", None)
    status = getattr(ns, "status", None) or _status_for(rc, timed_out, absent)
    pid = int(getattr(ns, "pid", 0) or 0)
    return SpawnHandle(
        agent_id=str(_field(identity, "id", "") or ""),
        provider=str(_field(identity, "provider", "andrewcode") or "andrewcode"),
        model=str(_field(identity, "model", "") or ""),
        pid=pid,
        out_path=out_path,
        log_path=log_path,
        status=status,
        returncode=rc if rc is not None else (124 if timed_out else (127 if absent else None)),
    )


def _subprocess_runner(
    argv: list[str],
    *,
    env: dict,
    cwd: str,
    timeout: Optional[float],
    out_path: str,
    log_path: str,
) -> SimpleNamespace:
    """Default runner. Never called by unit tests (they inject ``runner``)."""
    try:
        with open(out_path, "w", encoding="utf-8") as out, \
                open(log_path, "w", encoding="utf-8") as log:
            from .process_runtime import process_identity, terminate_process_group
            proc = subprocess.Popen(
                argv,
                env=env,
                cwd=cwd or None,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            child_identity = process_identity(proc.pid) if proc.pid else None
            try:
                rc = proc.wait(timeout=timeout)
                if rc == 0:
                    from .andrewcode_cli import final_text
                    raw = Path(out_path).read_text(encoding="utf-8")
                    Path(out_path + ".raw").write_text(raw, encoding="utf-8")
                    try:
                        text = final_text(raw)
                    except ValueError as exc:
                        log.write(str(exc) + "\n")
                        text, rc = "", 1
                    out.seek(0)
                    out.truncate()
                    out.write(text)
                return SimpleNamespace(
                    pid=proc.pid or 0, returncode=rc,
                    timed_out=False, absent=False,
                )
            except subprocess.TimeoutExpired:
                terminate_process_group(proc, child_identity)
                return SimpleNamespace(
                    pid=proc.pid or 0, returncode=124,
                    timed_out=True, absent=False,
                )
    except FileNotFoundError:
        return SimpleNamespace(pid=0, returncode=127, timed_out=False, absent=True)
    except subprocess.TimeoutExpired:
        return SimpleNamespace(pid=0, returncode=124, timed_out=True, absent=False)


def _invoke_runner(
    runner: Callable,
    argv: list[str],
    env: dict,
    cwd: str,
    timeout: Optional[float],
    out_path: str,
    log_path: str,
) -> Any:
    try:
        return runner(
            argv, env=env, cwd=cwd, timeout=timeout,
            out_path=out_path, log_path=log_path,
        )
    except TypeError:
        try:
            return runner(argv, env=env, cwd=cwd, timeout=timeout)
        except TypeError:
            return runner(argv)


def _build_direct_argv(binary: str, prompt_file: str, model: str, effort: str = "xhigh") -> list[str]:
    """Direct Node one-shot for authorized tool-enabled implementation workers.

    This is not a sandbox, tools-disabled review profile or ephemeral session.
    """
    from .andrewcode_cli import command
    return command(model, effort, prompt_file, node=binary)


def _build_script_argv(
    bash: str, prompt_file: str, out_path: str, model: str,
) -> list[str]:
    """Huge-prompt fallback: Git Bash + scripts/providers/andrewcode.sh.

    ``andrewcode.sh`` reads the prompt from a file so the Windows command
    line never carries the body. Documented fallback when ``-p`` would
    overflow ``PROMPT_INLINE_MAX``.
    """
    return [bash, _fs(ANDREWCODE_SH), _fs(prompt_file), _fs(out_path), model]


def build_argv(
    prompt: str,
    model: str,
    *,
    out_path: str = "",
    prompt_file: Optional[str] = None,
    binary: Optional[str] = None,
    bash: Optional[str] = None,
    effort: str = "xhigh",
) -> list[str]:
    """Build direct Node argv; all prompts require a UTF-8 file.

    Legacy binary/bash parameters remain for caller compatibility. ``binary``
    now means a Node executable, never an AndrewCode .cmd shim.
    """
    if not prompt_file:
        raise SpawnError("a UTF-8 prompt_file is required for AndrewCode dispatch")
    from .andrewcode_cli import DEFAULT_MODEL
    return _build_direct_argv(binary or _andrewcode_binary(), prompt_file,
                              model or DEFAULT_MODEL, effort)


# --- public spawn API --------------------------------------------------------

def spawn_andrewcode(
    identity,
    prompt: str,
    board_path,
    workdir,
    extra_env: Optional[dict] = None,
    timeout: Optional[float] = None,
    fusion_pythonpath: Optional[str] = None,
    runner: Optional[Callable] = None,
    limits: Optional[SpawnLimits] = None,
    current_total: int = 0,
    current_children: int = 0,
    parent: Any = None,
    swarm_id: Optional[str] = None,
    out_path: Optional[str] = None,
    log_path: Optional[str] = None,
) -> SpawnHandle:
    """Spawn one nested AndrewCode process for ``identity``.

    ``runner`` is an injectable callable::

        runner(argv, *, env, cwd, timeout, out_path, log_path) -> ns|SpawnHandle

    returning an object with ``pid``, ``returncode``, and optionally
    ``timed_out`` / ``absent`` / ``status``. Unit tests MUST pass a runner;
    the default shells out for real.
    """
    _enforce_limits(identity, limits, current_total, current_children, parent=parent)
    workdir = str(workdir)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    out_path, log_path = _paths(identity, workdir, out_path, log_path)
    env = _inject_env(identity, board_path, extra_env, fusion_pythonpath, swarm_id)
    model = str(_field(identity, "model", "") or "")
    prompt = prompt if prompt is not None else ""

    prompt_file = out_path + ".prompt.txt"
    Path(prompt_file).write_text(prompt, encoding="utf-8")
    argv = build_argv(
        prompt, model,
        out_path=out_path,
        prompt_file=prompt_file,
        effort=str(_field(identity, "effort", "") or env.get("FUSION_ANDREWCODE_EFFORT") or "xhigh"),
    )

    run = runner or _subprocess_runner
    try:
        result = _invoke_runner(run, argv, env, workdir, timeout, out_path, log_path)
    except subprocess.TimeoutExpired:
        result = SimpleNamespace(pid=0, returncode=124, timed_out=True, absent=False)
    except FileNotFoundError:
        result = SimpleNamespace(pid=0, returncode=127, timed_out=False, absent=True)
    return _handle_from_result(identity, out_path, log_path, result)


def spawn_via_adapter(
    provider: str,
    identity,
    prompt: str,
    board_path,
    workdir=None,
    extra_env: Optional[dict] = None,
    timeout: Optional[float] = None,
    fusion_pythonpath: Optional[str] = None,
    dispatch: Optional[Callable] = None,
    limits: Optional[SpawnLimits] = None,
    current_total: int = 0,
    current_children: int = 0,
    parent: Any = None,
    swarm_id: Optional[str] = None,
    model: str = "",
    effort: str = "",
    out_path: Optional[str] = None,
    log_path: Optional[str] = None,
) -> SpawnHandle:
    """Dispatch a captain that is NOT a nested AndrewCode process.

    Calls a passed ``dispatch`` callable (same shape as
    ``fusion_swarm.adapter.dispatch``). The adapter is imported lazily and
    only when ``dispatch`` is omitted, so unit tests never import-cycle-crash
    if adapter is missing — they pass their own callable.
    """
    _enforce_limits(identity, limits, current_total, current_children, parent=parent)
    if dispatch is None:
        try:
            from .adapter import dispatch as _dispatch  # lazy: no cycle at import
        except Exception as e:
            raise SpawnError(f"adapter.dispatch unavailable: {e}") from e
        dispatch = _dispatch

    if workdir is None:
        workdir = tempfile.mkdtemp(prefix="fusion-spawn-")
    Path(workdir).mkdir(parents=True, exist_ok=True)
    out_path, log_path = _paths(identity, workdir, out_path, log_path)
    # Env is still composed so a dispatch implementation that reads FUSION_*
    # (or a wrapper that forwards env) sees the same contract as nested spawn.
    _inject_env(identity, board_path, extra_env, fusion_pythonpath, swarm_id)

    model = model or str(_field(identity, "model", "") or "")
    try:
        reply = dispatch(
            provider, prompt, model=model, effort=effort, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        Path(out_path).write_text("", encoding="utf-8")
        Path(log_path).write_text("timeout", encoding="utf-8")
        return SpawnHandle(
            agent_id=str(_field(identity, "id", "") or ""),
            provider=provider,
            model=model,
            pid=0,
            out_path=out_path,
            log_path=log_path,
            status="timeout",
            returncode=124,
        )
    except Exception as e:
        Path(log_path).write_text(str(e), encoding="utf-8")
        return SpawnHandle(
            agent_id=str(_field(identity, "id", "") or ""),
            provider=provider,
            model=model,
            pid=0,
            out_path=out_path,
            log_path=log_path,
            status="error",
            returncode=1,
        )

    text = getattr(reply, "text", None)
    if text is None and isinstance(reply, dict):
        text = reply.get("text", "")
    status = getattr(reply, "status", None)
    if status is None and isinstance(reply, dict):
        status = reply.get("status")
    status = status or ("returned" if text else "error")
    Path(out_path).write_text("" if text is None else str(text), encoding="utf-8")
    return SpawnHandle(
        agent_id=str(_field(identity, "id", "") or ""),
        provider=provider,
        model=model,
        pid=0,
        out_path=out_path,
        log_path=log_path,
        status=str(status),
        returncode=0 if status == "returned" else (124 if status == "timeout" else (127 if status == "absent" else 1)),
    )


# --- run_provider: fusion.sh dispatch with real cancellation (spec 11.1) -----

FUSION_SH = FUSION_ROOT / "scripts" / "fusion.sh"
DISPATCH_STATUS = {0: "returned", 124: "timeout", 127: "absent", 3: "blocked"}   # other rc -> "error"


class ProcessCancel:
    """Minimal 0.6 cancel token for callers that do not use ``forms``.

    ``set()`` kills every registered process group; a child registered after the
    token is set is killed immediately.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: dict[int, tuple[Any, Optional[str]]] = {}

    def is_set(self) -> bool:
        return self._event.is_set()

    def _kill(self, proc: Any, identity: Optional[str]) -> None:
        from .process_runtime import terminate_process_group
        try:
            terminate_process_group(proc, identity)
        except Exception:
            pass

    def set(self) -> None:
        with self._lock:
            self._event.set()
            procs = list(self._procs.values())
        for proc, identity in procs:
            self._kill(proc, identity)

    def register(self, proc: Any, identity: Optional[str] = None) -> Callable[[], None]:
        key = id(proc)
        with self._lock:
            already = self._event.is_set()
            if not already:
                self._procs[key] = (proc, identity)
        if already:
            self._kill(proc, identity)
            return lambda: None

        def _unregister() -> None:
            with self._lock:
                self._procs.pop(key, None)
        return _unregister


def _resolve_child_host(env: Mapping[str, str]) -> str:
    """FUSION_HOST for the child (J-08): Claude markers win unless explicit."""
    try:
        from .peers.policy import detect_host  # type: ignore
        try:
            host = detect_host(dict(env))
        except TypeError:
            host = detect_host()
        if host:
            return str(host)
    except Exception:
        pass
    if env.get("CLAUDECODE") == "1" and env.get("FUSION_HOST_EXPLICIT") != "1":
        return "claude"
    return str(env.get("FUSION_HOST", "") or "unknown")


def _fusion_sh_path(env: Mapping[str, str]) -> str:
    # Test-only hook (J-13): honoured only with FUSION_TEST_HOOKS=1.
    override = env.get("FUSION_SH_OVERRIDE")
    if override and env.get("FUSION_TEST_HOOKS") == "1":
        return override
    return str(FUSION_SH)


def _read_meta(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run_provider(
    provider: str,
    prompt: str,
    *,
    role: str = "",
    cwd: "str | os.PathLike | None" = None,
    timeout: Optional[float] = 300,
    model: str = "",
    effort: str = "",
    cancel: Any = None,
    mode: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Dispatch one provider through ``fusion.sh dispatch`` and return the 0.7 dict.

    Never raises for provider failure. Cancel-aware: polls every 0.1 s, kills the
    whole process group on cancel (status ``cancelled``, rc 130) or timeout
    (status ``timeout``, rc 124).
    """
    from .process_runtime import process_identity, terminate_process_group

    if not isinstance(provider, str) or not provider:
        raise ValueError("provider must be a non-empty string")
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    if mode:
        try:
            from . import adapter as _adapter
            model = model or _adapter.resolve_model(provider, mode)
            effort = effort or _adapter.resolve_effort(provider, mode)
        except Exception:
            pass

    child_env = dict(env if env is not None else os.environ)
    host = _resolve_child_host(child_env)
    if host and host != "unknown":
        child_env["FUSION_HOST"] = host
    sh = _fusion_sh_path(child_env)

    t0 = time.monotonic()
    result = {
        "ok": False, "output": "", "status": "error", "provider": provider, "role": role,
        "rc": 1, "elapsed_s": 0.0, "model": None, "usage": None, "session_id": None,
    }
    work = tempfile.mkdtemp(prefix="fusion-run-")
    try:
        prompt_file = os.path.join(work, "prompt.txt")
        out_file = os.path.join(work, "out.txt")
        log_file = os.path.join(work, "dispatch.log")
        Path(prompt_file).write_text(prompt, encoding="utf-8")
        argv = [_find_bash(), _fs(sh), "dispatch", provider, _fs(prompt_file), _fs(out_file),
                model or "", effort or "", _fs(cwd) if cwd else ""]
        unregister: Callable[[], None] = lambda: None
        rc = 1
        status = "error"
        try:
            with open(log_file, "wb") as log:
                try:
                    proc = subprocess.Popen(
                        argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        env=child_env,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        start_new_session=os.name != "nt",
                    )
                except FileNotFoundError:
                    rc, status = 127, "absent"
                    proc = None
                except OSError:
                    rc, status = 1, "error"
                    proc = None
                if proc is not None:
                    identity = process_identity(proc.pid) if proc.pid else None
                    if cancel is not None:
                        unregister = cancel.register(proc, identity)
                    deadline = None if timeout is None else t0 + float(timeout)
                    outcome = None
                    while True:
                        try:
                            rc = proc.wait(timeout=0.1)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                        if cancel is not None and cancel.is_set():
                            outcome = "cancelled"
                        elif deadline is not None and time.monotonic() > deadline:
                            outcome = "timeout"
                        if outcome:
                            terminate_process_group(proc, identity)
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                pass
                            break
                    if outcome == "timeout":
                        rc, status = 124, "timeout"
                    elif outcome == "cancelled":
                        rc, status = 130, "cancelled"
                    else:
                        if cancel is not None and cancel.is_set() and (rc < 0 or rc >= 128):
                            rc, status = 130, "cancelled"
                        else:
                            status = DISPATCH_STATUS.get(rc, "error")
                            # the leader may have exited leaving grandchildren
                            if os.name != "nt" and proc.pid:
                                terminate_process_group(proc, identity)
        finally:
            unregister()
        text = ""
        try:
            text = Path(out_file).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        if status == "returned" and not text:
            status = "error"
        meta = _read_meta(out_file + ".meta.json")
        usage = meta.get("usage")
        result.update(
            ok=status == "returned", output=text if status == "returned" else text,
            status=status, rc=rc,
            model=meta.get("model") if isinstance(meta.get("model"), str) else None,
            usage=usage if isinstance(usage, dict) else None,
            session_id=meta.get("session_id") if isinstance(meta.get("session_id"), str) else None,
        )
    except Exception as exc:  # never raise for provider failure
        result.update(status="error", rc=1, ok=False, error=str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        result["elapsed_s"] = round(time.monotonic() - t0, 3)
    return result


__all__ = [
    "ANDREWCODE_SH",
    "DEFAULT_ANDREWCODE",
    "DISPATCH_STATUS",
    "FUSION_SH",
    "ProcessCancel",
    "PROMPT_INLINE_MAX",
    "SpawnError",
    "SpawnHandle",
    "SpawnLimits",
    "build_argv",
    "run_provider",
    "spawn_andrewcode",
    "spawn_via_adapter",
]
