#!/usr/bin/env python3
"""Capture real magazine command output using synthetic, offline account fixtures.

No real login, Keychain access, CLI processes or network requests are allowed.
Run from a clone: python3 scripts/demo.py [--json]
"""
import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch


def capture():
    with tempfile.TemporaryDirectory(prefix="magazine-demo-") as directory:
        root = Path(directory)
        with patch.dict(os.environ, {"MAGAZINE_HOME": str(root / "magazine"), "MAGAZINE_LANG": "en"}):
            spec = importlib.util.spec_from_file_location("magazine_demo", Path(__file__).resolve().parents[1] / "mag.py")
            mag = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mag)
        mag.use_keychain = lambda: False
        mag.claude_config_dir = lambda: str(root / "claude")
        mag.CODEX_AUTH_PATH = str(root / "codex" / "auth.json")
        mag.PROJECTS_DIR = str(root / "projects")
        mag.now = lambda: 1789034400.0
        mag.fmt_when = lambda epoch: "in 2h" if epoch else "?"

        def forbidden(*args, **kwargs):
            raise AssertionError("The offline demo attempted external access")

        def fake_http(url, token=None, **kwargs):
            if url == mag.PROFILE_URL:
                return {"account": {"email": f"{token.removeprefix('sk-ant-demo-')}@example.com"}}
            if url == mag.USAGE_URL:
                used = 97.0 if token == "sk-ant-demo-main" else 12.0
                return {"five_hour": {"utilization": used, "resets_at": "2026-09-10T14:00:00Z"},
                        "seven_day": {"utilization": 35.0 if token == "sk-ant-demo-main" else 8.0}}
            return forbidden()

        mag.http_json = fake_http
        scenes = []

        def command(title, argv, seconds=6, note=""):
            stream = io.StringIO()
            with patch.object(sys, "argv", ["mag", *argv]), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                rc = mag.main()
            if rc:
                raise AssertionError(f"{argv}: exit {rc}: {stream.getvalue()}")
            output = re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue()).strip()
            output = output.replace(str(root), "/demo")
            scenes.append({"title": title, "command": "mag " + " ".join(argv), "output": output,
                           "seconds": seconds, "note": note})

        with patch.object(mag.subprocess, "run", forbidden), patch.object(mag.subprocess, "Popen", forbidden), patch.object(mag.os, "execv", forbidden), patch.object(mag.urllib.request, "urlopen", forbidden):
            # The CLI's real registration and file-backed credential storage paths.
            for name in ("main", "spare"):
                mag.write_live_creds({"claudeAiOauth": {"accessToken": f"sk-ant-demo-{name}", "refreshToken": f"demo-{name}",
                                                       "expiresAt": (mag.now() + 86400) * 1000}})
                with contextlib.redirect_stdout(io.StringIO()):
                    assert mag.cmd_add(argparse.Namespace(provider="claude", alias=name)) == 0
            assert mag.do_load(mag.accounts_of("claude")[0]["slug"], "demo setup")
            claim = base64.urlsafe_b64encode(json.dumps({"email": "codex@example.com", "exp": mag.now() + 86400}).encode()).decode().rstrip("=")
            auth = {"tokens": {"access_token": f"demo.{claim}.fixture", "refresh_token": "demo-codex", "id_token": f"demo.{claim}.fixture"}}
            Path(mag.CODEX_AUTH_PATH).parent.mkdir()
            mag.codex_install_auth(auth)
            with contextlib.redirect_stdout(io.StringIO()):
                assert mag.cmd_add(argparse.Namespace(provider="codex", alias="codex-main")) == 0
            # Feed a synthetic journal result at the provider boundary.
            mag.codex_live_limits = lambda **kwargs: {"windows": [{"label": "weekly", "pct": 42.0, "window_minutes": 10080, "resets_at": mag.now() + 7200}]}
            command("01 / See every account", ["limits"], 9, "Synthetic accounts. Real command output. No network.")
            command("02 / Switch by name", ["use", "spare"], 6, "Claude Code can pick up changed credentials; timing depends on its version.")
            assert mag.current_oauth()["accessToken"] == "sk-ant-demo-spare"
            command("03 / Check the active account", ["limits"], 9, "The marker moves to spare. Claude and Codex rotate separately.")
            # A recorded limit message, without inventing an AI answer or replay.
            project = Path(mag.PROJECTS_DIR) / "demo-project"
            project.mkdir(parents=True)
            session = project / "demo-session.jsonl"
            session.write_text(json.dumps({"type": "assistant", "cwd": "/demo/project", "timestamp": "2026-09-10T11:58:00Z",
                                            "message": {"content": "hit your session limit"}}) + "\n", encoding="utf-8")
            os.utime(session, (mag.now(), mag.now()))
            command("04 / Find interrupted work", ["stalled"], 8, "Session discovery shown with a synthetic Claude Code journal.")
            mag.find_claude_bin = lambda: "claude"
            command("05 / Preview the resume command", ["resume", "demo-session", "--dry-run"], 8,
                    "Preview only. Codex switches require a new Codex launch.")
        return scenes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    scenes = capture()
    if args.json:
        print(json.dumps(scenes, ensure_ascii=False, indent=2))
    else:
        print("OFFLINE DEMO — synthetic accounts, real magazine commands; no live AI session.\n")
        for scene in scenes:
            print(f"$ {scene['command']}\n{scene['output']}\n")
