"""Pillar 2A: Speculative Worktree Race & Tournament Swarm (`race`, `speculate`, `tournament`).

Spawns parallel heterogeneous provider candidates in isolated APFS Copy-on-Write
workspaces (`clonefile(2)` / `cp -cR`), evaluates each against a deterministic
4-stage verification pipeline (`compile -> test -> scope -> diff`), promotes
the first fully qualified passer (or tournament survivor), and cleans up losing
workspaces and process groups.
"""
from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import scope, spawn, strategies

DEFAULT_PROVIDERS = ("codex", "opencode", "grok")
MODES = ("race", "speculate", "tournament")


def _evaluate_four_stage_gate(
    workspace_path: Path | None,
    *,
    output: str,
    gate_cmd: list[str] | str | None = None,
    allowed_writes: list[str] | None = None,
    before_manifest: dict[str, str] | None = None,
) -> dict:
    """Run deterministic 4-stage verification (`compile -> test -> scope -> diff`)."""
    stages: dict[str, dict] = {}

    # Stage 1: compile / syntax sanity
    compile_ok = bool(output and output.strip())
    compile_detail = "non-empty candidate output" if compile_ok else "empty candidate output"
    if workspace_path and workspace_path.is_dir():
        py_files = [
            p for p in workspace_path.rglob("*.py")
            if ".git" not in p.parts and ".fusion" not in p.parts
        ][:32]
        for py_file in py_files:
            try:
                compile(py_file.read_text(encoding="utf-8", errors="replace"), str(py_file), "exec")
            except SyntaxError as exc:
                compile_ok = False
                compile_detail = f"SyntaxError in {py_file.name}:{exc.lineno}: {exc.msg}"
                break
    stages["compile"] = {"passed": compile_ok, "detail": compile_detail}

    # Stage 2: test gate command
    test_ok = compile_ok
    test_detail = "skipped (no gate_cmd)"
    if compile_ok and gate_cmd:
        from . import process_runtime
        cmd = gate_cmd if isinstance(gate_cmd, list) else ["sh", "-c", str(gate_cmd)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace_path) if workspace_path else None,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                **process_runtime.hidden_subprocess_kwargs(),
            )
            test_ok = proc.returncode == 0
            test_detail = (proc.stdout or proc.stderr or f"rc={proc.returncode}").strip()[:240]
        except Exception as exc:
            test_ok = False
            test_detail = str(exc)
    stages["test"] = {"passed": test_ok, "detail": test_detail}

    # Stage 3: scope check
    scope_ok = True
    changed_files: list[str] = []
    violations: list[str] = []
    if workspace_path and workspace_path.is_dir() and before_manifest is not None:
        diff_info = scope.inspect_changes(
            before_manifest,
            allowed_writes if allowed_writes is not None else ["."],
        )
        changed_files = diff_info.get("changed_paths", [])
        violations = diff_info.get("violations", [])
        if allowed_writes is not None:
            scope_ok = bool(diff_info.get("clean_scope", True))
    stages["scope"] = {
        "passed": scope_ok,
        "changed": changed_files,
        "violations": violations,
    }

    # Stage 4: diff / conciseness scoring
    diff_lines = len(output.splitlines()) if output else 0
    if workspace_path and (workspace_path / ".git").exists():
        from . import process_runtime
        try:
            proc = subprocess.run(
                ["git", "-C", str(workspace_path), "diff", "--stat"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                **process_runtime.hidden_subprocess_kwargs(),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                diff_lines = len(proc.stdout.strip().splitlines())
        except Exception:
            pass
    diff_ok = compile_ok and test_ok and scope_ok
    stages["diff"] = {
        "passed": diff_ok,
        "diff_lines": diff_lines,
        "changed_count": len(changed_files),
    }

    passed = all(stage["passed"] for stage in stages.values())
    passed_stages = sum(1 for stage in stages.values() if stage["passed"])
    score = (passed_stages * 25.0) - min(20.0, diff_lines * 0.05)
    return {
        "passed": passed,
        "passed_stages": passed_stages,
        "score": round(score, 3),
        "stages": stages,
    }


def run(
    task: str,
    *,
    providers: list[str] | tuple[str, ...] | None = None,
    workspace: str | Path | None = None,
    gate_cmd: list[str] | str | None = None,
    allowed_writes: list[str] | None = None,
    mode: str = "race",
    timeout: int | None = None,
    dispatch_fn: Callable[..., dict] | None = None,
) -> dict:
    """Execute a speculative worktree race or tournament across heterogeneous providers."""
    if mode not in MODES:
        raise ValueError(f"unknown race mode {mode!r}; valid: {MODES}")

    roster = list(providers or DEFAULT_PROVIDERS)
    if not roster:
        raise ValueError("race requires at least one provider")

    dispatcher = dispatch_fn or spawn.run_provider
    source_ws = Path(workspace).resolve(strict=False) if workspace else None
    clone_root = Path(tempfile.mkdtemp(prefix="fusion-race-")).resolve(strict=False) if source_ws else None
    winner_event = threading.Event()
    winner_lock = threading.Lock()
    first_passer: dict | None = None
    tokens: dict[str, spawn.ProcessCancel] = {
        f"{prov}-{idx + 1}": spawn.ProcessCancel() for idx, prov in enumerate(roster)
    }
    try:
        _params = inspect.signature(dispatcher).parameters
        accepts_cancel = "cancel" in _params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in _params.values()
        )
    except (TypeError, ValueError):
        accepts_cancel = False

    def _run_candidate(idx: int, provider: str) -> dict:
        nonlocal first_passer
        candidate_id = f"{provider}-{idx + 1}"
        if mode in ("race", "speculate") and winner_event.is_set():
            return {
                "candidate_id": candidate_id,
                "provider": provider,
                "status": "cancelled_early",
                "elapsed_s": 0.0,
                "verification": {"passed": False, "passed_stages": 0, "score": 0.0, "stages": {}},
            }

        cand_ws: Path | None = None
        before_manifest: dict | None = None
        provision_meta: dict | None = None
        if source_ws and clone_root:
            cand_ws = clone_root / candidate_id
            provision_meta = scope.provision_clone(str(source_ws), str(cand_ws), authorized=True)
            before_manifest = provision_meta.get("provisioned_baseline")

        prompt = (
            f"[{mode.upper()} CANDIDATE {candidate_id}]\n"
            f"Task: {task}\n"
            "Produce a minimal, verification-ready implementation or solution."
        )
        t0 = time.monotonic()
        kwargs: dict = {}
        if accepts_cancel:
            kwargs["cancel"] = tokens[candidate_id]
        res = dispatcher(
            provider,
            prompt,
            role="implementer",
            cwd=cand_ws,
            timeout=timeout or 300,
            **kwargs,
        )
        elapsed = round(time.monotonic() - t0, 3)
        if (
            mode in ("race", "speculate")
            and (res.get("status") == "cancelled" or tokens[candidate_id].is_set())
            and not res.get("ok")
        ):
            return {
                "candidate_id": candidate_id,
                "provider": provider,
                "ok": False,
                "qualified": False,
                "status": "cancelled",
                "elapsed_s": elapsed,
                "output": "",
                "error": "cancelled: a first passer already won",
                "workspace": str(cand_ws) if cand_ws else None,
                "provision": provision_meta,
                "verification": {"passed": False, "passed_stages": 0, "score": 0.0, "stages": {}},
            }
        output = (res.get("output") or "").strip()
        ok = bool(res.get("ok")) and bool(output)

        verification = _evaluate_four_stage_gate(
            cand_ws,
            output=output if ok else "",
            gate_cmd=gate_cmd,
            allowed_writes=allowed_writes,
            before_manifest=before_manifest,
        )
        qualified = ok and verification["passed"]
        record = {
            "candidate_id": candidate_id,
            "provider": provider,
            "ok": ok,
            "qualified": qualified,
            "elapsed_s": elapsed,
            "output": output,
            "error": res.get("error", ""),
            "workspace": str(cand_ws) if cand_ws else None,
            "provision": provision_meta,
            "verification": verification,
        }
        if qualified:
            with winner_lock:
                if first_passer is None:
                    first_passer = record
                    winner_event.set()
                    if mode in ("race", "speculate"):
                        for cid, tok in tokens.items():
                            if cid != candidate_id:
                                tok.set()
        return record

    candidates: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=len(roster)) as pool:
            futures = {
                pool.submit(_run_candidate, idx, prov): (idx, prov)
                for idx, prov in enumerate(roster)
            }
            for fut in as_completed(futures):
                candidates.append(fut.result())

        candidates.sort(key=lambda c: c["candidate_id"])
        qualified_list = [c for c in candidates if c.get("qualified")]

        winner: dict | None = None
        if mode in ("race", "speculate") and first_passer is not None:
            winner = first_passer
        elif qualified_list:
            # Tournament mode (or fallback): highest verification score, then fastest elapsed time
            qualified_list.sort(
                key=lambda c: (-float(c["verification"]["score"]), float(c["elapsed_s"]))
            )
            winner = qualified_list[0]
        elif candidates:
            # Best partial candidate if none passed all 4 gates
            candidates_by_score = sorted(
                candidates,
                key=lambda c: (
                    -int(c.get("verification", {}).get("passed_stages", 0)),
                    -float(c.get("verification", {}).get("score", 0.0)),
                    float(c.get("elapsed_s", 9999.0)),
                ),
            )
            winner = candidates_by_score[0]
    finally:
        if clone_root and clone_root.exists():
            shutil.rmtree(clone_root, ignore_errors=True)

    return {
        "strategy": mode,
        "task": task,
        "providers": roster,
        "qualified_count": len([c for c in candidates if c.get("qualified")]),
        "winner": {
            "candidate_id": winner["candidate_id"],
            "provider": winner["provider"],
            "qualified": winner.get("qualified", False),
            "elapsed_s": winner.get("elapsed_s", 0.0),
            "score": winner.get("verification", {}).get("score", 0.0),
            "output": winner.get("output", ""),
        } if winner else None,
        "candidates": candidates,
    }
