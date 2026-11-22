"""Exercise install outcomes and faults in isolated homes; no paid CLI calls."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fusion_install as installer


class InstallerStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion installer storage ")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return installer.install_kimi(ROOT, self.home, **kwargs)

    def test_upgrade_preserves_other_plugins_and_fusion_options(self):
        registry = self.home / "plugins/installed.json"
        registry.parent.mkdir(parents=True)
        unrelated = {"id": "other", "enabled": False, "custom": {"retain": True}}
        registry.write_text(json.dumps({"version": 1, "extra": "keep", "plugins": [unrelated,
            {"id": "fusion", "installedAt": "original-time", "capabilities": {"mcp": "keep"}}]}))
        self.install()
        self.install()
        data = installer.read_object(registry)
        self.assertEqual(data["extra"], "keep")
        self.assertEqual(data["plugins"][0], unrelated)
        record = data["plugins"][1]
        self.assertEqual(record["installedAt"], "original-time")
        self.assertEqual(record["capabilities"], {"mcp": "keep"})
        self.assertEqual(len(data["plugins"]), 2)

    def test_empty_managed_directory_recovers_a_registered_installation(self):
        managed = self.home / "plugins/managed/fusion"
        managed.mkdir(parents=True)
        registry = self.home / "plugins/installed.json"
        unrelated = {"id": "other", "enabled": False, "custom": "keep"}
        installer.atomic_json(registry, {"version": 1, "plugins": [unrelated,
            {"id": "fusion", "root": str(managed), "installedAt": "original-time",
             "capabilities": {"mcp": "keep"}}]})
        self.assertEqual(self.install(), managed)
        self.assertEqual(installer.validate_source(managed)["version"],
                         installer.validate_source(ROOT)["version"])
        records = installer.read_object(registry)["plugins"]
        self.assertEqual(records[0], unrelated)
        self.assertEqual(records[1]["installedAt"], "original-time")
        self.assertEqual(records[1]["capabilities"], {"mcp": "keep"})
        self.assertEqual(len(records), 2)

    def test_empty_managed_directory_is_restored_on_registry_failure(self):
        managed = self.home / "plugins/managed/fusion"
        managed.mkdir(parents=True)
        registry = self.home / "plugins/installed.json"
        installer.atomic_json(registry, {"version": 1, "plugins": []})
        before = registry.read_bytes()
        with patch.object(installer, "atomic_json", side_effect=OSError("injected disk failure")):
            with self.assertRaisesRegex(OSError, "injected disk failure"):
                self.install()
        self.assertTrue(managed.is_dir())
        self.assertEqual(list(managed.iterdir()), [])
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse(list(managed.parent.glob(".fusion-*-*")))

    def test_nonempty_directory_without_a_fusion_manifest_is_preserved(self):
        managed = self.home / "plugins/managed/fusion"
        managed.mkdir(parents=True)
        sentinel = managed / "keep.txt"
        sentinel.write_text("unrelated content")
        registry = self.home / "plugins/installed.json"
        installer.atomic_json(registry, {"version": 1, "plugins": []})
        before = registry.read_bytes()
        with self.assertRaisesRegex(ValueError, "not owned by Fusion"):
            self.install()
        self.assertEqual(sentinel.read_text(), "unrelated content")
        self.assertEqual(registry.read_bytes(), before)

    def test_malformed_registry_keeps_existing_package_and_raw_data(self):
        managed = self.install()
        sentinel = managed / "sentinel.txt"
        sentinel.write_text("old installation")
        registry = self.home / "plugins/installed.json"
        registry.write_text("{broken json")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(registry.read_text(), "{broken json")
        self.assertEqual(sentinel.read_text(), "old installation")

    def test_registry_write_failure_restores_the_previous_package(self):
        managed = self.install()
        (managed / "sentinel.txt").write_text("previous package")
        registry = self.home / "plugins/installed.json"
        before = registry.read_bytes()
        with patch.object(installer, "atomic_json", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual((managed / "sentinel.txt").read_text(), "previous package")
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse(list(managed.parent.glob(".fusion-*-*")))

    def test_concurrent_install_lock_does_not_change_the_registry(self):
        self.install()
        registry = self.home / "plugins/installed.json"
        before = registry.read_bytes()
        with installer.install_lock(self.home / "plugins"):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertEqual(registry.read_bytes(), before)

    def test_install_destination_cannot_replace_the_checkout(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            installer.install_runtime(ROOT, ROOT.parent, sys.executable, None)
        self.assertFalse((ROOT.parent / "fusion-install.json").exists())

    def test_unowned_install_root_is_preserved(self):
        destination = self.root / "program"
        (destination / "bin").mkdir(parents=True)
        (destination / "bin/fusion.cmd").write_text("owned by someone else")
        with self.assertRaisesRegex(ValueError, "unowned"):
            installer.install_runtime(ROOT, destination, sys.executable, None)
        self.assertEqual((destination / "bin/fusion.cmd").read_text(), "owned by someone else")

    def test_legacy_fusion_install_root_without_marker_is_upgraded(self):
        destination = self.root / "program"
        (destination / "plugin").mkdir(parents=True)
        installer.copy_surface(ROOT, destination / "plugin")
        self.assertFalse((destination / "fusion-install.json").exists())
        result = installer.install_runtime(ROOT, destination, sys.executable, None)
        self.assertEqual(Path(result["root"]), destination / "plugin")
        self.assertTrue((destination / "fusion-install.json").is_file())
        self.assertEqual(installer.read_object(destination / "fusion-install.json")["owner"], "fusion-installer")

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary")
    def test_junction_destinations_preserve_their_target(self):
        target = self.root / "actual-target"
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("preserve")
        link = self.root / "alias"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            'New-Item -ItemType Junction -Path $env:FUSION_JUNCTION_LINK -Target $env:FUSION_JUNCTION_TARGET | Out-Null'],
            env={**os.environ, "FUSION_JUNCTION_LINK": str(link), "FUSION_JUNCTION_TARGET": str(target)},
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            with self.assertRaisesRegex(ValueError, "linked"):
                installer.install_runtime(ROOT, link, sys.executable, None)
            with self.assertRaisesRegex(ValueError, "linked"):
                with installer.replace_surface(ROOT, link):
                    self.fail("A linked target must not be replaced")
            self.assertEqual(sentinel.read_text(), "preserve")
        finally:
            # Remove only the junction entry, never recursively traverse it.
            link.rmdir()

    def test_pinned_kimi_mcp_starts_from_its_installed_root(self):
        managed = self.install(python=sys.executable)
        config = installer.read_object(managed / "kimi.plugin.json")["mcpServers"]["fusion-coordination"]
        result = subprocess.run([sys.executable, *config["args"]], cwd=managed / config["cwd"],
            input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            env={**os.environ, **config["env"]},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = {item["name"] for item in json.loads(result.stdout)["result"]["tools"]}
        self.assertTrue({"create_task", "start_task", "submit_task", "review_task", "send_message", "consume_delivery"} <= names)
        self.assertTrue(config["env"]["PATH"].startswith(str(Path(sys.executable).parent)))


@unittest.skipUnless(os.name == "nt", "Windows installers require Windows")
class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion installer [fixture] café ")
        self.root = Path(self.tmp.name)
        self.program = self.root / "program"
        self.shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        self.bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if not self.shell or not self.bash.is_file():
            self.fail("PowerShell and Git for Windows are required to exercise Windows installation")
        self.env = {**os.environ, "PYTHONUTF8": "1", "FUSION_TEST_PYTHON": sys.executable,
            "FUSION_TEST_FIXTURES": str(ROOT / "tests/fixtures"), "FUSION_TEST_ROOT": str(self.root),
            "CODEX_HOME": str(self.root / "codex-home"), "CLAUDE_CONFIG_DIR": str(self.root / "claude-home")}
        self.commands = {}
        for kind in ("codex", "claude", "andrewcode"):
            path = self.root / f"fixture-{kind}.ps1"
            path.write_text(f'& $env:FUSION_TEST_PYTHON -B (Join-Path $env:FUSION_TEST_FIXTURES "install_host.py") {kind} @args\nexit $LASTEXITCODE\n')
            self.commands[kind] = path

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, filename="install-all.ps1", *extra, env=None):
        return subprocess.run([self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / filename),
            "-InstallRoot", str(self.program), "-AndrewCodeHome", str(self.root / "andrewcode-home"),
            "-Python", sys.executable, "-GitBash", str(self.bash), "-CodexCommand", str(self.commands["codex"]),
            "-ClaudeCommand", str(self.commands["claude"]), "-AndrewCodeCommand", str(self.commands["andrewcode"]),
            "-SkipDependencies", "-SkipJq", "-NoPathUpdate", "-NoPause", *extra],
            env=env or self.env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW)

    def report(self):
        return installer.read_object(self.program / "logs/latest-install.json")

    def test_all_hosts_install_and_rerun_preserves_unrelated_entries(self):
        for attempt in range(2):
            result = self.run_install()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = self.report()
            self.assertEqual(report["status"], "complete")
            self.assertEqual({key: value["status"] for key, value in report["hosts"].items()},
                             {"andrewcode": "installed", "claude": "installed", "codex": "installed"})
        for host in ("codex", "claude"):
            data = installer.read_object(self.root / f"{host}-fixture.json")
            self.assertEqual(len(data["installed"]), 1)
            self.assertIn("unrelated", [item["name"] for item in data["marketplaces"]])
            adds = [args for args in data["commands"] if args[:3] == ["plugin", "marketplace", "add"]]
            self.assertEqual(len(adds), 1, "Reinstallation should reuse the same marketplace")
            self.assertFalse(any(args[:2] == ["plugin", "enable"] for args in data["commands"]))
        shim = subprocess.run([self.shell, "-NoProfile", "-File", str(self.program / "bin/fusion.ps1"), "forms", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(shim.returncode, 0, shim.stdout + shim.stderr)
        self.assertEqual(len(json.loads(shim.stdout)["forms"]), 16)

    def test_host_failure_returns_nonzero_and_finishes_other_hosts(self):
        result = self.run_install(env={**self.env, "FUSION_TEST_FAIL": "claude:plugin install"})
        self.assertNotEqual(result.returncode, 0)
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["hosts"]["claude"]["status"], "failed")
        self.assertEqual(report["hosts"]["andrewcode"]["status"], "installed")
        self.assertEqual(report["hosts"]["codex"]["status"], "installed")
        self.assertIn("Injected host installation failure", report["errors"][0])

    def test_stale_cached_code_is_not_reported_as_success(self):
        result = self.run_install("install-codex.ps1", env={**self.env, "FUSION_TEST_CORRUPT": "codex"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.report()["hosts"]["codex"]["status"], "failed")
        self.assertIn("does not match", " ".join(self.report()["errors"]))

    def test_locked_codex_cache_preserves_installation_and_reports_recovery(self):
        result = self.run_install("install-codex.ps1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = installer.read_object(self.root / "codex-fixture.json")
        cached = Path(before["installed"][0]["installPath"])
        sentinel = cached / "preserve.txt"
        sentinel.write_text("active cache")
        result = self.run_install("install-codex.ps1", env={**self.env, "FUSION_TEST_CACHE_LOCK": "1"})
        self.assertNotEqual(result.returncode, 0)
        report = self.report()
        self.assertEqual(report["hosts"]["codex"]["status"], "failed")
        failure = " ".join(report["errors"])
        self.assertIn("Access is denied", failure)
        self.assertIn("Close Codex completely", failure)
        self.assertIn("install-codex.ps1", failure)
        after = installer.read_object(self.root / "codex-fixture.json")
        self.assertEqual(after["installed"], before["installed"])
        self.assertEqual(after["marketplaces"], before["marketplaces"])
        self.assertEqual(sentinel.read_text(), "active cache")
        self.assertFalse(any(args[:3] == ["plugin", "marketplace", "remove"]
                             for args in after["commands"][len(before["commands"]):]))

    def test_dry_run_calls_no_host_and_creates_no_installation(self):
        result = self.run_install("install-all.ps1", "-DryRun")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.program.exists())
        self.assertFalse(list(self.root.glob("*-fixture.json")))

    def test_claude_local_source_is_verified_even_when_inventory_cache_is_stale(self):
        env = {**self.env, "FUSION_TEST_CLAUDE_LOAD_IN_PLACE": "1"}
        result = self.run_install("install-claude-code.ps1", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = installer.read_object(self.root / "claude-fixture.json")
        cached = Path(state["installed"][0]["installPath"])
        (cached / "python/fusion_swarm/team.py").write_text("# old cache is not the loaded source\n", encoding="utf-8")
        result = self.run_install("install-claude-code.ps1", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        verified = installer.verify_host("claude", self.program / "logs/claude-plugins.json",
            self.program / "plugin", None, "user", self.program / "logs/claude-marketplaces.json")
        self.assertEqual(verified["loadMode"], "local-source")
        self.assertEqual(Path(verified["installedRoot"]), self.program / "plugin")
        self.assertIn("old cache", (cached / "python/fusion_swarm/team.py").read_text())
        listing = self.program / "logs/claude-plugins.json"
        data = json.loads(listing.read_text(encoding="utf-8-sig"))
        data[0]["mcpServers"]["fusion-coordination"]["args"] = ["wrong-server.py"]
        listing.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "MCP declaration"):
            installer.verify_host("claude", listing, self.program / "plugin", None, "user",
                                  self.program / "logs/claude-marketplaces.json")

    def test_host_wrapper_only_registers_its_requested_host(self):
        managed = self.root / "andrewcode-home/plugins/managed/fusion"
        managed.mkdir(parents=True)
        registry = self.root / "andrewcode-home/plugins/installed.json"
        installer.atomic_json(registry, {"version": 1, "plugins": [
            {"id": "fusion", "root": str(managed), "installedAt": "original-time"},
            {"id": "unrelated", "enabled": False}]})
        result = self.run_install("install-andrewcode.ps1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report()["requestedHosts"], ["andrewcode"])
        self.assertFalse((self.root / "codex-fixture.json").exists())
        self.assertFalse((self.root / "claude-fixture.json").exists())
        self.assertTrue((self.root / "andrewcode-home/plugins/managed/fusion/install-all.ps1").is_file())
        records = installer.read_object(registry)["plugins"]
        self.assertEqual(len(records), 2)
        self.assertEqual(next(item for item in records if item["id"] == "fusion")["installedAt"],
                         "original-time")
        self.assertEqual(next(item for item in records if item["id"] == "unrelated"),
                         {"id": "unrelated", "enabled": False})

    def test_existing_fusion_marketplace_can_be_relocated_without_duplicates(self):
        old = self.root / "old-plugin"
        old.mkdir()
        installer.copy_surface(ROOT, old)
        for host in ("codex", "claude"):
            installer.atomic_json(self.root / f"{host}-fixture.json", {"commands": [], "installed": [],
                "marketplaces": [{"name": "fusion-hive", "root": str(old), "path": str(old)},
                                 {"name": "other", "root": "other", "path": "other"}]})
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for host in ("codex", "claude"):
            state = installer.read_object(self.root / f"{host}-fixture.json")
            self.assertEqual(len(state["marketplaces"]), 2)
            fusion = next(item for item in state["marketplaces"] if item["name"] == "fusion-hive")
            self.assertEqual(Path(fusion["root"]), self.program / "plugin")
        self.assertTrue((old / "plugin.json").is_file(), "Relocation must not delete the original checkout")

    def test_failed_relocation_restores_the_previous_fusion_marketplace(self):
        old = self.root / "old-plugin"
        old.mkdir()
        installer.copy_surface(ROOT, old)
        original = (old / "python/fusion_swarm/team.py").read_bytes()
        installer.atomic_json(self.root / "codex-fixture.json", {"commands": [], "installed": [],
            "marketplaces": [{"name": "fusion-hive", "root": str(old), "path": str(old)}]})
        result = self.run_install("install-codex.ps1", env={**self.env,
            "FUSION_TEST_CORRUPT": "codex", "FUSION_TEST_CORRUPT_ROOT": str(self.program / "plugin")})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.report()["status"], "failed")
        state = installer.read_object(self.root / "codex-fixture.json")
        self.assertEqual(Path(state["marketplaces"][0]["root"]), old)
        cache = Path(state["installed"][0]["installPath"])
        self.assertEqual((cache / "python/fusion_swarm/team.py").read_bytes(), original)
        self.assertEqual((old / "python/fusion_swarm/team.py").read_bytes(), original)

    def test_shared_marketplace_is_preserved_instead_of_removed(self):
        old = self.root / "shared-marketplace"
        (old / ".claude-plugin").mkdir(parents=True)
        installer.atomic_json(old / ".claude-plugin/marketplace.json", {"name": "fusion-hive", "plugins": [
            {"name": "fusion", "source": "./"}, {"name": "other", "source": "./other"}]})
        installer.atomic_json(self.root / "codex-fixture.json", {"commands": [], "installed": [],
            "marketplaces": [{"name": "fusion-hive", "root": str(old), "path": str(old)}]})
        result = self.run_install("install-codex.ps1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not exclusively owned", " ".join(self.report()["errors"]))
        state = installer.read_object(self.root / "codex-fixture.json")
        self.assertEqual(Path(state["marketplaces"][0]["root"]), old)
        self.assertFalse(any(args[:3] in (["plugin", "marketplace", "remove"],
                                         ["plugin", "marketplace", "add"]) for args in state["commands"]))

    def test_dependency_installer_preserves_arguments_and_reports_a_real_exit_failure(self):
        fixture = self.root / "fixture-winget.ps1"
        (self.root / "fixture-winget.py").write_text(
            'import json, os, sys\nfrom pathlib import Path\n'
            'Path(os.environ["FUSION_TEST_ROOT"]).joinpath("winget-args.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")\n'
            'if os.environ.get("FUSION_TEST_WINGET_FAIL"):\n'
            '    print("Injected package download failure", file=sys.stderr)\n    raise SystemExit(7)\n'
            'print("Fixture package installed")\n', encoding="utf-8")
        fixture.write_text(
            '& $env:FUSION_TEST_PYTHON -B (Join-Path $env:FUSION_TEST_ROOT "fixture-winget.py") @args\n'
            'exit $LASTEXITCODE\n', encoding="utf-8")
        harness = self.root / "dependency-harness.ps1"
        harness.write_text(
            '. $env:FUSION_TEST_INSTALLER -InstallRoot (Join-Path $env:FUSION_TEST_ROOT "program") '
            '-AndrewCodeHome (Join-Path $env:FUSION_TEST_ROOT "andrewcode-home")\n'
            'function Resolve-Executable([string]$Name) { return $env:FUSION_TEST_WINGET }\n'
            'try { Install-WingetPackage "Python.Python.3.14"; exit 0 } '
            'catch { Write-Host $_.Exception.Message; exit 1 }\n', encoding="utf-8")
        env = {**self.env, "FUSION_TEST_INSTALLER": str(ROOT / "install.ps1"), "FUSION_TEST_WINGET": str(fixture)}
        for fail in (False, True):
            result = subprocess.run([self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                env={**env, "FUSION_TEST_WINGET_FAIL": "1" if fail else ""}, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 1 if fail else 0, result.stdout + result.stderr)
            recorded = json.loads((self.root / "winget-args.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(recorded[:6], ["install", "--id", "Python.Python.3.14", "--exact", "--source", "winget"])
            self.assertIn("--disable-interactivity", recorded)
            if fail:
                self.assertIn("exit 7", result.stdout)
                self.assertIn("Injected package download failure", result.stdout)


@unittest.skipIf(os.name == "nt", "POSIX installers require macOS/Linux")
class PosixInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion installer [fixture] café ")
        self.root = Path(self.tmp.name)
        self.program = self.root / "program"
        self.bash = shutil.which("bash") or "/bin/bash"
        self.env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "FUSION_TEST_PYTHON": sys.executable,
            "FUSION_TEST_FIXTURES": str(ROOT / "tests/fixtures"),
            "FUSION_TEST_ROOT": str(self.root),
            "CODEX_HOME": str(self.root / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-home"),
        }
        self.commands = {}
        for kind in ("codex", "claude", "andrewcode"):
            path = self.root / f"fixture-{kind}"
            path.write_text(
                "#!/usr/bin/env bash\n"
                f'exec "$FUSION_TEST_PYTHON" -B "$FUSION_TEST_FIXTURES/install_host.py" {kind} "$@"\n',
                encoding="utf-8",
            )
            path.chmod(0o755)
            self.commands[kind] = path

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, filename="install-all.command", *extra, env=None):
        return subprocess.run(
            [
                self.bash,
                str(ROOT / filename),
                "--install-root", str(self.program),
                "--andrewcode-home", str(self.root / "andrewcode-home"),
                "--python", sys.executable,
                "--bash", str(self.bash),
                "--codex-command", str(self.commands["codex"]),
                "--claude-command", str(self.commands["claude"]),
                "--andrewcode-command", str(self.commands["andrewcode"]),
                "--skip-dependencies",
                "--skip-jq",
                "--no-path-update",
                "--no-pause",
                *extra,
            ],
            env=env or self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def report(self):
        return installer.read_object(self.program / "logs/latest-install.json")

    def test_all_hosts_install_and_rerun_preserves_unrelated_entries(self):
        for _ in range(2):
            result = self.run_install()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = self.report()
            self.assertEqual(report["status"], "complete")
            self.assertEqual(
                {key: value["status"] for key, value in report["hosts"].items()},
                {"andrewcode": "installed", "claude": "installed", "codex": "installed"},
            )
        for host in ("codex", "claude"):
            data = installer.read_object(self.root / f"{host}-fixture.json")
            self.assertEqual(len(data["installed"]), 1)
            self.assertIn("unrelated", [item["name"] for item in data["marketplaces"]])
            adds = [args for args in data["commands"] if args[:3] == ["plugin", "marketplace", "add"]]
            self.assertEqual(len(adds), 1, "Reinstallation should reuse the same marketplace")
            self.assertFalse(any(args[:2] == ["plugin", "enable"] for args in data["commands"]))
        shim = subprocess.run(
            [str(self.program / "bin/fusion"), "forms", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(shim.returncode, 0, shim.stdout + shim.stderr)
        self.assertEqual(len(json.loads(shim.stdout)["forms"]), 16)

    def test_host_failure_returns_nonzero_and_finishes_other_hosts(self):
        result = self.run_install(env={**self.env, "FUSION_TEST_FAIL": "claude:plugin install"})
        self.assertNotEqual(result.returncode, 0)
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["hosts"]["claude"]["status"], "failed")
        self.assertEqual(report["hosts"]["andrewcode"]["status"], "installed")
        self.assertEqual(report["hosts"]["codex"]["status"], "installed")
        self.assertIn("Injected host installation failure", report["errors"][0])

    def test_stale_cached_code_is_not_reported_as_success(self):
        result = self.run_install("install-codex.command", env={**self.env, "FUSION_TEST_CORRUPT": "codex"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.report()["hosts"]["codex"]["status"], "failed")
        self.assertIn("does not match", " ".join(self.report()["errors"]))

    def test_locked_codex_cache_preserves_installation_and_reports_recovery(self):
        result = self.run_install("install-codex.command")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = installer.read_object(self.root / "codex-fixture.json")
        cached = Path(before["installed"][0]["installPath"])
        sentinel = cached / "preserve.txt"
        sentinel.write_text("active cache")
        result = self.run_install("install-codex.command", env={**self.env, "FUSION_TEST_CACHE_LOCK": "1"})
        self.assertNotEqual(result.returncode, 0)
        report = self.report()
        self.assertEqual(report["hosts"]["codex"]["status"], "failed")
        failure = " ".join(report["errors"])
        self.assertIn("Access is denied", failure)
        self.assertIn("Close Codex completely", failure)
        self.assertIn("install-codex.command", failure)
        after = installer.read_object(self.root / "codex-fixture.json")
        self.assertEqual(after["installed"], before["installed"])
        self.assertEqual(after["marketplaces"], before["marketplaces"])
        self.assertEqual(sentinel.read_text(), "active cache")
        self.assertFalse(
            any(args[:3] == ["plugin", "marketplace", "remove"] for args in after["commands"][len(before["commands"]):])
        )

    def test_dry_run_calls_no_host_and_creates_no_installation(self):
        result = self.run_install("install.command", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.program.exists())
        self.assertFalse(list(self.root.glob("*-fixture.json")))

    def test_claude_local_source_is_verified_even_when_inventory_cache_is_stale(self):
        env = {**self.env, "FUSION_TEST_CLAUDE_LOAD_IN_PLACE": "1"}
        result = self.run_install("install-claude-code.command", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = installer.read_object(self.root / "claude-fixture.json")
        cached = Path(state["installed"][0]["installPath"])
        (cached / "python/fusion_swarm/team.py").write_text("# old cache is not the loaded source\n", encoding="utf-8")
        result = self.run_install("install-claude-code.command", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        verified = installer.verify_host(
            "claude",
            self.program / "logs/claude-plugins.json",
            self.program / "plugin",
            None,
            "user",
            self.program / "logs/claude-marketplaces.json",
        )
        self.assertEqual(verified["loadMode"], "local-source")
        self.assertEqual(Path(verified["installedRoot"]), self.program / "plugin")
        self.assertIn("old cache", (cached / "python/fusion_swarm/team.py").read_text())
        listing = self.program / "logs/claude-plugins.json"
        data = json.loads(listing.read_text(encoding="utf-8-sig"))
        data[0]["mcpServers"]["fusion-coordination"]["args"] = ["wrong-server.py"]
        listing.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "MCP declaration"):
            installer.verify_host(
                "claude",
                listing,
                self.program / "plugin",
                None,
                "user",
                self.program / "logs/claude-marketplaces.json",
            )

    def test_host_wrapper_only_registers_its_requested_host(self):
        managed = self.root / "andrewcode-home/plugins/managed/fusion"
        managed.mkdir(parents=True)
        registry = self.root / "andrewcode-home/plugins/installed.json"
        installer.atomic_json(
            registry,
            {
                "version": 1,
                "plugins": [
                    {"id": "fusion", "root": str(managed), "installedAt": "original-time"},
                    {"id": "unrelated", "enabled": False},
                ],
            },
        )
        result = self.run_install("install-andrewcode.command")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report()["requestedHosts"], ["andrewcode"])
        self.assertFalse((self.root / "codex-fixture.json").exists())
        self.assertFalse((self.root / "claude-fixture.json").exists())
        self.assertTrue((self.root / "andrewcode-home/plugins/managed/fusion/install-all.command").is_file())
        records = installer.read_object(registry)["plugins"]
        self.assertEqual(len(records), 2)
        self.assertEqual(next(item for item in records if item["id"] == "fusion")["installedAt"], "original-time")
        self.assertEqual(next(item for item in records if item["id"] == "unrelated"), {"id": "unrelated", "enabled": False})

    def test_existing_fusion_marketplace_can_be_relocated_without_duplicates(self):
        old = self.root / "old-plugin"
        old.mkdir()
        installer.copy_surface(ROOT, old)
        for host in ("codex", "claude"):
            installer.atomic_json(
                self.root / f"{host}-fixture.json",
                {
                    "commands": [],
                    "installed": [],
                    "marketplaces": [
                        {"name": "fusion-hive", "root": str(old), "path": str(old)},
                        {"name": "other", "root": "other", "path": "other"},
                    ],
                },
            )
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for host in ("codex", "claude"):
            state = installer.read_object(self.root / f"{host}-fixture.json")
            self.assertEqual(len(state["marketplaces"]), 2)
            fusion = next(item for item in state["marketplaces"] if item["name"] == "fusion-hive")
            self.assertEqual(Path(fusion["root"]), self.program / "plugin")
        self.assertTrue((old / "plugin.json").is_file(), "Relocation must not delete the original checkout")

    def test_failed_relocation_restores_the_previous_fusion_marketplace(self):
        old = self.root / "old-plugin"
        old.mkdir()
        installer.copy_surface(ROOT, old)
        original = (old / "python/fusion_swarm/team.py").read_bytes()
        installer.atomic_json(
            self.root / "codex-fixture.json",
            {
                "commands": [],
                "installed": [],
                "marketplaces": [{"name": "fusion-hive", "root": str(old), "path": str(old)}],
            },
        )
        result = self.run_install(
            "install-codex.command",
            env={
                **self.env,
                "FUSION_TEST_CORRUPT": "codex",
                "FUSION_TEST_CORRUPT_ROOT": str(self.program / "plugin"),
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.report()["status"], "failed")
        state = installer.read_object(self.root / "codex-fixture.json")
        self.assertEqual(Path(state["marketplaces"][0]["root"]), old)
        cache = Path(state["installed"][0]["installPath"])
        self.assertEqual((cache / "python/fusion_swarm/team.py").read_bytes(), original)
        self.assertEqual((old / "python/fusion_swarm/team.py").read_bytes(), original)

    def test_shared_marketplace_is_preserved_instead_of_removed(self):
        old = self.root / "shared-marketplace"
        (old / ".claude-plugin").mkdir(parents=True)
        installer.atomic_json(
            old / ".claude-plugin/marketplace.json",
            {
                "name": "fusion-hive",
                "plugins": [{"name": "fusion", "source": "./"}, {"name": "other", "source": "./other"}],
            },
        )
        installer.atomic_json(
            self.root / "codex-fixture.json",
            {
                "commands": [],
                "installed": [],
                "marketplaces": [{"name": "fusion-hive", "root": str(old), "path": str(old)}],
            },
        )
        result = self.run_install("install-codex.command")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not exclusively owned", " ".join(self.report()["errors"]))
        state = installer.read_object(self.root / "codex-fixture.json")
        self.assertEqual(Path(state["marketplaces"][0]["root"]), old)
        self.assertFalse(
            any(args[:3] in (["plugin", "marketplace", "remove"], ["plugin", "marketplace", "add"]) for args in state["commands"])
        )


if __name__ == "__main__":
    unittest.main()
