"""AndrewCode CLI transport contracts; no standalone Codex executable fallback.

Prompt mode runs in auto permission mode and persists sessions, so command
construction alone is not isolation. Consult seats (the legacy "codex" seat)
launch with ``--disable-tools --ephemeral`` (see ``command(isolated=True)``) after
``run_isolated`` confirms the installed AndrewCode build advertises both flags.
Tool-enabled worker seats and the legacy sandbox contract still fail before
launch rather than weaken safety.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

def _default_entry() -> Path:
    override = os.environ.get("FUSION_ANDREWCODE_ENTRY")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path("C:/dev/andrew-code/apps/kimi-code/dist/main.mjs")
    for candidate in (
        Path.home() / "dev/andrew-code/apps/kimi-code/dist/main.mjs",
        Path(__file__).resolve().parents[4] / "andrew-code/apps/kimi-code/dist/main.mjs",
        Path("C:/dev/andrew-code/apps/kimi-code/dist/main.mjs"),
    ):
        if candidate.is_file():
            return candidate
    return Path.home() / "dev/andrew-code/apps/kimi-code/dist/main.mjs"


# FUSION_ANDREWCODE_ENTRY lets tests (and a relocated checkout) point at another entry script.
ENTRY = _default_entry()
ISOLATION_FLAGS = ("--disable-tools", "--ephemeral")
DEFAULT_TIMEOUT_S = 600
DEFAULT_MODEL = "gpt-6.1-sol"
EFFORTS = frozenset(("off", "on", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))
CONTROL_GAP = (
    "AndrewCode CLI transport unavailable: verified tools-disabled, filesystem sandbox "
    "and ephemeral/non-persistent controls are not available for this legacy seat. "
    "Prompt mode enables auto permissions and persists sessions; a snapshot or temporary "
    "cwd is not isolation. No provider was called; use an explicitly authorized native runtime."
)


def command(model: str, effort: str, prompt_file: str, *, node: str = "node", isolated: bool = False) -> list[str]:
    """Build exact one-shot argv; the caller must independently enforce controls.

    AndrewCode validates model availability and model-specific effort support.
    Never translate a model ID, omit effort, or clamp an unsupported level.
    """
    if Path(node).name.lower() not in ("node", "node.exe"):
        raise ValueError("AndrewCode must use the Node executable directly, not a CLI shim")
    if not model or not model.strip():
        raise ValueError("an exact configured AndrewCode model alias is required")
    if effort not in EFFORTS:
        raise ValueError(f"unsupported exact thinking effort: {effort}")
    if not prompt_file:
        raise ValueError("a UTF-8 prompt file is required")
    argv = [node, str(ENTRY), "-m", model, "--thinking-effort", effort,
            "--prompt-file", prompt_file, "--output-format", "stream-json"]
    return argv + list(ISOLATION_FLAGS) if isolated else argv


def final_text(raw: str) -> str:
    """Extract the final assistant reply from AndrewCode stream-json, not tools."""
    final = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("invalid AndrewCode stream-json event")
        role, kind = event.get("role"), event.get("type")
        result = event.get("result")
        failed = event.get("state") in ("failed", "error") or event.get("status") in ("failed", "error")
        if isinstance(result, dict):
            failed = failed or result.get("type") in ("failed", "error") or result.get("state") in ("failed", "error")
        if role != "tool" and (
            role == "error" or kind in ("error", "turn.failed", "turn.error")
            or (kind in ("turn.ended", "turn.completed", "turn.result") and failed)
        ):
            # Provider error text can include credentials or private request data.
            # Report the failure category, never the raw event/message payload.
            raise ValueError("AndrewCode stream reported an explicit error or failed turn; no successful final reply")
        if role == "assistant" and not event.get("tool_calls"):
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                final = content
    if not final.strip():
        raise ValueError("AndrewCode returned no final assistant text")
    return final


def model_aliases(text: str, provider: str = "codex") -> list[str]:
    """Select only aliases owned by the requested OAuth provider, never secrets."""
    try:
        models = json.loads(text).get("models", {})
    except (ValueError, AttributeError):
        return []
    if not isinstance(models, dict):
        return []
    return [alias for alias, spec in models.items()
            if isinstance(alias, str) and isinstance(spec, dict) and spec.get("provider") == provider]


def _hidden_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _relay_stdout(pipe, sink: list, meta: dict) -> None:
    """Buffer stdout for final_text while forwarding bounded chat packets to stderr."""
    try:
        from . import cli_progress
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from fusion_swarm import cli_progress
    for chunk in iter(pipe.readline, b""):
        sink.append(chunk)
        try:
            cli_progress.relay(chunk.decode("utf-8", errors="replace"), meta=meta)
        except Exception:  # progress is advisory; never affect the result
            pass


def run_isolated(prompt_file: str, out_file: str, model: str, effort: str,
                 timeout: float = DEFAULT_TIMEOUT_S, *, progress: bool = False,
                 progress_meta: dict | None = None) -> tuple[int, str]:
    """One tools-disabled, non-persistent consult turn; returns (exit code, diagnostic).

    Fails closed (no provider call) unless the installed build advertises both
    isolation flags. The prompt must be self-contained: the run starts in an
    empty temporary directory with no stdin. The stream carries no model id, so
    identity stays "requested-only".

    Opt-in progress (``progress=True`` or env ``FUSION_PROGRESS_ID``): stdout is
    streamed line by line and bounded ``FUSION_CHAT_V1`` packets go to stderr;
    the buffered stream and the final text are unchanged.
    """
    node = shutil.which("node")
    if not ENTRY.is_file() or not node:
        return 127, "AndrewCode Node entry is missing; no provider was called"
    argv = command(model, effort, prompt_file, node=node, isolated=True)
    try:
        help_text = subprocess.run([node, str(ENTRY), "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                                   encoding="utf-8", errors="replace", timeout=30, check=False,
                                   creationflags=_hidden_flags()).stdout
    except subprocess.TimeoutExpired:
        return 1, "AndrewCode capability check timed out; no provider was called"
    if not all(flag in help_text for flag in ISOLATION_FLAGS):
        return 1, "The installed AndrewCode build lacks --disable-tools/--ephemeral, so isolation cannot be enforced; no provider was called. Rebuild AndrewCode."
    # Windows can hold the cwd briefly after the child exits; a stuck empty dir must not fail a good consult.
    relay_on = progress or bool(os.environ.get("FUSION_PROGRESS_ID"))
    meta = {**(progress_meta or {})}
    if relay_on:
        meta.setdefault("id", os.environ.get("FUSION_PROGRESS_ID") or uuid.uuid4().hex)
        meta.setdefault("model", model)
        meta.setdefault("effort", effort)
    with tempfile.TemporaryDirectory(prefix="fusion-andrewcode-", ignore_cleanup_errors=True) as cwd, \
            tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            from .process_runtime import process_identity, terminate_process_group
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from fusion_swarm.process_runtime import process_identity, terminate_process_group
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE if relay_on else stdout, stderr=stderr,
                                shell=False, creationflags=_hidden_flags(), start_new_session=os.name != "nt")
        child_identity = process_identity(proc.pid) if proc.pid else None
        chunks: list[bytes] = []
        reader = None
        if relay_on:
            reader = threading.Thread(target=_relay_stdout, args=(proc.stdout, chunks, meta), daemon=True)
            reader.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process_group(proc, child_identity)
            if reader is not None:
                reader.join(5)
            return 124, f"AndrewCode consult timed out after {timeout:g}s; owned process stopped; no retry"
        if reader is not None:
            reader.join(10)
            raw = b"".join(chunks).decode("utf-8", errors="replace")
        else:
            stdout.seek(0)
            raw = stdout.read().decode("utf-8", errors="replace")
        stderr.seek(0)
        error = stderr.read(8192).decode("utf-8", errors="replace").lower()
    if code != 0:
        category = ("authentication" if any(t in error for t in ("unauthorized", "authentication", "not logged in", "401"))
                    else "model availability" if "model" in error and any(t in error for t in ("not found", "unavailable", "access", "invalid"))
                    else "CLI failure")
        return code, f"AndrewCode exited {code}: {category}; raw diagnostics withheld to avoid secret disclosure"
    try:
        text = final_text(raw)
    except ValueError as exc:
        return 1, str(exc)
    Path(out_file + ".raw").write_text(raw, encoding="utf-8")
    Path(out_file).write_text(text, encoding="utf-8")
    return 0, f"ok; model={model}; effort={effort}; identity=requested-only; tools=disabled; session=ephemeral"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt_file")
    parser.add_argument("out_file")
    parser.add_argument("model")
    parser.add_argument("effort")
    parser.add_argument("--sandbox", default="snapshot-only")
    parser.add_argument("--isolated", action="store_true",
                        help="run a tools-disabled, ephemeral consult turn instead of failing closed")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("FUSION_ANDREWCODE_TIMEOUT") or DEFAULT_TIMEOUT_S))
    args = parser.parse_args()
    output = Path(args.out_file)
    output.write_text("", encoding="utf-8")
    try:
        Path(args.prompt_file).read_text(encoding="utf-8")
        command(args.model, args.effort, args.prompt_file)
        if args.isolated:
            if args.sandbox == "danger-full-access":
                diagnostic, code = "danger-full-access cannot run on the tools-disabled consult seat; no provider was called", 1
            else:
                code, diagnostic = run_isolated(args.prompt_file, args.out_file, args.model, args.effort, args.timeout)
                if args.sandbox == "workspace-write" and code == 0:
                    diagnostic += "; note: consult seat is text-only, requested workspace-write was not applied"
        elif not ENTRY.is_file() or not shutil.which("node"):
            diagnostic, code = "AndrewCode Node entry is missing; no provider was called", 127
        else:
            diagnostic, code = CONTROL_GAP + f" Requested contract: {args.sandbox}; model={args.model}; effort={args.effort}.", 1
    except (OSError, UnicodeError, ValueError) as exc:
        diagnostic, code = str(exc), 1
    Path(str(output) + ".log").write_text(diagnostic + "\n", encoding="utf-8")
    print(diagnostic, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
