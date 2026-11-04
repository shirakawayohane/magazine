"""The relay moves only whose usage is spent; the login (Artifacts, Remote Control) stays on main."""
import argparse
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_mag import Base, mag

FUTURE = (time.time() + 3600) * 1000


def oauth(token, rt=None):
    return {"accessToken": token, "refreshToken": rt or f"rt-{token}", "expiresAt": FUTURE}


class RelayBase(Base):
    def setUp(self):
        super().setUp()
        for slug in ("main", "spare"):
            self.add_account(slug)
        self.store = {"main": oauth("sk-ant-oat-main"), "spare": oauth("sk-ant-oat-spare")}
        self.live = dict(self.store["main"])
        self.patch("stored_oauth", lambda slug: self.store.get(slug))
        self.patch("store_oauth", lambda slug, o: self.store.__setitem__(slug, o))
        self.patch("current_oauth", lambda: self.live)
        self.installed = []
        self.patch("install_oauth", lambda o: (self.installed.append(o["accessToken"]),
                                               self.live.update(o)))
        self.patch("sync_live_credentials", lambda: None)
        self.patch("notify", lambda *a: None)
        self.patch("codex_sync_live", lambda: None)
        mag.set_main("main", "claude")
        mag.set_relay_enabled(True)
        mag.set_current("claude", "spare")
        self.auth = mag.RelayAuth()

    def head(self, method="POST", path="/v1/messages?beta=true", token="sk-ant-oat-main", extra=""):
        return (f"{method} {path} HTTP/1.1\r\nHost: api.anthropic.com\r\n"
                f"Authorization: Bearer {token}\r\n{extra}\r\n").encode()


