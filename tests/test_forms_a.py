"""Tests for the fm-a forms: refute_panel, tournament, hedged_race, mapreduce (spec section 9.1-9.4, judge J-13/J-16).

Deterministic by construction: a scripted FakeDispatch that honours the cancel token, fake verifiers, Event handshakes
instead of sleeps, a temp FUSION_HOME and ledger for every test, and no assertions on wall-clock time. The only real
waits are bounded polls (<= 5 s) for OS or thread facts.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "python"))

import fusion_swarm.forms as forms  # noqa: E402
from fusion_swarm.forms import capacity, hedged_race, journal as journal_mod, mapreduce, refute_panel, tournament  # noqa: E402
from fusion_swarm.forms.ledger import ReliabilityLedger  # noqa: E402
from fusion_swarm.forms.runtime import Budget, FormContext, FormError, Verdict  # noqa: E402

POSIX = os.name == "posix"


# -- scripted dispatch -----------------------------------------------------------------------------------------------
def ok(text: str) -> dict:
    return {"ok": True, "output": text, "status": "returned", "rc": 0}


def failed(text: str = "boom") -> dict:
    return {"ok": False, "output": text, "status": "error", "rc": 1}


def block_until_cancel(cancel, started: threading.Event | None = None, seen: threading.Event | None = None,
                       limit: float = 10.0) -> dict:
    """Signal ``started``, then wait for the cancel token; signal ``seen`` when the cancel arrived."""
    if started is not None:
        started.set()
    hit = cancel.wait(limit) if cancel is not None else False
    if hit and seen is not None:
        seen.set()
    return {"ok": False, "output": "", "status": "cancelled" if hit else "timeout", "rc": 1}


class FakeDispatch:
    """handler(provider, prompt, role, cwd, cancel) -> str | dict. Records every call."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, provider, prompt, *, role="", cwd=None, timeout=300, model="", effort="", cancel=None):
        with self._lock:
            self.calls.append({"provider": provider, "prompt": prompt, "role": role, "cwd": cwd, "timeout": timeout})
        out = self.handler(provider, prompt, role, cwd, cancel)
        d = dict(out) if isinstance(out, dict) else ok(str(out))
        d.setdefault("provider", provider)
        d.setdefault("role", role)
        d.setdefault("elapsed_s", 0.0)
        return d

    def by_role(self, role: str) -> list[dict]:
        with self._lock:
            return [c for c in self.calls if c["role"] == role]


