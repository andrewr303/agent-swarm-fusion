"""Offline tests for the vboard data core (python/fusion_swarm/vboard: model, store, claims).

stdlib unittest; temp dirs only; the clock is injected (a mutable list) so lease behaviour never sleeps.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_vboard_store -v   (from the plugin root)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm.vboard import model as M  # noqa: E402
from fusion_swarm.vboard import store as store_mod  # noqa: E402
from fusion_swarm.vboard.claims import ClaimCache, board_key, claim_key, claims_dir  # noqa: E402
from fusion_swarm.vboard.model import Caller, VBoardError  # noqa: E402
from fusion_swarm.vboard.store import BoardStore, fold_task  # noqa: E402

MESSAGE_KEYS = {"id", "channel", "text", "re", "replyTo", "thread", "labels", "reactions", "by", "noSync", "origin",
                "from", "createdAt", "editedAt", "deleted", "undelivered", "attachments", "links", "version",
                "revision", "author", "blocks"}
TASK_KEYS = {"state", "owner", "by", "for", "needs", "epoch", "claim", "blocked", "result", "reason"}
HEX64 = re.compile(r"[0-9a-f]{64}")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "b.db")
        self.t = [1_000.0]
        self._stores: list[BoardStore] = []
        self.s = self.open()
        self.alice = Caller.local(user="alice")
        self.bob = Caller.local(user="bob")
        self.claude = Caller.local("claude", user="alice")      # a profile of alice
        self.codex = Caller.local("codex", user="alice")

    def open(self) -> BoardStore:
        st = BoardStore(self.db, clock=lambda: self.t[0])
        self._stores.append(st)
        self.addCleanup(st.close)
        return st

    # -- helpers
    def task(self, caller=None, channel="work/x", text="do it", **kw) -> str:
        return self.s.create_task(caller or self.alice, channel=channel, text=text, **kw)["task"]["id"]

    def state(self, tid: str) -> str:
        return self.s.thread(self.alice, tid)["message"]["task"]["state"]

    def view(self, tid: str) -> dict:
        return self.s.thread(self.alice, tid)["message"]

    def evs(self, after="0", **kw) -> list[dict]:
        """Every event after a cursor (all=True), paging through the 200-event scan limit."""
        out: list[dict] = []
        cur = after
        while True:
            page = self.s.events(self.alice, after=cur, all=True, **kw)
            out += page["events"]
            if page["cursor"] == cur or int(page["cursor"]) >= self.head():
                return out
            cur = page["cursor"]

    def types(self, after="0", **kw) -> list[str]:
        return [e["type"] for e in self.evs(after, **kw)]

    def head(self) -> int:
        return int(self.s.head())

    def code(self, fn, *a, **kw) -> str:
        with self.assertRaises(VBoardError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def race(self, fns):
        out: list = [None] * len(fns)
        barrier = threading.Barrier(len(fns))

        def run(i):
            barrier.wait(timeout=5)
            try:
                out[i] = ("ok", fns[i]())
            except VBoardError as exc:
                out[i] = ("err", exc.code)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)
            self.assertFalse(th.is_alive())
        return out


class StoreBasics(Base):
    def test_schema_created_once_and_user_version_set(self) -> None:
        calls = []
        real = store_mod._create_schema

        def counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        store_mod._create_schema = counting
        try:
            path = os.path.join(self.tmp.name, "fresh.db")
            a = BoardStore(path)
            b = BoardStore(path)
            a.close()
            b.close()
        finally:
            store_mod._create_schema = real
        self.assertEqual(len(calls), 1)
        con = sqlite3.connect(path)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], store_mod.SCHEMA_VERSION)
        self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        con.execute("PRAGMA user_version = 99")
        con.close()
        self.assertEqual(self.code(BoardStore, path), "internal")

    def test_post_reply_thread_shapes(self) -> None:
        root = self.s.post(self.alice, text="hello", channel="dev")
        rid = root["message"]["id"]
        self.assertEqual(set(root["message"]), MESSAGE_KEYS)
        self.assertEqual(set(root["message"]["author"]), {"id", "kind", "handle", "name", "avatar", "via", "verified"})
        self.assertEqual(set(root["message"]["by"]), {"actor", "principal", "profile"})
        self.assertIsNone(root["message"]["re"])
        self.assertEqual(root["message"]["thread"], rid)
        self.assertRegex(root["message"]["createdAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(root["mentions"], [])
        r1 = self.s.post(self.claude, text="re one", re=rid)["message"]
        self.assertEqual((r1["re"], r1["thread"], r1["replyTo"], r1["channel"]), (rid, rid, None, "dev"))
        self.assertEqual(r1["author"]["kind"], "agent")
        r2 = self.s.post(self.alice, text="re two", re=r1["id"])["message"]
        self.assertEqual((r2["re"], r2["replyTo"]), (rid, r1["id"]))
        th = self.s.thread(self.alice, r2["id"])
        self.assertEqual(set(th), {"message", "replies", "cursor", "schedules"})
        self.assertEqual(th["message"]["id"], rid)
        self.assertEqual([m["id"] for m in th["replies"]], [r1["id"], r2["id"]])
        self.assertEqual([m["id"] for m in self.s.thread(self.alice, rid, limit=1)["replies"]], [r2["id"]])
        self.assertEqual([m["id"] for m in self.s.thread(self.alice, rid, after=r1["id"])["replies"]], [r2["id"]])
        summary = self.s.threads(self.alice, channel="dev")["threads"][0]
        self.assertEqual(set(summary), {"message", "replies", "last"})
        self.assertEqual((summary["replies"], summary["last"]["id"]), (2, r2["id"]))
        self.assertEqual(self.code(self.s.post, self.alice, text="x", channel="Not A Channel"), "invalid_request")
        self.assertEqual(self.code(self.s.post, self.alice, text="   ", channel="dev"), "invalid_request")
        self.assertEqual(self.code(self.s.post, self.alice, text="x" * 10_001, channel="dev"), "text_too_long")
        self.assertEqual(self.code(self.s.thread, self.alice, "999"), "message_not_found")
        # a post by a person mentions a unique handle
        bobmsg = self.s.post(self.bob, text="hi", channel="dev")["message"]
        mention = self.s.post(self.alice, text="ping @bob and `@bob` and @nobody", channel="dev")
        self.assertEqual(mention["mentions"], [bobmsg["by"]["actor"]])
        self.assertEqual(self.s.channels(self.alice)["channels"][0]["path"], "dev")

    def test_reply_after_newer_message_race(self) -> None:
        root = self.s.post(self.alice, text="root", channel="dev")["message"]["id"]
        stores = [self.open() for _ in range(3)]
        out = self.race([lambda i=i: stores[i].post(self.alice, text=f"r{i}", re=root, after=root) for i in range(3)])
        self.assertEqual(sorted(o[0] for o in out), ["err", "err", "ok"])
        self.assertEqual({o[1] for o in out if o[0] == "err"}, {"newer_message"})
        self.assertEqual(len(self.s.thread(self.alice, root)["replies"]), 1)
        # a deleted newer reply still counts as newer
        newest = self.s.thread(self.alice, root)["replies"][0]["id"]
        mine = self.s.post(self.alice, text="gone", re=root, after=newest)["message"]["id"]
        self.s.delete(self.alice, mine)
        self.assertEqual(self.code(self.s.post, self.alice, text="x", re=root, after=newest), "newer_message")
        # a retry with the same id returns the landed reply whatever `after` says now
        first = self.s.post(self.alice, text="keyed", re=root, after=mine, client_id="k1")["message"]
        again = self.s.post(self.alice, text="keyed", re=root, after=root, client_id="k1")["message"]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.code(self.s.post, self.alice, text="keyed", re=root, after="12345", client_id="k2"),
                         "invalid_after")

    def test_invalid_after_not_in_thread(self) -> None:
        a = self.s.post(self.alice, text="a", channel="dev")["message"]["id"]
        b = self.s.post(self.alice, text="b", channel="dev")["message"]["id"]
        for bad in (b, "4242", "nonsense"):
            self.assertEqual(self.code(self.s.post, self.alice, text="x", re=a, after=bad), "invalid_after")
        self.assertEqual(self.code(self.s.post, self.alice, text="x", channel="dev", after=a), "invalid_request")
        self.assertEqual(len(self.s.thread(self.alice, a)["replies"]), 0)

    def test_post_idempotency_same_and_conflict(self) -> None:
        m1 = self.s.post(self.alice, text="same", channel="dev", client_id="K")["message"]
        m2 = self.s.post(self.alice, text="same", channel="dev", client_id="K")["message"]
        self.assertEqual(m1["id"], m2["id"])
        self.assertEqual(len(self.s.messages(self.alice, "dev")["messages"]), 1)
        self.assertEqual(self.code(self.s.post, self.alice, text="different", channel="dev", client_id="K"),
                         "duplicate_id")
        self.assertEqual(self.code(self.s.post, self.bob, text="same", channel="dev", client_id="K"), "duplicate_id")
        self.assertEqual(self.code(self.s.post, self.alice, text="same", re=m1["id"], client_id="K"), "duplicate_id")
        self.assertEqual(self.code(self.s.post, self.claude, text="same", channel="dev", client_id="K"),
                         "duplicate_id")
        self.assertEqual(self.s.posted(self.alice, "K")["message"]["id"], m1["id"])
        self.assertEqual(self.code(self.s.posted, self.alice, "unknown"), "post_not_found")
        self.assertEqual(self.code(self.s.posted, self.bob, "K"), "post_not_found")
        # an id used by a plain thread can't create a task
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="dev", text="same", client_id="K"),
                         "duplicate_id")

    def test_version_and_revision_rules(self) -> None:
        m = self.s.post(self.alice, text="v0", channel="dev")["message"]
        mid, v0, r0 = m["id"], m["version"], m["revision"]
        m1 = self.s.react(self.alice, mid, ":eyes:")["message"]
        self.assertGreater(m1["version"], v0)
        self.assertEqual(m1["revision"], r0)
        self.assertEqual(m1["reactions"], {"eyes": [self.alice.actor]})
        self.assertEqual(self.s.react(self.alice, mid, "eyes")["message"]["version"], m1["version"])
        m2 = self.s.label(self.alice, mid, set={"owner": "a"})["message"]
        self.assertGreater(m2["version"], m1["version"])
        self.assertEqual(m2["revision"], r0)
        self.assertEqual(self.s.label(self.alice, mid, unset=["owner"])["message"]["labels"], {})
        m3 = self.s.react(self.alice, mid, "eyes", remove=True)["message"]
        self.assertEqual(m3["reactions"], {})
        m4 = self.s.edit(self.alice, mid, "v1", revision=r0)["message"]
        self.assertGreater(m4["version"], m3["version"])
        self.assertNotEqual(m4["revision"], r0)
        self.assertEqual((m4["text"], m4["editedAt"] is not None), ("v1", True))
        self.assertEqual(self.code(self.s.edit, self.alice, mid, "stale", revision=r0), "revision_conflict")
        self.assertEqual(self.code(self.s.delete, self.alice, mid, revision=r0), "revision_conflict")
        self.assertEqual(self.view(mid)["text"], "v1")
        self.assertEqual(self.code(self.s.edit, self.bob, mid, "hijack"), "forbidden")
        # 3 racing edits at one revision: one wins
        cur = self.view(mid)["revision"]
        stores = [self.open() for _ in range(3)]
        out = self.race([lambda i=i: stores[i].edit(self.alice, mid, f"e{i}", revision=cur) for i in range(3)])
        self.assertEqual(sorted(o[0] for o in out), ["err", "err", "ok"])
        self.assertEqual({o[1] for o in out if o[0] == "err"}, {"revision_conflict"})
        # an edit at a revision a delete followed is a conflict, not "gone"
        rev = self.view(mid)["revision"]
        self.s.delete(self.alice, mid)
        self.assertEqual(self.code(self.s.edit, self.alice, mid, "late", revision=rev), "revision_conflict")
        self.assertTrue(self.s.edit(self.alice, mid, "ignored")["message"]["deleted"])

    def test_delete_tombstone_and_task_cancel(self) -> None:
        root = self.s.post(self.alice, text="root", channel="dev", labels={"k": "v"})["message"]["id"]
        reply = self.s.post(self.alice, text="reply", re=root)["message"]["id"]
        self.s.react(self.alice, reply, "eyes")
        gone = self.s.delete(self.alice, reply)["message"]
        self.assertEqual((gone["deleted"], gone["text"], gone["labels"], gone["reactions"]), (True, "", {}, {}))
        self.assertEqual(self.s.thread(self.alice, root)["replies"][0]["deleted"], True)
        self.assertEqual(self.s.threads(self.alice, channel="dev", label=["k=v"])["threads"][0]["replies"], 1)
        # deleting a task root cancels it, then tombstones, in one write; dependents stay waiting
        a = self.task(text="alpha")
        b = self.task(text="beta", needs=[a])
        c = self.s.claim(self.alice, a)["claim"]
        before = self.head()
        self.s.delete(self.alice, a)
        evs = self.evs(str(before))
        self.assertEqual([e["type"] for e in evs], ["task.canceled", "message.deleted"])
        self.assertEqual(evs[0]["payload"]["reason"], "deleted")
        self.assertEqual(evs[0]["message"]["labels"].get("board.task"), "canceled")
        self.assertEqual(int(evs[1]["cursor"]), int(evs[0]["cursor"]) + 1)
        self.assertEqual(self.code(self.s.complete, self.alice, a, epoch=1, token=c["token"]), "claim_lost")
        self.assertNotIn(a, [t["message"]["id"] for t in self.s.list_tasks(self.alice, state="all")["tasks"]])
        self.assertEqual(self.state(b), "waiting")
        # a completed task's deletion is only a deletion
        d = self.task(text="done")
        cd = self.s.claim(self.alice, d)["claim"]
        self.s.complete(self.alice, d, epoch=1, token=cd["token"], result="ok")
        n = self.head()
        self.s.delete(self.alice, d)
        self.assertEqual(self.types(str(n)), ["message.deleted"])
        self.assertEqual(self.code(self.s.react, self.alice, d, "eyes"), "message_deleted")

    def test_label_cas_race_and_reserved_prefix(self) -> None:
        mid = self.s.post(self.alice, text="x", channel="dev")["message"]["id"]
        stores = [self.open() for _ in range(3)]
        out = self.race([lambda i=i: stores[i].label(self.alice, mid, set={"owner": f"agent-{i}"},
                                                     if_={"owner": None}) for i in range(3)])
        self.assertEqual(sorted(o[0] for o in out), ["err", "err", "ok"])
        self.assertEqual({o[1] for o in out if o[0] == "err"}, {"label_conflict"})
        winner = [o[1] for o in out if o[0] == "ok"][0]["message"]["labels"]["owner"]
        self.assertEqual(self.view(mid)["labels"]["owner"], winner)
        with self.assertRaises(VBoardError) as cm:
            self.s.label(self.alice, mid, set={"owner": "z"}, if_={"owner": "someone-else"})
        self.assertEqual(cm.exception.code, "label_conflict")
        self.assertNotIn(winner, cm.exception.message)
        self.assertNotIn("someone-else", cm.exception.message)
        # equal value lands, also for unset
        self.assertEqual(self.s.label(self.alice, mid, set={"owner": "n"}, if_={"owner": winner})["message"]
                         ["labels"]["owner"], "n")
        self.assertEqual(self.s.label(self.alice, mid, unset=["owner"], if_={"owner": "n"})["message"]["labels"], {})
        for bad in ({"board.x": "1"},):
            self.assertEqual(self.code(self.s.label, self.alice, mid, set=bad), "reserved_label")
        self.assertEqual(self.code(self.s.label, self.alice, mid, unset=["board.task"]), "reserved_label")
        self.assertEqual(self.code(self.s.post, self.alice, text="x", channel="dev", labels={"board.task": "x"}),
                         "reserved_label")
        self.assertEqual(self.code(self.s.label, self.alice, mid, set={"constructor": "x"}), "invalid_request")
        self.assertEqual(self.code(self.s.label, self.alice, mid, set={"a": "line\nbreak"}), "invalid_request")
        # board.task can be read, filtered on and used as an `if`
        t = self.task()
        self.assertEqual(self.s.label(self.alice, t, set={"seen": "1"}, if_={"board.task": "ready"})["message"]
                         ["labels"]["seen"], "1")
        self.assertEqual(self.code(self.s.label, self.alice, t, set={"seen": "2"}, if_={"board.task": None}),
                         "label_conflict")
        self.assertEqual(self.code(self.s.label, self.alice, t, set={"seen": "2"}, if_={"board.task": "claimed"}),
                         "label_conflict")

    def test_threads_paging_older_before_after(self) -> None:
        ids = []
        for i in range(5):
            ids.append(self.s.post(self.alice, text=f"t{i}", channel="dev")["message"]["id"])
        page1 = self.s.threads(self.alice, channel="dev", limit=2)
        self.assertEqual([t["message"]["id"] for t in page1["threads"]], ids[3:])
        self.assertIsNotNone(page1["older"])
        seen = [t["message"]["id"] for t in page1["threads"]]
        older = page1["older"]
        while older:
            page = self.s.threads(self.alice, channel="dev", limit=2, before=older)
            seen = [t["message"]["id"] for t in page["threads"]] + seen
            older = page["older"]
        self.assertEqual(seen, ids)
        self.assertEqual(self.code(self.s.threads, self.alice, channel="dev", before="1.1", after="1"),
                         "invalid_request")
        self.assertEqual(self.code(self.s.threads, self.alice, channel="dev", before="nope"), "invalid_cursor")
        # a reply moves a thread to the end; `after` returns threads with activity since
        cursor = page1["cursor"]
        self.s.post(self.alice, text="bump", re=ids[0])
        since = self.s.threads(self.alice, channel="dev", after=cursor)
        self.assertEqual([t["message"]["id"] for t in since["threads"]], [ids[0]])
        self.assertEqual(since["threads"][0]["replies"], 1)
        self.assertEqual(self.s.threads(self.alice, channel="dev", after=since["cursor"])["threads"], [])
        # messages(): newest N, then onward from the cursor with no repeats
        newest = self.s.messages(self.alice, "dev", limit=2)
        self.assertEqual(len(newest["messages"]), 2)
        self.s.post(self.alice, text="later", channel="dev")
        nxt = self.s.messages(self.alice, "dev", after=newest["cursor"], limit=500)
        self.assertEqual([m["text"] for m in nxt["messages"]], ["later"])
        walk, cur = [], "0"
        while True:
            page = self.s.messages(self.alice, "dev", after=cur, limit=2)
            walk += [m["id"] for m in page["messages"]]
            if len(page["messages"]) < 2:
                break
            cur = page["cursor"]
        self.assertEqual(len(walk), len(set(walk)))
        self.assertEqual(len(walk), 7)
        self.assertEqual(self.code(self.s.messages, self.alice, "dev", limit=501), "invalid_request")
        self.assertEqual(self.code(self.s.messages, self.alice, "dev", after="zzz"), "invalid_cursor")

    def test_unknown_channel_did_you_mean(self) -> None:
        self.s.post(self.alice, text="x", channel="deploys")
        with self.assertRaises(VBoardError) as cm:
            self.s.messages(self.alice, "deploy")
        self.assertEqual(cm.exception.code, "channel_not_found")
        self.assertIn("deploys", cm.exception.hint or "")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(self.code(self.s.threads, self.alice, channel="nothing-here"), "channel_not_found")
        self.assertEqual(self.code(self.s.list_tasks, self.alice, channel="nothing-here"), "channel_not_found")
        self.assertEqual(self.s.threads(self.alice)["threads"][0]["message"]["channel"], "deploys")
        self.assertEqual(self.s.create_channel(self.alice, "deploys")["created"], False)
        self.assertEqual(self.s.create_channel(self.alice, "ops/db")["created"], True)
        self.assertEqual(self.code(self.s.create_channel, self.alice, "Bad Name"), "invalid_request")
        self.assertEqual(self.s.threads(self.alice, channel="ops/")["threads"], [])
        page = self.s.channels(self.alice, limit=1)
        self.assertEqual((len(page["channels"]), page["next"]), (1, "deploys"))
        rest = self.s.channels(self.alice, after=page["next"])
        self.assertEqual([c["path"] for c in rest["channels"]], ["ops/db"])
        self.assertEqual([c["path"] for c in self.s.channels(self.alice, prefix="ops")["channels"]], ["ops/db"])

    def test_events_cursor_last_wait_and_filters(self) -> None:
        self.assertEqual(self.s.events(self.alice, last=5), {"events": [], "cursor": "0"})
        empty = self.s.events(self.alice)
        self.assertEqual(empty["events"], [])
        m = self.s.post(self.alice, text="first", channel="dev")["message"]
        page = self.s.events(self.alice, after=empty["cursor"], channel="dev")
        self.assertEqual([e["type"] for e in page["events"]], ["channel.created", "message.posted"])
        self.assertEqual(set(page["events"][1]), {"cursor", "type", "createdAt", "payload", "message", "actor"})
        self.assertEqual(page["events"][1]["message"]["id"], m["id"])
        self.assertEqual(self.s.events(self.alice, after=page["cursor"], channel="dev")["events"], [])
        self.assertEqual(self.code(self.s.events, self.alice, last=101), "invalid_request")
        self.assertEqual(self.code(self.s.events, self.alice, last=0), "invalid_request")
        self.assertEqual(self.code(self.s.events, self.alice, last=2, after="1"), "invalid_request")
        self.assertEqual(self.code(self.s.events, self.alice, last=2, wait=1), "invalid_request")
        self.assertEqual(self.code(self.s.events, self.alice, wait=301), "invalid_request")
        self.assertEqual(self.code(self.s.events, self.alice, after="garbage"), "invalid_cursor")
        for i in range(3):
            self.s.post(self.alice, text=f"m{i}", channel="dev")
        last2 = self.s.events(self.alice, last=2, channel="dev")
        self.assertEqual(len(last2["events"]), 2)
        self.assertLess(int(last2["events"][0]["cursor"]), int(last2["events"][1]["cursor"]))
        self.assertEqual(self.s.events(self.alice, after=last2["cursor"], channel="dev")["events"], [])
        # counting looks past a page of unrelated events
        for i in range(230):
            self.s.post(self.alice, text=f"o{i}", channel="ops")
        only = self.s.events(self.alice, last=1, channel="dev")["events"]
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0]["message"]["channel"], "dev")
        self.assertEqual(len(self.s.events(self.alice, last=100, channel="ops")["events"]), 100)
        self.assertEqual(len(self.s.events(self.alice, last=100)["events"]), 100)
        # long poll: a post made 150 ms later from another thread is returned by the call
        cur = self.s.head()
        other = self.open()

        def late():
            time.sleep(0.15)
            other.post(self.bob, text="late", channel="dev")

        th = threading.Thread(target=late)
        th.start()
        started = time.monotonic()
        got = self.s.events(self.alice, after=cur, wait=10, channel="dev")
        th.join(timeout=5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([e["message"]["text"] for e in got["events"] if e["type"] == "message.posted"], ["late"])
        t0 = time.monotonic()
        self.assertEqual(self.s.events(self.alice, after=got["cursor"], wait=0.15)["events"], [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.1)
        stop = self.s.events(self.alice, after=got["cursor"], wait=10, should_stop=lambda: True)
        self.assertEqual(stop["events"], [])
        # label filter sees a task enter and leave board.task=ready
        tid = self.task(labels={"lane": "a"})
        cl = self.s.claim(self.alice, tid)["claim"]
        self.s.complete(self.alice, tid, epoch=1, token=cl["token"])
        ready = self.s.events(self.alice, after=got["cursor"], label=["board.task=ready"])["events"]
        self.assertEqual([e["type"] for e in ready], ["task.created", "task.claimed"])
        self.assertEqual([e["message"]["task"]["state"] for e in ready], ["ready", "claimed"])
        both = self.s.events(self.alice, after=got["cursor"], label=["lane=a", "board.task=completed"])["events"]
        self.assertEqual([e["type"] for e in both], ["task.completed"])
        # hidden events need all=True
        shown = [e["type"] for e in self.s.events(self.alice, after="0")["events"]]
        self.assertNotIn("actor.seen", shown)
        self.assertIn("actor.seen", self.types("0"))
        self.assertEqual([e["type"] for e in self.s.events(self.alice, after=cur, by="bob")["events"]],
                         ["message.posted"])
        self.assertTrue(self.s.events(self.alice, after=cur, text="LATE")["events"])
        self.assertEqual(self.s.events(self.alice, after=cur, source="github")["events"], [])
        self.assertEqual(self.s.events(self.alice, after=cur, mentions="bob")["events"], [])
        self.s.post(self.alice, text="hey @bob", channel="dev")
        self.assertEqual(len(self.s.events(self.alice, after=cur, mentions="bob")["events"]), 1)


class TaskLifecycle(Base):
    def test_task_create_adopt_idempotent_claim_in_one(self) -> None:
        out = self.s.create_task(self.alice, channel="work/x", text="build it", labels={"kind": "build"})
        t = out["task"]
        self.assertEqual(set(t["task"]), TASK_KEYS)
        self.assertEqual(set(t), MESSAGE_KEYS | {"task"})
        self.assertEqual((t["task"]["state"], t["task"]["owner"], t["task"]["by"], t["task"]["needs"],
                          t["task"]["epoch"], t["task"]["claim"]),
                         ("ready", self.alice.principal, self.alice.actor, [], 0, None))
        self.assertEqual(t["labels"], {"kind": "build", "board.task": "ready"})
        self.assertNotIn("claim", out)
        listed = self.s.list_tasks(self.alice, channel="work/x")
        self.assertEqual(set(listed["tasks"][0]), {"message", "replies", "last"})
        self.assertEqual(len(self.s.threads(self.alice, label=["board.task=ready"])["threads"]), 1)
        # adopt an existing thread, keeping its text
        plain = self.s.post(self.bob, text="please look", channel="dev")["message"]["id"]
        reply = self.s.post(self.bob, text="re", re=plain)["message"]["id"]
        adopted = self.s.create_task(self.alice, re=plain)["task"]
        self.assertEqual((adopted["text"], adopted["task"]["state"], adopted["task"]["owner"]),
                         ("please look", "ready", self.alice.principal))
        self.assertEqual(self.code(self.s.create_task, self.alice, re=plain), "already_a_task")
        self.assertEqual(self.code(self.s.create_task, self.alice, re=reply), "invalid_request")
        gone = self.s.post(self.bob, text="gone", channel="dev")["message"]["id"]
        self.s.delete(self.bob, gone)
        self.assertEqual(self.code(self.s.create_task, self.alice, re=gone), "message_deleted")
        self.assertEqual(self.code(self.s.claim, self.alice, reply), "not_a_task")
        self.assertEqual(self.code(self.s.claim, self.alice, "9999"), "message_not_found")
        self.assertEqual(self.code(self.s.create_task, self.alice, re=plain, text="x"), "invalid_request")
        # client id idempotency: the retry doesn't re-answer the claim secret
        first = self.s.create_task(self.alice, channel="work/y", text="once", claim=True, lease=300, client_id="T1")
        self.assertEqual(first["task"]["task"]["state"], "claimed")
        self.assertEqual(first["claim"]["epoch"], 1)
        self.assertRegex(first["claim"]["token"], r"^[A-Za-z0-9_-]{22}$")
        retry = self.s.create_task(self.alice, channel="work/y", text="once", claim=True, lease=300, client_id="T1")
        self.assertNotIn("claim", retry)
        self.assertEqual(retry["task"]["id"], first["task"]["id"])
        self.assertEqual((retry["task"]["task"]["state"], retry["task"]["task"]["epoch"]), ("claimed", 1))
        tid = first["task"]["id"]
        self.s.renew(self.alice, tid, epoch=1, token=first["claim"]["token"])
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/y", text="other", client_id="T1"),
                         "duplicate_id")
        r = self.s.post(self.alice, text="a reply", re=plain, client_id="R1")["message"]["id"]
        self.assertEqual(self.code(self.s.create_task, self.alice, re=plain, client_id="R1"), "duplicate_id")
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="dev", text="x", client_id="R1"),
                         "duplicate_id")
        self.assertEqual(self.s.thread(self.alice, r)["message"]["id"], plain)
        # create-and-claim refusals create nothing
        dep = self.task(text="dep")
        head = self.head()
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="w", needs=[dep],
                                   claim=True), "task_waiting")
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="w", for_="codex",
                                   claim=True), "task_assigned")
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="w", claim=True, lease=5),
                         "invalid_request")
        self.assertEqual(self.head(), head)
        mine = self.s.create_task(self.claude, channel="work/x", text="mine", for_="claude", claim=True)
        self.assertEqual(mine["task"]["task"]["for"], self.claude.actor)
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="w", for_="bob+x"),
                         "invalid_request")

    def test_claim_race_exactly_one(self) -> None:
        tid = self.task()
        stores = [self.open() for _ in range(5)]
        callers = [Caller.local(f"w{i}", user="alice") for i in range(5)]
        out = self.race([lambda i=i: stores[i].claim(callers[i], tid) for i in range(5)])
        self.assertEqual(sorted(o[0] for o in out), ["err"] * 4 + ["ok"])
        self.assertEqual({o[1] for o in out if o[0] == "err"}, {"task_claimed"})
        self.assertEqual(self.types().count("task.claimed"), 1)
        winner = [o for o in out if o[0] == "ok"][0][1]
        self.assertEqual(winner["task"]["task"]["state"], "claimed")
        self.assertEqual(winner["task"]["labels"]["board.task"], "claimed")
        self.assertEqual(winner["task"]["task"]["claim"]["until"], winner["claim"]["until"])
        self.assertNotIn(winner["claim"]["token"], json.dumps(self.view(tid)))

    def test_fencing_wrong_token_stale_epoch_lapsed(self) -> None:
        tid = self.task()
        c1 = self.s.claim(self.alice, tid, lease=60)["claim"]
        self.assertEqual(c1["epoch"], 1)
        head = self.head()
        for kwargs in ({"epoch": 1, "token": "x" * 22}, {"epoch": 2, "token": c1["token"]}):
            for verb in ("complete", "release", "renew"):
                self.assertEqual(self.code(getattr(self.s, verb), self.alice, tid, **kwargs), "claim_lost")
            self.assertEqual(self.code(self.s.fail, self.alice, tid, reason="no", **kwargs), "claim_lost")
            self.assertEqual(self.code(self.s.block, self.alice, tid, reason="no", **kwargs), "claim_lost")
        self.assertEqual(self.head(), head)
        try:
            self.s.complete(self.alice, tid, epoch=1, token="y" * 22)
        except VBoardError as exc:
            self.assertNotIn(self.alice.actor, exc.message)
            self.assertNotIn(c1["token"], exc.message)
        rel = self.s.release(self.alice, tid, epoch=1, token=c1["token"])
        self.assertEqual((rel["task"]["task"]["state"], rel["task"]["task"]["claim"], rel["task"]["task"]["epoch"]),
                         ("ready", None, 1))
        self.assertEqual(self.code(self.s.renew, self.alice, tid, epoch=1, token=c1["token"]), "claim_lost")
        c2 = self.s.claim(self.bob, tid)["claim"]
        self.assertEqual(c2["epoch"], 2)
        self.assertEqual(self.code(self.s.complete, self.alice, tid, epoch=1, token=c1["token"]), "claim_lost")
        # lapsed with no timers and no sweep: ready for anyone, own writes fail, state filter says ready
        self.t[0] += 901
        head = self.head()
        self.assertEqual(self.state(tid), "ready")
        self.assertEqual([t["message"]["id"] for t in self.s.list_tasks(self.alice, state="ready")["tasks"]], [tid])
        self.assertEqual(self.s.list_tasks(self.alice, state="claimed")["tasks"], [])
        self.assertEqual(self.code(self.s.complete, self.bob, tid, epoch=2, token=c2["token"]), "claim_lost")
        self.assertEqual(self.code(self.s.renew, self.bob, tid, epoch=2, token=c2["token"]), "claim_lost")
        self.assertEqual(self.head(), head)
        for bad_epoch in ("1", True, None):
            self.assertEqual(self.code(self.s.complete, self.alice, tid, epoch=bad_epoch, token="t"),
                             "invalid_request")

    def test_lease_bounds_and_until(self) -> None:
        tid = self.task()
        for bad in (29, 3601, -1, "5m", 7200, True, float("nan")):
            self.assertEqual(self.code(self.s.claim, self.alice, tid, lease=bad), "invalid_request", bad)
        self.assertEqual(self.code(self.s.claim_next, self.alice, "work/x", lease=5), "invalid_request")
        self.assertEqual(self.state(tid), "ready")
        claim = self.s.claim(self.alice, tid, lease=300)["claim"]
        self.assertEqual(claim["until"], M.iso(int((1000 + 300) * 1000)))
        self.t[0] += 100
        out = self.s.renew(self.alice, tid, epoch=1, token=claim["token"], lease=60)
        self.assertEqual(out["until"], M.iso(int((1100 + 60) * 1000)))      # from NOW, shortened
        self.assertEqual(out["task"]["task"]["claim"]["until"], out["until"])
        self.t[0] += 10
        again = self.s.renew(self.alice, tid, epoch=1, token=claim["token"])
        self.assertEqual(again["until"], M.iso(int((1110 + 900) * 1000)))  # default lease
        self.s.release(self.alice, tid, epoch=1, token=claim["token"])
        self.assertEqual(self.s.claim(self.alice, tid)["claim"]["until"], M.iso(int((1110 + 900) * 1000)))
        capped = BoardStore(os.path.join(self.tmp.name, "cap.db"), clock=lambda: self.t[0], lease_cap_s=300)
        self.addCleanup(capped.close)
        cid = capped.create_task(self.alice, channel="w", text="x")["task"]["id"]
        self.assertEqual(self.code(capped.claim, self.alice, cid, lease=301), "invalid_request")
        self.assertEqual(capped.claim(self.alice, cid, lease=300)["claim"]["epoch"], 1)

    def test_expiry_event_lazy_and_sweep(self) -> None:
        self.s.post(self.bob, text="hi", channel="dev")
        tid = self.task()
        c1 = self.s.claim(self.alice, tid, lease=30)["claim"]
        self.t[0] += 31
        head = self.head()
        self.assertEqual(self.s.sweep(now=self.t[0] - 40), {"expired": [], "readied": [], "canceled": []})
        self.assertEqual(self.head(), head)                       # nothing was written by reads or an idle sweep
        c2 = self.s.claim(self.bob, tid, lease=60)["claim"]
        self.assertEqual(c2["epoch"], 2)
        evs = self.evs(str(head))
        self.assertEqual([e["type"] for e in evs], ["task.expired", "task.claimed"])
        self.assertEqual(evs[0]["payload"], {"id": tid, "thread": tid, "epoch": 1})
        self.assertEqual(int(evs[1]["cursor"]), int(evs[0]["cursor"]) + 1)
        self.assertIsNone(evs[0]["actor"])
        self.assertEqual(evs[0]["message"]["task"]["state"], "ready")
        # sweep writes task.expired for lapsed claims even when nobody takes the task
        self.t[0] += 61
        head = self.head()
        self.assertEqual(self.s.sweep(), {"expired": [tid], "readied": [], "canceled": []})
        self.assertEqual(self.types(str(head)), ["task.expired"])
        self.assertEqual(self.s.sweep(), {"expired": [], "readied": [], "canceled": []})
        self.assertEqual(self.types(str(head)), ["task.expired"])
        self.assertEqual(self.view(tid)["labels"]["board.task"], "ready")
        # a renewed claim has no expiry before its new deadline; a settled claim leaves no event
        c3 = self.s.claim(self.alice, tid, lease=60)["claim"]
        self.t[0] += 50
        self.s.renew(self.alice, tid, epoch=3, token=c3["token"], lease=60)
        self.t[0] += 50
        self.assertEqual(self.s.sweep()["expired"], [])
        self.s.complete(self.alice, tid, epoch=3, token=c3["token"])
        self.t[0] += 5000
        self.assertEqual(self.s.sweep()["expired"], [])
        self.assertEqual(c1["epoch"], 1)
        # a waiting events() call sees the expiry as a label-filter event
        t2 = self.task(text="two")
        self.s.claim(self.alice, t2, lease=30)
        cursor = self.s.head()
        self.t[0] += 31
        got = self.s.events(self.alice, after=cursor, wait=0.15, label=["board.task=ready"])
        self.assertEqual([e["type"] for e in got["events"]], ["task.expired"])

    def test_claim_next_order_assigned_priority_defer_from(self) -> None:
        a = self.alice
        low = self.task(text="low")
        hi1 = self.task(text="hi1", labels={"fusion.priority": "5"})
        hi2 = self.task(text="hi2", labels={"fusion.priority": "5"})
        mine = self.task(text="mine", for_="claude")
        other = self.task(text="other", for_="codex")
        later = M.iso(int((self.t[0] + 100) * 1000))
        deferred = self.task(text="deferred", labels={"fusion.priority": "9", "fusion.defer_until": later})
        bobs = self.task(self.bob, text="bobs")
        sub = self.task(channel="work/x/sub", text="sub")
        order = []
        for _ in range(4):
            got = self.s.claim_next(self.claude, "work/x")
            order.append(got["task"]["id"])
            self.assertEqual(got["claim"]["epoch"], 1)
        self.assertEqual(order, [mine, hi1, hi2, low])
        self.assertEqual(self.s.claim_next(self.claude, "work/x"), {"task": None})
        self.assertEqual(self.state(other), "ready")
        self.assertEqual(self.state(bobs), "ready")
        self.t[0] += 101
        self.assertEqual(self.s.claim_next(self.claude, "work/x")["task"]["id"], deferred)
        self.assertEqual(self.s.claim_next(self.claude, "work/x"), {"task": None})
        self.assertEqual(self.s.claim_next(self.claude, "work/x/")["task"]["id"], sub)
        # owners: only own principal unless from names trusted principals
        self.assertEqual(self.s.claim_next(self.claude, "work/x", from_=[self.bob.principal])["task"]["id"], bobs)
        for bad in (["bob"], ["alice/claude"], ["x" * 20] * 1, [f"local:user:u{i}" for i in range(33)]):
            self.assertEqual(self.code(self.s.claim_next, self.claude, "work/x", from_=bad), "invalid_from")
        # the assignee of `other` takes it; labels must all match
        got = self.s.claim_next(self.codex, "work/x")
        self.assertEqual(got["task"]["id"], other)
        t = self.task(text="labeled", labels={"lane": "a", "tier": "1"})
        self.assertEqual(self.s.claim_next(a, "work/x", label={"lane": "a", "tier": "2"}), {"task": None})
        self.assertEqual(self.s.claim_next(a, "work/x", label={"lane": "a", "tier": "1"})["task"]["id"], t)
        self.assertEqual(self.s.claim_next(a, "no-such-channel"), {"task": None})
        narrowed = Caller(self.alice.principal, None, frozenset({"read", "post"}))
        self.assertEqual(self.code(self.s.claim_next, narrowed, "work/x"), "forbidden")
        self.assertEqual(self.code(self.s.claim_next, a, "work/x", wait=301), "invalid_request")
        self.assertEqual(self.s.claim_next(a, "work/x", wait=0.12), {"task": None})

    def test_claim_next_retries_share_one_claim_until_expiry(self) -> None:
        first = self.task(text="first")
        second = self.task(text="second")
        token = "retry-token-for-claim-next"
        other = self.open()
        calls = self.race([
            lambda: self.s.claim_next(self.alice, "work/x", lease=60, token=token),
            lambda: other.claim_next(self.alice, "work/x", lease=60, token=token),
        ])
        self.assertEqual([kind for kind, _ in calls], ["ok", "ok"])
        self.assertTrue(all(result["task"]["id"] == first for _, result in calls))
        self.assertEqual(calls[0][1]["claim"], calls[1][1]["claim"])
        self.assertEqual(self.state(second), "ready")
        self.assertEqual(self.types().count("task.claimed"), 1)

        self.t[0] += 61
        claim = other.claim_next(self.alice, "work/x", lease=60, token=token)
        self.assertEqual(claim["task"]["id"], first)
        self.assertEqual(claim["claim"]["epoch"], 2)
        self.assertEqual(self.types().count("task.expired"), 1)

    def test_claim_next_wait_wakes_on_ready(self) -> None:
        pre = self.task(text="pre")
        waiting = self.task(text="dependent", needs=[pre])
        pc = self.s.claim(self.alice, pre)["claim"]
        other = self.open()

        def finish():
            time.sleep(0.12)
            other.complete(self.alice, pre, epoch=1, token=pc["token"])

        th = threading.Thread(target=finish)
        th.start()
        started = time.monotonic()
        got = self.s.claim_next(self.alice, "work/x", wait=10)
        th.join(timeout=5)
        self.assertEqual(got["task"]["id"], waiting)
        self.assertEqual(got["task"]["task"]["state"], "claimed")
        self.assertLess(time.monotonic() - started, 5)
        # a released task wakes a waiter too
        wc = got["claim"]

        def release():
            time.sleep(0.12)
            other.release(self.alice, waiting, epoch=1, token=wc["token"])

        th = threading.Thread(target=release)
        th.start()
        again = self.s.claim_next(self.alice, "work/x", wait=10)
        th.join(timeout=5)
        self.assertEqual((again["task"]["id"], again["claim"]["epoch"]), (waiting, 2))
        # a caller that stops waiting claims nothing
        self.assertEqual(self.s.claim_next(self.alice, "work/x", wait=10, should_stop=lambda: True), {"task": None})
        # expiry wakes a waiter through the sweep
        self.t[0] += 901
        self.assertEqual(self.s.claim_next(self.bob, "work/x", wait=0.1)["task"], None)
        self.assertEqual(self.s.claim_next(self.alice, "work/x", wait=0.1)["claim"]["epoch"], 3)

    def test_needs_waiting_ready_failed_canceled(self) -> None:
        fix = self.task(text="fix")
        docs = self.task(text="docs")
        top = self.task(text="top", needs=[fix, docs, fix])
        tv = self.view(top)
        self.assertEqual((tv["task"]["state"], tv["task"]["needs"], tv["labels"]["board.task"]),
                         ("waiting", [fix, docs], "waiting"))
        self.assertEqual(self.code(self.s.claim, self.alice, top), "task_waiting")
        c = self.s.claim(self.alice, fix)["claim"]
        self.s.complete(self.alice, fix, epoch=1, token=c["token"])
        self.assertEqual(self.state(top), "waiting")
        c = self.s.claim(self.alice, docs)["claim"]
        mark = self.head()
        self.s.complete(self.alice, docs, epoch=1, token=c["token"])
        evs = self.evs(str(mark))
        self.assertEqual([e["type"] for e in evs], ["task.completed", "task.ready"])
        self.assertEqual(int(evs[1]["cursor"]), int(evs[0]["cursor"]) + 1)
        self.assertEqual(self.state(top), "ready")
        # all needs already complete at creation: created then ready in one append
        mark = self.head()
        ready_now = self.task(text="now", needs=[fix])
        evs = self.evs(str(mark))
        self.assertEqual([e["type"] for e in evs], ["message.posted", "task.created", "task.ready"])
        self.assertEqual(self.state(ready_now), "ready")
        # a failed prerequisite is accepted but never readies; reopened then completed does
        flaky = self.task(text="flaky")
        fc = self.s.claim(self.alice, flaky)["claim"]
        self.assertEqual(self.code(self.s.fail, self.alice, flaky, epoch=1, token=fc["token"], reason=""),
                         "invalid_request")
        failed = self.s.fail(self.alice, flaky, epoch=1, token=fc["token"], reason="boom")["task"]["task"]
        self.assertEqual((failed["state"], failed["reason"], failed["result"]), ("failed", "boom", None))
        after_fail = self.task(text="after", needs=[flaky])
        self.assertEqual(self.state(after_fail), "waiting")
        self.assertEqual(self.code(self.s.claim, self.alice, flaky), "task_finished")
        self.assertEqual(self.s.reopen(self.alice, flaky)["task"]["task"]["state"], "ready")
        fc2 = self.s.claim(self.alice, flaky)["claim"]
        self.assertEqual(fc2["epoch"], 2)
        self.s.complete(self.alice, flaky, epoch=2, token=fc2["token"], result="fixed")
        self.assertEqual(self.state(after_fail), "ready")
        self.assertEqual(self.view(flaky)["task"]["result"], "fixed")
        # invalid needs: non-task, itself, canceled, unknown, too many
        plain = self.s.post(self.alice, text="p", channel="work/x")["message"]["id"]
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="t", needs=[plain]),
                         "invalid_needs")
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="t", needs=["99999"]),
                         "invalid_needs")
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="t", needs=["abc"]),
                         "invalid_needs")
        self.assertEqual(self.code(self.s.create_task, self.alice, re=plain, needs=[plain]), "invalid_needs")
        dead = self.task(text="dead")
        self.s.cancel(self.alice, dead, reason="nope")
        head = self.head()
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="t", needs=[dead]),
                         "invalid_needs")
        self.assertEqual(self.head(), head)
        self.assertIsNone(self.s.thread(self.alice, plain)["message"].get("task"))
        self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="t",
                                   needs=[str(90000 + i) for i in range(21)]), "invalid_request")
        # complete result bounds; canceled and finished tasks refuse claims
        self.assertEqual(self.code(self.s.cancel, self.alice, dead, reason="again"), "task_finished")
        self.assertEqual(self.code(self.s.claim, self.alice, dead), "task_finished")
        rc = self.s.claim(self.alice, top)["claim"]
        self.assertEqual(self.code(self.s.complete, self.alice, top, epoch=1, token=rc["token"], result="x" * 1001),
                         "invalid_request")
        self.assertIsNone(self.s.complete(self.alice, top, epoch=1, token=rc["token"])["task"]["task"]["result"])
        self.assertEqual(self.code(self.s.reopen, self.alice, top), "task_finished")

    def test_set_needs_rules_and_cycles(self) -> None:
        a, b, c = self.task(text="a"), self.task(text="b"), self.task(text="c")
        out = self.s.set_needs(self.alice, a, [b])
        self.assertEqual((out["outcome"], out["task"]["task"]["state"], out["task"]["task"]["needs"]),
                         ("set", "waiting", [b]))
        self.assertEqual(self.s.set_needs(self.alice, a, [b])["outcome"], "unchanged")
        self.assertEqual(self.types()[-1], "task.needs_changed")
        ev = self.evs()[-1]
        self.assertEqual(ev["message"]["task"]["state"], "waiting")
        # cycles: direct, transitive, self; refused change leaves the task unchanged
        head = self.head()
        self.assertEqual(self.code(self.s.set_needs, self.alice, b, [a]), "invalid_needs")
        self.s.set_needs(self.alice, b, [c])
        self.assertEqual(self.code(self.s.set_needs, self.alice, c, [a]), "invalid_needs")
        self.assertEqual(self.code(self.s.set_needs, self.alice, c, [c]), "invalid_needs")
        self.assertEqual(self.view(c)["task"]["needs"], [])
        # once the middle task's needs are cleared the same change is allowed
        self.s.set_needs(self.alice, b, [])
        self.assertEqual(self.state(b), "ready")
        self.assertEqual(self.s.set_needs(self.alice, c, [a])["task"]["task"]["state"], "waiting")
        self.assertGreater(self.head(), head)
        # all completed -> ready in the same write; [] -> ready at once
        done = self.task(text="done")
        dc = self.s.claim(self.alice, done)["claim"]
        self.s.complete(self.alice, done, epoch=1, token=dc["token"])
        self.assertEqual(self.s.set_needs(self.alice, c, [done])["task"]["task"]["state"], "ready")
        self.assertEqual(self.s.set_needs(self.alice, c, [a])["task"]["task"]["state"], "waiting")
        self.assertEqual(self.s.set_needs(self.alice, c, [])["task"]["task"]["state"], "ready")
        # blocked stays blocked and unblocks to where the needs put it
        self.s.block(self.alice, c, reason="hold")
        self.assertEqual(self.s.set_needs(self.alice, c, [a])["task"]["task"]["state"], "blocked")
        self.assertEqual(self.s.unblock(self.alice, c)["task"]["task"]["state"], "waiting")
        # claimed -> task_claimed (lands after release); finished -> task_finished
        cc = self.s.claim(self.alice, b)["claim"]
        self.assertEqual(self.code(self.s.set_needs, self.alice, b, [done]), "task_claimed")
        self.s.release(self.alice, b, epoch=1, token=cc["token"])
        self.assertEqual(self.s.set_needs(self.alice, b, [done])["task"]["task"]["state"], "ready")
        self.s.cancel(self.alice, b, reason="x")
        self.assertEqual(self.code(self.s.set_needs, self.alice, b, []), "task_finished")
        # the walk is bounded: a chain of more than 500 open tasks behind a new need is refused
        prev = self.task(text="chain0")
        chain = [prev]
        for i in range(1, 502):
            prev = self.s.create_task(self.alice, channel="work/x", text=f"chain{i}", needs=[prev])["task"]["id"]
            chain.append(prev)
        self.assertEqual(self.code(self.s.set_needs, self.alice, chain[0], [chain[-1]]), "invalid_needs")

    def test_dependents_listing(self) -> None:
        x = self.task(text="x")
        d1 = self.task(text="d1", needs=[x])
        d2 = self.task(text="d2", needs=[x], channel="work/x/sub")
        d3 = self.task(text="d3", needs=[x], channel="other")
        self.task(text="unrelated")
        ids = lambda **kw: [t["message"]["id"] for t in self.s.list_tasks(self.alice, needs=x, **kw)["tasks"]]  # noqa: E731
        self.assertEqual(ids(), [d1, d2, d3])
        self.assertEqual(ids(channel="work/x"), [d1])
        self.assertEqual(ids(channel="work/x/"), [d1, d2])
        self.assertEqual(ids(channel="other"), [d3])
        self.assertEqual(self.s.list_tasks(self.alice, needs=d1)["tasks"], [])
        # filters and paging on the task list
        page = self.s.list_tasks(self.alice, limit=2)
        self.assertEqual(len(page["tasks"]), 2)
        rest = self.s.list_tasks(self.alice, after=page["cursor"], limit=50)
        self.assertEqual(len(rest["tasks"]), 3)
        self.assertEqual(len(self.s.list_tasks(self.alice, state="waiting")["tasks"]), 3)
        self.assertEqual(self.s.list_tasks(self.alice, state="completed")["tasks"], [])
        self.assertEqual(self.code(self.s.list_tasks, self.alice, state="done"), "invalid_request")
        self.assertEqual(self.code(self.s.list_tasks, self.alice, latest=True, after="1"), "invalid_request")
        self.assertEqual(self.code(self.s.list_tasks, self.alice, after="zz"), "invalid_cursor")
        self.assertEqual(len(self.s.list_tasks(self.alice, by=self.alice.handle)["tasks"]), 5)
        self.assertEqual(self.s.list_tasks(self.alice, owner=self.bob.principal)["tasks"], [])
        latest = self.s.list_tasks(self.alice, latest=True, limit=1)
        self.assertEqual(len(latest["tasks"]), 1)
        # a reply bumps activity so `latest` picks that task
        self.s.post(self.alice, text="chatter", re=x)
        self.assertEqual(self.s.list_tasks(self.alice, latest=True, limit=1)["tasks"][0]["message"]["id"], x)
        self.assertEqual(self.s.list_tasks(self.alice, latest=True, limit=1)["tasks"][0]["replies"], 1)
        # reading to the end returns the board head so later tasks appear after it
        end = self.s.list_tasks(self.alice, limit=500)
        self.assertEqual(end["cursor"], self.s.head())
        late = self.task(text="late")
        self.assertEqual([t["message"]["id"] for t in self.s.list_tasks(self.alice, after=end["cursor"])["tasks"]],
                         [late])

    def test_conditional_block_if_failed(self) -> None:
        pred = self.task(text="pred")
        on_fail = self.task(text="on_fail", needs=[{"id": pred, "if": "failed"}])
        on_ok = self.task(text="on_ok", needs=[pred])
        v = self.view(on_fail)
        self.assertEqual((v["task"]["state"], v["task"]["needs"], v["task"]["needsIf"]), ("waiting", [pred],
                                                                                         {pred: "failed"}))
        self.assertNotIn("needsIf", self.view(on_ok)["task"])
        c = self.s.claim(self.alice, pred)["claim"]
        mark = self.head()
        self.s.fail(self.alice, pred, epoch=1, token=c["token"], reason="boom")
        self.assertEqual(self.types(str(mark)), ["task.failed", "task.ready"])
        self.assertEqual(self.state(on_fail), "ready")
        self.assertEqual(self.state(on_ok), "waiting")
        # completion cancels the conditional dependent, with a reason, in the same transaction
        p2 = self.task(text="p2")
        b2 = self.task(text="b2", needs=[{"id": p2, "if": "failed"}])
        chain = self.task(text="chain", needs=[b2])
        cond_chain = self.task(text="cc", needs=[{"id": b2, "if": "failed"}])
        c2 = self.s.claim(self.alice, p2)["claim"]
        mark = self.head()
        self.s.complete(self.alice, p2, epoch=1, token=c2["token"])
        evs = self.evs(str(mark))
        self.assertEqual([e["type"] for e in evs], ["task.completed", "task.canceled", "task.canceled",
                                                    "task.canceled"])
        self.assertEqual(evs[1]["payload"]["reason"], f"condition not met: {p2} completed")
        self.assertEqual(self.view(b2)["task"]["reason"], f"condition not met: {p2} completed")
        self.assertEqual(self.state(b2), "canceled")
        self.assertEqual(self.view(chain)["task"]["reason"], "prerequisite canceled")
        self.assertEqual(self.state(chain), "canceled")
        self.assertEqual(self.state(cond_chain), "canceled")
        # a canceled prerequisite is invalid in both kinds; a completed one can't fail
        for entry in (b2, {"id": b2, "if": "failed"}, {"id": p2, "if": "failed"}):
            self.assertEqual(self.code(self.s.create_task, self.alice, channel="work/x", text="t", needs=[entry]),
                             "invalid_needs")
        # a condition-cancel is a no-op on a dependent that is already claimed (event note only)
        p3 = self.task(text="p3")
        d3 = self.task(text="d3", needs=[{"id": p3, "if": "failed"}])
        c3 = self.s.claim(self.alice, p3)["claim"]
        self.s.fail(self.alice, p3, epoch=1, token=c3["token"], reason="x")
        dc = self.s.claim(self.alice, d3)["claim"]
        self.s.reopen(self.alice, p3)
        c3b = self.s.claim(self.alice, p3)["claim"]
        mark = self.head()
        self.s.complete(self.alice, p3, epoch=2, token=c3b["token"])
        self.assertEqual(self.state(d3), "claimed")
        self.assertIn("task.condition_unmet", self.types(str(mark)))
        self.s.complete(self.alice, d3, epoch=1, token=dc["token"])
        # conditional dependents can be requested in a batch and through set_needs
        p4 = self.task(text="p4")
        out = self.s.create_tasks(self.alice, "work/x", [{"key": "r", "text": "recover",
                                                           "needs": [{"id": p4, "if": "failed"}]}])
        self.assertEqual(out["tasks"][0]["task"]["needsIf"], {p4: "failed"})
        z = self.task(text="z")
        self.assertEqual(self.s.set_needs(self.alice, z, [{"id": p4, "if": "failed"}])["task"]["task"]["needsIf"],
                         {p4: "failed"})
        c4 = self.s.claim(self.alice, p4)["claim"]
        self.s.fail(self.alice, p4, epoch=1, token=c4["token"], reason="r")
        self.assertEqual(self.state(out["tasks"][0]["id"]), "ready")
        # sweep repairs conditional dependents left over from direct writes
        self.assertEqual(self.s.sweep(), {"expired": [], "readied": [], "canceled": []})

    def test_block_unblock_edit_hold_assignment_ownership(self) -> None:
        tid = self.task(self.alice)
        # block / unblock
        b = self.s.block(self.alice, tid, reason="need input", kind="needs_input")["task"]["task"]
        self.assertEqual((b["state"], b["blocked"]), ("blocked", {"kind": "needs_input", "reason": "need input"}))
        self.assertEqual(self.code(self.s.block, self.alice, tid, reason="again"), "task_blocked")
        self.assertEqual(self.code(self.s.claim, self.alice, tid), "task_blocked")
        self.assertEqual(self.s.unblock(self.alice, tid)["task"]["task"]["state"], "ready")
        mark = self.head()
        self.assertEqual(self.s.unblock(self.alice, tid)["outcome"], "unchanged")
        self.assertEqual(self.head(), mark)
        self.assertEqual(self.code(self.s.block, self.alice, tid, reason=""), "invalid_request")
        self.assertEqual(self.code(self.s.block, self.alice, tid, reason="x", kind="edited"), "invalid_request")
        self.assertEqual(self.code(self.s.block, self.alice, tid, reason="x", kind="Bad Kind"), "invalid_request")
        self.assertEqual(self.s.block(self.alice, tid, reason="x")["task"]["task"]["blocked"]["kind"], None)
        self.s.unblock(self.alice, tid)
        # owner can't block a claimed task; the holder can (epoch+token together); lapsed counts as no claim
        cl = self.s.claim(self.claude, tid)["claim"]
        self.assertEqual(self.code(self.s.block, self.alice, tid, reason="x"), "task_claimed")
        self.assertEqual(self.code(self.s.block, self.claude, tid, reason="x", epoch=1), "invalid_request")
        held = self.s.block(self.claude, tid, reason="stuck", epoch=1, token=cl["token"])["task"]["task"]
        self.assertEqual((held["state"], held["claim"]), ("blocked", None))
        self.assertEqual(self.code(self.s.complete, self.claude, tid, epoch=1, token=cl["token"]), "claim_lost")
        self.s.unblock(self.alice, tid)
        self.s.claim(self.claude, tid, lease=30)
        self.t[0] += 31
        self.assertEqual(self.s.block(self.alice, tid, reason="lapsed")["task"]["task"]["state"], "blocked")
        self.s.unblock(self.alice, tid)
        # unblock with needs -> waiting then ready when they complete
        dep = self.task(text="dep")
        wt = self.task(text="wt", needs=[dep])
        self.s.block(self.alice, wt, reason="hold")
        self.assertEqual(self.s.unblock(self.alice, wt)["task"]["task"]["state"], "waiting")
        dcl = self.s.claim(self.alice, dep)["claim"]
        self.s.complete(self.alice, dep, epoch=1, token=dcl["token"])
        self.assertEqual(self.state(wt), "ready")
        # edit-hold: someone other than the owner edits a task root
        root = self.s.post(self.bob, text="original", channel="work/x")["message"]["id"]
        adopted = self.s.create_task(self.alice, re=root)["task"]["id"]
        hc = self.s.claim(self.alice, adopted)["claim"]
        mark = self.head()
        self.assertEqual(self.s.edit(self.bob, root, "changed")["message"]["task"]["state"], "blocked")
        evs = self.evs(str(mark))
        self.assertEqual([e["type"] for e in evs], ["message.edited", "task.blocked"])
        self.assertEqual({e["actor"]["id"] for e in evs}, {self.bob.actor})
        self.assertEqual(self.view(adopted)["task"]["blocked"]["kind"], "edited")
        self.assertEqual(self.code(self.s.complete, self.alice, adopted, epoch=1, token=hc["token"]), "claim_lost")
        self.assertEqual(self.s.unblock(self.alice, adopted)["task"]["task"]["state"], "ready")
        self.assertEqual(self.s.edit(self.bob, root, "changed again")["message"]["task"]["state"], "blocked")
        self.assertEqual(self.s.unblock(self.alice, adopted)["task"]["task"]["state"], "ready")
        # the owner's own edit holds nothing; an edit of a finished task changes nothing
        own = self.task(self.alice, text="mine")
        self.assertEqual(self.s.edit(self.alice, own, "mine v2")["message"]["task"]["state"], "ready")
        oc = self.s.claim(self.alice, own)["claim"]
        self.s.complete(self.alice, own, epoch=1, token=oc["token"])
        self.s.edit(self.alice, own, "mine v3")
        self.assertEqual(self.state(own), "completed")
        # assignment
        asg = self.task(self.alice, text="assigned")
        out = self.s.assign(self.alice, asg, "claude")
        self.assertEqual(out["task"]["task"]["for"], self.claude.actor)
        self.assertEqual(self.s.assign(self.alice, asg, self.claude.actor)["outcome"], "unchanged")
        self.assertEqual(self.code(self.s.claim, self.codex, asg), "task_assigned")
        self.assertEqual(self.code(self.s.claim, self.alice, asg), "task_assigned")
        self.assertEqual(self.code(self.s.assign, self.alice, asg, "bob+x"), "invalid_request")
        self.assertEqual(self.code(self.s.assign, self.alice, asg, "Bad_Profile"), "invalid_request")
        self.assertEqual(self.s.claim(self.claude, asg)["claim"]["epoch"], 1)
        self.assertEqual(self.s.assign(self.alice, asg, None)["task"]["task"]["for"], None)
        self.s.cancel(self.alice, asg, reason="done with it")
        self.assertEqual(self.code(self.s.assign, self.alice, asg, "claude"), "task_finished")
        # ownership: another principal needs manage; a profile owns only tasks it made; narrowed owner loses it
        target = self.task(self.claude, text="by claude")
        for fn, args in ((self.s.unblock, ()), (self.s.reopen, ()), (self.s.assign, ("claude",)),
                         (self.s.set_needs, ([],)), (self.s.cancel, ()), (self.s.block, ())):
            kw = {"reason": "x"} if fn in (self.s.cancel, self.s.block) else {}
            self.assertEqual(self.code(fn, self.bob, target, *args, **kw), "task_not_owned", fn.__name__)
        self.assertEqual(self.code(self.s.block, self.codex, target, reason="x"), "task_not_owned")
        self.assertEqual(self.s.block(self.alice, target, reason="bare principal owns it")["task"]["task"]["state"],
                         "blocked")
        manager = Caller(self.bob.principal, None, frozenset({"read", "post", "react", "label", "manage"}))
        self.assertEqual(self.s.unblock(manager, target)["task"]["task"]["state"], "ready")
        read_only_owner = Caller(self.alice.principal, None, frozenset({"read"}))
        self.assertEqual(self.code(self.s.block, read_only_owner, target, reason="x"), "task_not_owned")
        # a non-owner may claim and settle (needs only label)
        theirs = self.task(self.alice, text="theirs")
        bc = self.s.claim(self.bob, theirs)["claim"]
        self.assertEqual(self.s.complete(self.bob, theirs, epoch=1, token=bc["token"])["outcome"], "completed")
        # narrowed `can`
        narrowed = Caller(self.bob.principal, None, frozenset({"read", "post"}))
        open_one = self.task(self.alice, text="open")
        self.assertEqual(self.code(self.s.claim, narrowed, open_one), "forbidden")
        self.assertEqual(self.code(self.s.react, narrowed, open_one, "eyes"), "forbidden")
        self.assertEqual(self.code(self.s.label, narrowed, open_one, set={"a": "b"}), "forbidden")
        self.assertEqual(self.code(self.s.post, Caller(self.bob.principal, None, frozenset({"read"})), text="x",
                                   channel="dev"), "forbidden")
        self.assertEqual(self.code(self.s.threads, Caller(self.bob.principal, None, frozenset({"post"}))),
                         "forbidden")
        self.assertEqual(self.state(open_one), "ready")
        self.assertEqual(self.s.me(narrowed)["can"], ["read", "post"])

    def test_batch_atomic_keys_cycles_limits_idempotency(self) -> None:
        plan = [{"key": "a", "text": "A"},
                {"key": "b", "text": "B", "needs": [{"key": "a"}], "labels": {"lane": "x"}},
                {"key": "c", "text": "C", "needs": ["b", "d"]},      # forward reference by key string
                {"key": "d", "text": "D"}]
        mark = self.head()
        out = self.s.create_tasks(self.alice, "work/b", plan, client_id="B1")["tasks"]
        ids = [t["id"] for t in out]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual([t["text"] for t in out], ["A", "B", "C", "D"])
        self.assertEqual([t["task"]["state"] for t in out], ["ready", "waiting", "waiting", "ready"])
        self.assertEqual(out[2]["task"]["needs"], [ids[1], ids[3]])
        self.assertEqual(out[1]["labels"], {"lane": "x", "board.task": "waiting"})
        self.assertEqual([t["message"]["id"] for t in self.s.list_tasks(self.alice, channel="work/b")["tasks"]], ids)
        # event order per task: posted, created, [ready], consecutive
        evs = self.evs(str(mark))
        core = [e["type"] for e in evs if e["type"] not in ("actor.seen", "channel.created")]
        self.assertEqual(core, ["message.posted", "task.created"] * 4)
        cursors = [int(e["cursor"]) for e in evs if e["type"] in ("message.posted", "task.created")]
        self.assertEqual(cursors, list(range(cursors[0], cursors[0] + 8)))
        # completing a batch task readies its dependents
        ca = self.s.claim(self.alice, ids[0])["claim"]
        self.s.complete(self.alice, ids[0], epoch=1, token=ca["token"])
        self.assertEqual(self.state(ids[1]), "ready")
        self.assertEqual(self.state(ids[2]), "waiting")
        # existing completed needs -> ready immediately with task.ready right after task.created
        mark = self.head()
        two = self.s.create_tasks(self.alice, "work/b", [{"text": "E", "needs": [ids[0]]}, {"text": "F"}])["tasks"]
        self.assertEqual([t["task"]["state"] for t in two], ["ready", "ready"])
        self.assertEqual([e["type"] for e in self.evs(str(mark))],
                         ["message.posted", "task.created", "task.ready", "message.posted", "task.created"])
        # idempotency: same request returns current state and creates nothing; any difference is duplicate_id
        mark = self.head()
        again = self.s.create_tasks(self.alice, "work/b", plan, client_id="B1")["tasks"]
        self.assertEqual([t["id"] for t in again], ids)
        self.assertEqual(again[0]["task"]["state"], "completed")
        self.assertEqual(self.head(), mark)
        self.assertEqual([t["id"] for t in self.s.batch(self.alice, "B1")["tasks"]], ids)
        self.assertEqual(self.code(self.s.batch, self.alice, "nope"), "batch_not_found")
        self.assertEqual(self.code(self.s.batch, self.bob, "B1"), "batch_not_found")
        for variant in (plan[:3], plan + [{"text": "Z"}], [dict(plan[0], text="A2")] + plan[1:],
                        [dict(plan[0], key="a2")] + plan[1:], plan[::-1]):
            self.assertEqual(self.code(self.s.create_tasks, self.alice, "work/b", variant, client_id="B1"),
                             "duplicate_id")
        self.assertEqual(self.code(self.s.create_tasks, self.alice, "work/other", plan, client_id="B1"),
                         "duplicate_id")
        self.assertEqual(self.code(self.s.create_tasks, self.bob, "work/b", plan, client_id="B1"), "duplicate_id")
        self.assertEqual(self.code(self.s.create_tasks, self.claude, "work/b", plan, client_id="B1"), "duplicate_id")
        # all-or-nothing: nothing is created, the channel isn't created, no events
        head = self.head()
        bad_plans = {
            "unknown key": [{"text": "x", "needs": [{"key": "nobody"}]}],
            "dup key": [{"key": "a", "text": "x"}, {"key": "a", "text": "y"}],
            "self": [{"key": "a", "text": "x", "needs": [{"key": "a"}]}],
            "cycle": [{"key": "x", "text": "x", "needs": [{"key": "z"}]}, {"key": "y", "text": "y",
                                                                           "needs": [{"key": "x"}]},
                      {"key": "z", "text": "z", "needs": [{"key": "y"}]}],
            "canceled": [{"text": "x", "needs": [self._canceled()]}],
            "non task": [{"text": "x", "needs": [self.s.post(self.alice, text="p", channel="dev")["message"]["id"]]}],
        }
        head = self.head()
        for name, bad in bad_plans.items():
            head = self.head() if name in ("canceled", "non task") else head
            with self.assertRaises(VBoardError, msg=name) as cm:
                self.s.create_tasks(self.alice, "fresh/chan", bad)
            self.assertEqual(cm.exception.code, "invalid_needs", name)
            self.assertEqual(self.head(), head, name)
        with self.assertRaises(VBoardError) as cm:
            self.s.create_tasks(self.alice, "fresh/chan", bad_plans["cycle"])
        self.assertEqual(cm.exception.message, '"x", "z", "y" wait for each other')
        self.assertEqual(self.code(self.s.threads, self.alice, channel="fresh/chan"), "channel_not_found")
        for bad in ([{"text": "ok", "for": "bob+x"}], [{"text": "   "}], [{"text": "x", "needs": [str(i) for i in
                                                                                                    range(100, 121)]}],
                    [{"text": "x", "key": "k" * 65}], [{"text": "x", "bogus": 1}], [{"text": "x", "labels":
                                                                                    {"board.x": "1"}}]):
            err = self.code(self.s.create_tasks, self.alice, "fresh/chan", bad)
            self.assertIn(err, ("invalid_request", "reserved_label"))
        self.assertEqual(self.code(self.s.create_tasks, self.alice, "fresh/chan", [{"text": "x" * 10001}]),
                         "text_too_long")
        # limits: 101 -> batch_too_large (checked before anything else), 100 chained tasks succeed
        too_many = [{"key": f"k{i}", "text": "t", "needs": [{"key": f"k{i + 1}"}]} for i in range(101)]
        too_many[5] = {"bogus": True}
        self.assertEqual(self.code(self.s.create_tasks, self.alice, "fresh/chan", too_many), "batch_too_large")
        self.assertEqual(self.code(self.s.create_tasks, self.alice, "fresh/chan", too_many[:100]), "invalid_request")
        chain = [{"key": f"k{i}", "text": f"t{i}", "needs": [{"key": f"k{i + 1}"}] if i < 99 else []}
                 for i in range(100)]
        made = self.s.create_tasks(self.alice, "chain/chan", chain)["tasks"]
        self.assertEqual((len(made), made[-1]["task"]["state"], made[0]["task"]["state"]), (100, "ready", "waiting"))
        self.assertEqual(self.code(self.s.create_tasks, self.alice, "fresh/chan", []), "invalid_request")

    def _canceled(self) -> str:
        tid = self.task(text="to cancel")
        self.s.cancel(self.alice, tid, reason="no")
        return tid

    def test_event_sequences_and_token_secrecy(self) -> None:
        a = self.task(text="a")
        b = self.task(text="b", needs=[a])
        mark = self.head()
        ca = self.s.claim(self.alice, a)["claim"]
        self.s.renew(self.alice, a, epoch=1, token=ca["token"])
        self.s.complete(self.alice, a, epoch=1, token=ca["token"], result="ok")
        cb = self.s.claim(self.alice, b)["claim"]
        self.s.block(self.alice, b, reason="wait", epoch=1, token=cb["token"])
        self.s.unblock(self.alice, b)
        evs = self.s.events(self.alice, after=str(mark))["events"]
        seq = [(e["type"], e["message"]["task"]["state"]) for e in evs]
        self.assertEqual(seq, [("task.claimed", "claimed"), ("task.completed", "completed"),
                               ("task.ready", "ready"), ("task.claimed", "claimed"), ("task.blocked", "blocked"),
                               ("task.unblocked", "waiting"), ("task.ready", "ready")])
        hidden = self.types(str(mark))
        self.assertIn("task.renewed", hidden)
        self.assertNotIn("task.renewed", [e["type"] for e in evs])
        # the create-waiting -> complete sequence of b5 item 70
        c = self.task(text="c")
        d = self.task(text="d", needs=[c])
        mark = self.head()
        cc = self.s.claim(self.alice, c)["claim"]
        self.s.complete(self.alice, c, epoch=1, token=cc["token"])
        got = [(e["type"], e["message"]["task"]["state"]) for e in self.s.events(self.alice, after=str(mark))["events"]]
        self.assertEqual(got, [("task.claimed", "claimed"), ("task.completed", "completed"),
                               ("task.ready", "ready")])
        self.assertEqual(self.state(d), "ready")
        # token secrecy: no "token" key, no 64-hex string, and never the secret itself, in any view or event
        tokens = [ca["token"], cb["token"], cc["token"]]
        dump = json.dumps({"events": self.evs(), "views": [self.view(x) for x in (a, b, c, d)],
                           "list": self.s.list_tasks(self.alice, state="all"),
                           "threads": self.s.threads(self.alice), "export": list(self.s.export_events())})
        self.assertNotIn('"token"', dump)
        self.assertNotIn('"token_hash"', dump)
        self.assertIsNone(HEX64.search(dump))
        for tok in tokens:
            self.assertNotIn(tok, dump)
        for e in self.evs():
            self.assertEqual(set(e["message"]["by"]) if e["message"] else {"actor", "principal", "profile"},
                             {"actor", "principal", "profile"})
        self.assertNotIn("session", dump)
        self.assertEqual([e["type"] for e in self.s.events(self.alice, after="0", label=["board.task=blocked"])
                          ["events"]], ["task.blocked", "task.unblocked"])   # enter and leave the filter
        # exported log is replayable and ordered
        exported = list(self.s.export_events("0"))
        self.assertEqual([int(e["cursor"]) for e in exported], sorted(int(e["cursor"]) for e in exported))
        self.assertEqual(len(exported), len(self.evs()))

    def test_fold_guards_ignore_stale_events(self) -> None:
        def created(**kw):
            return fold_task(None, "task.created", {"owner": "o", "by": "b", **kw})

        t = created()
        self.assertEqual((t["state"], t["epoch"]), ("ready", 0))
        self.assertEqual(created(needs=["1"])["state"], "waiting")
        # created only applies to a root, non-deleted, non-task message
        self.assertIsNone(fold_task(None, "task.created", {"owner": "o", "by": "b"}, is_root=False))
        self.assertIsNone(fold_task(None, "task.created", {"owner": "o", "by": "b"}, deleted=True))
        self.assertIs(fold_task(t, "task.created", {"owner": "o", "by": "b"}), t)
        # events that apply from the wrong state change nothing (same object back)
        self.assertIs(fold_task(t, "task.ready", {}), t)
        for etype in ("task.renewed", "task.released", "task.expired", "task.completed", "task.failed"):
            self.assertIs(fold_task(t, etype, {"epoch": 0, "until_ms": 1}), t, etype)
        self.assertIs(fold_task(t, "task.unblocked", {}), t)
        self.assertIs(fold_task(t, "task.reopened", {}), t)
        self.assertIs(fold_task(t, "task.assigned", {"for": None}), t)
        self.assertIsNot(fold_task(t, "task.assigned", {"for": "o+x"}), t)
        claim = {"actor": "o+x", "until_ms": 5, "token_hash": "h"}
        self.assertIs(fold_task(t, "task.claimed", {"epoch": 2, **claim}), t)      # must be epoch + 1
        c1 = fold_task(t, "task.claimed", {"epoch": 1, **claim})
        self.assertEqual((c1["state"], c1["epoch"], c1["claim"]["actor"]), ("claimed", 1, "o+x"))
        self.assertIs(fold_task(c1, "task.claimed", {"epoch": 2, **claim}), c1)    # only from ready
        self.assertIs(fold_task(c1, "task.ready", {}), c1)
        for etype in ("task.renewed", "task.released", "task.expired", "task.completed", "task.failed"):
            self.assertIs(fold_task(c1, etype, {"epoch": 0}), c1, etype)           # stale epoch
            self.assertIs(fold_task(c1, etype, {"epoch": 2}), c1, etype)
        self.assertEqual(fold_task(c1, "task.renewed", {"epoch": 1, "until_ms": 99})["claim"]["until_ms"], 99)
        self.assertEqual(fold_task(c1, "task.released", {"epoch": 1})["state"], "ready")
        self.assertEqual(fold_task(c1, "task.expired", {"epoch": 1})["claim"], None)
        done = fold_task(c1, "task.completed", {"epoch": 1, "result": "r"})
        self.assertEqual((done["state"], done["result"]), ("completed", "r"))
        for etype in ("task.completed", "task.failed", "task.released", "task.blocked", "task.canceled",
                      "task.needs_changed", "task.ready", "task.claimed", "task.unblocked", "task.reopened"):
            self.assertIs(fold_task(done, etype, {"epoch": 1, "needs": [], "reason": "x"}), done, etype)
        failed = fold_task(c1, "task.failed", {"epoch": 1, "reason": "no"})
        self.assertEqual(fold_task(failed, "task.reopened", {})["state"], "ready")
        self.assertEqual(fold_task(failed, "task.canceled", {"reason": "x"})["state"], "canceled")
        blocked = fold_task(c1, "task.blocked", {"kind": None, "reason": "r"})
        self.assertEqual((blocked["state"], blocked["claim"]), ("blocked", None))
        self.assertIs(fold_task(blocked, "task.blocked", {"reason": "r"}), blocked)
        self.assertEqual(fold_task(blocked, "task.needs_changed", {"needs": ["1"]})["state"], "blocked")
        self.assertEqual(fold_task(blocked, "task.unblocked", {"state": "waiting"})["state"], "waiting")
        self.assertIs(fold_task(c1, "task.needs_changed", {"needs": ["1"]}), c1)
        self.assertEqual(fold_task(t, "task.needs_changed", {"needs": ["1"], "state": "waiting"})["state"], "waiting")
        canceled = fold_task(c1, "task.canceled", {"reason": "x"})
        self.assertIs(fold_task(canceled, "task.canceled", {"reason": "y"}), canceled)
        self.assertIsNone(fold_task(None, "task.completed", {"epoch": 1}))
        # the store ignores a duplicate application through the same helper: re-claim with the same token is a no-op
        tid = self.task()
        tok = "T" * 22
        first = self.s.claim(self.alice, tid, token=tok)["claim"]
        mark = self.head()
        self.assertEqual(self.s.claim(self.alice, tid, token=tok)["claim"], first)
        self.assertEqual(self.head(), mark)

    def test_concurrent_writers_two_processes(self) -> None:
        total = 60
        ids = [self.task(text=f"job{i}") for i in range(total)]
        worker = (
            "import sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from fusion_swarm.vboard import BoardStore, Caller\n"
            "s = BoardStore(sys.argv[2])\n"
            "me = Caller.local(sys.argv[3], user='alice')\n"
            "n = 0\n"
            "while True:\n"
            "    got = s.claim_next(me, 'work/x', lease=300)\n"
            "    if got['task'] is None:\n"
            "        break\n"
            "    s.complete(me, got['task']['id'], epoch=got['claim']['epoch'], token=got['claim']['token'])\n"
            "    n += 1\n"
            "print(n)\n")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        procs = [subprocess.Popen([sys.executable, "-c", worker, str(ROOT / "python"), self.db, f"w{i}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for i in range(2)]
        # meanwhile this process also writes
        mine = 0
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            got = self.s.claim_next(self.alice, "work/x", lease=300)
            if got["task"] is None:
                break
            self.s.complete(self.alice, got["task"]["id"], epoch=got["claim"]["epoch"], token=got["claim"]["token"])
            mine += 1
        counts = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            self.assertNotIn("locked", err.lower())
            counts.append(int(out.strip()))
        self.assertEqual(sum(counts) + mine, total)
        self.assertEqual(len(self.s.list_tasks(self.alice, state="completed", limit=500)["tasks"]), total)
        self.assertEqual(self.s.list_tasks(self.alice, limit=500)["tasks"], [])
        types = self.types()
        self.assertEqual(types.count("task.claimed"), total)
        self.assertEqual(types.count("task.completed"), total)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        idxs = [r[0] for r in con.execute("SELECT idx FROM events ORDER BY idx")]
        self.assertEqual(idxs, list(range(1, len(idxs) + 1)))
        con.close()
        self.assertEqual(len(ids), total)


class JudgeRulings(Base):
    def test_replayed_complete_is_idempotent(self) -> None:
        tid = self.task()
        c = self.s.claim(self.alice, tid)["claim"]
        first = self.s.complete(self.alice, tid, epoch=1, token=c["token"], result="ok")
        self.assertEqual(first["outcome"], "completed")
        mark = self.head()
        again = self.s.complete(self.alice, tid, epoch=1, token=c["token"], result="ok")
        self.assertEqual(again["outcome"], "unchanged")
        self.assertEqual(again["task"]["task"]["state"], "completed")
        self.assertEqual(self.head(), mark)
        # a different settle on a settled task keeps task_finished; a wrong token is claim_lost
        self.assertEqual(self.code(self.s.fail, self.alice, tid, epoch=1, token=c["token"], reason="x"),
                         "task_finished")
        self.assertEqual(self.code(self.s.release, self.alice, tid, epoch=1, token=c["token"]), "task_finished")
        self.assertEqual(self.code(self.s.complete, self.alice, tid, epoch=1, token="z" * 22), "claim_lost")
        self.assertEqual(self.code(self.s.complete, self.alice, tid, epoch=2, token=c["token"]), "claim_lost")
        # fail and release replay the same way
        t2 = self.task(text="two")
        c2 = self.s.claim(self.alice, t2)["claim"]
        self.s.fail(self.alice, t2, epoch=1, token=c2["token"], reason="boom")
        self.assertEqual(self.s.fail(self.alice, t2, epoch=1, token=c2["token"], reason="boom")["outcome"],
                         "unchanged")
        t3 = self.task(text="three")
        c3 = self.s.claim(self.alice, t3)["claim"]
        self.s.release(self.alice, t3, epoch=1, token=c3["token"])
        mark = self.head()
        self.assertEqual(self.s.release(self.alice, t3, epoch=1, token=c3["token"])["outcome"], "unchanged")
        self.assertEqual(self.head(), mark)
        # once another actor holds a newer claim the old replay is a lost claim
        self.s.claim(self.bob, t3)
        self.assertEqual(self.code(self.s.release, self.alice, t3, epoch=1, token=c3["token"]), "claim_lost")

    def test_claim_retry_same_token_returns_same_claim(self) -> None:
        tid = self.task()
        tok = "abcdefghijklmnop1234"
        first = self.s.claim(self.alice, tid, token=tok, lease=300)
        self.assertEqual(first["claim"]["token"], tok)
        self.t[0] += 5
        mark = self.head()
        retry = self.s.claim(self.alice, tid, token=tok, lease=600)
        self.assertEqual(retry["claim"], first["claim"])
        self.assertEqual(self.head(), mark)
        self.assertEqual(self.code(self.s.claim, self.bob, tid, token="otherotherother12345"), "task_claimed")
        self.assertEqual(self.code(self.s.claim, self.alice, tid, token="short"), "invalid_request")
        self.assertEqual(self.code(self.s.claim, self.alice, tid, token="bad token!bad token!"), "invalid_request")
        nxt = self.task(text="next")
        got = self.s.claim_next(self.alice, "work/x", token="nexttokennexttoken12")
        self.assertEqual((got["task"]["id"], got["claim"]["token"]), (nxt, "nexttokennexttoken12"))
        # the stored form is the sha256 hash, never the token
        con = sqlite3.connect(self.db)
        stored = con.execute("SELECT claim_token_hash FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        con.close()
        self.assertRegex(stored, r"^[0-9a-f]{64}$")
        self.assertNotEqual(stored, tok)

    def test_shared_store_concurrent_read_write_threads(self) -> None:
        for i in range(24):
            self.task(text=f"job{i}")
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            me = Caller.local(f"w{n}", user="alice")
            try:
                for k in range(6):
                    self.s.post(me, text=f"post {n}.{k}", channel=f"chan{n % 3}")
                    self.s.threads(me, limit=5)
                    self.s.events(me, last=5)
                    self.s.list_tasks(me, state="all", limit=10)
                    got = self.s.claim_next(me, "work/x", lease=60)
                    if got["task"]:
                        self.s.renew(me, got["task"]["id"], epoch=got["claim"]["epoch"], token=got["claim"]["token"])
                        self.s.complete(me, got["task"]["id"], epoch=got["claim"]["epoch"],
                                        token=got["claim"]["token"])
                    self.s.events(me, after=self.s.head(), wait=0.02)
                    self.s.sweep()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
            self.assertFalse(th.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.s.list_tasks(self.alice, state="completed", limit=500)["tasks"]), 24)
        self.assertEqual(self.types().count("task.claimed"), 24)
        # each thread used its own sqlite connection ...
        self.assertGreaterEqual(len({id(c) for _, c in self.s._conns}), 2)
        # ... and the connections of ended threads are closed, not leaked (one-thread-per-request servers)
        for _ in range(30):
            th = threading.Thread(target=lambda: self.s.head())
            th.start()
            th.join(timeout=5)
        self.s.head()
        self.assertLess(len(self.s._conns), 12)

    def test_sweep_repairs_readiness_left_by_a_lost_update(self) -> None:
        a = self.task(text="a")
        b = self.task(text="b", needs=[a])
        self.assertEqual(self.state(b), "waiting")
        con = sqlite3.connect(self.db)      # simulate a crash between two writes: a completed, b never readied
        con.execute("UPDATE tasks SET state='completed' WHERE id=?", (a,))
        con.commit()
        con.close()
        self.assertEqual(self.s.sweep(), {"expired": [], "readied": [b], "canceled": []})
        self.assertEqual(self.state(b), "ready")
        self.assertEqual(self.s.sweep(), {"expired": [], "readied": [], "canceled": []})

    def test_sweep_probe_is_read_only_and_closed_store_errors(self) -> None:
        head = self.head()
        for _ in range(5):
            self.assertEqual(self.s.sweep(), {"expired": [], "readied": [], "canceled": []})
        self.assertEqual(self.head(), head)
        extra = self.open()
        extra.close()
        self.assertEqual(self.code(extra.head), "unavailable")

    def test_me_actors_and_event_actor_view(self) -> None:
        me = self.s.me(self.claude)
        self.assertEqual((me["actor"], me["principal"], me["kind"], me["handle"], me["profile"], me["source"]),
                         (self.claude.actor, self.alice.principal, "agent", "alice/claude", "claude", "local"))
        self.assertEqual(me["can"], ["read", "post", "react", "label", "manage"])
        self.assertIn(self.claude.actor, [a["id"] for a in self.s.actors(self.claude)["actors"]])
        self.s.post(self.bob, text="hello", channel="dev")
        ids = [a["id"] for a in self.s.actors(self.alice)["actors"]]
        self.assertIn(self.bob.actor, ids)
        self.assertEqual([a["id"] for a in self.s.actors(self.alice, ids=[self.bob.actor])["actors"]],
                         [self.bob.actor])
        ev = [e for e in self.evs() if e["type"] == "message.posted"][0]
        self.assertEqual(set(ev["actor"]), {"id", "kind", "handle", "name", "avatar"})
        self.assertEqual(ev["actor"]["kind"], "user")


class ModelAndClaims(unittest.TestCase):
    def test_error_table_and_documents(self) -> None:
        for code in ("claim_lost", "task_claimed", "newer_message", "duplicate_id"):
            self.assertEqual(M.REASONS[code].status, 409)
        self.assertEqual(M.REASONS["forbidden"].status, 403)
        self.assertEqual(M.REASONS["not_supported"].status, 501)
        self.assertTrue(M.REASONS["rate_limited"].retryable)
        err = VBoardError("claim_lost", "gone", hint="re-claim")
        self.assertEqual(err.status, 409)
        self.assertEqual(err.to_http(), {"error": {"code": "claim_lost", "message": "gone", "hint": "re-claim"}})
        doc = err.to_failure()
        self.assertEqual((doc["status"], doc["reason"], doc["data"]["error"]["status"], doc["data"]["retryable"]),
                         ("error", "claim_lost", 409, False))
        rl = VBoardError("rate_limited", "slow", retry_after=3)
        self.assertEqual(rl.to_failure()["data"]["error"]["retryAfter"], 3)
        self.assertTrue(rl.retryable)
        action = VBoardError("invalid_profile", "need --as", data={"action_required": True}).to_failure()
        self.assertEqual(action["status"], "action_required")
        self.assertEqual(VBoardError("weird", "x").status, 500)

    def test_validators_and_time(self) -> None:
        self.assertEqual(M.parse_duration("30s"), 30)
        self.assertEqual(M.parse_duration("10m"), 600)
        self.assertEqual(M.parse_duration("1h"), 3600)
        self.assertEqual(M.parse_duration("1d"), 86400)
        self.assertEqual(M.parse_duration(45), 45)
        self.assertEqual(M.parse_duration("90"), 90)
        for bad in ("abc", "-5", -1, True, "10x"):
            with self.assertRaises(VBoardError):
                M.parse_duration(bad)
        self.assertEqual(M.iso(1_700_000_000_123), "2023-11-14T22:13:20.123Z")
        self.assertEqual(M.parse_iso("2023-11-14T22:13:20.123Z"), 1_700_000_000_123)
        self.assertEqual(M.parse_iso("2023-11-14T22:13:20Z"), 1_700_000_000_000)
        self.assertTrue(M.channel_ok("work/x_1-a"))
        for bad in ("Work", "a//b", "/a", "a/", "", "a b", "a" * 2049):
            self.assertFalse(M.channel_ok(bad), bad)
        M.label_ok("k", "v")
        M.label_ok("k", None)
        for key, value, code in (("board.x", "v", "reserved_label"), ("Bad", "v", "invalid_request"),
                                 ("constructor", "v", "invalid_request"), ("k", "x" * 257, "invalid_request"),
                                 ("k", "a\x1bb", "invalid_request"), ("k" * 65, "v", "invalid_request")):
            with self.assertRaises(VBoardError) as cm:
                M.label_ok(key, value)
            self.assertEqual(cm.exception.code, code)
        M.label_ok("board.task", "ready", allow_reserved=True)
        self.assertEqual(M.normalize_profile("Claude"), "claude")
        self.assertIsNone(M.normalize_profile(None))
        with self.assertRaises(VBoardError):
            M.normalize_profile("9bad")
        c = Caller.local("claude", user="Some User!")
        self.assertEqual((c.principal, c.actor, c.handle), ("local:user:some-user", "local:user:some-user+claude",
                                                             "some-user/claude"))
        self.assertEqual(Caller.local(user="a").actor, "local:user:a")
        self.assertEqual(M.emoji_ok(":Eyes:"), "eyes")
        with self.assertRaises(VBoardError):
            M.emoji_ok("bad emoji")
        self.assertEqual(M.lease_ok(None, default_s=900, cap_s=3600), 900)
        self.assertEqual(M.lease_ok(30, default_s=900, cap_s=3600), 30)

    def test_claim_cache_layout_and_prune(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = [1_000_000.0]
            cache = ClaimCache(Path(tmp) / "board" / "claims", clock=lambda: now[0])
            board = board_key(os.path.join(tmp, "x.db"))
            self.assertTrue(board.startswith("file://"))
            path = cache.put(board, "812", "Claude", 3, "secret-token", "2026-11-21T20:01:20.474Z")
            self.assertEqual(path.name, claim_key(board, "812", "claude") + ".json")
            self.assertEqual(len(path.stem), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            doc = json.loads(path.read_text())
            self.assertEqual(set(doc), {"board", "task", "profile", "epoch", "token", "until"})
            self.assertEqual((doc["profile"], doc["epoch"]), ("claude", 3))
            self.assertEqual(cache.get(board, "812", "claude")["token"], "secret-token")
            self.assertIsNone(cache.get(board, "812", "other"))
            self.assertIsNone(cache.get(board, "812", None))
            self.assertIsNone(cache.get(board, "813", "claude"))
            expect = __import__("hashlib").sha256(json.dumps([board, "812", "claude"], separators=(",", ":"),
                                                             ensure_ascii=False).encode()).hexdigest()[:32]
            self.assertEqual(path.stem, expect)
            self.assertFalse(list(path.parent.glob("*.tmp")))
            # non-ascii board keys hash without escaping (ensure_ascii=False)
            self.assertNotEqual(claim_key("file:///tmp/\u00e9", "1", None), claim_key("file:///tmp/e", "1", None))
            # a claim whose until is more than 24 h old is pruned on the next put
            old = cache.put(board, "1", None, 1, "tok", M.iso(int((now[0] - 25 * 3600) * 1000)))
            fresh = cache.put(board, "2", None, 1, "tok", M.iso(int((now[0] + 100) * 1000)))
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(cache.drop(board, "2", None))
            self.assertFalse(cache.drop(board, "2", None))
            self.assertEqual(claims_dir({"XDG_CONFIG_HOME": "/x/y"}), Path("/x/y/board/claims"))
            self.assertEqual(claims_dir({"HOME": "/h"}), Path("/h/.config/board/claims"))

    def test_package_exports_and_no_forbidden_imports(self) -> None:
        import fusion_swarm.vboard as pkg
        self.assertEqual(pkg.VERSION, "3.3.0+swarm")
        self.assertEqual(pkg.PROTOCOL, "vercel-board/0.54.0-compatible")
        self.assertEqual(sorted(pkg.__all__), sorted(["VERSION", "PROTOCOL", "VBoardError", "Caller", "BoardStore"]))
        src = (ROOT / "python" / "fusion_swarm" / "vboard" / "__init__.py").read_text()
        for mod in ("port", "http", "cli", "mcp", "conformance"):
            self.assertNotRegex(src, rf"from \.{mod} import|import \.{mod}")


if __name__ == "__main__":
    unittest.main()
