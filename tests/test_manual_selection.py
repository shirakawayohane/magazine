"""A deliberate account choice survives preventive switching and old cooldowns."""
import argparse
import contextlib
import io
import time
from unittest.mock import patch

from test_mag import Base, mag


class Fixture(Base):
    """Accounts and stubs shared with other test modules (no tests here)."""

    def setUp(self):
        super().setUp()
        for slug in ("main", "fourdesign"):
            self.add_account(slug)
        for slug in ("cx-main", "cx-spare"):
            self.add_account(slug, provider="codex")
        self.patch("stored_oauth", lambda slug: {"accessToken": slug})
        self.patch("ensure_fresh", lambda slug, oauth: oauth)
        self.patch("sync_live_credentials", lambda: None)
        self.patch("install_oauth", lambda oauth: None)
        self.patch("codex_stored_auth", lambda slug: {"tokens": {"access_token": slug, "account_id": slug}})
        self.patch("codex_live_auth", lambda: {"tokens": {"account_id": mag.get_current("codex")}})
        self.patch("codex_apply_selection", lambda: True)
        self.patch("codex_ensure_fresh", lambda slug, auth, **kw: (auth, None, True))
        self.patch("codex_sync_live", lambda: None)
        self.patch("codex_install_auth", lambda auth: None)
        self.patch("print_codex_switch_note", lambda *args: None)
        self.relay_calls = []                          # `mag main` turns the relay on; tested in test_relay
        self.patch("relay_turn_on", lambda: self.relay_calls.append("on") or 0)
        self.patch("relay_turn_off", lambda: self.relay_calls.append("off") or 0)
        self.patch("codex_relay_turn_on", lambda: self.relay_calls.append("codex-on") or 0)
        self.patch("codex_relay_turn_off", lambda: self.relay_calls.append("codex-off") or 0)
        self.patch("reconcile_current", lambda: {})
        self.patch("mark_watch_alive", lambda **kw: None)
        self.patch("notify", lambda *args: None)
        self.patch("live_worst", lambda: None)
        self.patch("live_peak_pct", lambda: None)
        self.patch("codex_worst", lambda **kw: None)
        self.usage = {"main": self.reading(98), "fourdesign": self.reading(98)}
        self.patch("probe", lambda slug, **kw: self.usage[slug])
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    @staticmethod
    def reading(pct, kind="seven_day"):
        result = {"ok": True, "five_hour": {"pct": 1}, "seven_day": {"pct": 1}}
        result[kind] = {"pct": pct, "resets_at": time.time() + 3600}
        return result

    def use(self, slug):
        self.assertEqual(mag.cmd_load(argparse.Namespace(alias=slug)), 0)

    def auto(self, no_probe=False, provider="claude"):
        self.assertEqual(mag.cmd_auto(argparse.Namespace(
            provider=provider, no_probe=no_probe, verbose=False)), 0)

    def watch_once(self, threshold=None):
        with patch.object(mag.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(mag.cmd_watch(argparse.Namespace(
                threshold=threshold, interval=20, api_interval=0)), 0)


class ManualSelection(Fixture):
    def test_manual_use_clears_old_cooldown_and_survives_prelaunch(self):
        mag.set_current("claude", "main")
        mag.set_cooldown("fourdesign", "seven_day", time.time() + 3600)
        self.use("fourdesign")
        self.assertEqual(mag.cooldown_left("fourdesign"), 0)
        self.auto(no_probe=True)
        self.assertEqual(mag.get_current("claude"), "fourdesign")

    def test_reaffirming_current_choice_keeps_session_timestamp(self):
        mag.set_current("codex", "cx-main")
        since = mag.state()["last_switch_by"]["codex"]
        mag.set_cooldown("cx-main", "seven_day", time.time() + 3600)
        self.use("cx-main")
        self.assertEqual(mag.cooldown_left("cx-main"), 0)
        self.assertEqual(mag.state()["last_switch_by"]["codex"], since)
        self.auto(no_probe=True, provider="codex")
        self.assertEqual(mag.get_current("codex"), "cx-main")
        self.assertEqual(mag.state()["manual_selection"]["codex"], "cx-main")

    def test_both_accounts_at_98_keep_manual_choice_across_watch_restarts(self):
        self.use("fourdesign")
        self.patch("live_worst", lambda: {"kind": "seven_day", "pct": 98})
        self.watch_once()
        self.watch_once(threshold=90)
        self.auto()
        self.assertEqual(mag.get_current("claude"), "fourdesign")
        self.assertEqual(mag.cooldown_left("fourdesign"), 0)

    def test_manual_prelaunch_waits_for_exactly_100_in_either_window(self):
        for kind in ("five_hour", "seven_day"):
            with self.subTest(kind=kind):
                self.use("fourdesign")
                self.usage["fourdesign"] = self.reading(99.9, kind)
                self.auto()
                self.assertEqual(mag.get_current("claude"), "fourdesign")
                self.usage["fourdesign"] = self.reading(100, kind)
                self.auto()
                self.assertEqual(mag.get_current("claude"), "main")

    def test_manual_api_monitor_waits_for_exactly_100_in_either_window(self):
        for kind in ("five_hour", "seven_day"):
            with self.subTest(kind=kind):
                self.use("fourdesign")
                self.usage["fourdesign"] = self.reading(99.9, kind)
                self.watch_once()
                self.assertEqual(mag.get_current("claude"), "fourdesign")
                self.usage["fourdesign"] = self.reading(100, kind)
                self.watch_once()
                self.assertEqual(mag.get_current("claude"), "main")
                self.assertGreater(mag.cooldown_left("fourdesign"), 0)

    def test_stale_statusline_cannot_override_manual_choice(self):
        self.use("fourdesign")
        self.patch("live_worst", lambda: {"kind": "seven_day", "pct": 100})
        for pct in (2, 98, 99.9):
            with self.subTest(pct=pct):
                self.usage["fourdesign"] = self.reading(pct)
                self.watch_once()
                self.assertEqual(mag.get_current("claude"), "fourdesign")
                self.assertEqual(mag.cooldown_left("fourdesign"), 0)
        self.usage["fourdesign"] = {"ok": False, "error": "usage API unavailable"}
        self.watch_once()
        self.assertEqual(mag.get_current("claude"), "fourdesign")
        self.assertEqual(mag.cooldown_left("fourdesign"), 0)
        self.usage["fourdesign"] = self.reading(100)
        self.watch_once()
        self.assertEqual(mag.get_current("claude"), "main")

    def test_automatic_accounts_still_use_configured_watch_threshold(self):
        mag.set_current("claude", "fourdesign")
        self.watch_once()
        self.assertEqual(mag.get_current("claude"), "main")

    def test_manual_selection_is_per_provider_and_ends_on_next(self):
        self.use("fourdesign")
        self.use("cx-main")
        self.assertEqual(mag.cmd_next(argparse.Namespace(
            provider="claude", no_probe=True)), 0)
        self.assertEqual(mag.get_current("claude"), "main")
        self.assertNotIn("claude", mag.state()["manual_selection"])
        self.assertEqual(mag.state()["manual_selection"]["codex"], "cx-main")
        # Returning automatically must not revive an earlier manual selection.
        self.assertTrue(mag.do_load("fourdesign", "next"))
        self.watch_once()
        self.assertEqual(mag.get_current("claude"), "main")

    def test_failed_manual_switch_preserves_existing_choice(self):
        self.use("fourdesign")
        mag.set_cooldown("main", "seven_day", time.time() + 3600)
        self.patch("stored_oauth", lambda slug: None)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mag.cmd_load(argparse.Namespace(alias="main")), 1)
        self.assertEqual(mag.get_current("claude"), "fourdesign")
        self.assertEqual(mag.state()["manual_selection"]["claude"], "fourdesign")
        self.assertGreater(mag.cooldown_left("main"), 0)

    def test_codex_manual_choice_waits_for_100_even_with_reached_flag(self):
        self.use("cx-main")
        reading = {"pct": 98, "label": "7d window", "window_minutes": 10080,
                   "reached": "weekly", "resets_at": time.time() + 3600}
        self.patch("codex_worst", lambda **kw: reading)
        self.watch_once()
        self.assertEqual(mag.get_current("codex"), "cx-main")
        self.assertEqual(mag.cooldown_left("cx-main"), 0)
        reading["pct"] = 99.9
        self.watch_once()
        self.assertEqual(mag.get_current("codex"), "cx-main")
        reading["pct"] = 100
        self.watch_once()
        self.assertEqual(mag.get_current("codex"), "cx-spare")
        self.assertGreater(mag.cooldown_left("cx-main"), 0)

    def test_codex_automatic_selection_still_switches_at_97(self):
        mag.set_current("codex", "cx-main")
        self.patch("codex_worst", lambda **kw: {
            "pct": 97, "label": "5h window", "window_minutes": 300})
        self.watch_once()
        self.assertEqual(mag.get_current("codex"), "cx-spare")
