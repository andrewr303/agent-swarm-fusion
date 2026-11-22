"""Exercise the actual newline-delimited stdio transport without a model or SDK."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm.mcp_server import CoordinationServer


class CoordinationMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion mcp ")
        self.workspace = Path(self.tmp.name)
        self.db = str(self.workspace / "coordination.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def exchange(self, requests, *args):
        lines = [json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item for item in requests]
        result = subprocess.run([sys.executable, "-B", str(ROOT / "mcps" / "fusion-coordination" / "server.py"), *args],
            input="\n".join(lines) + "\n", text=True, encoding="utf-8", capture_output=True,
            cwd=self.workspace, timeout=15, env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return [json.loads(line) for line in result.stdout.splitlines()]

    @staticmethod
    def call(i, name, **arguments):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

    def test_initialize_notifications_schema_and_invalid_json_recovery(self):
        results = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "not json",
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}], "--require-board")
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0]["result"]["protocolVersion"], "2025-11-25")
        definitions = results[1]["result"]["tools"]
        self.assertEqual(len({tool["name"] for tool in definitions}), len(definitions))
        self.assertTrue(all("board_path" in tool["inputSchema"]["required"] for tool in definitions))
        self.assertEqual(results[2]["error"]["code"], -32700)
        self.assertEqual(results[3]["result"], {})
        self.assertFalse(Path(self.db).exists())

    def test_durable_unicode_delivery_and_compare_and_swap_across_processes(self):
        seat = lambda name: {"id": name, "provider": "native", "model": "chosen", "role": "worker"}
        results = self.exchange([self.call(1, "register", agent=seat("lead")), self.call(2, "register", agent=seat("worker")),
            self.call(3, "dm", from_agent="lead", to_agent="worker", body="Revue du café", idempotency_key="brief"),
            self.call(4, "dm", from_agent="lead", to_agent="worker", body="Revue du café", idempotency_key="brief"),
            self.call(5, "claim", agent_id="worker"),
            self.call(6, "context_set", agent_id="lead", namespace="run", key="contract", value={"revision": 1}, expected_version=0),
            self.call(7, "context_set", agent_id="worker", namespace="run", key="contract", value={"revision": 2}, expected_version=0)], "--db", self.db)
        self.assertEqual(results[2]["result"]["structuredContent"]["id"], results[3]["result"]["structuredContent"]["id"])
        message = results[4]["result"]["structuredContent"]["items"][0]
        self.assertEqual(message["body"], "Revue du café")
        self.assertTrue(results[-1]["result"]["isError"])
        result = self.exchange([self.call(8, "ack", agent_id="worker", message_id=message["id"],
                                        lease_token=message["delivery"]["lease_token"]),
                                self.call(9, "inbox", agent_id="worker")], "--db", self.db)
        self.assertFalse(result[0]["result"]["isError"])
        self.assertEqual(result[1]["result"]["structuredContent"], {"items": []})

    def test_explicit_boards_are_isolated_and_missing_path_fails_without_side_effects(self):
        server = CoordinationServer(None)
        first = server.handle(self.call(1, "stats"))
        self.assertTrue(first["result"]["isError"])
        self.assertEqual(list(self.workspace.iterdir()), [])
        seat = {"id": "a", "provider": "native", "model": "chosen", "role": "worker"}
        server.call("register", {"board_path": self.db, "agent": seat})
        other = str(self.workspace / "other.sqlite")
        self.assertEqual(server.call("agents", {"board_path": other}), [])
        self.assertEqual(len(server.call("agents", {"board_path": self.db})), 1)

    def test_bound_actor_schema_errors_and_unsafe_override_are_tool_errors(self):
        result = self.exchange([self.call(1, "register", agent={"id": "peer", "provider": "native", "model": "chosen", "role": "worker"}),
            self.call(2, "claim", agent_id="worker", limit=True),
            self.call(3, "stats", board_path=str(self.workspace / "other.sqlite")),
            self.call(4, "unknown"), self.call(5, "stats", unexpected="value")], "--db", self.db, "--agent-id", "worker")
        self.assertTrue(all(item["result"]["isError"] for item in result))
        self.assertFalse(Path(self.db).exists())

    def test_notification_cannot_execute_a_tool(self):
        request = self.call(1, "register", agent={"id": "worker"})
        del request["id"]
        self.assertEqual(self.exchange([request], "--db", self.db), [])
        self.assertFalse(Path(self.db).exists())

    def test_declared_native_and_legacy_configs_boot_the_real_transport(self):
        for file in ("mcp.json", ".mcp.json"):
            with self.subTest(config=file):
                config = json.loads((ROOT / file).read_text(encoding="utf-8"))["mcpServers"]["fusion-coordination"]
                args = [arg.replace("${PLUGIN_ROOT}", str(ROOT)) for arg in config["args"]]
                cwd = ROOT / config.get("cwd", ".")
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                result = subprocess.run([sys.executable, *args], input=json.dumps(request) + "\n",
                    text=True, encoding="utf-8", capture_output=True, cwd=cwd, timeout=10,
                    env={**os.environ, **config.get("env", {})})
                self.assertEqual(result.returncode, 0, result.stderr)
                response = json.loads(result.stdout)
                self.assertTrue(all("board_path" in tool["inputSchema"]["required"] for tool in response["result"]["tools"]))


if __name__ == "__main__":
    unittest.main()