def exec_verifier(passes=lambda out: True, score=lambda out: 1.0):
    def verify(candidate: dict) -> Verdict:
        out = candidate.get("output", "")
        return Verdict(bool(passes(out)), float(score(out)), "exec", f"checked {candidate.get('id')}")
    return verify


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, {"FUSION_HOME": str(self.tmp / "home"), "XDG_CONFIG_HOME": str(self.tmp / "xdg")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._n = 0

    def ledger(self) -> ReliabilityLedger:
        return ReliabilityLedger(path=self.tmp / "ledger.json")

    def ctx(self, form: str, dispatch, *, verifier=None, budget=None, ledger=None, run_id=None, max_workers=8) -> FormContext:
        self._n += 1
        return FormContext(form, run_id=run_id, run_dir=self.tmp / f"run{self._n}", dispatch_fn=dispatch,
                           ledger=ledger or self.ledger(), verifier=verifier,
                           budget=budget or Budget(max_calls=200), max_workers=max_workers)

    @staticmethod
    def records(result) -> list[dict]:
        return journal_mod.read_records(Path(result.run_dir) / "journal.jsonl")


NO_REFUTE = {"claim_refuted": False, "evidence_type": "none", "reproduction": "", "summary": "no flaw found"}


def refuter_handler(rules: dict):
    """rules[(provider, marker)] = response dict; default is a clean 'not refuted'. Authors are never called."""
    def handler(provider, prompt, role, cwd, cancel):
        if role == "refuter":
            for (p, marker), resp in rules.items():
                if p == provider and marker in prompt:
                    return json.dumps(resp)
            return json.dumps(NO_REFUTE)
        return "unexpected role " + role
    return handler


def cands(*pairs):
    return [{"id": cid, "provider": prov, "output": out} for cid, prov, out in pairs]


# -- refute_panel ------------------------------------------------------------------------------------------------------
class RefutePanelTests(Base):
    def test_refute_unverified_refutation_is_concern_not_kill(self):
        quote = {"claim_refuted": True, "evidence_type": "quote", "reproduction": "", "summary": "docs disagree"}
        d = FakeDispatch(refuter_handler({("r1", "CAND-X"): quote}))
        res = refute_panel.run("review it", ctx=self.ctx("refute_panel", d), refuters=["r1", "r2", "r3"], k=2, max_k=3,
                               candidates=cands(("x", "a1", "CAND-X claim")))
        self.assertIsNone(res.extra.get("error"))
        self.assertEqual(res.status, "ok")
        cand = res.candidates[0]
        self.assertEqual(cand["status"], "survivor")
        self.assertEqual(cand["concerns"], 1)
        self.assertEqual(cand["verified_refutations"], 0)
        self.assertFalse(res.winner["verified"])                 # no gate configured: survivor stays unverified
        self.assertEqual(len(d.by_role("refuter")), 3)           # r1 concern + r2 clean disagree -> escalated to max_k
        # evidence_type=test with a reproduction that does NOT demonstrate the failure is still only a concern
        test_ref = {"claim_refuted": True, "evidence_type": "test", "reproduction": "pytest -k x", "summary": "fails"}
        d2 = FakeDispatch(refuter_handler({("r1", "CAND-X"): test_ref}))
        res2 = refute_panel.run("review it", ctx=self.ctx("refute_panel", d2), refuters=["r1", "r2", "r3"],
                                candidates=cands(("x", "a1", "CAND-X claim")),
                                reproduce=lambda cwd, repro: Verdict(False, 0.0, "exec", "does not fail"))
        self.assertEqual(res2.candidates[0]["status"], "survivor")
        self.assertEqual(res2.candidates[0]["concerns"], 1)
        refutations = [r for r in self.records(res2) if r["kind"] == "refutation"]
        self.assertTrue(any(r["concern"] and not r["verified"] and r["repro"] == {"ran": True, "passed": False}
                            for r in refutations))

    def test_refute_verified_kill_and_escalation_to_max_k(self):
        kill = {"claim_refuted": True, "evidence_type": "test", "reproduction": "REPRO-OK", "summary": "fails"}
        concern = {"claim_refuted": True, "evidence_type": "quote", "reproduction": "", "summary": "smells"}
        rules = {("r1", "CAND-X"): kill, ("r1", "CAND-Y"): concern, ("r3", "CAND-Y"): kill}
        d = FakeDispatch(refuter_handler(rules))
        led = self.ledger()
        res = refute_panel.run(
            "review it", ctx=self.ctx("refute_panel", d, verifier=exec_verifier(), ledger=led, budget=Budget(max_calls=40)),
            refuters=["r1", "r2", "r3"], k=2, max_k=3, task_class="review",
            candidates=cands(("X", "a1", "CAND-X"), ("Y", "a2", "CAND-Y"), ("Z", "a3", "CAND-Z")),
            reproduce=lambda cwd, repro: Verdict(repro == "REPRO-OK", 1.0, "exec", "repro ran"))
        self.assertIsNone(res.extra.get("error"))
        status = {c["id"]: c["status"] for c in res.candidates}
        self.assertEqual(status, {"X": "killed", "Y": "killed", "Z": "survivor"})
        per_candidate = {m: sum(1 for c in d.by_role("refuter") if m in c["prompt"]) for m in ("CAND-X", "CAND-Y", "CAND-Z")}
        self.assertEqual(per_candidate, {"CAND-X": 2, "CAND-Y": 3, "CAND-Z": 2})   # Y escalated 2 -> max_k, others did not
        self.assertEqual(res.winner["id"], "Z")
        self.assertTrue(res.winner["verified"])
        self.assertEqual(res.status, "ok")
        # ledger: r3's confirmed kill counts, r2's miss on the reproduced-defective candidate counts against it
        self.assertGreater(led.cell("r3", "review", "refuter").alpha, 1.0)
        self.assertGreater(led.cell("r2", "review", "refuter").beta, 1.0)
        self.assertTrue(any(r["kind"] == "ledger" for r in self.records(res)))

    def test_refute_survivor_needs_gate(self):
        d = FakeDispatch(refuter_handler({}))
        res = refute_panel.run("review it", ctx=self.ctx("refute_panel", d, verifier=exec_verifier(lambda o: "GOOD" in o)),
                               refuters=["r1", "r2"], candidates=cands(("P", "a1", "GOOD answer"), ("Q", "a2", "BAD answer")))
        status = {c["id"]: c["status"] for c in res.candidates}
        self.assertEqual(status, {"P": "survivor", "Q": "rejected"})   # no refutation at all, but Q fails the gate
        self.assertEqual(res.winner["id"], "P")
        self.assertTrue(res.winner["verified"])
        d2 = FakeDispatch(refuter_handler({}))
        res2 = refute_panel.run("review it", ctx=self.ctx("refute_panel", d2, verifier=exec_verifier(lambda o: False)),
                                refuters=["r1", "r2"], candidates=cands(("P", "a1", "GOOD")))
        self.assertEqual(res2.status, "failed")
        self.assertEqual(res2.stop_reason, "no_survivor")
        self.assertIsNone(res2.winner)

    def test_refute_authors_round_zero_is_independent_and_refuters_avoid_author_family(self):
        def handler(provider, prompt, role, cwd, cancel):
            if role == "author":
                return f"ANSWER-{provider}"
            return json.dumps(NO_REFUTE)
        d = FakeDispatch(handler)
        # agy and copilot share a model family (adapter.CORRELATION_GROUPS): the refuters for agy's candidate must skip copilot
        res = refute_panel.run("do it", ctx=self.ctx("refute_panel", d, verifier=exec_verifier()),
                               authors=["agy", "claude"], refuters=["copilot", "codex", "claude"], k=2)
        self.assertEqual(res.status, "ok")
        for call in d.by_role("author"):
            self.assertNotIn("ANSWER-", call["prompt"])           # round 0 never shows another candidate
        agy_refuters = {c["provider"] for c in d.by_role("refuter") if "ANSWER-agy" in c["prompt"]}
        self.assertNotIn("copilot", agy_refuters)
        self.assertEqual(len(agy_refuters), 2)

    def test_refute_input_validation(self):
        d = FakeDispatch(refuter_handler({}))
        with self.assertRaises(FormError) as cm:
            refute_panel.run("t", ctx=self.ctx("refute_panel", d), authors=[])
        self.assertEqual(cm.exception.code, "no_providers")
        with self.assertRaises(FormError) as cm:
            refute_panel.run("t", ctx=self.ctx("refute_panel", d), refuters=["r"],
                             candidates=cands(("a", "p", "1"), ("a", "p", "2")))
        self.assertEqual(cm.exception.code, "invalid_input")


# -- tournament --------------------------------------------------------------------------------------------------------
_AB = re.compile(r"=== CANDIDATE A ===\n(.*?)\n=== END A ===.*?=== CANDIDATE B ===\n(.*?)\n=== END B ===", re.S)


def _scores(prompt: str) -> tuple[int, int]:
    m = _AB.search(prompt)
    return (int(re.search(r"score=(\d+)", m.group(1)).group(1)), int(re.search(r"score=(\d+)", m.group(2)).group(1)))


def score_judge(provider, prompt, role, cwd, cancel):
    a, b = _scores(prompt)
    return json.dumps({"winner": "A" if a >= b else "B", "reason": "higher score"})


class TournamentTests(Base):
    CANDS = cands(("c1", "p1", "score=3"), ("c2", "p2", "score=9"), ("c3", "p3", "score=1"), ("c4", "p4", "score=7"),
                  ("c5", "p5", "score=5"))

    def test_tournament_seeded_bracket_n_minus_1_matches_byes(self):
        d = FakeDispatch(score_judge)
        res = tournament.run("pick", ctx=self.ctx("tournament", d), providers=["p1", "p2", "p3", "p4", "p5"],
                             judges=["j1", "j2", "j3"], candidates=self.CANDS, seed=7)
        self.assertIsNone(res.extra.get("error"))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.winner["id"], "c2")                   # highest score wins under a consistent judge
        self.assertEqual(res.extra["matches"], 4)                  # n-1 matches for n=5
        self.assertEqual(res.rounds, 3)                            # ceil(log2 5)
        self.assertEqual(res.extra["seed"], 7)
        rounds = [r for r in self.records(res) if r["kind"] == "round"]
        self.assertEqual([r["n"] for r in rounds], [1, 2, 3])
        self.assertEqual(len(rounds[0]["summary"]["byes"]), 3)     # 8-slot bracket, 5 candidates: 3 byes for the top seeds
        self.assertEqual(sum(len(r["summary"]["matches"]) for r in rounds), 4)
        for r in rounds:
            for m in r["summary"]["matches"]:
                self.assertEqual(m["basis"], "judge")
                self.assertEqual(set(m["judge_votes"]), {"AB", "BA"})
        self.assertEqual(len(d.by_role("judge")), 8)               # two orders per match, no third judge needed
        self.assertTrue({c["provider"] for c in d.by_role("judge")} <= {"j1", "j2", "j3"})
        # same seed -> same bracket; a different seed reorders it
        d2 = FakeDispatch(score_judge)
        res2 = tournament.run("pick", ctx=self.ctx("tournament", d2), providers=["p1", "p2", "p3", "p4", "p5"],
                              judges=["j1", "j2", "j3"], candidates=self.CANDS, seed=7)
        pairs = lambda r: [(m["a"], m["b"]) for m in r.extra["bracket"]]  # noqa: E731
        self.assertEqual(pairs(res), pairs(res2))
        # default seed = int(sha256(run_id)[:8], 16)
        d3 = FakeDispatch(score_judge)
        res3 = tournament.run("pick", ctx=self.ctx("tournament", d3, run_id="fixed-run-id"), judges=["j1"],
                              providers=["p1"], candidates=self.CANDS)
        self.assertEqual(res3.extra["seed"], tournament.default_seed("fixed-run-id"))
        # byes: every bracket size has exactly n-1 real matches
        for n in (1, 2, 3, 4, 6, 7, 8):
            items = cands(*[(f"k{i}", f"p{i}", f"score={i}") for i in range(n)])
            dn = FakeDispatch(score_judge)
            rn = tournament.run("pick", ctx=self.ctx("tournament", dn), providers=["p"], judges=["j1", "j2"],
                                candidates=items, seed=n)
            self.assertEqual(rn.extra["matches"], n - 1, f"n={n}")
            self.assertEqual(rn.winner["id"], f"k{n - 1}", f"n={n}")

    def test_tournament_both_orders_and_exec_beats_judge(self):
        two = cands(("c1", "p1", "score=1"), ("c2", "p2", "score=2"))

        def handler(provider, prompt, role, cwd, cancel):
            if provider == "j1":
                return json.dumps({"winner": "A", "reason": "always first"})     # position-biased judge
            return score_judge(provider, prompt, role, cwd, cancel)
        d = FakeDispatch(handler)
        res = tournament.run("pick", ctx=self.ctx("tournament", d), providers=["p1", "p2"], judges=["j1", "j2", "j3"],
                             candidates=two, seed=1)
        j1_calls = [c for c in d.by_role("judge") if c["provider"] == "j1"]
        self.assertEqual(len(j1_calls), 2)                                      # judged in both orders
        firsts = {re.search(r"score=(\d)", _AB.search(c["prompt"]).group(1)).group(1) for c in j1_calls}
        self.assertEqual(firsts, {"1", "2"})                                    # each candidate was shown first once
        self.assertEqual([c["provider"] for c in d.by_role("judge")].count("j2"), 1)   # disagreement -> a third judge
        match = res.extra["bracket"][0]
        self.assertEqual(match["basis"], "judge3")
        self.assertEqual(res.winner["id"], "c2")
        # execution beats judgment: the verifier decides and the (adversarial) judge is never consulted
        d2 = FakeDispatch(lambda p, pr, r, c, x: json.dumps({"winner": "A" if _scores(pr)[0] < _scores(pr)[1] else "B"}))
        verifier = exec_verifier(score=lambda out: int(re.search(r"score=(\d+)", out).group(1)))
        res2 = tournament.run("pick", ctx=self.ctx("tournament", d2, verifier=verifier), providers=["p1", "p2", "p3"],
                              judges=["j1"], candidates=cands(("c1", "p1", "score=4"), ("c2", "p2", "score=8"),
                                                              ("c3", "p3", "score=6")), seed=3)
        self.assertEqual(res2.winner["id"], "c2")
        self.assertTrue(res2.winner["verified"])
        self.assertEqual(d2.calls, [])
        self.assertTrue(all(m["basis"] == "exec" for m in res2.extra["bracket"]))
        # an exec tie falls through to judgment
        d3 = FakeDispatch(score_judge)
        res3 = tournament.run("pick", ctx=self.ctx("tournament", d3, verifier=exec_verifier()), providers=["p1", "p2"],
                              judges=["j1"], candidates=two, seed=2)
        self.assertEqual(res3.extra["bracket"][0]["basis"], "judge")
        self.assertEqual(res3.winner["id"], "c2")

    def test_tournament_generates_candidates_and_judges_never_author(self):
        def handler(provider, prompt, role, cwd, cancel):
            if role == "author":
                return f"score={ {'p1': 2, 'p2': 8, 'p3': 5}[provider] }"
            return score_judge(provider, prompt, role, cwd, cancel)
        d = FakeDispatch(handler)
        res = tournament.run("write", ctx=self.ctx("tournament", d), providers=["p1", "p2", "p3"])
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.winner["provider"], "p2")
        self.assertEqual(len(d.by_role("author")), 3)
        for match in res.extra["bracket"]:
            authors = {m["provider"] for m in res.candidates if m["id"] in (match["a"], match["b"])}
            self.assertNotIn(match["judge"], authors)


