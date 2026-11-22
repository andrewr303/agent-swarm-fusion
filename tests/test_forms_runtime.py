"""Tests for the shared forms runtime (spec section 8, judge J-14/J-16).

Deterministic by construction: clocks are injected, concurrency uses Event handshakes, and the only real-time waits are
bounded polls (<= 5 s) for OS-level facts (a process disappearing).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "python"))

import fusion_swarm.forms as forms  # noqa: E402
from fusion_swarm.forms import capacity, journal as journal_mod, runtime  # noqa: E402
from fusion_swarm.forms.ledger import ReliabilityLedger  # noqa: E402
from fusion_swarm.forms.runtime import (  # noqa: E402
    Budget, CallSpec, CancelToken, FormContext, FormError, FormSpec, Verdict, command_verifier, json_object,
    kill_group,
)

POSIX = os.name == "posix"


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:  # a zombie still answers kill(pid, 0); treat it as gone
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith("Z") or not out.strip()


def _wait_gone(pid: int, limit_s: float = 5.0) -> bool:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if _pid_gone(pid):
            return True
        time.sleep(0.02)
    return _pid_gone(pid)


GRANDCHILD_LEADER = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(60)\n"
)
GRANDCHILD_ORPHANER = (
    "import subprocess, sys\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(g.pid, flush=True)\n"
)


def _spawn(code: str) -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            start_new_session=True)
    gpid = int(proc.stdout.readline().strip())
    return proc, gpid


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _ctx(tmp: str, **kw) -> FormContext:
    kw.setdefault("ledger", ReliabilityLedger(Path(tmp) / "ledger.json"))
    kw.setdefault("run_dir", Path(tmp) / "run")
    return FormContext("unit", run_id="unit-run", **kw)


def _ok_dispatch(provider, prompt, *, role="", cwd=None, timeout=300, cancel=None, **_):
    return {"ok": True, "output": f"{provider}:{prompt}", "status": "returned", "provider": provider, "role": role,
            "rc": 0, "elapsed_s": 0.0, "usage": {"input_tokens": 3, "output_tokens": 4}}


class BudgetTests(unittest.TestCase):
    def test_budget_calls_tokens_deadline(self):
        b = Budget(max_calls=2)
        self.assertEqual([b.try_reserve_call() for _ in range(3)], [True, True, False])
        self.assertEqual(b.exhausted(), "calls")
        self.assertEqual(b.remaining()["calls"], 0)

        t = Budget(max_tokens=10)
        self.assertTrue(t.try_reserve_call())
        t.charge_tokens(10)
        self.assertEqual(t.exhausted(), "tokens")
        self.assertFalse(t.try_reserve_call())  # judge J-16b: tokens gate new calls too
        t.charge_tokens(-5)
        t.charge_tokens("junk")  # never raises
        self.assertEqual(t.tokens_used, 10)

        clock = FakeClock()
        d = Budget(deadline_s=5, monotonic=clock).start()
        self.assertEqual(d.remaining()["seconds"], 5)
        clock.advance(4.9)
        self.assertIsNone(d.exhausted())
        self.assertTrue(d.try_reserve_call())
        clock.advance(0.1)
        self.assertEqual(d.exhausted(), "deadline")
        self.assertFalse(d.try_reserve_call())
        self.assertEqual(d.remaining(), {"calls": None, "tokens": None, "seconds": 0.0})
        self.assertEqual(d.to_dict()["exhausted"], "deadline")
        self.assertIs(d.start(), d)  # idempotent: t0 not reset
        self.assertEqual(d.exhausted(), "deadline")

    def test_budget_reserve_is_thread_safe(self):
        b = Budget(max_calls=50)
        got = []

        def worker():
            for _ in range(20):
                got.append(b.try_reserve_call())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(sum(got), 50)


@unittest.skipUnless(POSIX, "process groups are POSIX only")
class CancelTests(unittest.TestCase):
    def test_cancel_token_kills_process_group(self):
        proc, gpid = _spawn(GRANDCHILD_LEADER)
        token = CancelToken()
        token.register(proc)
        self.assertFalse(_pid_gone(gpid))
        token.cancel("test")
        self.assertTrue(token.is_set())
        self.assertEqual(token.reason, "test")
        proc.wait(timeout=5)  # leader reaped; the grandchild must be dead regardless (J-14)
        self.assertTrue(_wait_gone(gpid), "grandchild survived the group kill")
        proc.stdout.close()

    def test_kill_group_finishes_with_sigkill_after_leader_exited(self):
        proc, gpid = _spawn(GRANDCHILD_ORPHANER)
        proc.wait(timeout=5)  # leader already gone, grandchild keeps the group alive
        self.assertFalse(_pid_gone(gpid))
        kill_group(proc)
        self.assertTrue(_wait_gone(gpid), "killpg(SIGKILL) must run even when the leader already exited")
        proc.stdout.close()

    def test_kill_group_local_fallback_without_process_runtime(self):
        proc, gpid = _spawn(GRANDCHILD_LEADER)
        with mock.patch.dict(sys.modules, {"fusion_swarm.process_runtime": None}):
            kill_group(proc)
        self.assertTrue(_wait_gone(gpid))
        self.assertIsNotNone(proc.poll())
        proc.stdout.close()

    def test_cancel_child_propagation(self):
        parent = CancelToken()
        a, b = parent.child(), parent.child()
        grand = a.child()
        a.cancel("only-a")
        self.assertTrue(a.is_set() and grand.is_set())
        self.assertFalse(parent.is_set() or b.is_set())
        parent.cancel("all")
        self.assertTrue(b.is_set())
        self.assertEqual(b.reason, "all")
        late = parent.child()  # child of an already-cancelled parent is born cancelled
        self.assertTrue(late.is_set())
        self.assertEqual(late.reason, "all")
        self.assertTrue(late.wait(0))
        parent.cancel("again")  # idempotent
        self.assertEqual(parent.reason, "all")

    def test_register_after_cancel_kills_immediately(self):
        token = CancelToken()
        token.cancel()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        unregister = token.register(proc)
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.returncode)
        unregister()  # no-op, must not raise

    def test_unregister_removes_process(self):
        token = CancelToken()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            token.register(proc)()
            token.cancel()
            self.assertIsNone(proc.poll())  # unregistered: cancel must not touch it
        finally:
            kill_group(proc)


class ParallelTests(unittest.TestCase):
    def test_parallel_first_k_cancels_losers_without_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}
            started = threading.Barrier(3)

            def dispatch(provider, prompt, *, role="", cwd=None, timeout=300, cancel=None):
                started.wait(timeout=10)
                if provider == "fast":
                    return {"ok": True, "output": "done", "status": "returned"}
                seen[provider] = cancel.wait(30)  # returns True only if the loser was cancelled
                return {"ok": False, "output": "", "status": "cancelled"}

            ctx = _ctx(tmp, dispatch_fn=dispatch)
            specs = [CallSpec(k, p, "x") for k, p in (("a", "fast"), ("b", "slow1"), ("c", "slow2"))]
            results = ctx.parallel(specs, first_k=1)
            self.assertEqual([r.status for r in results], ["returned", "cancelled", "cancelled"])
            self.assertTrue(results[0].ok)
            self.assertFalse(any(r.ok for r in results[1:]))
            self.assertEqual(seen, {"slow1": True, "slow2": True})
            kinds = [r["kind"] for r in journal_mod.read_records(ctx.journal.path)]
            self.assertEqual(kinds.count("cancel"), 2)
            ctx.finish("ok", winner=None, candidates=[], rounds=1, stop_reason="first_k")

    def test_parallel_does_not_wait_for_a_loser_that_ignores_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = threading.Event()
            entered = threading.Event()

            def dispatch(provider, prompt, *, role="", cwd=None, timeout=300, cancel=None):
                if provider == "stubborn":
                    entered.set()
                    release.wait(30)
                    return {"ok": True, "output": "late", "status": "returned"}
                entered.wait(10)
                return {"ok": True, "output": "win", "status": "returned"}

            ctx = _ctx(tmp, dispatch_fn=dispatch)
            try:
                results = ctx.parallel([CallSpec("w", "winner", "x"), CallSpec("s", "stubborn", "x")], first_k=1,
                                       loser_grace_s=0.05)
                self.assertFalse(release.is_set())  # we returned while the loser was still blocked
                self.assertEqual([r.status for r in results], ["returned", "cancelled"])
            finally:
                release.set()
            ctx.finish("ok", winner={"id": "w"}, candidates=[], rounds=1, stop_reason="first_k")
            time.sleep(0.1)  # let the late loser thread try to journal
            kinds = [r["kind"] for r in journal_mod.read_records(ctx.journal.path)]
            self.assertEqual(kinds[-1], "run.end")  # no call.* after run.end (J-16a)

    def test_parallel_all_results_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch, budget=Budget(max_calls=2))
            res = ctx.parallel([CallSpec(str(i), "p", "q") for i in range(3)])
            self.assertEqual(sorted(r.status for r in res), ["budget", "returned", "returned"])
            ctx.finish("budget_exhausted", winner=None, candidates=[], rounds=0, stop_reason="budget")


class ContextTests(unittest.TestCase):
    def test_call_journals_charges_tokens_and_writes_safe_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}

            def dispatch(provider, prompt, *, role="", cwd=None, timeout=300, cancel=None):
                seen.update(provider=provider, role=role, cwd=cwd, timeout=timeout, cancel=cancel)
                return {"ok": True, "output": "hello", "status": "returned", "model": "m1", "session_id": "s1",
                        "usage": {"input_tokens": 3, "output_tokens": 4, "cost_usd": 0.5}}

            ctx = _ctx(tmp, dispatch_fn=dispatch, budget=Budget(max_tokens=100))
            res = ctx.call(CallSpec("../../etc/evil key", "codex", "do it", role="worker", cwd="/x", timeout=12))
            self.assertTrue(res.ok)
            self.assertEqual((res.tokens, res.cost_usd, res.model, res.session_id), (7, 0.5, "m1", "s1"))
            self.assertIs(seen["cancel"], ctx.cancel)
            self.assertEqual(ctx.budget.tokens_used, 7)
            self.assertRegex(res.output_ref, r"^outputs/0001-[0-9a-f]{16}\.txt$")
            self.assertEqual((ctx.run_dir / res.output_ref).read_text(), "hello")
            for path in Path(tmp).rglob("*"):
                path.resolve().relative_to(Path(tmp).resolve())
            self.assertFalse((Path(tmp) / "etc").exists())
            recs = journal_mod.read_records(ctx.journal.path)
            self.assertEqual([r["kind"] for r in recs], ["run.start", "call.start", "call.end"])
            self.assertEqual(recs[2]["output_ref"], res.output_ref)
            self.assertEqual(recs[2]["key"], "../../etc/evil key")  # the key stays in the journal only
            ctx.finish("ok", winner=None, candidates=[], rounds=0, stop_reason="done")

    def test_call_key_cannot_escape_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch)
            before = {p for p in Path(tmp).iterdir()}
            for key in ("../../escape", "/abs/path", "a/../../b", "..\\..\\win", "x" * 600, "\x00nul"):
                res = ctx.call(CallSpec(key, "p", "q"))
                self.assertTrue(res.ok, key)
                target = (ctx.run_dir / res.output_ref).resolve()
                target.relative_to((ctx.run_dir / "outputs").resolve())
                self.assertLessEqual(len(Path(res.output_ref).name), 40)
            self.assertEqual({p for p in Path(tmp).iterdir()}, before)  # nothing written beside the run dir
            ctx.finish("ok", winner=None, candidates=[], rounds=0, stop_reason="done")

    def test_unsafe_run_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("../x", "a/b", ".hidden", "a b", "x" * 200):
                with self.assertRaises(FormError) as cm:
                    FormContext("unit", run_id=bad, run_dir=Path(tmp) / "r", ledger=ReliabilityLedger(Path(tmp) / "l"))
                self.assertEqual(cm.exception.code, "invalid_input")

    def test_call_budget_and_cancel_and_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom(provider, prompt, **kw):
                raise RuntimeError("provider exploded")

            ctx = _ctx(tmp, dispatch_fn=boom, budget=Budget(max_calls=1))
            res = ctx.call(CallSpec("a", "p", "q"))
            self.assertEqual((res.ok, res.status), (False, "error"))
            self.assertIn("provider exploded", res.output)
            over = ctx.call(CallSpec("b", "p", "q"))
            self.assertEqual(over.status, "budget")
            budget_rec = [r for r in journal_mod.read_records(ctx.journal.path) if r["kind"] == "budget"][0]
            self.assertEqual(budget_rec["budget_kind"], "calls")
            ctx.cancel.cancel("stop")
            self.assertEqual(ctx.call(CallSpec("c", "p", "q")).status, "cancelled")
            ctx.finish("cancelled", winner=None, candidates=[], rounds=0, stop_reason="cancelled")

    def test_call_clamps_timeout_to_remaining_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            got = {}

            def dispatch(provider, prompt, *, role="", cwd=None, timeout=300, cancel=None):
                got["timeout"] = timeout
                return {"ok": True, "output": "x", "status": "returned"}

            ctx = _ctx(tmp, dispatch_fn=dispatch, budget=Budget(deadline_s=10, monotonic=clock), monotonic=clock)
            clock.advance(7)
            ctx.call(CallSpec("a", "p", "q", timeout=300))
            self.assertEqual(got["timeout"], 3.0)
            ctx.finish("ok", winner=None, candidates=[], rounds=0, stop_reason="done")

    def test_dispatch_without_cancel_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            def legacy(provider, prompt, *, role="", cwd=None, timeout=300):
                return {"ok": True, "output": "legacy", "status": "returned"}

            ctx = _ctx(tmp, dispatch_fn=legacy)
            self.assertEqual(ctx.call(CallSpec("a", "p", "q")).output, "legacy")
            ctx.finish("ok", winner=None, candidates=[], rounds=0, stop_reason="done")

    def test_deadline_timer_cancels_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch, budget=Budget(deadline_s=0.05))
            self.assertTrue(ctx.cancel.wait(5))  # fires without any call in flight
            self.assertEqual(ctx.cancel.reason, "deadline")
            ctx.finish("cancelled", winner=None, candidates=[], rounds=0, stop_reason="deadline")

    def test_finish_cancels_timer_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch, budget=Budget(deadline_s=3600))
            first = ctx.finish("ok", winner={"id": "c1"}, candidates=[{"id": "c1"}], rounds=2, stop_reason="done", k=1)
            self.assertFalse(ctx._timer.is_alive() and not ctx._timer.finished.is_set())
            self.assertIs(ctx.finish("failed", winner=None, candidates=[], rounds=0, stop_reason="x"), first)
            self.assertEqual(first.extra, {"k": 1})
            self.assertEqual(first.to_dict()["status"], "ok")
            data = json.loads((ctx.run_dir / "result.json").read_text())
            self.assertEqual(data["run_id"], "unit-run")
            kinds = [r["kind"] for r in journal_mod.read_records(ctx.journal.path)]
            self.assertEqual(kinds.count("run.end"), 1)

    def test_verify_with_and_without_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch)
            v = ctx.verify({"id": "c1", "output": "x"})
            self.assertEqual((v.passed, v.kind), (False, "none"))
            ctx2 = _ctx(tmp + "/b" if False else tmp, dispatch_fn=_ok_dispatch,
                        verifier=lambda cand: Verdict(True, 1.0, "exec", "ok"), run_dir=Path(tmp) / "run2")
            v2 = ctx2.verify({"id": "c9"})
            self.assertTrue(v2.passed)
            rec = [r for r in journal_mod.read_records(ctx2.journal.path) if r["kind"] == "verdict"][0]
            self.assertEqual((rec["candidate_id"], rec["verdict_kind"], rec["passed"]), ("c9", "exec", True))
            crashing = _ctx(tmp, dispatch_fn=_ok_dispatch, verifier=lambda c: 1 / 0, run_dir=Path(tmp) / "run3")
            self.assertFalse(crashing.verify({"id": "z"}).passed)
            for c in (ctx, ctx2, crashing):
                c.finish("failed", winner=None, candidates=[], rounds=0, stop_reason="x")

    def test_default_dispatch_is_lazy_spawn_run_provider(self):
        fake = types.ModuleType("fusion_swarm.spawn")
        fake.run_provider = lambda *a, **k: {"ok": True}
        with mock.patch.dict(sys.modules, {"fusion_swarm.spawn": fake}):
            with mock.patch.object(sys.modules["fusion_swarm"], "spawn", fake, create=True):
                self.assertIs(runtime.default_dispatch(), fake.run_provider)
        self.assertTrue(runtime.new_run_id("my form").startswith("my-form-"))
        self.assertRegex(runtime.new_run_id("x"), r"^x-\d{8}T\d{6}-[0-9a-f]{6}$")


class JsonObjectTests(unittest.TestCase):
    def test_json_object(self):
        self.assertEqual(json_object('noise {"a": {"b": 1}, "c": "}"} tail {"z": 1}'), {"a": {"b": 1}, "c": "}"})
        self.assertEqual(json_object('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(json_object('{bad json} then {"good": 2}'), {"good": 2})
        self.assertIsNone(json_object("no braces"))
        self.assertIsNone(json_object('{"unterminated": 1'))
        self.assertIsNone(json_object("[1, 2]"))
        self.assertIsNone(json_object(None))  # type: ignore[arg-type]


@unittest.skipUnless(POSIX, "bash verifier is POSIX only")
class CommandVerifierTests(unittest.TestCase):
    def test_pass_fail_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "marker").write_text("x")
            ok = command_verifier("test -f marker", cwd=tmp)({"id": "c"})
            self.assertEqual((ok.passed, ok.score, ok.kind), (True, 1.0, "exec"))
            bad = command_verifier("echo boom; exit 3", cwd=tmp)({"id": "c"})
            self.assertEqual((bad.passed, bad.score, bad.kind, bad.detail["returncode"]), (False, 0.0, "exec", 3))
            self.assertIn("boom", bad.evidence)
            per_candidate = command_verifier("test -f marker")({"id": "c", "cwd": tmp})  # candidate cwd wins
            self.assertTrue(per_candidate.passed)
            argv = command_verifier([sys.executable, "-c", "import sys; sys.exit(0)"])({"id": "c"})
            self.assertTrue(argv.passed)
            missing = command_verifier(["/definitely/not/a/binary"])({"id": "c"})
            self.assertEqual((missing.passed, missing.kind), (False, "exec"))
            long = command_verifier("python3 -c \"print('x' * 9000)\"; exit 1")({"id": "c"})
            self.assertLessEqual(len(long.evidence), 4000)

    def test_timeout_is_min_of_timeout_and_budget_remaining(self):
        v = command_verifier("sleep 60", timeout=0.1)({"id": "c"})
        self.assertEqual((v.passed, v.detail.get("timed_out")), (False, True))
        verifier = command_verifier("sleep 60", timeout=600)
        verifier.bind(budget=Budget(deadline_s=0.15).start())
        v2 = verifier({"id": "c"})
        self.assertEqual((v2.passed, v2.detail.get("timed_out")), (False, True))
        done = Budget(deadline_s=0.0).start()
        verifier.bind(budget=done)
        self.assertTrue(verifier({"id": "c"}).detail.get("deadline"))

    def test_cancel_kills_the_verifier_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp, "pid")
            token = CancelToken()
            verifier = command_verifier(f"echo $$ > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}; sleep 60").bind(cancel=token)
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("v", verifier({"id": "c"})), daemon=True)
            t.start()
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            pid = int(pidfile.read_text())
            token.cancel("stop")
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
            self.assertFalse(out["v"].passed)
            self.assertTrue(out["v"].detail.get("cancelled"))
            self.assertTrue(_wait_gone(pid))
            # a verifier started after the cancel does not run at all
            self.assertTrue(verifier({"id": "c"}).detail.get("cancelled"))

    def test_context_binds_verifier_to_its_cancel_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            verifier = command_verifier("true")
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch, verifier=verifier)
            self.assertIs(verifier.cancel, ctx.cancel)
            self.assertIs(verifier.budget, ctx.budget)
            self.assertTrue(ctx.verify({"id": "c"}).passed)
            ctx.finish("ok", winner={"id": "c"}, candidates=[], rounds=1, stop_reason="done")


class JournalTests(unittest.TestCase):
    def test_journal_append_fsync_and_torn_line_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = journal_mod.Journal(tmp, run_id="r1", form="f")
            synced = []
            real_fsync = os.fsync
            with mock.patch.object(journal_mod.os, "fsync", side_effect=lambda fd: (synced.append(fd), real_fsync(fd))[1]):
                rec = j.append("run.start", params={"a": 1}, budget={})
                j.append("candidate", id="c1", provider="p", output_ref="outputs/x.txt", round=0)
            self.assertGreaterEqual(len(synced), 2)  # one fsync per record
            self.assertEqual((rec["seq"], rec["run_id"], rec["form"], rec["kind"]), (1, "r1", "f", "run.start"))
            self.assertRegex(rec["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
            raw = j.path.read_bytes()
            self.assertEqual(raw.count(b"\n"), 2)  # exactly one full line per record
            j.close()
            with open(j.path, "ab") as handle:  # simulate a crash mid-write
                handle.write(b'{"seq": 3, "kind": "call.end", "key": "tor')
            self.assertEqual([r["seq"] for r in journal_mod.read_records(j.path)], [1, 2])
            j2 = journal_mod.Journal(tmp, run_id="r1", form="f")  # resume continues the sequence, heals the tail
            self.assertEqual(j2.append("stop", reason="resumed")["seq"], 3)
            self.assertEqual([r["seq"] for r in journal_mod.read_records(j.path)], [1, 2, 3])
            with open(j.path, "ab") as handle:
                handle.write(b"\n\n   \nnot json\n")
            self.assertEqual(len(journal_mod.read_records(j.path)), 3)  # blank and garbage lines skipped
            j2.close()
            self.assertEqual(journal_mod.read_records(Path(tmp) / "missing.jsonl"), [])

    def test_journal_kind_field_does_not_clobber_record_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = journal_mod.Journal(tmp, run_id="r", form="f")
            v = j.append("verdict", candidate_id="c", passed=True, score=1.0, kind="exec", evidence="")
            b = j.append("budget", kind="tokens")
            self.assertEqual((v["kind"], v["verdict_kind"], b["kind"], b["budget_kind"]), ("verdict", "exec", "budget", "tokens"))
            j.close()

    def test_journal_concurrent_appends_keep_whole_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = journal_mod.Journal(tmp, run_id="r", form="f")

            def worker(n):
                for i in range(15):
                    j.append("round", n=n * 100 + i, summary="x" * 200)

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            recs = journal_mod.read_records(j.path)
            self.assertEqual(len(recs), 120)
            self.assertEqual(sorted(r["seq"] for r in recs), list(range(1, 121)))
            self.assertEqual([r["seq"] for r in recs], sorted(r["seq"] for r in recs))  # file order == seq order
            j.close()

    def test_no_call_records_after_run_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = journal_mod.Journal(tmp, run_id="r", form="f")
            j.append("call.start", key="a")
            j.append("run.end", status="ok", winner_id=None)
            self.assertTrue(j.ended)
            late = j.append("call.end", key="a")
            self.assertTrue(late.get("dropped"))
            self.assertEqual([r["kind"] for r in journal_mod.read_records(j.path)], ["call.start", "run.end"])
            j.close()

    def test_checkpoint_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = journal_mod.Journal(tmp, run_id="r", form="f")
            j.append("run.start", params={}, budget={})
            j.checkpoint({"round": 0, "best": None})
            doc = json.loads(j.checkpoint_path.read_text())
            self.assertEqual((doc["journal_seq"], doc["run_id"], doc["form"]), (1, "r", "f"))
            stop = threading.Event()
            torn = []

            def reader():
                while not stop.is_set():
                    try:
                        json.loads(j.checkpoint_path.read_text())
                    except FileNotFoundError:
                        torn.append("missing")
                    except ValueError:
                        torn.append("torn")

            rt = threading.Thread(target=reader, daemon=True)
            rt.start()

            def writer(n):
                for i in range(40):
                    j.checkpoint({"round": i, "blob": "y" * (2000 + n * 500)})

            writers = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
            for t in writers:
                t.start()
            for t in writers:
                t.join(timeout=60)
            stop.set()
            rt.join(timeout=5)
            self.assertEqual(torn, [])  # readers only ever see complete documents
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])
            self.assertEqual(json.loads(j.checkpoint_path.read_text())["round"], 39)
            j.close()

    def test_write_atomic_failure_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            journal_mod.write_atomic(target, "one")
            with mock.patch.object(journal_mod.os, "replace", side_effect=OSError("nope")):
                with self.assertRaises(OSError):
                    journal_mod.write_atomic(target, "two")
            self.assertEqual(target.read_text(), "one")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["a.txt"])


def _write_run(tmp: str, outputs: dict[str, str], events: list[tuple]) -> Path:
    """events: ('cand', id, round) | ('verdict', id, kind, passed, score) | ('end', status) | ('round', n) | ('stop', r)"""
    run = Path(tmp)
    j = journal_mod.Journal(run, run_id="r", form="f")
    j.append("run.start", params={}, budget={})
    for cid, text in outputs.items():
        ref = f"outputs/{cid}.txt"
        journal_mod.write_atomic(run / ref, text)
    for ev in events:
        if ev[0] == "cand":
            j.append("call.end", key=ev[1], provider="p", status="returned", ok=True, output_ref=f"outputs/{ev[1]}.txt")
            j.append("candidate", id=ev[1], provider="p", output_ref=f"outputs/{ev[1]}.txt", round=ev[2])
        elif ev[0] == "verdict":
            j.append("verdict", candidate_id=ev[1], kind=ev[2], passed=ev[3], score=ev[4], evidence="e")
        elif ev[0] == "round":
            j.append("round", n=ev[1], summary="s")
        elif ev[0] == "stop":
            j.append("stop", reason=ev[1])
        elif ev[0] == "end":
            j.append("run.end", status=ev[1], winner_id=None)
    j.close()
    return run


class SalvageTests(unittest.TestCase):
    OUT = {"a": "text-a", "b": "text-b", "c": "text-c"}

    def test_salvage_prefers_exec_verified_then_llm_then_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(tmp, self.OUT, [
                ("cand", "a", 0), ("verdict", "a", "llm", True, 0.95),
                ("cand", "b", 0), ("verdict", "b", "exec", True, 0.5),
                ("cand", "c", 1), ("round", 0), ("round", 1), ("stop", "cancelled")])
            s = journal_mod.salvage(run)
            self.assertEqual((s["status"], s["best"]["id"], s["verified"]), ("salvaged", "b", True))
            self.assertEqual(s["best"]["output"], "text-b")
            self.assertEqual((s["run_id"], s["form"], s["calls"], s["rounds"], s["stop_reason"]), ("r", "f", 3, 2, "cancelled"))
            self.assertEqual([c["id"] for c in s["candidates"]], ["a", "b", "c"])
        with tempfile.TemporaryDirectory() as tmp:  # exec failed + llm passed: llm wins, verified False
            run = _write_run(tmp, self.OUT, [
                ("cand", "a", 0), ("verdict", "a", "llm", True, 0.4),
                ("cand", "b", 0), ("verdict", "b", "exec", False, 0.0),
                ("cand", "c", 1)])
            s = journal_mod.salvage(run)
            self.assertEqual((s["best"]["id"], s["verified"]), ("a", False))
        with tempfile.TemporaryDirectory() as tmp:  # no verdicts: latest candidate
            run = _write_run(tmp, self.OUT, [("cand", "a", 0), ("cand", "b", 0), ("cand", "c", 1)])
            s = journal_mod.salvage(run)
            self.assertEqual((s["best"]["id"], s["best"]["output"], s["verified"]), ("c", "text-c", False))
        with tempfile.TemporaryDirectory() as tmp:  # higher exec score wins among passed exec verdicts
            run = _write_run(tmp, self.OUT, [
                ("cand", "a", 0), ("verdict", "a", "exec", True, 0.9),
                ("cand", "b", 0), ("verdict", "b", "exec", True, 0.3)])
            self.assertEqual(journal_mod.salvage(run)["best"]["id"], "a")

    def test_salvage_status_complete_only_for_ok_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(tmp, self.OUT, [("cand", "a", 0), ("end", "ok")])
            self.assertEqual(journal_mod.salvage(run)["status"], "complete")
        for end in ("failed", "cancelled", "partial", "budget_exhausted"):
            with tempfile.TemporaryDirectory() as tmp:
                run = _write_run(tmp, self.OUT, [("cand", "a", 0), ("end", end)])
                self.assertEqual(journal_mod.salvage(run)["status"], "salvaged", end)

    def test_salvage_checkpoint_used_only_when_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_run(tmp, self.OUT, [("cand", "a", 0), ("verdict", "a", "exec", True, 1.0), ("cand", "b", 0)])
            last = journal_mod.read_records(run / "journal.jsonl")[-1]["seq"]
            ckpt = {"journal_seq": last - 1, "best": {"id": "STALE", "output": "stale"}, "round": 9, "calls": 99}
            (run / "checkpoint.json").write_text(json.dumps(ckpt))
            s = journal_mod.salvage(run)
            self.assertEqual((s["best"]["id"], s["rounds"], s["calls"]), ("a", 0, 2))  # stale checkpoint ignored
            ckpt = {"journal_seq": last, "best": {"id": "FRESH", "output": "fresh-out",
                                                  "verdict": {"kind": "exec", "passed": True, "score": 1.0}},
                    "round": 3, "calls": 5, "stop_reason": "enough"}
            (run / "checkpoint.json").write_text(json.dumps(ckpt))
            s = journal_mod.salvage(run)
            self.assertEqual((s["best"]["id"], s["best"]["output"], s["verified"]), ("FRESH", "fresh-out", True))
            self.assertEqual((s["rounds"], s["calls"], s["stop_reason"]), (3, 5, "enough"))
            for candidate_id, output, verified in (
                ("a", "text-a", True),
                ("a", "revised output", False),
                ("b", "text-b", False),
                ("aggregate", "union of findings", False),
            ):
                with self.subTest(candidate_id=candidate_id, output=output):
                    ckpt["best"] = {"id": candidate_id, "output": output}
                    (run / "checkpoint.json").write_text(json.dumps(ckpt))
                    recovered = journal_mod.salvage(run)
                    self.assertEqual(recovered["best"]["output"], output)
                    self.assertEqual(recovered["verified"], verified)
            (run / "checkpoint.json").write_text("{garbage")  # unparseable checkpoint falls back to the journal
            self.assertEqual(journal_mod.salvage(run)["best"]["id"], "a")

    def test_salvage_garbage_dir_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(journal_mod.salvage(Path(tmp) / "nope")["status"], "empty")
            self.assertIsNone(journal_mod.salvage(tmp)["best"])
            Path(tmp, "journal.jsonl").write_bytes(b"\x00\xff garbage\n{not json\n[1,2]\n")
            Path(tmp, "checkpoint.json").write_text("][")
            s = journal_mod.salvage(tmp)
            self.assertEqual((s["status"], s["best"], s["verified"], s["candidates"]), ("empty", None, False, []))

    def test_salvage_output_ref_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "secret.txt"
            outside.write_text("TOP SECRET")
            run = Path(tmp) / "run"
            j = journal_mod.Journal(run, run_id="r", form="f")
            j.append("candidate", id="evil", provider="p", output_ref="../secret.txt", round=0)
            j.close()
            s = journal_mod.salvage(run)
            self.assertEqual(s["best"]["output"], "")

    def test_salvage_from_a_real_context_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch, verifier=lambda c: Verdict(True, 1.0, "exec", "green"))
            res = ctx.call(CallSpec("k1", "codex", "hello"))
            ctx.journal.append("candidate", id="c1", provider="codex", output_ref=res.output_ref, round=0)
            ctx.verify({"id": "c1", "output": res.output})
            # no finish(): the run was killed. Salvage still recovers the verified candidate.
            s = journal_mod.salvage(ctx.run_dir)
            self.assertEqual((s["status"], s["best"]["id"], s["verified"], s["best"]["output"]),
                             ("salvaged", "c1", True, "codex:hello"))
            ctx.finish("ok", winner={"id": "c1", "provider": "codex", "output": "codex:hello"},
                       candidates=[{"id": "c1"}], rounds=1, stop_reason="done")
            s2 = journal_mod.salvage(ctx.run_dir)
            self.assertEqual((s2["status"], s2["best"]["id"], s2["verified"]), ("complete", "c1", True))


class LedgerTests(unittest.TestCase):
    def test_ledger_rejects_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = ReliabilityLedger(Path(tmp) / "l.json")
            with self.assertRaisesRegex(ValueError, "unverified outcome"):
                led.record("codex", "review", "refuter", True, verified=False)
            self.assertEqual(led.cell("codex", "review", "refuter").n, 0)
            self.assertFalse(led.dirty)
            led.observe_latency("codex", "review", 12.0)  # latency needs no verification
            self.assertEqual(led.cell("codex", "review", "*").latencies, [12.0])

    def test_ledger_beta_update_decay_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = ReliabilityLedger(Path(tmp) / "l.json", decay=0.5, clock=FakeClock(100.0))
            led.record("w", "t", "r", True, verified=True)
            cell = led.record("w", "t", "r", True, verified=True)
            self.assertEqual((cell.alpha, cell.beta, cell.n, cell.successes, cell.last_ts), (2.5, 1.0, 2, 2, 100.0))
            cell = led.record("w", "t", "r", False, verified=True, latency_s=3.0)
            self.assertEqual((cell.alpha, cell.beta, cell.failures), (1.0 + 1.5 * 0.5, 2.0, 1))
            self.assertAlmostEqual(led.mean("w", "t", "r"), cell.alpha / (cell.alpha + cell.beta))
            self.assertEqual(led.mean("unseen", "t", "r"), 0.5)

    def test_ledger_thompson_ucb_floor_deterministic_seed(self):
        import random

        def build(seed):
            led = ReliabilityLedger(None, rng=random.Random(seed))
            return led

        def seed_cells(led, tmp):
            led.path = Path(tmp) / "x.json"
            for _ in range(20):
                led.record("good", "t", "r", True, verified=True)
                led.record("bad", "t", "r", False, verified=True)
            return led

        with tempfile.TemporaryDirectory() as tmp:
            a = seed_cells(build(7), tmp)
            b = seed_cells(build(7), tmp)
            picks_a = [a.choose(["good", "bad", "new"], "t", "r", explore=0.3) for _ in range(40)]
            picks_b = [b.choose(["good", "bad", "new"], "t", "r", explore=0.3) for _ in range(40)]
            self.assertEqual(picks_a, picks_b)  # deterministic under a seeded rng
            self.assertIn("new", picks_a)       # the exploration floor reaches under-sampled workers
            self.assertEqual(seed_cells(build(1), tmp).choose(["bad", "good"], "t", "r", explore=0.0), "good")
            # explore=1 draws uniformly among workers with n < min_n (only "new")
            self.assertEqual({a.choose(["good", "bad", "new"], "t", "r", explore=1.0) for _ in range(20)}, {"new"})
            # when nobody is under-sampled the floor picks among all
            self.assertEqual({a.choose(["good", "bad"], "t", "r", explore=1.0) for _ in range(40)}, {"good", "bad"})
            # UCB: bonus shrinks with n; an untried worker outscores an equal-mean tried one
            self.assertGreater(a.ucb("new", "t", "r", total=40), a.ucb("good", "t", "r", total=40) - 0.5)
            self.assertAlmostEqual(a.ucb("new", "t", "r", total=40), 0.5 + (2.0 * __import__("math").log(41) / 1) ** 0.5)
            self.assertEqual(a.choose(["bad", "good"], "t", "r", method="ucb", explore=0.0), "good")
            # cost normalization: a cheap mediocre worker beats an expensive good one
            self.assertEqual(a.choose(["good", "bad"], "t", "r", method="mean", explore=0.0,
                                      cost=lambda w: 100.0 if w == "good" else 1.0), "bad")
            self.assertEqual([w for w, _ in a.rank(["bad", "new", "good"], "t", "r")], ["good", "new", "bad"])
            with self.assertRaises(ValueError):
                a.choose([], "t", "r")
            with self.assertRaises(ValueError):
                a.choose(["good"], "t", "r", method="bogus")

    def test_ledger_pair_disable_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = ReliabilityLedger(Path(tmp) / "l.json")
            self.assertEqual(led.pair_stats("a", "b", "t"), {"n": 0, "mean_delta": 0.0, "disabled": False})
            led.record_pair("a", "b", "t", -0.2)
            led.record_pair("a", "b", "t", -0.1)
            self.assertFalse(led.pair_stats("a", "b", "t")["disabled"])  # n < 3
            led.record_pair("a", "b", "t", -0.3)
            st = led.pair_stats("a", "b", "t")
            self.assertEqual(st["n"], 3)
            self.assertAlmostEqual(st["mean_delta"], -0.2)
            self.assertTrue(st["disabled"])
            for d in (0.5, 0.5, 0.5):
                led.record_pair("b", "a", "t", d)  # the mirrored direction is independent
            self.assertFalse(led.pair_stats("b", "a", "t")["disabled"])
            led.record_pair("a", "b", "other", -1.0)
            self.assertEqual(led.pair_stats("a", "b", "other")["n"], 1)

    def test_ledger_concurrent_save_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"
            one = ReliabilityLedger(path, decay=1.0)
            two = ReliabilityLedger(path, decay=1.0)
            one.record("a", "t", "r", True, verified=True, latency_s=1.0)
            two.record("a", "t", "r", False, verified=True, latency_s=2.0)
            one.record("b", "t", "r", True, verified=True)
            two.record_pair("a", "b", "t", -0.5)
            one.record_pair("a", "b", "t", -0.25)
            one.save()
            two.save()
            fresh = ReliabilityLedger(path, decay=1.0)
            a = fresh.cell("a", "t", "r")
            self.assertEqual((a.successes, a.failures, a.n, a.alpha, a.beta), (1, 1, 2, 2.0, 2.0))
            self.assertEqual(fresh.cell("b", "t", "r").n, 1)
            self.assertEqual(sorted(fresh.cell("a", "t", "*").latencies), [1.0, 2.0])
            ps = fresh.pair_stats("a", "b", "t")
            self.assertEqual(ps["n"], 2)
            self.assertAlmostEqual(ps["mean_delta"], -0.375)
            # the saver's in-memory view includes the other writer's updates after the merge
            self.assertEqual(two.cell("b", "t", "r").n, 1)
            self.assertFalse(two.dirty)
            self.assertTrue(path.with_name("l.json.lock").exists())

    def test_ledger_concurrent_threads_lose_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"

            def writer(n):
                led = ReliabilityLedger(path, decay=1.0)
                for i in range(5):
                    led.record("w", "t", "r", i % 2 == 0, verified=True)
                    led.save()

            threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            c = ReliabilityLedger(path).cell("w", "t", "r")
            self.assertEqual((c.n, c.successes, c.failures), (30, 18, 12))
            self.assertEqual((c.alpha, c.beta), (19.0, 13.0))

    @unittest.skipUnless(POSIX, "cross-process flock test is POSIX only")
    def test_ledger_concurrent_processes_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"
            code = (
                "import sys\n"
                "from fusion_swarm.forms.ledger import ReliabilityLedger\n"
                "led = ReliabilityLedger(sys.argv[1], decay=1.0)\n"
                "for i in range(10):\n"
                "    led.record('w', 't', 'r', True, verified=True)\n"
                "    led.save()\n"
            )
            env = dict(os.environ, PYTHONPATH=os.path.join(_ROOT, "python"), PYTHONDONTWRITEBYTECODE="1")
            procs = [subprocess.Popen([sys.executable, "-c", code, str(path)], env=env) for _ in range(3)]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)
            self.assertEqual(ReliabilityLedger(path).cell("w", "t", "r").n, 30)

    def test_ledger_latency_window_and_quantile(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = ReliabilityLedger(Path(tmp) / "l.json")
            for v in range(1, 20):
                led.observe_latency("w", "t", float(v))
            self.assertIsNone(led.latency_quantile("w", "t"))  # < 20 samples
            led.observe_latency("w", "t", 20.0)
            self.assertAlmostEqual(led.latency_quantile("w", "t", 0.9), 18.1)
            self.assertAlmostEqual(led.latency_quantile("w", "t", 0.5, min_samples=5), 10.5)
            for v in range(500):
                led.observe_latency("w", "t", 1.0)
            self.assertEqual(len(led.cell("w", "t", "*").latencies), 200)  # window
            led.save()
            self.assertEqual(len(ReliabilityLedger(led.path).cell("w", "t", "*").latencies), 200)

    def test_ledger_survives_corrupt_file_and_default_path_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.json"
            path.write_text("{corrupt")
            led = ReliabilityLedger(path)
            led.record("w", "t", "r", True, verified=True)
            led.save()
            self.assertEqual(ReliabilityLedger(path).cell("w", "t", "r").n, 1)
            with mock.patch.dict(os.environ, {"FUSION_LEDGER_PATH": str(Path(tmp) / "env.json")}):
                self.assertEqual(ReliabilityLedger().path, Path(tmp) / "env.json")
            with mock.patch.dict(os.environ, {"FUSION_HOME": tmp}, clear=False):
                os.environ.pop("FUSION_LEDGER_PATH", None)
                self.assertEqual(ReliabilityLedger().path, Path(tmp) / "ledger" / "reliability.json")

    def test_finish_persists_dirty_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch)
            ctx.ledger.record("codex", "t", "r", True, verified=True)
            ctx.finish("ok", winner=None, candidates=[], rounds=0, stop_reason="done")
            self.assertEqual(ReliabilityLedger(Path(tmp) / "ledger.json").cell("codex", "t", "r").n, 1)


class CapacityTests(unittest.TestCase):
    def test_capacity_formulas(self):
        self.assertEqual(capacity.workers_needed(0.5, 104), 52)
        self.assertEqual(capacity.workers_needed(0, 100), 1)
        self.assertEqual(capacity.workers_needed(0.011, 100), 2)
        self.assertEqual(capacity.captains_needed(100, 104), 5)
        self.assertEqual(capacity.captains_needed(100, 30), 8)           # cap
        self.assertEqual(capacity.captains_needed(100, 30, cap=20), 15)
        self.assertEqual(capacity.captains_needed(1, 104), 1)
        self.assertEqual(capacity.slots_per_captain(104), 25)
        self.assertEqual(capacity.slots_per_captain(1), 1)
        self.assertEqual(capacity.SPAWN_GAP_S, 4.24)
        w = capacity.size_width(100, 104, max_width=64)
        self.assertEqual((w["width"], w["captains"], w["slots_per_captain"]), (64, 3, 25))
        self.assertAlmostEqual(w["dispatch_ceiling_per_min"], 3 * 60 / 4.24, places=2)
        self.assertEqual(capacity.size_width(100, 104, deadline_s=600)["width"], 18)
        self.assertEqual(capacity.size_width(3, 104)["width"], 3)
        self.assertEqual(capacity.size_width(0, 104)["width"], 0)
        full = capacity.size_width(100, 104, max_width=100)
        self.assertEqual(full["captains"], 5)
        self.assertAlmostEqual(full["expected_makespan_s"], 100 * 4.24 / 5 + 104, places=2)
        with self.assertRaises(ValueError):
            capacity.captains_needed(10, 0)
        self.assertIsNone(capacity.hedge_delay_s([10.0] * 19))
        self.assertEqual(capacity.hedge_delay_s([10.0] * 20), 30)                      # floor
        self.assertEqual(capacity.hedge_delay_s([1000.0] * 20), 600)                   # ceiling
        self.assertAlmostEqual(capacity.hedge_delay_s([float(v) for v in range(100, 120)]), 117.1, places=1)
        self.assertEqual(capacity.quantile([4, 1, 3, 2], 0.5), 2.5)

    def test_hedge_budget_bucket(self):
        b = capacity.HedgeBudget()
        self.assertEqual(b.tokens, 10)  # starts full (J-16)
        grants = 0
        while b.try_hedge():
            grants += 1
            self.assertLess(grants, 50)
        self.assertEqual(grants, 5)      # refused once tokens <= max/2
        self.assertFalse(b.try_hedge())
        for _ in range(20):
            b.on_primary_done()          # +0.05 each -> back above max/2
        self.assertTrue(b.try_hedge())
        self.assertFalse(b.try_hedge())
        for _ in range(1000):
            b.on_primary_done()
        self.assertEqual(b.tokens, 10)   # capped at max

    def test_aimd_increase_backoff_and_window(self):
        mono = FakeClock(1000.0)
        wall = FakeClock(5000.0)
        lim = capacity.AIMDLimiter(8, min_limit=2, max_limit=10, monotonic=mono, clock=wall)
        self.assertEqual(lim.limit, 8)
        for _ in range(7):
            lim.on_success(8)
        self.assertEqual(lim.limit, 8)   # +1 only per `limit` successes
        lim.on_success(8)
        self.assertEqual(lim.limit, 9)
        for _ in range(100):
            lim.on_success(1)            # app-limited (inflight*2 < limit): never grows
        self.assertEqual(lim.limit, 9)
        for _ in range(30):
            lim.on_success(9)
        self.assertEqual(lim.limit, 10)  # max
        lim.on_overload()
        self.assertEqual(lim.limit, 9)   # floor(10 * 0.9)
        lim.on_overload()
        lim.on_overload()
        self.assertEqual(lim.limit, 9)   # burst inside the 5 s window counts once
        mono.advance(5.0)
        lim.on_overload()
        self.assertEqual(lim.limit, 8)
        for _ in range(40):
            mono.advance(5.0)
            lim.on_overload()
        self.assertEqual(lim.limit, 2)   # floor at min_limit
        # admission control
        lim2 = capacity.AIMDLimiter(2, min_limit=1, monotonic=mono, clock=wall)
        self.assertTrue(lim2.acquire(0) and lim2.acquire(0))
        self.assertFalse(lim2.acquire(0))
        lim2.release()
        self.assertTrue(lim2.acquire(0))
        lim2.release()
        lim2.release()
        lim2.release()  # extra release never goes negative
        self.assertEqual(lim2.inflight, 0)
        # usage exhaustion: drop to min and pause until reset_at
        lim3 = capacity.AIMDLimiter(8, min_limit=2, monotonic=mono, clock=wall)
        lim3.on_usage_exhausted(wall() + 60)
        self.assertEqual(lim3.limit, 2)
        self.assertFalse(lim3.acquire(0))
        wall.advance(61)
        self.assertTrue(lim3.acquire(0))
        self.assertIsNone(lim3.paused_until)

    def test_aimd_acquire_unblocks_on_release(self):
        lim = capacity.AIMDLimiter(1, min_limit=1)
        self.assertTrue(lim.acquire(0))
        got = []
        t = threading.Thread(target=lambda: got.append(lim.acquire(10)), daemon=True)
        t.start()
        lim.release()
        t.join(timeout=10)
        self.assertEqual(got, [True])

    def test_gradient2_reacts_to_rtt_inflation(self):
        g = capacity.Gradient2Limiter(16, min_limit=2, max_limit=64)
        self.assertEqual(g.limit, 16)
        idle = g.on_sample(1.0, 0)       # app-limited: unchanged
        self.assertEqual(idle, 16)
        for _ in range(30):
            g.on_sample(1.0, g.limit)    # stable RTT at saturation: limit grows
        peak = g.limit
        self.assertGreater(peak, 16)
        for _ in range(60):
            g.on_sample(8.0, g.limit)    # 8x RTT inflation: median window flips, gradient floors at 0.5
        self.assertLess(g.limit, peak)
        self.assertLessEqual(g.limit, 12)
        self.assertGreaterEqual(g.limit, 2)
        self.assertEqual(g.on_sample(0, 5), g.limit)  # invalid sample ignored

    def test_composite_limiter_takes_the_minimum(self):
        aimd = capacity.AIMDLimiter(8)
        grad = capacity.Gradient2Limiter(3, min_limit=2)
        comp = capacity.CompositeLimiter(aimd, grad)
        self.assertEqual(comp.limit, 3)
        self.assertEqual([comp.acquire(0) for _ in range(4)], [True, True, True, False])
        comp.release()
        self.assertTrue(comp.acquire(0))
        comp.on_overload()
        self.assertEqual(aimd.limit, 7)
        comp.on_success(7)
        comp.on_usage_exhausted(None)
        self.assertEqual(comp.limit, 2)
        solo = capacity.CompositeLimiter(capacity.AIMDLimiter(4))
        self.assertEqual(solo.limit, 4)
        self.assertEqual(solo.on_sample(1.0, 1), 4)


class RegistryTests(unittest.TestCase):
    def test_registry_lists_forms_without_import_errors(self):
        listing = forms.list_forms()  # must not raise even though form modules may be missing
        self.assertEqual([f["name"] for f in listing], list(forms.FORMS))
        for item in listing:
            self.assertIn(item["available"], (True, False))
            if not item["available"]:
                self.assertTrue(item["error"])
        broken = types.ModuleType("fusion_swarm.forms.zz_broken")  # imports fine but has no FORM
        good = types.ModuleType("fusion_swarm.forms.zz_good")
        good.FORM = FormSpec("zz_good", "Does a thing", {"task": "str"}, "never", "first", {"max_calls": 1})
        with mock.patch.dict(forms.FORMS, {"zz_broken": "zz_broken", "zz_good": "zz_good", "zz_missing": "zz_missing"}), \
                mock.patch.dict(sys.modules, {"fusion_swarm.forms.zz_broken": broken, "fusion_swarm.forms.zz_good": good}):
            by = {f["name"]: f for f in forms.list_forms()}
            self.assertFalse(by["zz_broken"]["available"])
            self.assertFalse(by["zz_missing"]["available"])
            self.assertIn("ModuleNotFoundError", by["zz_missing"]["error"])
            self.assertTrue(by["zz_good"]["available"])
            self.assertEqual((by["zz_good"]["summary"], by["zz_good"]["stop_rule"], by["zz_good"]["aggregation"]),
                             ("Does a thing", "never", "first"))
            self.assertIs(forms.get_form("zz_good"), good)
            with self.assertRaises(ImportError):
                forms.get_form("zz_missing")

    def test_aliases_and_unknown(self):
        self.assertEqual(forms.resolve("refute"), "refute_panel")
        self.assertEqual(forms.resolve("Hedged-Race"), "hedged_race")
        for alias, target in forms.ALIASES.items():
            self.assertIn(target, forms.FORMS, alias)
        for name in ("refute_panel", "tournament", "hedged_race", "mapreduce", "critic_repair", "pair_swap", "delphi",
                     "auction", "loop_until_dry", "blackboard"):
            self.assertIn(name, forms.FORMS)
        with self.assertRaises(FormError) as cm:
            forms.get_form("nope")
        self.assertEqual(cm.exception.code, "invalid_input")


class FormResultTests(unittest.TestCase):
    def test_form_error_and_result_shape(self):
        err = FormError("verifier_required", "needs one")
        self.assertEqual((err.code, err.message), ("verifier_required", "needs one"))
        self.assertIsInstance(FormError("cancelled"), Exception)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, dispatch_fn=_ok_dispatch)
            ctx.call(CallSpec("a", "p", "q"))
            r = ctx.finish("partial", winner=None, candidates=[{"id": "x"}], rounds=1, stop_reason="budget")
            d = r.to_dict()
            for key in ("form", "run_id", "status", "winner", "candidates", "rounds", "stop_reason", "calls", "tokens",
                        "elapsed_s", "run_dir", "extra"):
                self.assertIn(key, d)
            self.assertEqual((d["calls"], d["tokens"]), (1, 7))


if __name__ == "__main__":
    unittest.main()
