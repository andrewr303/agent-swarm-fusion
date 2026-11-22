"""Offline exact transport, fail-closed isolation and windowless launch checks."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "metaloop"))
from fusion_swarm import andrewcode_cli as cli, discovery, spawn
from fusion_swarm.host_runtime import CapabilityUnavailable, CodexRuntime, cli_request


class TransportTests(unittest.TestCase):
    def test_exact_node_prompt_file_model_effort_contract(self):
        for alias in ("gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-sol"):
            argv = cli.command(alias, "xhigh", "C:/work/é prompt.txt")
            self.assertEqual(argv, ["node", str(cli.ENTRY), "-m", alias, "--thinking-effort", "xhigh",
                                    "--prompt-file", "C:/work/é prompt.txt", "--output-format", "stream-json"])
            self.assertNotIn("--auto", argv)
            self.assertNotIn("--yolo", argv)
        self.assertEqual(cli.command("gpt-6.1-sol", "low", "p.txt", isolated=True)[-2:], ["--disable-tools", "--ephemeral"])
        self.assertNotIn("--disable-tools", cli.command("gpt-6.1-sol", "low", "p.txt"))
        for effort in ("", "extreme", "XHIGH"):
            with self.assertRaises(ValueError):
                cli.command("gpt-6.1-sol", effort, "prompt.txt")
        with self.assertRaises(ValueError):
            cli.command("gpt-6.1-sol", "xhigh", "prompt.txt", node="codex.exe")

    def test_restricted_hosts_refuse_before_executable_resolution(self):
        for host in ("codex", "andrewcode"):
            with patch("fusion_swarm.host_runtime.executable_argv", side_effect=AssertionError("must not resolve or launch old transport")):
                with self.assertRaisesRegex(CapabilityUnavailable, "No provider was called"):
                    cli_request(host, "unused", {}, explicitly_requested=True)
        native = CodexRuntime(models=["gpt-6-astra"], tools={})
        self.assertEqual(native.host, "codex")

    def test_legacy_panels_use_isolated_andrewcode_never_a_standalone_binary(self):
        try:
            from codefusion import dispatch as legacy
        except ModuleNotFoundError as exc:
            if exc.name != "codefusion":
                raise
            self.skipTest("legacy MetaLoop package is absent from the plugin export")
        cfg = SimpleNamespace(orchestrator_provider="host", panel_timeout_s=60,
                              roster=[{"provider": "codex", "model": "gpt-6-astra", "effort": "xhigh"}])
        seen = {}
        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            Path(argv[3]).with_name("out.txt").write_text("PANEL-OK", encoding="utf-8")
            return 0, "", ""
        with patch.object(legacy, "load_config", return_value=cfg), \
             patch.object(legacy, "_which", side_effect=AssertionError("must not resolve old binary")), \
             patch.object(legacy, "_run", side_effect=fake_run):
            result = legacy.dispatch("codex", "public prompt")
        self.assertEqual((result.ok, result.text, result.model), (True, "PANEL-OK", "gpt-6-astra"))
        argv = seen["argv"]
        self.assertEqual(Path(argv[2]).name, "andrewcode_cli.py")
        self.assertIn("--isolated", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertNotIn("codex", [Path(item).stem.lower() for item in argv])
        cfg.roster = [{"provider": "andrewcode", "model": "", "effort": ""}]
        with patch.object(legacy, "load_config", return_value=cfg), \
             patch.object(legacy, "_run", side_effect=AssertionError("must not call a provider")):
            result = legacy.dispatch("andrewcode", "public prompt")
        self.assertFalse(result.ok)
        self.assertIn("no provider was called", result.error)

    def test_doctor_probes_node_entry_never_standalone_codex(self):
        from fusion_swarm import supervision
        from fusion_swarm.host_runtime import executable_argv
        for binary in ("codex", "codex.exe", "C:/tools/codex.cmd"):
            with self.assertRaises(CapabilityUnavailable):
                executable_argv(binary)
        with patch.object(supervision.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="fixture-version", stderr="")) as run:
            result = supervision.doctor("codex")
        argv = run.call_args.args[0]
        self.assertIn(Path(argv[0]).name.lower(), ("node", "node.exe"))
        self.assertEqual(argv[1:], [str(cli.ENTRY), "--version"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["capabilities"]["installation"]["state"], "observed")

    def test_powershell_wrapper_uses_hidden_noninteractive_launch(self):
        from fusion_swarm.host_runtime import executable_argv
        if os.name != "nt":
            return
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "fixture.ps1"
            wrapper.write_text("# fixture", encoding="utf-8")
            argv = executable_argv(str(wrapper))
        self.assertIn("-NoProfile", argv)
        self.assertIn("-NonInteractive", argv)
        self.assertEqual(argv[argv.index("-WindowStyle") + 1], "Hidden")

    def test_stream_extracts_only_final_assistant_text(self):
        raw = '\n'.join(json.dumps(item) for item in [
            {"role": "assistant", "content": "working", "tool_calls": [{"id": "one"}]},
            {"role": "tool", "content": "do not return this"},
            {"role": "assistant", "content": "answer é"},
            {"role": "meta", "type": "session", "id": "opaque"},
        ])
        self.assertEqual(cli.final_text(raw), "answer é")
        for raw in ("", '{"role":"tool","content":"not an answer"}', 'not json'):
            with self.assertRaises(ValueError):
                cli.final_text(raw)

    def test_stream_errors_reject_an_earlier_assistant_answer_without_leaking_message(self):
        failures = [
            {"role": "error", "message": "private fixture sk-secret-do-not-log"},
            {"type": "turn.failed", "error": {"message": "private fixture"}},
            {"role": "meta", "type": "turn.ended", "state": "failed"},
            {"type": "turn.completed", "result": {"type": "failed", "message": "private fixture"}},
        ]
        for failure in failures:
            with self.subTest(event=failure):
                raw = '\n'.join(json.dumps(item) for item in [
                    {"role": "assistant", "content": "previous answer"}, failure,
                ])
                with self.assertRaisesRegex(ValueError, "AndrewCode stream reported") as caught:
                    cli.final_text(raw)
                self.assertNotIn("private fixture", str(caught.exception))
                self.assertNotIn("sk-secret", str(caught.exception))
        recovered = '\n'.join(json.dumps(item) for item in [
            {"role": "tool", "type": "error", "is_error": True, "content": "recoverable tool failure"},
            {"role": "assistant", "content": "recovered final answer"},
        ])
        self.assertEqual(cli.final_text(recovered), "recovered final answer")

    def test_shell_adapters_return_nonzero_and_preserve_diagnostic(self):
        roots = [ROOT, ROOT.parent / "fusion-ml"]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            prompt = work / "public.txt"
            prompt.write_text("Return OK", encoding="utf-8")
            env = {**os.environ, "FUSION_PYTHON": sys.executable,
                   "FUSION_CONFIG": str(work / "no-config"), "FUSION_ANDREWCODE_AUTO": "1"}
            for root in roots:
                seats = ["andrewcode"] if root == ROOT else ["codex"]
                for seat in seats:
                    with self.subTest(plugin=root.name, seat=seat):
                        adapter = root / f"scripts/providers/{seat}.sh"
                        if root != ROOT and not adapter.is_file():
                            self.skipTest("sibling Fusion plugin is absent from the export")
                        output = work / f"{root.name}-{seat}.out"
                        result = subprocess.run([spawn._find_bash(), str(adapter),
                                                 str(prompt), str(output), "gpt-6-astra", "xhigh"],
                                                capture_output=True, text=True, env=env, timeout=20,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        self.assertIn(result.returncode, (1, 127), result.stderr)
                        self.assertEqual(output.read_text(encoding="utf-8"), "")
                        self.assertIn("provider was called", Path(str(output) + ".log").read_text(encoding="utf-8"))

    def test_nested_worker_prompt_is_utf8_file_and_effort_is_exact(self):
        identity = SimpleNamespace(id="child", model="gpt-6-astra", effort="high", provider="andrewcode",
                                   role="worker", depth=1, parent_id="captain", max_depth=2, spawn_budget=0)
        with tempfile.TemporaryDirectory() as directory:
            seen = {}
            def runner(argv, **kwargs):
                seen.update(argv=argv, **kwargs)
                seen["prompt"] = Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8")
                return SimpleNamespace(pid=1, returncode=0, timed_out=False, absent=False)
            result = spawn.spawn_andrewcode(identity, "public é \" prompt", Path(directory) / "board", directory,
                                             runner=runner)
            self.assertEqual(result.status, "returned")
            self.assertEqual(seen["prompt"], "public é \" prompt")
            self.assertEqual(seen["argv"][seen["argv"].index("--thinking-effort") + 1], "high")
            self.assertEqual(seen["env"]["FUSION_AGENT_ID"], "child")

    def test_default_runner_hides_child_and_converts_recorded_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            output, log = Path(directory) / "out", Path(directory) / "log"
            def popen(argv, **kwargs):
                kwargs["stdout"].write('{"role":"assistant","content":"OK"}\n')
                kwargs["stdout"].flush()
                return SimpleNamespace(pid=123, wait=lambda timeout: 0)
            with patch.object(spawn.subprocess, "Popen", side_effect=popen) as launch:
                result = spawn._subprocess_runner(["node", "fixture"], env={}, cwd=directory, timeout=2,
                                                  out_path=str(output), log_path=str(log))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "OK")
            self.assertIn('"role":"assistant"', Path(str(output) + ".raw").read_text(encoding="utf-8"))
            self.assertEqual(launch.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


    def test_default_runner_rejects_zero_exit_stream_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output, log = Path(directory) / "out", Path(directory) / "log"
            def popen(argv, **kwargs):
                kwargs["stdout"].write('{"role":"assistant","content":"previous answer"}\n'
                                       '{"role":"error","message":"private fixture sk-secret"}\n')
                kwargs["stdout"].flush()
                return SimpleNamespace(pid=123, wait=lambda timeout: 0)
            with patch.object(spawn.subprocess, "Popen", side_effect=popen):
                result = spawn._subprocess_runner(["node", "fixture"], env={}, cwd=directory, timeout=2,
                                                  out_path=str(output), log_path=str(log))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "")
            diagnostic = log.read_text(encoding="utf-8")
            self.assertIn("explicit error or failed turn", diagnostic)
            self.assertNotIn("private fixture", diagnostic)
            self.assertNotIn("sk-secret", diagnostic)


FAKE_ENTRY = r"""
const fs = require('fs'), a = process.argv.slice(2);
if (a.includes('--help')) { console.log(process.env.FAKE_HELP || '--disable-tools --ephemeral'); process.exit(0); }
fs.writeFileSync(process.env.FAKE_LOG, JSON.stringify({argv: a, cwd: process.cwd()}));
if (process.env.FAKE_MODE === 'sleep') setTimeout(() => {}, 60000);
else if (process.env.FAKE_MODE === 'fail') { console.error('401 unauthorized sk-secret'); process.exit(3); }
else console.log(JSON.stringify({role: 'assistant', content: 'FAKE-OK'}));
"""


class IsolatedConsultSeatTests(unittest.TestCase):
    def _run(self, *, mode="ok", help_text=None, sandbox=None, timeout="20"):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "fake.js").write_text(FAKE_ENTRY, encoding="utf-8")
            prompt, output, record = work / "p.txt", work / "out.txt", work / "argv.json"
            prompt.write_text("Return OK", encoding="utf-8")
            env = {**os.environ, "FUSION_PYTHON": sys.executable, "FUSION_ANDREWCODE_ENTRY": str(work / "fake.js"),
                   "FAKE_LOG": str(record), "FAKE_MODE": mode, "FUSION_ANDREWCODE_TIMEOUT": timeout}
            if help_text is not None:
                env["FAKE_HELP"] = help_text
            args = [str(prompt), str(output), "gpt-6.1-sol", "low"] + ([sandbox] if sandbox else [])
            result = subprocess.run([spawn._find_bash(), str(ROOT / "scripts/providers/codex.sh"), *args],
                                    capture_output=True, text=True, env=env, timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            log = Path(str(output) + ".log").read_text(encoding="utf-8")
            return (result.returncode, output.read_text(encoding="utf-8"), log,
                    json.loads(record.read_text(encoding="utf-8")) if record.exists() else None, work)

    def test_codex_seat_runs_tools_disabled_ephemeral_from_an_empty_temp_dir(self):
        code, text, log, seen, work = self._run()
        self.assertEqual((code, text), (0, "FAKE-OK"), log)
        self.assertEqual(seen["argv"][seen["argv"].index("-m") + 1], "gpt-6.1-sol")
        self.assertEqual(seen["argv"][seen["argv"].index("--thinking-effort") + 1], "low")
        self.assertIn("--disable-tools", seen["argv"])
        self.assertIn("--ephemeral", seen["argv"])
        for banned in ("--yolo", "--auto", "-y"):
            self.assertNotIn(banned, seen["argv"])
        self.assertNotEqual(Path(seen["cwd"]).resolve(), Path.cwd().resolve())
        self.assertIn("identity=requested-only", log)

    def test_codex_seat_fails_closed_when_the_build_lacks_isolation_flags(self):
        code, text, log, seen, _ = self._run(help_text="--prompt-file only")
        self.assertEqual((code, text, seen), (1, "", None))
        self.assertIn("no provider was called", log)

    def test_codex_seat_refuses_full_access_and_notes_ignored_workspace_write(self):
        code, _, log, seen, _ = self._run(sandbox="danger-full-access")
        self.assertEqual((code, seen), (1, None))
        self.assertIn("no provider was called", log)
        code, text, log, _, _ = self._run(sandbox="workspace-write")
        self.assertEqual((code, text), (0, "FAKE-OK"))
        self.assertIn("workspace-write was not applied", log)

    def test_codex_seat_timeout_and_failure_withhold_raw_diagnostics(self):
        code, text, log, _, _ = self._run(mode="sleep", timeout="1")
        self.assertEqual((code, text), (124, ""))
        self.assertIn("timed out", log)
        code, text, log, _, _ = self._run(mode="fail")
        self.assertEqual((code, text), (3, ""))
        self.assertIn("authentication", log)
        self.assertNotIn("sk-secret", log)


class DiscoveryTests(unittest.TestCase):
    def test_only_provider_owned_aliases_not_upstream_model_ids_or_credentials(self):
        text = json.dumps({"providers": {"codex": {"api_key": "fixture-secret"}}, "models": {
            "gpt-6-astra": {"provider": "codex", "model": "upstream-id"},
            "gpt-5.6-sol": {"provider": "codex", "model": "different-upstream-id"},
            "other-alias": {"provider": "other"}}})
        self.assertEqual(cli.model_aliases(text), ["gpt-6-astra", "gpt-5.6-sol"])
        old = {"provider": "codex", "status": "live", "models": ["stale"], "cached_at": time.time()}
        with patch.object(discovery, "_load_cache", return_value={"codex": old}), \
             patch.object(discovery, "_save_cache"), patch.object(discovery.shutil, "which", return_value="node"), \
             patch.object(discovery, "_run_probe", return_value=(0, text)) as probe:
            self.assertEqual(discovery.cached("codex")["status"], "unprobed")
            result = discovery.discover("codex")
        self.assertEqual(result["models"], ["gpt-6-astra", "gpt-5.6-sol"])
        self.assertEqual(result["transport"], "andrewcode")
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(probe.call_args.args[0], ["node", cli.ENTRY.as_posix(), "provider", "list", "--json"])

    def test_failed_probe_is_not_live_or_a_model_fallback_call(self):
        with patch.object(discovery, "_load_cache", return_value={}), patch.object(discovery, "_save_cache"), \
             patch.object(discovery.shutil, "which", return_value="node"), \
             patch.object(discovery, "_run_probe", return_value=(1, "private failure")) as probe:
            result = discovery.discover("codex", force=True)
        self.assertEqual(probe.call_count, 1)
        self.assertTrue(result["is_curated"])
        self.assertEqual(result["status"], "missing")
        self.assertNotIn("private failure", json.dumps(result))

    def test_probe_combines_windows_process_flags(self):
        fake = SimpleNamespace(communicate=lambda timeout: ("{}", None), returncode=0)
        with patch.object(discovery.subprocess, "Popen", return_value=fake) as launch:
            self.assertEqual(discovery._run_probe(["node", "fixture"]), (0, "{}"))
        if os.name == "nt":
            self.assertEqual(launch.call_args.kwargs["creationflags"], subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)


class OAuthRefreshTests(unittest.TestCase):
    def test_direct_oauth_provider_keeps_credentials_but_never_launches_refresh_cli(self):
        import importlib.util
        for root in (ROOT, ROOT.parent / "fusion-ml"):
            with self.subTest(plugin=root.name):
                path = root / "ultracode/providers/codex_oauth.py"
                if root != ROOT and not path.is_file():
                    self.skipTest("sibling Fusion plugin is absent from the export")
                spec = importlib.util.spec_from_file_location("fixture_codex_oauth", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with patch.object(module, "_load_auth", return_value={"tokens": {"access_token": "fixture-token"}}), \
                     patch.object(module, "_is_expiring", return_value=False):
                    self.assertEqual(module._access_token(), "fixture-token")
                with patch.object(module.subprocess, "run", side_effect=AssertionError("must not launch credential CLI")):
                    with self.assertRaisesRegex(module.CodexAuthError, "Automatic CLI credential refresh is unavailable"):
                        module._best_effort_refresh()


class LaunchContractTests(unittest.TestCase):
    def test_live_python_launch_sites_have_explicit_hidden_flags(self):
        for directory in (ROOT / "python", ROOT / "scripts", ROOT / "metaloop/codefusion",
                          ROOT / "ultracode", ROOT.parent / "fusion-ml/python",
                          ROOT.parent / "fusion-ml/scripts", ROOT.parent / "fusion-ml/ultracode"):
            for path in directory.rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                       isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and \
                       node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}:
                        keys = {item.arg for item in node.keywords}
                        self.assertTrue("creationflags" in keys or None in keys, f"{path}:{node.lineno}")


if __name__ == "__main__":
    unittest.main()
