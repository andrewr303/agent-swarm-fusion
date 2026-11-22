"""Owned process supervision with durable identity, output and cancellation.

Used for explicit local commands or explicitly selected host CLI transports.
It never chooses a provider or falls back from a native host operation.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import select
import subprocess
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from .host_runtime import CapabilityUnavailable, cli_request
from .mailbox import BoardError, envelope_fingerprint, positive_int, positive_number


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid: int) -> str | None:
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                return None
            value = values[0]
            return str(value.dwLowDateTime | value.dwHighDateTime << 32)
        finally:
            kernel.CloseHandle(handle)
    if sys.platform == "darwin":
        class _ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]
        try:
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            info = _ProcBsdInfo()
            size = lib.proc_pidinfo(int(pid), 3, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info) or info.pbi_status == 5:  # SZOMB == 5
                return None
            return f"{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
        except OSError:
            return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + fields[19]
    except (OSError, IndexError):
        return None


def _wait_process_exit(process: subprocess.Popen, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for ``process`` to exit; uses kqueue EVFILT_PROC on macOS."""
    if process.poll() is not None:
        return True
    if hasattr(select, "kqueue") and hasattr(select, "KQ_FILTER_PROC"):
        kq = None
        try:
            kq = select.kqueue()
            note_exit = getattr(select, "KQ_NOTE_EXIT", 0x80000000)
            ev = select.kevent(
                process.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                fflags=note_exit,
            )
            kq.control([ev], 0, 0)
            events = kq.control(None, 1, max(0.0, float(timeout)))
            if events:
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
                return process.poll() is not None
        except OSError:
            pass
        finally:
            if kq is not None:
                try:
                    kq.close()
                except OSError:
                    pass
    try:
        process.wait(timeout=max(0.0, float(timeout)))
        return True
    except subprocess.TimeoutExpired:
        return process.poll() is not None


