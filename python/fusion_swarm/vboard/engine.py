"""Engine manager: find, start, stop and install a `vb` board engine, and pick a board for `open_board()`.

Judge J-04 / amendment A1.2 (evidence: notes/g1-board-engine-runtime.md). The preferred engine is the real
`@vercel/board` (`vb serve`, pinned 0.54.0): 85/85 applicable upstream conformance checks and ~4,100 completions/min.
Fusion never runs `npx` per command and never installs anything on its own:

    resolve(env)  FUSION_VBOARD_ENGINE path > `vb` on PATH > <FUSION_HOME>/engine/node_modules/@vercel/board/dist/cli.mjs
                  with node >= 22.18 (> `npx -y @vercel/board@0.54.0` only when FUSION_VBOARD_ALLOW_NPX=1)
    start(scope)  `<engine> serve --port <free> --dir <FUSION_HOME>/board/<scope> --no-inspect --no-open` on 127.0.0.1,
                  waits for the discovery document, records pid/url in <FUSION_HOME>/board/<scope>/engine.json
    stop(scope)   SIGTERM (then SIGKILL) the recorded process, only when its discovery answer still matches
    install(yes)  prints the npm command; runs it only with yes=True (a user-run setup step)
    open_board()  explicit url > live engine state file > resolvable engine (started) > Python LocalEngineAdapter

`python -m fusion_swarm.vboard.engine {resolve|status|start|stop|install [--yes]}` is the command line for all of this
(the main CLI forwards `engine ...` here). Stdlib only; no network beyond 127.0.0.1; never prints tokens.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .model import VBoardError
from .port import BoardPort, HttpBoardAdapter, LocalEngineAdapter, _resolve_profile, fusion_home

try:  # POSIX advisory locks; Windows falls back to an O_EXCL lock file
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("fusion_swarm.vboard")

ENGINE_PACKAGE = "@vercel/board@0.54.0"
MIN_NODE = (22, 18)
SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SEMVER = re.compile(r"\d+\.\d+\.\d+\S*")
_CHILDREN: dict[int, subprocess.Popen] = {}


@dataclass(frozen=True)
class EngineSpec:
    argv: tuple[str, ...]       # command prefix; `serve ...` is appended
    source: str                 # "env" | "path" | "installed" | "npx"
    path: str                   # the file that defines the engine (binary or cli.mjs)
    version: str | None = None

    def as_dict(self) -> dict:
        return {"argv": list(self.argv), "source": self.source, "path": self.path, "version": self.version}


# ================================================================================================= resolution
def _env_of(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _which(cmd: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(cmd, path=env.get("PATH") or os.defpath)


def _version_of(argv: Sequence[str], run: Callable[..., Any], env: Mapping[str, str]) -> str | None:
    try:
        proc = run(list(argv), capture_output=True, text=True, timeout=15, env=dict(env))
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = _SEMVER.search((proc.stdout or "") + (proc.stderr or ""))
    return match.group(0) if match else None


def node_ok(node: str, run: Callable[..., Any] = subprocess.run, env: Mapping[str, str] | None = None) -> bool:
    """`node --version` >= 22.18 (the engine needs node:sqlite)."""
    got = _version_of([node, "--version"], run, _env_of(env))
    if not got:
        return False
    parts = got.split(".")
    try:
        return (int(parts[0]), int(parts[1])) >= MIN_NODE
    except (ValueError, IndexError):
        return False


def installed_cli(env: Mapping[str, str] | None = None) -> Path:
    return fusion_home(_env_of(env)) / "engine" / "node_modules" / "@vercel" / "board" / "dist" / "cli.mjs"


def resolve(env: Mapping[str, str] | None = None, *, which: Callable[[str, Mapping[str, str]], str | None] | None = None,
            run: Callable[..., Any] = subprocess.run) -> EngineSpec | None:
    """Find a `vb` engine, or None. Never installs, never runs npx unless FUSION_VBOARD_ALLOW_NPX=1."""
    env = _env_of(env)
    find = which or _which

    def node_script(script: str, source: str) -> EngineSpec | None:
        node = find("node", env)
        if not node or not node_ok(node, run, env):
            return None
        return EngineSpec((node, script), source, script)

    override = env.get("FUSION_VBOARD_ENGINE")
    if override:
        path = os.path.expanduser(override)
        if os.path.isfile(path):
            if path.endswith((".mjs", ".js", ".cjs")):
                spec = node_script(path, "env")
                if spec:
                    return spec
            elif os.access(path, os.X_OK):
                return EngineSpec((path,), "env", path)
        log.warning("FUSION_VBOARD_ENGINE=%r is not usable; trying the other engine locations", override)
    vb = find("vb", env)
    if vb:
        version = _version_of([vb, "--version"], run, env)
        if version:
            return EngineSpec((vb,), "path", vb, version)
    cli = installed_cli(env)
    if cli.is_file():
        spec = node_script(str(cli), "installed")
        if spec:
            return spec
    if env.get("FUSION_VBOARD_ALLOW_NPX") == "1":
        npx = find("npx", env)
        if npx:
            return EngineSpec((npx, "-y", ENGINE_PACKAGE), "npx", npx)
    return None


# ================================================================================================= state file
def board_dir(scope: str, env: Mapping[str, str] | None = None) -> Path:
    if not isinstance(scope, str) or not SCOPE_RE.match(scope):
        raise ValueError(f"scope must match {SCOPE_RE.pattern}: {scope!r}")
    return fusion_home(_env_of(env)) / "board" / scope


def state_path(scope: str = "default", env: Mapping[str, str] | None = None) -> Path:
    return board_dir(scope, env) / "engine.json"


def read_state(scope: str = "default", env: Mapping[str, str] | None = None) -> dict | None:
    try:
        doc = json.loads(state_path(scope, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("url"), str) and isinstance(doc.get("pid"), int) \
        else None


def _write_state(scope: str, doc: Mapping[str, Any], env: Mapping[str, str] | None) -> Path:
    path = state_path(scope, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc))
    os.replace(tmp, path)
    return path


def pid_alive(pid: int) -> bool:
    child = _CHILDREN.get(pid)
    if child is not None:
        return child.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def discovery(url: str, timeout: float = 1.0) -> dict | None:
    """GET <url>/ (unauthenticated discovery document) over plain http.client; None when nothing answers."""
    parts = _split(url)
    if parts is None:
        return None
    host, port, base = parts
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", base + "/", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            return None
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return doc if isinstance(doc, dict) and "version" in doc else None


def _split(url: str) -> tuple[str, int, str] | None:
    import urllib.parse
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or 80, parsed.path.rstrip("/")


def live_state(scope: str = "default", env: Mapping[str, str] | None = None, *,
               http_get: Callable[[str], dict | None] | None = None) -> dict | None:
    """The state-file document when its process is alive AND its discovery endpoint answers; else None."""
    state = read_state(scope, env)
    if state is None or not pid_alive(state["pid"]):
        return None
    return state if (http_get or discovery)(state["url"]) else None


status = live_state


# ================================================================================================= start / stop
@contextmanager
def _scope_lock(directory: Path, timeout: float = 30.0) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "engine.lock"
    if fcntl is not None:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return
    deadline = time.monotonic() + timeout  # pragma: no cover - Windows fallback
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise VBoardError("unavailable", "another process is starting the engine") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lock)
        except OSError:
            pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start(scope: str = "default", port: int | None = None, *, env: Mapping[str, str] | None = None,
          spec: EngineSpec | None = None, spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
          http_get: Callable[[str], dict | None] | None = None, sleep: Callable[[float], None] = time.sleep,
          monotonic: Callable[[], float] = time.monotonic, ready_timeout: float = 20.0,
          which: Callable[[str, Mapping[str, str]], str | None] | None = None,
          run: Callable[..., Any] = subprocess.run) -> dict:
    """Start (or reuse) the engine for `scope`. Returns `{url, pid, dir}` (`reused: True` when already running)."""
    env_map = _env_of(env)
    directory = board_dir(scope, env_map)
    probe = http_get or discovery
    with _scope_lock(directory):
        live = live_state(scope, env_map, http_get=probe)
        if live is not None:
            return {"url": live["url"], "pid": live["pid"], "dir": str(directory), "reused": True}
        spec = spec or resolve(env_map, which=which, run=run)
        if spec is None:
            raise VBoardError("not_supported", "no vb engine found",
                              hint="run `python -m fusion_swarm.vboard.engine install --yes` (needs Node >= 22.18), "
                                   "or set FUSION_VBOARD_ENGINE")
        use_port = int(port) if port else free_port()
        url = f"http://127.0.0.1:{use_port}/api/board"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        argv = [*spec.argv, "serve", "--port", str(use_port), "--dir", str(directory), "--no-inspect", "--no-open"]
        logfile = directory / "engine.log"
        with open(logfile, "ab") as out:
            kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": out,
                                      "env": dict(env_map), "close_fds": True}
            if os.name == "posix":
                kwargs["start_new_session"] = True
            elif hasattr(subprocess, "CREATE_NO_WINDOW"):  # pragma: no cover - Windows
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                proc = spawn(argv, **kwargs)
            except OSError as exc:
                raise VBoardError("unavailable", f"cannot start the engine ({exc.__class__.__name__})") from None
        _CHILDREN[proc.pid] = proc
        deadline = monotonic() + float(ready_timeout)
        while True:
            if probe(url):
                break
            if proc.poll() is not None:
                _CHILDREN.pop(proc.pid, None)
                raise VBoardError("unavailable", f"the engine exited with {proc.returncode} before it was ready",
                                  hint=f"see {logfile}")
            if monotonic() >= deadline:
                _terminate(proc.pid, 2.0, sleep, monotonic)
                raise VBoardError("unavailable", f"the engine did not answer at {url} in {ready_timeout:g}s",
                                  hint=f"see {logfile}")
            sleep(0.05)
        _write_state(scope, {"pid": proc.pid, "url": url, "port": use_port, "dir": str(directory), "scope": scope,
                             "engine": spec.as_dict(), "started": time.time()}, env_map)
        return {"url": url, "pid": proc.pid, "dir": str(directory)}


def _terminate(pid: int, grace: float, sleep: Callable[[float], None], monotonic: Callable[[], float]) -> bool:
    """SIGTERM, wait up to `grace`, then SIGKILL the process group. True when the process is gone."""
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    deadline = monotonic() + grace
    while pid_alive(pid) and monotonic() < deadline:
        sleep(0.05)
    if pid_alive(pid):
        try:
            if os.name == "posix":
                os.killpg(pid, sigkill)
            else:  # pragma: no cover
                os.kill(pid, sigkill)
        except OSError:
            pass
        deadline = monotonic() + 2.0
        while pid_alive(pid) and monotonic() < deadline:
            sleep(0.05)
    child = _CHILDREN.get(pid)
    if child is not None:
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    gone = not pid_alive(pid)
    if gone:
        _CHILDREN.pop(pid, None)
    return gone


def stop(scope: str = "default", *, env: Mapping[str, str] | None = None, grace: float = 10.0,
         http_get: Callable[[str], dict | None] | None = None, sleep: Callable[[float], None] = time.sleep,
         monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Stop the engine recorded for `scope`. A stale state file (dead pid, or nothing answering the recorded url,
    which also guards against a recycled pid) is removed without signalling anything."""
    env_map = _env_of(env)
    state = read_state(scope, env_map)
    if state is None:
        return {"stopped": False, "reason": "not_running"}
    path = state_path(scope, env_map)
    pid = state["pid"]
    probe = http_get or discovery
    if not pid_alive(pid) or not probe(state["url"]):
        try:
            path.unlink()
        except OSError:
            pass
        return {"stopped": False, "reason": "stale", "pid": pid}
    gone = _terminate(pid, grace, sleep, monotonic)
    if gone:
        try:
            path.unlink()
        except OSError:
            pass
    return {"stopped": gone, "pid": pid, "url": state["url"], "reason": "stopped" if gone else "still_running"}


