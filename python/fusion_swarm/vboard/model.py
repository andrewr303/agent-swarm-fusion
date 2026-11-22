"""Constants, errors, caller identity, validators and view builders for the vboard engine.

Frozen cross-module interface (design/v4-spec.md section 0.5 / 1.2). Other vboard modules (cli, http, port,
conformance, mcp) import from here and never redefine these names. Stdlib only.
"""
from __future__ import annotations

import getpass
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------------------------- constants
TEXT_MAX = 10_000
LABEL_KEY_RE = r"^[a-z0-9][a-z0-9_.-]*$"
LABEL_KEY_MAX = 64
LABEL_VALUE_MAX = 256
CHANNEL_SEG_RE = r"[a-z0-9][a-z0-9_-]*"
CHANNEL_MAX = 2048
ID_MAX = 128
CLIENT_ID_MAX = 200
EMOJI_RE = r"^[a-z0-9_+-]+$"
FORBIDDEN_KEYS = {"constructor", "__proto__", "prototype"}
PROFILE_RE = r"^[A-Za-z][A-Za-z0-9-]{0,31}$"
BLOCK_KIND_RE = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
TASK_STATES = ("waiting", "ready", "claimed", "blocked", "completed", "failed", "canceled")
OPEN_STATES = ("waiting", "ready", "claimed", "blocked")
FINISHED_STATES = ("completed", "failed", "canceled")
TASK_NEEDS_MAX = 20
TASK_BATCH_MAX = 100
TASK_KEY_MAX = 64
TASK_NOTE_MAX = 1000
NEEDS_WALK_MAX = 500
FROM_MAX = 32
LEASE_MIN_S = 30
LEASE_DEFAULT_S = 900
LEASE_CAP_S = 3600
CLAIM_WAIT_MAX_S = 300
EVENTS_WAIT_MAX_S = 300
EVENTS_LAST_MAX = 100
READ_LIMIT_DEFAULT = 50
READ_LIMIT_MAX = 500
FEED_SCAN = 200
HIDDEN_EVENTS = {"actor.seen", "thread.synced", "message.synced", "message.files_synced", "task.renewed"}
FUSION_LABELS = {"priority": "fusion.priority", "defer_until": "fusion.defer_until", "ack_due": "fusion.ack_due"}

PERMISSIONS = ("read", "post", "react", "label", "manage")
RESERVED_PREFIX = "board."
TOKEN_RE = r"^[A-Za-z0-9_-]{16,128}$"
ASSIGN_PROFILE_RE = r"^[a-z][a-z0-9-]{0,31}$"

