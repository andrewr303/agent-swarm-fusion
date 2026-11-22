"""`python -m fusion_swarm.vboard`: a `vercel board`-compatible CLI over BoardStore (design/v4-spec.md section 2).

Verb names, flags and `--json` field names follow the real CLI (b3-cli.md sections 3-5). The claim cache is the shared
vboard.claims layout (judge J-06), so a claim made here can be settled by the stock CLI and the other way round.
`serve` and `conformance` import vboard.http / vboard.conformance lazily and fail `not_supported` (exit 2) when absent.

Test hooks (judge J-07): `main(argv, *, stdout, stderr, env, store_factory, clock, sleep, renew_floor_s)`.
  store_factory(path, name) -> store   (default BoardStore(path, name=name))
  clock() -> seconds since epoch       (renew interval and wait/claim deadlines; also the claim-cache clock)
  sleep(seconds)                       (the `tasks run` renew loop waits through it)
Success output is one compact JSON document (pretty when stdout is a TTY) with or without --json; the claim token is
never printed. Failures: see `_render_failure`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import VERSION
from .claims import ClaimCache, board_key
from .model import REASONS, Caller, VBoardError, normalize_profile, parse_duration, parse_iso
from .store import BoardStore

PROG = "python -m fusion_swarm.vboard"
TASK_SUBS = ("create", "claim", "run", "renew", "release", "complete", "fail", "block", "unblock", "reopen",
             "assign", "needs", "cancel")
UNSUPPORTED = ("grants", "schemas", "schedule", "schedules", "typing", "sync", "inbox", "sources", "download",
               "hooks", "inspect")
SLICE_S = 25.0
_ID = re.compile(r"^#?\d+$")

# --------------------------------------------------------------------------------------------- option tables
# kind: flag | value | multi | opt (optional value, only `--opt=v` or a value that looks like an actor)
GLOBAL = {"--db": "value", "--url": "value", "--name": "value", "--as": "value", "--json": "flag",
          "--version": "flag", "--help": "flag"}
ALIAS = {"-v": "--version", "-h": "--help", "-n": "--limit", "-a": "--all"}
FILTERS = {"--re": "value", "--by": "value", "--mentions": "opt", "--label": "multi", "--source": "value",
           "--text": "value", "--after": "value"}
CLAIMING = {"--label": "multi", "--from": "multi", "--wait": "value", "--lease": "value", "--ready": "flag"}
VERB_OPTS: dict[str, dict[str, str]] = {
    "whoami": {}, "actors": {},
    "channels": {"--limit": "value", "--after": "value", "--private": "flag"},
    "read": {**FILTERS, "--before": "value", "--limit": "value"},
    "post": {"--label": "multi", "--file": "multi", "--no-sync": "flag", "--idempotency-key": "value",
             "--after": "value"},
    "edit": {"--if-revision": "value"}, "delete": {"--if-revision": "value"}, "react": {"--remove": "flag"},
    "label": {"--unset": "multi", "--if": "multi", "--if-unset": "multi"},
    "tail": {**FILTERS, "--all": "flag", "--last": "value"},
    "wait": {**FILTERS, "--timeout": "value"},
    "tasks": {"--state": "value", "--label": "multi", "--by": "value", "--for": "value", "--after": "value",
              "--limit": "value"},
    "tasks create": {"--label": "multi", "--needs": "multi", "--needs-if-failed": "multi", "--for": "value",
                     "--claim": "flag", "--lease": "value", "--no-sync": "flag", "--idempotency-key": "value",
                     "--file": "value"},
    "tasks claim": dict(CLAIMING),
    "tasks run": {**CLAIMING, "--result": "value"},
    "tasks renew": {"--lease": "value"},
    "tasks block": {"--kind": "value"},
    "tasks assign": {"--pool": "flag"},
    "tasks needs": {"--none": "flag"},
    "serve": {"--port": "value", "--host": "value", "--no-auth": "flag", "--mount": "value"},
    "sweep": {},
    "conformance": {"--channel": "value", "--group": "multi"},
}
for _sub in ("release", "complete", "fail", "unblock", "reopen", "cancel"):
    VERB_OPTS[f"tasks {_sub}"] = {}
VERB_OPTS["reply"] = VERB_OPTS["post"]
VERB_OPTS["rm"] = VERB_OPTS["del"] = VERB_OPTS["delete"]


def _usage(code: str, message: str, hint: str | None = None) -> VBoardError:
    return VBoardError(code, message, hint=hint)


def _norm(name: str) -> str:
    return ALIAS.get(name, name)


def _parse(tokens: Sequence[str], spec: Mapping[str, str]) -> tuple[dict, list[str], list[str] | None]:
    """-> (options by dest, positionals, tokens after `--` or None)."""
    opts: dict[str, Any] = {}
    pos: list[str] = []
    rest: list[str] | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            rest = list(tokens[i + 1:])
            break
        if tok.startswith("-") and tok != "-" and not re.match(r"^-\d", tok):
            name, eq, val = tok.partition("=") if tok.startswith("--") else (tok, "", "")
            name = _norm(name)
            kind = spec.get(name)
            if kind is None:
                raise _usage("unknown_option", f"Unknown option {name}.", f"Run `{PROG} --help`.")
            dest = name.lstrip("-").replace("-", "_")
            if kind == "flag":
                if eq:
                    raise _usage("invalid_request", f"{name} doesn't take a value.")
                opts[dest] = True
            elif kind == "opt":
                if eq:
                    opts[dest] = val
                elif i + 1 < len(tokens) and re.match(r"^[@+][^\s]*$|^[a-z0-9._-]+:[^\s]+$", tokens[i + 1]):
                    i += 1
                    opts[dest] = tokens[i]
                else:
                    opts[dest] = "me"
            else:
                if not eq:
                    if i + 1 >= len(tokens):
                        raise _usage("missing_arguments", f"{name} needs a value.")
                    i += 1
                    val = tokens[i]
                if kind == "multi":
                    opts.setdefault(dest, []).append(val)
                else:
                    opts[dest] = val
        else:
            pos.append(tok)
        i += 1
    return opts, pos, rest


def _split_verb(argv: Sequence[str]) -> tuple[list[str], str | None, list[str]]:
    pre: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("-") and tok != "-" and tok != "--":
            pre.append(tok)
            if GLOBAL.get(_norm(tok.split("=", 1)[0])) == "value" and "=" not in tok and i + 1 < len(argv):
                i += 1
                pre.append(argv[i])
            i += 1
            continue
        break
    verb = argv[i] if i < len(argv) else None
    return pre, verb, list(argv[i + 1:])


# --------------------------------------------------------------------------------------------- small helpers
def _mid(raw: str) -> str:
    text = str(raw).strip().lstrip("#")
    if not text:
        raise _usage("missing_arguments", "A message id is needed.")
    return text


def _is_id(raw: str) -> bool:
    return bool(_ID.match(raw))


def _int(opts: Mapping, name: str, minimum: int | None = None) -> int | None:
    if name not in opts:
        return None
    try:
        value = int(str(opts[name]).strip())
    except ValueError:
        raise _usage("invalid_request", f"--{name.replace('_', '-')} takes a whole number.") from None
    if minimum is not None and value < minimum:
        raise _usage("invalid_request", f"--{name.replace('_', '-')} must be at least {minimum}.")
    return value


def _dur(opts: Mapping, name: str) -> float | None:
    if name not in opts:
        return None
    try:
        return parse_duration(opts[name])
    except VBoardError as exc:
        raise _usage("invalid_request", f"--{name}: {exc.message}") from None


def _pairs(items: Sequence[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or ():
        key, eq, val = item.partition("=")
        if not eq or not key:
            raise _usage("invalid_request", f"{what} takes key=value, not {item[:40]!r}.")
        out[key] = val
    return out


def _lease_arg(opts: Mapping) -> float | None:
    """`--lease` accepts durations (300s, 5m) and bare seconds (A1.3)."""
    return _dur(opts, "lease")


def _text_arg(pos: Sequence[str], required: bool = True) -> str | None:
    if not pos:
        if required:
            raise _usage("missing_arguments", "Text is needed: pass it as an argument or `-` to read stdin.")
        return None
    text = " ".join(pos)
    if text == "-":
        text = sys.stdin.read()
    return text


# --------------------------------------------------------------------------------------------- HTTP store
class _HttpStore:
    """Minimal urllib client with BoardStore's method names, for `--url` (a loopback vb serve or any board that
    speaks the protocol). The caller argument is ignored: identity travels in `x-board-profile`."""

    def __init__(self, url: str, profile: str | None, env: Mapping[str, str], timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.profile = profile
        self.timeout = timeout
        self.name = None
        token = env.get("BOARD_TOKEN") or ""
        if not token:
            base = env.get("XDG_CONFIG_HOME") or ""
            root = Path(base) if base else Path(env.get("HOME") or os.path.expanduser("~")) / ".config"
            digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:32]
            try:
                token = json.loads((root / "board" / "local" / f"{digest}.json").read_text("utf-8")).get("token") or ""
            except (OSError, ValueError):
                token = ""
        self._token = token

    def close(self) -> None:
        pass

    def _call(self, method: str, path: str, body: Mapping | None = None, query: Mapping | None = None,
              hold: float = 0.0) -> dict:
        url = self.url + path
        if query:
            pairs = [(k, str(v)) for k, vs in query.items() if vs is not None
                     for v in (vs if isinstance(vs, (list, tuple)) else [vs])]
            if pairs:
                url += "?" + urllib.parse.urlencode(pairs)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"accept": "application/json"}
        if data is not None:
            headers["content-type"] = "application/json"
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        if self.profile:
            headers["x-board-profile"] = self.profile
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout + hold) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8")).get("error") or {}
            except Exception:
                err = {}
            retry = exc.headers.get("Retry-After") if exc.headers else None
            raise VBoardError(str(err.get("code") or _status_code(exc.code)), str(err.get("message") or exc.reason),
                              status=exc.code, hint=err.get("hint"),
                              retry_after=float(retry) if retry and retry.replace(".", "", 1).isdigit() else None
                              ) from None
        except (urllib.error.URLError, OSError) as exc:
            raise VBoardError("network_error", f"couldn't reach {self.url}: {getattr(exc, 'reason', exc)}") from None
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise VBoardError("unexpected_error", "the board answered with something that isn't JSON") from None

    @staticmethod
    def _t(task_id: Any) -> str:
        return urllib.parse.quote(str(task_id), safe="")

    def me(self, caller=None) -> dict:
        me = self._call("GET", "/me")
        try:
            disco = self._call("GET", "/")
        except VBoardError:
            disco = {}
        me.setdefault("board", disco.get("name") or self.url)
        me.setdefault("source", "http")
        return me

    def actors(self, caller=None, ids=()) -> dict:
        return self._call("GET", "/actors", query={"id": list(ids)})

    def channels(self, caller=None, *, prefix=None, after=None, limit=500) -> dict:
        return self._call("GET", "/channels", query={"prefix": prefix, "after": after, "limit": limit})

    def create_channel(self, caller, path, *, private=False) -> dict:
        return self._call("POST", "/channels", {"path": path, **({"private": True} if private else {})})

    def head(self) -> str:
        return self.events(None, wait=0)["cursor"]

    def post(self, caller, *, text, channel=None, re=None, labels=None, client_id=None, after=None,
             no_sync=False) -> dict:
        body: dict[str, Any] = {"text": text}
        if re is not None:
            body["re"] = re
        else:
            body["channel"] = channel
        for key, value in (("labels", labels), ("id", client_id), ("after", after)):
            if value:
                body[key] = value
        if no_sync:
            body["noSync"] = True
        return self._call("POST", "/messages", body)

    def thread(self, caller, message_id, *, after=None, limit=None, label=()) -> dict:
        return self._call("GET", f"/messages/{self._t(message_id)}",
                          query={"after": after, "limit": limit, "label": list(label)})

    def threads(self, caller, *, channel=None, re=None, by=None, mentions=None, label=(), source=None, text=None,
                after=None, before=None, limit=50) -> dict:
        return self._call("GET", "/threads", query={"channel": channel, "re": re, "by": by, "mentions": mentions,
                                                    "label": list(label), "from": source, "text": text,
                                                    "after": after, "before": before, "limit": limit})

    def edit(self, caller, message_id, text, *, revision=None) -> dict:
        return self._call("POST", f"/messages/{self._t(message_id)}/edit",
                          {"text": text, **({"revision": revision} if revision is not None else {})})

    def delete(self, caller, message_id, *, revision=None) -> dict:
        return self._call("POST", f"/messages/{self._t(message_id)}/delete",
                          {"revision": revision} if revision is not None else {})

    def react(self, caller, message_id, emoji, *, remove=False) -> dict:
        return self._call("POST", f"/messages/{self._t(message_id)}/reactions",
                          {"emoji": emoji, **({"remove": True} if remove else {})})

    def label(self, caller, message_id, *, set=None, unset=(), if_=None) -> dict:
        body: dict[str, Any] = {}
        if set:
            body["set"] = set
        if unset:
            body["unset"] = list(unset)
        if if_:
            body["if"] = if_
        return self._call("POST", f"/messages/{self._t(message_id)}/labels", body)

    def events(self, caller, *, after=None, last=None, wait=0.0, all=False, channel=None, re=None, by=None,
               mentions=None, label=(), source=None, text=None, should_stop=None) -> dict:
        return self._call("GET", "/events", hold=float(wait),
                          query={"after": after, "last": last, "wait": wait or None, "all": "true" if all else None,
                                 "channel": channel, "re": re, "by": by, "mentions": mentions,
                                 "label": list(label), "from": source, "text": text})

    def create_task(self, caller, *, channel=None, text=None, re=None, labels=None, needs=(), for_=None,
                    claim=False, lease=None, token=None, client_id=None, no_sync=False) -> dict:
        body: dict[str, Any] = {"re": re} if re is not None else {"channel": channel, "text": text}
        for key, value in (("labels", labels), ("needs", list(needs)), ("for", for_), ("lease", lease),
                           ("token", token), ("id", client_id)):
            if value:
                body[key] = value
        if claim:
            body["claim"] = True
        if no_sync:
            body["noSync"] = True
        return self._call("POST", "/tasks", body)

    def create_tasks(self, caller, channel, tasks, *, client_id=None) -> dict:
        return self._call("POST", "/tasks/batch", {"channel": channel, "tasks": list(tasks),
                                                   **({"id": client_id} if client_id else {})})

    def list_tasks(self, caller, *, channel=None, state="open", label=(), by=None, owner=None, for_=None,
                   needs=None, after=None, limit=50, latest=False) -> dict:
        return self._call("GET", "/tasks", query={"channel": channel, "state": state, "label": list(label),
                                                  "by": by, "owner": owner, "for": for_, "needs": needs,
                                                  "after": after, "limit": limit,
                                                  "latest": "true" if latest else None})

    def claim(self, caller, task_id, *, lease=None, token=None) -> dict:
        return self._call("POST", f"/tasks/{self._t(task_id)}/claim",
                          {k: v for k, v in (("lease", lease), ("token", token)) if v is not None})

    def claim_next(self, caller, channel, *, label=None, from_=(), lease=None, wait=0.0, token=None,
                   should_stop=None) -> dict:
        body: dict[str, Any] = {"channel": channel}
        for key, value in (("label", label), ("from", list(from_)), ("lease", lease), ("wait", wait or None),
                           ("token", token)):
            if value:
                body[key] = value
        return self._call("POST", "/tasks/claim", body, hold=float(wait or 0))

    def _settle(self, task_id, verb, body, outcome) -> dict:
        res = self._call("POST", f"/tasks/{self._t(task_id)}/{verb}", body)
        res.setdefault("outcome", outcome)
        return res

    def renew(self, caller, task_id, *, epoch, token, lease=None) -> dict:
        return self._settle(task_id, "renew", {"epoch": epoch, "token": token,
                                               **({"lease": lease} if lease is not None else {})}, "renewed")

    def release(self, caller, task_id, *, epoch, token) -> dict:
        return self._settle(task_id, "release", {"epoch": epoch, "token": token}, "released")

    def complete(self, caller, task_id, *, epoch, token, result=None) -> dict:
        return self._settle(task_id, "complete", {"epoch": epoch, "token": token,
                                                  **({"result": result} if result is not None else {})},
                            "completed")

    def fail(self, caller, task_id, *, epoch, token, reason) -> dict:
        return self._settle(task_id, "fail", {"epoch": epoch, "token": token, "reason": reason}, "failed")

    def block(self, caller, task_id, *, reason, kind=None, epoch=None, token=None) -> dict:
        body: dict[str, Any] = {"reason": reason}
        if kind:
            body["kind"] = kind
        if epoch is not None:
            body.update(epoch=epoch, token=token)
        return self._settle(task_id, "block", body, "blocked")

    def unblock(self, caller, task_id) -> dict:
        return self._settle(task_id, "unblock", {}, "unblocked")

    def reopen(self, caller, task_id) -> dict:
        return self._settle(task_id, "reopen", {}, "reopened")

    def assign(self, caller, task_id, for_) -> dict:
        return self._settle(task_id, "assign", {"for": for_}, "assigned" if for_ else "pooled")

    def set_needs(self, caller, task_id, needs) -> dict:
        return self._settle(task_id, "needs", {"needs": list(needs)}, "set")

    def cancel(self, caller, task_id, *, reason) -> dict:
        return self._settle(task_id, "cancel", {"reason": reason}, "canceled")

    def sweep(self, now=None) -> dict:
        raise VBoardError("not_supported", "sweep runs inside the board that owns the data, not over --url")


def _status_code(status: int) -> str:
    return {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
            413: "too_large", 429: "rate_limited", 501: "not_supported", 503: "unavailable"}.get(status, "internal")


# --------------------------------------------------------------------------------------------- context
class _Ctx:
    def __init__(self, *, env: Mapping[str, str], stdout, stderr, json_mode: bool, clock, sleep, renew_floor_s,
                 store_factory):
        self.env = env
        self.stdout = stdout
        self.stderr = stderr
        self.json = json_mode
        self.clock = clock
        self.sleep = sleep
        self.renew_floor_s = renew_floor_s
        self.store_factory = store_factory
        self.opts: dict = {}
        self.profile: str | None = None
        self.caller: Caller | None = None
        self.url: str | None = None
        self.db_path: str | None = None
        self.name: str | None = None
        self._store = None
        self._claims: ClaimCache | None = None
        self.board = ""

    # -- board selection and identity
    def configure(self, opts: Mapping) -> None:
        env = self.env
        self.opts = dict(opts)
        flag_url, flag_db = opts.get("url"), opts.get("db")
        if flag_url and flag_db:
            raise _usage("conflicting_options", "--url and --db choose different boards: use one.")
        if flag_url and opts.get("name"):
            raise _usage("conflicting_options", "--url and --name choose different boards: use one.")
        self.name = opts.get("name") or env.get("BOARD_NAME") or None
        if flag_url or (not flag_db and env.get("BOARD_URL") and not env.get("FUSION_VBOARD_DB")):
            self.url = str(flag_url or env.get("BOARD_URL")).rstrip("/")
        else:
            home = env.get("FUSION_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".fusion")
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", self.name or "default") or "default"
            self.db_path = os.path.abspath(str(flag_db or env.get("FUSION_VBOARD_DB")
                                               or os.path.join(home, "vboard", f"{safe}.db")))
        raw = opts.get("as")
        action = False
        if raw is not None:
            if not str(raw).strip():
                action = True
            profile = raw
        elif env.get("BOARD_PROFILE"):
            profile = env["BOARD_PROFILE"]
        elif env.get("FUSION_AGENT_ID"):
            profile = re.sub(r"[^A-Za-z0-9-]+", "-", env["FUSION_AGENT_ID"]).strip("-")[:32]
            if profile and not profile[0].isalpha():
                profile = ("a" + profile)[:32]
        else:
            profile = None
        if action:
            raise VBoardError("invalid_profile", "--as needs a profile name.", data={"action_required": True},
                              hint="Pass --as <name> (a letter, then letters, digits or hyphens).")
        try:
            self.profile = normalize_profile(profile)
        except VBoardError as exc:
            raise VBoardError("invalid_profile", exc.message, hint="Pass --as <name>.") from None
        self.caller = Caller.local(self.profile)
        self.board = board_key(db_path=self.db_path, url=self.url)

    @property
    def store(self):
        if self._store is None:
            if self.url:
                self._store = _HttpStore(self.url, self.profile, self.env)
            elif self.store_factory is not None:
                self._store = self.store_factory(self.db_path, self.name)
            else:
                self._store = BoardStore(self.db_path, name=self.name)
        return self._store

    @property
    def claims(self) -> ClaimCache:
        if self._claims is None:
            self._claims = ClaimCache(env=self.env, clock=self.clock)
        return self._claims

    def close(self) -> None:
        if self._store is not None:
            try:
                self._store.close()
            except Exception:
                pass

    # -- output
    def emit(self, doc: Any) -> None:
        pretty = False
        try:
            pretty = bool(self.stdout.isatty())
        except Exception:
            pass
        self.stdout.write(json.dumps(doc, ensure_ascii=False, indent=2 if pretty else None) + "\n")
        try:
            self.stdout.flush()
        except Exception:
            pass


def _view_id(res: Mapping, fallback: str) -> str:
    msg = res.get("message") if isinstance(res, Mapping) else None
    return str((msg or {}).get("id") or fallback)


# --------------------------------------------------------------------------------------------- verbs: board / messages
def v_whoami(ctx: _Ctx, opts, pos):
    return ctx.store.me(ctx.caller)


def v_actors(ctx, opts, pos):
    return {"actors": ctx.store.actors(ctx.caller)["actors"]}


def v_channels(ctx, opts, pos):
    if pos and pos[0] == "create":
        if len(pos) < 2:
            raise _usage("missing_arguments", "channels create needs a channel path.")
        res = ctx.store.create_channel(ctx.caller, pos[1], private=bool(opts.get("private")))
        return {"channel": res["channel"], "outcome": "created" if res.get("created") else "unchanged"}
    if pos and pos[0] == "list":
        pos = pos[1:]
    res = ctx.store.channels(ctx.caller, prefix=pos[0] if pos else None, after=opts.get("after"),
                             limit=_int(opts, "limit", 1) or 500)
    return {"channels": res["channels"], "next": res.get("next")}


def _mentions(ctx, opts) -> str | None:
    value = opts.get("mentions")
    return ctx.caller.actor if value == "me" else value


def v_read(ctx, opts, pos):
    target = pos[0] if pos else None
    if target is not None and _is_id(target):
        return ctx.store.thread(ctx.caller, _mid(target), after=opts.get("after"), limit=_int(opts, "limit", 1),
                                label=opts.get("label") or ())
    return ctx.store.threads(ctx.caller, channel=target, re=opts.get("re"), by=opts.get("by"),
                             mentions=_mentions(ctx, opts), label=opts.get("label") or (),
                             source=opts.get("source"), text=opts.get("text"), after=opts.get("after"),
                             before=opts.get("before"), limit=_int(opts, "limit", 1) or 50)


def _post(ctx, opts, pos, *, reply: bool):
    if not pos:
        raise _usage("missing_arguments", "Name a message to reply to." if reply else "Name a channel or message.")
    target, text_pos = pos[0], pos[1:]
    if opts.get("file"):
        raise VBoardError("not_supported", "attachments aren't kept by this board.")
    text = _text_arg(text_pos)
    labels = _pairs(opts.get("label") or (), "--label")
    kw: dict[str, Any] = {"text": text, "labels": labels or None, "client_id": opts.get("idempotency_key"),
                          "after": opts.get("after"), "no_sync": bool(opts.get("no_sync"))}
    if reply or _is_id(target):
        res = ctx.store.post(ctx.caller, re=_mid(target), **kw)
    else:
        res = ctx.store.post(ctx.caller, channel=target, **kw)
    out = {"message": res["message"], "outcome": "posted"}
    if res.get("mentions") is not None:
        out["mentions"] = res["mentions"]
    return out


def v_post(ctx, opts, pos):
    return _post(ctx, opts, pos, reply=False)


def v_reply(ctx, opts, pos):
    return _post(ctx, opts, pos, reply=True)


def v_edit(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "edit needs a message id and text.")
    mid = _mid(pos[0])
    res = ctx.store.edit(ctx.caller, mid, _text_arg(pos[1:]), revision=_int(opts, "if_revision"))
    return {"message": {"id": _view_id(res, mid)}, "outcome": "edited"}


def v_delete(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "delete needs a message id.")
    mid = _mid(pos[0])
    res = ctx.store.delete(ctx.caller, mid, revision=_int(opts, "if_revision"))
    return {"message": {"id": _view_id(res, mid)}, "outcome": "deleted"}


def v_react(ctx, opts, pos):
    if len(pos) < 2:
        raise _usage("missing_arguments", "react needs a message id and an emoji.")
    mid = _mid(pos[0])
    remove = bool(opts.get("remove"))
    res = ctx.store.react(ctx.caller, mid, pos[1], remove=remove)
    return {"message": {"id": _view_id(res, mid)}, "outcome": "unreacted" if remove else "reacted"}


def v_label(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "label needs a message id.")
    mid = _mid(pos[0])
    setting = _pairs(pos[1:], "label")
    unset = list(opts.get("unset") or ())
    cond: dict[str, str | None] = dict(_pairs(opts.get("if") or (), "--if"))
    for key in opts.get("if_unset") or ():
        cond[key] = None
    if not setting and not unset:
        raise _usage("missing_arguments", "label needs k=v pairs or --unset.")
    res = ctx.store.label(ctx.caller, mid, set=setting or None, unset=unset, if_=cond or None)
    return {"message": {"id": _view_id(res, mid)}, "outcome": "labeled"}


# --------------------------------------------------------------------------------------------- verbs: feed
def _feed_filters(ctx, opts) -> dict:
    return {"re": opts.get("re"), "by": opts.get("by"), "mentions": _mentions(ctx, opts),
            "label": opts.get("label") or (), "source": opts.get("source"), "text": opts.get("text")}


def v_wait(ctx, opts, pos):
    timeout = _dur(opts, "timeout")
    timeout = 1800.0 if timeout is None else timeout
    flt = _feed_filters(ctx, opts)
    after = opts.get("after")
    channel = None
    target = pos[0] if pos else None
    if target is not None:
        if _is_id(target):
            root = ctx.store.thread(ctx.caller, _mid(target), limit=1)["message"]["id"]
            flt["re"] = str(root)
            after = after or _mid(target)
        else:
            channel = target
    if after is None:
        after = ctx.store.events(ctx.caller, wait=0)["cursor"]
    deadline = ctx.clock() + timeout
    while True:
        remaining = deadline - ctx.clock()
        res = ctx.store.events(ctx.caller, after=after, wait=max(0.0, min(SLICE_S, remaining)), channel=channel,
                               **flt)
        if res["events"]:
            return {"event": res["events"][0]}
        after = res["cursor"]
        if deadline - ctx.clock() <= 0:
            raise VBoardError("timeout", f"Nothing matched within {opts.get('timeout', '30m')}.",
                              hint=f"Run again with --after {after}.")


def v_tail(ctx, opts, pos):
    channel = pos[0] if pos else None
    flt = _feed_filters(ctx, opts)
    show_all = bool(opts.get("all"))
    last = _int(opts, "last", 1)
    after = opts.get("after")

    def out(events):
        for event in events:
            ctx.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        ctx.stdout.flush()

    if last is not None:
        res = ctx.store.events(ctx.caller, last=last, all=show_all, channel=channel, **flt)
        out(res["events"])
        after = res["cursor"]
    elif after is None:
        after = ctx.store.events(ctx.caller, wait=0)["cursor"]
    while True:
        res = ctx.store.events(ctx.caller, after=after, wait=SLICE_S, all=show_all, channel=channel, **flt)
        out(res["events"])
        after = res["cursor"]


# --------------------------------------------------------------------------------------------- verbs: tasks
def v_tasks_list(ctx, opts, pos):
    res = ctx.store.list_tasks(ctx.caller, channel=pos[0] if pos else None, state=opts.get("state") or "open",
                               label=opts.get("label") or (), by=opts.get("by"), for_=opts.get("for"),
                               after=opts.get("after"), limit=_int(opts, "limit", 1) or 50)
    return {"tasks": res["tasks"], "cursor": res.get("cursor")}


def _put_claim(ctx: _Ctx, task_id: str, claim: Mapping) -> None:
    ctx.claims.put(ctx.board, str(task_id), ctx.profile, claim["epoch"], claim["token"], claim["until"])


def _claim_doc(claim: Mapping) -> dict:
    return {"epoch": claim["epoch"], "until": claim["until"]}


def _not_held(ctx: _Ctx, task_id: str) -> VBoardError:
    who = ctx.profile or "yourself"
    cmd = f"{PROG} tasks claim {task_id}" + (f" --as {ctx.profile}" if ctx.profile else "")
    argv = [sys.executable, "-m", "fusion_swarm.vboard", "tasks", "claim", task_id] + (
        ["--as", ctx.profile] if ctx.profile else [])
    return VBoardError("claim_not_held", f"You don't hold a claim on task #{task_id} as {who}.",
                       hint="Claims are kept per --as profile: use the same --as you claimed with.",
                       data={"next": [{"command": cmd, "argv": argv, "when": "Claim it"}]})


def _proof(ctx: _Ctx, task_id: str) -> dict:
    doc = ctx.claims.get(ctx.board, task_id, ctx.profile)
    if doc is None:
        raise _not_held(ctx, task_id)
    return doc


def _with_claim(ctx: _Ctx, task_id: str, fn: Callable[[dict], dict], *, drop: bool = True) -> dict:
    doc = _proof(ctx, task_id)
    try:
        res = fn(doc)
    except VBoardError as exc:
        if exc.code == "claim_lost":
            ctx.claims.drop(ctx.board, task_id, ctx.profile)
            exc.message = f"Lost the claim on task #{task_id}: {exc.message}"
        raise
    if drop:
        ctx.claims.drop(ctx.board, task_id, ctx.profile)
    return res


def _needs_args(opts) -> list:
    needs: list[Any] = [_mid(n) for n in opts.get("needs") or ()]
    needs += [{"id": _mid(n), "if": "failed"} for n in opts.get("needs_if_failed") or ()]
    return needs


def _rewrite_plan_needs(chunk: list[dict], keymap: Mapping[str, str], here: set[str], later: set[str]) -> list[dict]:
    out = []
    for item in chunk:
        if not isinstance(item, Mapping) or not item.get("needs"):
            out.append(item)
            continue
        needs = []
        for entry in item["needs"]:
            if isinstance(entry, Mapping) and entry.get("key") is not None:
                key = str(entry["key"])
                if key not in here:
                    if key in keymap:
                        entry = {**{k: v for k, v in entry.items() if k != "key"}, "id": keymap[key]}
                    elif key in later:
                        raise VBoardError("invalid_needs", f"task needs key {key!r}, which lands in a later part of "
                                                           "the plan: order prerequisites before dependents.")
            needs.append(entry)
        out.append({**item, "needs": needs})
    return out


def _create_plan(ctx: _Ctx, channel: str, path: str, key: str | None) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        raise _usage("invalid_request", f"couldn't read the plan {path}: {exc}") from None
    if isinstance(plan, Mapping):
        plan = plan.get("tasks")
    if not isinstance(plan, list) or not plan:
        raise _usage("invalid_request", "a plan is a non-empty list of tasks (or {\"tasks\": [...]}).")
    keys_at = {str(t["key"]): n for n, t in enumerate(plan) if isinstance(t, Mapping) and t.get("key") is not None}
    created: list[dict] = []
    keymap: dict[str, str] = {}
    offset, size = 0, min(len(plan), 100)
    while offset < len(plan):
        chunk = plan[offset:offset + size]
        here = {k for k, n in keys_at.items() if offset <= n < offset + len(chunk)}
        later = {k for k, n in keys_at.items() if n >= offset + len(chunk)}
        body = _rewrite_plan_needs(chunk, keymap, here, later)
        cid = None if key is None else (key if (offset == 0 and len(chunk) >= len(plan)) else f"{key}:{offset}")
        try:
            res = ctx.store.create_tasks(ctx.caller, channel, body, client_id=cid)
        except VBoardError as exc:
            if exc.code == "batch_too_large" and len(chunk) > 1:
                size = max(1, len(chunk) // 2)
                continue
            raise
        tasks = res["tasks"]
        for item, task in zip(chunk, tasks):
            if isinstance(item, Mapping) and item.get("key") is not None:
                keymap[str(item["key"])] = str(task["id"])
        created.extend(tasks)
        offset += len(chunk)
    return {"tasks": created, "outcome": "created"}


def t_create(ctx, opts, pos):
    key = opts.get("idempotency_key")
    if opts.get("file"):
        if not pos:
            raise _usage("missing_arguments", "tasks create --file needs a channel.")
        return _create_plan(ctx, pos[0], opts["file"], key)
    if not pos:
        raise _usage("missing_arguments", "tasks create needs a channel and text, or a thread id.")
    lease = _lease_arg(opts)
    if lease is not None and not opts.get("claim"):
        raise _usage("invalid_request", "--lease only applies together with --claim.")
    kw: dict[str, Any] = {"labels": _pairs(opts.get("label") or (), "--label") or None, "needs": _needs_args(opts),
                          "for_": opts.get("for"), "claim": bool(opts.get("claim")), "lease": lease,
                          "client_id": key, "no_sync": bool(opts.get("no_sync"))}
    if len(pos) == 1 and _is_id(pos[0]):
        res = ctx.store.create_task(ctx.caller, re=_mid(pos[0]), **kw)
    else:
        if len(pos) < 2:
            raise _usage("missing_arguments", "tasks create needs the task text.")
        res = ctx.store.create_task(ctx.caller, channel=pos[0], text=_text_arg(pos[1:]), **kw)
    out: dict[str, Any] = {"task": res["task"], "outcome": "created"}
    if res.get("claim"):
        _put_claim(ctx, str(res["task"]["id"]), res["claim"])
        out["claim"] = _claim_doc(res["claim"])
    return out


def _claim_task(ctx: _Ctx, opts, pos) -> tuple[dict, dict]:
    """claim by id or `--ready <channel>`; returns (task view, claim) and stores the proof."""
    lease = _lease_arg(opts)
    wait = _dur(opts, "wait")
    if opts.get("ready"):
        if not pos:
            raise _usage("missing_arguments", "--ready needs a channel or prefix/.")
        label = _pairs(opts.get("label") or (), "--label")
        deadline = ctx.clock() + (wait or 0.0)
        while True:
            slice_s = max(0.0, min(SLICE_S, deadline - ctx.clock())) if wait else 0.0
            got = ctx.store.claim_next(ctx.caller, pos[0], label=label or None, from_=opts.get("from") or (),
                                       lease=lease, wait=slice_s)
            if got.get("task") is not None:
                break
            if not wait or deadline - ctx.clock() <= 0:
                if wait:
                    raise VBoardError("timeout", f"Nothing became ready within {opts.get('wait')}.")
                raise VBoardError("nothing_ready", f"No task is ready in {pos[0]}.")
    else:
        if not pos:
            raise _usage("missing_arguments", "tasks claim needs a task id (or --ready <channel>).")
        got = ctx.store.claim(ctx.caller, _mid(pos[0]), lease=lease)
    _put_claim(ctx, str(got["task"]["id"]), got["claim"])
    return got["task"], got["claim"]


def t_claim(ctx, opts, pos):
    task, claim = _claim_task(ctx, opts, pos)
    return {"task": task, "claim": _claim_doc(claim)}


def t_renew(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "tasks renew needs a task id.")
    tid = _mid(pos[0])
    lease = _lease_arg(opts)
    holder: dict = {}

    def go(doc):
        res = ctx.store.renew(ctx.caller, tid, epoch=doc["epoch"], token=doc["token"], lease=lease)
        holder["epoch"] = doc["epoch"]
        holder["token"] = doc["token"]
        return res

    res = _with_claim(ctx, tid, go, drop=False)
    until = res.get("until") or (res["task"].get("task") or {}).get("claim", {}).get("until")
    ctx.claims.put(ctx.board, tid, ctx.profile, holder["epoch"], holder["token"], until)
    return {"task": res["task"], "claim": {"epoch": holder["epoch"], "until": until}}


def t_release(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "tasks release needs a task id.")
    tid = _mid(pos[0])
    return _with_claim(ctx, tid, lambda d: ctx.store.release(ctx.caller, tid, epoch=d["epoch"], token=d["token"]))


def t_complete(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "tasks complete needs a task id.")
    tid = _mid(pos[0])
    result = _text_arg(pos[1:], required=False)
    return _with_claim(ctx, tid, lambda d: ctx.store.complete(ctx.caller, tid, epoch=d["epoch"], token=d["token"],
                                                              result=result))


def t_fail(ctx, opts, pos):
    if len(pos) < 2:
        raise _usage("missing_arguments", "tasks fail needs a task id and a reason.")
    tid = _mid(pos[0])
    reason = _text_arg(pos[1:])
    return _with_claim(ctx, tid, lambda d: ctx.store.fail(ctx.caller, tid, epoch=d["epoch"], token=d["token"],
                                                          reason=reason))


def t_block(ctx, opts, pos):
    if len(pos) < 2:
        raise _usage("missing_arguments", "tasks block needs a task id and a reason.")
    tid = _mid(pos[0])
    reason = _text_arg(pos[1:])
    doc = ctx.claims.get(ctx.board, tid, ctx.profile)
    if doc is None:  # owner block
        return ctx.store.block(ctx.caller, tid, reason=reason, kind=opts.get("kind"))
    return _with_claim(ctx, tid, lambda d: ctx.store.block(ctx.caller, tid, reason=reason, kind=opts.get("kind"),
                                                           epoch=d["epoch"], token=d["token"]))


def t_unblock(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "tasks unblock needs a task id.")
    return ctx.store.unblock(ctx.caller, _mid(pos[0]))


def t_reopen(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "tasks reopen needs a task id.")
    return ctx.store.reopen(ctx.caller, _mid(pos[0]))


def t_assign(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "tasks assign needs a task id.")
    if opts.get("pool") and len(pos) > 1:
        raise _usage("conflicting_options", "--pool and a profile name conflict.")
    if not opts.get("pool") and len(pos) < 2:
        raise _usage("missing_arguments", "tasks assign needs a profile name, or --pool.")
    res = ctx.store.assign(ctx.caller, _mid(pos[0]), None if opts.get("pool") else pos[1])
    if res.get("outcome") == "pooled":
        res = {**res, "outcome": "unassigned"}
    return res


def t_needs(ctx, opts, pos):
    if not pos:
        raise _usage("missing_arguments", "tasks needs needs a task id.")
    if opts.get("none") and len(pos) > 1:
        raise _usage("conflicting_options", "--none and task ids conflict.")
    if not opts.get("none") and len(pos) < 2:
        raise _usage("missing_arguments", "tasks needs takes task ids, or --none.")
    return ctx.store.set_needs(ctx.caller, _mid(pos[0]), [] if opts.get("none") else [_mid(p) for p in pos[1:]])


def t_cancel(ctx, opts, pos):
    if len(pos) < 2:
        raise _usage("missing_arguments", "tasks cancel needs a task id and a reason.")
    return ctx.store.cancel(ctx.caller, _mid(pos[0]), reason=_text_arg(pos[1:]))


# --------------------------------------------------------------------------------------------- tasks run
def _sink(stream) -> tuple[Any, Any]:
    """(Popen argument, stream needing a pump thread or None)."""
    try:
        stream.flush()
        stream.fileno()
        return stream, None
    except Exception:
        return subprocess.PIPE, stream


def _pump(src, dst) -> None:
    try:
        for chunk in iter(lambda: src.read1(4096) if hasattr(src, "read1") else src.read(4096), b""):
            dst.write(chunk.decode("utf-8", "replace"))
    except Exception:
        pass


def t_run(ctx: _Ctx, opts, pos, cmd: list[str] | None):
    if not cmd:
        raise _usage("missing_arguments", "tasks run needs a command after `--`.")
    task, claim = _claim_task(ctx, opts, pos)
    tid = str(task["id"])
    lease = _lease_arg(opts)
    state = {"until": claim["until"], "epoch": claim["epoch"], "token": claim["token"]}
    stop, lost = threading.Event(), threading.Event()
    sig: dict[str, int] = {}
    env = {**os.environ, **{k: str(v) for k, v in ctx.env.items()}, "BOARD_TASK": tid}
    out_arg, out_pump = _sink(ctx.stderr if ctx.json else ctx.stdout)
    err_arg, err_pump = _sink(ctx.stderr)
    try:
        proc = subprocess.Popen(
            cmd, env=env, stdout=out_arg, stderr=err_arg, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as exc:
        _with_claim(ctx, tid, lambda d: ctx.store.release(ctx.caller, tid, epoch=d["epoch"], token=d["token"]))
        raise VBoardError("command_not_found", f"couldn't run {cmd[0]!r}: {exc.strerror or exc}", status=404) from None
    pumps = []
    for stream, sink in ((proc.stdout, out_pump), (proc.stderr, err_pump)):
        if stream is not None and sink is not None:
            t = threading.Thread(target=_pump, args=(stream, sink), daemon=True)
            t.start()
            pumps.append(t)

    def terminate(signum: int = signal.SIGTERM) -> None:
        try:
            proc.send_signal(signum)
        except (OSError, ProcessLookupError):
            pass

    def waiter(seconds: float) -> None:
        if ctx.sleep is not None:
            ctx.sleep(seconds)
        else:
            stop.wait(seconds)

    def renewer() -> None:
        while not stop.is_set():
            try:
                remaining = parse_iso(state["until"]) / 1000.0 - ctx.clock()
            except VBoardError:
                remaining = 0.0
            waiter(max(ctx.renew_floor_s, remaining / 3.0))
            if stop.is_set():
                return
            try:
                res = ctx.store.renew(ctx.caller, tid, epoch=state["epoch"], token=state["token"], lease=lease)
            except VBoardError as exc:
                if exc.code == "claim_lost":
                    lost.set()
                    terminate()
                    return
                continue  # network trouble: try again at the next tick
            except Exception:
                continue
            state["until"] = res.get("until") or state["until"]
            try:
                ctx.claims.put(ctx.board, tid, ctx.profile, state["epoch"], state["token"], state["until"])
            except OSError:
                pass

    def on_signal(signum, _frame):
        sig["n"] = signum
        terminate(signal.SIGTERM)

    old = {}
    if threading.current_thread() is threading.main_thread():
        for s in (signal.SIGTERM,):
            try:
                old[s] = signal.signal(s, on_signal)
            except (ValueError, OSError):
                pass
    thread = threading.Thread(target=renewer, daemon=True)
    thread.start()
    try:
        while True:
            try:
                rc = proc.wait()
                break
            except KeyboardInterrupt:
                sig["n"] = signal.SIGINT
                terminate(signal.SIGTERM)
    finally:
        stop.set()
        for s, handler in old.items():
            try:
                signal.signal(s, handler)
            except (ValueError, OSError):
                pass
    thread.join(timeout=2.0)
    for t in pumps:
        t.join(timeout=2.0)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    if lost.is_set():
        ctx.claims.drop(ctx.board, tid, ctx.profile)
        raise VBoardError("claim_lost", f"Lost the claim on task #{tid}: the child was stopped.")
    if "n" in sig:
        try:
            _with_claim(ctx, tid, lambda d: ctx.store.release(ctx.caller, tid, epoch=d["epoch"], token=d["token"]))
        except VBoardError:
            pass
        return 128 + sig["n"]
    if rc == 0:
        res = _with_claim(ctx, tid, lambda d: ctx.store.complete(ctx.caller, tid, epoch=d["epoch"], token=d["token"],
                                                                 result=opts.get("result")))
        code = 0
    else:
        if rc < 0:
            try:
                why = f"killed by {signal.Signals(-rc).name}"
            except ValueError:
                why = f"killed by signal {-rc}"
            code = 128 - rc
        else:
            why, code = f"exited {rc}", rc
        res = _with_claim(ctx, tid, lambda d: ctx.store.fail(ctx.caller, tid, epoch=d["epoch"], token=d["token"],
                                                             reason=why))
    ctx.emit({"task": res["task"], "outcome": res["outcome"], "code": code})
    return code


# --------------------------------------------------------------------------------------------- Fusion verbs
def v_sweep(ctx, opts, pos):
    res = ctx.store.sweep()
    return {"expired": res.get("expired", []), "readied": res.get("readied", []), "canceled": res.get("canceled", [])}


def _lazy(name: str):
    try:
        return __import__(f"{__package__}.{name}", fromlist=["*"])
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.endswith(f".{name}"):
            raise VBoardError("not_supported", f"`{name}` isn't available in this install.",
                              hint=f"The vboard.{name} module is missing; update the Fusion plugin.",
                              data={"exit_code": 2}) from None
        raise


def v_serve(ctx: _Ctx, opts, pos):
    mod = _lazy("http")
    if ctx.url:
        raise _usage("conflicting_options", "serve runs a local board: use --db, not --url.")
    port = _int(opts, "port", 0)
    kw: dict[str, Any] = {"host": opts.get("host") or "127.0.0.1", "port": 4100 if port is None else port,
                          "mount": opts.get("mount") or "/api/board", "auth": not opts.get("no_auth")}
    try:
        server = mod.BoardHttpServer(ctx.store, **kw).start()
    except ValueError as exc:
        raise _usage("invalid_request", str(exc)) from None
    except OSError as exc:
        raise VBoardError("port_taken", f"couldn't listen on port {kw['port']}: {exc.strerror or exc}",
                          status=409) from None
    token_file = None
    if kw["auth"]:
        try:
            token_file = str(server.write_token_file(ctx.env.get("XDG_CONFIG_HOME") or None))
        except Exception:
            token_file = None
    ctx.emit({"url": server.url, "token_file": token_file})
    done = threading.Event()
    old = {}
    if threading.current_thread() is threading.main_thread():
        try:
            old[signal.SIGTERM] = signal.signal(signal.SIGTERM, lambda *_: done.set())
        except (ValueError, OSError):
            pass
    try:
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)
        try:
            server.stop()
        except Exception:
            pass
    return 130


def v_conformance(ctx: _Ctx, opts, pos):
    from tempfile import TemporaryDirectory

    conf = _lazy("conformance")
    port = _lazy("port")

    target = ctx.env.get("FUSION_VBOARD_CONFORMANCE_TARGET", "")
    hosted_channel = ctx.env.get("FUSION_VBOARD_CONFORMANCE_CHANNEL", "").strip()
    http_url = opts.get("url") or ctx.env.get("FUSION_VBOARD_CONFORMANCE_URL")
    db = opts.get("db")
    channel = opts.get("channel")
    if target not in ("", "local", "vercel"):
        raise _usage("invalid_request", "FUSION_VBOARD_CONFORMANCE_TARGET must be local or vercel.")
    if target == "vercel":
        if not hosted_channel:
            raise _usage("invalid_request", "Set FUSION_VBOARD_CONFORMANCE_CHANNEL to a disposable hosted channel.")
        if http_url or db or channel:
            raise _usage("conflicting_options", "For hosted conformance, use the environment target and channel.")
        channel = hosted_channel
    elif http_url and db:
        raise _usage("conflicting_options", "Choose either the conformance URL or --db.")

    groups = tuple(opts["group"]) if opts.get("group") else conf.CORE_GROUPS
    if set(groups) - set(conf.CORE_GROUPS):
        raise _usage("invalid_request", f"Conformance groups are: {', '.join(conf.CORE_GROUPS)}.")
    if target == "vercel":
        report = conf.run(lambda profile: port.VercelCliAdapter(as_=profile, env=ctx.env),
                          channel=channel, groups=groups)
    elif http_url:
        report = conf.run(lambda profile: port.HttpBoardAdapter(http_url, as_=profile, env=ctx.env),
                          channel=channel, groups=groups)
    elif db:
        report = conf.run(lambda profile: port.LocalEngineAdapter(ctx.store, as_=profile, claim_cache=ctx.claims),
                          channel=channel, groups=groups)
    else:
        with TemporaryDirectory(prefix="fusion-vboard-conformance-") as temp:
            now = ctx.clock()

            def clock():
                return now

            def advance(seconds):
                nonlocal now
                now += seconds

            store = BoardStore(Path(temp) / "board.db", clock=clock)
            claims = ClaimCache(Path(temp) / "claims", clock=clock)
            try:
                report = conf.run(
                    lambda profile: port.LocalEngineAdapter(store, as_=profile, claim_cache=claims, clock=clock),
                    channel=channel, groups=groups, clock_control=advance,
                )
            finally:
                store.close()
    doc = report.to_dict()
    ctx.emit(doc)
    return 0 if doc.get("ok") else 1


# --------------------------------------------------------------------------------------------- failure rendering
def _exit_code(err: VBoardError) -> int:
    forced = err.data.get("exit_code") if isinstance(err.data, dict) else None
    if forced:
        return int(forced)
    reason = REASONS.get(err.code)
    cls = reason.cls if reason else ("usage" if err.status == 400 else "unknown")
    return 2 if cls in ("usage", "missing_input") else 1


def _render_failure(err: VBoardError, json_mode: bool, stderr) -> int:
    doc = err.to_failure()
    if json_mode:
        stderr.write(json.dumps(doc, ensure_ascii=False) + "\n")
    else:
        lines = [f"error: {err.message}"]
        if err.hint:
            lines.append(f"  {err.hint}")
        lines.append("")
        for step in doc.get("next") or []:
            lines.append(f"{step.get('when', 'Next')}:")
            lines.append(f"  {step.get('command', '')}")
        stderr.write("\n".join(lines).rstrip("\n") + "\n")
    try:
        stderr.flush()
    except Exception:
        pass
    return _exit_code(err)


HELP = f"""usage: {PROG} [--db PATH | --url URL] [--name N] [--as PROFILE] [--json] <verb> [args]