# ================================================================================================= install
def install_command(env: Mapping[str, str] | None = None) -> list[str]:
    env_map = _env_of(env)
    npm = _which("npm", env_map) or "npm"
    return [npm, "i", "--prefix", str(fusion_home(env_map) / "engine"), ENGINE_PACKAGE]


def install(yes: bool = False, *, env: Mapping[str, str] | None = None, run: Callable[..., Any] = subprocess.run,
            out: Any = None) -> dict:
    """Print the npm command that installs the pinned engine; run it only when `yes` is True."""
    out = sys.stdout if out is None else out
    cmd = install_command(env)
    out.write("$ " + " ".join(cmd) + "\n")
    if not yes:
        out.write("not run: pass --yes to install the engine (needs Node >= 22.18)\n")
        return {"command": cmd, "ran": False}
    proc = run(cmd, env=dict(_env_of(env)))
    rc = int(getattr(proc, "returncode", 0) or 0)
    return {"command": cmd, "ran": True, "rc": rc}


# ================================================================================================= open_board
def open_board(url: str | None = None, scope: str = "default", as_: str | None = None, *,
               env: Mapping[str, str] | None = None, start_engine: bool = True,
               hooks: Mapping[str, Any] | None = None) -> BoardPort:
    """Pick a board: explicit url > live engine state file > resolvable engine (started) > Python LocalEngineAdapter.

    Returns the port with `port.selection` set to `explicit-url | engine-state | engine-started | local-fallback`
    and logs the choice. `hooks` forwards test hooks (`which`, `run`, `spawn`, `http_get`, `sleep`, `monotonic`,
    `ready_timeout`) to the engine helpers."""
    env_map = _env_of(env)
    hooks = dict(hooks or {})
    profile = _resolve_profile(as_, env_map)

    def chosen(port: BoardPort, how: str, detail: str) -> BoardPort:
        port.selection = how                                      # type: ignore[attr-defined]
        log.info("board backend: %s (%s)", how, detail)
        return port

    if url:
        return chosen(HttpBoardAdapter(url, as_=profile, env=env_map), "explicit-url", url)
    http_get = hooks.get("http_get")
    live = live_state(scope, env_map, http_get=http_get)
    if live is not None:
        return chosen(HttpBoardAdapter(live["url"], as_=profile, env=env_map), "engine-state", live["url"])
    if start_engine:
        spec = resolve(env_map, which=hooks.get("which"), run=hooks.get("run", subprocess.run))
        if spec is not None:
            try:
                kw = {k: hooks[k] for k in ("spawn", "http_get", "sleep", "monotonic", "ready_timeout", "which", "run")
                      if k in hooks}
                started = start(scope, env=env_map, spec=spec, **kw)
            except (VBoardError, ValueError) as exc:
                log.warning("vb engine (%s) did not start: %s; using the Python local engine", spec.source, exc)
            else:
                return chosen(HttpBoardAdapter(started["url"], as_=profile, env=env_map), "engine-started",
                              started["url"])
    path = fusion_home(env_map) / "vboard" / f"{scope}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return chosen(LocalEngineAdapter(path, as_=profile), "local-fallback", str(path))


