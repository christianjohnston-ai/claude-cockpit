#!/usr/bin/env python3
"""Offline Windows-branch tests. Temporary fake homes/tokens; subprocess and HTTP blocked.

Run: python -m unittest discover -s tests -p test_windows.py
No real CLI, keychain, sign-in, install, or network call is allowed.
"""
import contextlib
import importlib.util
import io
import json
import ntpath
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch, mock_open
import urllib.request

sys.dont_write_bytecode = True
TOOLS = Path(__file__).resolve().parents[1] / "tools"


class WindowsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cockpit-offline-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / ".claude-profiles"
        self.base.mkdir()
        for source in TOOLS.glob("*.py"):
            shutil.copy2(source, self.base / source.name)
        env = {k: v for k, v in os.environ.items()
               if k not in ("COCKPIT_FLEET_CONFIG_DIR", "CLAUDE_CONFIG_DIR", "COCKPIT_CLAUDE_BIN")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, env, clear=True).start()
        # os.path remains host-native so fixture paths work on Linux and Windows.
        patch("sys.platform", "win32").start()
        self.process = patch("subprocess.run", side_effect=AssertionError("Unexpected subprocess")).start()
        self.http = patch.object(urllib.request, "urlopen", side_effect=AssertionError("Unexpected HTTP")).start()
        self.switch = self.load("claude-switch.py")
        self.swap = self.load("claude-swap.py")
        self.launcher = self.load("claude-windows.py")

    def load(self, name):
        spec = importlib.util.spec_from_file_location("test_" + name.replace("-", "_"), self.base / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def profile(self, name="work", token="fake-refresh"):
        directory = self.base / name
        directory.mkdir(exist_ok=True)
        (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": name + "@example.invalid"}}))
        value = {"claudeAiOauth": {"refreshToken": token, "accessToken": "fake-access"},
                 "mcpOAuth": {"fixture": {"accessToken": "fake-mcp"}}}
        (directory / ".credentials.json").write_text(json.dumps(value))
        return directory, value

    def test_native_executable_no_path_search(self):
        self.assertEqual(self.switch.CLAUDE_BIN, str(self.home / ".local" / "bin" / "claude.exe"))
        self.switch.CLAUDE_BIN = "claude.exe"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.switch.require_claude()
        shim = self.home / "claude.cmd"
        shim.write_text("fixture")
        self.switch.CLAUDE_BIN = str(shim)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.switch.require_claude()
        self.process.assert_not_called()

    def test_file_credentials_read_only_and_mcp_preserved(self):
        directory, value = self.profile()
        path = directory / ".credentials.json"
        before = path.read_bytes()
        self.assertEqual(self.switch.slot_for(str(directory)), str(path))
        self.assertEqual(self.swap.Keychain().read(str(path)), value)
        self.assertTrue(self.switch.has_claude_login(str(path)))
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(self.swap.Keychain().read("claude-cockpit-spare-work"))
        self.process.assert_not_called()

    def test_windows_separator_aliases_open_only_allowlisted_store(self):
        # Reproduce Windows expanduser's mixed separators even on a POSIX host.
        mixed_base = r"C:\Users\Fixture/.claude-profiles"
        with patch.object(self.switch, "BASE", mixed_base), \
                patch.object(self.switch, "profiles", return_value=["work"]):
            trusted = self.switch.slot_for(os.path.join(mixed_base, "work"))
            native = ntpath.join(ntpath.normpath(mixed_base), "work", ".credentials.json")
            content = {"claudeAiOauth": {"refreshToken": "fixture-only"}}
            for spelling in (native, native.replace("\\", "/"), native.upper()):
                with patch("builtins.open", mock_open(read_data=json.dumps(content))) as reader:
                    self.assertEqual(self.switch.read_windows_credential(spelling), content)
                    # Normalization only selects an existing allowlist entry. The
                    # caller's alternate casing/spelling is never used for I/O.
                    reader.assert_called_once_with(trusted, encoding="utf-8-sig")
            for denied in (ntpath.join(ntpath.dirname(native), "other.json"),
                           ntpath.join(mixed_base, "other", ".credentials.json"),
                           ntpath.join(mixed_base, "work", "..", "other", ".credentials.json"),
                           r"C:Users\Fixture\.claude-profiles\work\.credentials.json"):
                with patch("builtins.open") as reader:
                    with self.assertRaises(self.switch.CredentialReadError):
                        self.switch.read_windows_credential(denied)
                    reader.assert_not_called()

    def test_missing_is_empty_but_corrupt_and_denied_fail_closed(self):
        directory, _ = self.profile()
        path = directory / ".credentials.json"
        path.unlink()
        self.assertIsNone(self.switch.read_windows_credential(str(path)))
        for malformed in ("{", "[]", '{"claudeAiOauth": "bad"}'):
            path.write_text(malformed)
            with self.assertRaises(self.switch.CredentialReadError):
                self.switch.read_windows_credential(str(path))
            with self.assertRaises(self.swap.CorruptSlot):
                self.swap.Keychain().read(str(path))
        with patch("builtins.open", side_effect=PermissionError("private")):
            with self.assertRaises(self.switch.CredentialReadError):
                self.switch.read_windows_credential(str(path))
        with self.assertRaises(self.switch.CredentialReadError):
            self.switch.read_windows_credential(str(self.home / "unrecognized"))

    def test_windows_swap_cannot_be_enabled_or_called_directly(self):
        config = self.base / "autoswitch.json"
        config.write_text(json.dumps({"swap_enabled": True, "auto_swap": True, "exclude_from_auto": []}))
        self.assertFalse(self.swap.swap_enabled())
        for op, args in (("write", ("any", {})), ("delete", ("any",))):
            with self.assertRaises(self.swap.SwapError):
                getattr(self.swap.Keychain(), op)(*args)
        for dry_run in (False, True):
            with self.assertRaises(self.swap.SwapError):
                self.swap.Swapper(kc=object()).swap("work", dry_run=dry_run)
        watcher = self.load("claude-autoswitch.py")
        config = watcher.cfg()
        self.assertFalse(config["swap_enabled"])
        self.assertFalse(config["auto_swap"])
        with patch.object(watcher, "log") as log:
            watcher.notify("test", "fixture")
            log.assert_called_once()
        self.process.assert_not_called()
        self.http.assert_not_called()

    def test_new_profile_never_copies_credentials_or_shared_config(self):
        (self.home / ".claude.json").write_text('{"apiKey": "fake-secret"}')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.launcher.new_profile("fresh"), 0)
        directory = self.base / "fresh"
        self.assertEqual(json.loads((directory / ".claude.json").read_text()), {})
        self.assertEqual([p.name for p in directory.iterdir()], [".claude.json"])
        with self.assertRaises(FileExistsError):
            self.launcher.new_profile("fresh")
        for name in ("../escape", "", "CON", "aux", "COM1", "LPT9", "has space", "x&y", "__pycache__", "--fleet", "-work"):
            with self.assertRaises(ValueError):
                self.launcher.new_profile(name)
        self.process.assert_not_called()

    def test_profile_creation_failure_removes_only_new_empty_profile(self):
        with patch("builtins.open", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                self.launcher.new_profile("failed")
        self.assertFalse((self.base / "failed").exists())
        with patch("json.dump", side_effect=ValueError("fixture")):
            with self.assertRaises(ValueError):
                self.launcher.new_profile("partial")
        self.assertFalse((self.base / "partial").exists())
        self.profile("existing")
        before = (self.base / "existing" / ".credentials.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.launcher.new_profile("existing")
        self.assertEqual((self.base / "existing" / ".credentials.json").read_bytes(), before)

    def test_corrupt_refresh_is_never_fresh_and_other_profiles_continue(self):
        broken, _ = self.profile("broken")
        healthy, _ = self.profile("healthy")
        (broken / ".credentials.json").write_text("{")
        cockpit = self.load("claude-cockpit.py")
        usage = self.load("claude-usage.py")
        ping = self.load("claude-ping.py")
        for module, refresh in ((cockpit, cockpit.refresh_one), (usage, usage.refresh)):
            module.REFRESH_STATUS["broken"] = "ok"
            refresh("broken")
            self.assertEqual(module.REFRESH_STATUS["broken"], "unreadable")
            self.process.assert_not_called()
            self.http.assert_not_called()
            with patch("subprocess.run") as run:
                run.return_value.stdout = "Current session"
                refresh("healthy")
                self.assertEqual(module.REFRESH_STATUS["healthy"], "ok")
                self.assertEqual(run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], str(healthy))
        with patch.object(cockpit, "active_profile", return_value=None):
            ui = cockpit.UI.__new__(cockpit.UI)
            ui.load()
            row = next(row for row in ui.rows if row["name"] == "broken")
            self.assertEqual(row["status"], "unreadable")
            self.assertFalse(row["trust"])
            ui.BAD = 0
            self.assertIn("unreadable", ui.rowstate(row)[0])
        self.assertFalse(ping.is_cold({"credential_error": "unreadable"}))
        with self.assertRaises(RuntimeError):
            ping._run("broken", ["/usage"])
        self.process.assert_not_called()
        self.http.assert_not_called()

    def test_launch_preserves_arguments_and_restores_environment(self):
        directory, _ = self.profile()
        os.environ["CLAUDE_CONFIG_DIR"] = "parent-pinned"
        args = ["space here", "a&b", "%PATH%", 'say "hi"']
        with patch.object(self.launcher.switch, "require_claude"), patch("subprocess.run") as run:
            run.return_value.returncode = 7
            self.assertEqual(self.launcher.launch("work", args), 7)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], args)
        self.assertEqual(run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], "parent-pinned")

    def test_login_fleet_scrubs_inherited_config_and_checks_identity(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "parent-pinned"
        with patch.object(self.launcher.switch, "require_claude"), patch("subprocess.run") as run, \
                patch.object(self.launcher.switch, "authed_email", return_value="verified@example.invalid") as identity, \
                contextlib.redirect_stdout(io.StringIO()):
            run.return_value.returncode = 0
            self.assertEqual(self.launcher.login("--fleet"), 0)
        self.assertNotIn("CLAUDE_CONFIG_DIR", run.call_args.kwargs["env"])
        self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], "parent-pinned")
        identity.assert_called_once_with(None)
        self.assertEqual(run.call_args.args[0][1:], ["auth", "login"])

    def test_manual_switch_detects_wrong_identity_and_no_convergence_claim(self):
        self.profile()
        messages = []
        with patch.object(self.switch, "slot_fingerprint", side_effect=["old", "new"]), \
                patch.object(self.switch, "fleet_email", side_effect=["previous@example.invalid", "other@example.invalid"]), \
                contextlib.redirect_stdout(io.StringIO()):
            result = self.switch.fleet_switch("work", runner=lambda: None)
        self.assertEqual(result, 3)
        # A failed read stops before the injected login runner, never looks empty.
        (self.base / "work" / ".credentials.json").write_text("{")
        runner = unittest.mock.Mock()
        self.assertEqual(self.switch.windows_browser_switch("work", runner, messages.append), 4)
        runner.assert_not_called()

    def test_manual_switch_success_abort_and_unverified(self):
        self.profile()
        for fingerprints, identities, expected in (
                (["old", "new"], ["previous@example.invalid", "work@example.invalid"], 0),
                (["old", "old"], ["previous@example.invalid", "previous@example.invalid"], 2),
                (["old", "new"], ["previous@example.invalid", None], 4)):
            with patch.object(self.switch, "slot_fingerprint", side_effect=fingerprints), \
                    patch.object(self.switch, "fleet_email", side_effect=identities):
                messages = []
                self.assertEqual(self.switch.windows_browser_switch("work", lambda: None, messages.append), expected)
                self.assertFalse(any("follow within" in message for message in messages))
        self.process.assert_not_called()
        self.http.assert_not_called()

    def test_no_macos_browser_or_process_introspection(self):
        self.assertIsNone(self.switch.chrome_profile_for_email("work@example.invalid"))
        self.assertIsNone(self.switch._process_lanes())
        self.process.assert_not_called()

    def test_cockpit_and_tools_import_with_windows_gates(self):
        # Curses is supplied by windows-curses on Windows; no screen is initialized.
        (self.base / "autoswitch.json").write_text(json.dumps(
            {"swap_enabled": True, "auto_swap": True, "exclude_from_auto": []}))
        cockpit = self.load("claude-cockpit.py")
        self.assertFalse(cockpit.SWITCH_PROVEN_ON)
        self.assertFalse(cockpit.SWAP_PROVEN_ON)
        self.assertFalse(cockpit.swap_on())
        self.assertFalse(cockpit.auto_armed())
        for name in ("claude-usage.py", "claude-ping.py", "claude-meters.py"):
            self.load(name)
        self.process.assert_not_called()
        self.http.assert_not_called()

    def test_fleet_dir_paths_and_env_are_explicit(self):
        directory, _ = self.profile("fleet")
        os.environ["COCKPIT_FLEET_CONFIG_DIR"] = str(directory)
        switch = self.load("claude-switch.py")
        self.assertEqual(switch.FLEET_SLOT, str(directory / ".credentials.json"))
        self.assertEqual(switch.CFG, str(directory / ".claude.json"))
        self.assertEqual(switch.fleet_env({"CLAUDE_CONFIG_DIR": "wrong"})["CLAUDE_CONFIG_DIR"], str(directory))


if __name__ == "__main__":
    unittest.main()
