"""Portable conformance suite for any `BoardPort` (design/v4-spec.md section 5, judge J-06, A1.4).

The checks are the CORE items of vercel/board's own conformance suite (notes/b5-conformance.md, items 1-78; the OPT items
7, 17, 29-32, 60 and 75-78 are skipped by design) plus five Fusion checks (`f01`..`f05`). Each check is a function
`c<NN>_<slug>(ctx)` that drives the board only through the `BoardPort` verbs; the few things a port cannot express
(event pages with `last`, channel message pages, create-channel, dependents listing, batch limits, a claim retried with
the same token, forged claim proofs) go through `_Raw`, which reaches under the local and HTTP adapters and raises
`Skip` for any other adapter.

Targets (A1.4):
  * default: `LocalEngineAdapter` over a throwaway sqlite store (`python -m fusion_swarm.vboard conformance`, the tests)
  * opt-in: a running board URL (`HttpBoardAdapter`), `FUSION_VBOARD_CONFORMANCE_URL`; upstream parity gate:
        node <cli.mjs> conformance --url <url>        (the stock suite, run against the same board)
  * opt-in, never default and never in CI: the hosted board through `VercelCliAdapter`, only with
    `FUSION_VBOARD_CONFORMANCE_TARGET=vercel` and a throwaway channel in `FUSION_VBOARD_CONFORMANCE_CHANNEL`.
    Only the checks marked `v` run there (single principal, real clock, no subtree reads); the rest are `skipped`.

Hooks (all optional; a check that needs one the caller did not give is `skipped`, never `failed`):
  * `other_principal`   factory for a port acting as a DIFFERENT principal
  * `narrowed`          factory for a port whose permissions are narrowed to read+post
  * `clock_control`     `advance(seconds)` for the board's clock (lease expiry checks)
  For a `LocalEngineAdapter` target the first two are derived from the adapter's own store.

Every run labels what it posts `conformance=<uuid8>`, works in `conf/<uuid8>/...` (or under a given throwaway channel)
and deletes what it made in reverse order at the end (best effort). Stdlib only; never prints a token.
"""
from __future__ import annotations

import json
import re as _re
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .model import REASONS, Caller, VBoardError, parse_iso
from .port import BoardPort, HttpBoardAdapter

PortFactory = Callable[[str], BoardPort]
CORE_GROUPS = ("core", "paging", "tasks", "fusion")
OPTIONAL_GROUPS = ("tasks", "fusion")          # a 501 here is `skipped` on a hosted target, `failed` locally
_HEX64 = _re.compile(r"\b[0-9a-f]{64}\b")


# ============================================================================================= report
@dataclass
class Check:
    group: str
    name: str
    status: str = "passed"          # passed | failed | skipped
    detail: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"group": self.group, "name": self.name, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class Report:
    url: str
    checks: list[Check]
    ok: bool

    def to_dict(self) -> dict:
        return {"url": self.url, "checks": [c.to_dict() for c in self.checks], "ok": self.ok}

    @property
    def passed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "passed"]

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "failed"]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.status == "skipped"]


class Skip(Exception):
    """Raised inside a check: this target cannot express it."""


class Fail(AssertionError):
    """An assertion of the suite did not hold."""


# ============================================================================================= assertion helpers
def need(cond: Any, msg: str = "assertion failed") -> None:
    if not cond:
        raise Fail(msg)


def eq(got: Any, want: Any, what: str = "value") -> None:
    if got != want:
        raise Fail(f"{what}: expected {want!r}, got {got!r}")


def expect_err(code: str, fn: Callable[..., Any], *args: Any, **kw: Any) -> VBoardError:
    """`fn` must raise VBoardError(code) with the status the vocabulary pins for that code (notes/b5)."""
    try:
        fn(*args, **kw)
    except VBoardError as exc:
        if exc.code != code:
            raise Fail(f"expected {code}, got {exc.code}: {exc.message[:160]}") from None
        reason = REASONS.get(code)
        if reason is not None and exc.status != reason.status:
            raise Fail(f"{code}: expected HTTP {reason.status}, got {exc.status}") from None
        return exc
    raise Fail(f"expected {code}, but the call succeeded")


