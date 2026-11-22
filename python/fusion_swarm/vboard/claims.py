"""Claim cache shared with the real `vercel board` CLI (design/v4-spec.md 2.3, judge J-06).

Layout: `$XDG_CONFIG_HOME/board/claims/` (default `~/.config/board/claims`), directory mode 0700; one file per
(board, task, profile): `sha256(json.dumps([board, task, profile], ensure_ascii=False, separators=(",", ":")))
.hexdigest()[:32] + ".json"`, mode 0600, content `{board, task, profile, epoch, token, until}`, written through
`<file>.<pid>.<uuid>.tmp` then `os.replace`. Files whose `until` is older than 24 h are pruned on every put.

The token is a secret: nothing here logs or returns it except `get()` to the caller that owns the claim.
Used by vboard.cli and vboard.port (LocalEngineAdapter); owned by vb-store so both import one implementation.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping

from .model import iso, parse_iso

PRUNE_AFTER_MS = 24 * 3600 * 1000


def claims_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or ""
    root = Path(base) if base else Path(env.get("HOME") or os.path.expanduser("~")) / ".config"
    return root / "board" / "claims"


def board_key(db_path: str | os.PathLike | None = None, url: str | None = None) -> str:
    """`--url` value, or `file://<absolute db path>` for a local store."""
    if url:
        return url
    if db_path is None:
        raise ValueError("board_key needs a url or a db path")
    return "file://" + os.path.abspath(os.fspath(db_path))


def claim_key(board: str, task: str, profile: str | None) -> str:
    prof = profile.lower() if profile else None
    payload = json.dumps([board, str(task), prof], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ClaimCache:
    def __init__(self, directory: str | os.PathLike | None = None, *, env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.time):
        self.dir = Path(directory) if directory is not None else claims_dir(env)
        self._clock = clock

    # -- paths
    def path(self, board: str, task: str, profile: str | None) -> Path:
        return self.dir / (claim_key(board, task, profile) + ".json")

    def _ensure_dir(self) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass

    # -- operations
    def put(self, board: str, task: str, profile: str | None, epoch: int, token: str, until: str | int) -> Path:
        """Store a claim proof. `until` is an ISO string (as returned by the store) or epoch milliseconds."""
        until_iso = iso(until) if isinstance(until, int) else str(until)
        self._ensure_dir()
        prof = profile.lower() if profile else None
        doc = {"board": board, "task": str(task), "profile": prof, "epoch": int(epoch), "token": token,
               "until": until_iso}
        target = self.path(board, task, prof)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, ensure_ascii=False))
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.prune()
        return target

    def get(self, board: str, task: str, profile: str | None) -> dict | None:
        """Return the stored doc when the three keys match, else None."""
        prof = profile.lower() if profile else None
        try:
            doc = json.loads(self.path(board, task, prof).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        if doc.get("board") != board or str(doc.get("task")) != str(task) or doc.get("profile") != prof:
            return None
        return doc

    def drop(self, board: str, task: str, profile: str | None) -> bool:
        try:
            self.path(board, task, profile).unlink()
            return True
        except OSError:
            return False

    def prune(self, now: float | None = None) -> int:
        """Delete claim files whose `until` is more than 24 h in the past (and unreadable ones that old)."""
        now_ms = int((self._clock() if now is None else now) * 1000)
        removed = 0
        try:
            names = [p for p in self.dir.iterdir() if p.suffix == ".json"]
        except OSError:
            return 0
        for path in names:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                until_ms = parse_iso(doc["until"])
            except Exception:
                try:
                    stale = now_ms - int(path.stat().st_mtime * 1000) > PRUNE_AFTER_MS
                except OSError:
                    continue
                if not stale:
                    continue
                until_ms = 0
            if now_ms - until_ms > PRUNE_AFTER_MS:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


__all__ = ["ClaimCache", "claims_dir", "board_key", "claim_key", "PRUNE_AFTER_MS"]
