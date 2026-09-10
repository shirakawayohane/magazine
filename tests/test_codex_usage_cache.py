"""Codex usage snapshots: offline fixtures, never real accounts or sessions."""
import argparse
import contextlib
import io
import json

from test_mag import Base, mag


class CodexUsageCache(Base):
    def setUp(self):
        super().setUp()
        self.patch("now", lambda: 2000000000.0)
        self.patch("codex_live_limits", lambda **kwargs: None)
        self.patch("codex_probe", self._no_network)
        self.patch("reconcile_current", lambda: {})
        self.patch("codex_stored_auth", lambda slug: {"tokens": {"refresh_token": slug}})
        self.add_account("cx1", provider="codex")
        self.add_account("cx2", provider="codex")
        mag.set_current("codex", "cx1")
        self.snapshot = {"ts": mag.now() - 600, "windows": [
            {"label": "5h window", "pct": 73.0, "resets_at": mag.now() - 60}],
            "reached": "primary"}

    def save_snapshot(self, slug="cx2"):
        s = mag.state()
        s.setdefault("limits", {})[slug] = self.snapshot
        mag.save_state(s)

    def status(self, quick=False):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(mag.cmd_status(argparse.Namespace(quick=quick)), 0)
        return stream.getvalue()

    def test_inactive_status_shows_last_value_and_time_not_live_limit(self):
        self.save_snapshot()
        output = self.status()
        self.assertIn("73.0%", output)
        self.assertIn("last seen", output)
        self.assertNotIn("usage readable only while active", output)
        self.assertNotIn("limit reached:", output)
        self.assertIn("reset time passed; unverified", output)
        self.assertEqual(mag.known_limits("cx2")["ts"], self.snapshot["ts"])

    def test_active_without_new_records_falls_back_in_both_commands(self):
        self.save_snapshot("cx1")
        self.assertIn("73.0%", self.status())
        row = mag.collect_limits()[0]
        self.assertEqual(row["windows"], self.snapshot["windows"])
        self.assertEqual(row["stale_ts"], self.snapshot["ts"])
        self.assertNotIn("reached", row)

    def test_status_saves_actual_observation_time_for_later_inactive_display(self):
        calls = []

        def live(**kwargs):
            calls.append(kwargs)
            return self.snapshot

        self.patch("codex_live_limits", live)
        self.status()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["only_after"], mag.state()["last_switch_by"]["codex"])
        self.assertEqual(mag.known_limits("cx1")["ts"], self.snapshot["ts"])
        mag.set_current("codex", "cx2")
        self.patch("codex_live_limits", lambda **kwargs: None)
        self.assertIn("73.0%", self.status())

    def test_limits_does_not_replace_observation_time_with_read_time(self):
        self.patch("codex_live_limits", lambda **kwargs: self.snapshot)
        row = mag.collect_limits()[0]
        self.assertEqual(mag.known_limits("cx1")["ts"], self.snapshot["ts"])
        self.assertEqual(row["observed_ts"], self.snapshot["ts"])

    def test_older_session_record_does_not_overwrite_newer_saved_value(self):
        self.save_snapshot("cx1")
        older = {"ts": self.snapshot["ts"] - 60, "windows": [
            {"label": "5h window", "pct": 12.0, "resets_at": None}]}
        self.patch("codex_live_limits", lambda **kwargs: older)
        row = mag.collect_limits()[0]
        self.assertEqual(row["windows"], self.snapshot["windows"])
        self.assertEqual(row["stale_ts"], self.snapshot["ts"])
        self.assertEqual(mag.known_limits("cx1")["ts"], self.snapshot["ts"])

    def test_explicit_refresh_saves_its_observation_time(self):
        self.patch("codex_probe", lambda slug: self.snapshot)
        row = mag.collect_limits(args_ns=argparse.Namespace(refresh=True))[1]
        self.assertEqual(row["observed_ts"], self.snapshot["ts"])
        self.assertEqual(mag.known_limits("cx2")["ts"], self.snapshot["ts"])
        self.assertNotIn("stale_ts", row)

    def test_json_identifies_stale_values(self):
        self.save_snapshot()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            mag.cmd_limits(argparse.Namespace(refresh=False, json=True))
        row = json.loads(stream.getvalue())[1]
        self.assertEqual(row["stale_ts"], self.snapshot["ts"])
        self.assertEqual(row["windows"][0]["pct"], 73.0)

    def test_japanese_status_labels_cached_usage(self):
        self.save_snapshot()
        self.patch("LANG", "ja")
        output = self.status()
        self.assertIn("前回観測:", output)
        self.assertIn("現在の使用量は不明", output)

    def test_zero_usage_is_preserved_after_reset_time(self):
        self.snapshot["windows"][0]["pct"] = 0.0
        self.save_snapshot()
        row = mag.collect_limits()[1]
        self.assertEqual(row["windows"][0]["pct"], 0.0)
        self.assertEqual(row["windows"][0]["resets_at"], self.snapshot["windows"][0]["resets_at"])
        self.assertEqual(row["stale_ts"], self.snapshot["ts"])

    def test_failed_explicit_refresh_keeps_snapshot_and_error(self):
        self.save_snapshot()
        for result in (None, {"dead": True}):
            with self.subTest(result=result):
                self.patch("codex_probe", lambda slug: result)
                row = mag.collect_limits(args_ns=argparse.Namespace(refresh=True))[1]
                self.assertEqual(row["windows"], self.snapshot["windows"])
                self.assertEqual(row["stale_ts"], self.snapshot["ts"])
                self.assertTrue(row["note"])
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    mag.cmd_limits(argparse.Namespace(refresh=True, json=False))
                self.assertIn(row["note"], stream.getvalue())

    def test_never_observed_does_not_borrow_another_accounts_value(self):
        self.patch("codex_live_limits", lambda **kwargs: self.snapshot)
        rows = mag.collect_limits()
        self.assertEqual(rows[1]["windows"], [])
        self.assertIn("never observed", rows[1]["note"])

    def test_quick_status_does_not_scan_sessions(self):
        self.patch("codex_live_limits", self._no_network)
        self.status(quick=True)

    def test_switch_saves_outgoing_usage_before_credentials_change(self):
        self.patch("codex_sync_live", lambda: "cx1")
        self.patch("codex_ensure_fresh", lambda slug, auth, **kwargs: (auth, None) if kwargs.get("verify") else auth)
        self.patch("codex_live_limits", lambda **kwargs: self.snapshot)

        def install(auth):
            self.assertEqual(mag.known_limits("cx1")["windows"], self.snapshot["windows"])

        self.patch("codex_install_auth", install)
        self.assertTrue(mag.do_load("cx2"))
        self.assertEqual(mag.get_current("codex"), "cx2")
        self.assertIsNone(mag.known_limits("cx2"))

    def test_unmatched_outgoing_account_is_not_assigned_session_usage(self):
        self.patch("codex_sync_live", lambda: "cx2")
        self.patch("codex_ensure_fresh", lambda slug, auth, **kwargs: (auth, None) if kwargs.get("verify") else auth)
        self.patch("codex_live_limits", self._no_network)
        self.patch("codex_install_auth", lambda auth: None)
        self.assertTrue(mag.do_load("cx2"))
        self.assertIsNone(mag.known_limits("cx1"))
        self.assertIsNone(mag.known_limits("cx2"))
