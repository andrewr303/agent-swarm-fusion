"""Cross-CLI peer layer (policy, identity, structured one-shot exec)."""
from .policy import (CODEX_BINARIES, HOSTS, PEERS, TRANSPORTS, PeerError, Route, claude_spawn_allowed,
                     codex_exec_allowed, detect_host, require, route)
from .identity import (NAME_RE, agent_name, child_env, identity_env, normalize, resolve_name, worker_name)
from .exec_json import (PeerRequest, PeerResult, as_dispatch_fn, build_argv, feature_flags, parse, run)

__all__ = [
    "CODEX_BINARIES", "HOSTS", "PEERS", "TRANSPORTS", "PeerError", "Route", "claude_spawn_allowed",
    "codex_exec_allowed", "detect_host", "require", "route",
    "NAME_RE", "agent_name", "child_env", "identity_env", "normalize", "resolve_name", "worker_name",
    "PeerRequest", "PeerResult", "as_dispatch_fn", "build_argv", "feature_flags", "parse", "run",
]
