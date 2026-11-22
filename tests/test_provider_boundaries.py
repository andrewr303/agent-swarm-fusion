"""Missing/empty rosters must not launch hidden synthesis or discovery calls."""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from fusion_swarm import discovery, discuss, heavy, moa
from fusion_swarm.__main__ import main


class ProviderBoundaryTests(unittest.TestCase):
    def test_empty_explicit_panel_does_not_rediscover_or_call_a_provider(self):
        for module in (moa, heavy, discuss):
            with self.subTest(pattern=module.__name__):
                with patch.object(module, "available", side_effect=AssertionError("must not discover")), \
                     patch.object(module, "cli_synthesize", side_effect=AssertionError("must not synthesize")):
                    result = module.run("task", providers=[])
                self.assertTrue(result["blocked"])
                self.assertEqual(result["returning"], [])

    def test_failed_detection_blocks_before_question_generation_or_synthesis(self):
        for module in (moa, heavy, discuss):
            with self.subTest(pattern=module.__name__):
                with patch.object(module, "available", return_value=[]), \
                     patch.object(module, "cli_synthesize", side_effect=AssertionError("must not synthesize")):
                    result = module.run("task")
                self.assertTrue(result["blocked"])

    def test_blocked_cli_is_nonzero_and_does_not_record_a_run(self):
        with patch.object(heavy, "available", return_value=[]), \
             patch("fusion_swarm.adapter.record_run", side_effect=AssertionError("must not record")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["heavy", "task", "--json"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(output.getvalue())["blocked"])

    def test_claude_discovery_is_disabled_before_cache_or_cli(self):
        with patch.dict(os.environ, {"FUSION_ALLOW_CLAUDE": "0"}), \
             patch.object(discovery, "_load_cache", side_effect=AssertionError("must not read an old live roster")), \
             patch.object(discovery, "_run_probe", side_effect=AssertionError("must not launch Claude")):
            result = discovery.discover("fable", force=True)
            cached = discovery.cached("fable")
        self.assertTrue(result["disabled"])
        self.assertEqual(result["models"], [])
        self.assertTrue(cached["disabled"])

    def test_empty_discovery_subset_does_not_probe_every_provider(self):
        with patch.object(discovery, "discover", side_effect=AssertionError("must not probe")):
            self.assertEqual(discovery.discover_all([]), {})
        self.assertEqual(discovery.cached_all([]), {})


if __name__ == "__main__":
    unittest.main()