class RelayLifecycle(Base):
    def server(self):
        srv = mag.RelayServer(0)
        srv.server_ctx = lambda: None
        self.addCleanup(srv.stop)
        return srv

    def test_stop_releases_port_and_accept_thread(self):
        srv = self.server()
        srv.start()
        port = srv.sock.getsockname()[1]
        thread = srv.thread
        srv.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    def test_failed_start_releases_port(self):
        srv = self.server()
        listener = socket.socket()
        self.addCleanup(listener.close)
        with mock.patch.object(mag.socket, "socket", return_value=listener):
            with mock.patch.object(srv, "server_ctx", side_effect=ValueError("bad cert")):
                with self.assertRaises(ValueError):
                    srv.start()
        self.assertEqual(listener.fileno(), -1)

    def test_supervisor_releases_dead_listener_before_restarting(self):
        stale = self.server()
        stale.sock = socket.socket()
        stale.sock.bind(("127.0.0.1", 0))
        stale.sock.listen()
        port = stale.sock.getsockname()[1]
        replacement = self.server()
        replacement.port = port
        self.patch("_RELAY_SERVERS", {"claude": stale})
        self.patch("_RELAY_FAIL_AT", {})
        self.patch("relay_enabled", lambda: True)
        self.patch("codex_relay_enabled", lambda: False)
        self.patch("relay_port", lambda: port)
        self.patch("notify", lambda *args: None)
        with mock.patch.object(mag, "RelayServer", return_value=replacement):
            mag.relay_supervise()
        self.assertIs(mag._RELAY_SERVERS["claude"], replacement)
        self.assertTrue(replacement.alive())

    def test_accept_error_releases_port(self):
        srv = self.server()
        listener = socket.socket()
        self.addCleanup(listener.close)
        wrapped = mock.Mock(wraps=listener)
        wrapped.accept.side_effect = OSError(24, "Too many open files")
        with mock.patch.object(mag.socket, "socket", return_value=wrapped):
            srv.start()
        srv.thread.join(2)
        self.assertFalse(srv.alive())
        self.assertEqual(listener.fileno(), -1)

    def test_failed_connect_reply_closes_upstream(self):
        srv = self.server()
        client, peer = socket.socketpair()
        upstream, upstream_peer = socket.socketpair()
        for sock in (client, peer, upstream, upstream_peer):
            self.addCleanup(sock.close)
        peer.close()
        with mock.patch.object(mag, "_recv_head", return_value=(b"CONNECT example.test:443 HTTP/1.1\r\n\r\n", b"")):
            with mock.patch.object(mag.socket, "create_connection", return_value=upstream):
                srv._handle(client, ("127.0.0.1", 12345))
        self.assertEqual(client.fileno(), -1)
        self.assertEqual(upstream.fileno(), -1)

    def test_connection_limit_queues_then_serves_next_client(self):
        with mock.patch.object(mag.RelayServer, "MAX_CONNECTIONS", 2):
            srv = self.server()
        entered = threading.Event()
        count = []
        handle = srv._handle

        def tracked_handle(client, addr):
            count.append(addr)
            if len(count) == 2:
                entered.set()
            handle(client, addr)

        srv._handle = tracked_handle
        srv.start()
        addr = srv.sock.getsockname()
        first = socket.create_connection(addr, timeout=2)
        second = socket.create_connection(addr, timeout=2)
        third = socket.create_connection(addr, timeout=2)
        for sock in (first, second, third):
            self.addCleanup(sock.close)
        self.assertTrue(entered.wait(2))
        third.sendall(b"GET / HTTP/1.1\r\n\r\n")
        third.settimeout(0.1)
        with self.assertRaises(socket.timeout):
            third.recv(1024)
        self.assertEqual(len(count), 2)
        first.close()
        third.settimeout(2)
        self.assertIn(b"405 Method Not Allowed", third.recv(1024))

    def test_intercept_disconnect_unblocks_upstream_reader(self):
        srv = self.server()
        client, client_peer = socket.socketpair()
        upstream, upstream_peer = socket.socketpair()
        for sock in (client, client_peer, upstream, upstream_peer):
            self.addCleanup(sock.close)
        reader_done = threading.Event()
        original_pipe = mag._pipe

        def pipe(src, dst):
            try:
                original_pipe(src, dst)
            finally:
                reader_done.set()

        ctx = mock.Mock()
        ctx.wrap_socket.side_effect = lambda sock, **kw: sock
        srv.server_ctx = lambda: ctx
        with mock.patch.object(mag.ssl, "create_default_context", return_value=ctx):
            with mock.patch.object(mag, "relay_forward_requests", return_value=None):
                with mock.patch.object(mag, "_pipe", side_effect=pipe):
                    srv._intercept(client, upstream, b"")
                    finished = reader_done.wait(1)
                    # Also clean up a blocked reader when testing the broken implementation.
                    upstream_peer.close()
                    reader_done.wait(1)
        self.assertTrue(finished, "disconnected client left the upstream reader blocked")


class SwapDecision(RelayBase):
    def swapped_to(self, **kw):
        head, _hdr, swapped = mag.rewrite_request_head(self.head(**kw), self.auth)
        line = [ln for ln in head.decode().split("\r\n") if ln.lower().startswith("authorization")][0]
        return line.split()[-1] if swapped else None

    def test_conversation_requests_use_the_spare(self):
        self.assertEqual(self.swapped_to(), "sk-ant-oat-spare")

    def test_everything_else_keeps_the_main_login(self):
        self.assertIsNone(self.swapped_to(path="/api/artifacts/mcp"))
        self.assertIsNone(self.swapped_to(path="/v1/code/sessions"))
        self.assertIsNone(self.swapped_to(method="GET", path="/v1/messages"))

    def test_a_token_the_caller_chose_is_left_alone(self):
        # e.g. the pre-check runs `claude` with CLAUDE_CODE_OAUTH_TOKEN for a spare
        self.assertIsNone(self.swapped_to(token="sk-ant-oat-other"))

    def test_no_swap_on_main_or_when_off(self):
        mag.set_current("claude", "main")
        self.assertIsNone(self.swapped_to())
        mag.set_current("claude", "spare")
        mag.set_relay_enabled(False)
        self.assertIsNone(self.swapped_to())

    def test_follows_a_switch_immediately(self):
        self.add_account("third")
        self.store["third"] = oauth("sk-ant-oat-third")
        self.assertEqual(self.swapped_to(), "sk-ant-oat-spare")
        mag.set_current("claude", "third")
        self.assertEqual(self.swapped_to(), "sk-ant-oat-third")

    def test_unusable_spare_falls_back_to_main_instead_of_failing(self):
        self.store["spare"] = dict(self.store["spare"], expiresAt=(time.time() - 10) * 1000)
        self.patch("refresh_oauth", lambda o: None)
        self.assertIsNone(self.swapped_to())

    def test_main_token_refreshed_by_claude_code_is_recognised(self):
        self.assertEqual(self.swapped_to(), "sk-ant-oat-spare")
        self.live["accessToken"] = "sk-ant-oat-main2"
        self.auth._live = (self.auth._live[0], 0)     # past the recheck interval
        self.assertEqual(self.swapped_to(token="sk-ant-oat-main2"), "sk-ant-oat-spare")