# -- hedged_race -------------------------------------------------------------------------------------------------------
GOOD_VERIFIER = exec_verifier(lambda out: "GOOD" in out)


class HedgedRaceTests(Base):
    def test_hedged_race_first_verified_wins_and_losers_cancelled(self):
        started, seen = threading.Event(), threading.Event()

        def handler(provider, prompt, role, cwd, cancel):
            if provider == "p":
                return block_until_cancel(cancel, started, seen)
            started.wait(5)                                       # the primary is demonstrably running
            return ok("GOOD from " + provider)
        d = FakeDispatch(handler)
        res = hedged_race.run("task", ctx=self.ctx("hedged_race", d, verifier=GOOD_VERIFIER), primary="p",
                              backups=["b1", "b2"], hedge_delay_s=0.0, max_hedges=1, read_only=True)
        self.assertIsNone(res.extra.get("error"))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.winner["provider"], "b1")
        self.assertTrue(res.winner["verified"])
        self.assertEqual(res.extra["hedges"], 1)
        self.assertEqual(res.calls, 2)
        self.assertTrue(seen.wait(5), "primary never observed the cancel token")
        recs = self.records(res)
        self.assertTrue(any(r["kind"] == "cancel" and r.get("attempt") == 0 for r in recs))
        self.assertTrue(any(r["kind"] == "hedge" and r["provider"] == "b1" for r in recs))
        self.assertEqual(recs[-1]["kind"], "run.end")             # no call.* records after run.end
        # first VERIFIED, not first finished: b1 finishes first with a result that fails the verifier
        started2, seen2 = threading.Event(), threading.Event()

        def handler2(provider, prompt, role, cwd, cancel):
            if provider == "p":
                return block_until_cancel(cancel, started2, seen2)
            if provider == "b1":
                return ok("BAD quick answer")
            started2.wait(5)
            return ok("GOOD late answer")
        res2 = hedged_race.run("task", ctx=self.ctx("hedged_race", FakeDispatch(handler2), verifier=GOOD_VERIFIER),
                               primary="p", backups=["b1", "b2"], hedge_delay_s=0.0, max_hedges=2, read_only=True)
        self.assertEqual(res2.status, "ok")
        self.assertEqual(res2.winner["provider"], "b2")
        self.assertTrue(seen2.wait(5))

    @unittest.skipUnless(POSIX, "process groups are POSIX only")
    def test_hedged_race_loser_process_group_killed(self):
        procs: list[subprocess.Popen] = []
        started = threading.Event()

        def handler(provider, prompt, role, cwd, cancel):
            if provider == "p":
                proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
                procs.append(proc)
                cancel.register(proc)
                started.set()
                proc.wait()                                       # returns when the group is killed by the cancel token
                return {"ok": False, "output": "", "status": "cancelled", "rc": proc.returncode or 1}
            started.wait(5)
            return ok("GOOD")
        res = hedged_race.run("task", ctx=self.ctx("hedged_race", FakeDispatch(handler), verifier=GOOD_VERIFIER), primary="p",
                              backups=["b1"], hedge_delay_s=0.0, max_hedges=1, read_only=True)
        self.assertEqual(res.status, "ok")
        self.assertEqual(len(procs), 1)
        try:
            procs[0].wait(timeout=5)                              # killed, not left running
        except subprocess.TimeoutExpired:
            procs[0].kill()
            self.fail("loser process group survived the cancel")
        self.assertIsNotNone(procs[0].returncode)

    def test_hedged_race_refuses_unisolated_writes(self):
        d = FakeDispatch(lambda *a: ok("GOOD"))
        with self.assertRaises(FormError) as cm:
            hedged_race.run("task", ctx=self.ctx("hedged_race", d, verifier=GOOD_VERIFIER), primary="p", backups=["b"])
        self.assertEqual(cm.exception.code, "invalid_input")
        self.assertEqual(d.calls, [])
        with self.assertRaises(FormError) as cm:                  # read-only declared but no verifier
            hedged_race.run("task", ctx=self.ctx("hedged_race", d), primary="p", backups=["b"], read_only=True)
        self.assertEqual(cm.exception.code, "verifier_required")
        with self.assertRaises(FormError) as cm:
            hedged_race.run("task", ctx=self.ctx("hedged_race", d, verifier=GOOD_VERIFIER), primary="p", backups=["b"],
                            read_only=True, max_hedges=3)
        self.assertEqual(cm.exception.code, "invalid_input")
        # sandboxed writes are fine and every attempt gets its own directory
        dirs = [str(self.tmp / f"sandbox{i}") for i in range(3)]
        started = threading.Event()

        def handler(provider, prompt, role, cwd, cancel):
            if provider == "p":
                return block_until_cancel(cancel, started)
            started.wait(5)
            return ok("GOOD")
        d2 = FakeDispatch(handler)
        res = hedged_race.run("task", ctx=self.ctx("hedged_race", d2, verifier=GOOD_VERIFIER), primary="p", backups=["b"],
                              hedge_delay_s=0.0, max_hedges=1, isolate=lambda i: dirs[i])
        self.assertEqual(res.status, "ok")
        self.assertEqual({c["provider"]: c["cwd"] for c in d2.calls}, {"p": dirs[0], "b": dirs[1]})

    def test_hedged_race_hedge_budget_refusal(self):
        gate = threading.Event()

        def handler(provider, prompt, role, cwd, cancel):
            self.assertEqual(provider, "p", "no hedge may launch once the budget refuses")
            gate.wait(5)
            return ok("GOOD")
        spent = capacity.HedgeBudget(max_tokens=2)
        spent.tokens = 1.0                                         # <= max/2: hedging refused
        d = FakeDispatch(handler)
        ctx = self.ctx("hedged_race", d, verifier=GOOD_VERIFIER)
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("res", hedged_race.run(
            "task", ctx=ctx, primary="p", backups=["b"], hedge_delay_s=0.0, read_only=True, hedge_budget=spent)), daemon=True)
        t.start()
        path = Path(ctx.run_dir) / "journal.jsonl"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(r.get("kind") == "hedge" and r.get("refused")
                                                      for r in journal_mod.read_records(path)):
            time.sleep(0.01)
        gate.set()
        t.join(10)
        self.assertFalse(t.is_alive())
        res = box["res"]
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.winner["provider"], "p")
        self.assertEqual(res.extra["hedges"], 0)
        self.assertTrue(res.extra["hedge_blocked"])

    def test_hedged_race_all_fail_returns_best_partial(self):
        outputs = {"p": ok("BAD-1"), "b1": ok("BAD-2"), "b2": failed("provider crashed")}
        d = FakeDispatch(lambda provider, *a: outputs[provider])
        scores = {"BAD-1": 0.2, "BAD-2": 0.6}
        led = self.ledger()
        verifier = exec_verifier(lambda out: False, lambda out: scores.get(out, 0.0))
        res = hedged_race.run("task", ctx=self.ctx("hedged_race", d, verifier=verifier, ledger=led), primary="p",
                              backups=["b1", "b2"], hedge_delay_s=0.0, max_hedges=2, read_only=True)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.stop_reason, "all_attempts_failed")
        self.assertEqual(res.winner["provider"], "b1")             # best partial by verdict score
        self.assertFalse(res.winner["verified"])
        self.assertEqual(len(res.extra["attempts"]), 3)
        self.assertGreater(led.cell("p", "general", "worker").beta, 1.0)   # exec-verified failure is recorded
        salv = journal_mod.salvage(res.run_dir)
        self.assertEqual(salv["best"]["id"], res.winner["id"])
        self.assertFalse(salv["verified"])

    def test_hedged_race_ledger_p90_sets_hedge_delay(self):
        led = self.ledger()
        for _ in range(25):
            led.observe_latency("p", "general", 45.0)
        d = FakeDispatch(lambda *a: ok("GOOD"))
        res = hedged_race.run("task", ctx=self.ctx("hedged_race", d, verifier=GOOD_VERIFIER, ledger=led), primary="p",
                              backups=["b"], read_only=True)
        self.assertEqual(res.extra["delay_s"], 45.0)
        self.assertEqual(res.extra["hedges"], 0)                   # the primary verified long before the delay
        self.assertEqual(res.calls, 1)


