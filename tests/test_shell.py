"""Check that optional wrappers find the installed command from any clone."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipIf(os.name == "nt", "POSIX shell integration")
class ShellIntegration(unittest.TestCase):
    def exercise(self, shell, provider, arguments, expect_auto, proxy="http://127.0.0.1:47821"):
        if not shutil.which(shell):
            self.skipTest(f"{shell} is not installed")
        with tempfile.TemporaryDirectory(prefix="magazine-shell-") as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "accounts.json").write_text("[]")
            source = Path(__file__).resolve().parents[1]
            for name in ("mag", "claude", "codex"):
                script = root / name
                body = '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$MAGAZINE_TEST_TRACE"\n'
                if name == "mag":
                    body += f'if [ "$1" = _claude-launch ]; then exec "{sys.executable}" "{source / "mag.py"}" "$@"; fi\n'
                if name == "claude":
                    body += 'printf "%s\\n" "${HTTPS_PROXY-unset}" >> "$MAGAZINE_TEST_PROXY"\n'
                script.write_text(body)
                script.chmod(0o755)
            trace = root / "trace.txt"
            observed_proxy = root / "proxy.txt"
            env = {"PATH": str(root) + os.pathsep + "/usr/bin:/bin",
                   "MAGAZINE_HOME": str(data), "MAGAZINE_TEST_TRACE": str(trace),
                   "MAGAZINE_SRC": str(root / "not-the-source"),
                   "HTTPS_PROXY": proxy, "MAGAZINE_TEST_PROXY": str(observed_proxy)}
            extension = "fish" if shell == "fish" else "sh"
            wrapper = source / "shell" / f"magazine.{extension}"
            command = f'source "{wrapper}"; {provider} {arguments}'
            flags = ["--no-config"] if shell == "fish" else ["--noprofile", "--norc"]
            result = subprocess.run([shutil.which(shell), *flags, "-c", command], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = trace.read_text().splitlines()
            self.assertEqual(len(lines), 2 if expect_auto or provider == "claude" else 1, lines)
            self.assertTrue(lines[-1].startswith(str(root / provider)))
            if provider == "claude":
                self.assertIn("mag _claude-launch", lines[0])
                self.assertEqual(lines[-1].rstrip(), f"{root / provider} {arguments}".rstrip())
                self.assertEqual(observed_proxy.read_text().strip(),
                                 "unset" if proxy == "http://127.0.0.1:47821" else proxy)
            elif expect_auto:
                self.assertIn("mag auto --no-probe --provider codex", lines[0])

    def test_bash_normal_launch_and_auth_passthrough(self):
        for provider, args, auto in (("claude", "", True), ("codex", "", True),
                                      ("claude", "auth status", False), ("codex", "login", False)):
            with self.subTest(provider=provider, args=args):
                self.exercise("bash", provider, args, auto)

    def test_fish_normal_launch_and_auth_passthrough(self):
        for provider, args, auto in (("claude", "", True), ("codex", "", True),
                                      ("claude", "auth status", False), ("codex", "login", False)):
            with self.subTest(provider=provider, args=args):
                self.exercise("fish", provider, args, auto)

    def test_unrelated_proxy_is_preserved(self):
        self.exercise("bash", "claude", "", True, proxy="http://proxy.example:8080")