def par(fns: Sequence[Callable[[], Any]], timeout: float = 30.0) -> list[Any]:
    """Run callables concurrently; results in order, a raised exception is returned as the value."""
    out: list[Any] = [None] * len(fns)

    def runner(i: int, fn: Callable[[], Any]) -> None:
        try:
            out[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            out[i] = exc

    threads = [threading.Thread(target=runner, args=(i, fn), daemon=True) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
        if t.is_alive():
            raise Fail("a concurrent call did not finish")
    return out


def _walk_keys(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(str(k))
            _walk_keys(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_keys(v, found)


def _mid(event: dict) -> str | None:
    msg = event.get("message")
    return str(msg["id"]) if isinstance(msg, dict) and msg.get("id") is not None else None


def _dump(*docs: Any) -> str:
    return json.dumps(docs, ensure_ascii=False, default=str)


# ============================================================================================= raw access
class _Raw:
    """What a `BoardPort` cannot say. Works on LocalEngineAdapter (via its store) and HttpBoardAdapter (via `_request`)."""

    def __init__(self, port: Any):
        self.port = port
        self.store = getattr(port, "store", None)
        self.caller = getattr(port, "caller", None)
        self.http = self.store is None and callable(getattr(port, "_request", None))

    @property
    def ok(self) -> bool:
        return self.store is not None or self.http

    def _need(self) -> None:
        if not self.ok:
            raise Skip("needs a raw feed (local or http adapter)")

    def _get(self, path: str, query: Sequence[tuple[str, Any]] = ()) -> dict:
        return self.port._request("GET", path, query=list(query))

    def events(self, **kw: Any) -> dict:
        self._need()
        if self.store is not None:
            if "label" in kw:
                kw["label"] = list(kw["label"] or ())
            return self.store.events(self.caller, **kw)
        q: list[tuple[str, Any]] = []
        for k, v in kw.items():
            if v is None or v is False:
                continue
            if k == "label":
                q.extend(("label", x) for x in v)
            elif v is True:
                q.append((k, "1"))
            else:
                q.append((k, v))
        return self._get("/events", q)

    def messages(self, channel: str, *, after: str | None = None, limit: int = 50) -> dict:
        self._need()
        if self.store is not None:
            return self.store.messages(self.caller, channel, after=after, limit=limit)
        return self._get("/messages", [("channel", channel), ("after", after), ("limit", limit)])

    def channels(self, prefix: str | None = None, *, after: str | None = None, limit: int | None = None) -> dict:
        self._need()
        if self.store is not None:
            kw: dict[str, Any] = {"prefix": prefix, "after": after}
            if limit is not None:
                kw["limit"] = limit
            return self.store.channels(self.caller, **kw)
        return self._get("/channels", [("prefix", prefix), ("after", after), ("limit", limit)])

    def create_channel(self, path: str) -> dict:
        self._need()
        if self.store is not None:
            return self.store.create_channel(self.caller, path)
        return self.port._request("POST", "/channels", body={"path": path})

    def posted(self, client_id: str) -> dict:
        self._need()
        if self.store is not None:
            return self.store.posted(self.caller, client_id)
        return self._get(f"/posts/{urllib.parse.quote(client_id, safe='')}")

    def tasks(self, **kw: Any) -> dict:
        self._need()
        if self.store is not None:
            if "label" in kw:
                kw["label"] = list(kw["label"] or ())
            return self.store.list_tasks(self.caller, **kw)
        q = [(("for" if k == "for_" else k), v) for k, v in kw.items() if k != "label"]
        q.extend(("label", x) for x in kw.get("label") or ())
        return self._get("/tasks", q)

    def claim_with_token(self, task_id: str, token: str, lease: float | None = None) -> dict:
        self._need()
        if self.store is not None:
            return self.store.claim(self.caller, task_id, lease=lease, token=token)
        body: dict[str, Any] = {"token": token}
        if lease is not None:
            body["lease"] = lease
        return self.port._request("POST", f"/tasks/{urllib.parse.quote(task_id, safe='')}/claim", body=body)


def _derive_local(base: Any, kind: str) -> PortFactory | None:
    store, caller = getattr(base, "store", None), getattr(base, "caller", None)
    if store is None or caller is None:
        return None
    from .port import LocalEngineAdapter

    def make(profile: str) -> Any:
        name = profile or None
        if kind == "other":
            who = Caller(principal="local:user:conf-other", profile=name)
        else:
            who = Caller(principal=caller.principal, profile=name, can=frozenset({"read", "post"}))
        return LocalEngineAdapter(store, caller=who, claim_cache=False)

    return make


# ============================================================================================= context
class Ctx:
    def __init__(self, factory: PortFactory, run_id: str, root: str, *, other: PortFactory | None,
                 narrowed: PortFactory | None, tick: Callable[[float], None] | None):
        self.factory = factory
        self.run_id = run_id
        self.root = root
        self._ports: list[Any] = []
        self.made: list[tuple[Any, str]] = []
        self.c = root
        self.tick = tick
        try:
            self.p = self.port("conf-a")
            self.p2 = self.port("conf-b")
            self.caps = self.p.capabilities()
            self.backend = getattr(self.caps, "backend", "?")
            self.o = self._open(other or _derive_local(self.p, "other"), "conf-c")
            self.nar = self._open(narrowed or _derive_local(self.p, "narrow"), "conf-n")
            self.raw = _Raw(self.p)
            self.me = self.p.whoami()
        except BaseException:
            self.cleanup()
            raise

    def _open(self, fac: PortFactory | None, profile: str) -> Any:
        if fac is None:
            return None
        port = fac(profile)
        self._ports.append(port)
        return port

    def port(self, profile: str) -> Any:
        port = self.factory(profile)
        self._ports.append(port)
        return port

    def sub(self, name: str) -> str:
        return f"{self.c}/{name}"

    def threaded(self, port: Any, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        finally:
            if isinstance(port, HttpBoardAdapter):
                # HTTP connections belong to the calling thread.
                port.close()

    # -- recorded writes (cleanup deletes them in reverse)
    def post(self, port: Any, target: str, text: str = "m", labels: dict | None = None, **kw: Any) -> dict:
        lab = {"conformance": self.run_id}
        lab.update(labels or {})
        doc = port.post(target, text, labels=lab, **kw)
        self.made.append((port, str(doc["message"]["id"])))
        return doc

    def reply(self, port: Any, root: str, text: str = "r", **kw: Any) -> dict:
        lab = {"conformance": self.run_id}
        lab.update(kw.pop("labels", None) or {})
        doc = port.reply(root, text, labels=lab, **kw)
        self.made.append((port, str(doc["message"]["id"])))
        return doc

    def task(self, port: Any, channel: str, text: str = "t", labels: dict | None = None, **kw: Any) -> dict:
        lab = {"conformance": self.run_id}
        lab.update(labels or {})
        doc = port.task_create(channel, text, labels=lab, **kw)
        self.made.append((port, str(doc["task"]["id"])))
        return doc

    def task_id(self, port: Any, channel: str, text: str = "t", **kw: Any) -> str:
        return str(self.task(port, channel, text, **kw)["task"]["id"])

    def batch(self, port: Any, channel: str, plan: list[dict], *, idempotency_key: str,
              single: bool = False) -> dict:
        labeled = [{**item, "noSync": True, "labels": {**item.get("labels", {}), "conformance": self.run_id}}
                   for item in plan]
        if single:
            doc = port._batch_once(channel, labeled, idempotency_key)
        else:
            doc = port.task_create_batch(channel, labeled, idempotency_key=idempotency_key)
        self.made.extend((port, str(task["id"])) for task in doc["tasks"])
        return doc

    def now_ms(self) -> int:
        doc = self.post(self.p, self.sub("clock"), "tick")
        return parse_iso(doc["message"]["createdAt"])

    def cleanup(self) -> None:
        removed: set[str] = set()
        for port, mid in reversed(self.made):
            if mid in removed:
                continue
            try:
                port.delete(mid)
                removed.add(mid)
            except Exception:  # noqa: BLE001 - best effort
                pass
        for port in reversed(self._ports):
            try:
                port.close()
            except Exception:  # noqa: BLE001
                pass

    # -- reading helpers
    def msg(self, port: Any, mid: str) -> dict:
        return port.read(mid)["message"]

    def tk(self, port: Any, tid: str) -> dict:
        task = self.msg(port, tid).get("task")
        need(task is not None, f"{tid} is not a task")
        return task

    def state(self, tid: str, port: Any = None) -> str:
        return self.tk(port or self.p, tid)["state"]

    def tids(self, channel: str, state: str = "open", port: Any = None) -> list[str]:
        out = (port or self.p).tasks(channel, state=state, limit=500)
        return [str(t["message"]["id"]) for t in out.get("tasks") or []]

    def task_events(self, tid: str, after: str, channel: str | None = None, all_: bool = False) -> list[dict]:
        kw: dict[str, Any] = {"after": after, "all": all_}
        if channel:
            kw["channel"] = channel
        evs = self.raw.events(**kw).get("events") or []
        out = []
        for ev in evs:
            pl = ev.get("payload") or {}
            if str(pl.get("id")) == tid or str(pl.get("thread")) == tid or str((ev.get("message") or {}).get("id")) == tid:
                out.append(ev)
        return out

    @staticmethod
    def supports_proofs(port: BoardPort) -> bool:
        return callable(getattr(port, "_proof_save", None))

    def proof(self, port: Any, tid: str) -> tuple[int, str]:
        if not self.supports_proofs(port):
            raise Skip("needs claim-proof injection (local or http adapter)")
        return port._proofs[tid]

    def inject(self, port: Any, tid: str, epoch: int, token: str, until: str = "1970-01-01T00:00:00.000Z") -> None:
        if not self.supports_proofs(port):
            raise Skip("needs claim-proof injection (local or http adapter)")
        port._proof_save(tid, {"epoch": epoch, "until": until}, token)


# ============================================================================================= registry
@dataclass(frozen=True)
class _Spec:
    cid: str
    slug: str
    group: str
    fn: Callable[[Ctx], None]
    vercel: bool
    needs: tuple[str, ...]

    @property
    def name(self) -> str:
        return f"{self.cid}_{self.slug}"


_REGISTRY: list[_Spec] = []


def check(cid: str, slug: str, group: str, *, v: bool = False, needs: tuple[str, ...] = ()):
    def deco(fn: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
        _REGISTRY.append(_Spec(cid, slug, group, fn, v, needs))
        return fn

    return deco


def check_names(groups: Sequence[str] = CORE_GROUPS) -> list[str]:
    """Every check name the suite defines for these groups (`c01_post_read_back`, ...)."""
    return [s.name for s in _REGISTRY if s.group in groups]


def vercel_subset() -> list[str]:
    return [s.name for s in _REGISTRY if s.vercel]


# ============================================================================================= A. messages
@check("c01", "post_read_back", "core", v=True)
def c01_post_read_back(ctx: Ctx) -> None:
    doc = ctx.post(ctx.p, ctx.c, "hello conformance")
    msg = doc["message"]
    need(isinstance(msg["id"], str) and 1 <= len(msg["id"]) <= 128, "id is a 1..128 char string")
    need(isinstance(msg["version"], (int, float)), "version is numeric")
    back = ctx.msg(ctx.p, msg["id"])
    eq(back["id"], msg["id"], "read back id")
    eq(back["text"], "hello conformance", "read back text")


@check("c02", "reply_placement", "core", v=True)
def c02_reply_placement(ctx: Ctx) -> None:
    root = ctx.post(ctx.p, ctx.c, "root")["message"]["id"]
    rep = ctx.reply(ctx.p, root, "child")["message"]
    eq(str(rep["re"]), str(root), "reply.re")
    view = ctx.p.read(root)
    need(any(r["id"] == rep["id"] for r in view["replies"]), "the thread lists the reply")


@check("c03", "conditional_reply_after", "core", v=True)
def c03_conditional_reply_after(ctx: Ctx) -> None:
    root = ctx.post(ctx.p, ctx.c, "root")["message"]["id"]
    r1 = ctx.reply(ctx.p, root, "r1")["message"]["id"]
    r2 = ctx.reply(ctx.p, root, "r2", after=r1)["message"]["id"]      # r1 was the newest: lands
    expect_err("newer_message", ctx.reply, ctx.p, root, "stale", after=r1)
    need(len(ctx.p.read(root)["replies"]) == 2, "a refused reply changed nothing")
    ctx.p.delete(r2)                                                   # a deleted reply still counts as newer
    expect_err("newer_message", ctx.reply, ctx.p, root, "stale again", after=r1)
    r3 = ctx.reply(ctx.p, root, "r3")["message"]["id"]
    ports = [ctx.p, ctx.p2, ctx.port("conf-d")]
    res = par([lambda pt=pt: ctx.threaded(pt, lambda: ctx.reply(pt, root, "race", after=r3))
               for pt in ports])
    landed = [r for r in res if isinstance(r, dict)]
    refused = [r for r in res if isinstance(r, VBoardError) and r.code == "newer_message"]
    eq(len(landed), 1, "concurrent replies after the same id that landed")
    eq(len(refused), 2, "concurrent replies refused with newer_message")


@check("c04", "invalid_after", "core", v=True)
def c04_invalid_after(ctx: Ctx) -> None:
    a = ctx.post(ctx.p, ctx.c, "a")["message"]["id"]
    b = ctx.post(ctx.p, ctx.c, "b")["message"]["id"]
    expect_err("invalid_after", ctx.reply, ctx.p, a, "x", after=b)             # another thread's message
    expect_err("invalid_after", ctx.reply, ctx.p, a, "x", after="999999999")   # nonexistent


@check("c05", "post_idempotency", "core", v=True)
def c05_post_idempotency(ctx: Ctx) -> None:
    key = f"conf-{ctx.run_id}-c05"
    first = ctx.post(ctx.p, ctx.c, "once", idempotency_key=key)["message"]["id"]
    again = ctx.post(ctx.p, ctx.c, "once", idempotency_key=key)["message"]["id"]
    eq(again, first, "same client id returns the same message")
    eq(len(ctx.p.read(ctx.c, label=[f"conformance={ctx.run_id}"])["threads"]), 1, "nothing new was posted")
    if ctx.raw.ok:
        got = ctx.raw.posted(key)
        eq(str((got.get("message") or got).get("id")), first, "posted(clientId)")
        expect_err("post_not_found", ctx.raw.posted, f"conf-{ctx.run_id}-unknown")


@check("c06", "duplicate_id_other_place", "core", v=True)
def c06_duplicate_id_other_place(ctx: Ctx) -> None:
    key = f"conf-{ctx.run_id}-c06"
    root = ctx.post(ctx.p, ctx.c, "x", idempotency_key=key)["message"]["id"]
    expect_err("duplicate_id", ctx.reply, ctx.p, root, "same id as a reply", idempotency_key=key)
    if ctx.o is not None:                                                  # other-principal half: local only
        err = expect_err("duplicate_id", ctx.post, ctx.o, ctx.c, "stolen", idempotency_key=key)
        need("x" not in err.message.split(), "the error leaks no message body")


@check("c08", "retry_conditional_reply_same_id", "core", v=True)
def c08_retry_conditional_reply_same_id(ctx: Ctx) -> None:
    root = ctx.post(ctx.p, ctx.c, "root")["message"]["id"]
    r1 = ctx.reply(ctx.p, root, "r1")["message"]["id"]
    key = f"conf-{ctx.run_id}-c08"
    landed = ctx.reply(ctx.p, root, "r2", after=r1, idempotency_key=key)["message"]["id"]
    ctx.reply(ctx.p, root, "r3")
    for stale in (r1, root, "999999999"):
        again = ctx.reply(ctx.p, root, "r2", after=stale, idempotency_key=key)["message"]["id"]
        eq(again, landed, f"retry with after={stale}")
    expect_err("invalid_after", ctx.reply, ctx.p, root, "new id", after="999999999", idempotency_key=key + "-new")


@check("c09", "version_bump", "core", v=True)
def c09_version_bump(ctx: Ctx) -> None:
    mid = ctx.post(ctx.p, ctx.c, "v0")["message"]["id"]
    versions = [ctx.msg(ctx.p, mid)["version"]]
    ctx.p.edit(mid, "v1")
    versions.append(ctx.msg(ctx.p, mid)["version"])
    ctx.p.react(mid, "eyes")
    versions.append(ctx.msg(ctx.p, mid)["version"])
    ctx.p.label(mid, set={"k": "v"})
    versions.append(ctx.msg(ctx.p, mid)["version"])
    ctx.p.label(mid, unset=["k"])
    view = ctx.msg(ctx.p, mid)
    versions.append(view["version"])
    need(all(b > a for a, b in zip(versions, versions[1:])), f"version strictly increases: {versions}")
    eq(view["text"], "v1", "edited text")
    need(view.get("editedAt"), "editedAt is set")
    need(ctx.me["actor"] in (view["reactions"].get("eyes") or []), "reactions.eyes includes the actor")
    need("k" not in view["labels"], "an unset label is gone")


@check("c10", "revision_cas", "core", v=True)
def c10_revision_cas(ctx: Ctx) -> None:
    mid = ctx.post(ctx.p, ctx.c, "r0")["message"]["id"]
    rev = ctx.msg(ctx.p, mid)["revision"]
    ctx.p.react(mid, "eyes")
    ctx.p.label(mid, set={"k": "v"})
    eq(ctx.msg(ctx.p, mid)["revision"], rev, "react and label leave the revision alone")
    ctx.p.edit(mid, "r1", if_revision=rev)
    new_rev = ctx.msg(ctx.p, mid)["revision"]
    need(new_rev != rev, "an edit changes the revision")
    expect_err("revision_conflict", ctx.p.edit, mid, "stale", if_revision=rev)
    expect_err("revision_conflict", ctx.p.delete, mid, if_revision=rev)
    eq(ctx.msg(ctx.p, mid)["text"], "r1", "a refused write changed nothing")
    res = par([lambda i=i: ctx.threaded(ctx.p, lambda: ctx.p.edit(mid, f"race-{i}", if_revision=new_rev))
               for i in range(3)])
    eq(sum(1 for r in res if isinstance(r, dict)), 1, "racing edits at one revision that won")
    gone = ctx.post(ctx.p, ctx.c, "to delete")["message"]["id"]
    grev = ctx.msg(ctx.p, gone)["revision"]
    ctx.p.delete(gone)
    expect_err("revision_conflict", ctx.p.edit, gone, "late", if_revision=grev)


@check("c11", "delete_tombstone", "core", v=True)
def c11_delete_tombstone(ctx: Ctx) -> None:
    mid = ctx.post(ctx.p, ctx.c, "doomed", labels={"c11": "1"})["message"]["id"]
    ctx.p.react(mid, "eyes")
    ctx.p.delete(mid)
    view = ctx.msg(ctx.p, mid)
    need(view["deleted"] is True, "deleted flag")
    eq(view["text"], "", "tombstone text")
    eq(view["labels"], {}, "tombstone labels")
    need(not view.get("reactions"), "tombstone reactions")
    page = ctx.p.read(ctx.c, label=["c11=1"])
    need(all(t["message"]["id"] != mid for t in page["threads"]), "label reads exclude the tombstone")


@check("c12", "unknown_id_and_bad_channel", "core", v=True)
def c12_unknown_id_and_bad_channel(ctx: Ctx) -> None:
    expect_err("message_not_found", ctx.p.read, "999999999")
    expect_err("invalid_request", ctx.post, ctx.p, "Not A Channel", "x")
    if ctx.raw.ok:
        expect_err("invalid_request", ctx.raw.create_channel, "Not A Channel")


@check("c13", "create_channel_idempotent", "core")
def c13_create_channel_idempotent(ctx: Ctx) -> None:
    ctx.post(ctx.p, ctx.c, "creates the channel")
    got = ctx.raw.create_channel(ctx.c)
    need(got.get("created") is False, "an existing channel is not created again")
    eq((got.get("channel") or {}).get("path"), ctx.c, "channel.path")


# ============================================================================================= B. labels
@check("c14", "label_cas", "core", v=True)
def c14_label_cas(ctx: Ctx) -> None:
    mid = ctx.post(ctx.p, ctx.c, "cas")["message"]["id"]
    ports = [ctx.p, ctx.p2, ctx.port("conf-d")]
    res = par([lambda i=i, pt=pt: ctx.threaded(pt, lambda: pt.label(mid, set={"owner": f"agent-{i}"}, if_unset=["owner"]))
               for i, pt in enumerate(ports)])
    wins = [i for i, r in enumerate(res) if isinstance(r, dict)]
    eq(len(wins), 1, "concurrent label claims that landed")
    for r in res:
        if not isinstance(r, dict):
            need(isinstance(r, VBoardError) and r.code == "label_conflict", f"the losers get label_conflict: {r!r}")
    eq(ctx.msg(ctx.p, mid)["labels"].get("owner"), f"agent-{wins[0]}", "final label is the winner's")
    expect_err("label_conflict", ctx.p.label, mid, set={"owner": "x"}, if_={"owner": "nobody"})
    eq(ctx.msg(ctx.p, mid)["labels"].get("owner"), f"agent-{wins[0]}", "a refused write changed nothing")
    ctx.p.label(mid, set={"owner": "same"}, if_={"owner": f"agent-{wins[0]}"})
    ctx.p.label(mid, unset=["owner"], if_={"owner": "same"})
    need("owner" not in ctx.msg(ctx.p, mid)["labels"], "unset with a matching if")
    ctx.p.label(mid, set={"owner": "late"}, if_unset=["owner"])


@check("c15", "label_if_concurrent", "core", v=True)
def c15_label_if_concurrent(ctx: Ctx) -> None:
    mid = ctx.post(ctx.p, ctx.c, "if")["message"]["id"]
    ctx.p.label(mid, set={"k": "zq-first-8841"})
    ctx.reply(ctx.p, mid, "unrelated append")
    ctx.p.label(mid, set={"k": "zq-second-5527"}, if_={"k": "zq-first-8841"})     # unrelated append: still lands
    err = expect_err("label_conflict", ctx.p2.label, mid, set={"k": "x"}, if_={"k": "zq-first-8841"})
    need("zq-second-5527" not in err.message, "the error does not reveal the competing value")


@check("c16", "reserved_labels", "core", v=True)
def c16_reserved_labels(ctx: Ctx) -> None:
    mid = ctx.post(ctx.p, ctx.c, "plain")["message"]["id"]
    expect_err("reserved_label", ctx.p.label, mid, set={"board.task": "x"})
    expect_err("reserved_label", ctx.p.label, mid, unset=["board.task"])
    expect_err("reserved_label", ctx.post, ctx.p, ctx.c, "bad", labels={"board.x": "1"})
    tid = ctx.task_id(ctx.p, ctx.c, "t")
    expect_err("reserved_label", ctx.p.label, tid, set={"board.task": "claimed"})
    eq(ctx.state(tid), "ready", "task state unaffected")
    ctx.p.label(tid, set={"k": "v"}, if_={"board.task": "ready"})
    expect_err("label_conflict", ctx.p.label, tid, set={"k": "w"}, if_={"board.task": None})
    expect_err("label_conflict", ctx.p.label, tid, set={"k": "w"}, if_={"board.task": "claimed"})
    page = ctx.p.read(ctx.c, label=["board.task=ready"])
    need(any(t["message"]["id"] == tid for t in page["threads"]), "board.task can be filtered on")


# ============================================================================================= C. reads, paging, feed
@check("c18", "channel_message_cursor", "paging", needs=("raw",))
def c18_channel_message_cursor(ctx: Ctx) -> None:
    start = ctx.post(ctx.p, ctx.c, "start marker")["message"]["id"]
    m1 = ctx.post(ctx.p, ctx.c, "one")["message"]["id"]
    rep = ctx.reply(ctx.p, m1, "reply")["message"]["id"]
    m2 = ctx.post(ctx.p, ctx.c, "two")["message"]["id"]
    page = ctx.raw.messages(ctx.c, after=start, limit=500)
    need(page.get("cursor") is not None, "response has a cursor")
    ids = [m["id"] for m in page["messages"]]
    eq(ids, [m1, rep, m2], "oldest first, root before its reply")
    need(all(m["channel"] == ctx.c for m in page["messages"]), "all in the channel")
    one = ctx.raw.messages(ctx.c, after=start, limit=1)
    eq(len(one["messages"]), 1, "limit 1")
    seen = [m["id"] for m in one["messages"]]
    cur = one["cursor"]
    for _ in range(5):
        nxt = ctx.raw.messages(ctx.c, after=cur, limit=1)
        if not nxt["messages"]:
            break
        seen += [m["id"] for m in nxt["messages"]]
        cur = nxt["cursor"]
    eq(seen, [m1, rep, m2], "reading on from the cursor repeats nothing and reaches the rest")


@check("c19", "newest_n_read", "paging")
def c19_newest_n_read(ctx: Ctx) -> None:
    ids = [ctx.post(ctx.p, ctx.c, f"n{i}")["message"]["id"] for i in range(3)]
    page = ctx.p.read(ctx.c, limit=2)
    got = [t["message"]["id"] for t in page["threads"]]
    need(len(got) <= 2 and ids[2] in got and ids[0] not in got, f"newest two threads, got {got}")
    fresh = ctx.post(ctx.p, ctx.c, "after the page")["message"]["id"]
    more = [t["message"]["id"] for t in ctx.p.read(ctx.c, after=page["cursor"])["threads"]]
    eq(more, [fresh], "messages posted after are reached through the page cursor")


@check("c20", "thread_reads", "paging", v=True)
def c20_thread_reads(ctx: Ctx) -> None:
    root = ctx.post(ctx.p, ctx.c, "root")["message"]["id"]
    rs = [ctx.reply(ctx.p, root, f"r{i}")["message"]["id"] for i in range(3)]
    after = ctx.p.read(root, after=rs[0])
    eq(after["message"]["id"], root, "root is always included")
    eq([r["id"] for r in after["replies"]], rs[1:], "replies after a reply")
    first = ctx.p.read(rs[0], after=root, limit=1)
    eq(first["message"]["id"], root, "thread(reply) returns the root")
    eq([r["id"] for r in first["replies"]], [rs[0]], "after the root, limit 1: the first reply")
    newest = ctx.p.read(root, limit=1)
    eq([r["id"] for r in newest["replies"]], [rs[2]], "limit 1: the newest reply")


@check("c21", "thread_summaries_subtree", "paging")
def c21_thread_summaries_subtree(ctx: Ctx) -> None:
    root = ctx.post(ctx.p, ctx.c, "root", labels={"c21": "1"})["message"]["id"]
    rep = ctx.reply(ctx.p, root, "latest")["message"]["id"]
    page = ctx.p.read(ctx.c, label=["c21=1"])
    summary = next((t for t in page["threads"] if t["message"]["id"] == root), None)
    need(summary is not None, "the thread is summarised")
    eq(summary["replies"], 1, "replies count")
    eq((summary["last"] or {}).get("id"), rep, "last is the latest reply")
    if not ctx.caps.subtree_read:
        raise Skip("the board has no channel subtree reads")
    child = ctx.post(ctx.p, ctx.sub("child"), "in a child channel", labels={"c21": "1"})["message"]["id"]
    tree = [t["message"]["id"] for t in ctx.p.read(ctx.c + "/", label=["c21=1"])["threads"]]
    need(root in tree and child in tree, "a trailing slash reads the whole subtree")
    bare = [t["message"]["id"] for t in ctx.p.read(ctx.c, label=["c21=1"])["threads"]]
    need(root in bare and child not in bare, "a bare channel excludes its children")


@check("c22", "threads_back_paging", "paging")
def c22_threads_back_paging(ctx: Ctx) -> None:
    ids = [ctx.post(ctx.p, ctx.c, f"t{i}")["message"]["id"] for i in range(5)]
    page = ctx.p.read(ctx.c, limit=2)
    if "older" not in page:
        raise Skip("unpaged: the board returns no `older` key")
    need(isinstance(page["older"], str), "limit 2 over 5 threads yields an older cursor")
    seen = [t["message"]["id"] for t in page["threads"]]
    older = page["older"]
    for _ in range(10):
        if older is None:
            break
        nxt = ctx.p.read(ctx.c, before=older, limit=2)
        seen += [t["message"]["id"] for t in nxt["threads"]]
        older = nxt.get("older")
    eq(sorted(seen), sorted(ids), "every thread seen exactly once")
    expect_err("invalid_request", ctx.p.read, ctx.c, before=page["older"], after=page["cursor"])


@check("c23", "channel_listing", "paging")
def c23_channel_listing(ctx: Ctx) -> None:
    names = [ctx.sub(f"l{i}") for i in range(3)]
    for n in names:
        ctx.post(ctx.p, n, "x")
    listing = ctx.p.channels(ctx.c)
    paths = [c["path"] for c in listing["channels"]]
    need(all(p.startswith(ctx.c) for p in paths), "every path starts with the prefix")
    need(all(n in paths for n in names), "every posted channel is listed")
    for c in listing["channels"]:
        for key in ("lastIndex", "messages", "threads"):
            need(key not in c or isinstance(c[key], (int, float)), f"{key} is a number or omitted")
    need(ctx.me.get("actor"), "whoami names the caller")
    if ctx.raw.ok:
        paged: list[str] = []
        after = None
        for _ in range(10):
            page = ctx.raw.channels(ctx.c, after=after, limit=1)
            eq(len(page["channels"]) <= 1, True, "pages of at most 1")
            paged += [c["path"] for c in page["channels"]]
            after = page.get("next")
            if not after:
                break
        eq(sorted(paged), sorted(paths), "the union of pages is the unpaged list")


@check("c24", "event_cursors", "core", v=True)
def c24_event_cursors(ctx: Ctx) -> None:
    ctx.post(ctx.p, ctx.c, "creates the channel")
    cur = ctx.p.head()
    mid = ctx.post(ctx.p, ctx.c, "event")["message"]["id"]
    ev = ctx.p.wait(ctx.c, after=cur, timeout=2)
    need(ev is not None and ev.get("cursor") is not None, "the post produced an event with its own cursor")
    eq(ev["type"], "message.posted", "event type")
    eq(str(ev["message"]["id"]), str(mid), "event message")
    need(ctx.p.wait(ctx.c, after=ev["cursor"], timeout=0.05) is None, "polling from the event cursor repeats none")
    if ctx.raw.ok:
        page = ctx.raw.events(channel=ctx.c, after=cur)
        need(page.get("cursor") is not None and page["events"], "the page has events and a cursor")
        need(all(e.get("cursor") is not None for e in page["events"]), "each event has a cursor")
        eq(ctx.raw.events(channel=ctx.c, after=page["cursor"])["events"], [], "nothing repeats after the page cursor")


@check("c25", "events_last", "core", needs=("raw",))
def c25_events_last(ctx: Ctx) -> None:
    dev, ops = ctx.sub("dev"), ctx.sub("ops")
    empty = ctx.raw.events(channel=dev, last=1)
    eq(empty["events"], [], "an empty channel has no events")
    need(empty.get("cursor") is not None, "and still a cursor")
    first = ctx.post(ctx.p, dev, "first")["message"]["id"]
    caught = ctx.raw.events(channel=dev, after=empty["cursor"])
    need(any(_mid(e) == first for e in caught["events"]), "the cursor later catches the first event")
    mids = [ctx.post(ctx.p, dev, f"d{i}")["message"]["id"] for i in range(3)]
    last2 = ctx.raw.events(channel=dev, last=2)
    eq([_mid(e) for e in last2["events"]], mids[1:], "the 2 newest, oldest first")
    eq(len(ctx.raw.events(channel=dev, last=1)["events"]), 1, "last=1")
    tail = ctx.post(ctx.p, dev, "tail")["message"]["id"]
    cont = ctx.raw.events(channel=dev, after=last2["cursor"])
    need(tail in [_mid(e) for e in cont["events"]], "no event is lost after the cursor")
    expect_err("invalid_request", ctx.raw.events, channel=dev, last=1, after=last2["cursor"])
    expect_err("invalid_request", ctx.raw.events, channel=dev, last=1, wait=1)
    expect_err("invalid_request", ctx.raw.events, channel=dev, last=101)
    need(ctx.raw.events(last=1)["events"], "board-wide scope")
    need(ctx.raw.events(channel=ctx.c + "/", last=1)["events"], "tree scope")
    for i in range(205):                                              # more than one scan page of unrelated events
        ctx.post(ctx.p, ops, f"o{i}")
    want = ctx.post(ctx.p, dev, "buried")["message"]["id"]
    for i in range(205):
        ctx.post(ctx.p, ops, f"p{i}")
    got = ctx.raw.events(channel=dev, last=1)["events"]
    eq([_mid(e) for e in got], [str(want)], "counting looks past a page of unrelated events")


@check("c26", "long_poll", "core", v=True)
def c26_long_poll(ctx: Ctx) -> None:
    ctx.post(ctx.p, ctx.c, "creates the channel")
    cur = ctx.p.head()
    box: list[Any] = []
    t0 = time.monotonic()
    th = threading.Thread(target=lambda: box.append(ctx.threaded(ctx.p2, lambda: ctx.p2.wait(ctx.c, after=cur, timeout=10))), daemon=True)
    th.start()
    time.sleep(0.15)
    mid = ctx.post(ctx.p, ctx.c, "wakes the waiter")["message"]["id"]
    th.join(15)
    need(not th.is_alive(), "the waiter returned")
    ev = box[0] if box else None
    need(ev is not None and str(ev["message"]["id"]) == str(mid), "the long poll returned the new post")
    need(time.monotonic() - t0 < 9, "and returned promptly")
    if ctx.raw.ok:
        t1 = time.monotonic()
        eq(ctx.raw.events(channel=ctx.c, after=ev["cursor"], wait=0)["events"], [], "wait=0 returns at once")
        need(time.monotonic() - t1 < 2, "wait=0 does not block")
        expect_err("invalid_request", ctx.raw.events, channel=ctx.c, after=ev["cursor"], wait=301)


@check("c27", "event_state_views", "core")
def c27_event_state_views(ctx: Ctx) -> None:
    ctx.post(ctx.p, ctx.c, "creates the channel")
    cur = ctx.p.head()
    m1 = ctx.post(ctx.p, ctx.c, "orig")["message"]["id"]
    m2 = ctx.post(ctx.p, ctx.c, "to delete")["message"]["id"]
    ctx.p.edit(m1, "edited now")
    ctx.p.delete(m2)
    ctx.p.react(m1, "eyes")
    ctx.p.react(m1, "eyes", remove=True)
    e1 = ctx.p.wait(ctx.c, after=cur, timeout=2)
    need(e1 is not None and str(e1["message"]["id"]) == str(m1), "first event is the first post")
    eq(e1["message"]["text"], "edited now", "events show a message as of response time")
    need(not (e1["message"].get("reactions") or {}).get("eyes"), "a removed reaction is omitted")
    e2 = ctx.p.wait(ctx.c, after=e1["cursor"], timeout=2)
    need(e2 is not None and str(e2["message"]["id"]) == str(m2), "second event is the second post")
    need(e2["message"]["deleted"] is True and e2["message"]["text"] == "", "a deleted message is a tombstone")


@check("c28", "filtered_feed_ready_label", "core", v=True)
def c28_filtered_feed_ready_label(ctx: Ctx) -> None:
    ctx.post(ctx.p, ctx.c, "creates the channel")
    cur = ctx.p.head()
    tid = ctx.task_id(ctx.p, ctx.c, "feed")
    ev = ctx.p.wait(ctx.c, after=cur, timeout=2, label=["board.task=ready"])
    need(ev is not None and _mid(ev) == tid, "a ready task appears in the ready feed")
    ctx.p.task_claim(tid)
    cur2 = ctx.p.head()
    ctx.p.task_release(tid)
    back = ctx.p.wait(ctx.c, after=cur2, timeout=2, label=["board.task=ready"])
    need(back is not None and _mid(back) == tid, "a released task reappears in the ready feed")
    eq(ctx.p.read(ctx.c, label=["board.task=claimed"])["threads"], [], "and leaves the claimed filter")


# ============================================================================================= D. task lifecycle
@check("c33", "task_create", "tasks", v=True)
def c33_task_create(ctx: Ctx) -> None:
    doc = ctx.task(ctx.p, ctx.c, "a task", labels={"k": "v"})
    msg = doc["task"]
    task = msg["task"]
    eq(task["state"], "ready", "state")
    eq(task["owner"], ctx.me["principal"], "owner")
    eq(task["by"], ctx.me["actor"], "by")
    eq(task["needs"], [], "needs")
    eq(task["epoch"], 0, "epoch")
    need(task["claim"] is None, "no claim")
    eq(msg["labels"].get("board.task"), "ready", "board.task label")
    tid = str(msg["id"])
    need(any(t["message"]["id"] == tid for t in ctx.p.read(ctx.c, label=["board.task=ready"])["threads"]),
         "threads(label board.task=ready) finds it")
    listed = next((t for t in ctx.p.tasks(ctx.c)["tasks"] if t["message"]["id"] == tid), None)
    need(listed is not None and "replies" in listed and "last" in listed, "tasks.list rows carry replies and last")


@check("c34", "adopt_thread", "tasks", v=True)
def c34_adopt_thread(ctx: Ctx) -> None:
    root = ctx.post(ctx.p, ctx.c, "an existing thread")["message"]["id"]
    adopted = ctx.p.task_adopt(root)
    eq(adopted["task"]["text"], "an existing thread", "adopting keeps the text")
    eq(adopted["task"]["task"]["state"], "ready", "state")
    expect_err("already_a_task", ctx.p.task_adopt, root)
    plain = ctx.post(ctx.p, ctx.c, "plain")["message"]["id"]
    expect_err("not_a_task", ctx.p.task_claim, plain)
    rep = ctx.reply(ctx.p, plain, "a reply")["message"]["id"]
    expect_err("invalid_request", ctx.p.task_adopt, rep)
    dead = ctx.post(ctx.p, ctx.c, "dead")["message"]["id"]
    ctx.p.delete(dead)
    expect_err("message_deleted", ctx.p.task_adopt, dead)


@check("c35", "create_idempotency", "tasks", v=True)
def c35_create_idempotency(ctx: Ctx) -> None:
    key = f"conf-{ctx.run_id}-c35"
    first = ctx.task(ctx.p, ctx.c, "once", claim=True, idempotency_key=key)
    tid = str(first["task"]["id"])
    need(first.get("claim") is not None, "the first create-and-claim carries a claim")
    again = ctx.task(ctx.p, ctx.c, "once", claim=True, idempotency_key=key)
    eq(str(again["task"]["id"]), tid, "the same id gives the same task")
    need(not again.get("claim"), "a retry does not re-answer the original claim")
    task = ctx.tk(ctx.p, tid)
    eq((task["state"], task["epoch"]), ("claimed", 1), "still claimed at epoch 1")
    ctx.p.task_complete(tid)                                            # the original claim still works
    plain_key = f"conf-{ctx.run_id}-c35-plain"
    plain = ctx.post(ctx.p, ctx.c, "plain", idempotency_key=plain_key)["message"]["id"]
    before = ctx.tids(ctx.c, "all")
    expect_err("duplicate_id", ctx.task, ctx.p, ctx.c, "dup", idempotency_key=plain_key)
    eq(ctx.tids(ctx.c, "all"), before, "a refused create made nothing")
    need(ctx.msg(ctx.p, plain).get("task") is None, "the plain thread stays a non-task")


@check("c36", "create_and_claim", "tasks", v=True)
def c36_create_and_claim(ctx: Ctx) -> None:
    doc = ctx.task(ctx.p, ctx.c, "claimed at birth", claim=True, lease=300)
    eq(doc["task"]["task"]["state"], "claimed", "state")
    eq(doc["claim"]["epoch"], 1, "claim.epoch")
    prereq = ctx.task_id(ctx.p, ctx.c, "unfinished prerequisite")
    before = ctx.tids(ctx.c, "all")
    expect_err("task_waiting", ctx.task, ctx.p, ctx.c, "waits", needs=[prereq], claim=True)
    eq(ctx.tids(ctx.c, "all"), before, "a refused create-and-claim made nothing")
    expect_err("task_assigned", ctx.task, ctx.p, ctx.c, "for someone else", for_="conf-b", claim=True)
    eq(ctx.tids(ctx.c, "all"), before, "and nothing for an assignment either")


@check("c37", "claim_race_one_winner", "tasks", v=True)
def c37_claim_race_one_winner(ctx: Ctx) -> None:
    tid = ctx.task_id(ctx.p, ctx.c, "contended")
    cur = ctx.p.head()
    ports = [ctx.p, ctx.p2, ctx.port("conf-d"), ctx.port("conf-e")]
    res = par([lambda pt=pt: ctx.threaded(pt, lambda: pt.task_claim(tid)) for pt in ports])
    wins = [r for r in res if isinstance(r, dict)]
    eq(len(wins), 1, "concurrent claims that succeeded")
    for r in res:
        if not isinstance(r, dict):
            need(isinstance(r, VBoardError) and r.code == "task_claimed", f"the losers get task_claimed: {r!r}")
    won = wins[0]
    task = ctx.tk(ctx.p, tid)
    eq(task["state"], "claimed", "state")
    eq(task["claim"]["until"], won["claim"]["until"], "claim.until matches")
    need(task["claim"]["actor"].startswith(ctx.me["principal"]), "claim.actor is the caller's actor")
    eq(ctx.msg(ctx.p, tid)["labels"].get("board.task"), "claimed", "label")
    if ctx.raw.ok:
        claimed = [e for e in ctx.task_events(tid, cur, ctx.c) if e["type"] == "task.claimed"]
        eq(len(claimed), 1, "task.claimed events in the log")


@check("c38", "epoch_fencing", "tasks", v=True)
def c38_epoch_fencing(ctx: Ctx) -> None:
    tid = ctx.task_id(ctx.p, ctx.c, "fenced")
    c1 = ctx.p.task_claim(tid)
    eq(c1["claim"]["epoch"], 1, "first claim epoch")
    if ctx.supports_proofs(ctx.p):
        epoch, token = ctx.proof(ctx.p, tid)
        until = c1["claim"]["until"]
        cur = ctx.p.head()
        ctx.inject(ctx.p, tid, epoch, "w" * 43, until)                    # right epoch, wrong token
        expect_err("claim_lost", ctx.p.task_renew, tid)
        ctx.inject(ctx.p, tid, epoch, "w" * 43, until)
        expect_err("claim_lost", ctx.p.task_complete, tid)
        ctx.inject(ctx.p, tid, epoch + 7, token, until)                   # right token, wrong epoch
        expect_err("claim_lost", ctx.p.task_fail, tid, "nope")
        eq(ctx.p.head(), cur, "refused writes appended nothing")
        ctx.inject(ctx.p, tid, epoch, token, until)
    ctx.p.task_release(tid)
    c2 = ctx.p2.task_claim(tid)
    eq(c2["claim"]["epoch"], 2, "after a release the next claim is the previous epoch + 1")
    if ctx.supports_proofs(ctx.p):
        ctx.inject(ctx.p, tid, epoch, token, until)                       # the old holder retries, stale
        err = expect_err("claim_lost", ctx.p.task_complete, tid)
        need("conf-b" not in err.message, "the error never says who holds the task now")
    ctx.p2.task_complete(tid)


@check("c39", "release", "tasks", v=True)
def c39_release(ctx: Ctx) -> None:
    tid = ctx.task_id(ctx.p, ctx.c, "released")
    c1 = ctx.p.task_claim(tid)
    proof = ctx.proof(ctx.p, tid) if ctx.supports_proofs(ctx.p) else None
    ctx.p.task_release(tid)
    task = ctx.tk(ctx.p, tid)
    eq(task["state"], "ready", "state")
    need(task["claim"] is None, "claim cleared")
    eq(task["epoch"], 1, "epoch retained")
    if proof:
        ctx.inject(ctx.p, tid, *proof, c1["claim"]["until"])
        expect_err("claim_lost", ctx.p.task_renew, tid)


@check("c40", "complete_fail", "tasks", v=True)
def c40_complete_fail(ctx: Ctx) -> None:
    a = ctx.task_id(ctx.p, ctx.c, "completes", claim=True)
    expect_err("invalid_request", ctx.p.task_complete, a, "x" * 1001)
    ctx.p.task_complete(a, "all good")
    task = ctx.tk(ctx.p, a)
    eq((task["state"], task["result"]), ("completed", "all good"), "completed with a result")
    need(task["claim"] is None, "claim cleared")
    b = ctx.task_id(ctx.p, ctx.c, "no result", claim=True)
    ctx.p.task_complete(b)
    need(ctx.tk(ctx.p, b)["result"] is None, "a result is optional")
    c = ctx.task_id(ctx.p, ctx.c, "fails", claim=True)
    expect_err("invalid_request", ctx.p.task_fail, c, "")
    ctx.p.task_fail(c, "broken")
    task = ctx.tk(ctx.p, c)
    eq((task["state"], task["reason"], task["result"]), ("failed", "broken", None), "failed with a reason")
    for done in (a, c):
        expect_err("task_finished", ctx.p.task_claim, done)


@check("c41", "reopen", "tasks", v=True)
def c41_reopen(ctx: Ctx) -> None:
    f = ctx.task_id(ctx.p, ctx.c, "failed", claim=True)
    ctx.p.task_fail(f, "oops")
    ctx.p.task_reopen(f)
    task = ctx.tk(ctx.p, f)
    eq((task["state"], task["reason"], task["epoch"]), ("ready", None, 1), "reopen: ready, no reason, epoch kept")
    d = ctx.task_id(ctx.p, ctx.c, "completed", claim=True)
    ctx.p.task_complete(d)
    expect_err("task_finished", ctx.p.task_reopen, d)


@check("c42", "cancel", "tasks", v=True)
def c42_cancel(ctx: Ctx) -> None:
    prereq = ctx.task_id(ctx.p, ctx.c, "prereq")
    waiting = ctx.task_id(ctx.p, ctx.c, "waiting", needs=[prereq])
    ready = ctx.task_id(ctx.p, ctx.c, "ready")
    claimed = ctx.task_id(ctx.p, ctx.c, "claimed", claim=True)
    blocked = ctx.task_id(ctx.p, ctx.c, "blocked")
    ctx.p.task_block(blocked, "waiting for a person")
    for tid in (waiting, ready, claimed, blocked):
        ctx.p.task_cancel(tid, "not needed")
        task = ctx.tk(ctx.p, tid)
        eq((task["state"], task["reason"]), ("canceled", "not needed"), f"cancel {tid}")
        need(task["claim"] is None and task["blocked"] is None, "claim and block cleared")
        expect_err("task_finished", ctx.p.task_claim, tid)
        expect_err("task_finished", ctx.p.task_cancel, tid, "again")
    done = ctx.task_id(ctx.p, ctx.c, "done", claim=True)
    ctx.p.task_complete(done)
    expect_err("task_finished", ctx.p.task_cancel, done, "too late")


@check("c43", "claim_precondition_order", "tasks", v=True)
def c43_claim_precondition_order(ctx: Ctx) -> None:
    pre = ctx.task_id(ctx.p, ctx.c, "pre")
    waiting = ctx.task_id(ctx.p, ctx.c, "w", needs=[pre])
    expect_err("task_waiting", ctx.p2.task_claim, waiting)
    blocked = ctx.task_id(ctx.p, ctx.c, "b")
    ctx.p.task_block(blocked, "hold")
    expect_err("task_blocked", ctx.p2.task_claim, blocked)
    finished = ctx.task_id(ctx.p, ctx.c, "f", claim=True)
    ctx.p.task_complete(finished)
    expect_err("task_finished", ctx.p2.task_claim, finished)
    assigned = ctx.task_id(ctx.p, ctx.c, "a", for_="conf-b")
    expect_err("task_assigned", ctx.p.task_claim, assigned)
    held = ctx.task_id(ctx.p, ctx.c, "h", claim=True)
    expect_err("task_claimed", ctx.p2.task_claim, held)


@check("c44", "idempotent_reclaim", "tasks", needs=("proofs",))
def c44_idempotent_reclaim(ctx: Ctx) -> None:
    tid = ctx.task_id(ctx.p, ctx.c, "reclaim")
    first = ctx.p.task_claim(tid)
    _epoch, token = ctx.proof(ctx.p, tid)
    cur = ctx.p.head()
    again = ctx.raw.claim_with_token(tid, token)
    claim = again["claim"]
    eq(claim["epoch"], first["claim"]["epoch"], "same epoch")
    eq(claim["until"], first["claim"]["until"], "same until")
    eq(ctx.p.head(), cur, "no new event")


@check("c45", "lease_bounds", "tasks", v=True)
def c45_lease_bounds(ctx: Ctx) -> None:
    tid = ctx.task_id(ctx.p, ctx.c, "leased")
    for bad in (29, -1):
        expect_err("invalid_request", ctx.p.task_claim, tid, lease=bad)
    if ctx.caps.lease_cap_s >= 3600:
        expect_err("invalid_request", ctx.p.task_claim, tid, lease=3601)
        expect_err("invalid_request", ctx.p.task_claim, tid, lease=7200)
    before = ctx.tids(ctx.c, "all")
    expect_err("invalid_request", ctx.task, ctx.p, ctx.c, "never made", claim=True, lease=29)
    eq(ctx.tids(ctx.c, "all"), before, "a refused create-and-claim made nothing")
    need(ctx.state(tid) == "ready", "refused claims left the task alone")
    ctx.p.task_claim(tid, lease=30)                                       # the lower bound itself is fine
    ctx.p.task_release(tid)
    expect_err("invalid_request", ctx.p.task_claim_next, ctx.c, lease=29)


@check("c46", "until_math", "tasks", v=True)
def c46_until_math(ctx: Ctx) -> None:
    tid = ctx.task_id(ctx.p, ctx.c, "until")
    now = ctx.now_ms()
    doc = ctx.p.task_claim(tid, lease=300)
    until = parse_iso(doc["claim"]["until"])
    need(295_000 <= until - now <= 320_000, f"claim lease 300 ends in ~300 s (got {(until - now) / 1000:.1f})")
    if ctx.tick:
        ctx.tick(10)
    now = ctx.now_ms()
    renewed = ctx.p.task_renew(tid, lease=60)
    until = parse_iso(renewed["claim"]["until"])
    if ctx.backend == "vercel" and renewed["claim"] == doc["claim"]:
        need(until > now, "an early hosted renew preserves the active grant")
    else:
        need(55_000 <= until - now <= 75_000, f"renew 60 sets until = now + 60 (got {(until - now) / 1000:.1f})")
    now = ctx.now_ms()
    default = ctx.p.task_renew(tid)
    until = parse_iso(default["claim"]["until"])
    need(until - now >= 295_000, "a renew without a lease uses the default again")
    ctx.p.task_release(tid)
    ctx.p.task_claim(tid)
    need(parse_iso(ctx.tk(ctx.p, tid)["claim"]["until"]) - ctx.now_ms() >= 290_000,
         "a claim without a lease lasts at least ~295 s")


@check("c47", "lapsed_claim", "tasks", needs=("clock",))
def c47_lapsed_claim(ctx: Ctx) -> None:
    a = ctx.task_id(ctx.p, ctx.c, "lapses")
    b = ctx.task_id(ctx.p, ctx.c, "lapses before renew")
    c = ctx.task_id(ctx.p, ctx.c, "lapses before completion")
    for tid in (a, b, c):
        ctx.p.task_claim(tid, lease=30)
    ctx.tick(31)
    need(a in ctx.tids(ctx.c, "ready", ctx.p2), "a lapsed claim is listed as ready")
    need(a not in ctx.tids(ctx.c, "claimed", ctx.p2), "and not as claimed")
    expect_err("claim_lost", ctx.p.task_complete, a)
    c2 = ctx.p2.task_claim(a)
    eq(c2["claim"]["epoch"], 2, "anyone can claim a lapsed task")
    expect_err("claim_lost", ctx.p.task_renew, b)                        # even if nobody took it
    expect_err("claim_lost", ctx.p.task_complete, c)


@check("c48", "expiry_event", "tasks", needs=("clock", "raw"))
def c48_expiry_event(ctx: Ctx) -> None:
    tid = ctx.task_id(ctx.p, ctx.c, "expires")
    cur = ctx.p.head()
    ctx.p.task_claim(tid, lease=30)
    ctx.tick(31)
    ctx.p2.task_claim(tid)
    types = [(e["type"], (e.get("payload") or {}).get("epoch")) for e in ctx.task_events(tid, cur, ctx.c)
             if e["type"].startswith("task.") and e["type"] in ("task.claimed", "task.expired")]
    eq(types, [("task.claimed", 1), ("task.expired", 1), ("task.claimed", 2)], "claim, expiry, takeover in order")
    kept = ctx.task_id(ctx.p, ctx.c, "renewed")
    ctx.p.task_claim(kept, lease=30)
    ctx.tick(20)
    ctx.p.task_renew(kept, lease=60)
    ctx.tick(20)
    ctx.p.wait(ctx.c, after=ctx.p.head(), timeout=0.05)               # gives the board a chance to sweep
    eq(ctx.state(kept), "claimed", "a renewed claim's earlier deadline does nothing")
    done = ctx.task_id(ctx.p, ctx.c, "settled in time")
    cur2 = ctx.p.head()
    ctx.p.task_claim(done, lease=30)
    ctx.p.task_complete(done)
    ctx.tick(31)
    ctx.p.wait(ctx.c, after=ctx.p.head(), timeout=0.05)
    need(not [e for e in ctx.task_events(done, cur2, ctx.c) if e["type"] == "task.expired"],
         "a claim settled before its deadline leaves no expiry event")


@check("c49", "claim_next_order", "tasks", v=True)
def c49_claim_next_order(ctx: Ctx) -> None:
    q = ctx.sub("q")
    a = ctx.task_id(ctx.p, q, "oldest", labels={"kind": "x"})
    b = ctx.task_id(ctx.p, q, "newer", labels={"kind": "x", "extra": "1"})
    expect_err_none = ctx.p.task_claim_next(q, label={"kind": "nope"})
    need(expect_err_none is None, "no task matches every label: None")
    got = ctx.p.task_claim_next(q, label={"kind": "x", "extra": "1"})
    eq(str(got["task"]["id"]), b, "requires ALL labels given")
    got = ctx.p.task_claim_next(q)
    eq(str(got["task"]["id"]), a, "the oldest ready task")
    need(ctx.p.task_claim_next(q) is None, "none left: None")
    mine = ctx.sub("mine")
    ctx.task_id(ctx.p, mine, "older unassigned")
    assigned = ctx.task_id(ctx.p, mine, "assigned to me", for_=ctx.me["profile"] if ctx.me.get("profile") else "conf-a")
    got = ctx.p.task_claim_next(mine)
    eq(str(got["task"]["id"]), assigned, "a task assigned to the caller is taken before an older unassigned one")
    other = ctx.sub("other")
    ctx.task_id(ctx.p, other, "for conf-b", for_="conf-b")
    need(ctx.p.task_claim_next(other) is None, "assigned-to-other tasks are skipped")
    if ctx.caps.subtree_read:
        parent = ctx.sub("tree")
        child = ctx.task_id(ctx.p, parent + "/kid", "in the child")
        need(ctx.p.task_claim_next(parent) is None, "a bare channel excludes its children")
        got = ctx.p.task_claim_next(parent + "/")
        eq(str(got["task"]["id"]), child, "a trailing slash includes the subtree")


@check("c50", "claim_next_wait", "tasks", v=True)
def c50_claim_next_wait(ctx: Ctx) -> None:
    q = ctx.sub("wait")
    ctx.post(ctx.p, q, "creates the channel")
    duration = 1.0 if ctx.backend == "vercel" else 0.2
    t0 = time.monotonic()
    need(ctx.p.task_claim_next(q, wait=duration) is None, "with no task the call returns None after the wait")
    need(time.monotonic() - t0 >= duration - 0.05, "and only after the wait elapsed")
    box: list[Any] = []
    th = threading.Thread(target=lambda: box.append(ctx.threaded(ctx.p2, lambda: ctx.p2.task_claim_next(q, wait=10))), daemon=True)
    th.start()
    time.sleep(0.15)
    tid = ctx.task_id(ctx.p, q, "wakes the waiter")
    th.join(15)
    need(not th.is_alive() and box and box[0], "a waiting claim returns a task that appears")
    eq(str(box[0]["task"]["id"]), tid, "the new task")
    box2: list[Any] = []
    th = threading.Thread(target=lambda: box2.append(ctx.threaded(ctx.p, lambda: ctx.p.task_claim_next(q, wait=10))), daemon=True)
    th.start()
    time.sleep(0.15)
    ctx.p2.task_release(tid)
    th.join(15)
    need(not th.is_alive() and box2 and box2[0], "a released task is claimed by the waiter")
    if ctx.backend == "local":
        expect_err("invalid_request", ctx.p.task_claim_next, q, wait=301)


@check("c51", "claim_next_from", "tasks", needs=("other",))
def c51_claim_next_from(ctx: Ctx) -> None:
    q = ctx.sub("from")
    theirs = ctx.task_id(ctx.o, q, "their task")
    need(ctx.p.task_claim_next(q) is None, "other owners' tasks are not taken by default")
    principal = ctx.o.whoami()["principal"]
    got = ctx.p.task_claim_next(q, from_=[principal])
    eq(str(got["task"]["id"]), theirs, "from=[principal] opens it")
    expect_err("invalid_from", ctx.p.task_claim_next, q, from_=["somebody"])


@check("c52", "claim_needs_label", "tasks", needs=("narrow",))
def c52_claim_needs_label(ctx: Ctx) -> None:
    q = ctx.sub("narrow")
    tid = ctx.task_id(ctx.p, q, "needs the label permission")
    expect_err("forbidden", ctx.nar.task_claim, tid)
    expect_err("forbidden", ctx.nar.task_claim_next, q)
    eq(ctx.state(tid), "ready", "the task is unchanged")


# ============================================================================================= F. needs
@check("c53", "needs_waiting", "tasks", v=True)
def c53_needs_waiting(ctx: Ctx) -> None:
    fix = ctx.task_id(ctx.p, ctx.c, "fix")
    docs = ctx.task_id(ctx.p, ctx.c, "docs")
    tid = ctx.task_id(ctx.p, ctx.c, "waits", needs=[fix, docs, fix])
    msg = ctx.msg(ctx.p, tid)
    eq(msg["task"]["state"], "waiting", "state")
    eq(msg["labels"].get("board.task"), "waiting", "label")
    eq(msg["task"]["needs"], [fix, docs], "needs are deduped, order kept")
    expect_err("task_waiting", ctx.p.task_claim, tid)
    for n in (fix, docs):
        ctx.p.task_claim(n)
        ctx.p.task_complete(n)
    cur = ctx.p.head()
    ready = ctx.task_id(ctx.p, ctx.c, "ready at creation", needs=[fix])
    eq(ctx.state(ready), "ready", "all needs completed: ready at creation")
    if ctx.raw.ok:
        seq = [e["type"] for e in ctx.task_events(ready, cur, ctx.c)]
        eq([t for t in seq if t.startswith("task.")], ["task.created", "task.ready"], "task.ready follows task.created")


@check("c54", "readiness_rule", "tasks", v=True)
def c54_readiness_rule(ctx: Ctx) -> None:
    a = ctx.task_id(ctx.p, ctx.c, "prereq", claim=True)
    b = ctx.task_id(ctx.p, ctx.c, "dependent", needs=[a])
    ctx.p.task_fail(a, "broke")
    eq(ctx.state(b), "waiting", "a failed prerequisite does not ready the dependent")
    ctx.p.task_reopen(a)
    ctx.p.task_claim(a)
    cur = ctx.p.head()
    ctx.p.task_complete(a)
    eq(ctx.state(b), "ready", "reopened then completed readies it")
    if ctx.raw.ok:
        evs = ctx.task_events(a, cur, ctx.c) + [e for e in ctx.task_events(b, cur, ctx.c) if e["type"] == "task.ready"]
        done = next((e for e in evs if e["type"] == "task.completed"), None)
        rdy = next((e for e in evs if e["type"] == "task.ready"), None)
        need(done is not None and rdy is not None, "the completion and the readiness are both in the log")
        try:
            eq(int(rdy["cursor"]), int(done["cursor"]) + 1, "adjacent cursors")
        except ValueError:
            pass
    c = ctx.task_id(ctx.p, ctx.c, "prereq 2")
    d = ctx.task_id(ctx.p, ctx.c, "dependent 2", needs=[c])
    ctx.p.task_cancel(c, "never mind")
    need(ctx.state(d) != "ready", "a canceled prerequisite does not ready the dependent")


@check("c55", "needs_validation", "tasks", v=True)
def c55_needs_validation(ctx: Ctx) -> None:
    plain = ctx.post(ctx.p, ctx.c, "not a task")["message"]["id"]
    expect_err("invalid_needs", ctx.task, ctx.p, ctx.c, "x", needs=[plain])
    tid = ctx.task_id(ctx.p, ctx.c, "self")
    expect_err("invalid_needs", ctx.p.task_needs, tid, [tid])
    dead = ctx.task_id(ctx.p, ctx.c, "canceled")
    ctx.p.task_cancel(dead, "gone")
    expect_err("invalid_needs", ctx.task, ctx.p, ctx.c, "x", needs=[dead])
    expect_err("invalid_request", ctx.task, ctx.p, ctx.c, "x", needs=[str(900_000 + i) for i in range(21)])
    thread = ctx.post(ctx.p, ctx.c, "an existing thread")["message"]["id"]
    expect_err("invalid_needs", ctx.p.task_adopt, thread, needs=[plain])
    need(ctx.msg(ctx.p, thread).get("task") is None, "the thread stays a non-task on refusal")
    failed = ctx.task_id(ctx.p, ctx.c, "failed prereq", claim=True)
    ctx.p.task_fail(failed, "broke")
    waiter = ctx.task_id(ctx.p, ctx.c, "waits on a failed one", needs=[failed])
    eq(ctx.state(waiter), "waiting", "a failed prerequisite is accepted")


@check("c56", "change_needs", "tasks", v=True)
def c56_change_needs(ctx: Ctx) -> None:
    a = ctx.task_id(ctx.p, ctx.c, "a")
    done = ctx.task_id(ctx.p, ctx.c, "done", claim=True)
    ctx.p.task_complete(done)
    t = ctx.task_id(ctx.p, ctx.c, "t")
    ctx.p.task_needs(t, [a])
    eq(ctx.state(t), "waiting", "unmet needs: waiting")
    ctx.p.task_needs(t, [done])
    eq(ctx.state(t), "ready", "completed needs: ready in the same write")
    ctx.p.task_needs(t, [a])
    ctx.p.task_needs(t, [])
    eq(ctx.state(t), "ready", "[] is ready at once")
    ctx.p.task_needs(t, [a])
    cur = ctx.p.head()
    ctx.p.task_needs(t, [a])
    eq(ctx.p.head(), cur, "the same set again changes nothing")
    ctx.p.task_needs(t, [])
    ctx.p.task_block(t, "hold")
    ctx.p.task_needs(t, [a])
    eq(ctx.state(t), "blocked", "a blocked task stays blocked")
    ctx.p.task_unblock(t)
    eq(ctx.state(t), "waiting", "and lands where the needs put it after unblock")


@check("c57", "cycle_refusal", "tasks", v=True)
def c57_cycle_refusal(ctx: Ctx) -> None:
    t1 = ctx.task_id(ctx.p, ctx.c, "t1")
    t2 = ctx.task_id(ctx.p, ctx.c, "t2", needs=[t1])
    t3 = ctx.task_id(ctx.p, ctx.c, "t3", needs=[t2])
    expect_err("invalid_needs", ctx.p.task_needs, t1, [t3])               # transitive
    expect_err("invalid_needs", ctx.p.task_needs, t1, [t2])               # direct
    eq(ctx.tk(ctx.p, t1)["needs"], [], "a refused change leaves the task unchanged")
    ctx.p.task_needs(t2, [])
    ctx.p.task_needs(t1, [t3])                                            # allowed once the middle is cleared
    eq(ctx.state(t1), "waiting", "the same change now lands")


@check("c58", "needs_only_unclaimed", "tasks", v=True)
def c58_needs_only_unclaimed(ctx: Ctx) -> None:
    a = ctx.task_id(ctx.p, ctx.c, "a")
    t = ctx.task_id(ctx.p, ctx.c, "t", claim=True)
    expect_err("task_claimed", ctx.p.task_needs, t, [a])
    ctx.p.task_release(t)
    ctx.p.task_needs(t, [a])
    eq(ctx.state(t), "waiting", "the change lands after release")
    ctx.p.task_cancel(t, "x")
    expect_err("task_finished", ctx.p.task_needs, t, [])


@check("c59", "dependents_listing", "tasks", needs=("raw",))
def c59_dependents_listing(ctx: Ctx) -> None:
    x = ctx.task_id(ctx.p, ctx.c, "x")
    dep = ctx.task_id(ctx.p, ctx.c, "dependent", needs=[x])
    ctx.task_id(ctx.p, ctx.c, "unrelated")
    other = ctx.task_id(ctx.p, ctx.sub("elsewhere"), "dependent elsewhere", needs=[x])
    got = [str(t["message"]["id"]) for t in ctx.raw.tasks(channel=ctx.c, needs=x, limit=500)["tasks"]]
    eq(got, [dep], "only tasks that list X in needs, in this channel")
    tree = [str(t["message"]["id"]) for t in ctx.raw.tasks(channel=ctx.c + "/", needs=x, limit=500)["tasks"]]
    need(dep in tree and other in tree, "a trailing slash covers the subtree")


# ============================================================================================= G. blocks, assignment
@check("c61", "block_unblock", "tasks", v=True)
def c61_block_unblock(ctx: Ctx) -> None:
    t = ctx.task_id(ctx.p, ctx.c, "blockable")
    ctx.p.task_block(t, "needs a person", kind="review")
    task = ctx.tk(ctx.p, t)
    eq(task["state"], "blocked", "state")
    eq(task["blocked"], {"kind": "review", "reason": "needs a person"}, "blocked")
    expect_err("task_blocked", ctx.p.task_block, t, "again")
    expect_err("task_blocked", ctx.p2.task_claim, t)
    ctx.p.task_unblock(t)
    eq(ctx.state(t), "ready", "unblock without needs: ready")
    cur = ctx.p.head()
    ctx.p.task_unblock(t)
    eq(ctx.p.head(), cur, "unblock of a non-blocked task is a no-op")
    plain = ctx.task_id(ctx.p, ctx.c, "kinds")
    ctx.p.task_block(plain, "default kind")
    eq(ctx.tk(ctx.p, plain)["blocked"]["kind"], None, "kind defaults to null")
    ctx.p.task_unblock(plain)
    expect_err("invalid_request", ctx.p.task_block, plain, "")
    expect_err("invalid_request", ctx.p.task_block, plain, "x", kind="Bad Kind")
    expect_err("invalid_request", ctx.p.task_block, plain, "x", kind="edited")
    held = ctx.task_id(ctx.p, ctx.c, "held by p2")
    ctx.p2.task_claim(held)
    expect_err("task_claimed", ctx.p.task_block, held, "owner cannot block a claimed task")
    proof = ctx.proof(ctx.p2, held) if ctx.supports_proofs(ctx.p2) else None
    until = ctx.tk(ctx.p, held)["claim"]["until"]
    ctx.p2.task_block(held, "the holder can")
    task = ctx.tk(ctx.p, held)
    need(task["state"] == "blocked" and task["claim"] is None, "the holder's block ends the claim")
    if proof:
        ctx.inject(ctx.p2, held, *proof, until)
        expect_err("claim_lost", ctx.p2.task_complete, held)
    pre = ctx.task_id(ctx.p, ctx.c, "needed")
    waiter = ctx.task_id(ctx.p, ctx.c, "has needs", needs=[pre])
    ctx.p.task_block(waiter, "pause")
    ctx.p.task_unblock(waiter)
    eq(ctx.state(waiter), "waiting", "unblock with needs: waiting")


@check("c62", "edit_hold", "tasks", needs=("raw",))
def c62_edit_hold(ctx: Ctx) -> None:
    root = ctx.post(ctx.p2, ctx.c, "written by p2")["message"]["id"]
    tid = str(ctx.p.task_adopt(root)["task"]["id"])                     # made by p, written by p2
    ctx.p.task_claim(tid)
    epoch, token = ctx.proof(ctx.p, tid)
    until = ctx.tk(ctx.p, tid)["claim"]["until"]
    cur = ctx.p.head()
    ctx.p2.edit(root, "edited by someone who did not make the task")
    task = ctx.tk(ctx.p, tid)
    eq((task["state"], (task["blocked"] or {}).get("kind")), ("blocked", "edited"), "edit-hold")
    need(task["claim"] is None, "the claim ended")
    types = [e["type"] for e in ctx.task_events(tid, cur, ctx.c) if e["type"] in ("message.edited", "task.blocked")]
    eq(types, ["message.edited", "task.blocked"], "event pair")
    ctx.inject(ctx.p, tid, epoch, token, until)
    expect_err("claim_lost", ctx.p.task_complete, tid)
    ctx.p.task_unblock(tid)
    eq(ctx.state(tid), "ready", "the owner unblocks it")
    own = ctx.task_id(ctx.p, ctx.c, "own task")
    ctx.p.edit(own, "the owner edits its own task")
    eq(ctx.state(own), "ready", "the owner's own edit holds nothing")
    ctx.p.task_cancel(own, "x")
    ctx.p.edit(own, "edit after finishing")
    eq(ctx.state(own), "canceled", "editing a finished task changes nothing about its state")


@check("c63", "delete_cancels", "tasks", v=True)
def c63_delete_cancels(ctx: Ctx) -> None:
    t = ctx.task_id(ctx.p, ctx.c, "doomed", claim=True)
    dep = ctx.task_id(ctx.p, ctx.c, "dependent", needs=[t])
    cur = ctx.p.head()
    ctx.p.delete(t)
    need(t not in ctx.tids(ctx.c, "all"), "a deleted task is excluded from the list")
    eq(ctx.state(dep), "waiting", "dependents stay waiting")
    if ctx.raw.ok:
        types = [e["type"] for e in ctx.task_events(t, cur, ctx.c) if e["type"] in ("task.canceled", "message.deleted")]
        eq(types, ["task.canceled", "message.deleted"], "cancel then delete")
        canceled = next(e for e in ctx.task_events(t, cur, ctx.c) if e["type"] == "task.canceled")
        eq((canceled.get("payload") or {}).get("reason"), "deleted", "reason")
    finished = ctx.task_id(ctx.p, ctx.c, "finished", claim=True)
    ctx.p.task_complete(finished)
    ctx.p.delete(finished)
    task = ctx.msg(ctx.p, finished).get("task")
    need(task is None or task.get("state") == "completed", "deleting a completed task is only a deletion")


@check("c64", "assignment", "tasks", v=True)
def c64_assignment(ctx: Ctx) -> None:
    t = ctx.task_id(ctx.p, ctx.c, "assigned", for_="conf-b")
    expect_err("task_assigned", ctx.p.task_claim, t)
    ctx.p2.task_claim(t)                                                  # the assignee claims
    ctx.p2.task_release(t)
    ctx.p.task_assign(t, None)
    ctx.p.task_claim(t)                                                   # back in the pool
    ctx.p.task_release(t)
    expect_err("invalid_request", ctx.p.task_assign, t, "Not A Profile!")
    done = ctx.task_id(ctx.p, ctx.c, "done", claim=True)
    ctx.p.task_complete(done)
    expect_err("task_finished", ctx.p.task_assign, done, "conf-b")


@check("c65", "mutation_authorization", "tasks", needs=("other",))
def c65_mutation_authorization(ctx: Ctx) -> None:
    t = ctx.task_id(ctx.p, ctx.sub("auth"), "owned by p")
    verbs = [lambda: ctx.o.task_block(t, "x"), lambda: ctx.o.task_unblock(t), lambda: ctx.o.task_reopen(t),
             lambda: ctx.o.task_assign(t, "conf-c"), lambda: ctx.o.task_cancel(t, "x"),
             lambda: ctx.o.task_needs(t, []),
             lambda: ctx.p2.task_cancel(t, "x")]                          # a profile that did not make it
    for verb in verbs:
        expect_err("task_not_owned", verb)
    ctx.o.task_claim(t)                                                   # a non-owner may claim and settle
    ctx.o.task_complete(t, "done by someone else")
    eq(ctx.state(t), "completed", "claim and settle need only the label permission")


# ============================================================================================= H. batches
def _plan(n: int, *, chain: bool = False, prefix: str = "b") -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        item: dict[str, Any] = {"key": f"{prefix}{i}", "text": f"task {i}"}
        if chain and i:
            item["needs"] = [{"key": f"{prefix}{i - 1}"}]
        out.append(item)
    return out


@check("c66", "batch_create", "tasks", v=True)
def c66_batch_create(ctx: Ctx) -> None:
    q = ctx.sub("batch")
    done = ctx.task_id(ctx.p, q, "an already completed prerequisite", claim=True)
    ctx.p.task_complete(done)
    plan = [{"key": "x", "text": "forward reference", "needs": [{"key": "y"}]},
            {"key": "y", "text": "plain"},
            {"key": "z", "text": "needs an old completed task", "needs": [done]}]
    cur = ctx.p.head()
    res = ctx.batch(ctx.p, q, plan, idempotency_key=f"conf-{ctx.run_id}-c66")
    tasks = res["tasks"]
    ids = [str(t["id"]) for t in tasks]
    eq(len(set(ids)), 3, "three distinct ids in the given order")
    eq([ctx.state(i) for i in ids], ["waiting", "ready", "ready"], "per-task state")
    if ctx.raw.ok:
        seq = [e["type"] for e in ctx.task_events(ids[2], cur, q) if e["type"].startswith(("task.", "message.posted"))]
        eq(seq, ["message.posted", "task.created", "task.ready"], "created then ready for an old completed need")
    eq([i for i in ctx.tids(q, "all") if i in ids], ids, "listing returns them in the given order")
    ctx.p.task_claim(ids[1])
    ctx.p.task_complete(ids[1])
    eq(ctx.state(ids[0]), "ready", "completing one batch task readies its dependent")


@check("c67", "batch_all_or_nothing", "tasks", v=True)
def c67_batch_all_or_nothing(ctx: Ctx) -> None:
    q = ctx.sub("atomic")
    plain = ctx.post(ctx.p, ctx.sub("plain"), "not a task")["message"]["id"]
    dead = ctx.task_id(ctx.p, ctx.sub("plain"), "canceled")
    ctx.p.task_cancel(dead, "gone")
    bad: list[tuple[str, list[dict]]] = [
        ("invalid_needs", [{"key": "a", "text": "a", "needs": [{"key": "nobody"}]}]),
        ("invalid_needs", [{"key": "a", "text": "a"}, {"key": "a", "text": "b"}]),
        ("invalid_needs", [{"key": "a", "text": "a", "needs": [{"key": "a"}]}]),
        ("invalid_needs", [{"key": "x", "text": "x", "needs": [{"key": "z"}]},
                           {"key": "y", "text": "y", "needs": [{"key": "x"}]},
                           {"key": "z", "text": "z", "needs": [{"key": "y"}]}]),
        ("invalid_needs", [{"key": "a", "text": "a", "needs": [dead]}]),
        ("invalid_needs", [{"key": "a", "text": "a", "needs": [plain]}]),
        ("invalid_request", [{"key": "a", "text": "a", "for": "Not A Profile!"}]),
        ("invalid_request", [{"key": "a", "text": "   "}]),
    ]
    for i, (code, plan) in enumerate(bad):
        expect_err(code, ctx.batch, ctx.p, q, [{"key": "ok", "text": "fine"}] + plan
                   if i not in (1, 3, 7) else plan, idempotency_key=f"conf-{ctx.run_id}-c67-{i}")
    try:
        left = ctx.tids(q, "all")
    except VBoardError as exc:
        need(exc.code == "channel_not_found", f"unexpected {exc.code}")
        left = []
    eq(left, [], "nothing was created")
    eq([c for c in ctx.p.channels(q)["channels"] if c["path"] == q], [], "the channel was not created either")


@check("c68", "batch_limits", "tasks", v=True)
def c68_batch_limits(ctx: Ctx) -> None:
    q = ctx.sub("limits")
    once = getattr(ctx.p, "_batch_once", None)
    if once is None:
        raise Skip("needs the adapter's single-batch call")
    cap = int(ctx.caps.batch_max or 15)
    over = _plan(cap + 1)
    expect_err("batch_too_large", ctx.batch, ctx.p, q, over, idempotency_key=f"conf-{ctx.run_id}-c68a", single=True)
    over[0]["text"] = "   "
    expect_err("batch_too_large", ctx.batch, ctx.p, q, over, idempotency_key=f"conf-{ctx.run_id}-c68b", single=True)        # size is checked first
    expect_err("invalid_request", ctx.batch, ctx.p, q, [{"key": "a", "text": "   "}],
               idempotency_key=f"conf-{ctx.run_id}-c68c", single=True)
    expect_err("text_too_long", ctx.task, ctx.p, q, "x" * 10_001)
    expect_err("invalid_request", ctx.batch, ctx.p, q,
               [{"key": "k" * 65, "text": "long key"}], idempotency_key=f"conf-{ctx.run_id}-c68d")
    expect_err("invalid_request", ctx.batch, ctx.p, q,
               [{"key": "n", "text": "many", "needs": [str(900_000 + i) for i in range(21)]}],
               idempotency_key=f"conf-{ctx.run_id}-c68e")
    n = min(100, cap * 3) if cap < 100 else 100
    res = ctx.batch(ctx.p, q, _plan(n, chain=True), idempotency_key=f"conf-{ctx.run_id}-c68f")
    eq(len(res["tasks"]), n, f"{n} chained tasks succeed")


@check("c69", "batch_idempotency", "tasks", v=True)
def c69_batch_idempotency(ctx: Ctx) -> None:
    q = ctx.sub("idem")
    key = f"conf-{ctx.run_id}-c69"
    plan = _plan(2, chain=True)
    first = ctx.batch(ctx.p, q, plan, idempotency_key=key)
    ids = [str(t["id"]) for t in first["tasks"]]
    ctx.p.task_claim(ids[0])
    again = ctx.batch(ctx.p, q, plan, idempotency_key=key)
    eq([str(t["id"]) for t in again["tasks"]], ids, "the same id and request returns the same tasks")
    eq(len(ctx.tids(q, "all")), 2, "and creates none")
    changed = _plan(2, chain=True)
    changed[1]["text"] = "another text"
    expect_err("duplicate_id", ctx.batch, ctx.p, q, changed, idempotency_key=key)
    expect_err("duplicate_id", ctx.batch, ctx.p, q, _plan(3, chain=True), idempotency_key=key)
    expect_err("duplicate_id", ctx.batch, ctx.p, ctx.sub("idem2"), plan, idempotency_key=key)


# ============================================================================================= I. event feed
@check("c70", "event_sequences", "tasks", v=True)
def c70_event_sequences(ctx: Ctx) -> None:
    d = ctx.task_id(ctx.p, ctx.c, "dep")
    cur = ctx.p.head()
    t = ctx.task_id(ctx.p, ctx.c, "sequenced", needs=[d])
    ctx.p.task_claim(d)
    ctx.p.task_complete(d)
    ctx.p.task_claim(t)
    ctx.p.task_renew(t)
    ctx.p.task_block(t, "hold")
    ctx.p.task_unblock(t)
    if not ctx.raw.ok:
        eq(ctx.state(t), "ready", "final state")
        return
    evs = [e for e in ctx.task_events(t, cur, ctx.c) if e["type"].startswith("task.")]
    eq([e["type"] for e in evs],
       ["task.created", "task.ready", "task.claimed", "task.blocked", "task.unblocked", "task.ready"],
       "one event per change; renewals are hidden")
    with_all = [e["type"] for e in ctx.task_events(t, cur, ctx.c, all_=True)]
    need("task.renewed" in with_all, "renewals show with all=1")
    ctx.p.task_needs(t, [])
    ev = [e for e in ctx.task_events(t, cur, ctx.c) if e["type"] == "task.needs_changed"]
    need(ev, "a needs change is one task.needs_changed event")


@check("c71", "token_secrecy", "tasks", v=True)
def c71_token_secrecy(ctx: Ctx) -> None:
    cur = ctx.p.head()
    tid = ctx.task_id(ctx.p, ctx.c, "secret", claim=True)
    ctx.p.task_renew(tid)
    views: list[Any] = [ctx.p.read(tid), ctx.p.tasks(ctx.c, state="all"), ctx.p.read(ctx.c)]
    if ctx.raw.ok:
        views.append(ctx.raw.events(channel=ctx.c, after=cur, all=True))
    text = _dump(*views)
    keys: set[str] = set()
    _walk_keys(views, keys)
    need("token" not in keys, "no token key in any view")
    need(not _HEX64.search(text), "no 64-hex string in any view")
    token = getattr(ctx.p, "_proofs", {}).get(tid, (0, ""))[1]
    need(not token or token not in text, "the claim token itself never appears in a view")
    eq(set((ctx.tk(ctx.p, tid)["claim"] or {}).keys()), {"actor", "until"}, "claim shows only actor and until")


@check("c72", "event_identity", "core", v=True)
def c72_event_identity(ctx: Ctx) -> None:
    cur = ctx.p.head()
    doc = ctx.post(ctx.p, ctx.c, "who")
    by = doc["message"]["by"]
    need({"actor", "principal", "profile"} <= set(by), f"by is {{actor, principal, profile}}: {sorted(by)}")
    docs: list[Any] = [doc, ctx.me]
    ev = ctx.p.wait(ctx.c, after=cur, timeout=2)
    need(ev is not None, "the post produced an event")
    docs.append(ev)
    if ctx.raw.ok:
        docs.append(ctx.raw.events(channel=ctx.c, after=cur, all=True))
    keys: set[str] = set()
    _walk_keys(docs, keys)
    need("session" not in keys, "no session key anywhere")


# ============================================================================================= J. fold safety
@check("c73", "fold_guards", "tasks")
def c73_fold_guards(ctx: Ctx) -> None:
    if ctx.backend != "local":
        raise Skip("store-level: runs against the local engine only")
    from .store import fold_task

    base = {"owner": "o", "by": "o+a"}
    t = fold_task(None, "task.created", base)
    need(t is not None and t["state"] == "ready" and t["epoch"] == 0, "created -> ready")
    need(fold_task(t, "task.created", base) is t, "created applies only once")
    need(fold_task(None, "task.created", base, is_root=False) is None, "created needs a root")
    need(fold_task(None, "task.created", base, deleted=True) is None, "created needs a non-deleted message")
    need(fold_task(t, "task.assigned", {"for": None}) is t, "assigned is a no-op for the same `for`")
    need(fold_task(t, "task.ready", {}) is t, "ready applies only from waiting")
    claimed_payload = {"epoch": 1, "actor": "a", "until_ms": 5, "token_hash": "h"}
    need(fold_task(t, "task.claimed", {**claimed_payload, "epoch": 2}) is t, "claimed needs epoch + 1")
    c = fold_task(t, "task.claimed", claimed_payload)
    need(c is not t and c["state"] == "claimed" and c["epoch"] == 1, "claimed from ready at epoch + 1")
    need(fold_task(c, "task.claimed", {**claimed_payload, "epoch": 2}) is c, "claimed only from ready")
    for etype in ("task.renewed", "task.released", "task.expired", "task.completed", "task.failed"):
        need(fold_task(c, etype, {"epoch": 9, "until_ms": 7}) is c, f"{etype} with a stale epoch changes nothing")
        need(fold_task(t, etype, {"epoch": 0}) is t, f"{etype} applies only while claimed")
    need(fold_task(t, "task.needs_changed", {"needs": ["1"]})["state"] == "waiting", "needs_changed from ready")
    need(fold_task(c, "task.needs_changed", {"needs": ["1"]}) is c, "needs_changed not while claimed")
    blocked = fold_task(c, "task.blocked", {"kind": None, "reason": "x"})
    need(blocked["state"] == "blocked" and blocked["claim"] is None, "blocked clears the claim")
    need(fold_task(blocked, "task.blocked", {"reason": "y"}) is blocked, "blocked only from waiting/ready/claimed")
    need(fold_task(t, "task.unblocked", {}) is t, "unblocked only from blocked")
    done = fold_task(c, "task.completed", {"epoch": 1})
    need(fold_task(done, "task.canceled", {"reason": "x"}) is done, "canceled never applies to a completed task")
    need(fold_task(t, "task.reopened", {}) is t, "reopened only from failed")
    failed = fold_task(c, "task.failed", {"epoch": 1, "reason": "x"})
    need(fold_task(failed, "task.reopened", {})["state"] == "ready", "reopened from failed")


@check("c74", "task_on_roots_only", "tasks", v=True)
def c74_task_on_roots_only(ctx: Ctx) -> None:
    root = ctx.post(ctx.p, ctx.c, "root")["message"]["id"]
    rep = ctx.reply(ctx.p, root, "reply")["message"]["id"]
    expect_err("invalid_request", ctx.p.task_adopt, rep)
    need(ctx.msg(ctx.p, rep).get("task") is None, "a reply is never a task")
    t = ctx.task_id(ctx.p, ctx.c, "doomed root")
    ctx.p.delete(t)
    need("board.task" not in ctx.msg(ctx.p, t)["labels"], "a deleted root wears no label")


# ============================================================================================= Fusion group
def _board_iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}Z"


@check("f01", "priority_order", "fusion")
def f01_priority_order(ctx: Ctx) -> None:
    q = ctx.sub("prio")
    low = ctx.task_id(ctx.p, q, "older, no priority")
    high = ctx.task_id(ctx.p, q, "newer, priority 5", labels={"fusion.priority": "5"})
    got = ctx.p.task_claim_next(q)
    eq(str(got["task"]["id"]), high, "the higher priority first")
    ctx.p.task_release(high)
    plain = ctx.p.task_claim_next(q, respect_priority=False)
    eq(str(plain["task"]["id"]), low, "creation order when priority is not respected")


@check("f02", "defer_until", "fusion")
def f02_defer_until(ctx: Ctx) -> None:
    q = ctx.sub("defer")
    now = ctx.now_ms()
    tid = ctx.task_id(ctx.p, q, "deferred", labels={"fusion.defer_until": _board_iso(now + 3_600_000)})
    need(ctx.p.task_claim_next(q) is None, "a deferred task is not claimable yet")
    if ctx.tick:
        ctx.tick(3601)
        got = ctx.p.task_claim_next(q)
        need(got is not None and str(got["task"]["id"]) == tid, "and is claimable once its time has come")


@check("f03", "conditional_block_if_failed", "fusion")
def f03_conditional_block_if_failed(ctx: Ctx) -> None:
    if not ctx.caps.conditional_needs:
        raise Skip("the board has no conditional needs")
    a = ctx.task_id(ctx.p, ctx.c, "may fail", claim=True)
    on_fail = ctx.task_id(ctx.p, ctx.c, "runs if a fails", needs=[{"id": a, "if": "failed"}])
    eq(ctx.state(on_fail), "waiting", "waits for the failure")
    ctx.p.task_fail(a, "broke")
    eq(ctx.state(on_fail), "ready", "a failure readies the branch")
    b = ctx.task_id(ctx.p, ctx.c, "may complete", claim=True)
    on_fail2 = ctx.task_id(ctx.p, ctx.c, "runs only if b fails", needs=[{"id": b, "if": "failed"}])
    ctx.p.task_complete(b)
    eq(ctx.state(on_fail2), "canceled", "a completion cancels the failure branch")


@check("f04", "sweep_wakes_waiter", "fusion", needs=("clock",))
def f04_sweep_wakes_waiter(ctx: Ctx) -> None:
    store = getattr(ctx.p, "store", None)
    if store is None:
        raise Skip("needs the store's sweep (local engine)")
    q = ctx.sub("sweep")
    tid = ctx.task_id(ctx.p, q, "lapses")
    ctx.p.task_claim(tid, lease=30)
    box: list[Any] = []
    th = threading.Thread(target=lambda: box.append(ctx.threaded(ctx.p2, lambda: ctx.p2.task_claim_next(q, wait=10))), daemon=True)
    th.start()
    time.sleep(0.15)
    ctx.tick(31)
    store.sweep()
    th.join(15)
    need(not th.is_alive() and box and box[0], "the sweep woke the waiter")
    eq(str(box[0]["task"]["id"]), tid, "and it claimed the lapsed task")


@check("f05", "claim_cache_shared_with_cli", "fusion")
def f05_claim_cache_shared_with_cli(ctx: Ctx) -> None:
    cache, board, profile = getattr(ctx.p, "_cache", None), getattr(ctx.p, "_board", None), getattr(ctx.p, "_profile", None)
    if cache is None or board is None:
        raise Skip("needs the shared claim cache (local or http adapter with the cache enabled)")
    tid = ctx.task_id(ctx.p, ctx.c, "cached")
    doc = ctx.p.task_claim(tid)
    entry = cache.get(board, tid, profile)
    need(entry is not None, "the claim is in the shared cache")
    eq(entry.get("epoch"), doc["claim"]["epoch"], "cached epoch")
    ctx.p.task_complete(tid)
    need(cache.get(board, tid, profile) is None, "settling drops the cache entry")


# ============================================================================================= runner
def run(factory: PortFactory, *, channel: str | None = None, groups: Sequence[str] = CORE_GROUPS,
        other_principal: PortFactory | None = None, clock_control: Callable[[float], None] | None = None,
        narrowed: PortFactory | None = None, only: Sequence[str] | None = None, url: str | None = None) -> Report:
    """Run the suite against `factory(profile_name) -> BoardPort` (a port acting as `<principal>+<profile>`).

    `channel` is a throwaway root channel (default `conf/<uuid8>`); `groups` selects check groups; `only` restricts to
    check ids/names (`c01`, `c01_post_read_back`). Returns a Report whose `ok` is true when nothing failed."""
    run_id = uuid.uuid4().hex[:8]
    root = (channel or f"conf/{run_id}").strip("/")
    checks: list[Check] = []
    wanted = set(only or ())
    ctx = Ctx(factory, run_id, root, other=other_principal, narrowed=narrowed, tick=clock_control)
    label = url or getattr(ctx.p, "url", None) or ctx.backend
    hosted = ctx.backend == "vercel"
    avail = {"other": ctx.o is not None, "narrow": ctx.nar is not None, "clock": ctx.tick is not None,
             "raw": ctx.raw.ok, "proofs": ctx.supports_proofs(ctx.p)}
    try:
        for spec in _REGISTRY:
            if spec.group not in groups or (wanted and spec.cid not in wanted and spec.name not in wanted):
                continue
            if hosted and not spec.vercel:
                checks.append(Check(spec.group, spec.name, "skipped", "not in the hosted-board subset"))
                continue
            missing = [n for n in spec.needs if not avail.get(n)]
            if missing:
                why = {"other": "needs other_principal", "narrow": "needs a narrowed caller",
                       "clock": "needs clock_control", "raw": "needs a raw feed (local or http adapter)",
                       "proofs": "needs claim-proof injection (local or http adapter)"}
                checks.append(Check(spec.group, spec.name, "skipped", ", ".join(why[m] for m in missing)))
                continue
            ctx.c = f"{root}/{spec.cid}"
            try:
                spec.fn(ctx)
                checks.append(Check(spec.group, spec.name))
            except Skip as exc:
                checks.append(Check(spec.group, spec.name, "skipped", str(exc)))
            except VBoardError as exc:
                if exc.status == 501 or exc.code == "not_supported":
                    optional = spec.group in OPTIONAL_GROUPS
                    if optional and (hosted or ctx.backend == "http" and not ctx.caps.tasks):
                        checks.append(Check(spec.group, spec.name, "skipped", f"not kept here: {exc.message[:160]}"))
                        continue
                checks.append(Check(spec.group, spec.name, "failed", f"{exc.code}: {exc.message[:300]}"))
            except Exception as exc:  # noqa: BLE001 - any failure is a failed check, not a crash
                checks.append(Check(spec.group, spec.name, "failed", f"{type(exc).__name__}: {str(exc)[:300]}"))
    finally:
        ctx.cleanup()
    return Report(url=str(label), checks=checks, ok=not any(c.status == "failed" for c in checks))


__all__ = ["CORE_GROUPS", "Check", "Report", "Skip", "check_names", "run", "vercel_subset"]