# -- mapreduce ---------------------------------------------------------------------------------------------------------
def _sid(prompt: str) -> str:
    return re.search(r"SHARD ID: (\S+)", prompt).group(1)


def map_json(sid: str, facts=None, conf=0.9) -> str:
    return json.dumps({"shard_id": sid, "facts": facts if facts is not None else [f"fact-{sid}"], "confidence": conf})


def make_reducer(cite_only=None):
    def reduce(prompt: str) -> str:
        ids = re.findall(r'"shard_id": "([^"]+)"', prompt)
        return json.dumps({"merged": "merged " + ",".join(ids), "conflicts": [], "cited": cite_only if cite_only is not None else ids})
    return reduce


class MapReduceTests(Base):
    SHARDS = [{"id": f"s{i}", "payload": f"payload {i}"} for i in range(1, 5)]

    def test_mapreduce_backup_for_straggler_dedup_by_shard(self):
        started, seen = threading.Event(), threading.Event()
        reducer_prompts: list[str] = []

        def handler(provider, prompt, role, cwd, cancel):
            if role == "reducer":
                reducer_prompts.append(prompt)
                return make_reducer()(prompt)
            sid = _sid(prompt)
            if sid == "s4" and provider == "m2":                   # the primary of s4 is the straggler
                block_until_cancel(cancel, started, seen)
                return ok(map_json("s4", ["late"]))                # ignores the cancel and still answers: must be deduped
            return ok(map_json(sid, ["backup"] if sid == "s4" else None))
        d = FakeDispatch(handler)
        res = mapreduce.run("extract", ctx=self.ctx("mapreduce", d), shards=self.SHARDS, mappers=["m1", "m2"], reducer="red")
        self.assertIsNone(res.extra.get("error"))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.extra["backups"], 1)
        self.assertEqual(res.extra["backup_cap"], 1)               # ceil(0.10 * 4)
        self.assertTrue(seen.wait(5), "the straggler primary was never cancelled")
        recs = self.records(res)
        shard_cands = [r for r in recs if r["kind"] == "candidate" and r["id"].startswith("shard:")]
        self.assertEqual(sorted(r["id"] for r in shard_cands), ["shard:s1", "shard:s2", "shard:s3", "shard:s4"])  # dedupe by id
        s4 = next(r for r in shard_cands if r["id"] == "shard:s4")
        self.assertEqual(s4["provider"], "m1")                     # the backup ran on a different mapper
        self.assertTrue(any(r["kind"] == "hedge" and r.get("reason") == "straggler" for r in recs))
        self.assertTrue(any(r["kind"] == "cancel" and r.get("reason") == "dedupe" for r in recs))
        self.assertEqual(len(reducer_prompts), 1)
        self.assertIn("backup", reducer_prompts[0])
        self.assertNotIn("late", reducer_prompts[0])
        self.assertEqual(res.winner["cited"], ["s1", "s2", "s3", "s4"])
        self.assertEqual(res.extra["missing"], [])

    def test_mapreduce_coverage_missing_is_partial(self):
        shards = [{"id": f"s{i}", "payload": f"p{i}"} for i in (1, 2, 3)]

        def handler(provider, prompt, role, cwd, cancel):
            if role == "reducer":
                return make_reducer()(prompt)
            sid = _sid(prompt)
            return failed("mapper crashed") if sid == "s3" else ok(map_json(sid))
        d = FakeDispatch(handler)
        res = mapreduce.run("extract", ctx=self.ctx("mapreduce", d), shards=shards, mappers=["m1", "m2"], reducer="red")
        self.assertEqual(res.status, "partial")
        self.assertEqual(res.stop_reason, "coverage_missing")
        self.assertEqual(res.extra["missing"], ["s3"])
        self.assertEqual(res.winner["cited"], ["s1", "s2"])
        s3_calls = [c for c in d.by_role("mapper") if "SHARD ID: s3" in c["prompt"]]
        self.assertEqual(len(s3_calls), 2)                         # primary + its one backup, both failed
        self.assertEqual({c["provider"] for c in s3_calls}, {"m1", "m2"})
        # a reducer that skips a shard it was given is also partial
        d2 = FakeDispatch(lambda p, pr, role, c, x: make_reducer(["s1"])(pr) if role == "reducer" else ok(map_json(_sid(pr))))
        res2 = mapreduce.run("extract", ctx=self.ctx("mapreduce", d2), shards=shards, mappers=["m1"], reducer="red")
        self.assertEqual(res2.status, "partial")
        self.assertEqual(res2.extra["uncited"], ["s2", "s3"])
        self.assertEqual(res2.extra["missing"], [])
        # nothing mapped at all -> failed, no reducer call
        d3 = FakeDispatch(lambda p, pr, role, c, x: failed())
        res3 = mapreduce.run("extract", ctx=self.ctx("mapreduce", d3), shards=shards, mappers=["m1"], reducer="red")
        self.assertEqual(res3.status, "failed")
        self.assertEqual(d3.by_role("reducer"), [])

    def test_mapreduce_shard_id_mismatch_is_invalid_and_conflicts_surface(self):
        def handler(provider, prompt, role, cwd, cancel):
            if role == "reducer":
                return json.dumps({"merged": "x", "cited": ["s1", "s2"],
                                   "conflicts": [{"claim": "colour", "shards": ["s1", "s2"]}]})
            sid = _sid(prompt)
            if provider == "m1" and sid == "s1":
                return ok(map_json("s9"))                          # wrong shard id: invalid, backup on m2 answers
            return ok(map_json(sid))
        d = FakeDispatch(handler)
        res = mapreduce.run("extract", ctx=self.ctx("mapreduce", d), shards=self.SHARDS[:2], mappers=["m1", "m2"], reducer="red")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.extra["conflicts"], [{"claim": "colour", "shards": ["s1", "s2"]}])
        self.assertEqual(res.extra["shard_results"]["s1"]["shard_id"], "s1")
        self.assertEqual(res.extra["backups"], 1)

    def test_mapreduce_input_validation_and_partitioner(self):
        d = FakeDispatch(lambda *a: failed())
        for bad in ([], [{"id": "a", "payload": "x"}, {"id": "a", "payload": "y"}], [{"id": "", "payload": "x"}]):
            with self.assertRaises(FormError) as cm:
                mapreduce.run("t", ctx=self.ctx("mapreduce", d), shards=bad, mappers=["m"], reducer="r")
            self.assertEqual(cm.exception.code, "invalid_input")
        with self.assertRaises(FormError) as cm:
            mapreduce.run("t", ctx=self.ctx("mapreduce", d), shards=self.SHARDS, mappers=[], reducer="r")
        self.assertEqual(cm.exception.code, "no_providers")
        items = [f"file{i}.py" for i in range(10)]
        a, b = mapreduce.partition(items, 3), mapreduce.partition(items, 3)
        self.assertEqual(a, b)
        self.assertEqual("\n".join(s["payload"] for s in a), "\n".join(items))     # chunking is order preserving and complete
        h = mapreduce.partition(items, 4, by="hash")
        self.assertEqual(sorted("\n".join(s["payload"] for s in h).split("\n")), sorted(items))
        self.assertEqual(h, mapreduce.partition(items, 4, by="hash"))


