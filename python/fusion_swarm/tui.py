"""Pillar 1D: Capability-Detected `tmux` Split-Pane Viewer & Active-Instance Diff TUI (`fusion tui`).

Provides:
  1. Active-instance status snapshot + per-task / workspace git diff rendering.
  2. Capability-detected optional `tmux` split-pane layout (`--tmux`) that never
     forces `tmux` when unavailable or when running non-interactively.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import board as board_mod, process_runtime


def detect_tmux_capability() -> dict:
    """Return non-intrusive capability check for optional `tmux` viewer."""
    tmux_bin = shutil.which("tmux")
    in_tmux = bool(os.environ.get("TMUX"))
    return {
        "available": bool(tmux_bin),
        "binary": tmux_bin or "",
        "inside_tmux": in_tmux,
    }


def collect_workspace_diff(workspace: Path, *, max_lines: int = 120) -> str:
    """Return a concise `git status --short` + `git diff` summary for `workspace`."""
    ws = Path(workspace).resolve(strict=False)
    if not (ws / ".git").exists():
        return "(no git repository at workspace)"
    try:
        status_proc = subprocess.run(
            ["git", "-C", str(ws), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **process_runtime.hidden_subprocess_kwargs(),
        )
        diff_proc = subprocess.run(
            ["git", "-C", str(ws), "diff", "--no-color"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **process_runtime.hidden_subprocess_kwargs(),
        )
        lines: list[str] = []
        if status_proc.stdout.strip():
            lines.append("--- git status --short ---")
            lines.extend(status_proc.stdout.strip().splitlines()[:30])
        if diff_proc.stdout.strip():
            lines.append("--- git diff ---")
            lines.extend(diff_proc.stdout.strip().splitlines()[:max_lines])
        return "\n".join(lines) if lines else "(working tree clean)"
    except Exception as exc:
        return f"(diff error: {exc})"


def build_tui_snapshot(
    workspace: str | Path = ".",
    *,
    db_path: str | Path | None = None,
) -> dict:
    """Gather board status, active Stigmergy traces, tmux capability, and live git diff."""
    ws = Path(workspace).resolve(strict=False)
    resolved_db = Path(db_path).resolve(strict=False) if db_path else (ws / ".fusion" / "board.sqlite")
    board_snapshot: dict = {"tasks": [], "agents": [], "findings": [], "questions": [], "decisions": []}
    stigmergy_traces: list[dict] = []

    if resolved_db.exists():
        try:
            with board_mod.HiveBoard(resolved_db) as b:
                board_snapshot = {
                    "tasks": b.tasks(),
                    "agents": b.agents(),
                    "findings": [],
                    "questions": [],
                    "decisions": [],
                }
                stigmergy_traces = b.sense_traces(limit=10)
        except Exception:
            pass

    return {
        "workspace": str(ws),
        "db_path": str(resolved_db),
        "tmux": detect_tmux_capability(),
        "board": board_snapshot,
        "stigmergy": stigmergy_traces,
        "diff": collect_workspace_diff(ws),
    }


def render_tui_text(snapshot: dict) -> str:
    """Render an ASCII cockpit view showing active instances, stigmergy, and git diff."""
    board = snapshot.get("board", {})
    tasks = board.get("tasks", [])
    agents = board.get("agents", [])
    traces = snapshot.get("stigmergy", [])
    tmux_info = snapshot.get("tmux", {})

    lines = [
        "======================================================================",
        " FUSION TUI — Active-Instance & Diff Viewer",
        f" Workspace: {snapshot.get('workspace')}",
        f" tmux capability: available={tmux_info.get('available')} inside_tmux={tmux_info.get('inside_tmux')}",
        "======================================================================",
        f"[Active Tasks: {len(tasks)} | Agents: {len(agents)} | Stigmergy Trails: {len(traces)}]",
    ]
    if tasks:
        for t in tasks[:12]:
            tid = t.get("task_id") or t.get("id", "?")
            status = t.get("state") or t.get("status", "?")
            owner = t.get("owner_id") or t.get("owner") or "-"
            title = t.get("title") or t.get("goal", "")
            lines.append(
                f"  * {tid:<14} [{status:<11}] "
                f"owner={owner:<10} {title}"
            )
    else:
        lines.append("  (no tasks recorded on board)")

    if traces:
        lines.append("")
        lines.append("[Stigmergy Pheromone Trails]")
        for tr in traces[:6]:
            locus = tr.get("locus") or tr.get("target")
            strength = tr.get("effective_strength") or tr.get("strength") or tr.get("intensity")
            agent = tr.get("agent_id") or tr.get("agent")
            lines.append(
                f"  ~ {locus} ({tr.get('kind')}) strength={strength} "
                f"by {agent}: {tr.get('note')}"
            )

    lines.append("")
    lines.append("[Active-Instance Diff Tab]")
    lines.append(snapshot.get("diff") or "(working tree clean)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fusion tui")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--db", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--tmux", action="store_true", help="Print or launch optional tmux split viewer")
    parser.add_argument("--dry-run", action="store_true", help="Print tmux command without executing")
    args = parser.parse_args(argv)

    snap = build_tui_snapshot(args.workspace, db_path=args.db or None)
    if args.json:
        print(json.dumps(snap, indent=2, sort_keys=True))
        return 0

    if args.tmux:
        cap = snap["tmux"]
        cmd = f"tmux split-window -h 'python3 -m fusion_swarm tui --workspace {snap['workspace']}'"
        if args.dry_run or not cap["available"] or not cap["inside_tmux"]:
            print(f"# Optional tmux split-pane command (available={cap['available']}):\n{cmd}")
            print(render_tui_text(snap))
            return 0
        subprocess.run(
            ["tmux", "split-window", "-h", sys.executable, "-m", "fusion_swarm", "tui", "--workspace", snap["workspace"]],
            check=False,
            **process_runtime.hidden_subprocess_kwargs(),
        )
        return 0

    print(render_tui_text(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
