"""The main account collects saved work; Claude no longer reports a five-hour window."""
import argparse
import time
from unittest.mock import patch

import test_manual_selection
from test_mag import mag

Harness = test_manual_selection.Fixture


def weekly(pct):
    # Claude subscriptions now return `five_hour: null`.
    return {"ok": True, "five_hour": None,
            "seven_day": {"pct": pct, "resets_at": time.time() + 3600}, "scoped": []}


class MainAccount(Harness):
    def setUp(self):
        super().setUp()
        self.usage = {"main": weekly(10), "fourdesign": weekly(10)}

    def set_main(self, name):
        self.assertEqual(mag.cmd_main(argparse.Namespace(name=name, clear=None)), 0)

    def test_one_main_per_provider(self):
        self.set_main("main")
        self.set_main("cx-spare")
        self.set_main("fourdesign")
        self.assertEqual(mag.main_slug("claude"), "fourdesign")
        self.assertEqual(mag.main_slug("codex"), "cx-spare")
        mag.cmd_main(argparse.Namespace(name="clear", provider="claude"))
        self.assertIsNone(mag.main_slug("claude"))
        self.assertEqual(mag.main_slug("codex"), "cx-spare")
        mag.cmd_main(argparse.Namespace(name="clear", provider=None))
        self.assertIsNone(mag.main_slug("codex"))

    def test_clear_is_reserved(self):
        with patch("sys.stderr"):
            self.assertEqual(mag.cmd_rename(argparse.Namespace(name="fourdesign", new_name="Clear")), 1)
            self.assertEqual(mag.cmd_main(argparse.Namespace(name="main", provider="codex")), 1)
        self.assertIsNotNone(mag.find_account("fourdesign"))
        self.assertIsNone(mag.main_slug("claude"))

    def test_rotation_prefers_main_when_usable(self):
        self.add_account("third")
        self.usage["third"] = weekly(10)
        self.set_main("main")
        # Order after `fourdesign` would be third → main; main comes first instead.
        slug, _ = mag.next_slug("fourdesign")
        self.assertEqual(slug, "main")
        mag.set_cooldown("main", "seven_day", time.time() + 3600)
        slug, _ = mag.next_slug("fourdesign")
        self.assertEqual(slug, "third")

    def test_watch_returns_to_main_once_it_recovers(self):
        self.set_main("main")
        mag.set_current("claude", "fourdesign")          # automatic move to a spare
        self.usage["main"] = weekly(99)
        self.watch_once()
        self.assertEqual(mag.get_current("claude"), "fourdesign")
        self.usage["main"] = weekly(5)
        self.watch_once()
        self.assertEqual(mag.get_current("claude"), "main")

    def test_manual_choice_is_not_pulled_back_to_main(self):
        self.set_main("main")
        self.use("fourdesign")
        self.watch_once()
        self.auto()
        self.assertEqual(mag.get_current("claude"), "fourdesign")

    def test_auto_returns_to_main_only_when_it_can_check(self):
        self.set_main("main")
        mag.set_current("claude", "fourdesign")
        self.auto(no_probe=True)
        self.assertEqual(mag.get_current("claude"), "fourdesign")
        self.auto()
        self.assertEqual(mag.get_current("claude"), "main")

    def test_codex_returns_to_main_after_its_cooldown(self):
        self.set_main("cx-main")
        mag.set_current("codex", "cx-spare")
        mag.set_cooldown("cx-main", "seven_day", time.time() + 3600)
        self.watch_once()
        self.assertEqual(mag.get_current("codex"), "cx-spare")
        mag.set_cooldown("cx-main", "seven_day", time.time() - 1)
        self.watch_once()
        self.assertEqual(mag.get_current("codex"), "cx-main")

    def test_main_mark_follows_rename_and_remove(self):
        self.set_main("fourdesign")
        mag.cmd_rename(argparse.Namespace(name="fourdesign", new_name="work"))
        self.assertEqual(mag.main_slug("claude"), "fourdesign")
        self.assertTrue(mag.main_mark("fourdesign"))
        mag.cmd_remove(argparse.Namespace(name="work", yes=True))
        self.assertIsNone(mag.main_slug("claude"))


class NoFiveHourWindow(Harness):
    def test_status_shows_only_reported_windows(self):
        self.usage = {"main": weekly(40), "fourdesign": weekly(10)}
        mag.cmd_status(argparse.Namespace(quick=False))
        out = self.output.getvalue()
        self.assertIn("7d", out)
        self.assertNotIn("5h", out)

    def test_watch_switches_on_weekly_when_five_hour_is_missing(self):
        mag.set_current("claude", "main")
        self.usage = {"main": weekly(98), "fourdesign": weekly(10)}
        self.watch_once()
        self.assertEqual(mag.get_current("claude"), "fourdesign")

    def test_unknown_limit_kind_is_rechecked_soon(self):
        mag.set_cooldown("main", None, None)
        cd = mag.state()["cooldowns"]["main"]
        self.assertEqual(cd["kind"], "unknown")
        self.assertLess(cd["until"] - time.time(), 6 * 3600)

    def test_stale_five_hour_records_are_hidden(self):
        mag.record_limits("main", {"windows": [
            {"label": "5-hour", "pct": 100, "resets_at": 1},
            {"label": "weekly", "pct": 13, "resets_at": 1}]})
        self.patch("probe", lambda slug, **kw: {"ok": False, "error": "x"})
        self.patch("accounts_of", lambda prov: [a for a in mag.accounts() if mag.provider_of(a) == prov and a["slug"] == "main"])
        rows = mag.collect_limits(parallel_fetch=False)
        labels = [w["label"] for w in rows[0]["windows"]]
        self.assertEqual(labels, ["weekly"])

    def test_live_peak_uses_weekly(self):
        import os
        mag.set_current("claude", "main")
        os.makedirs(mag.LIVE_DIR, exist_ok=True)
        mag.write_json(os.path.join(mag.LIVE_DIR, "s.json"), {
            "ts": time.time(), "slug": "main",
            "five_hour": {"pct": None}, "seven_day": {"pct": 92}})
        self.assertEqual(ORIGINAL_LIVE_PEAK(), 92)


ORIGINAL_LIVE_PEAK = mag.live_peak_pct
