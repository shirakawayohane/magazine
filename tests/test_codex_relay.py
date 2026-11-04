"""Codex relay: the login (auth.json) stays on main; only conversation requests spend a spare's usage."""
import argparse
import base64
import contextlib
import io
import json
import os
import socket
import threading
import time
import unittest

from test_mag import Base, mag


def auth(account, token=None, refresh=None):
    claims = {"email": account + "@example.invalid", "exp": mag.now() + 3600,
              "https://api.openai.com/auth": {"chatgpt_account_id": "acc-" + account, "chatgpt_plan_type": "pro"}}
    jwt = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".sig"
    return {"auth_mode": "chatgpt", "tokens": {"account_id": "acc-" + account,
            "access_token": token or jwt, "refresh_token": refresh or "rt-" + account, "id_token": jwt},
            "last_refresh": "2026-11-04T00:00:00Z"}


class CodexBase(Base):
    def setUp(self):
        super().setUp()
        for slug in ("cx-main", "cx-spare"):
            self.add_account(slug, provider="codex")
        self.store = {"cx-main": auth("main"), "cx-spare": auth("spare")}
        self.patch("codex_stored_auth", lambda slug: self.store.get(slug))
        self.patch("codex_store_auth", lambda slug, a: self.store.__setitem__(slug, a))
        self.patch("codex_ensure_fresh", lambda slug, a, verify=False: (a, None, True))
        self.patch("notify", lambda *a: None)
        live = auth("main", token="main-token")
        self.patch("codex_live_auth", lambda: live)
        mag.set_main("cx-main", "codex")
        mag.set_codex_relay_enabled(True)
        mag.set_current("codex", "cx-spare")
        self.auth = mag.CodexRelayAuth()

    def head(self, path="/backend-api/codex/responses", account="acc-main", token="main-token"):
        return (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:47822\r\nAuthorization: Bearer {token}\r\n"
                f"ChatGPT-Account-Id: {account}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n").encode()

    def rewritten(self, **kw):
        conn = mag.CodexConn(None, None)
        head, _hdr, swapped = mag.codex_rewrite_head(self.head(**kw), self.auth, conn)
        hdr = dict(ln.split(": ", 1) for ln in head.decode().split("\r\n")[1:] if ": " in ln)
        return hdr, swapped, conn


class Rewrite(CodexBase):
    def test_conversation_requests_use_the_spare(self):
        hdr, swapped, conn = self.rewritten()
        self.assertTrue(swapped)
        self.assertEqual(hdr["Host"], "chatgpt.com")
        self.assertEqual(hdr["Authorization"], "Bearer " + self.store["cx-spare"]["tokens"]["access_token"])
        self.assertEqual(hdr["ChatGPT-Account-Id"], "acc-spare")
        self.assertEqual(conn.slug, "cx-spare")

    def test_main_usage_passes_through(self):
        mag.set_current("codex", "cx-main")
        hdr, swapped, conn = self.rewritten()
        self.assertFalse(swapped)
        self.assertEqual(hdr["Host"], "chatgpt.com")
        self.assertEqual(hdr["Authorization"], "Bearer main-token")
        self.assertIsNone(conn.slug)

    def test_only_the_main_login_is_rewritten(self):
        self.assertFalse(self.rewritten(account="acc-someone-else")[1])
        self.assertFalse(self.rewritten(path="/backend-api/wham/tasks/list")[1])
        # Knowing the main account ID is not enough without the main login itself.
        self.assertFalse(self.rewritten(token="someone-elses-token")[1])

    def test_relay_off_passes_through(self):
        mag.set_codex_relay_enabled(False)
        self.assertFalse(self.rewritten()[1])

    def test_rejected_spare_falls_back_to_main_for_a_while(self):
        self.auth.mark_failed("cx-spare", 401)
        self.assertFalse(self.rewritten()[1])
        self.auth._failed["cx-spare"] = mag.now() - 1
        self.assertTrue(self.rewritten()[1])

    def test_a_401_after_a_swapped_request_marks_the_spare(self):
        _hdr, _swapped, conn = self.rewritten()
        up, up_peer = socket.socketpair()
        cl, cl_peer = socket.socketpair()
        for s in (up, up_peer, cl, cl_peer):
            self.addCleanup(s.close)
        t = threading.Thread(target=mag.codex_back_pipe, args=(up, cl, conn, self.auth))
        t.start()
        up_peer.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
        up_peer.shutdown(socket.SHUT_WR)
        t.join(5)
        self.assertIn(b"401", cl_peer.recv(100))
        self.assertIsNone(self.auth.paying())


class Sweep(CodexBase):
    def test_connections_on_the_old_account_are_closed_once_quiet(self):
        srv = mag.CodexRelayServer(0, self.auth)
        closed = []

        def conn(slug, used=True, idle=10.0):
            c = mag.CodexConn(None, None)
            c.slug, c.used, c.last = slug, used, mag.now() - idle
            c.close = lambda c=c: closed.append(c)
            srv.conns.add(c)
            return c

        old = conn(None)                       # opened while main was paying
        busy = conn(None, idle=0.0)            # still streaming
        unused = conn(None, used=False)        # never carried a conversation request
        current = conn("cx-spare")
        srv.sweep()
        self.assertEqual(closed, [old])
        self.assertNotIn(busy, closed)
        self.assertNotIn(unused, closed)
        self.assertNotIn(current, closed)