class Forwarding(RelayBase):
    def run_forward(self, payload: bytes) -> bytes:
        c_out, c_in = socket.socketpair()      # client side
        u_in, u_out = socket.socketpair()      # upstream side
        got = []
        reader = threading.Thread(target=lambda: got.append(self._drain(u_out)))
        reader.start()
        c_out.sendall(payload)
        c_out.shutdown(socket.SHUT_WR)
        mag.relay_forward_requests(c_in, u_in, self.auth)
        u_in.close()
        reader.join(5)
        for sock in (c_out, c_in, u_out):
            sock.close()
        return got[0]

    @staticmethod
    def _drain(sock):
        out = b""
        while True:
            d = sock.recv(65536)
            if not d:
                return out
            out += d

    def test_keep_alive_requests_each_get_rewritten(self):
        body = b'{"x":1}'
        first = self.head(extra=f"Content-Length: {len(body)}\r\n") + body
        chunks = b"5\r\nhello\r\n0\r\n\r\n"
        second = self.head(extra="Transfer-Encoding: chunked\r\n") + chunks
        third = self.head(method="GET", path="/api/oauth/profile")
        out = self.run_forward(first + second + third)
        self.assertEqual(out.count(b"Bearer sk-ant-oat-spare"), 2)
        self.assertEqual(out.count(b"Bearer sk-ant-oat-main"), 1)
        self.assertIn(body, out)
        self.assertIn(chunks, out)

    def test_upgrade_is_passed_through_raw(self):
        req = self.head(method="GET", path="/v1/ws", extra="Connection: Upgrade\r\nUpgrade: websocket\r\n")
        out = self.run_forward(req + b"\x81\x05frame")
        self.assertTrue(out.endswith(b"\x81\x05frame"))


class Tunnel(RelayBase):
    def test_other_hosts_are_tunnelled_untouched(self):
        echo = socket.socket()
        echo.bind(("127.0.0.1", 0))
        echo.listen(1)

        def serve():
            c, _ = echo.accept()
            c.sendall(c.recv(1024).upper())
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        srv = mag.RelayServer(0)
        self.patch("RELAY_DIR", tempfile.mkdtemp())
        srv.server_ctx = lambda: None
        srv.start()
        port = srv.sock.getsockname()[1]
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
                c.sendall(f"CONNECT 127.0.0.1:{echo.getsockname()[1]} HTTP/1.1\r\n\r\n".encode())
                self.assertIn(b"200", c.recv(1024))
                c.sendall(b"ping")
                self.assertEqual(c.recv(1024), b"PING")
        finally:
            srv.stop()
            echo.close()


