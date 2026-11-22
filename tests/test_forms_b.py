"""Tests for the fm-b forms: critic_repair, pair_swap, delphi (spec 9.5-9.7; judge J-16).

No real provider CLIs: every dispatch function is an in-process fake and the verifiers are plain callables. Everything is
sequential or Event-synchronised; no wall-clock assertions.
"""
from __future__ import annotations

import difflib
import json
import os
import random
import statistics
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "python"))

import fusion_swarm.forms as forms  # noqa: E402
from fusion_swarm.forms import critic_repair, delphi, journal as journal_mod, pair_swap  # noqa: E402
from fusion_swarm.forms.ledger import ReliabilityLedger  # noqa: E402
from fusion_swarm.forms.runtime import Budget, FormContext, FormError, Verdict  # noqa: E402
from fusion_swarm.peers import exec_json  # noqa: E402


def reply(output="", *, ok=True, status="returned", provider="fake", role=""):
    return {"ok": ok, "output": output, "status": status, "provider": provider, "role": role, "rc": 0,
            "elapsed_s": 0.0, "model": None, "usage": None, "session_id": None}


class FakeDispatch:
    """Thread-safe scripted dispatch. ``handler(provider, prompt, role, n) -> dict | str`` gets the 1-based call count."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, provider, prompt, *, role="", cwd=None, timeout=300, model="", effort="", cancel=None):
        with self._lock:
            self.calls.append({"provider": provider, "prompt": prompt, "role": role})
            n = len(self.calls)
        out = self.handler(provider, prompt, role, n)
        return out if isinstance(out, dict) else reply(out, provider=provider, role=role)

    def prompts(self, role=None, provider=None):
        return [c["prompt"] for c in self.calls
                if (role is None or c["role"] == role) and (provider is None or c["provider"] == provider)]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, {"FUSION_HOME": str(self.tmp / "home"),
                                               "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
                                               "FUSION_LEDGER_PATH": str(self.tmp / "home" / "ledger.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._n = 0

    def ledger(self):
        self._ledgers = getattr(self, "_ledgers", 0) + 1
        return ReliabilityLedger(self.tmp / f"ledger{self._ledgers}.json", rng=random.Random(7))

    def ctx(self, form, dispatch=None, verifier=None, max_calls=None, ledger=None):
        self._n += 1
        return FormContext(form, run_dir=self.tmp / f"run{self._n}", run_id=f"{form}-t{self._n}",
                           budget=Budget(max_calls=max_calls), dispatch_fn=dispatch, verifier=verifier,
                           ledger=ledger if ledger is not None else self.ledger())

    def journal(self, ctx, kind):
        return [r for r in journal_mod.read_records(ctx.journal.path) if r.get("kind") == kind]


def table_verifier(scores, *, kind="exec", passing=1.0):
    """Verifier scoring a candidate by its output text via ``scores`` (unknown text -> 0.0)."""
    seen = []

    def verify(candidate):
        score = float(scores.get(candidate["output"], 0.0))
        seen.append(candidate["id"])
        return Verdict(score >= passing, score, kind, f"score={score} for {candidate['output'][:30]}")

    verify.seen = seen
    return verify


# ======================================================================================================== critic_repair
class CriticRepairTests(Base):
    def scripted(self, writes, critic_json=None):
        """writer returns writes[0] then the repairs in order; every critic reply is ``critic_json``."""
        repairs = list(writes[1:])
        crit = critic_json or json.dumps({"issues": [{"summary": "bug", "evidence": "x", "severity": "high"}],
                                          "verdict": "issues"})

        def handler(provider, prompt, role, n):
            if role == "writer":
                return writes[0]
            if role == "repairer":
                return repairs.pop(0)
            return crit

        return FakeDispatch(handler)

    def run_form(self, writes, scores, *, critic_json=None, max_rounds=3, patience=2, kind="exec", restore=None,
                 critics=("grok", "agy"), writer="opencode"):
        disp = self.scripted(writes, critic_json)
        ctx = self.ctx("critic_repair", disp, table_verifier(scores, kind=kind), max_calls=1 + 3 * max_rounds)
        res = critic_repair.run("do it", ctx=ctx, writer=writer, critics=list(critics), max_rounds=max_rounds,
                                patience=patience, restore=restore)
        return res, disp, ctx

    def test_critic_repair_stop_rules_a_to_e(self):
        ok = json.dumps({"issues": [], "verdict": "ok"})
        with self.subTest("a: verifier passes and critic ok"):
            res, disp, ctx = self.run_form(["w0"], {"w0": 1.0}, critic_json=ok)
            self.assertEqual((res.status, res.stop_reason, res.calls), ("ok", "verified_and_critic_ok", 2))
            self.assertEqual(res.winner["id"], "v0")
            self.assertTrue(res.extra["verified"])
            self.assertEqual(self.journal(ctx, "stop")[-1]["reason"], "verified_and_critic_ok")
        with self.subTest("a': a passing verifier with issues keeps repairing; critic ok on the repair ends it"):
            seq = iter([json.dumps({"issues": [{"summary": "style"}], "verdict": "issues"}), ok])
            disp = FakeDispatch(lambda p, pr, role, n: {"writer": "w0", "repairer": "w1"}.get(role) or next(seq))
            ctx = self.ctx("critic_repair", disp, table_verifier({"w0": 1.0, "w1": 1.0}), max_calls=10)
            res = critic_repair.run("t", ctx=ctx, writer="opencode", critics=["grok"], max_rounds=3)
            self.assertEqual((res.stop_reason, res.winner["id"]), ("verified_and_critic_ok", "v0"))
            # equal verdicts: the earlier version stays best, so the repair is not preferred over a verified draft
            self.assertEqual(len(res.candidates), 2)
        with self.subTest("b: no improvement for patience rounds"):
            res, _, _ = self.run_form(["w0", "w1", "w2", "w3"], {"w0": 0.2, "w1": 0.2, "w2": 0.2, "w3": 0.9},
                                      max_rounds=5, patience=2)
            self.assertEqual((res.stop_reason, res.rounds, res.status), ("no_improvement", 2, "failed"))
            self.assertEqual(res.winner["id"], "v0")
            self.assertFalse(res.extra["verified"])
        with self.subTest("c: repeated output hash"):
            res, _, _ = self.run_form(["w0", "w1", "w0"], {"w0": 0.1, "w1": 0.3}, max_rounds=5, patience=5)
            self.assertEqual(res.stop_reason, "output_repeated")
            self.assertEqual(len(res.candidates), 2)  # the repeat was not even verified
            self.assertEqual(res.winner["id"], "v1")
        with self.subTest("d: max_rounds"):
            res, _, _ = self.run_form(["w0", "w1", "w2"], {"w0": 0.1, "w1": 0.2, "w2": 0.3}, max_rounds=2,
                                      patience=5)
            self.assertEqual((res.stop_reason, res.rounds, res.winner["id"]), ("max_rounds", 2, "v2"))
        with self.subTest("e: regression restores the best-so-far snapshot"):
            restored = []
            res, _, ctx = self.run_form(["w0", "w1"], {"w0": 0.6, "w1": 0.2}, max_rounds=3, restore=restored.append)
            self.assertEqual(res.stop_reason, "regression_reverted")
            self.assertEqual(res.winner["id"], "v0")
            self.assertEqual(res.winner["output"], "w0")
            self.assertEqual([c["id"] for c in res.candidates], ["v0", "v1"])
            self.assertEqual([r["id"] for r in restored], ["v0"])
            # the regressed version never wins and the journal records the stop
            self.assertEqual(self.journal(ctx, "stop")[-1]["reason"], "regression_reverted")

    def test_critic_repair_requires_verifier(self):
        disp = FakeDispatch(lambda *a: "x")
        with self.assertRaises(FormError) as cm:
            critic_repair.run("t", ctx=self.ctx("critic_repair", disp), writer="opencode", critics=["grok"])
        self.assertEqual(cm.exception.code, "verifier_required")
        with self.assertRaises(FormError) as cm:
            critic_repair.run("t", writer="opencode", critics=["grok"], dispatch_fn=disp)
        self.assertEqual(cm.exception.code, "verifier_required")
        self.assertEqual(disp.calls, [])  # nothing was dispatched
        with self.assertRaises(FormError) as cm:
            critic_repair.run("t", ctx=self.ctx("critic_repair", disp, table_verifier({})), writer="opencode",
                              critics=[])
        self.assertEqual(cm.exception.code, "no_providers")

    def test_critic_family_is_different_and_rotates(self):
        fam = {"opencode": "glm", "agy": "gemini", "grok": "grok", "glm2": "glm"}
        with mock.patch("fusion_swarm.adapter.model_family", side_effect=lambda p: fam.get(p, p)):
            res, disp, _ = self.run_form(["w0", "w1", "w2", "w3"], {"w0": 0.1, "w1": 0.2, "w2": 0.3, "w3": 0.4},
                                         max_rounds=3, patience=5, critics=("glm2", "grok", "agy"))
        critics_used = [c["provider"] for c in disp.calls if c["role"] == "critic"]
        self.assertEqual(critics_used, ["grok", "agy", "grok"])  # glm2 shares the writer's family: skipped

    def test_critic_repair_ledger_only_from_verified_verdicts(self):
        ledger = self.ledger()
        disp = self.scripted(["w0", "w1"])
        ctx = self.ctx("critic_repair", disp, table_verifier({"w0": 0.2, "w1": 0.8}, kind="exec"), ledger=ledger)
        critic_repair.run("t", ctx=ctx, writer="opencode", critics=["grok"], max_rounds=1)
        self.assertEqual(ledger.cell("grok", "implement", "critic").n, 1)
        self.assertGreater(ledger.mean("grok", "implement", "critic"), 0.5)  # its critique led to an improvement
        self.assertEqual(ledger.cell("opencode", "implement", "writer").failures, 1)
        # an llm-kind verifier is not verified: the ledger stays untouched
        ledger2 = self.ledger()
        ctx2 = self.ctx("critic_repair", self.scripted(["w0", "w1"]),
                        table_verifier({"w0": 0.2, "w1": 0.8}, kind="llm"), ledger=ledger2)
        critic_repair.run("t", ctx=ctx2, writer="opencode", critics=["grok"], max_rounds=1)
        self.assertEqual(ledger2.cell("grok", "implement", "critic").n, 0)

    def test_writer_failure_and_budget(self):
        disp = FakeDispatch(lambda *a: reply("", ok=False, status="error"))
        res = critic_repair.run("t", ctx=self.ctx("critic_repair", disp, table_verifier({})), writer="opencode",
                                critics=["grok"])
        self.assertEqual((res.status, res.stop_reason, res.winner), ("failed", "writer_failed", None))
        # budget of 2 calls: write + critic, no room to repair
        disp = self.scripted(["w0", "w1"])
        ctx = self.ctx("critic_repair", disp, table_verifier({"w0": 0.3}), max_calls=2)
        res = critic_repair.run("t", ctx=ctx, writer="opencode", critics=["grok"])
        self.assertEqual((res.status, res.stop_reason, res.winner["id"]), ("budget_exhausted", "budget", "v0"))

    def test_salvage_recovers_best_version(self):
        res, _, ctx = self.run_form(["w0", "w1"], {"w0": 0.6, "w1": 0.2})
        out = journal_mod.salvage(ctx.run_dir)
        self.assertEqual(out["best"]["id"], "v0")
        self.assertEqual(out["best"]["output"], "w0")
        self.assertEqual(out["stop_reason"], "regression_reverted")


# ============================================================================================================ pair_swap
BASE = "line1\nline2\nline3\nline4\nline5"


class PairSwapTests(Base):
    def make(self, a="alpha", b="beta", writes=None, reviews=None, scores=None, kind="exec", ledger=None):
        writes = writes or {a: BASE, b: BASE}
        reviews = reviews or {a: BASE, b: BASE}

        def handler(provider, prompt, role, n):
            return (writes if role == "writer" else reviews)[provider]

        disp = FakeDispatch(handler)
        verifier = table_verifier(scores or {}, kind=kind)
        ctx = self.ctx("pair_swap", disp, verifier, max_calls=4, ledger=ledger)
        return disp, verifier, ctx

    def test_pair_swap_auto_direction_from_ledger_and_disable_negative(self):
        ledger = self.ledger()
        for _ in range(3):
            ledger.record_pair("alpha", "beta", "implement", 0.5)
            ledger.record_pair("beta", "alpha", "implement", 0.1)
        disp, _, ctx = self.make(ledger=ledger)
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        self.assertEqual(res.extra["selection"]["directions"], ["ab"])  # never assumes symmetry: higher mean wins
        self.assertEqual([(c["provider"], c["role"]) for c in disp.calls], [("alpha", "writer"), ("beta", "reviewer")])
        self.assertEqual(ledger.pair_stats("alpha", "beta", "implement")["n"], 4)  # 3 + this run's verified delta
        # negative ledger delta disables a pairing: only the other direction runs
        ledger2 = self.ledger()
        for _ in range(3):
            ledger2.record_pair("alpha", "beta", "implement", -0.4)
        disp, _, ctx = self.make(ledger=ledger2)
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        self.assertEqual(res.extra["selection"]["directions"], ["ba"])
        self.assertEqual(res.extra["selection"]["reason"], "other_pairing_disabled")
        self.assertEqual(disp.calls[0]["provider"], "beta")
        self.assertTrue(ledger2.pair_stats("alpha", "beta", "implement")["disabled"])
        # both disabled: the writer works alone (no review step, no pair record)
        ledger3 = self.ledger()
        for _ in range(3):
            ledger3.record_pair("alpha", "beta", "implement", -0.4)
            ledger3.record_pair("beta", "alpha", "implement", -0.2)
        disp, _, ctx = self.make(ledger=ledger3)
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        self.assertEqual(res.extra["selection"]["reason"], "all_pairings_disabled_solo")
        self.assertEqual([c["role"] for c in disp.calls], ["writer"])
        self.assertEqual(ledger3.pair_stats("alpha", "beta", "implement")["n"], 3)
        # no history: explore both directions with the same verifier
        ledger4 = self.ledger()
        disp, verifier, ctx = self.make(ledger=ledger4)
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        self.assertEqual((res.extra["selection"]["reason"], res.extra["selection"]["directions"]),
                         ("explore", ["ab", "ba"]))
        self.assertEqual(len(disp.calls), 4)
        self.assertEqual(sorted(verifier.seen), ["ab-v0", "ab-v1", "ba-v0", "ba-v1"])
        self.assertEqual(ledger4.pair_stats("alpha", "beta", "implement")["n"], 1)
        self.assertEqual(ledger4.pair_stats("beta", "alpha", "implement")["n"], 1)

    def test_pair_swap_records_signed_delta_and_picks_by_score(self):
        ledger = self.ledger()
        # alpha writes 0.2, beta's review lifts it to 0.9; beta writes 0.5, alpha's review hurts it to 0.1
        writes = {"alpha": "A0", "beta": "B0"}
        reviews = {"alpha": "A1", "beta": "B1"}
        scores = {"A0": 0.2, "B1": 0.9, "B0": 0.5, "A1": 0.1}
        disp, _, ctx = self.make(writes=writes, reviews=reviews, scores=scores, ledger=ledger)
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        self.assertEqual(res.winner["id"], "ab-v1")
        self.assertEqual(res.winner["output"], "B1")
        ab = ledger.pair_stats("alpha", "beta", "implement")
        ba = ledger.pair_stats("beta", "alpha", "implement")
        self.assertAlmostEqual(ab["mean_delta"], 0.7)
        self.assertAlmostEqual(ba["mean_delta"], -0.4)
        self.assertEqual(res.status, "ok" if res.winner["verdict"]["passed"] else "failed")

    def test_pair_swap_tie_fewer_lines(self):
        # both reviewed versions tie on score; the one that changed fewer lines vs its writer's draft wins
        writes = {"alpha": BASE, "beta": BASE}
        three = BASE.replace("line1", "L1").replace("line2", "L2").replace("line3", "L3")
        one = BASE.replace("line1", "L1")
        reviews = {"beta": three, "alpha": one}  # ab: beta changes 3 lines; ba: alpha changes 1 line
        scores = {BASE: 0.5, three: 1.0, one: 1.0}
        disp, _, ctx = self.make(writes=writes, reviews=reviews, scores=scores)
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        self.assertEqual(res.winner["id"], "ba-v1")
        self.assertEqual(res.winner["changed_lines"], pair_swap.changed_lines(BASE, one))
        self.assertEqual(pair_swap.changed_lines(BASE, one), 2)    # 1 removed + 1 added
        self.assertEqual(pair_swap.changed_lines(BASE, three), 6)
        # a tie between a draft and its own unchanged review keeps the writer's draft (zero changed lines)
        disp, _, ctx = self.make(scores={BASE: 0.5})
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta", direction="ab")
        self.assertEqual(res.winner["id"], "ab-v0")

    def test_pair_swap_uses_peers_dispatch_by_default(self):
        sentinel = FakeDispatch(lambda *a: BASE)
        ctx = self.ctx("pair_swap", None, table_verifier({BASE: 1.0}), max_calls=4)
        with mock.patch.object(exec_json, "as_dispatch_fn", return_value=sentinel) as factory:
            res = pair_swap.run("task", ctx=ctx, a="claude", b="codex", direction="ab")
        factory.assert_called_once()
        self.assertEqual([c["provider"] for c in sentinel.calls], ["claude", "codex"])
        self.assertEqual(res.status, "ok")
        # an explicit dispatch_fn on the ctx wins, and use_peers=False never touches the peers package
        own = FakeDispatch(lambda *a: BASE)
        ctx = self.ctx("pair_swap", own, table_verifier({BASE: 1.0}), max_calls=4)
        with mock.patch.object(exec_json, "as_dispatch_fn") as factory:
            pair_swap.run("task", ctx=ctx, a="claude", b="codex", direction="ab")
            factory.assert_not_called()
        ctx = self.ctx("pair_swap", None, table_verifier({BASE: 1.0}), max_calls=4)
        with mock.patch.object(exec_json, "as_dispatch_fn") as factory, \
                mock.patch("fusion_swarm.forms.runtime.default_dispatch", return_value=own):
            pair_swap.run("task", ctx=ctx, a="claude", b="codex", direction="ab", use_peers=False)
            factory.assert_not_called()

    def test_pair_swap_routes_through_peers_policy_d2(self):
        calls = []

        def runner(argv, *, cwd, env, input, timeout, cancel):
            calls.append(os.path.basename(argv[0]))
            if os.path.basename(argv[0]) == "claude":
                return 0, json.dumps({"type": "result", "result": "from claude"}), ""
            return 0, json.dumps({"role": "assistant", "content": "from andrewcode"}) + "\n", ""

        verifier = table_verifier({"from claude": 0.4, "from andrewcode": 0.9})
        # Codex host: the claude seat is a real peer; "codex" would be served by andrewcode on a Claude host
        ctx = self.ctx("pair_swap", None, verifier, max_calls=4)
        res = pair_swap.run("task", ctx=ctx, a="claude", b="andrewcode", direction="ab",
                            peer_env={"FUSION_HOST": "codex"}, peer_runner=runner)
        self.assertEqual(calls, ["claude", "andrewcode"])
        self.assertEqual(res.winner["output"], "from andrewcode")
        # Claude host: the claude seat is host-native -> blocked (never raises, never executes); codex is never run
        calls.clear()
        ctx = self.ctx("pair_swap", None, table_verifier({"from andrewcode": 0.9}), max_calls=4)
        res = pair_swap.run("task", ctx=ctx, a="claude", b="andrewcode", direction="both",
                            peer_env={"CLAUDECODE": "1"}, peer_runner=runner)
        self.assertEqual(calls, ["andrewcode"])  # ab: claude write blocked; ba: andrewcode writes, claude review blocked
        by_dir = {d["direction"]: d for d in res.extra["directions"]}
        self.assertEqual(by_dir["ab"]["write_status"], "blocked")
        self.assertEqual(by_dir["ba"]["review_status"], "blocked")
        self.assertEqual(res.winner["id"], "ba-v0")
        self.assertNotIn("recorded", by_dir["ba"])
        self.assertEqual(ctx.ledger.pair_stats("claude", "andrewcode", "implement")["n"], 0)
        # an unknown peer name is "absent", not a crash
        ctx = self.ctx("pair_swap", None, table_verifier({}), max_calls=4)
        res = pair_swap.run("task", ctx=ctx, a="nonesuch", b="grok", direction="both", peer_env={"CLAUDECODE": "1"},
                            peer_runner=runner)
        self.assertEqual((res.status, res.stop_reason, res.winner), ("failed", "no_candidates", None))

    def test_pair_swap_requires_verifier_and_ledger_only_from_verified(self):
        disp = FakeDispatch(lambda *a: BASE)
        with self.assertRaises(FormError) as cm:
            pair_swap.run("t", ctx=self.ctx("pair_swap", disp), a="alpha", b="beta")
        self.assertEqual(cm.exception.code, "verifier_required")
        with self.assertRaises(FormError):
            pair_swap.run("t", a="alpha", b="alpha", verifier=table_verifier({}), dispatch_fn=disp)
        self.assertEqual(disp.calls, [])
        ledger = self.ledger()
        disp, _, ctx = self.make(scores={BASE: 1.0}, kind="llm", ledger=ledger)
        pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        self.assertEqual(ledger.pair_stats("alpha", "beta", "implement")["n"], 0)
        self.assertEqual(ledger.cell("alpha", "implement", "writer").n, 0)

    def test_pair_swap_salvage_and_cancel(self):
        disp, _, ctx = self.make(scores={BASE: 1.0})
        res = pair_swap.run("task", ctx=ctx, a="alpha", b="beta")
        out = journal_mod.salvage(ctx.run_dir)
        self.assertEqual(out["status"], "complete")
        self.assertTrue(out["verified"])
        ctx2 = self.ctx("pair_swap", FakeDispatch(lambda *a: BASE), table_verifier({BASE: 1.0}))
        ctx2.cancel.cancel("stop")
        res = pair_swap.run("task", ctx=ctx2, a="alpha", b="beta")
        self.assertEqual((res.status, res.stop_reason), ("cancelled", "cancelled"))


# ============================================================================================================== delphi
def answer(text, conf=0.8, why="", estimate=None):
    return json.dumps({"answer": text, "estimate": estimate, "confidence": conf, "rationale": why})


class DelphiTests(Base):
    def delphi_ctx(self, script, experts_calls=None, max_calls=None):
        disp = FakeDispatch(lambda provider, prompt, role, n: script(provider, prompt, self.round_of(prompt)))
        return disp, self.ctx("delphi", disp, max_calls=max_calls)

    @staticmethod
    def round_of(prompt):
        return 0 if "Panel summary" not in prompt else 1

    def test_delphi_round0_blind_and_deidentified_summary(self):
        marks = {"alpha": "RATIONALE-ALPHA-7731", "beta": "RATIONALE-BETA-2210", "gamma": "RATIONALE-GAMMA-9904"}

        def script(provider, prompt, rnd):
            if rnd == 0:
                return answer("Yes" if provider != "gamma" else "no", 0.7,
                              f"{marks[provider]}: as Claude and expert 3 and {provider} I think so. "
                              f"My previous answer stands.")
            return answer("Yes", 0.9, "stays " + marks[provider])

        disp, ctx = self.delphi_ctx(script)
        res = delphi.run("Is it safe?", ctx=ctx, experts=["alpha", "beta", "gamma"], rounds=3, seed=11)
        round0 = disp.calls[:3]
        for call in round0:  # blind: no other expert's content, no summary, only the question and the schema
            self.assertNotIn("Panel summary", call["prompt"])
            for m in marks.values():
                self.assertNotIn(m, call["prompt"])
            self.assertIn("Is it safe?", call["prompt"])
        revise = disp.calls[3:6]
        for call in revise:
            p = call["prompt"]
            self.assertIn("Panel summary", p)
            self.assertIn('- "yes": 2', p)   # anonymized histogram, casefolded keys
            self.assertIn('- "no": 1', p)
            for name in ("alpha", "beta", "gamma", "Claude", "claude"):
                self.assertNotIn(name, p.split("Your previous answer")[0].split("QUESTION:")[1].split("Anonymous")[0]
                                 + p.split("Anonymous rationales")[1].split("Your previous answer")[0])
            self.assertNotIn("expert 3", p)
            self.assertNotIn("My previous answer", p)
        # each expert's prompt carries its own previous answer, and only that one
        own = {c["provider"]: c["prompt"] for c in revise}
        self.assertIn('Your previous answer: "Yes"', own["alpha"])
        self.assertIn('Your previous answer: "no"', own["gamma"])
        # rationale text itself reaches the panel (de-identified) so persuasion is possible
        self.assertIn("RATIONALE-ALPHA-7731", own["beta"])
        # same seed -> same shuffle -> identical prompts; the summary lists no provider names even with duplicates
        disp2, ctx2 = self.delphi_ctx(script)
        delphi.run("Is it safe?", ctx=ctx2, experts=["alpha", "beta", "gamma"], rounds=3, seed=11)
        self.assertEqual([c["prompt"] for c in disp.calls[3:6]], [c["prompt"] for c in disp2.calls[3:6]])
        self.assertEqual(res.extra["seed"], 11)

    def test_delphi_deidentify_helper(self):
        text = "As an AI model, claude says Expert #2 and OpenCode agree; I previously said no.  " + "x" * 900
        out = delphi.deidentify(text, ["OpenCode"])
        for bad in ("claude", "OpenCode", "Expert #2", "As an AI", "I previously said"):
            self.assertNotIn(bad, out)
        self.assertLessEqual(len(out), delphi.RATIONALE_CHARS)
        self.assertEqual(delphi.answer_key("  Yes\n\tPlease  "), "yes please")

    def test_delphi_stops_on_stable_histogram_and_reports_dissent(self):
        def script(provider, prompt, rnd):
            if rnd == 0:
                return answer({"alpha": "Blue", "beta": "blue ", "gamma": "Red"}[provider],
                              {"alpha": 0.9, "beta": 0.6, "gamma": 0.4}[provider], f"why {provider}")
            return answer({"alpha": "Blue", "beta": "BLUE", "gamma": "Red"}[provider], 0.7, f"still {provider}")

        disp, ctx = self.delphi_ctx(script)
        res = delphi.run("Color?", ctx=ctx, experts=["alpha", "beta", "gamma"], rounds=5)
        self.assertEqual(res.stop_reason, "stable_histogram")
        self.assertEqual((res.rounds, res.calls, res.status), (2, 6, "ok"))  # histogram unchanged after round 1
        self.assertEqual(res.winner["output"], "Blue")  # casefolded plurality; representative = highest confidence
        self.assertEqual(res.extra["final"]["votes"], 2)
        dissent = res.extra["dissent"]
        self.assertEqual([d["expert"] for d in dissent], ["gamma"])
        self.assertEqual(dissent[0]["answer"], "Red")
        self.assertIn("still", dissent[0]["rationale"])
        self.assertEqual(res.extra["flips"], {"alpha": 0, "beta": 0, "gamma": 0})
        self.assertEqual(len(res.candidates), 3)  # last-round responses

    def test_delphi_flips_ties_and_round_cap(self):
        order = {"alpha": ["A", "B", "A"], "beta": ["B", "B", "B"]}
        seen = {"alpha": 0, "beta": 0}

        def script(provider, prompt, rnd):
            i = seen[provider]
            seen[provider] += 1
            return answer(order[provider][i], 0.5 if provider == "alpha" else 0.9, "r")

        disp, ctx = self.delphi_ctx(script)
        res = delphi.run("Q?", ctx=ctx, experts=["alpha", "beta"], rounds=3)
        # histograms: {a:1,b:1} -> {b:2} -> {a:1,b:1}: never stable, runs to the cap
        self.assertEqual((res.stop_reason, res.rounds, res.calls), ("rounds_reached", 3, 6))
        self.assertEqual(res.extra["flips"], {"alpha": 2, "beta": 0})
        # last round is a 1-1 tie: the higher mean confidence (beta) wins
        self.assertEqual(res.winner["output"], "B")
        self.assertEqual([d["expert"] for d in res.extra["dissent"]], ["alpha"])
        # equal confidence -> lexicographic key
        stats = delphi.facilitate([{"key": "b", "confidence": 0.5, "estimate": None},
                                   {"key": "a", "confidence": 0.5, "estimate": None}], numeric=False)
        self.assertEqual(delphi.plurality_key(stats), "a")

    def test_delphi_numeric_median_iqr(self):
        rounds = {0: [10.0, 12.0, 14.0, 100.0], 1: [11.0, 12.0, 14.0, 20.0], 2: [12.0, 12.0, 14.0, 15.0]}
        names = ["alpha", "beta", "gamma", "delta"]
        count = {n: 0 for n in names}

        def script(provider, prompt, rnd):
            r = count[provider]
            count[provider] += 1
            est = rounds[r][names.index(provider)]
            return answer(f"about {est:g}", 0.5, "numbers", estimate=est)

        disp, ctx = self.delphi_ctx(script)
        res = delphi.run("How many?", ctx=ctx, experts=names, rounds=5, numeric=True, tol=0.5)
        q = statistics.quantiles(rounds[0], n=4)
        self.assertAlmostEqual(res.extra["histograms"][0]["median"], statistics.median(rounds[0]))
        self.assertAlmostEqual(res.extra["histograms"][0]["iqr"], q[2] - q[0])
        revise_prompt = disp.calls[4]["prompt"]
        self.assertIn("Median estimate: 13", revise_prompt)
        self.assertIn(f"interquartile range: {delphi.format_number(q[2] - q[0])}", revise_prompt)
        # round 1: median 13 (change 0) but IQR shrank -> already stable? prev IQR is larger, so yes: stop after round 1
        self.assertEqual((res.stop_reason, res.rounds), ("stable_histogram", 2))
        self.assertEqual(res.winner["estimate"], statistics.median(rounds[1]))  # median of the LAST round
        self.assertEqual(res.winner["output"], delphi.format_number(statistics.median(rounds[1])))
        dissent_estimates = sorted(d["estimate"] for d in res.extra["dissent"])
        med = statistics.median(rounds[1])
        self.assertEqual(dissent_estimates, sorted(x for x in rounds[1] if abs(x - med) > 0.5))
        # IQR increasing blocks the stop even when the median holds
        prev = delphi.facilitate([{"key": str(x), "confidence": 0.5, "estimate": x} for x in (1.0, 2.0, 3.0, 4.0)],
                                 numeric=True)
        wide = delphi.facilitate([{"key": str(x), "confidence": 0.5, "estimate": x} for x in (0.0, 2.0, 3.0, 5.0)],
                                 numeric=True)
        self.assertEqual(prev["median"], wide["median"])
        self.assertGreater(wide["iqr"], prev["iqr"])
        self.assertFalse(delphi.is_stable(prev, wide, numeric=True, tol=0.0))
        self.assertTrue(delphi.is_stable(wide, prev, numeric=True, tol=0.0))

    def test_delphi_numeric_requires_estimate_and_drops_bad_replies(self):
        def script(provider, prompt, rnd):
            if provider == "beta":
                return "I refuse to produce JSON"
            if provider == "gamma":
                return answer("seven", estimate=None)  # numeric panel: estimate is mandatory
            return answer("5", estimate=5.0)

        disp, ctx = self.delphi_ctx(script)
        res = delphi.run("n?", ctx=ctx, experts=["alpha", "beta", "gamma"], rounds=2, numeric=True)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.winner["estimate"], 5.0)
        self.assertEqual(res.extra["histograms"][0]["n"], 1)
        # nobody usable at all: failed, no winner, never a crash
        disp, ctx = self.delphi_ctx(lambda *a: "nope")
        res = delphi.run("n?", ctx=ctx, experts=["alpha", "beta"], rounds=2)
        self.assertEqual((res.status, res.stop_reason, res.winner), ("failed", "no_valid_responses", None))

    def test_delphi_budget_keeps_last_complete_round_and_validation(self):
        def script(provider, prompt, rnd):
            return answer("A" if provider == "alpha" else "B", 0.5, "r")

        disp = FakeDispatch(lambda p, pr, role, n: script(p, pr, 0))
        ctx = self.ctx("delphi", disp, max_calls=3)  # round 0 complete (2 calls), round 1 cut after one call
        res = delphi.run("Q?", ctx=ctx, experts=["alpha", "beta"], rounds=3)
        self.assertEqual((res.status, res.stop_reason, res.rounds), ("budget_exhausted", "budget", 1))
        self.assertIsNotNone(res.winner)  # the last complete round survives
        out = journal_mod.salvage(ctx.run_dir)
        self.assertEqual(out["best"]["output"], res.winner["output"])
        with self.assertRaises(FormError) as cm:
            delphi.run("Q?", ctx=self.ctx("delphi", disp), experts=[])
        self.assertEqual(cm.exception.code, "no_providers")
        for bad in ({"rounds": 0}, {"tol": -1}, {"tol": "x"}):
            with self.assertRaises(FormError) as cm:
                delphi.run("Q?", ctx=self.ctx("delphi", disp), experts=["alpha"], **bad)
            self.assertEqual(cm.exception.code, "invalid_input")

    def test_delphi_default_budget_and_registry(self):
        disp = FakeDispatch(lambda *a: answer("A"))
        res = delphi.run("Q?", experts=["alpha", "beta"], rounds=2, dispatch_fn=disp, ledger=self.ledger())
        self.assertEqual(res.calls, 4)
        self.assertTrue(Path(res.run_dir).is_dir())
        self.assertTrue(str(res.run_dir).startswith(str(self.tmp)))  # FUSION_HOME is the temp dir
        listing = {f["name"]: f for f in forms.list_forms()}
        for name in ("critic_repair", "pair_swap", "delphi"):
            self.assertTrue(listing[name]["available"], listing[name])
        self.assertIs(forms.get_form("critic"), critic_repair)
        self.assertIs(forms.get_form("pair"), pair_swap)


if __name__ == "__main__":
    unittest.main()
