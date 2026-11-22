"""Pillar 2B: Two-Phase Execution-Then-Hardening Wave Swarm (`harden`, `wave`).

Alternates:
  1. Execution Wave: Builder generates or repairs candidate output/patch.
  2. Adversarial Hardening Wave: Hardener probes edge cases, security
     invariants, and regression vectors.

Terminates early after `required_clean_waves` (default 2) consecutive clean
hardening waves, capped at `max_waves` (default 4). Rolls back to the previous
verified baseline if a repair wave regresses the deterministic test gate.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import scope, spawn

_FINDING_RE = re.compile(
    r"^\s*(?:[-*]|\d+\.)?\s*\[?(CRITICAL|HIGH|BLOCKER|BUG|REGRESSION)\]?\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_CLEAN_RE = re.compile(r"\b(HARDEN_STATUS:\s*CLEAN|NO_FINDINGS|ZERO_DEFECTS|LGTM)\b", re.IGNORECASE)


def _extract_findings(text: str) -> list[str]:
    if not text:
        return []
    if _CLEAN_RE.search(text):
        return []
    matches = [f"{m.group(1).upper()}: {m.group(2).strip()}" for m in _FINDING_RE.finditer(text)]
    return matches


def _run_gate(workspace: Path | None, gate_cmd: list[str] | str | None) -> tuple[bool, str]:
    if not gate_cmd:
        return True, "no gate_cmd configured"
    from . import process_runtime
    cmd = gate_cmd if isinstance(gate_cmd, list) else ["sh", "-c", str(gate_cmd)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace) if workspace else None,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            **process_runtime.hidden_subprocess_kwargs(),
        )
        ok = proc.returncode == 0
        detail = (proc.stdout or proc.stderr or f"rc={proc.returncode}").strip()[:240]
        return ok, detail
    except Exception as exc:
        return False, str(exc)


def run(
    task: str,
    *,
    builder: str = "codex",
    hardener: str = "grok",
    workspace: str | Path | None = None,
    gate_cmd: list[str] | str | None = None,
    max_waves: int = 4,
    required_clean_waves: int = 2,
    timeout: int | None = None,
    dispatch_fn: Callable[..., dict] | None = None,
) -> dict:
    """Execute alternating Execution -> Hardening waves with rollback on gate regression."""
    if max_waves < 1:
        raise ValueError("max_waves must be >= 1")
    if required_clean_waves < 1:
        raise ValueError("required_clean_waves must be >= 1")

    dispatcher = dispatch_fn or spawn.run_provider
    source_ws = Path(workspace).resolve(strict=False) if workspace else None
    temp_root = Path(tempfile.mkdtemp(prefix="fusion-harden-")).resolve(strict=False) if source_ws else None

    waves: list[dict] = []
    clean_streak = 0
    baseline_output = ""
    open_findings: list[str] = []
    rollback_count = 0

    try:
        for wave_idx in range(1, max_waves + 1):
            wave_ws: Path | None = None
            if source_ws and temp_root:
                wave_ws = temp_root / f"wave-{wave_idx}"
                scope.provision_clone(str(source_ws), str(wave_ws), authorized=True)

            # Phase 1: Execution Wave (initial build on wave 1, or targeted repair when findings exist)
            need_build = (wave_idx == 1) or bool(open_findings) or not baseline_output
            candidate_output = baseline_output
            build_res: dict = {"ok": True, "output": baseline_output, "skipped": not need_build}

            if need_build:
                findings_block = (
                    "\nOpen findings to repair:\n" + "\n".join(f"- {f}" for f in open_findings)
                    if open_findings
                    else ""
                )
                prior_block = f"\nPrevious baseline:\n{baseline_output}\n" if baseline_output else ""
                exec_prompt = (
                    f"[EXECUTION WAVE {wave_idx}/{max_waves}]\n"
                    f"Task: {task}{prior_block}{findings_block}\n"
                    "Produce the complete, hardened solution."
                )
                build_res = dispatcher(
                    builder,
                    exec_prompt,
                    role="implementer",
                    cwd=wave_ws,
                    timeout=timeout or 300,
                )
                candidate_output = (build_res.get("output") or "").strip()

            gate_ok, gate_detail = _run_gate(wave_ws, gate_cmd)
            if not build_res.get("ok", False) or not candidate_output or not gate_ok:
                # Roll back to prior verified baseline if repair regressed tests
                rolled_back = bool(baseline_output)
                if rolled_back:
                    rollback_count += 1
                    candidate_output = baseline_output
                clean_streak = 0
                waves.append(
                    {
                        "wave": wave_idx,
                        "builder": builder,
                        "hardener": hardener,
                        "execution_ok": bool(build_res.get("ok")) and bool(candidate_output),
                        "gate_ok": gate_ok,
                        "gate_detail": gate_detail,
                        "rolled_back": rolled_back,
                        "findings": ["GATE_FAILURE: " + gate_detail],
                        "clean_streak": clean_streak,
                    }
                )
                open_findings = ["GATE_FAILURE: " + gate_detail]
                continue

            baseline_output = candidate_output

            # Phase 2: Adversarial Hardening Wave
            harden_prompt = (
                f"[ADVERSARIAL HARDENING WAVE {wave_idx}/{max_waves}]\n"
                f"Task: {task}\n\n"
                f"Candidate under test:\n{baseline_output}\n\n"
                "Probe boundary conditions, concurrency hazards, security invariants, and error handling.\n"
                "If clean, output `HARDEN_STATUS: CLEAN`. Otherwise list each defect as `HIGH: <issue>` or `CRITICAL: <issue>`."
            )
            harden_res = dispatcher(
                hardener,
                harden_prompt,
                role="critic",
                cwd=wave_ws,
                timeout=timeout or 300,
            )
            harden_text = (harden_res.get("output") or "").strip()
            findings = _extract_findings(harden_text)

            if not findings and harden_res.get("ok", False):
                clean_streak += 1
                open_findings = []
            else:
                clean_streak = 0
                open_findings = findings or ["UNVERIFIED_HARDENER_RESPONSE"]

            waves.append(
                {
                    "wave": wave_idx,
                    "builder": builder,
                    "hardener": hardener,
                    "execution_ok": True,
                    "gate_ok": gate_ok,
                    "gate_detail": gate_detail,
                    "rolled_back": False,
                    "findings": findings,
                    "hardener_output": harden_text,
                    "clean_streak": clean_streak,
                }
            )

            if clean_streak >= required_clean_waves:
                break
    finally:
        if temp_root and temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)

    converged = clean_streak >= required_clean_waves
    return {
        "strategy": "harden",
        "task": task,
        "builder": builder,
        "hardener": hardener,
        "max_waves": max_waves,
        "required_clean_waves": required_clean_waves,
        "waves_executed": len(waves),
        "clean_streak": clean_streak,
        "rollback_count": rollback_count,
        "converged": converged,
        "baseline_output": baseline_output,
        "waves": waves,
    }