class PeerDetection(Base):
    def test_claude_command_lines(self):
        yes = ["/Users/x/.local/share/claude/versions/2.1.317 --resume",
               "claude", "/opt/homebrew/bin/claude -p hi",
               "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]
        no = ["python3 script.py", "curl https://api.anthropic.com", "/bin/zsh -c claude-ish", ""]
        for cmd in yes:
            self.assertTrue(mag.is_claude_command(cmd), cmd)
        for cmd in no:
            self.assertFalse(mag.is_claude_command(cmd), cmd)


class Switching(RelayBase):
    def test_switch_changes_usage_not_login(self):
        self.assertTrue(mag.do_load("main", "next"))
        self.assertTrue(mag.do_load("spare", "next"))
        self.assertEqual(self.installed, [])
        self.assertEqual(mag.get_current("claude"), "spare")
        self.assertEqual(self.live["accessToken"], "sk-ant-oat-main")

    def test_reconcile_keeps_the_paying_account(self):
        mag.reconcile_current()
        self.assertEqual(mag.get_current("claude"), "spare")

    @unittest.skipIf(mag.IS_WINDOWS, "the relay is not available on Windows")
    def test_changing_main_moves_the_login(self):
        self.add_account("other")
        self.store["other"] = oauth("sk-ant-oat-other")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mag.cmd_main(argparse.Namespace(name="other", clear=None)), 0)
        self.assertEqual(self.installed, ["sk-ant-oat-other"])



class Settings(Base):
    def setUp(self):
        super().setUp()
        self.conf = tempfile.mkdtemp()
        self.patch("claude_config_dir", lambda: self.conf)
        self.path = os.path.join(self.conf, "settings.json")
        for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "NODE_EXTRA_CA_CERTS"):
            if k in os.environ:
                self.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))

    def write(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_on_then_off_restores_the_user_settings(self):
        self.write({"env": {"FOO": "1", "NO_PROXY": "corp.local"}, "model": "x"})
        with contextlib.redirect_stdout(io.StringIO()):
            mag.relay_settings_apply(True)
        env = self.read()["env"]
        self.assertEqual(env["HTTPS_PROXY"], mag.relay_url())
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], mag.relay_ca_path())
        self.assertEqual(env["NO_PROXY"], "corp.local,localhost,127.0.0.1,::1")
        self.assertTrue(mag.relay_settings_installed())
        with contextlib.redirect_stdout(io.StringIO()):
            mag.relay_settings_apply(False)
        self.assertEqual(self.read(), {"env": {"FOO": "1", "NO_PROXY": "corp.local"}, "model": "x"})

    def test_turning_on_twice_still_restores_the_original(self):
        self.write({"env": {"NO_PROXY": "corp.local"}})
        with contextlib.redirect_stdout(io.StringIO()):
            mag.relay_settings_apply(True)
            mag.relay_settings_apply(True)
            mag.relay_settings_apply(False)
        self.assertEqual(self.read(), {"env": {"NO_PROXY": "corp.local"}})

    def test_existing_extra_ca_is_kept_in_a_bundle(self):
        os.makedirs(mag.RELAY_DIR, exist_ok=True)
        with open(mag.relay_ca_path(), "w") as f:
            f.write("RELAY\n")
        corp = os.path.join(self.conf, "corp.pem")
        with open(corp, "w") as f:
            f.write("CORP\n")
        self.write({"env": {"NODE_EXTRA_CA_CERTS": corp}})
        with contextlib.redirect_stdout(io.StringIO()):
            mag.relay_settings_apply(True)
        bundle = self.read()["env"]["NODE_EXTRA_CA_CERTS"]
        with open(bundle) as f:
            self.assertEqual(f.read(), "CORP\nRELAY\n")
        with contextlib.redirect_stdout(io.StringIO()):
            mag.relay_settings_apply(False)
        self.assertEqual(self.read()["env"]["NODE_EXTRA_CA_CERTS"], corp)

    def test_another_proxy_blocks_enabling(self):
        self.write({"env": {"HTTPS_PROXY": "http://corp:8080"}})
        self.assertIn("corp:8080", mag.relay_settings_conflict())
        self.write({})
        self.assertIsNone(mag.relay_settings_conflict())


