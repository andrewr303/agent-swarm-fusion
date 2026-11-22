"""Loopback tests for the vboard HTTP server (python/fusion_swarm/vboard/http.py).

stdlib unittest; loopback only; temp dirs for the database and the token file (XDG_CONFIG_HOME is never the real one).
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_vboard_http -v   (from the plugin root)
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm.vboard import VERSION  # noqa: E402
from fusion_swarm.vboard.http import BoardHttpServer, parse_can_header, parse_profile_header, serve  # noqa: E402
from fusion_swarm.vboard.model import VBoardError  # noqa: E402
from fusion_swarm.vboard.store import BoardStore  # noqa: E402


class Client:
    """Tiny http.client wrapper bound to one server; one connection per instance (not thread-safe)."""

    def __init__(self, server: BoardHttpServer, *, token: str | None = "default", profile: str | None = None,
                 host_header: str | None = None, timeout: float = 10.0):
        parts = urlsplit(server.url)
        self.base = parts.path.rstrip("/")
        self.host, self.port = parts.hostname, parts.port
        self.token = server.token if token == "default" else token
        self.profile = profile
        self.host_header = host_header
        self.timeout = timeout
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def close(self) -> None:
        self.conn.close()

    def headers(self, extra: dict | None = None) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.profile:
            h["x-board-profile"] = self.profile
        if self.host_header:
            h["Host"] = self.host_header
        h.update(extra or {})
        return h

    def request(self, method: str, path: str, body: dict | None = None, *, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        self.conn.request(method, self.base + path, body=data, headers=self.headers(headers))
        resp = self.conn.getresponse()
        raw = resp.read()
        doc = json.loads(raw) if raw else None
        return resp.status, doc, resp

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)


class Base(unittest.TestCase):
    server_kw: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.tmp.name, "config")
        self.store = BoardStore(os.path.join(self.tmp.name, "b.db"), name="testboard")
        self.addCleanup(self.store.close)
        kw = dict(user="alice", ping_s=0.2, stream_max_s=5.0, hold_s=0.6)
        kw.update(self.server_kw)
        self.server = BoardHttpServer(self.store, **kw).start()
        self.addCleanup(self.server.stop)
        self._clients: list[Client] = []
        self.c = self.client(profile="claude")

    def client(self, **kw) -> Client:
        c = Client(self.server, **kw)
        self._clients.append(c)
        self.addCleanup(c.close)
        return c


class TestDiscoveryAndAuth(Base):
    def test_discovery_document_audience_matches_url(self):
        c = self.client(token=None)
        status, doc, _ = c.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(doc["url"], self.server.url)
        self.assertEqual(doc["audience"], doc["url"])
        self.assertIsNone(doc["team_id"])
        self.assertIsNone(doc["project_id"])
        self.assertEqual(doc["sync"], [])
        self.assertEqual(doc["version"], VERSION)
        self.assertEqual(doc["name"], "testboard")
        self.assertEqual(doc["fusion"], {"conditional_needs": True, "priority": True, "defer": True, "batch_max": 100})
        # audience tracks the URL the client used (localhost vs 127.0.0.1)
        port = self.server.port
        c2 = self.client(token=None, host_header=f"localhost:{port}")
        _, doc2, _ = c2.get("/")
        self.assertEqual(doc2["audience"], f"http://localhost:{port}/api/board")
        self.assertEqual(doc2["url"], doc2["audience"])

    def test_auth_required_and_token_file_written_0600(self):
        anon = self.client(token=None)
        status, doc, resp = anon.get("/me")
        self.assertEqual(status, 401)
        self.assertEqual(doc["error"]["code"], "unauthorized")
        self.assertEqual(resp.getheader("WWW-Authenticate"), "Bearer")
        bad = self.client(token="not-the-token")
        status, doc, _ = bad.get("/me")
        self.assertEqual((status, doc["error"]["code"]), (401, "unauthorized"))
        self.assertNotIn(self.server.token, json.dumps(doc))
        status, me, _ = self.c.get("/me")
        self.assertEqual(status, 200)
        self.assertEqual(me["actor"], "local:user:alice+claude")

        path = self.server.write_token_file(self.cfg)
        expect = Path(self.cfg) / "board" / "local" / (hashlib.sha256(self.server.url.encode()).hexdigest()[:32] + ".json")
        self.assertEqual(path, expect)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        doc = json.loads(path.read_text())
        self.assertEqual(doc, {"url": self.server.url, "token": self.server.token, "handle": "alice"})
        # honours XDG_CONFIG_HOME when no explicit home is given, and rewrites an over-permissive file at 0600
        os.chmod(path, 0o644)
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.cfg
        try:
            again = self.server.write_token_file()
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME")
            else:
                os.environ["XDG_CONFIG_HOME"] = old
        self.assertEqual(again, path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.server.stop()
        self.assertFalse(path.exists(), "stop() removes the token files it wrote")

    def test_no_auth_mode_and_supplied_token(self):
        with tempfile.TemporaryDirectory() as d:
            st = BoardStore(os.path.join(d, "x.db"))
            srv = serve(st, auth=False, user="bob")
            try:
                c = Client(srv, token=None)
                status, me, _ = c.get("/me")
                self.assertEqual((status, me["principal"]), (200, "local:user:bob"))
                c.close()
            finally:
                srv.stop()
                st.close()
            st2 = BoardStore(os.path.join(d, "y.db"))
            srv2 = serve(st2, token="t" * 24)
            try:
                self.assertEqual(srv2.token, "t" * 24)
            finally:
                srv2.stop()
                st2.close()

    def test_non_loopback_host_rejected(self):
        with self.assertRaises(ValueError):
            BoardHttpServer(self.store, host="0.0.0.0")
        with self.assertRaises(ValueError):
            BoardHttpServer(self.store, host="192.168.1.5")
        for bad in ("evil.example.com", f"evil.example.com:{self.server.port}", "10.0.0.5:4100"):
            c = self.client(host_header=bad)
            status, doc, _ = c.get("/me")
            self.assertEqual(status, 403, bad)
            self.assertEqual(doc["error"]["code"], "forbidden")
            c2 = self.client(token=None, host_header=bad)
            self.assertEqual(c2.get("/")[0], 403, "discovery is guarded too")
        ok = self.client(host_header=f"localhost:{self.server.port}")
        self.assertEqual(ok.get("/me")[0], 200)
        self.assertIn(self.server.host, ("127.0.0.1",))

    def test_profile_header_parsing_and_actor_id(self):
        self.assertEqual(parse_profile_header(None), (None, None))
        self.assertEqual(parse_profile_header("Claude"), ("claude", None))
        self.assertEqual(parse_profile_header('fixer;avatar="https://example.com/a.png"'),
                         ("fixer", "https://example.com/a.png"))
        self.assertEqual(parse_profile_header('"quoted"'), ("quoted", None))
        for bad in ("9lives", "has space", "a" * 40, 'x;avatar="http://example.com/a.png"', "x;avatar=https://e.com"):
            with self.assertRaises(VBoardError, msg=bad) as cm:
                parse_profile_header(bad)
            self.assertEqual(cm.exception.code, "invalid_profile")
        c = self.client(profile='Fixer;avatar="https://example.com/a.png"')
        status, me, _ = c.get("/me")
        self.assertEqual(status, 200)
        self.assertEqual((me["actor"], me["profile"]), ("local:user:alice+fixer", "fixer"))
        status, doc, _ = self.client(profile="9bad").get("/me")
        self.assertEqual((status, doc["error"]["code"]), (400, "invalid_profile"))
        # no profile -> the bare principal
        status, me, _ = self.client(profile=None).get("/me")
        self.assertEqual(me["actor"], "local:user:alice")
        # a profile on a message shows up as the actor and the author handle
        status, doc, _ = c.post("/messages", {"channel": "dev", "text": "hi"})
        self.assertEqual(status, 201)
        self.assertEqual(doc["message"]["by"]["actor"], "local:user:alice+fixer")
        self.assertEqual(doc["message"]["author"]["handle"], "alice/fixer")

    def test_can_header_narrows(self):
        self.assertIsNone(parse_can_header(None))
        self.assertEqual(parse_can_header("dev/=read,post;ci/=read"),
                         [("dev/", frozenset({"read", "post"})), ("ci/", frozenset({"read"}))])
        with self.assertRaises(VBoardError):
            parse_can_header("dev=nuke")
        status, _doc, _ = self.c.post("/messages", {"channel": "dev/a", "text": "ok"})
        self.assertEqual(status, 201)
        h = {"x-board-can": "dev/=read,post;ci/=read"}
        status, doc, _ = self.c.post("/messages", {"channel": "dev/a", "text": "ok2"}, headers=h)
        self.assertEqual(status, 201)
        status, doc, _ = self.c.post("/messages", {"channel": "ci/a", "text": "no"}, headers=h)
        self.assertEqual((status, doc["error"]["code"]), (403, "forbidden"))
        status, doc, _ = self.c.get("/messages?channel=ci/a", headers=h)
        self.assertNotEqual(status, 403, "ci/ is readable")
        status, me, _ = self.c.get("/me", headers={"x-board-can": "read"})
        self.assertEqual(me["can"], ["read"])


class TestRoutes(Base):
    def test_claim_next_retry_recovers_the_same_claim_on_a_new_connection(self) -> None:
        channel = "work/claim-retry"
        _, first, _ = self.c.post("/tasks", {"channel": channel, "text": "first", "labels": {"phase": "ready"}})
        _, second, _ = self.c.post("/tasks", {"channel": channel, "text": "second", "labels": {"phase": "ready"}})
        request = {"channel": channel, "label": {"phase": "ready"}, "lease": 60, "token": "retry-proof-for-claim-next"}
        status, original, _ = self.c.post("/tasks/claim", request)
        self.assertEqual((status, original["task"]["id"]), (200, first["task"]["id"]))
        self.c.post(f"/messages/{first['task']['id']}/labels", {"set": {"phase": "processing"}})
        self.c.close()

        retry = self.client(profile="claude")
        status, replayed, _ = retry.post("/tasks/claim", request)
        self.assertEqual((status, replayed["task"]["id"], replayed["claim"]),
                         (200, first["task"]["id"], original["claim"]))
        _, other_channel, _ = retry.post("/tasks/claim", {**request, "channel": "work/elsewhere"})
        self.assertEqual(other_channel, {"task": None})
        _, other_label, _ = retry.post("/tasks/claim", {**request, "label": {"phase": "processing"}})
        self.assertEqual(other_label, {"task": None})

        other_profile = self.client(profile="codex")
        status, next_claim, _ = other_profile.post("/tasks/claim", request)
        self.assertEqual((status, next_claim["task"]["id"]), (200, second["task"]["id"]))

    def test_post_and_thread_roundtrip_http(self):
        status, doc, _ = self.c.post("/messages", {"channel": "dev", "text": "hello @nobody", "labels": {"k": "v"},
                                                   "id": "client-1"})
        self.assertEqual(status, 201)
        mid = doc["message"]["id"]
        self.assertEqual(doc["message"]["labels"], {"k": "v"})
        # idempotency lookup
        status, posted, _ = self.c.get("/posts/client-1")
        self.assertEqual((status, posted["message"]["id"]), (200, mid))
        status, doc, _ = self.c.get("/posts/nope")
        self.assertEqual((status, doc["error"]["code"]), (404, "post_not_found"))
        status, reply, _ = self.c.post("/messages", {"re": mid, "text": "a reply"})
        self.assertEqual(status, 201)
        status, th, _ = self.c.get(f"/messages/{mid}")
        self.assertEqual(status, 200)
        self.assertEqual([r["text"] for r in th["replies"]], ["a reply"])
        self.assertEqual(th["message"]["text"], "hello @nobody")
        status, msgs, _ = self.c.get("/messages?channel=dev")
        self.assertEqual(status, 200)
        self.assertEqual(len(msgs["messages"]), 2)
        status, threads, _ = self.c.get("/threads?channel=dev&limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(threads["threads"][0]["replies"], 1)
        status, chans, _ = self.c.get("/channels")
        self.assertEqual(status, 200)
        self.assertIn("dev", [c["path"] for c in chans["channels"]])
        status, created, _ = self.c.post("/channels", {"path": "ops/new"})
        self.assertEqual((status, created["created"]), (201, True))
        status, again, _ = self.c.post("/channels", {"path": "ops/new"})
        self.assertEqual((status, again["created"]), (200, False))
        status, actors, _ = self.c.get("/actors")
        self.assertEqual(status, 200)
        self.assertTrue(actors["actors"])
        # edit / react / label / delete
        status, doc, _ = self.c.post(f"/messages/{mid}/edit", {"text": "edited"})
        self.assertEqual((status, doc["ok"], doc["message"]["text"]), (200, True, "edited"))
        status, doc, _ = self.c.post(f"/messages/{mid}/reactions", {"emoji": "eyes"})
        self.assertEqual(status, 200)
        status, doc, _ = self.c.post(f"/messages/{mid}/labels", {"set": {"a": "b"}, "unset": ["k"]})
        self.assertEqual((status, doc["message"]["labels"]), (200, {"a": "b"}))
        status, doc, _ = self.c.post(f"/messages/{mid}/delete", {})
        self.assertEqual((status, doc["message"]["deleted"]), (200, True))

    def test_task_claim_complete_over_http_includes_token_on_wire(self):
        status, doc, _ = self.c.post("/tasks", {"channel": "work/x", "text": "do it", "id": "t-1"})
        self.assertEqual(status, 201)
        tid = doc["task"]["id"]
        self.assertEqual(doc["task"]["task"]["state"], "ready")
        self.assertNotIn("outcome", doc)
        status, doc, _ = self.c.post(f"/tasks/{tid}/claim", {"lease": 60, "token": "tok-" + "a" * 20})
        self.assertEqual(status, 200)
        claim = doc["claim"]
        self.assertEqual(set(claim), {"epoch", "token", "until"})
        self.assertEqual(claim["token"], "tok-" + "a" * 20)
        self.assertEqual(doc["task"]["task"]["state"], "claimed")
        # retried claim with the same token is success
        status, again, _ = self.c.post(f"/tasks/{tid}/claim", {"lease": 60, "token": claim["token"]})
        self.assertEqual((status, again["claim"]["epoch"]), (200, claim["epoch"]))
        # a different caller is refused
        other = self.client(profile="codex")
        status, err, _ = other.post(f"/tasks/{tid}/claim", {})
        self.assertEqual((status, err["error"]["code"]), (409, "task_claimed"))
        status, doc, _ = self.c.post(f"/tasks/{tid}/renew", {"epoch": claim["epoch"], "token": claim["token"],
                                                             "lease": 120})
        self.assertEqual(status, 200)
        self.assertIn("until", doc)
        self.assertNotIn("outcome", doc)
        status, err, _ = self.c.post(f"/tasks/{tid}/complete", {"epoch": claim["epoch"]})
        self.assertEqual((status, err["error"]["code"]), (400, "invalid_request"))
        status, doc, _ = self.c.post(f"/tasks/{tid}/complete", {"epoch": claim["epoch"], "token": claim["token"],
                                                                "result": "done"})
        self.assertEqual(status, 200)
        self.assertEqual((doc["task"]["task"]["state"], doc["task"]["task"]["result"]), ("completed", "done"))
        self.assertNotIn("outcome", doc)
        # the same complete replayed is idempotent
        status, doc, _ = self.c.post(f"/tasks/{tid}/complete", {"epoch": claim["epoch"], "token": claim["token"],
                                                                "result": "done"})
        self.assertEqual((status, doc["task"]["task"]["state"]), (200, "completed"))
        # pool claim, fail, block/unblock, needs, assign, cancel, batch
        status, t2, _ = self.c.post("/tasks", {"channel": "work/x", "text": "second"})
        status, got, _ = self.c.post("/tasks/claim", {"channel": "work/", "lease": 60})
        self.assertEqual((status, got["task"]["id"]), (200, t2["task"]["id"]))
        status, doc, _ = self.c.post(f"/tasks/{got['task']['id']}/fail", {"epoch": got["claim"]["epoch"],
                                                                         "token": got["claim"]["token"],
                                                                         "reason": "broke"})
        self.assertEqual((status, doc["task"]["task"]["state"]), (200, "failed"))
        status, empty, _ = self.c.post("/tasks/claim", {"channel": "work/x"})
        self.assertEqual((status, empty), (200, {"task": None}))
        status, b, _ = self.c.post("/tasks/batch", {"channel": "work/b", "id": "batch-1",
                                                    "tasks": [{"key": "a", "text": "A"},
                                                              {"key": "b", "text": "B", "needs": [{"key": "a"}]}]})
        self.assertEqual(status, 201)
        self.assertEqual(len(b["tasks"]), 2)
        status, b2, _ = self.c.get("/tasks/batch/batch-1")
        self.assertEqual((status, len(b2["tasks"])), (200, 2))
        status, lst, _ = self.c.get("/tasks?channel=work/b&state=open")
        self.assertEqual((status, len(lst["tasks"])), (200, 2))
        first = b["tasks"][0]["id"] if "id" in b["tasks"][0] else b["tasks"][0]["message"]["id"]
        status, doc, _ = self.c.post(f"/tasks/{first}/block", {"reason": "waiting on human"})
        self.assertEqual((status, doc["task"]["task"]["state"]), (200, "blocked"))
        status, doc, _ = self.c.post(f"/tasks/{first}/unblock", {})
        self.assertEqual((status, doc["task"]["task"]["state"]), (200, "ready"))
        status, doc, _ = self.c.post(f"/tasks/{first}/cancel", {"reason": "nope"})
        self.assertEqual((status, doc["task"]["task"]["state"]), (200, "canceled"))
        status, doc, _ = self.c.post(f"/tasks/{got['task']['id']}/reopen", {})      # the failed one
        self.assertEqual((status, doc["task"]["task"]["state"]), (200, "ready"))
        status, doc, _ = self.c.post(f"/tasks/{got['task']['id']}/assign", {"for": "local:user:alice+codex"})
        self.assertEqual(status, 200)
        status, doc, _ = self.c.post(f"/tasks/{got['task']['id']}/needs", {"needs": []})
        self.assertEqual(status, 200)

    def test_error_envelope_and_status_codes(self):
        status, doc, _ = self.c.get("/messages/999999")
        self.assertEqual((status, doc["error"]["code"]), (404, "message_not_found"))
        self.assertEqual(set(doc), {"error"})
        self.assertIn("message", doc["error"])
        status, mid_doc, _ = self.c.post("/tasks", {"channel": "work/e", "text": "t"})
        tid = mid_doc["task"]["id"]
        status, claim, _ = self.c.post(f"/tasks/{tid}/claim", {})
        status, doc, _ = self.client(profile="other").post(f"/tasks/{tid}/claim", {})
        self.assertEqual((status, doc["error"]["code"]), (409, "task_claimed"))
        status, doc, _ = self.c.post("/messages", {"channel": "dev", "text": "x", "labels": {"board.x": "1"}})
        self.assertEqual((status, doc["error"]["code"]), (400, "reserved_label"))
        for path in ("/grants", "/schemas", "/schedules", "/attachments", "/watchers", "/sync/x", "/inbox/k",
                     "/sources/k/x", "/webhooks/k", "/wake", "/links?url=x"):
            status, doc, _ = self.c.get(path)
            self.assertEqual((status, doc["error"]["code"]), (501, "not_supported"), path)
        status, doc, _ = self.c.post("/grants", {})
        self.assertEqual((status, doc["error"]["code"]), (501, "not_supported"))
        status, doc, _ = self.c.post(f"/messages/{tid}/typing", {})
        self.assertEqual((status, doc["error"]["code"]), (501, "not_supported"))
        status, doc, _ = self.c.get("/nothing-here")
        self.assertEqual((status, doc["error"]["code"]), (404, "not_found"))
        status, doc, _ = self.c.request("PATCH", "/grants/1", {})
        self.assertEqual(status, 501)
        status, doc, _ = self.c.request("DELETE", "/messages/1", {})
        self.assertEqual((status, doc["error"]["code"]), (404, "not_found"))
        # malformed bodies
        self.c.conn.request("POST", self.c.base + "/messages", body=b"{not json", headers=self.c.headers())
        resp = self.c.conn.getresponse()
        body = json.loads(resp.read())
        self.assertEqual((resp.status, body["error"]["code"]), (400, "invalid_request"))
        self.c.conn.request("POST", self.c.base + "/messages", body=b"[1,2]", headers=self.c.headers())
        resp = self.c.conn.getresponse()
        self.assertEqual(resp.status, 400)
        resp.read()
        status, doc, _ = self.c.post("/messages", {"channel": "dev", "text": 5})
        self.assertEqual((status, doc["error"]["code"]), (400, "invalid_request"))
        # connection still usable after errors (keep-alive framing intact)
        self.assertEqual(self.c.get("/me")[0], 200)

    def test_body_limit_and_unauthorized_post_keeps_framing(self):
        c = self.client(token=None)
        status, doc, _ = c.post("/messages", {"channel": "dev", "text": "x"})
        self.assertEqual((status, doc["error"]["code"]), (401, "unauthorized"))
        # a fresh connection works afterwards
        self.assertEqual(self.client().get("/me")[0], 200)
        conn = http.client.HTTPConnection(self.c.host, self.c.port, timeout=5)
        conn.putrequest("POST", self.c.base + "/messages")
        for k, v in self.c.headers().items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(8 * 1024 * 1024 + 1))
        conn.endheaders()
        resp = conn.getresponse()
        body = json.loads(resp.read())
        self.assertEqual((resp.status, body["error"]["code"]), (413, "too_large"))
        conn.close()

    def test_idle_keepalive_connection_is_closed(self):
        self.server.stop()
        self.setUp_idle()

    def setUp_idle(self) -> None:
        srv = BoardHttpServer(self.store, user="alice", idle_timeout_s=0.3).start()
        self.addCleanup(srv.stop)
        c = Client(srv)
        self.addCleanup(c.close)
        self.assertEqual(c.get("/me")[0], 200)
        time.sleep(0.8)
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
            c.get("/me")
        c2 = Client(srv)
        self.addCleanup(c2.close)
        self.assertEqual(c2.get("/me")[0], 200)


def _read_lines(resp, want: int, timeout: float = 5.0) -> list[str]:
    """Read `want` non-empty-or-blank raw lines (with newlines) from an SSE response."""
    lines: list[str] = []
    end = time.monotonic() + timeout
    while len(lines) < want and time.monotonic() < end:
        line = resp.fp.readline()
        if not line:
            break
        lines.append(line.decode("utf-8"))
    return lines


def _read_until(resp, predicate, timeout: float = 5.0) -> str:
    buf = ""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        line = resp.fp.readline()
        if not line:
            break
        buf += line.decode("utf-8")
        if predicate(buf):
            return buf
    return buf


class TestEvents(Base):
    def setUp(self) -> None:
        super().setUp()
        self.c.post("/channels", {"path": "dev"})
        self.c.post("/channels", {"path": "other"})

    def test_events_long_poll_wakes(self):
        status, head, _ = self.c.get("/events")
        self.assertEqual((status, head["events"]), (200, []))
        cursor = head["cursor"]

        def later():
            time.sleep(0.15)
            self.client(profile="poster").post("/messages", {"channel": "dev", "text": "wake"})

        t = threading.Thread(target=later)
        t.start()
        started = time.monotonic()
        status, doc, _ = self.c.get(f"/events?after={cursor}&wait=5")
        elapsed = time.monotonic() - started
        t.join(5)
        self.assertEqual(status, 200)
        self.assertEqual([e["type"] for e in doc["events"]], ["message.posted"])
        self.assertLess(elapsed, 0.55, "woken by the write, not by the hold timeout")
        # nothing new: the hold is capped at hold_s (0.6) even though wait=60 was asked
        started = time.monotonic()
        status, doc, _ = self.c.get(f"/events?after={doc['cursor']}&wait=60")
        self.assertEqual((status, doc["events"]), (200, []))
        self.assertLess(time.monotonic() - started, 3.0)
        # last=N with filters
        status, last, _ = self.c.get("/events?last=5")
        self.assertEqual(last["events"][-1]["type"], "message.posted")
        status, err, _ = self.c.get("/events?last=5&wait=1")
        self.assertEqual((status, err["error"]["code"]), (400, "invalid_request"))
        status, err, _ = self.c.get("/events?after=abc")
        self.assertEqual(status, 400)

    def test_sse_frames_and_bare_id_frames(self):
        c = self.client()
        c.conn.request("GET", c.base + "/events?channel=dev", headers=c.headers({"Accept": "text/event-stream"}))
        resp = c.conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        self.assertIn("no-cache", resp.getheader("Cache-Control"))
        self.assertEqual(_read_lines(resp, 2), [": connected\n", "\n"])
        poster = self.client(profile="poster")
        poster.post("/messages", {"channel": "dev", "text": "visible"})
        buf = _read_until(resp, lambda b: "event: board" in b and b.endswith("\n\n"))
        lines = [ln for ln in buf.split("\n") if ln]
        ids = [ln for ln in lines if ln.startswith("id: ")]
        self.assertTrue(ids, buf)
        self.assertIn("event: board", lines)
        data = json.loads(next(ln for ln in lines if ln.startswith("data: "))[len("data: "):])
        self.assertEqual(data["type"], "message.posted")
        self.assertEqual(ids[0], f"id: {data['cursor']}")
        # an event the filter drops advances the cursor with a bare id frame (no event/data lines)
        poster.post("/messages", {"channel": "other", "text": "filtered out"})
        buf = _read_until(resp, lambda b: b.endswith("\n\n") and "id: " in b and "data: " not in b)
        self.assertIn("id: ", buf)
        self.assertNotIn("data: ", buf)
        self.assertNotIn("event: ", buf)
        # idle -> ping comments
        buf = _read_until(resp, lambda b: b.endswith(": ping\n\n"), timeout=3.0)
        self.assertIn(": ping\n\n", buf)

    def test_sse_closes_after_max_and_honours_last_event_id(self):
        self.server.stop()
        srv = BoardHttpServer(self.store, user="alice", ping_s=0.1, stream_max_s=0.8, hold_s=0.3).start()
        self.addCleanup(srv.stop)
        c = Client(srv, profile="p")
        self.addCleanup(c.close)
        c.post("/channels", {"path": "dev"})
        _, base, _ = c.get("/events")
        ids = []
        for i in range(3):
            _, doc, _ = c.post("/messages", {"channel": "dev", "text": f"m{i}"})
            ids.append(doc["message"]["id"])
        _, page, _ = c.get(f"/events?after={base['cursor']}")
        self.assertEqual(len(page["events"]), 3)
        first = page["events"][0]["cursor"]
        s = Client(srv)
        self.addCleanup(s.close)
        started = time.monotonic()
        s.conn.request("GET", s.base + "/events", headers=s.headers({"Accept": "text/event-stream",
                                                                    "Last-Event-ID": first}))
        resp = s.conn.getresponse()
        body = resp.read().decode("utf-8")        # reads until the server closes (stream_max_s)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.7)
        self.assertLess(elapsed, 4.0)
        frames = [ln for ln in body.split("\n") if ln.startswith("data: ")]
        self.assertEqual(len(frames), 2, "events after Last-Event-ID only")
        self.assertTrue(body.startswith(": connected\n\n"))
        # bad cursor -> a JSON error, not a stream
        s2 = Client(srv)
        self.addCleanup(s2.close)
        s2.conn.request("GET", s2.base + "/events?after=zzz", headers=s2.headers({"Accept": "text/event-stream"}))
        r2 = s2.conn.getresponse()
        self.assertEqual(r2.status, 400)
        self.assertEqual(json.loads(r2.read())["error"]["code"], "invalid_cursor")


class TestConcurrency(Base):
    def test_concurrent_mixed_ops_through_one_server(self):
        self.server.stop()
        srv = BoardHttpServer(self.store, user="alice", hold_s=0.2).start()
        self.addCleanup(srv.stop)
        errors: list[str] = []
        done = {"claimed": 0, "posted": 0}
        lock = threading.Lock()
        seed = Client(srv, profile="seed")
        seed.post("/channels", {"path": "dev"})
        for i in range(40):
            seed.post("/tasks", {"channel": "work/c", "text": f"job {i}"})
        seed.close()

        def worker(n: int) -> None:
            c = Client(srv, profile=f"w{n}")
            try:
                for i in range(12):
                    kind = (n + i) % 4
                    if kind == 0:
                        st, doc, _ = c.post("/messages", {"channel": "dev", "text": f"w{n} m{i}"})
                        ok = st == 201
                        with lock:
                            done["posted"] += ok
                    elif kind == 1:
                        st, doc, _ = c.get("/messages?channel=dev&limit=20")
                        ok = st == 200
                    elif kind == 2:
                        st, doc, _ = c.post("/tasks/claim", {"channel": "work/c", "lease": 60})
                        ok = st == 200
                        if ok and doc.get("task"):
                            cl = doc["claim"]
                            st2, d2, _ = c.post(f"/tasks/{doc['task']['id']}/complete",
                                                {"epoch": cl["epoch"], "token": cl["token"], "result": "ok"})
                            ok = st2 == 200
                            with lock:
                                done["claimed"] += ok
                    else:
                        st, doc, _ = c.get("/events?after=0&wait=0.05")
                        ok = st == 200
                    if not ok:
                        with lock:
                            errors.append(f"w{n} op{kind}: {st} {doc}")
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"w{n}: {type(exc).__name__}: {exc}")
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertFalse([t for t in threads if t.is_alive()], "workers finished")
        self.assertEqual(errors, [])
        self.assertGreater(done["claimed"], 0)
        self.assertGreater(done["posted"], 0)


class TestCliInterop(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FUSION_VBOARD_PARITY") == "1"
                         and (shutil.which("vb") or shutil.which("vercel")),
                         "set FUSION_VBOARD_PARITY=1 with vb or vercel on PATH (loopback only)")
    def test_stock_cli_parity_optin(self):
        with tempfile.TemporaryDirectory() as d:
            store = BoardStore(os.path.join(d, "p.db"))
            srv = serve(store, user="parity")
            try:
                cfg = os.path.join(d, "cfg")
                srv.write_token_file(cfg)
                assert urlsplit(srv.url).hostname in ("127.0.0.1", "localhost", "::1")   # never a hosted board
                exe = shutil.which("vb")
                argv = [exe] if exe else [shutil.which("vercel"), "board"]
                env = dict(os.environ, XDG_CONFIG_HOME=cfg, BOARD_URL=srv.url)
                env.pop("BOARD_TOKEN", None)
                out = subprocess.run(argv + ["--url", srv.url, "--json", "--as", "parity", "post", "work/parity", "hi"],
                                     capture_output=True, text=True, timeout=60, env=env)
                self.assertEqual(out.returncode, 0, out.stderr[-500:])
                doc = json.loads(out.stdout)
                self.assertIn("message", doc)
                self.assertEqual(doc["message"]["text"], "hi")
            finally:
                srv.stop()
                store.close()


def _cli_importable() -> bool:
    try:
        return importlib.util.find_spec("fusion_swarm.vboard.cli") is not None
    except (ImportError, ValueError):
        return False


class TestServeVerb(unittest.TestCase):
    @unittest.skipUnless(_cli_importable() and os.name == "posix", "vboard.cli not importable yet")
    def test_serve_verb_smoke(self):
        """`vboard serve --port 0` prints {url, token_file}, serves discovery, and exits on SIGINT.

        Run as a subprocess because `serve` blocks until SIGINT (signal handlers need a main thread)."""
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, PYTHONPATH=str(ROOT / "python"), XDG_CONFIG_HOME=os.path.join(d, "cfg"),
                       FUSION_HOME=os.path.join(d, "home"), PYTHONDONTWRITEBYTECODE="1")
            for k in ("BOARD_URL", "BOARD_TOKEN", "BOARD_PROFILE"):
                env.pop(k, None)
            proc = subprocess.Popen([sys.executable, "-m", "fusion_swarm.vboard", "--db", os.path.join(d, "s.db"),
                                     "serve", "--port", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, text=True)
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 15)
                self.assertTrue(ready, "serve printed nothing")
                info = json.loads(proc.stdout.readline())
                self.assertIn("url", info)
                self.assertTrue(info["url"].startswith("http://127.0.0.1:"))
                parts = urlsplit(info["url"])
                conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
                conn.request("GET", parts.path.rstrip("/") + "/")
                resp = conn.getresponse()
                doc = json.loads(resp.read())
                self.assertEqual(doc["audience"], doc["url"])
                conn.close()
                if info.get("token_file"):
                    self.assertEqual(stat.S_IMODE(os.stat(info["token_file"]).st_mode), 0o600)
            finally:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                for stream in (proc.stdout, proc.stderr):
                    if stream:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
