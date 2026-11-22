"""One board/agent identity everywhere (D10, J-12)."""
from __future__ import annotations

import hashlib
import os
import re
from typing import Mapping

from .policy import PeerError, detect_host

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
HOST_DEFAULTS = {"claude": "claude", "codex": "codex", "andrewcode": "andrewcode", "kimi": "kimi"}
DEFAULT_MAX_DEPTH = 2


def _env(env):
    return os.environ if env is None else env


def normalize(name: str) -> str:
    if not isinstance(name, str):
        raise PeerError("invalid_request", "name must be a string")
    n = name.strip().lower()
    if not NAME_RE.match(n):
        raise PeerError("invalid_request", f"invalid agent name {name!r}")
    return n


def agent_name(env: Mapping[str, str] | None = None, *, default: str | None = None) -> str | None:
    """BOARD_PROFILE > FUSION_AGENT_ID > default (invalid env values are skipped)."""
    e = _env(env)
    for key in ("BOARD_PROFILE", "FUSION_AGENT_ID"):
        raw = e.get(key)
        if raw:
            try:
                return normalize(raw)
            except PeerError:
                continue
    return normalize(default) if default else None


def resolve_name(env: Mapping[str, str] | None = None, *, default: str | None = None) -> str | None:
    """BOARD_PROFILE > FUSION_AGENT_ID > host default (claude/codex/...) > default."""
    e = _env(env)
    found = agent_name(e)
    if found:
        return found
    host = HOST_DEFAULTS.get(detect_host(e))
    if host:
        return host
    return normalize(default) if default else None


def worker_name(run_id: str, n: int, *, prefix: str = "uc") -> str:
    raw = f"{prefix}-{run_id}-{n}".lower()
    clean = re.sub(r"[^a-z0-9-]+", "-", raw)
    clean = re.sub(r"-{2,}", "-", clean).strip("-")
    if not clean or not clean[0].isalpha():
        clean = "w" + clean
    if len(clean) > 32:
        clean = clean[:23] + "-" + hashlib.sha256(clean.encode()).hexdigest()[:8]
    return normalize(clean)


def identity_env(name: str, *, board_path: str | None = None, vboard: str | None = None, parent: str | None = None,
                 swarm_id: str | None = None, run_id: str | None = None, task_id: str | None = None) -> dict[str, str]:
    n = normalize(name)
    out = {"FUSION_AGENT_ID": n, "BOARD_PROFILE": n}
    for key, val in (("FUSION_BOARD", board_path), ("FUSION_VBOARD", vboard), ("FUSION_PARENT_ID", parent),
                     ("FUSION_SWARM_ID", swarm_id), ("FUSION_RUN_ID", run_id), ("BOARD_TASK", task_id)):
        if val:
            out[key] = str(val)
    return out


def _int(value, default: int) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def child_env(base: Mapping[str, str] | None, name: str, *, strip_claude_nesting: bool = True,
              **identity) -> dict[str, str]:
    env = dict(_env(base))
    depth = _int(env.get("FUSION_PEER_DEPTH"), 0) + 1
    limit = _int(env.get("FUSION_PEER_MAX_DEPTH"), DEFAULT_MAX_DEPTH)
    if depth > limit:
        raise PeerError("depth_exceeded", f"peer depth {depth} exceeds FUSION_PEER_MAX_DEPTH={limit}")
    env.update(identity_env(name, **identity))
    env["FUSION_PEER_DEPTH"] = str(depth)
    if strip_claude_nesting:
        # Pin the parent's resolved host BEFORE the Claude markers go: detect_host lets those markers beat an
        # inherited legacy FUSION_HOST=codex, so a child with the markers stripped would otherwise read as a
        # Codex host and open the D2 codex gate (a Claude host spawning `codex`).
        try:
            from .policy import detect_host
            host = detect_host(env)
        except Exception:  # pragma: no cover - policy import problems must not break spawning
            host = "unknown"
        if host != "unknown":
            env["FUSION_HOST"] = host
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return env