class Forwarding(CodexBase):
    def test_request_reaches_chatgpt_rewritten_and_the_answer_comes_back(self):
        up_ours, up_far = socket.socketpair()
        self.addCleanup(up_far.close)
        srv = mag.CodexRelayServer(0, self.auth)
        srv.connect_upstream = lambda: up_ours
        srv.start()
        self.addCleanup(srv.stop)
        port = srv.sock.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"GET /backend-api/codex/models HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                           b"Authorization: Bearer main-token\r\nChatGPT-Account-Id: acc-main\r\n\r\n")
            up_far.settimeout(5)
            got = b""
            while b"\r\n\r\n" not in got:
                got += up_far.recv(4096)
            self.assertIn(b"Host: chatgpt.com", got)
            self.assertIn(b"ChatGPT-Account-Id: acc-spare", got)
            self.assertNotIn(b"main-token", got)
            up_far.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            client.settimeout(5)
            back = b""
            while not back.endswith(b"ok"):
                back += client.recv(4096)
            self.assertTrue(back.startswith(b"HTTP/1.1 200"))


class Config(CodexBase):
    def setUp(self):
        super().setUp()
        self.path = mag.codex_config_path()
        with open(self.path, "w") as f:
            f.write('model = "gpt-5"\n\n[mcp_servers.x]\ncommand = "x"\n')

    def read(self):
        with open(self.path) as f:
            return f.read()

    def test_on_adds_one_top_level_line_and_off_restores(self):
        before = self.read()
        with contextlib.redirect_stdout(io.StringIO()):
            mag.codex_config_apply(True)
            mag.codex_config_apply(True)            # repeated on changes nothing
        text = self.read()
        self.assertTrue(mag.codex_config_installed())
        self.assertEqual(text.count("openai_base_url"), 1)
        self.assertLess(text.index("openai_base_url"), text.index("[mcp_servers.x]"))
        with contextlib.redirect_stdout(io.StringIO()):
            mag.codex_config_apply(False)
        self.assertEqual(self.read(), before)
        self.assertFalse(mag.codex_config_installed())

    def test_someone_elses_base_url_is_a_conflict(self):
        with open(self.path, "w") as f:
            f.write('openai_base_url = "https://proxy.example/v1"\n')
        self.assertIn("proxy.example", mag.codex_config_conflict())
        with contextlib.redirect_stdout(io.StringIO()):
            mag.codex_config_apply(False)
        self.assertIn("proxy.example", self.read())     # never removed


@unittest.skipIf(mag.IS_WINDOWS, "the Codex relay is not available on Windows")
class Switching(CodexBase):
    def setUp(self):
        super().setUp()
        self.installed = []
        self.patch("codex_install_auth", lambda a: self.installed.append(a["tokens"]["account_id"]))
        self.patch("codex_sync_live", lambda: "cx-main")
        self.patch("codex_usage_snapshot", lambda slug, refresh=False: {})
        self.patch("codex_apply_selection", lambda: self.fail("the shared server must keep the main login"))
        self.patch("watch_daemon_state", lambda: mag.T("running", "稼働中"))

    def test_switch_changes_usage_not_login(self):
        mag.set_current("codex", "cx-main")
        self.assertTrue(mag.do_load("cx-spare", "manual"))
        self.assertEqual(self.installed, [])
        self.assertEqual(mag.get_current("codex"), "cx-spare")

    def test_reconcile_keeps_the_paying_account(self):
        self.patch("sync_live_credentials", lambda: None)
        mag.reconcile_current()
        self.assertEqual(mag.get_current("codex"), "cx-spare")

    def test_choosing_main_turns_the_relay_on_and_clear_turns_it_off(self):
        mag.set_codex_relay_enabled(False)
        mag.set_main(None, "codex")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mag.cmd_main(argparse.Namespace(name="cx-main", provider=None)), 0)
            self.assertTrue(mag.codex_relay_enabled())
            self.assertTrue(mag.codex_config_installed())
            self.assertEqual(mag.get_current("codex"), "cx-spare")      # usage stays where it was
            self.assertEqual(mag.cmd_main(argparse.Namespace(name="clear", provider="codex")), 0)
        self.assertFalse(mag.codex_relay_enabled())
        self.assertFalse(mag.codex_config_installed())
        self.assertEqual(mag.get_current("codex"), "cx-main")
        self.assertIsNone(mag.main_slug("codex"))


class RefreshLock(CodexBase):
    def test_concurrent_refreshes_spend_the_token_once(self):
        calls = []

        def refresh(slug, a, verify=False):
            if a["tokens"]["refresh_token"] == "rt-spare":
                calls.append(1)
                time.sleep(0.2)
                new = auth("spare", refresh="rt-spare-2")
                self.store[slug] = new
                return new, None, True
            return a, None, False
        self.patch("codex_ensure_fresh", refresh)
        out = []
        threads = [threading.Thread(target=lambda: out.append(mag.codex_fresh_locked("cx-spare")))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(out), 3)
