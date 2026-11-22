import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm.forms import FORMS
from fusion_swarm.modes_catalog import RUNTIME_FORM_COMMANDS, all_modes


class V4DocsTests(unittest.TestCase):
    def test_new_commands_have_frontmatter(self):
        for name in (*RUNTIME_FORM_COMMANDS, "vboard", "peers"):
            with self.subTest(command=name):
                text = (ROOT / "commands" / f"{name}.md").read_text()
                self.assertTrue(text.startswith("---\ndescription: "))
                self.assertIn("argument-hint:", "\n".join(text.splitlines()[:6]))

    def test_forms_command_keeps_native_catalog(self):
        text = (ROOT / "commands/forms.md").read_text()
        self.assertIn("swarm.sh forms --json", text)
        self.assertIn("Runtime forms (3.3.0)", text)

    def test_runtime_form_commands_registered(self):
        modes = {mode["mode"]: mode for mode in all_modes()}
        self.assertEqual(set(RUNTIME_FORM_COMMANDS.values()), set(FORMS))
        for name, form in RUNTIME_FORM_COMMANDS.items():
            self.assertEqual(modes[name]["execution_kind"], "runtime-form")
            self.assertEqual(modes[name]["runtime_form"], form)
            self.assertIsNone(modes[name]["engine_pattern"])
        for name in ("vboard", "peers"):
            self.assertEqual(modes[name]["execution_kind"], "tool")
        self.assertEqual(modes["forms"]["execution_kind"], "offline-catalog")

    def test_board_protocol_skill_frontmatter(self):
        skill = ROOT / "skills/fusion-board-protocol/SKILL.md"
        self.assertTrue(skill.read_text().startswith("---\nname: fusion-board-protocol\ndescription: "))

    def test_docs_mention_codex_policy(self):
        peers = (ROOT / "commands/peers.md").read_text()
        self.assertIn("FUSION_ALLOW_CODEX_EXEC=1", peers)
        self.assertIn("AndrewCode has no sandbox", peers)