# ================================================================================================= command line
def main(argv: Sequence[str] | None = None, *, out: Any = None, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if out is None else out
    verb = args[0] if args else ""
    rest = args[1:]

    def opt(name: str, default: str | None = None) -> str | None:
        if name in rest:
            i = rest.index(name)
            return rest[i + 1] if i + 1 < len(rest) else default
        return default

    scope = opt("--scope", "default") or "default"
    try:
        if verb == "resolve":
            spec = resolve(env)
            out.write(json.dumps({"engine": spec.as_dict() if spec else None}) + "\n")
            return 0 if spec else 1
        if verb == "status":
            state = live_state(scope, env)
            out.write(json.dumps({"running": state is not None, "state": state}) + "\n")
            return 0 if state else 1
        if verb == "start":
            port = opt("--port")
            out.write(json.dumps(start(scope, int(port) if port else None, env=env)) + "\n")
            return 0
        if verb == "stop":
            res = stop(scope, env=env)
            out.write(json.dumps(res) + "\n")
            return 0 if res.get("stopped") or res.get("reason") in ("not_running", "stale") else 1
        if verb == "install":
            res = install("--yes" in rest, env=env, out=out)
            return int(res.get("rc", 0))
    except VBoardError as exc:
        out.write(json.dumps(exc.to_failure()) + "\n")
        return 1
    except ValueError as exc:
        out.write(json.dumps({"status": "error", "reason": "invalid_request", "message": str(exc)}) + "\n")
        return 2
    out.write("usage: python -m fusion_swarm.vboard.engine {resolve|status|start|stop|install [--yes]} "
              "[--scope NAME] [--port N]\n")
    return 2


__all__ = ["EngineSpec", "ENGINE_PACKAGE", "resolve", "start", "stop", "install", "install_command", "open_board",
           "live_state", "status", "read_state", "state_path", "board_dir", "node_ok", "installed_cli", "discovery",
           "free_port", "pid_alive", "main"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
