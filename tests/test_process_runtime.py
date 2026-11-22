import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm.process_runtime import ProcessRuntime, process_identity
from fusion_swarm.mailbox import BoardError
from fusion_swarm.host_runtime import CapabilityUnavailable, ClaudeCodeRuntime


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion process é ")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.factory = lambda packet: {"argv": [sys.executable, "-B", str(ROOT / "tests/fixtures/runtime_worker.py")], "stdin": json.dumps(packet)}
        self.runtime = ProcessRuntime(str(self.root / "runtime"), models=["fixture"], command=self.factory)
        self.packet = {"ticket_id": "a" * 32, "model": "fixture", "workspace": str(self.workspace), "task_id": "job", "attempt": 1, "write": ["é.txt"]}

    def tearDown(self):
        for handle in list((self.root / "runtime").glob("*/owner.json")):
            if "process:" + handle.parent.name == getattr(self, "uncertain_handle", ""):
                continue
            self.runtime.cancel("process:" + handle.parent.name)
        self.tmp.cleanup()

    def test_real_process_completion_and_restart_do_not_relaunch(self):
        first = self.runtime.dispatch(self.packet)
        restored = ProcessRuntime(str(self.root / "runtime"), models=["fixture"], command=self.factory)
        self.assertTrue(restored.dispatch(self.packet)["reused"])
        result = restored.wait(first["native_handle"], 10000)
        self.assertEqual((result["state"], result["exit_code"], result["model"]), ("stopped", 0, "fixture"))
        self.assertEqual(result["report"]["changed_paths"], ["é.txt"])
        self.assertEqual(self.runtime.wait(first["native_handle"], 10000)["token"], result["token"])
        with self.assertRaises(BoardError):
            restored.dispatch({**self.packet, "write": ["different.txt"]})

    def test_cancel_owned_windows_job_and_timeout_are_observed(self):
        handle = self.runtime.dispatch({**self.packet, "delay": 30})["native_handle"]
        self.assertEqual(self.runtime.cancel(handle)["state"], "cancelled")
        bounded = ProcessRuntime(str(self.root / "runtime"), models=["fixture"], command=lambda packet: {**self.factory(packet), "timeout_seconds": .15})
        handle = bounded.dispatch({**self.packet, "ticket_id": "b" * 32, "delay": 30})["native_handle"]
        result = bounded.wait(handle, 10000)
        self.assertEqual(result["state"], "failed")
        self.assertIn("deadline", result["error"])
        self.assertFalse((self.workspace / "é.txt").exists())

    def test_process_identity_fences_reused_or_missing_pid(self):
        handle = self.runtime.dispatch({**self.packet, "delay": .3})["native_handle"]
        owner_path = self.root / "runtime" / ("a" * 32) / "owner.json"
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        self.assertEqual(process_identity(owner["helper_pid"]), owner["helper_identity"])
        altered = {**owner, "helper_identity": "wrong-creation-time"}
        owner_path.write_text(json.dumps(altered), encoding="utf-8")
        self.assertEqual(self.runtime.status(handle)["state"], "uncertain")
        with self.assertRaises(BoardError):
            self.runtime.cancel(handle)
        owner_path.write_text(json.dumps(owner), encoding="utf-8")
        self.runtime.wait(handle, 10000)

    def test_native_missing_operations_and_claude_require_explicit_request(self):
        with self.assertRaises(CapabilityUnavailable):
            ProcessRuntime(str(self.root), models=["fixture"], host="codex")
        with self.assertRaises(CapabilityUnavailable):
            ClaudeCodeRuntime(models=["selected"], tools={})

    def test_crashed_owned_supervisor_is_uncertain_and_never_relaunched(self):
        import time
        handle = self.runtime.dispatch({**self.packet, "delay": 30})["native_handle"]
        helper = self.runtime._children[self.packet["ticket_id"]]
        child_path = self.root / "runtime" / self.packet["ticket_id"] / "child.json"
        deadline = time.monotonic() + 5
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(child_path.exists())
        helper.kill()  # Only this test's owned supervisor; fault injection.
        helper.wait(timeout=5)
        self.assertIsNone(process_identity(helper.pid))
        self.assertEqual(self.runtime.status(handle)["state"], "uncertain")
        self.assertTrue(self.runtime.dispatch({**self.packet, "delay": 30})["reused"])
        # Windows job lifetime closes the owned child when its supervisor dies.
        child = json.loads(child_path.read_text(encoding="utf-8"))
        if sys.platform == "win32":
            deadline = time.monotonic() + 5
            while process_identity(child["pid"]) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(process_identity(child["pid"]))
        else:
            import os, signal
            if process_identity(child["pid"]) == child["identity"]:
                os.killpg(child["pid"], signal.SIGTERM)
        self.runtime._children.pop(self.packet["ticket_id"])
        # Uncertain supervision intentionally refuses normal cancel/recovery.
        self.packet["ticket_id"] = "b" * 32
        self.uncertain_handle = handle


if __name__ == "__main__":
    unittest.main()
