"""First-run integration must preserve the user's existing Claude settings."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from test_mag import Base, mag


class StatuslineSetup(Base):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="magazine-onboarding-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "claude config"
        self.directory.mkdir()
        self.settings = self.directory / "settings.json"
        self.patch("claude_config_dir", lambda: str(self.directory))

    def install(self):
        return mag.cmd_install_statusline(argparse.Namespace())

    def test_first_run_creates_settings_and_executable_hook(self):
        self.assertEqual(self.install(), 0)
        settings = json.loads(self.settings.read_text())
        suffix = "cmd" if mag.IS_WINDOWS else "sh"
        hook = self.directory / f"statusline-command.{suffix}"
        self.assertTrue(hook.exists())
        self.assertIn(str(hook), settings["statusLine"]["command"])
        if not mag.IS_WINDOWS:
            self.assertTrue(os.access(hook, os.X_OK))

    def test_existing_settings_are_backed_up_and_other_keys_preserved(self):
        original = '{"model":"example-model", "statusLine":{"type":"command","command":"old-status","padding":2}}\n'
        self.settings.write_text(original)
        self.assertEqual(self.install(), 0)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["model"], "example-model")
        self.assertEqual(settings["statusLine"]["padding"], 2)
        backups = list(self.directory.glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(self.install(), 0)
        self.assertEqual(list(self.directory.glob("settings.json.bak.*")), backups)

    def test_invalid_settings_leave_both_settings_and_hook_untouched(self):
        for original in ('{"broken":', '[]', 'null'):
            with self.subTest(original=original):
                self.settings.write_text(original)
                self.assertEqual(self.install(), 1)
                self.assertEqual(self.settings.read_text(), original)
                self.assertEqual(list(self.directory.glob("statusline-command.*")), [])
