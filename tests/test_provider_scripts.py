"""Provider adapter scripts against fake CLIs (never a real provider)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROV = ROOT / "scripts" / "providers"
FAKES = ROOT / "tests" / "fixtures" / "fake_cli"
BASH = shutil.which("bash")


class ProviderScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fusion-prov-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "fake.log"
        self.prompt = self.tmp / "prompt.txt"
        self.prompt.write_text("do the thing", encoding="utf-8")
        self.out = self.tmp / "out.txt"

    def env(self, **extra):
        py_dir = str(Path(sys.executable).parent)
        env = {
            "PATH": os.pathsep.join([str(FAKES), py_dir, "/usr/bin", "/bin"]),
            "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.tmp / "xdg"), "FUSION_HOME": str(self.tmp / "fh"),
            "FUSION_CONFIG": str(self.tmp / "no-defaults.env"),
            "FUSION_PYTHON": sys.executable, "FAKE_CLI_LOG": str(self.log),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(extra)
        return env

    def run_adapter(self, name, *args, **env):
        proc = subprocess.run([BASH, str(PROV / f"{name}.sh"), str(self.prompt), str(self.out), *args],
                              env=self.env(**env), capture_output=True, text=True, timeout=60)
        return proc

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    def meta(self):
        return json.loads(Path(str(self.out) + ".meta.json").read_text())

    # ------------------------------------------------------------------ tests
    def test_opencode_uses_model_hash_variant_not_variant_flag(self):
        proc = self.run_adapter("opencode", "opencode-go/glm-5.2", "high")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.calls()[-1]["argv"]
        self.assertNotIn("--variant", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "opencode-go/glm-5.2#high")
        self.assertEqual(self.out.read_text().strip(), "hello opencode")
        # model already carrying a variant, or empty variant, is passed as-is
        self.log.unlink()
        self.run_adapter("opencode", "p/m#max", "high")
        self.assertEqual(self.calls()[-1]["argv"][self.calls()[-1]["argv"].index("-m") + 1], "p/m#max")

    def test_identity_env_exported_to_every_adapter(self):
        ident = {"FUSION_AGENT_ID": "agent-7", "FUSION_BOARD": "/tmp/board.json"}
        for name, extra in (("opencode", {}), ("grok", {}), ("agy", {}), ("kimi", {}),
                            ("fable", {"FUSION_ALLOW_CLAUDE": "1"})):
            with self.subTest(adapter=name):
                if self.log.exists():
                    self.log.unlink()
                proc = self.run_adapter(name, **ident, **extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                call = self.calls()[-1]
                self.assertEqual(call["agent_id"], "agent-7")
                self.assertEqual(call["board_profile"], "agent-7")  # defaults to FUSION_AGENT_ID
                self.assertEqual(call["board"], "/tmp/board.json")
        # an explicit BOARD_PROFILE wins
        self.log.unlink()
        self.run_adapter("grok", FUSION_AGENT_ID="a", BOARD_PROFILE="prof")
        self.assertEqual(self.calls()[-1]["board_profile"], "prof")

    def test_meta_json_written_with_model_and_usage(self):
        expect = {
            "opencode": ("glm-5.2", "ses_fake1", {"input": 10, "output": 5}, {}),
            "grok": ("grok-observed-1", "grok-sess-1", {"input_tokens": 3, "output_tokens": 4}, {}),
            "agy": ("agy-observed-1", "agy-sess-1", {"input_tokens": 3, "output_tokens": 4}, {}),
            "fable": ("claude-fable-5", "claude-sess-1", {"input_tokens": 7, "output_tokens": 9},
                      {"FUSION_ALLOW_CLAUDE": "1"}),
            "kimi": ("azure/kimi-k3", "kimi-sess-1", None, {}),
        }
        for name, (model, sess, usage, extra) in expect.items():
            with self.subTest(adapter=name):
                proc = self.run_adapter(name, **extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                meta = self.meta()
                self.assertEqual(meta["model"], model)
                self.assertEqual(meta["session_id"], sess)
                self.assertEqual(meta["usage"], usage)
                self.assertNotIn("parse_error", meta)
        self.run_adapter("fable", FUSION_ALLOW_CLAUDE="1")
        self.assertEqual(self.meta()["cost_usd"], 0.25)
        self.assertEqual(self.out.read_text().strip(), '{"decision": "approve"}')

    def test_json_out_parse_failure_falls_back_to_raw(self):
        for name, extra in (("opencode", {}), ("grok", {}), ("agy", {}), ("fable", {"FUSION_ALLOW_CLAUDE": "1"})):
            with self.subTest(adapter=name):
                proc = self.run_adapter(name, FAKE_BAD_JSON="1", **extra)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("not json", self.out.read_text())
                self.assertTrue(self.meta().get("parse_error"))
        # direct helper: unknown garbage keeps raw text and flags parse_error
        raw = self.tmp / "raw.txt"
        raw.write_text("plain words only\n")
        out = self.tmp / "direct.txt"
        r = subprocess.run([sys.executable, "-I", str(PROV / "_json_out.py"), "claude", str(raw), str(out)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(out.read_text(), "plain words only\n")
        self.assertTrue(json.loads(Path(str(out) + ".meta.json").read_text())["parse_error"])

    def test_grok_and_agy_feature_detect_json(self):
        for name in ("grok", "agy"):
            with self.subTest(adapter=name, json=True):
                self.log.exists() and self.log.unlink()
                self.assertEqual(self.run_adapter(name).returncode, 0)
                argv = self.calls()[-1]["argv"]
                self.assertEqual(argv[argv.index("--output-format") + 1], "json")
            with self.subTest(adapter=name, json=False):
                self.log.unlink()
                proc = self.run_adapter(name, FAKE_NO_JSON="1")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                argv = self.calls()[-1]["argv"]
                self.assertNotIn("json", argv)
                self.assertEqual(self.out.read_text().strip(), f"hello {name} plain")
                self.assertEqual(self.meta()["model"], self.meta()["model"])  # meta still written
        # kimi: stream-json path only when --prompt-file and stream-json are advertised
        self.log.unlink()
        self.run_adapter("kimi", "azure/kimi-k3", "high")
        argv = self.calls()[-1]["argv"]
        self.assertIn("--prompt-file", argv)
        self.assertEqual(argv[argv.index("--thinking-effort") + 1], "high")
        self.assertEqual(self.out.read_text().strip(), "hello kimi")
        self.log.unlink()
        self.run_adapter("kimi", FAKE_KIMI_LEGACY="1")
        argv = self.calls()[-1]["argv"]
        self.assertIn("-p", argv)
        self.assertNotIn("--prompt-file", argv)
        self.assertEqual(self.out.read_text().strip(), "hello kimi plain")

    def test_fable_gate_still_blocks(self):
        proc = self.run_adapter("fable")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.calls(), [])
        proc = self.run_adapter("fable", FUSION_ALLOW_CLAUDE="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.calls()[-1]["argv"]
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertIn("--disallowedTools", argv)

    def test_exit_codes_unchanged_absent_cli_127(self):
        empty = self.tmp / "emptybin"
        empty.mkdir()
        py_dir = str(Path(sys.executable).parent)
        path = os.pathsep.join([str(empty), py_dir, "/usr/bin", "/bin"])
        for name in ("opencode", "grok", "agy", "kimi", "claude"):
            if shutil.which(name, path=path):
                self.skipTest(f"real {name} on PATH")
        for name, extra in (("opencode", {}), ("grok", {}), ("agy", {}), ("kimi", {}),
                            ("fable", {"FUSION_ALLOW_CLAUDE": "1"})):
            with self.subTest(adapter=name):
                proc = self.run_adapter(name, PATH=path, **extra)
                self.assertEqual(proc.returncode, 127, proc.stderr)


if __name__ == "__main__":
    unittest.main()