# -- salvage and registry ----------------------------------------------------------------------------------------------
class SalvageAndRegistryTests(Base):
    def test_forms_a_salvage_after_cancel(self):
        # refute_panel: cancelled while the gate runs on the second candidate; the first verified survivor is salvaged
        holder: dict = {}

        def verifier(candidate):
            if "GOOD1" in candidate["output"]:
                return Verdict(True, 1.0, "exec", "ok")
            holder["ctx"].cancel.cancel("test-cancel")
            return Verdict(False, 0.0, "none", "interrupted")
        ctx = self.ctx("refute_panel", FakeDispatch(refuter_handler({})), verifier=verifier)
        holder["ctx"] = ctx
        res = refute_panel.run("t", ctx=ctx, refuters=["r1", "r2"], candidates=cands(("P1", "a1", "GOOD1"), ("P2", "a2", "GOOD2")))
        self.assertEqual(res.status, "cancelled")
        self.assertEqual(res.stop_reason, "test-cancel")
        self.assertEqual(res.winner["id"], "P1")
        salv = journal_mod.salvage(res.run_dir)
        self.assertEqual(salv["status"], "salvaged")
        self.assertEqual(salv["best"]["id"], "P1")
        self.assertTrue(salv["verified"])
        self.assertEqual(salv["best"]["output"], "GOOD1")

        # tournament: cancelled in round 2; survivors of the completed round 1 are salvaged, best by verifier
        calls = {"n": 0}
        lock = threading.Lock()
        holder2: dict = {}

        def judge(provider, prompt, role, cwd, cancel):
            with lock:
                calls["n"] += 1
                n = calls["n"]
            if n == 5:                                              # first call of round 2 (round 1 used exactly 4)
                holder2["ctx"].cancel.cancel("stop-now")
            return score_judge(provider, prompt, role, cwd, cancel)
        ctx2 = self.ctx("tournament", FakeDispatch(judge), verifier=exec_verifier())    # equal exec scores -> judges decide
        holder2["ctx"] = ctx2
        res2 = tournament.run("pick", ctx=ctx2, providers=["p"], judges=["j1", "j2"], seed=5,
                              candidates=cands(("c1", "p1", "score=1"), ("c2", "p2", "score=2"),
                                               ("c3", "p3", "score=3"), ("c4", "p4", "score=4")))
        self.assertEqual(res2.status, "cancelled")
        self.assertEqual(len(res2.extra["survivors"]), 2)
        self.assertEqual(res2.rounds, 1)
        salv2 = journal_mod.salvage(res2.run_dir)
        self.assertEqual(salv2["status"], "salvaged")
        self.assertIn(salv2["best"]["id"], res2.extra["survivors"])
        self.assertTrue(salv2["verified"])

        # mapreduce: cancelled while mapping s3; per-shard results of s1 and s2 and the missing list survive, no reduce
        holder3: dict = {}

        def mapper(provider, prompt, role, cwd, cancel):
            if role == "reducer":
                raise AssertionError("reduce must not run after a cancel")
            sid = _sid(prompt)
            if sid == "s3":
                holder3["ctx"].cancel.cancel("halt")
                return {"ok": False, "output": "", "status": "cancelled", "rc": 1}
            return ok(map_json(sid))
        d3 = FakeDispatch(mapper)
        ctx3 = self.ctx("mapreduce", d3, max_workers=1)
        holder3["ctx"] = ctx3
        shards = [{"id": f"s{i}", "payload": f"p{i}"} for i in (1, 2, 3)]
        res3 = mapreduce.run("extract", ctx=ctx3, shards=shards, mappers=["m1"], reducer="red")
        self.assertEqual(res3.status, "cancelled")
        self.assertEqual(res3.extra["missing"], ["s3"])
        salv3 = journal_mod.salvage(res3.run_dir)
        self.assertEqual({c["id"] for c in salv3["candidates"]}, {"shard:s1", "shard:s2"})
        self.assertEqual(d3.by_role("reducer"), [])

        # hedged_race: cancelled while only the primary runs and nothing verified -> no winner, journal intact
        started = threading.Event()
        holder4: dict = {}

        def slow(provider, prompt, role, cwd, cancel):
            holder4["ctx"].cancel.cancel("deadline-ish") if started.is_set() else None
            started.set()
            return block_until_cancel(cancel)
        ctx4 = self.ctx("hedged_race", FakeDispatch(slow), verifier=GOOD_VERIFIER)
        holder4["ctx"] = ctx4
        res4 = hedged_race.run("task", ctx=ctx4, primary="p", backups=["b"], hedge_delay_s=0.0, max_hedges=1, read_only=True)
        self.assertEqual(res4.status, "cancelled")
        self.assertIsNone(res4.winner)
        self.assertEqual(self.records(res4)[-1]["kind"], "run.end")

    def test_registry_forms_a_available_and_names_match(self):
        listing = {f["name"]: f for f in forms.list_forms()}
        for name, module in (("refute_panel", refute_panel), ("tournament", tournament), ("hedged_race", hedged_race),
                             ("mapreduce", mapreduce)):
            self.assertTrue(listing[name]["available"], listing[name])
            self.assertEqual(module.FORM.name, name)
            self.assertIs(forms.get_form(name), module)
            self.assertTrue(callable(module.run))
            self.assertIn("max_calls", module.FORM.default_budget)
            self.assertTrue(module.FORM.stop_rule and module.FORM.aggregation)
        self.assertIs(forms.get_form("refute"), refute_panel)
        self.assertIs(forms.get_form("hedge"), hedged_race)
        self.assertIs(forms.get_form("map"), mapreduce)


if __name__ == "__main__":
    unittest.main()