class RefreshLock(Base):
    def test_concurrent_refreshes_spend_the_token_once(self):
        self.add_account("spare")
        store = {"spare": {"accessToken": "old", "refreshToken": "rt1", "expiresAt": 0}}
        self.patch("stored_oauth", lambda slug: store.get(slug))
        self.patch("store_oauth", lambda slug, o: store.__setitem__(slug, o))
        self.patch("current_oauth", lambda: {"refreshToken": "live"})
        calls = []

        def refresh(o):
            calls.append(o["refreshToken"])
            time.sleep(0.2)
            return {"accessToken": "new", "refreshToken": "rt2", "expiresAt": FUTURE}
        self.patch("refresh_oauth", refresh)
        out = []
        threads = [threading.Thread(target=lambda: out.append(mag.ensure_fresh("spare", dict(store["spare"]))))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(calls, ["rt1"])
        self.assertEqual(len(out), 3)                      # no thread died on the lock
        self.assertEqual({o["accessToken"] for o in out}, {"new"})


@unittest.skipIf(mag.IS_WINDOWS, "the relay is not available on Windows")
class OnOff(RelayBase):
    def setUp(self):
        super().setUp()
        mag.set_relay_enabled(False)
        self.conf = tempfile.mkdtemp()
        self.patch("claude_config_dir", lambda: self.conf)
        self.patch("relay_certs_ready", lambda: True)
        self.patch("watch_daemon_state", lambda: mag.T("running", "稼働中"))
        self.patch("relay_settings_conflict", lambda: None)
        self.live = dict(self.store["spare"])          # Claude Code currently signed in to the spare
        for k in ("NODE_EXTRA_CA_CERTS", "NO_PROXY"):
            if k in os.environ:
                self.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))
        os.makedirs(mag.RELAY_DIR, exist_ok=True)
        with open(mag.relay_ca_path(), "w") as f:
            f.write("RELAY\n")

    def relay(self, action):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return mag.cmd_relay(argparse.Namespace(action=action))

    def test_on_signs_in_to_main_and_keeps_the_paying_choice(self):
        mag.set_current("claude", "spare", manual=True)
        self.assertEqual(self.relay("on"), 0)
        self.assertEqual(self.live["accessToken"], "sk-ant-oat-main")
        self.assertEqual(mag.get_current("claude"), "spare")
        self.assertTrue(mag.manually_selected("spare"))
        self.assertTrue(mag.relay_settings_installed())
        self.assertEqual(self.relay("off"), 0)
        self.assertFalse(mag.relay_enabled())
        self.assertFalse(mag.relay_settings_installed())
        self.assertEqual(mag.get_current("claude"), "main")

    def main_cmd(self, name=None, provider=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return mag.cmd_main(argparse.Namespace(name=name, provider=provider))

    def test_choosing_main_is_all_it_takes(self):
        mag.set_main(None, "claude")
        self.assertEqual(self.main_cmd("main"), 0)
        self.assertTrue(mag.relay_enabled())
        self.assertTrue(mag.relay_settings_installed())
        self.assertEqual(self.live["accessToken"], "sk-ant-oat-main")
        self.assertEqual(mag.get_current("claude"), "spare")      # usage stays where it was

    def test_clearing_main_turns_the_relay_off(self):
        self.assertEqual(self.main_cmd("main"), 0)
        self.assertEqual(self.main_cmd("clear"), 0)
        self.assertFalse(mag.relay_enabled())
        self.assertFalse(mag.relay_settings_installed())
        self.assertIsNone(mag.main_slug("claude"))

    def test_main_is_kept_when_the_relay_cannot_start(self):
        mag.set_main(None, "claude")
        self.patch("relay_settings_conflict", lambda: "HTTPS_PROXY=http://corp:8080")
        self.assertEqual(self.main_cmd("main"), 1)
        self.assertEqual(mag.main_slug("claude"), "main")
        self.assertFalse(mag.relay_enabled())

    def test_on_requires_a_main_account(self):
        mag.set_main(None, "claude")
        self.assertEqual(self.relay("on"), 1)
        self.assertFalse(mag.relay_enabled())


class WindowsMain(RelayBase):
    def test_main_without_relay_on_windows(self):
        mag.set_relay_enabled(False)
        mag.set_main(None, "claude")
        self.patch("IS_WINDOWS", True)
        self.patch("relay_turn_on", lambda: self.fail("relay started on Windows"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mag.cmd_main(argparse.Namespace(name="main", provider=None)), 0)
        self.assertEqual(mag.main_slug("claude"), "main")
        self.assertFalse(mag.relay_enabled())
