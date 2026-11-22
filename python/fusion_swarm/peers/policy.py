"""Peer routing policy (D2): who may be spawned, and through which transport.

Hard rule: the `codex` binary is only ever executed when `codex_exec_allowed()`;
on a Claude host the codex-model seat goes through `andrewcode`.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Mapping

HOSTS = ("claude", "codex", "andrewcode", "kimi", "unknown")
PEERS = ("claude", "codex", "andrewcode", "opencode")
TRANSPORTS = ("claude-print", "andrewcode-oneshot", "andrewcode-acp", "codex-exec",
              "claude-acp", "codex-acp", "opencode-run")
CODEX_BINARIES = frozenset({"codex", "codex.exe", "codex.cmd", "codex.bat"})
DEFAULT_CODEX_MODEL = "gpt-6.1-sol"


class PeerError(Exception):
    """codes: peer_not_allowed, peer_absent, peer_failed, invalid_request, depth_exceeded, timeout, cancelled"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def detect_host(env: Mapping[str, str] | None = None) -> str:
    """Resolve the host CLI (J-08).

    Claude markers (CLAUDECODE=="1" or CLAUDE_CODE_ENTRYPOINT set) win over the
    legacy FUSION_HOST=codex default unless FUSION_HOST_EXPLICIT=1.
    """
    e = _env(env)
    declared = (e.get("FUSION_HOST") or "").strip().lower()
    marker = e.get("CLAUDECODE") == "1" or bool(e.get("CLAUDE_CODE_ENTRYPOINT"))
    explicit = e.get("FUSION_HOST_EXPLICIT") == "1"
    if declared in HOSTS and declared != "unknown":
        if declared == "codex" and marker and not explicit:
            return "claude"
        return declared
    return "claude" if marker else "unknown"


def codex_exec_allowed(env: Mapping[str, str] | None = None) -> bool:
    e = _env(env)
    return e.get("FUSION_ALLOW_CODEX_EXEC") == "1" or detect_host(e) == "codex"


def claude_spawn_allowed(env: Mapping[str, str] | None = None) -> bool:
    e = _env(env)
    return detect_host(e) == "codex" or e.get("FUSION_ALLOW_CLAUDE") == "1"


def is_codex_binary(argv0: str) -> bool:
    return os.path.basename(str(argv0)).lower() in CODEX_BINARIES


@dataclass(frozen=True)
class Route:
    peer: str
    transport: str
    binary: str
    allowed: bool
    reason: str
    default_model: str | None = None


def _on_path(name: str, env: Mapping[str, str]) -> bool:
    return shutil.which(name, path=env.get("PATH")) is not None


def route(peer: str, *, session: bool = False, env: Mapping[str, str] | None = None) -> Route:
    e = _env(env)
    if peer not in PEERS:
        raise PeerError("invalid_request", f"unknown peer {peer!r}")
    if peer == "andrewcode":
        return Route(peer, "andrewcode-acp" if session else "andrewcode-oneshot", "andrewcode", True, "always")
    if peer == "opencode":
        if session:
            raise PeerError("invalid_request", "opencode has no session transport")
        return Route(peer, "opencode-run", "opencode", True, "always")
    if peer == "claude":
        allowed = claude_spawn_allowed(e)
        if allowed:
            reason = "codex host or FUSION_ALLOW_CLAUDE=1"
        elif detect_host(e) == "claude":
            reason = "host-native"
        else:
            reason = "claude spawn needs a codex host or FUSION_ALLOW_CLAUDE=1"
        if session:
            return Route(peer, "claude-acp", "claude-agent-acp", allowed, reason)
        return Route(peer, "claude-print", "claude", allowed, reason)
    # codex
    gated = codex_exec_allowed(e)
    if session:
        if gated and _on_path("codex-acp", e):
            return Route(peer, "codex-acp", "codex-acp", True, "codex exec allowed", None)
        return Route(peer, "andrewcode-acp", "andrewcode", True, "andrewcode seat", DEFAULT_CODEX_MODEL)
    if gated:
        return Route(peer, "codex-exec", "codex", True, "codex exec allowed")
    return Route(peer, "andrewcode-oneshot", "andrewcode", True, "andrewcode seat (codex exec gated)",
                 DEFAULT_CODEX_MODEL)


def require(rt: Route) -> Route:
    if not rt.allowed:
        raise PeerError("peer_not_allowed", rt.reason)
    return rt


def assert_exec_allowed(argv0: str, env: Mapping[str, str] | None = None) -> None:
    """Defence in depth: refuse to execute a codex binary without the gate."""
    if is_codex_binary(argv0) and not codex_exec_allowed(env):
        raise PeerError("peer_not_allowed", "codex binary exec requires FUSION_ALLOW_CODEX_EXEC=1 or a codex host")