verbs: whoami channels actors read post reply edit delete react label tail wait
       tasks [create|claim|run|renew|release|complete|fail|block|unblock|reopen|assign|needs|cancel]
       serve sweep conformance
Same verbs, flags and --json output as `vercel board`. Claims are kept per --as profile.
"""

HANDLERS: dict[str, Callable] = {
    "whoami": v_whoami, "actors": v_actors, "channels": v_channels, "read": v_read, "post": v_post,
    "reply": v_reply, "edit": v_edit, "delete": v_delete, "rm": v_delete, "del": v_delete, "react": v_react,
    "label": v_label, "tail": v_tail, "wait": v_wait, "tasks": v_tasks_list, "tasks create": t_create,
    "tasks claim": t_claim, "tasks renew": t_renew, "tasks release": t_release, "tasks complete": t_complete,
    "tasks fail": t_fail, "tasks block": t_block, "tasks unblock": t_unblock, "tasks reopen": t_reopen,
    "tasks assign": t_assign, "tasks needs": t_needs, "tasks cancel": t_cancel, "serve": v_serve,
    "sweep": v_sweep, "conformance": v_conformance,
}


def main(argv: Sequence[str] | None = None, *, stdout=None, stderr=None, env: Mapping[str, str] | None = None,
         store_factory: Callable | None = None, clock: Callable[[], float] | None = None,
         sleep: Callable[[float], None] | None = None, renew_floor_s: float = 10.0) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    env = os.environ if env is None else env
    ctx = _Ctx(env=env, stdout=stdout, stderr=stderr,
               json_mode="--json" in (argv[:argv.index("--")] if "--" in argv else argv),
               clock=clock or time.time, sleep=sleep, renew_floor_s=renew_floor_s, store_factory=store_factory)
    try:
        return _run(ctx, argv)
    except VBoardError as err:
        return _render_failure(err, ctx.json, stderr)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001 - last-resort envelope
        return _render_failure(VBoardError("unexpected_error", f"{type(exc).__name__}: {exc}"), ctx.json, stderr)
    finally:
        ctx.close()


def _run(ctx: _Ctx, argv: list[str]) -> int:
    pre, verb, after = _split_verb(argv)
    if verb is None:
        gopts, _, _ = _parse(pre, GLOBAL)
        if gopts.get("version"):
            return _version(ctx)
        ctx.stdout.write(HELP)
        return 0 if gopts.get("help") or not pre else 2
    if verb in UNSUPPORTED:
        raise VBoardError("not_supported", f"`{verb}` isn't available on the Fusion local board.", status=501,
                          hint="Use the stock `vercel board` for this verb.")
    key = verb
    if verb in ("tasks", "channels") and after and after[0] in (TASK_SUBS if verb == "tasks" else ()):
        key, after = f"tasks {after[0]}", after[1:]
    spec = VERB_OPTS.get(key)
    if spec is None:
        raise _usage("unknown_command", f"Unknown command {verb}.", f"Run `{PROG} --help`.")
    cmd_rest: list[str] | None = None
    opts, pos, rest = _parse(pre + after, {**GLOBAL, **spec})
    if rest is not None:
        if key == "tasks run":
            cmd_rest = rest
        else:
            pos += rest
    if opts.get("help"):
        ctx.stdout.write(HELP)
        return 0
    if opts.get("version"):
        return _version(ctx)
    ctx.configure(opts)
    if key == "tasks run":
        return t_run(ctx, opts, pos, cmd_rest)
    result = HANDLERS[key](ctx, opts, pos)
    if isinstance(result, int):
        return result
    ctx.emit(result)
    return 0


def _version(ctx: _Ctx) -> int:
    if ctx.json:
        ctx.emit({"version": VERSION})
    else:
        ctx.stdout.write(f"{VERSION}\n")
    return 0


__all__ = ["main"]
