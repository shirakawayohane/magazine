"""Codex usage snapshots: offline fixtures, never real accounts or sessions."""
import argparse
import contextlib
import io
import json
from unittest.mock import patch

from test_mag import Base, mag


class CodexRateLimits(Base):
    def setUp(self):
        super().setUp()
        self.auth = {"tokens": {"access_token": "fixture-access", "account_id": "fixture-account",
                                "refresh_token": "must-not-be-sent"}}
        self.patch("codex_refresh", self._no_network)
        self.patch("codex_install_auth", self._no_network)

    def read_reply(self, result=None, notification=None):
        replies = [{"id": 1, "result": {}}, {"id": 2, "result": {}}]
        replies.append(notification or {"id": 3, "result": result})

        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO("\n".join(json.dumps(r) for r in replies) + "\n")
                self.stopped = False
                self.sent = None

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.sent = self.stdin.getvalue()
                self.stopped = True

            def wait(self, timeout=None):
                return 0

        proc = Process()
        with patch.object(mag.subprocess, "Popen", return_value=proc) as launch:
            value = mag.codex_rate_limits(self.auth)
        self.assertTrue(proc.stopped)
        self.assertTrue(proc.stdout.closed)
        self.assertNotIn("must-not-be-sent", proc.sent)
        self.assertNotIn("exec", launch.call_args.args[0])
        self.assertEqual(launch.call_args.args[0][-1], "app-server")
        return value

    def test_zero_usage_and_codex_bucket_selected_over_model_bucket(self):
        result = self.read_reply({
            "rateLimits": {"limitId": "spark", "primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {"codex": {"primary": {
                "usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 2000000000}}}})
        self.assertEqual(result["windows"][0]["pct"], 0)
        self.assertEqual(result["windows"][0]["label"], "7d window")
        self.assertEqual(result["windows"][0]["resets_at"], 2000000000)

    def test_model_only_bucket_is_not_reported_as_account_usage(self):
        result = self.read_reply({"rateLimitsByLimitId": {"spark": {
            "primary": {"usedPercent": 99}}}})
        self.assertNotIn("windows", result)
        self.assertIn("error", result)

    def test_server_refresh_request_does_not_rotate_credentials(self):
        result = self.read_reply(notification={"id": 9,
            "method": "account/chatgptAuthTokens/refresh", "params": {"reason": "unauthorized"}})
        self.assertIn("sign-in needs renewal", result["error"])
        self.assertNotIn("dead", result)

    def test_early_server_exit_retains_unknown_usage(self):
        result = self.read_reply(notification={"method": "unrelated"})
        self.assertIn("could not read", result["error"])
        self.assertNotIn("windows", result)

    def test_missing_codex_binary_is_an_availability_error(self):
        with patch.object(mag.subprocess, "Popen", side_effect=FileNotFoundError):
            self.assertIn("could not read", mag.codex_rate_limits(self.auth)["error"])

    def test_timeout_stops_server_and_leaves_usage_unknown(self):
        with patch.object(mag.queue.Queue, "get", side_effect=mag.queue.Empty):
            result = self.read_reply({})
        self.assertIn("could not read", result["error"])
        self.assertNotIn("windows", result)

    def test_newer_live_credentials_only_used_for_matching_account(self):
        stored = {**self.auth, "last_refresh": "2026-09-20T00:00:00Z"}
        live = {"tokens": {"account_id": "another-account", "access_token": "live"},
                "last_refresh": "2026-10-01T00:00:00Z"}
        self.patch("codex_stored_auth", lambda slug: stored)
        self.patch("codex_live_auth", lambda: live)
        calls = []
        self.patch("codex_rate_limits", lambda auth, **kwargs: calls.append(auth))
        mag.codex_probe("cx1")
        self.assertIs(calls[-1], stored)
        live["tokens"]["account_id"] = "fixture-account"
        mag.codex_probe("cx1")
        self.assertIs(calls[-1], live)


class CodexUsageCache(Base):
    def setUp(self):
        super().setUp()
        self.patch("now", lambda: 2000000000.0)
        self.patch("codex_live_limits", lambda **kwargs: None)
        self.patch("codex_probe", lambda slug: None)
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
        self.snapshot["ts"] = mag.now() - 30
        self.snapshot["windows"][0]["resets_at"] = mag.now() + 60
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
        self.patch("codex_probe", self._no_network)
        self.status(quick=True)

    def test_active_explicit_refresh_fetches_even_with_fresh_session_records(self):
        self.patch("codex_live_limits", self._no_network)
        self.patch("codex_probe", lambda slug: self.snapshot)
        row = mag.codex_usage_snapshot("cx1", refresh=True)
        self.assertEqual(row["observed_ts"], self.snapshot["ts"])
        self.assertEqual(mag.known_limits("cx1")["windows"], row["windows"])

    def test_normal_status_recovers_expired_usage_and_reuses_fresh_result(self):
        self.save_snapshot("cx1")
        fresh = {"ts": mag.now(), "windows": [
            {"label": "7d window", "pct": 0, "resets_at": mag.now() + 604800}]}
        calls = []
        self.patch("codex_probe", lambda slug: calls.append(slug) or fresh)
        row = mag.codex_usage_snapshot("cx1")
        self.assertEqual(row["windows"][0]["pct"], 0)
        self.assertNotIn("stale_ts", row)
        self.assertEqual(mag.codex_usage_snapshot("cx1")["observed_ts"], fresh["ts"])
        self.assertEqual(calls, ["cx1"])

    def test_reset_expiry_forces_refresh_even_with_recent_cache(self):
        self.snapshot["ts"] = mag.now() - 1
        self.save_snapshot("cx1")
        calls = []
        self.patch("codex_probe", lambda slug: calls.append(slug))
        row = mag.codex_usage_snapshot("cx1")
        self.assertEqual(calls, ["cx1"])
        self.assertIn("stale_ts", row)

    def test_active_failed_refresh_preserves_usage_and_reason(self):
        self.save_snapshot("cx1")
        self.patch("codex_probe", lambda slug: {"error": "usage service unavailable"})
        for refresh in (True, False):
            row = mag.codex_usage_snapshot("cx1", refresh=refresh)
            self.assertEqual(row["note"], "usage service unavailable")
            self.assertEqual(row["stale_ts"], self.snapshot["ts"])
            self.assertNotIn("reached", row)

    def test_switch_saves_outgoing_usage_before_credentials_change(self):
        self.patch("codex_sync_live", lambda: "cx1")
        self.patch("codex_ensure_fresh", lambda slug, auth, **kwargs: (auth, None, True))
        self.patch("codex_live_limits", lambda **kwargs: self.snapshot)

        def install(auth):
            self.assertEqual(mag.known_limits("cx1")["windows"], self.snapshot["windows"])

        self.patch("codex_install_auth", install)
        self.assertTrue(mag.do_load("cx2"))
        self.assertEqual(mag.get_current("codex"), "cx2")
        self.assertIsNone(mag.known_limits("cx2"))

    def test_unmatched_outgoing_account_is_not_assigned_session_usage(self):
        self.patch("codex_sync_live", lambda: "cx2")
        self.patch("codex_ensure_fresh", lambda slug, auth, **kwargs: (auth, None, True))
        self.patch("codex_live_limits", self._no_network)
        self.patch("codex_install_auth", lambda auth: None)
        self.assertTrue(mag.do_load("cx2"))
        self.assertIsNone(mag.known_limits("cx1"))
        self.assertIsNone(mag.known_limits("cx2"))
