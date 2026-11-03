"""Auth handover and refresh: synthetic credentials, no real services or daemons."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_mag import Base, mag


def auth(account="a", token="access", refresh="refresh", expires=None):
    claims = {"email": account + "@example.invalid", "exp": expires or mag.now() + 3600,
              "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": "pro"}}
    jwt = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".sig"
    return {"auth_mode": "chatgpt", "tokens": {"account_id": account, "access_token": token,
            "refresh_token": refresh, "id_token": jwt}, "last_refresh": "2026-10-16T00:00:00Z"}


class FakeRPC:
    def __init__(self):
        self.calls = []
        self.access_token = None
        self.busy = False

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "account/login/start":
            self.access_token = params["accessToken"]
            return {"type": "chatgptAuthTokens"}
        if method == "getAuthStatus":
            return {"authToken": self.access_token, "authMethod": "chatgptAuthTokens"}
        if method == "thread/loaded/list": return {"data": ["thread"], "nextCursor": None}
        if method == "thread/read": return {"thread": {"status": {"type": "active" if self.busy else "idle"}}}
        raise AssertionError("unexpected RPC")


class CodexSelection(Base):
    def setUp(self):
        super().setUp()
        self.add_account("cx", provider="codex")
        self.add_account("other", provider="codex")
        self.add_account("claude")
        self.value = auth()
        self.patch("codex_stored_auth", lambda slug: self.value)
        self.patch("codex_live_auth", lambda: self.value)
        self.patch("codex_sync_live", lambda: None)
        self.patch("codex_ensure_fresh", lambda slug, value, **kw: (value, None, True))
        self.patch("codex_usage_snapshot", lambda *a: {})
        self.install = Mock()
        self.patch("codex_install_auth", self.install)
        self.apply = Mock(return_value=True)
        self.patch("codex_apply_selection", self.apply)
        self.output = io.StringIO()
        output = contextlib.redirect_stdout(self.output)
        output.__enter__(); self.addCleanup(output.__exit__, None, None, None)

    def use(self, alias="cx", compatibility_flag=False):
        return mag.cmd_load(argparse.Namespace(alias=alias, restart_codex=compatibility_flag))

    def test_manual_switch_installs_then_schedules_once(self):
        self.apply.side_effect = lambda: bool(self.install.call_count)
        self.assertEqual(self.use(), 0)
        self.install.assert_called_once_with(self.value)
        self.apply.assert_called_once()

    def test_reselect_repairs_owner_without_rotating_credentials(self):
        mag.set_current("codex", "cx")
        self.patch("codex_ensure_fresh", self._no_network)
        self.assertEqual(self.use(), 0)
        self.install.assert_not_called()
        self.apply.assert_called_once()

    def test_same_alias_with_different_disk_account_is_actually_selected(self):
        mag.set_current("codex", "cx")
        self.patch("codex_live_auth", lambda: auth("different"))
        self.assertEqual(self.use(), 0)
        self.install.assert_called_once()

    def test_same_alias_installs_a_newer_login_saved_in_the_vault(self):
        mag.set_current("codex", "cx")
        old = {**self.value, "last_refresh": "2026-10-15T00:00:00Z"}
        self.patch("codex_live_auth", lambda: old)
        self.assertEqual(self.use(), 0)
        self.install.assert_called_once_with(self.value)

    def test_next_and_monitor_common_path_also_schedule_auth_sync(self):
        self.assertTrue(mag.do_load("cx", "watch"))
        self.assertEqual(mag.cmd_next(argparse.Namespace(provider="codex", no_probe=True)), 0)
        self.assertEqual(self.apply.call_count, 2)

    def test_failed_validation_does_not_schedule_or_change_current(self):
        self.patch("codex_ensure_fresh", lambda *a, **k: (self.value, "expired", True))
        with contextlib.redirect_stderr(self.output):
            self.assertEqual(self.use(), 1)
        self.apply.assert_not_called()
        self.install.assert_not_called()
        self.assertIsNone(mag.get_current("codex"))

    def test_compatibility_flag_never_requests_a_restart(self):
        with patch.object(mag.subprocess, "run", side_effect=AssertionError("no restart")):
            self.assertEqual(self.use(compatibility_flag=True), 0)
        self.apply.assert_called_once()

    def test_claude_does_not_start_auth_host(self):
        self.patch("install_oauth", lambda value: None)
        self.patch("stored_oauth", lambda slug: {"accessToken": "fixture"})
        self.patch("ensure_fresh", lambda slug, value: value)
        self.patch("sync_live_credentials", lambda: None)
        self.assertEqual(self.use("claude"), 0)
        self.apply.assert_not_called()


class CodexOwnerLaunch(Base):
    def setUp(self):
        super().setUp()
        root = tempfile.TemporaryDirectory(prefix="mag-sync-test-")
        self.addCleanup(root.cleanup)
        self.patch("ROOT", root.name)
        os.makedirs(os.path.dirname(mag.codex_daemon_socket()))
        with open(mag.codex_daemon_socket(), "w"): pass
        self.spawn = Mock()
        popen = patch.object(mag.subprocess, "Popen", self.spawn)
        popen.start(); self.addCleanup(popen.stop)
        # Base prevents launching user processes; explicitly restore the real function.
        self.real_apply = next(original for name, original in self._patched if name == "codex_apply_selection")

    def test_launch_carries_paths_and_no_credential_copy(self):
        self.assertTrue(self.real_apply())
        request = mag.read_json(mag.codex_sync_state_path(), {})
        self.assertEqual(set(request), {"id", "status", "at"})
        argv, options = self.spawn.call_args
        self.assertEqual(argv[0][-1], "_codex-auth-worker")
        self.assertEqual(options["env"]["CODEX_HOME"], os.path.dirname(mag.CODEX_AUTH_PATH))
        self.assertEqual(options["env"]["MAGAZINE_HOME"], mag.ROOT)
        self.assertTrue(options["start_new_session"])

    def test_absent_server_is_not_started(self):
        os.unlink(mag.codex_daemon_socket())
        self.assertTrue(self.real_apply())
        self.spawn.assert_not_called()

    def test_launch_failure_is_visible_without_provider_error_output(self):
        self.spawn.side_effect = OSError("private-provider-value")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertFalse(self.real_apply())
        self.assertEqual(mag.read_json(mag.codex_sync_state_path(), {})["status"], "failed")
        self.assertNotIn("private-provider-value", output.getvalue())

    @unittest.skipIf(os.name == "nt", "Legacy Unix restart implementation")
    def test_retires_old_restart_request(self):
        path = os.path.join(mag.ROOT, "codex-restart.json")
        mag.write_json(path, {"id": "old", "status": "pending"})
        self.assertTrue(self.real_apply())
        self.assertEqual(mag.read_json(path, {})["status"], "superseded")


class CodexTokenHost(Base):
    def setUp(self):
        super().setUp()
        root = tempfile.TemporaryDirectory(prefix="mag-host-test-")
        self.addCleanup(root.cleanup)
        self.patch("ROOT", root.name)
        self.add_account("a", provider="codex", email="a@example.invalid")
        self.add_account("b", provider="codex", email="b@example.invalid")
        self.patch("codex_refresh", self._no_network)
        mag.codex_install_auth(auth())
        mag.write_json(mag.codex_sync_state_path(), {"id": "first", "status": "pending", "at": mag.now()})
        self.sync = mag.CodexAuthSync()
        self.client = FakeRPC()

    def test_switch_updates_same_client_and_never_reads_or_stops_threads(self):
        self.sync.tick(self.client)
        mag.codex_install_auth(auth("b", "new-access"))
        self.sync.tick(self.client)
        self.assertEqual(self.client.access_token, "new-access")
        methods = [method for method, _ in self.client.calls]
        self.assertEqual(methods.count("account/login/start"), 2)
        self.assertFalse(any(x in methods for x in ("turn/interrupt", "thread/unsubscribe")))
        state = mag.read_json(mag.codex_sync_state_path(), {})
        self.assertEqual(state["status"], "active")
        self.assertNotIn("access", json.dumps(state))
        self.assertNotIn("refresh", json.dumps(state))

    def test_expiring_token_is_saved_and_vault_synced_before_handover(self):
        value = auth()
        value["tokens"]["access_token"] = value["tokens"]["id_token"]
        claims = mag.jwt_claims(value["tokens"]["access_token"])
        claims["exp"] = mag.now() + 60
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        value["tokens"]["access_token"] = "e30." + payload + ".sig"
        mag.codex_install_auth(value)
        renewed = auth("a", "renewed-access", "renewed-refresh")
        refresh = Mock(return_value=(renewed, None))
        self.patch("codex_refresh", refresh)
        self.sync.tick(self.client)
        self.assertEqual(self.client.access_token, "renewed-access")
        self.assertEqual(mag.codex_live_auth()["tokens"]["refresh_token"], "renewed-refresh")
        self.assertEqual(mag.codex_stored_auth("a")["tokens"]["refresh_token"], "renewed-refresh")
        refresh.assert_called_once_with(value, timeout=6)

    def test_unauthorized_callback_rotates_and_next_tick_confirms_it(self):
        self.sync.tick(self.client)
        refreshed = auth("a", "new-access", "new-refresh")
        self.patch("codex_refresh", Mock(return_value=(refreshed, None)))
        result = self.sync.refresh_request("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        self.assertEqual(result["accessToken"], "new-access")
        self.assertNotIn("refresh", result)
        # The server consumes the host response without performing another login.
        self.client.access_token = result["accessToken"]
        self.sync.tick(self.client)
        self.assertEqual(self.client.access_token, "new-access")
        self.assertEqual([name for name, _ in self.client.calls].count("account/login/start"), 1)

    def test_old_turn_refresh_does_not_overwrite_pending_selection(self):
        self.sync.tick(self.client)
        mag.codex_install_auth(auth("b", "selected-b"))
        renewed = auth("a", "renewed-a", "renewed-refresh")
        self.patch("codex_refresh", Mock(return_value=(renewed, None)))
        result = self.sync.refresh_request("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        self.assertEqual(result["chatgptAccountId"], "a")
        self.assertEqual(result["accessToken"], "renewed-a")
        self.assertEqual(mag.codex_stored_auth("a")["tokens"]["refresh_token"], "renewed-refresh")
        self.assertEqual(mag.codex_live_auth()["tokens"]["access_token"], "selected-b")

    def test_vault_failure_keeps_old_turn_rotation_in_memory_for_retry(self):
        self.sync.tick(self.client)
        mag.codex_install_auth(auth("b", "selected-b"))
        renewed = auth("a", "rotated-a", "rotated-refresh")
        refresh = Mock(return_value=(renewed, None))
        self.patch("codex_refresh", refresh)
        self.patch("codex_store_auth", Mock(side_effect=RuntimeError("private-store-error")))
        with self.assertRaises(mag.CodexAuthError):
            self.sync.refresh_request("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        store = Mock()
        self.patch("codex_store_auth", store)
        result = self.sync.refresh_request("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        refresh.assert_called_once()
        self.assertEqual(result["accessToken"], "rotated-a")
        store.assert_called_once_with("a", renewed)
        self.assertEqual(mag.codex_live_auth()["tokens"]["account_id"], "b")

    def test_active_turn_defers_handover_without_restarting_connection(self):
        self.sync.tick(self.client)
        self.client.busy = True
        mag.codex_install_auth(auth("b", "selected-b"))
        clock = mag.now()
        self.patch("now", lambda: clock)
        self.sync.tick(self.client)
        self.assertEqual(self.client.access_token, "access")
        self.assertEqual(mag.read_json(mag.codex_sync_state_path(), {})["status"], "pending")
        self.client.busy = False
        clock += 6
        self.sync.tick(self.client)
        self.assertEqual(self.client.access_token, "selected-b")

    def test_newer_disk_token_is_not_refreshed_again_for_a_stale_request(self):
        self.sync.tick(self.client)
        mag.codex_install_auth(auth("a", "already-newer"))
        result = self.sync.refresh_request("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        self.assertEqual(result["accessToken"], "already-newer")

    def test_invalid_refresh_is_not_retried_until_credentials_change(self):
        self.sync.tick(self.client)
        refresh = Mock(return_value=(None, "private-error-message"))
        self.patch("codex_refresh", refresh)
        with self.assertRaises(mag.CodexAuthError):
            self.sync.refresh_request("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        with self.assertRaises(mag.CodexAuthError): self.sync.tick(self.client)
        refresh.assert_called_once()
        state = mag.read_json(mag.codex_sync_state_path(), {})
        self.assertEqual(state["status"], "reauthentication_required")
        self.assertNotIn("private-error", json.dumps(state))
        mag.codex_install_auth(auth("a", "new-login", "new-login-refresh"))
        self.sync.tick(self.client)
        self.assertEqual(self.client.access_token, "new-login")

    def test_network_failure_retains_saved_refresh_and_is_recoverable(self):
        self.sync.tick(self.client)
        self.patch("codex_refresh", Mock(return_value=(None, None)))
        with self.assertRaises(mag.CodexAuthError):
            self.sync.refresh_request("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        self.assertEqual(mag.codex_live_auth()["tokens"]["refresh_token"], "refresh")
        self.assertEqual(mag.read_json(mag.codex_sync_state_path(), {})["status"], "refresh_failed")
        renewed = auth("a", "recovered", "new-refresh")
        retry = Mock(return_value=(renewed, None))
        self.patch("codex_refresh", retry)
        self.sync.tick(self.client)
        retry.assert_called_once()
        self.assertEqual(self.client.access_token, "recovered")
        self.assertEqual(mag.read_json(mag.codex_sync_state_path(), {})["status"], "active")

    def test_reconnection_applies_latest_file_to_new_connection(self):
        self.sync.tick(self.client)
        mag.codex_install_auth(auth("b", "current-b"))
        sync, client = mag.CodexAuthSync(), FakeRPC()
        sync.tick(client)
        self.assertEqual(client.access_token, "current-b")

    def test_application_is_not_reported_successful_without_verification(self):
        client = Mock()
        client.rpc.side_effect = [{"type": "chatgptAuthTokens"}, {"authToken": "wrong"}]
        with patch.object(mag, "codex_daemon_busy_count", return_value=0):
            with self.assertRaises(ValueError): self.sync.tick(client)
        self.assertEqual(mag.read_json(mag.codex_sync_state_path(), {})["status"], "pending")

    def test_superseded_request_is_not_overwritten_by_older_report(self):
        mag.write_json(mag.codex_sync_state_path(), {"id": "new-request", "status": "pending"})
        mag.codex_sync_report("active", "first")
        self.assertEqual(mag.read_json(mag.codex_sync_state_path(), {})["status"], "pending")

    def test_logout_does_not_resurrect_saved_credentials(self):
        self.sync.tick(self.client)
        os.unlink(mag.CODEX_AUTH_PATH)
        with self.assertRaises(mag.CodexAuthError): self.sync.tick(self.client)
        self.assertFalse(os.path.exists(mag.CODEX_AUTH_PATH))

    def test_api_key_login_is_not_overwritten_by_subscription_handover(self):
        value = auth()
        value["auth_mode"] = "apikey"
        mag.codex_install_auth(value)
        with self.assertRaises(mag.CodexAuthError): self.sync.tick(self.client)
        self.assertEqual(self.client.calls, [])

    def test_stale_heartbeat_is_visible(self):
        mag.write_json(mag.codex_sync_state_path(), {"status": "active", "checked_at": mag.now() - 40})
        output = io.StringIO()
        with contextlib.redirect_stdout(output): mag.print_codex_sync_status()
        self.assertIn("not responding", output.getvalue())

    def test_lock_prevents_concurrent_selection_and_is_reentrant_for_callbacks(self):
        acquired = []
        def contender():
            with mag.codex_auth_lock(blocking=False) as lock: acquired.append(lock is not None)
        with mag.codex_auth_lock():
            with mag.codex_auth_lock() as nested: self.assertIsNotNone(nested)
            thread = threading.Thread(target=contender)
            thread.start(); thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(acquired, [False])


class CodexHandoverReadiness(Base):
    def test_active_approval_waiting_turns_defer_handover(self):
        client = Mock()
        client.rpc.side_effect = [{"data": ["a"], "nextCursor": None},
            {"thread": {"status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}},
            {"data": ["a"], "nextCursor": None}]
        self.assertEqual(mag.codex_daemon_busy_count(client), 1)

    def test_failed_turns_allow_repair_but_unknown_status_does_not(self):
        for status, expected in (("idle", 0), ("systemError", 0), ("notLoaded", None), ("future", None)):
            client = Mock()
            client.rpc.side_effect = [{"data": ["a"], "nextCursor": None},
                {"thread": {"status": {"type": status}}}, {"data": ["a"], "nextCursor": None}]
            self.assertEqual(mag.codex_daemon_busy_count(client), expected)

    def test_changed_or_unreadable_inventory_is_not_treated_as_idle(self):
        client = Mock()
        client.rpc.side_effect = [{"data": []}, {"data": ["new"]}]
        self.assertIsNone(mag.codex_daemon_busy_count(client))
        client.rpc.side_effect = TimeoutError()
        self.assertIsNone(mag.codex_daemon_busy_count(client))

    def test_all_pages_are_checked(self):
        client = Mock()
        client.rpc.side_effect = [{"data": ["a"], "nextCursor": "next"}, {"data": ["b"], "nextCursor": None},
            {"thread": {"status": {"type": "idle"}}}, {"thread": {"status": {"type": "active"}}},
            {"data": ["a", "b"], "nextCursor": None}]
        self.assertEqual(mag.codex_daemon_busy_count(client), 1)


@unittest.skipUnless(hasattr(mag.socket, "AF_UNIX"), "Unix socket transport")
class CodexLocalTransport(Base):
    class Socket:
        def __init__(self, bad_handshake=False, fail_rpc=False):
            self.buffer = b""
            self.closed = False
            self.calls = []
            self.pongs = []
            self.bad_handshake = bad_handshake
            self.fail_rpc = fail_rpc
        def connect(self, path): pass
        def settimeout(self, seconds): pass
        def close(self): self.closed = True
        def recv(self, size):
            data, self.buffer = self.buffer[:min(size, 3)], self.buffer[min(size, 3):]
            return data
        def sendall(self, data):
            if data.startswith(b"GET"):
                key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                self.buffer += b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + (b"bad" if self.bad_handshake else accept) + b"\r\n\r\n"
                return
            n, offset = data[1] & 127, 2
            if n == 126: n, offset = struct.unpack("!H", data[2:4])[0], 4
            mask = data[offset:offset + 4]
            body = bytes(x ^ mask[i % 4] for i, x in enumerate(data[offset + 4:]))
            if data[0] & 15 == 10:
                self.pongs.append(body)
                return
            request = json.loads(body)
            self.calls.append(request)
            if "method" not in request or "id" not in request: return
            if self.fail_rpc: reply = {"id": request["id"], "error": {"message": "private diagnostic"}}
            else: reply = {"id": request["id"], "result": {}}
            payload = json.dumps(reply).encode()
            self.buffer += b"\x89\x02ok" + bytes([129, len(payload)]) + payload

    def test_framing_masks_requests_and_handles_short_reads_and_ping(self):
        sock = self.Socket()
        with patch.object(mag.socket, "socket", return_value=sock):
            with mag.CodexLocalRPC() as client: self.assertEqual(client.rpc("fixture", {}), {})
        self.assertTrue(sock.closed)
        self.assertEqual(len(sock.pongs), 2)

    def test_invalid_handshake_or_rpc_error_closes_socket(self):
        for options in ({"bad_handshake": True}, {"fail_rpc": True}):
            sock = self.Socket(**options)
            with patch.object(mag.socket, "socket", return_value=sock):
                with self.assertRaises(ValueError):
                    with mag.CodexLocalRPC(): pass
            self.assertTrue(sock.closed)

    def test_server_refresh_response_and_error_never_echo_secret_diagnostics(self):
        client = mag.CodexLocalRPC(on_request=Mock(return_value={"accessToken": "fixture"}))
        client.send = Mock()
        client.dispatch({"id": 55, "method": "account/chatgptAuthTokens/refresh", "params": {}})
        self.assertEqual(client.send.call_args.args[0], {"id": 55, "result": {"accessToken": "fixture"}})
        client.on_request.side_effect = RuntimeError("private-secret")
        client.dispatch({"id": 56, "method": "account/chatgptAuthTokens/refresh", "params": {}})
        self.assertIn("error", client.send.call_args.args[0])
        self.assertNotIn("private-secret", str(client.send.call_args.args[0]))

    def test_refresh_request_can_be_interleaved_with_an_rpc_response(self):
        class Interleaved(self.Socket):
            def sendall(self, data):
                super().sendall(data)
                if self.calls and self.calls[-1].get("method") == "fixture":
                    payload = json.dumps({"id": 2, "method": "account/chatgptAuthTokens/refresh",
                                          "params": {"previousAccountId": "a"}}).encode()
                    self.buffer = bytes([129, len(payload)]) + payload + self.buffer
        sock, handler = Interleaved(), Mock(return_value={"accessToken": "new"})
        with patch.object(mag.socket, "socket", return_value=sock):
            with mag.CodexLocalRPC(on_request=handler) as client:
                self.assertEqual(client.rpc("fixture", {}), {})
        handler.assert_called_once_with("account/chatgptAuthTokens/refresh", {"previousAccountId": "a"})
        self.assertIn({"id": 2, "result": {"accessToken": "new"}}, sock.calls)