_LABEL_KEY = re.compile(LABEL_KEY_RE)
_CHANNEL_SEG = re.compile(CHANNEL_SEG_RE)
_PROFILE = re.compile(PROFILE_RE)
_EMOJI = re.compile(EMOJI_RE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------------------------------- errors
@dataclass(frozen=True)
class Reason:
    status: int
    cls: str
    retryable: bool = False


def _reasons() -> dict[str, Reason]:
    table: dict[str, Reason] = {}

    def add(codes: str, status: int, cls: str, retryable: bool = False) -> None:
        for code in codes.split():
            table[code] = Reason(status, cls, retryable)

    add("invalid_request invalid_cursor invalid_after invalid_needs invalid_from invalid_profile reserved_label "
        "batch_too_large text_too_long message_deleted", 400, "usage")
    add("unauthorized", 401, "auth")
    add("forbidden task_not_owned", 403, "permission")
    add("message_not_found not_a_task post_not_found batch_not_found not_found channel_not_found", 404, "not_found")
    add("duplicate_id label_conflict revision_conflict newer_message already_a_task task_claimed task_assigned "
        "task_waiting task_blocked task_finished claim_lost", 409, "conflict")
    add("too_large", 413, "limit")
    add("rate_limited", 429, "rate_limit", True)
    add("internal", 500, "platform", True)
    add("not_supported no_tasks", 501, "platform")
    add("unavailable", 503, "network", True)
    # CLI-only reasons (not produced by the store); statuses are the nearest HTTP analogue.
    add("nothing_ready", 404, "not_found")
    add("timeout", 408, "timeout")
    add("claim_not_held", 409, "conflict")
    add("network_error", 503, "network", True)
    add("unknown_command unknown_option missing_arguments conflicting_options", 400, "usage")
    add("unexpected_error", 500, "unknown")
    return table


REASONS: dict[str, Reason] = _reasons()


class VBoardError(Exception):
    """Stable-coded error. `code` is snake_case; messages never reveal other callers' secrets."""

    def __init__(self, code: str, message: str, *, status: int | None = None, hint: str | None = None,
                 retry_after: float | None = None, data: dict | None = None):
        super().__init__(message)
        reason = REASONS.get(code)
        self.code = code
        self.message = message
        self.status = int(status) if status is not None else (reason.status if reason else 500)
        self.hint = hint
        self.retry_after = retry_after
        self.data = dict(data or {})

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"

    @property
    def retryable(self) -> bool:
        reason = REASONS.get(self.code)
        return bool(reason.retryable) if reason else False

    def to_http(self) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        return {"error": err}

    def to_failure(self, argv: list[str] | None = None) -> dict:
        """CLI failure document (spec 2.4). `argv` is accepted for call-site symmetry and is not embedded."""
        inner: dict[str, Any] = {"code": self.code, "message": self.message, "status": self.status}
        if self.retry_after is not None:
            inner["retryAfter"] = self.retry_after
        doc: dict[str, Any] = {
            "status": "action_required" if (self.code == "invalid_profile" and self.data.get("action_required"))
            else "error",
            "reason": self.code,
            "message": self.message,
            "next": list(self.data.get("next") or []),
        }
        if self.hint:
            doc["hint"] = self.hint
        doc["data"] = {"error": inner, "retryable": self.retryable}
        return doc


# --------------------------------------------------------------------------------------------- time
def iso(ms: int) -> str:
    ms = int(ms)
    secs, milli = divmod(ms, 1000)
    stamp = datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{milli:03d}Z"


def parse_iso(text: str) -> int:
    if not isinstance(text, str) or not text:
        raise VBoardError("invalid_request", "expected an ISO-8601 time")
    raw = text.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00") if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise VBoardError("invalid_request", f"not an ISO-8601 time: {text[:40]!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
_UNIT = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str | int | float) -> float:
    """'30s', '10m', '1h', '1d' or bare seconds -> seconds (float)."""
    if isinstance(text, bool):
        raise VBoardError("invalid_request", "a duration is a number of seconds or like 30s, 10m, 1h")
    if isinstance(text, (int, float)):
        value = float(text)
    else:
        m = _DURATION.match(str(text).strip().lower())
        if not m:
            raise VBoardError("invalid_request", f"{str(text)[:40]!r} isn't a duration: use 30s, 10m, 1h or seconds")
        value = float(m.group(1)) * _UNIT[m.group(2)]
    if value < 0 or value != value:
        raise VBoardError("invalid_request", "a duration can't be negative")
    return value


# --------------------------------------------------------------------------------------------- identity
def normalize_profile(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if not _PROFILE.match(text):
        raise VBoardError("invalid_profile",
                          "a profile starts with a letter and has up to 32 letters, digits or hyphens")
    return text.lower()


@dataclass(frozen=True)
class Caller:
    principal: str
    profile: str | None = None
    can: frozenset[str] | None = None

    @property
    def actor(self) -> str:
        return self.principal if not self.profile else f"{self.principal}+{self.profile}"

    @property
    def handle(self) -> str:
        user = self.principal.rsplit(":", 1)[-1]
        return user if not self.profile else f"{user}/{self.profile}"

    @classmethod
    def local(cls, profile: str | None = None, *, user: str | None = None) -> "Caller":
        name = user if user is not None else _os_user()
        name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "user"
        return cls(principal=f"local:user:{name}", profile=normalize_profile(profile))


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - environment dependent
        return "user"


# --------------------------------------------------------------------------------------------- validators
def channel_ok(path: str) -> bool:
    if not isinstance(path, str) or not path or len(path) > CHANNEL_MAX:
        return False
    return all(_CHANNEL_SEG.fullmatch(seg) for seg in path.split("/"))


def label_ok(key: str, value: str | None = None, *, allow_reserved: bool = False) -> None:
    """Validate a label key (and value when not None). Raises invalid_request / reserved_label."""
    if not isinstance(key, str) or not key or len(key) > LABEL_KEY_MAX or not _LABEL_KEY.match(key):
        raise VBoardError("invalid_request", f"label key {str(key)[:40]!r}: lowercase letters, digits, _ . - "
                                             f"up to {LABEL_KEY_MAX} characters")
    if key in FORBIDDEN_KEYS:
        raise VBoardError("invalid_request", f"label key {key!r} isn't allowed")
    if key.startswith(RESERVED_PREFIX) and not allow_reserved:
        raise VBoardError("reserved_label", "labels that start with board. belong to the board itself")
    if value is not None:
        if not isinstance(value, str) or len(value) > LABEL_VALUE_MAX or _CONTROL.search(value):
            raise VBoardError("invalid_request",
                              f"label {key}: a value is text of up to {LABEL_VALUE_MAX} characters, no control characters")


def emoji_ok(raw: str) -> str:
    name = str(raw or "").strip().strip(":").lower()
    if not name or not _EMOJI.match(name) or name in FORBIDDEN_KEYS:
        raise VBoardError("invalid_request", "an emoji is a short name like eyes or white_check_mark")
    return name


def lease_ok(lease: Any, *, default_s: int, cap_s: int) -> int:
    """Validate a lease in seconds (number in [LEASE_MIN_S, cap_s]) and return whole seconds; None -> default."""
    if lease is None:
        return int(default_s)
    if isinstance(lease, bool) or not isinstance(lease, (int, float)) or lease != lease:
        raise VBoardError("invalid_request", f"lease: a lease is {LEASE_MIN_S} to {cap_s} seconds")
    if lease < LEASE_MIN_S or lease > cap_s:
        raise VBoardError("invalid_request", f"lease: a lease is {LEASE_MIN_S} to {cap_s} seconds")
    return int(lease) if float(lease).is_integer() else int(round(lease))


# --------------------------------------------------------------------------------------------- views
def actor_view(row: Mapping[str, Any]) -> dict:
    principal = row["principal"]
    profile = row["profile"]
    via = None
    if profile:
        via = principal.rsplit(":", 1)[-1]
    return {
        "id": row["id"], "kind": row["kind"], "handle": row["handle"], "name": row["name"],
        "avatar": row["avatar"], "via": via, "verified": False,
    }


def task_view(*, state: str, owner: str, by: str, for_: str | None, needs: Sequence[str], epoch: int,
              claim: dict | None, blocked: dict | None, result: str | None, reason: str | None,
              needs_if: Mapping[str, str] | None = None) -> dict:
    view: dict[str, Any] = {
        "state": state, "owner": owner, "by": by, "for": for_, "needs": list(needs), "epoch": int(epoch),
        "claim": claim, "blocked": blocked, "result": result, "reason": reason,
    }
    if needs_if:
        view["needsIf"] = dict(needs_if)
    return view


def message_view(row: Mapping[str, Any], labels: Mapping[str, str], reactions: Mapping[str, Sequence[str]],
                 task: dict | None, author: dict) -> dict:
    """Message view with the exact vercel field names. `author` is `actor_view(...)` output."""
    deleted = bool(row["deleted"])
    is_root = row["root_id"] == row["id"]
    view: dict[str, Any] = {
        "id": row["id"],
        "channel": row["channel"],
        "text": "" if deleted else row["text"],
        "re": None if is_root else row["root_id"],
        "replyTo": row["reply_to"],
        "thread": row["root_id"],
        "labels": {} if deleted else dict(labels),
        "reactions": {} if deleted else {k: list(v) for k, v in reactions.items()},
        "by": {"actor": row["actor"], "principal": row["principal"], "profile": row["profile"]},
        "noSync": bool(row["no_sync"]),
        "origin": None,
        "from": None,
        "createdAt": iso(row["created_ms"]),
        "editedAt": iso(row["edited_ms"]) if row["edited_ms"] is not None else None,
        "deleted": deleted,
        "undelivered": {},
        "attachments": [],
        "links": [],
        "version": int(row["version"]),
        "revision": int(row["revision"]),
        "author": author,
    }
    if task is not None:
        view["task"] = task
    view["blocks"] = []
    return view


def thread_summary(message: dict, replies: int, last: dict | None) -> dict:
    return {"message": message, "replies": int(replies), "last": last}


def event_view(*, cursor: str, type: str, created_ms: int, payload: dict, message: dict | None,
               actor: dict | None) -> dict:
    out_actor = None
    if actor is not None:
        out_actor = {k: actor[k] for k in ("id", "kind", "handle", "name", "avatar")}
    return {"cursor": cursor, "type": type, "createdAt": iso(created_ms), "payload": payload,
            "message": message, "actor": out_actor}
