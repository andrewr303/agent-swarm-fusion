"""Regression tests for f1 bugs B1, B8, B9a, B11, B16 and the renew_task tool (MCP side)."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm import mcp_server, supervision, team
from fusion_swarm.board import HiveBoard
from fusion_swarm.mcp_server import CoordinationServer, SERVER_VERSION

CAPS = {"host": "local", "source": "local-process", "models": ["m"], "operations": ["dispatch", "wait"]}


def spec(run_id, workspace, prefix):
    return {"run_id": run_id, "task": "t", "workspace": str(workspace), "form": "sidekick",
            "roster": [{"id": "lead", "provider": "native", "model": "m", "role": "lead"},
                       {"id": prefix + "w", "provider": "native", "model": "m"}],
            "jobs": [{"id": prefix + "job", "goal": "do " + prefix, "assigned_to": prefix + "w",
                      "writes": [prefix + ".py"], "success_criteria": ["done"]}]}


def rpc(i, name, **arguments):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class McpFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion f1 mcp ")
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.db = str(self.root / "board.sqlite")
        team.prepare(team.compile_spec(spec("r1", self.ws, "a")), self.db)
        self.server = CoordinationServer(self.db)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def test_b01_unexpected_exception_keeps_request_id(self):
        with patch.object(HiveBoard, "send_message", side_effect=KeyError("coordination_peers")):
            response = self.server.handle(rpc(41, "send_message", from_agent="lead", to_agent="aw",
                                              envelope={}, idempotency_key="k"))
        self.assertEqual(response["id"], 41)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "KeyError: internal error")

    def test_b01_main_keeps_id_when_handle_raises(self):
        line = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
        out = io.StringIO()
        with patch.object(CoordinationServer, "handle", side_effect=RuntimeError("boom")), \
                patch.object(sys, "stdin", io.StringIO(line)), patch.object(sys, "stdout", out):
            mcp_server.main(["--db", self.db])
        reply = json.loads(out.getvalue())
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["error"]["code"], -32603)

    def test_b08_usage_tokens_not_redacted(self):
        data = {"usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
                "totals": {"prompt_tokens": 4}, "completion_tokens": 9}
        self.assertEqual(supervision.redact(data), data)

    def test_b08_secrets_still_redacted(self):
        out = supervision.redact({"lease_token": "x", "claim_token": "y", "api_key": "k", "x-api-key": "k",
                                  "password": "p", "Authorization": "a", "session_cookie": "c", "client_secret": "s",
                                  "note": "Bearer abc.def sk-abcdefghijklmnop"})
        for key in ("lease_token", "claim_token", "api_key", "x-api-key", "password", "Authorization", "session_cookie", "client_secret"):
            self.assertEqual(out[key], "[redacted]", key)
        self.assertNotIn("abc.def", out["note"])
        self.assertNotIn("sk-abcdefghijklmnop", out["note"])

    def test_b11_board_opened_once_per_process(self):
        opened = []
        original = HiveBoard.__init__

        def counting(board, *args, **kwargs):
            opened.append(1)
            original(board, *args, **kwargs)
        with patch.object(HiveBoard, "__init__", counting):
            for i in range(20):
                response = self.server.handle(rpc(i, "stats"))
                self.assertFalse(response["result"]["isError"], response)
        self.assertEqual(len(opened), 1)

    def test_b09_receipts_hides_lease_tokens(self):
        sent = self.server.call("dm", {"from_agent": "lead", "to_agent": "aw", "body": "hello"})
        message_id = sent["id"] if "id" in sent else sent["message_id"]
        claimed = self.server.call("claim", {"agent_id": "aw", "limit": 50})
        self.assertTrue(claimed)
        anonymous = self.server.call("receipts", {"message_id": message_id})
        self.assertTrue(all(not row.get("lease_token") for row in anonymous))
        other = self.server.call("receipts", {"message_id": message_id, "agent_id": "lead"})
        self.assertTrue(all(not row.get("lease_token") for row in other))
        own = self.server.call("receipts", {"message_id": message_id, "agent_id": "aw"})
        self.assertTrue(any(row.get("lease_token") for row in own))

    def test_b04_renew_task_tool_listed_and_works(self):
        listed = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(len(listed), 51)
        self.assertIn("renew_task", {tool["name"] for tool in listed})
        self.server.call("start_task", {"agent_id": "aw", "task_id": "ajob", "lease_seconds": 30})
        before = self.server.call("tasks", {"task_id": "ajob"})[0]
        renewed = self.server.call("renew_task", {"agent_id": "aw", "task_id": "ajob", "attempt": 1, "lease_seconds": 600})
        after = self.server.call("tasks", {"task_id": "ajob"})[0]
        self.assertGreater(after["lease_until"], before["lease_until"])
        self.assertTrue(renewed)

    def test_server_version_constant(self):
        self.assertEqual(SERVER_VERSION, "3.3.0+swarm")
        info = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]["serverInfo"]
        self.assertEqual(info["version"], "3.3.0+swarm")

    def test_b16_supervision_is_run_scoped(self):
        team.prepare(team.compile_spec(spec("r2", self.ws, "b")), self.db, allow_multi_run=True)
        board = self.server._board(self.db)
        board.create_run("lead", "r1", "local", CAPS)
        board.create_run("lead", "r2", "local", CAPS)
        # Mail and a task for run r2 only.
        board.dm("lead", "bw", "r2 brief", task_id="bjob")
        view = supervision.status(board, "lead", "r1")
        self.assertEqual([row for row in view["mail_backlog"] if row["agent_id"] == "bw"], [])
        other = supervision.status(board, "lead", "r2")
        self.assertTrue([row for row in other["mail_backlog"] if row["agent_id"] == "bw"])
        exported = supervision.export(board, "lead", "r1")
        self.assertEqual({item["id"] for item in exported["tasks"]}, {"ajob"})
        self.assertEqual({job["id"] for job in view["dependencies"]}, {"ajob"})


if __name__ == "__main__":
    unittest.main()
