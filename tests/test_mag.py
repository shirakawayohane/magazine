#!/usr/bin/env python3
"""magazine の回帰テスト。

ここに並んでいるのは、実際のアカウントで踏んだ間違いをそのまま固定したもの。
どれも「使えるアカウントを誤って外す」「使えないアカウントを掴み続ける」形で
実害が出た経路なので、壊れたら気づけるようにしておく。

実行:  python3 -m unittest discover -s tests -v
       ./tests/run.sh

テストは本物のキーチェーン・本物のアカウント・ネットワークに触れない。
データ置き場は MAGAZINE_HOME で一時ディレクトリに逃がし、外に出る関数は差し替える。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

# mag.py は取り込み時に置き場と言語を決めるので、その前に環境を固定する
_TMP_HOME = tempfile.mkdtemp(prefix="magazine-test-")
os.environ["MAGAZINE_HOME"] = _TMP_HOME
os.environ["MAGAZINE_LANG"] = "en"

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location("mag", os.path.join(_HERE, "..", "mag.py"))
mag = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mag)


def tearDownModule():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


class Base(unittest.TestCase):
    """毎回まっさらな状態から始め、外に出る操作は差し替える。"""

    def setUp(self):
        for name in ("accounts.json", "state.json", "config.json"):
            p = os.path.join(mag.ROOT, name)
            if os.path.exists(p):
                os.remove(p)
        self._kc = {}
        self._patched = []
        self.patch("kc_read", lambda s, a: self._kc.get((s, a)))
        self.patch("kc_write", lambda s, a, d: self._kc.__setitem__((s, a), d))
        self.patch("kc_delete", lambda s, a: self._kc.pop((s, a), None))
        # ネットワークに出る口を塞ぐ。使うテストでは個別に差し替える。
        self.patch("http_json", self._no_network)
        self.patch("log", lambda *a, **k: None)

    def tearDown(self):
        for name, original in reversed(self._patched):
            setattr(mag, name, original)

    def patch(self, name, replacement):
        self._patched.append((name, getattr(mag, name)))
        setattr(mag, name, replacement)

    @staticmethod
    def _no_network(*a, **k):
        raise AssertionError("テスト中にネットワークへ出ようとしました")

    def add_account(self, slug, provider="claude", email=None, label=None):
        accs = mag.accounts()
        accs.append({"slug": slug, "provider": provider,
                     "email": email or f"{slug}@example.com", "label": label or slug})
        mag.save_accounts(accs)


# ── 上限データの読み方 ────────────────────────────────────────────────────
class UsageInterpretation(Base):
    """使用量レスポンスの解釈。ここを誤ると使えるアカウントを捨てる。"""

    def test_model_scoped_weekly_does_not_become_account_wide(self):
        # 特定モデルの週次枠が満杯でも、他モデルは動く。全体扱いにしてはいけない。
        u = {"five_hour": {"utilization": 5.0, "resets_at": None},
             "seven_day": {"utilization": 91.0, "resets_at": None},
             "limits": [
                 {"kind": "weekly_scoped", "group": "weekly", "percent": 100,
                  "scope": {"model": {"display_name": "Fable"}}, "resets_at": None},
             ]}
        s = mag.usage_summary(u)
        self.assertEqual(s["seven_day"]["pct"], 91.0, "モデル別枠を全体に混ぜてはいけない")
        self.assertEqual([x["model"] for x in s["scoped"]], ["Fable"])
        self.assertEqual(s["scoped"][0]["pct"], 100)

    def test_unscoped_weekly_does_count_and_takes_the_stricter_value(self):
        u = {"five_hour": {"utilization": 1.0}, "seven_day": {"utilization": 40.0},
             "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 77,
                         "scope": None, "resets_at": None}]}
        s = mag.usage_summary(u)
        self.assertEqual(s["seven_day"]["pct"], 77, "スコープ無しの週次は全体の枠")

    def test_metered_billing_state_is_surfaced(self):
        u = {"five_hour": {"utilization": 0}, "seven_day": {"utilization": 0},
             "extra_usage": {"is_enabled": False, "spend_limit_reached": True,
                             "disabled_reason": "org_level_disabled_until"}}
        s = mag.usage_summary(u)
        self.assertTrue(s["metered"]["limit_reached"])
        self.assertFalse(s["metered"]["enabled"])


class AccountUsability(Base):
    """「このアカウントを使ってよいか」の判定。"""

    def test_usage_api_429_does_not_bench_the_account(self):
        # 使用量APIには独自の制限があり、推論が通る状態でも429を返す。
        # これを枯渇と解釈すると、生きているアカウントを捨てることになる。
        self.add_account("a")
        self.patch("stored_oauth", lambda s: {"accessToken": "x", "refreshToken": "y",
                                              "expiresAt": (time.time() + 9999) * 1000})
        self.patch("ensure_fresh", lambda s, o: o)

        def limited(*a, **k):
            raise mag.Limited("five_hour", None)
        self.patch("fetch_usage", limited)

        p = mag.probe("a")
        self.assertFalse(p["ok"])
        self.assertTrue(p["info_unavailable"])
        self.assertFalse(p.get("dead"))
        ok, _why = mag.is_usable("a", p)
        self.assertTrue(ok, "残量が読めないだけで使用不可にしてはいけない")

    def test_network_error_is_not_a_limit(self):
        self.add_account("a")
        self.patch("stored_oauth", lambda s: {"accessToken": "x", "refreshToken": "y",
                                              "expiresAt": (time.time() + 9999) * 1000})
        self.patch("ensure_fresh", lambda s, o: o)

        def offline(*a, **k):
            raise OSError("nodename nor servname provided")
        self.patch("fetch_usage", offline)

        p = mag.probe("a")
        self.assertTrue(p.get("offline"))
        self.assertTrue(mag.is_usable("a", p)[0], "圏外は上限ではない")

    def test_revoked_refresh_token_is_skipped(self):
        # 期限切れなのに更新できない＝ refresh token が失効。掴んでも401になる。
        self.add_account("a")
        self.patch("stored_oauth", lambda s: {"accessToken": "x", "refreshToken": "y",
                                              "expiresAt": (time.time() - 10) * 1000})
        self.patch("ensure_fresh", lambda s, o: o)   # 更新できなかった体
        p = mag.probe("a")
        self.assertTrue(p.get("dead"))
        ok, why = mag.is_usable("a", p)
        self.assertFalse(ok)
        self.assertIn("re-login", why)

    def test_threshold_skips_without_recording_a_cooldown(self):
        # 閾値超えは「予防的に飛ばす」であって上限確定ではない。
        # cooldown を張ると、まだ余裕のある枠を使い切らせずに終わる。
        self.add_account("a")
        p = {"ok": True, "five_hour": {"pct": 99.9, "resets_at": None},
             "seven_day": {"pct": 1.0, "resets_at": None}, "scoped": []}
        ok, _ = mag.is_usable("a", p)
        self.assertFalse(ok)
        self.assertEqual(mag.cooldown_left("a"), 0.0, "予防スキップで cooldown を張ってはいけない")


# ── 切り替えの筋道 ────────────────────────────────────────────────────────
class Rotation(Base):
    def setUp(self):
        super().setUp()
        for s in ("a", "b", "c"):
            self.add_account(s)
        self.add_account("cx1", provider="codex")
        self.add_account("cx2", provider="codex")

    def test_advances_in_registration_order_and_wraps(self):
        self.assertEqual(mag.next_slug("a", probe_all=False)[0], "b")
        self.assertEqual(mag.next_slug("b", probe_all=False)[0], "c")
        self.assertEqual(mag.next_slug("c", probe_all=False)[0], "a", "一周したら先頭に戻る")

    def test_skips_accounts_in_cooldown(self):
        mag.set_cooldown("b", "five_hour", time.time() + 3600)
        self.assertEqual(mag.next_slug("a", probe_all=False)[0], "c")

    def test_returns_nothing_when_all_are_limited(self):
        for s in ("a", "b", "c"):
            mag.set_cooldown(s, "five_hour", time.time() + 3600)
        slug, report = mag.next_slug("a", probe_all=False)
        self.assertIsNone(slug)
        self.assertEqual(len(report), 3, "全件について理由が返る")

    def test_rotation_stays_within_a_provider(self):
        self.assertEqual(mag.next_slug("cx1", probe_all=False, provider="codex")[0], "cx2")
        self.assertEqual(mag.next_slug("cx2", probe_all=False, provider="codex")[0], "cx1")
        for s in ("a", "b", "c"):
            self.assertNotIn(s, [r[0] for r in
                                 mag.next_slug("cx1", probe_all=False, provider="codex")[1]])

    def test_has_spare_is_scoped_to_the_provider(self):
        mag.set_cooldown("cx2", "five_hour", time.time() + 3600)
        self.assertFalse(mag.has_spare("cx1", "codex"), "codex 側は他に空きがない")
        self.assertTrue(mag.has_spare("a", "claude"), "claude 側には空きがある")

    def test_expired_cooldowns_are_released(self):
        mag.set_cooldown("b", "five_hour", time.time() - 1)
        mag.clear_expired_cooldowns()
        self.assertEqual(mag.cooldown_left("b"), 0.0)


class MisattributionGuards(Base):
    """切り替え直後の測定値を、切り替えた先のものと取り違えない。

    Claude Code は直近の応答に付いてきた残量を保持しているので、入れ替えた直後は
    前のアカウントの数値を報告し続ける。これを新しい方の数値として扱った結果、
    まっさらなアカウントを 98%扱いで即座に外し、無限に切り替わる不具合を出した。
    """

    def setUp(self):
        super().setUp()
        self.add_account("a")
        self.add_account("b")

    def test_readings_from_before_the_swap_are_ignored(self):
        mag.set_current("claude", "b")          # ここで last_switch_by が入る
        just_switched = time.time()
        self.assertFalse(mag.switch_grace_ok(just_switched - 10),
                         "切り替え前の測定値は前のアカウントのもの")
        self.assertTrue(mag.switch_grace_ok(just_switched + 120),
                        "十分あとの測定値は信用してよい")

    def test_a_claim_contradicted_by_measurement_is_dropped(self):
        self.patch("probe", lambda slug, quiet=True: {
            "ok": True, "five_hour": {"pct": 7.0, "resets_at": None},
            "seven_day": {"pct": 3.0, "resets_at": None}, "scoped": []})
        recorded = mag.set_cooldown_verified("a", "five_hour", None, source="98% と申告")
        self.assertFalse(recorded, "実測7%のアカウントを98%の申告で外してはいけない")
        self.assertEqual(mag.cooldown_left("a"), 0.0)

    def test_a_claim_backed_by_measurement_is_recorded(self):
        reset = time.time() + 1800
        self.patch("probe", lambda slug, quiet=True: {
            "ok": True, "five_hour": {"pct": 100.0, "resets_at": reset},
            "seven_day": {"pct": 3.0, "resets_at": None}, "scoped": []})
        self.assertTrue(mag.set_cooldown_verified("a", "five_hour", None, source="実ヒット"))
        self.assertGreater(mag.cooldown_left("a"), 0)
        self.assertAlmostEqual(mag.state()["cooldowns"]["a"]["until"], reset, places=0,
                               msg="復帰時刻は実測の resets_at を採る")


# ── Codex ────────────────────────────────────────────────────────────────
class CodexUsage(Base):
    def setUp(self):
        super().setUp()
        self.sessions = tempfile.mkdtemp(prefix="codex-sessions-")
        self.patch("CODEX_SESSIONS_DIR", self.sessions)
        self.addCleanup(shutil.rmtree, self.sessions, ignore_errors=True)

    def write_record(self, name, ts, pct, limit_id="codex", window=10080):
        rec = {"timestamp": ts, "type": "event_msg",
               "payload": {"type": "token_count",
                           "rate_limits": {"limit_id": limit_id, "plan_type": "pro",
                                           "primary": {"used_percent": pct,
                                                       "window_minutes": window,
                                                       "resets_at": time.time() + 600},
                                           "secondary": None}}}
        with open(os.path.join(self.sessions, name), "w") as f:
            f.write(json.dumps(rec) + "\n")

    def test_reads_the_latest_usage(self):
        self.write_record("s1.jsonl", "2026-07-27T10:00:00Z", 40.0)
        self.write_record("s2.jsonl", "2026-07-27T11:00:00Z", 73.0)
        lim = mag.codex_live_limits()
        self.assertEqual(lim["windows"][0]["pct"], 73.0)

    def test_limited_time_model_quota_is_a_separate_budget(self):
        # Spark 等の期間限定枠は limit_id が別。通常枠と混ぜると誤判定になる。
        self.write_record("normal.jsonl", "2026-07-27T10:00:00Z", 12.0, limit_id="codex")
        self.write_record("spark.jsonl", "2026-07-27T11:00:00Z", 100.0,
                          limit_id="codex_bengalfox")
        lim = mag.codex_live_limits()
        self.assertEqual(lim["windows"][0]["pct"], 12.0, "別枠の100%を通常枠として読まない")

    def test_records_predating_the_swap_are_excluded(self):
        self.write_record("old.jsonl", "2026-07-27T10:00:00Z", 95.0)
        cutoff = mag.parse_iso("2026-07-27T12:00:00Z")
        self.assertIsNone(mag.codex_live_limits(only_after=cutoff),
                          "切り替え前の記録は別アカウントの数値")

    def test_window_is_labelled_by_its_length(self):
        self.write_record("w.jsonl", "2026-07-27T10:00:00Z", 5.0, window=300)   # 5時間
        self.assertIn("5", mag.codex_live_limits()["windows"][0]["label"])


class CodexIdentity(Base):
    def test_plan_and_email_come_out_of_the_id_token(self):
        import base64
        claims = {"email": "someone@example.com",
                  "https://api.openai.com/auth": {"chatgpt_plan_type": "team",
                                                  "chatgpt_account_id": "acc-1"}}
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        auth = {"tokens": {"id_token": f"header.{body}.sig"}}
        ident = mag.codex_identity(auth)
        self.assertEqual(ident["email"], "someone@example.com")
        self.assertEqual(ident["plan"], "team")
        self.assertEqual(ident["account_id"], "acc-1")

    def test_a_broken_token_does_not_raise(self):
        self.assertEqual(mag.codex_identity({"tokens": {"id_token": "garbage"}})["email"],
                         "unknown")
        self.assertEqual(mag.codex_identity({})["email"], "unknown")


# ── 状態の持ち方 ──────────────────────────────────────────────────────────
class StateHandling(Base):
    def test_legacy_single_account_state_still_reads(self):
        # 以前は current が文字列だった。壊さず読めること。
        mag.save_state({"current": "old-slug", "cooldowns": {}})
        self.assertEqual(mag.get_current("claude"), "old-slug")
        self.assertIsNone(mag.get_current("codex"))

    def test_providers_track_their_own_current_account(self):
        mag.set_current("claude", "a")
        mag.set_current("codex", "cx1")
        self.assertEqual(mag.get_current("claude"), "a")
        self.assertEqual(mag.get_current("codex"), "cx1")

    def test_accounts_without_a_provider_field_are_claude(self):
        mag.save_accounts([{"slug": "legacy"}])
        self.assertEqual(mag.provider_of(mag.accounts()[0]), "claude")
        self.assertEqual([a["slug"] for a in mag.accounts_of("claude")], ["legacy"])


class NameResolution(Base):
    def setUp(self):
        super().setUp()
        self.add_account("mikuto-matsuo-example-co-jp", email="mikuto@example.co.jp",
                         label="main")
        self.add_account("hanoruru-stream-example-com", email="hanoruru@example.com",
                         label="sub")

    def test_exact_slug(self):
        acct, err = mag.resolve_account("hanoruru-stream-example-com")
        self.assertEqual(acct["label"], "sub", err)

    def test_partial_and_label_and_email(self):
        for needle in ("hanoruru", "sub", "hanoruru@example.com"):
            acct, err = mag.resolve_account(needle)
            self.assertIsNotNone(acct, f"{needle}: {err}")
            self.assertEqual(acct["label"], "sub")

    def test_ambiguous_input_is_refused_rather_than_guessed(self):
        acct, err = mag.resolve_account("example")
        self.assertIsNone(acct, "曖昧なまま勝手に選ばない")
        self.assertIn("matches several", err)

    def test_unknown_input(self):
        acct, err = mag.resolve_account("nope")
        self.assertIsNone(acct)
        self.assertIn("no account", err)


class Helpers(Base):
    def test_slugify(self):
        self.assertEqual(mag.slugify("Mikuto.Matsuo@Example.co.jp"),
                         "mikuto-matsuo-example-co-jp")
        self.assertEqual(mag.slugify(""), "acct")

    def test_parse_iso_accepts_the_shapes_the_apis_return(self):
        self.assertIsNotNone(mag.parse_iso("2026-07-27T10:00:00Z"))
        self.assertIsNotNone(mag.parse_iso("2026-07-26T12:50:00.601595+00:00"))
        self.assertIsNone(mag.parse_iso(None))
        self.assertIsNone(mag.parse_iso("not a date"))

    def test_bar_stays_within_bounds(self):
        for pct in (0, 50, 100, 150, None):
            out = mag.bar(pct)
            self.assertIsInstance(out, str)
            self.assertLessEqual(out.count("█"), 10)

    def test_config_defaults_are_present(self):
        cfg = mag.config()
        for key in ("hotswap_threshold", "five_hour_threshold", "seven_day_threshold",
                    "warm_threshold", "poll_schedule"):
            self.assertIn(key, cfg)
        self.assertLess(cfg["hotswap_threshold"], 100,
                        "枠を超える前に動かないと従量課金に流れる")


if __name__ == "__main__":
    unittest.main(verbosity=2)
