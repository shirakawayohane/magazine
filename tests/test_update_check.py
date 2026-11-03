"""Any command checks for a newer release at most once a day, in the background."""
import io
import os
import sys
import time
from unittest.mock import patch

from test_mag import Base, mag


class Tty(io.StringIO):
    def isatty(self):
        return True


class UpdateCheck(Base):
    def setUp(self):
        super().setUp()
        if os.path.exists(mag.UPDATE_PATH):
            os.remove(mag.UPDATE_PATH)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MAGAZINE_NO_UPDATE_CHECK", None)
        self.spawned = []
        self.patch("start_update_check", lambda: (self.spawned.append(1),
                                                  mag.write_json(mag.UPDATE_PATH, {**mag.update_info(),
                                                                                   "checked_at": time.time()})))
        self.patch("VERSION", "0.1.3")

    def run_cmd(self, cmd="limits"):
        err = Tty()
        with patch.object(sys, "stderr", err):
            mag.maybe_check_update(cmd)
        return err.getvalue()

    def test_checks_once_a_day(self):
        self.run_cmd()
        self.run_cmd()
        self.assertEqual(len(self.spawned), 1)
        mag.write_json(mag.UPDATE_PATH, {"checked_at": time.time() - mag.UPDATE_INTERVAL - 1})
        self.run_cmd()
        self.assertEqual(len(self.spawned), 2)

    def test_newer_release_is_announced_once_a_day(self):
        mag.write_json(mag.UPDATE_PATH, {"checked_at": time.time(), "latest": "v0.1.10"})
        self.assertIn("v0.1.10", self.run_cmd())
        self.assertEqual(self.run_cmd(), "")

    def test_same_or_older_release_is_quiet(self):
        for tag in ("v0.1.3", "v0.1.2", "garbage"):
            mag.write_json(mag.UPDATE_PATH, {"checked_at": time.time(), "latest": tag})
            self.assertEqual(self.run_cmd(), "", tag)

    def test_automatic_commands_and_pipes_are_quiet(self):
        mag.write_json(mag.UPDATE_PATH, {"checked_at": time.time(), "latest": "v0.2.0"})
        for cmd in mag.UPDATE_QUIET_COMMANDS:
            self.assertEqual(self.run_cmd(cmd), "", cmd)
        with patch.object(sys, "stderr", io.StringIO()) as err:
            mag.maybe_check_update("limits")
        self.assertEqual(err.getvalue(), "")

    def test_can_be_turned_off(self):
        mag.write_json(mag.CONFIG_PATH, {"update_check": False})
        mag.write_json(mag.UPDATE_PATH, {"checked_at": 0, "latest": "v9.0.0"})
        self.assertEqual(self.run_cmd(), "")
        self.assertEqual(self.spawned, [])

    def test_fetch_records_the_latest_tag_and_survives_errors(self):
        self.patch("http_json", lambda url, **kw: {"tag_name": "v0.2.0", "html_url": "https://example.invalid/r"})
        mag.fetch_latest_release()
        self.assertEqual(mag.available_update(), "v0.2.0")

        def offline(url, **kw):
            raise OSError("offline")
        self.patch("http_json", offline)
        mag.fetch_latest_release()
        self.assertEqual(mag.available_update(), "v0.2.0")        # last known answer is kept
        self.assertEqual(mag.update_info()["error"], "OSError")

    def test_version_order(self):
        self.assertGreater(mag.version_tuple("v0.1.10"), mag.version_tuple("0.1.9"))
        self.assertEqual(mag.version_tuple("v1.2.3-rc1"), (1, 2, 3))
