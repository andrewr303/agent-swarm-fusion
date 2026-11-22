"""Host adapter contracts. Native tools are injected, never guessed or replaced.

CLI request construction is an explicit alternative; merely building a request
does not launch a provider. Native model inventory must come from live runtime.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .mailbox import BoardError, positive_int


class CapabilityUnavailable(BoardError):
    pass


@dataclass(frozen=True)
class HostCapabilities:
    host: str
    models: tuple[str, ...]
    source: str
    operations: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"host": self.host, "models": list(self.models), "source": self.source,
                "operations": list(self.operations), "native": True}


class NativeHostRuntime:
    def __init__(self, host: str, *, models: list[str], tools: dict[str, Callable], source: str = "live-runtime"):
        if host not in ("codex", "andrewcode", "claude"):
            raise BoardError("unsupported native host")
        if source != "live-runtime" or not models or not all(isinstance(item, str) and item for item in models):
            raise CapabilityUnavailable("native model inventory must be supplied by the live runtime")
        if not all(callable(value) for value in tools.values()):
            raise BoardError("native operation adapters must be callable")
        self.host, self.models, self.tools = host, tuple(models), dict(tools)

    def capabilities(self) -> dict:
        return HostCapabilities(self.host, self.models, "live-runtime", tuple(sorted(self.tools))).to_dict()

    def _call(self, operation: str, **arguments):
        if operation not in self.tools:
            raise CapabilityUnavailable(f"{self.host} native {operation} is unavailable; no external fallback was invoked")
        return self.tools[operation](**arguments)

    def dispatch(self, packet: dict) -> dict:
        model = packet["model"]
        if model not in self.models:
            raise CapabilityUnavailable(f"selected model is unavailable: {model}")
        result = self._call("dispatch", request_id=packet["ticket_id"], model=model, prompt=packet["prompt"],
                            workspace=packet["workspace"], worker_id=packet["worker_id"])
        if not isinstance(result, dict) or not result.get("native_handle") or result.get("model") != model:
            raise BoardError("native dispatch must return its real handle and exact observed model")
        return result

    def message(self, native_handle: str, envelope: dict):
        return self._call("message", native_handle=native_handle, envelope=envelope)

    def wait(self, native_handle: str, timeout_ms: int = 10000):
        positive_int(timeout_ms, "timeout_ms")
        if timeout_ms > 60000:
            raise BoardError("one native wait cannot exceed 60000 ms")
        return self._call("wait", native_handle=native_handle, timeout_ms=timeout_ms)

    def status(self, native_handle: str):
        return self._call("status", native_handle=native_handle)

    def cancel(self, native_handle: str):
        return self._call("cancel", native_handle=native_handle)

    def recover(self, native_handle: str):
        return self._call("recover", native_handle=native_handle)


class CodexRuntime(NativeHostRuntime):
    def __init__(self, **kwargs):
        super().__init__("codex", **kwargs)


class AndrewCodeRuntime(NativeHostRuntime):
    def __init__(self, **kwargs):
        super().__init__("andrewcode", **kwargs)


class ClaudeCodeRuntime(NativeHostRuntime):
    def __init__(self, *, explicitly_requested: bool = False, **kwargs):
        if not explicitly_requested:
            raise CapabilityUnavailable("Claude execution needs explicit invocation; installing its plugin is not authorization")
        super().__init__("claude", **kwargs)

    def capabilities(self) -> dict:
        return {**super().capabilities(), "explicitly_requested": True}


def executable_argv(executable: str) -> list[str]:
    if Path(executable).stem.lower() in ("codex", "codex-cli"):
        raise CapabilityUnavailable("standalone Codex CLI is prohibited, including help/version probes; use AndrewCode Node transport")
    path = str(Path(executable).resolve()) if Path(executable).is_file() else shutil.which(executable)
    if not path:
        raise CapabilityUnavailable(f"host executable is missing: {executable}")
    if os.name == "nt" and path.lower().endswith(".ps1"):
        return [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", path]
    return [path]


def cli_request(host: str, executable: str, packet: dict, *, explicitly_requested: bool = False,
                allow_claude: bool = False) -> dict:
    if not explicitly_requested:
        raise CapabilityUnavailable("CLI transport must be selected explicitly; native requests do not fall back")
    if host == "claude" and not allow_claude:
        raise CapabilityUnavailable("Claude CLI calls need explicit Claude authorization")
    if host in ("codex", "andrewcode"):
        from .andrewcode_cli import CONTROL_GAP
        raise CapabilityUnavailable(CONTROL_GAP)
    command = executable_argv(executable)
    model, prompt, workspace = packet["model"], packet["prompt"], packet["workspace"]
    report = packet["report_path"]
    prompt += "\nReturn a single WorkerReport JSON object as the final response. Save that same JSON to " + report + ". Report real checks and gaps; exclude the report file from changed_paths."
    if host == "claude":
        command += ["--print", "--model", model, "--output-format", "stream-json", "--verbose", prompt]
        stdin = ""
    else:
        raise BoardError("unsupported CLI host")
    return {"host": host, "transport": "cli", "argv": command, "stdin": stdin, "cwd": workspace,
            "requested_model": model, "observed_model": None, "report_path": report,
            "model_identity": "requested; runtime observation required", "provider_called": False}