def terminate_process_group(
    process: subprocess.Popen,
    expected_identity: str | None = None,
    *,
    grace_int: float = 2.0,
    grace_term: float = 2.0,
    pgid: int | None = None,
) -> None:
    """Creation-bound SIGINT -> SIGTERM -> SIGKILL ladder for an owned process group.

    The group id is recorded up front (``pgid`` or the leader pid, valid for a child
    started with ``start_new_session=True``) and the call ALWAYS finishes with one
    ``os.killpg(pgid, SIGKILL)`` on POSIX, even when the leader already exited, so
    grandchildren that outlive the leader are reaped (J-14). The final kill is skipped
    only when a live leader's creation identity no longer matches (pid reuse).
    """
    if process is None:
        return
    pid = process.pid
    if not pid:
        return
    group = int(pgid) if pgid else pid
    leader_alive = process.poll() is None

    def _identity_ok() -> bool:
        if expected_identity is None:
            return True
        current = process_identity(pid)
        return current is not None and current == expected_identity

    if os.name == "nt":
        if leader_alive and _identity_ok():
            process.terminate()
            if not _wait_process_exit(process, grace_int + grace_term):
                process.kill()
                _wait_process_exit(process, 2.0)
        return

    identity_mismatch = False
    if leader_alive:
        ladder = (
            (signal.SIGINT, grace_int),
            (signal.SIGTERM, grace_term),
            (signal.SIGKILL, 2.0),
        )
        for sig, wait_s in ladder:
            if process.poll() is not None:
                break
            if not _identity_ok():
                identity_mismatch = process.poll() is None
                break
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                break
            except (PermissionError, OSError):
                try:
                    process.send_signal(sig)
                except OSError:
                    break
            if _wait_process_exit(process, wait_s):
                break
    if identity_mismatch:
        return
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class WindowsJob:
    """Kill only the owned command tree if its supervisor exits."""
    def __init__(self, process: subprocess.Popen):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes
        class BASIC(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                        ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                        ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class EXTENDED(ctypes.Structure):
            _fields_ = [("basic", BASIC), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        settings = EXTENDED()
        settings.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(settings), ctypes.sizeof(settings))
                or not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle)))):
            self.close()
            raise OSError("owned command could not be assigned to its Windows lifetime job")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessRuntime:
    def __init__(self, root: str, *, models: list[str], command: Callable | None = None,
                 host: str = "local", executable: str | None = None,
                 explicitly_requested: bool = False, allow_claude: bool = False):
        if host != "local" and not explicitly_requested:
            raise CapabilityUnavailable("host CLI transport must be selected explicitly")
        if host == "claude" and not allow_claude:
            raise CapabilityUnavailable("Claude CLI execution is not authorized")
        if host == "local" and not callable(command):
            raise BoardError("a local process runtime needs an explicit command factory")
        if not models or not all(isinstance(item, str) and item for item in models):
            raise BoardError("an explicit selected model/worker label inventory is required")
        self.root = Path(root).resolve()
        self.models, self.command, self.host, self.executable = tuple(models), command, host, executable
        self.allow_claude, self._children = allow_claude, {}

    def capabilities(self) -> dict:
        return {"host": self.host, "models": list(self.models), "source": "local-process" if self.host == "local" else "explicit-cli",
                "operations": ["dispatch", "wait", "status", "cancel", "recover"], "native": False,
                "transport": "process" if self.host == "local" else "cli", "explicitly_requested": True}

    def _directory(self, handle: str) -> Path:
        if not isinstance(handle, str) or not re.fullmatch(r"process:[a-f0-9]{32}", handle):
            raise BoardError("unknown owned process handle")
        path = self.root / handle.split(":", 1)[1]
        if not path.is_dir() or path.resolve() != path or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise BoardError("owned process directory is absent or escapes the runtime root")
        for name in ("owner.json", "request.json", "outcome.json", "cancel"):
            child = path / name
            if child.is_symlink() or (child.exists() and bool(getattr(child.lstat(), "st_file_attributes", 0) & 0x400)):
                raise BoardError("owned process metadata cannot be linked")
        return path

    def dispatch(self, packet: dict) -> dict:
        if packet["model"] not in self.models:
            raise CapabilityUnavailable("the selected worker is unavailable")
        ticket = packet["ticket_id"]
        if not isinstance(ticket, str) or not re.fullmatch(r"[a-f0-9]{32}", ticket):
            raise BoardError("process tickets must be controller-issued identities")
        request = self.command(packet) if self.host == "local" else cli_request(self.host, self.executable or self.host, packet,
                    explicitly_requested=True, allow_claude=self.allow_claude)
        if not isinstance(request, dict) or not isinstance(request.get("argv"), list) or not request["argv"] or any(not isinstance(x, str) for x in request["argv"]):
            raise BoardError("command factory must return an argv list")
        if not isinstance(request.get("stdin", ""), str):
            raise BoardError("process stdin must be text")
        positive_number(request.get("timeout_seconds", 900), "timeout_seconds")
        if not Path(packet["workspace"]).is_dir():
            raise BoardError("process workspace is missing")
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / ticket
        if request.get("report_path"):
            report = Path(request["report_path"]).resolve()
            if self.root not in report.parents:
                raise BoardError("process report must stay inside its owned runtime root")
        fingerprint = envelope_fingerprint(request=request, cwd=packet["workspace"], model=packet["model"], host=self.host)
        if directory.exists():
            self._directory("process:" + ticket)
            owner_path = directory / "owner.json"
            if not owner_path.is_file():
                raise BoardError("previous process dispatch is uncertain; reconcile instead of launching again")
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            if owner["fingerprint"] != fingerprint:
                raise BoardError("process ticket reused for a different command")
            return {"native_handle": "process:" + ticket, "model": packet["model"] if self.host == "local" else None, "reused": True}
        directory.mkdir()
        request = {**request, "ticket_id": ticket, "model": packet["model"], "host": self.host,
                   "cwd": packet["workspace"], "timeout_seconds": request.get("timeout_seconds", 900), "token": uuid.uuid4().hex}
        atomic_json(directory / "request.json", request)
        if request.get("report_path"):
            report.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[1]) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        helper = subprocess.Popen([sys.executable, "-B", "-m", "fusion_swarm.process_runtime", "--execute", str(directory)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        atomic_json(directory / "owner.json", {"ticket_id": ticket, "token": request["token"], "fingerprint": fingerprint,
                    "helper_pid": helper.pid, "helper_identity": process_identity(helper.pid)})
        self._children[ticket] = helper
        return {"native_handle": "process:" + ticket, "model": packet["model"] if self.host == "local" else None, "reused": False}

    def status(self, native_handle: str) -> dict:
        directory = self._directory(native_handle)
        owner = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
        if owner.get("ticket_id") != native_handle.split(":", 1)[1]:
            raise BoardError("process ownership ticket differs from its handle")
        outcome = directory / "outcome.json"
        if outcome.is_file():
            result = json.loads(outcome.read_text(encoding="utf-8"))
            if result["token"] != owner["token"]:
                raise BoardError("process result ownership token differs")
            child = self._children.pop(owner["ticket_id"], None)
            if child:
                child.wait(timeout=5)
            return {**result, "native_handle": native_handle}
        identity = process_identity(owner["helper_pid"])
        if identity is None or identity != owner["helper_identity"]:
            return {"state": "uncertain", "native_handle": native_handle, "error": "supervisor identity unavailable; inspect owned child before recovery"}
        return {"state": "working", "native_handle": native_handle, "model": None if self.host != "local" else json.loads((directory / "request.json").read_text(encoding="utf-8"))["model"]}

    def wait(self, native_handle: str, timeout_ms: int = 10000) -> dict:
        positive_int(timeout_ms, "timeout_ms")
        if timeout_ms > 60000:
            raise BoardError("one process wait cannot exceed 60000 ms")
        until = time.monotonic() + timeout_ms / 1000
        while True:
            result = self.status(native_handle)
            if result["state"] != "working" or time.monotonic() >= until:
                return result
            time.sleep(min(0.05, max(0, until - time.monotonic())))

    def cancel(self, native_handle: str) -> dict:
        directory = self._directory(native_handle)
        result = self.status(native_handle)
        if result["state"] == "uncertain":
            raise BoardError("cannot cancel a process with uncertain ownership; reconcile its identity")
        if result["state"] == "working":
            (directory / "cancel").write_text("cancel owned command", encoding="utf-8")
            return self.wait(native_handle, 10000)
        return result

    def recover(self, native_handle: str) -> dict:
        return self.status(native_handle)

    def message(self, native_handle: str, envelope: dict):
        raise CapabilityUnavailable("running CLI commands have no native follow-up capability; use the board or an explicit session continuation")


def execute(directory: Path) -> int:
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    process = None
    job = None
    result = {"token": request["token"], "state": "failed", "model": None, "host": request["host"], "exit_code": None,
              "stdout_path": str(directory / "stdout.log"), "stderr_path": str(directory / "stderr.log"), "started_at": time.time()}
    try:
        with (directory / "stdout.log").open("wb") as output, (directory / "stderr.log").open("wb") as error:
            process = subprocess.Popen(request["argv"], cwd=request["cwd"], stdin=subprocess.PIPE, stdout=output, stderr=error,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, start_new_session=os.name != "nt")
            job = WindowsJob(process)
            child_identity = process_identity(process.pid)
            atomic_json(directory / "child.json", {"pid": process.pid, "identity": child_identity, "token": request["token"]})
            if process.stdin:
                try:
                    process.stdin.write(request.get("stdin", "").encode("utf-8"))
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            deadline = time.monotonic() + request["timeout_seconds"]
            while process.poll() is None:
                if (directory / "cancel").exists() or time.monotonic() >= deadline:
                    result["state"] = "cancelled" if (directory / "cancel").exists() else "failed"
                    result["error"] = "owned command cancelled" if result["state"] == "cancelled" else "owned command deadline reached"
                    if job.handle:
                        job.close()
                    else:
                        terminate_process_group(process, child_identity)
                    break
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            result["exit_code"] = process.returncode
            if "error" not in result:
                result["state"] = "stopped" if process.returncode == 0 else "failed"
                if process.returncode != 0:
                    result["failure_kind"] = "worker"
        text = (directory / "stdout.log").read_text(encoding="utf-8", errors="replace")
        events = []
        for line in text.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        for event in events:
            if isinstance(event, dict):
                if event.get("model"):
                    result["model"] = event["model"]
                if event.get("thread_id") or event.get("session_id"):
                    result["provider_session_id"] = event.get("thread_id") or event.get("session_id")
                if isinstance(event.get("report"), dict):
                    result["report"] = event["report"]
        report_path = request.get("report_path")
        if report_path and Path(report_path).is_file():
            result["report"] = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except Exception as exc:
        result["state"] = "failed"
        result["failure_kind"] = "infrastructure"
        result["error"] = f"{type(exc).__name__}: {exc}"
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    finally:
        if job:
            job.close()
        result["completed_at"] = time.time()
        atomic_json(directory / "outcome.json", result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", type=Path, required=True)
    return execute(parser.parse_args().execute)


if __name__ == "__main__":
    raise SystemExit(main())
