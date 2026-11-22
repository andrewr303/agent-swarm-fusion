"""carry: FUSION_ANDREWCODE_SELECTION parsing and adapter wiring."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fusion_swarm import adapter, model_selection  # noqa: E402

ENV = model_selection.ENV


def sel(participants=None, roles=None, version=1):
    d = {"version": version, "participants": participants if participants is not None else [
        {"model": "alias-a", "effort": "high"}, {"model": "alias-b", "effort": "low"}]}
    if roles is not None:
        d["roles"] = roles
    return json.dumps(d)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(ENV, None)

    def test_selection_parse_and_validation(self):
        self.assertIsNone(model_selection.selection())
        self.assertIsNone(model_selection.providers())
        self.assertIsNone(model_selection.binding_for("codex"))
        os.environ[ENV] = sel(roles={"architect": {"model": "alias-b", "effort": "max"}})
        self.assertEqual(model_selection.providers(), ["selected-1", "selected-2"])
        self.assertEqual(model_selection.binding_for("selected-2"),
                         {"model": "alias-b", "effort": "low", "role": ""})
        self.assertEqual(model_selection.binding_for("fable"),
                         {"model": "alias-b", "effort": "max", "role": "architect"})
        self.assertEqual(model_selection.binding_for("codex")["model"], "alias-b")  # seat index 1
        self.assertEqual(model_selection.binding_for("andrewcode")["model"], "alias-a")  # index 2 % 2
        bad = [sel(version=2), "[]", sel(participants=[]), sel(participants=[{"model": "x", "effort": "bogus"}]),
               sel(participants=[{"model": " ", "effort": "low"}]),
               sel(roles={"worker": {"model": "outside", "effort": "low"}}),
               sel(participants=[{"model": f"m{i}", "effort": "low"} for i in range(65)])]
        for raw in bad:
            with self.subTest(raw=raw[:30]):
                os.environ[ENV] = raw
                with self.assertRaises(ValueError):
                    model_selection.selection()
        os.environ[ENV] = sel()
        with self.assertRaises(ValueError):
            model_selection.binding_for("selected-9")

    def test_invalid_env_never_echoed(self):
        secret = "SUPER-SECRET-ALIAS-123"
        for raw in ("{not json " + secret, json.dumps({"version": 1, "participants": [{"model": secret, "effort": "zzz"}]}),
                    json.dumps({"version": secret})):
            os.environ[ENV] = raw
            with self.assertRaises(ValueError) as cm:
                model_selection.selection()
            self.assertNotIn(secret, str(cm.exception))
            self.assertIsNone(cm.exception.__cause__)
        # adapter fails closed without raising or echoing
        os.environ[ENV] = "{bad " + secret
        self.assertEqual(adapter.available(), [])
        with mock.patch.object(adapter, "_bash", side_effect=AssertionError("no bash")):
            r = adapter.dispatch("selected-1", "hi")
        self.assertFalse(r.ok)
        self.assertEqual(r.status, "error")
        self.assertNotIn(secret, json.dumps(r.dict()))

    def test_adapter_available_and_dispatch_binding(self):
        os.environ[ENV] = sel()
        bash = mock.Mock(side_effect=AssertionError("fusion.sh path must not be used"))
        calls = []

        def fake_run(pf, of, model, effort, timeout, **kw):
            calls.append((model, effort, timeout, kw))
            Path(of).write_text("answer", encoding="utf-8")
            return 0, "ok"

        with mock.patch.object(adapter, "_bash", bash), \
                mock.patch("fusion_swarm.andrewcode_cli.run_isolated", side_effect=fake_run):
            self.assertEqual(adapter.available(), ["selected-1", "selected-2"])
            r = adapter.dispatch("selected-2", "p", model="ignored", effort="max", timeout=7, role="critic")
            r2 = adapter.dispatch("codex", "p")
            rs = adapter.panel("p", timeout=3)
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "answer")
        self.assertEqual(calls[0][:3], ("alias-b", "low", 7.0))
        self.assertTrue(calls[0][3]["progress"])
        self.assertEqual(calls[0][3]["progress_meta"]["role"], "critic")
        self.assertEqual(calls[1][0], "alias-b")  # codex seat -> participants[1]
        self.assertTrue(r2.ok)
        self.assertEqual([x.provider for x in rs], ["selected-1", "selected-2"])
        bash.assert_not_called()

    def test_adapter_failure_status_mapping(self):
        os.environ[ENV] = sel()
        for rc, status in ((124, "timeout"), (127, "absent"), (1, "error")):
            with mock.patch("fusion_swarm.andrewcode_cli.run_isolated", return_value=(rc, "d")):
                self.assertEqual(adapter.dispatch("selected-1", "p").status, status)
        with mock.patch("fusion_swarm.andrewcode_cli.run_isolated", return_value=(0, "ok")):
            self.assertEqual(adapter.dispatch("selected-1", "p").status, "error")  # empty output

    def test_selected_seat_family_unverified(self):
        self.assertEqual(adapter.model_family("grok"), "grok")
        os.environ[ENV] = sel()
        for p in ("selected-1", "selected-2", "grok", "codex"):
            self.assertEqual(adapter.model_family(p), "unverified")
        self.assertTrue(adapter.same_family("selected-1", "selected-2"))

    def test_unset_env_dispatch_uses_bash(self):
        captured = []

        def fake(argv, timeout=None):
            captured.append(argv)
            Path(argv[4]).write_text("x", encoding="utf-8")
            return 0, "", ""
        with mock.patch.object(adapter, "_bash", fake):
            self.assertTrue(adapter.dispatch("grok", "p").ok)
        self.assertEqual(captured[0][1:3], ["dispatch", "grok"])


class HostDetectionTests(unittest.TestCase):
    def host(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return adapter._resolve_host()

    def test_claude_host_keeps_codex_in_default_panel(self):
        self.assertEqual(self.host(CLAUDECODE="1", FUSION_HOST="codex"), "claude")
        self.assertEqual(self.host(CLAUDECODE="1", FUSION_HOST="codex", FUSION_HOST_EXPLICIT="1"), "codex")
        self.assertEqual(self.host(), "codex")  # legacy default unchanged
        self.assertEqual(self.host(FUSION_HOST="codex"), "codex")
        self.assertIn("codex", [p for p in adapter.ALL_PANEL if p != "claude" and p != "andrewcode"])


if __name__ == "__main__":
    unittest.main()
