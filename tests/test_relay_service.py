"""Relay enablement is usable only after a verified listener is running."""
import argparse
import contextlib
import io
import plistlib
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_mag import Base, mag

ENSURE_RUNNING = mag.relay_ensure_running
PREPARE_SWITCH = mag.relay_prepare_switch


class Startup(Base):
    def test_enabled_but_unusable_relay_fails_the_status_check(self):
        mag.set_relay_enabled(True)
        self.patch("relay_listening", lambda: False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mag.cmd_relay(argparse.Namespace(action="status", provider="claude")), 1)

    def test_service_failure_is_reported(self):
        self.patch("relay_listening", lambda *args: False)
        self.patch("relay_service_start", lambda: "service rejected")
        self.assertEqual(ENSURE_RUNNING("claude"), "service rejected")

    def test_waits_for_verified_listener(self):
        with mock.patch.object(mag, "codex_relay_listening", side_effect=[False, False, True]):
            with mock.patch.object(mag, "relay_service_start", return_value=None) as start:
                self.assertIsNone(ENSURE_RUNNING("codex"))
        start.assert_called_once()

    def test_timeout_is_a_failure(self):
        self.patch("relay_listening", lambda *args: False)
        self.patch("relay_service_start", lambda: None)
        self.patch("RELAY_START_WAIT", 0)
        self.assertIn("did not start", ENSURE_RUNNING("claude"))

    def test_watch_does_not_bind_over_the_service_listener(self):
        self.patch("_RELAY_SERVERS", {})
        self.patch("_RELAY_FAIL_AT", {})
        self.patch("relay_enabled", lambda: True)
        self.patch("codex_relay_enabled", lambda: False)
        self.patch("relay_listening", lambda *args: True)
        with mock.patch.object(mag, "RelayServer") as server:
            mag.relay_supervise()
        server.assert_not_called()

    @unittest.skipIf(mag.IS_WINDOWS, "launchd requires Unix user IDs")
    def test_loaded_launchd_service_reports_a_failed_start(self):
        self.patch("relay_service_kind", lambda: "launchd")
        self.patch("_write_if_changed", lambda *args: False)
        self.patch("_run", lambda cmd: subprocess.CompletedProcess(
            cmd, 1 if "kickstart" in cmd else 0, "", "kickstart rejected"))
        self.assertEqual(mag.relay_service_start(), "kickstart rejected")

    def test_health_probe_does_not_open_an_upstream_connection(self):
        for provider, cls in (("claude", mag.RelayServer), ("codex", mag.CodexRelayServer)):
            with self.subTest(provider=provider):
                server = cls(0)
                server.server_ctx = lambda: None
                server.start()
                try:
                    port = server.sock.getsockname()[1]
                    with mock.patch.object(mag.socket, "create_connection", wraps=socket.create_connection) as connect:
                        self.assertTrue(mag._relay_is_listening(port, provider, 1))
                        self.assertFalse(mag._relay_is_listening(port, "other", 1))
                    self.assertEqual(connect.call_count, 2)
                finally:
                    server.stop()

    def test_an_unrelated_http_server_is_not_a_healthy_relay(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()

            def respond():
                with listener.accept()[0] as client:
                    client.recv(4096)
                    client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

            thread = threading.Thread(target=respond)
            thread.start()
            self.assertFalse(mag._relay_is_listening(listener.getsockname()[1], "claude", 1))
            thread.join(2)

    def test_orphan_flag_is_removed_before_a_real_switch(self):
        mag.set_relay_enabled(True)
        self.patch("reconcile_current", lambda: {})
        self.assertTrue(PREPARE_SWITCH("claude"))
        self.assertFalse(mag.relay_enabled())

    def test_broken_relay_is_repaired_before_switching(self):
        self.add_account("main")
        mag.set_main("main", "claude")
        mag.set_relay_enabled(True)
        self.patch("relay_listening", lambda: False)
        with mock.patch.object(mag, "relay_turn_on", return_value=1) as repair:
            self.assertFalse(PREPARE_SWITCH("claude"))
        repair.assert_called_once_with(operation="account switch repair")

    @unittest.skipIf(mag.IS_WINDOWS, "launchd requires Unix user IDs")
    def test_service_removal_logs_the_trigger_and_os_result(self):
        with tempfile.TemporaryDirectory() as directory:
            registration = Path(directory) / "relay.plist"
            registration.touch()
            self.patch("_relay_launchd_path", lambda: str(registration))
            self.patch("relay_service_kind", lambda: "launchd")
            self.patch("_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""))
            records = []
            self.patch("log", records.append)
            mag.relay_service_stop("claude", "mag main clear")
            self.assertFalse(registration.exists())
        self.assertIn("provider=claude operation='mag main clear' step=stop_service result=launchd_exit_0",
                      records[0])
        self.assertIn("provider=claude operation='mag main clear' step=remove_service_registration result=ok",
                      records[1])

    @unittest.skipIf(mag.IS_WINDOWS, "launchd/systemd support is Unix-only")
    def test_service_definition_preserves_explicit_profile_and_paths(self):
        self.patch("_relay_service_cmd", lambda: ["/Python With Spaces/python", "/source/mag.py", "relay", "serve"])
        self.patch("_relay_service_env", lambda: {"MAGAZINE_HOME": "/profile with spaces", "CLAUDE_CONFIG_DIR": "/claude"})
        body = plistlib.loads(mag._relay_launchd_plist())
        self.assertEqual(body["ProgramArguments"][0], "/Python With Spaces/python")
        self.assertEqual(body["EnvironmentVariables"]["MAGAZINE_HOME"], "/profile with spaces")
        unit = mag._relay_systemd_unit().decode()
        self.assertIn('ExecStart="/Python With Spaces/python"', unit)
        self.assertIn('Environment="MAGAZINE_HOME=/profile with spaces"', unit)


class Selection(Base):
    def test_same_claude_selection_restores_login_when_disk_differs(self):
        self.add_account("spare")
        mag.set_current("claude", "spare")
        self.patch("current_oauth", lambda: {"refreshToken": "main-login"})
        self.patch("stored_oauth", lambda slug: {"refreshToken": "spare-login"})
        with mock.patch.object(mag, "do_load", return_value=True) as load:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mag.cmd_load(argparse.Namespace(alias="spare")), 0)
        load.assert_called_once_with("spare", "manual")
