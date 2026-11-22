"""carry: FUSION_CHAT_V1 packets and the opt-in run_isolated progress relay."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fusion_swarm import andrewcode_cli, cli_progress  # noqa: E402

META = {"id": "pid1", "role": "worker", "model": "m", "effort": "high"}


def packets(text):
    return [json.loads(l[len(cli_progress.PREFIX):]) for l in text.splitlines() if l.startswith(cli_progress.PREFIX)]


class PacketTests(unittest.TestCase):
    def test_packets_framed_and_clipped(self):
        out = io.StringIO()
        raw = {"role": "assistant", "content": "x" * 20000, "meta": {"auth": "TOKEN"},
               "tool_calls": [{"id": "c" * 300, "function": {"name": "n", "arguments": "a" * 9000}}] * 10}
        msg = cli_progress.relay(json.dumps(raw), stream=out, meta=META)
        (pkt,) = packets(out.getvalue())
        self.assertTrue(out.getvalue().startswith("FUSION_CHAT_V1 "))
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertEqual((pkt["id"], pkt["kind"], pkt["model"], pkt["effort"]), ("pid1", "message", "m", "high"))
        self.assertEqual(len(pkt["message"]["content"]), 12000)
        self.assertEqual(len(pkt["message"]["toolCalls"]), 8)
        self.assertEqual(len(pkt["message"]["toolCalls"][0]["id"]), 100)
        self.assertEqual(len(pkt["message"]["toolCalls"][0]["arguments"]), 4000)
        self.assertNotIn("TOKEN", out.getvalue())
        self.assertEqual(msg["role"], "assistant")
        # tool message, non-chat roles, junk lines
        out = io.StringIO()
        cli_progress.relay(json.dumps({"role": "tool", "content": "r", "tool_call_id": "t1", "is_error": True}),
                           stream=out, meta=META)
        self.assertEqual(packets(out.getvalue())[0]["message"], {"role": "tool", "content": "r", "toolCallId": "t1", "isError": True})
        for junk in ("not json", "[1]", json.dumps({"role": "system", "content": "s"}), json.dumps({"type": "turn.ended"})):
            self.assertIsNone(cli_progress.relay(junk, stream=out, meta=META))
        self.assertEqual(len(packets(out.getvalue())), 1)

    def test_emit_reads_env_and_utf16_clip(self):
        with mock.patch.dict(os.environ, {"FUSION_PROGRESS_ID": "e1", "FUSION_PROGRESS_MODEL": "mm",
                                          "FUSION_PROGRESS_EFFORT": "low", "FUSION_PROGRESS_ROLE": "r"}):
            out = io.StringIO()
            cli_progress.emit("message", _stream=out, message={"x": "☃"})
        self.assertEqual(packets(out.getvalue())[0]["role"], "r")
        self.assertLessEqual(len(cli_progress.clipped("\U0001F600" * 10, 5).encode("utf-16-le")) // 2, 5)


STREAM = [{"role": "assistant", "content": "thinking", "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]},
          {"role": "tool", "content": "res", "tool_call_id": "1"},
          {"role": "assistant", "content": "FINAL ANSWER"}]


class RelayRunTests(unittest.TestCase):
    def run_once(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            entry = d / "main.mjs"
            entry.write_text("// fake entry\n")
            stream = d / "stream.jsonl"
            stream.write_text("".join(json.dumps(e) + "\n" for e in STREAM))
            node = d / "bin" / "node"
            node.parent.mkdir()
            node.write_text("#!/bin/sh\nif [ \"$2\" = \"--help\" ]; then echo '--disable-tools --ephemeral'; exit 0; fi\n"
                            f"cat '{stream}'\n")
            node.chmod(node.stat().st_mode | stat.S_IXUSR)
            pf = d / "p.txt"
            pf.write_text("prompt")
            out = d / "out.txt"
            err = io.StringIO()
            env = {"PATH": f"{node.parent}:{os.environ['PATH']}"}
            with mock.patch.dict(os.environ, env), mock.patch.object(andrewcode_cli, "ENTRY", entry), \
                    mock.patch("sys.stderr", err):
                code, diag = andrewcode_cli.run_isolated(str(pf), str(out), "alias", "high", 20, **kw)
            return code, diag, out.read_text() if out.exists() else None, (Path(str(out) + ".raw").read_text()
                                                                         if Path(str(out) + ".raw").exists() else None), err.getvalue()

    @unittest.skipUnless(os.name == "posix", "posix shell fixture")
    def test_run_isolated_progress_relay_preserves_final_text(self):
        base = self.run_once()
        self.assertEqual(base[0], 0)
        self.assertEqual(base[2], "FINAL ANSWER")
        self.assertEqual(packets(base[4]), [])  # default: no relay
        relayed = self.run_once(progress=True, progress_meta={"role": "critic"})
        self.assertEqual(relayed[:4], base[:4])  # code, diagnostic, final text, raw identical
        pk = packets(relayed[4])
        self.assertEqual([p["message"]["role"] for p in pk], ["assistant", "tool", "assistant"])
        self.assertEqual(pk[0]["role"], "critic")
        self.assertEqual((pk[0]["model"], pk[0]["effort"]), ("alias", "high"))
        self.assertEqual(len({p["id"] for p in pk}), 1)
        self.assertTrue(pk[0]["id"])
        with mock.patch.dict(os.environ, {"FUSION_PROGRESS_ID": "envid"}):
            via_env = self.run_once()
        self.assertEqual(via_env[2], "FINAL ANSWER")
        self.assertEqual(packets(via_env[4])[0]["id"], "envid")


if __name__ == "__main__":
    unittest.main()
