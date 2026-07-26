#!/usr/bin/env python3
"""
claude-magazine — 複数の Claude Max アカウントを「弾倉」のように自動装填する。

- 認証情報は macOS Keychain に置いたまま扱う（平文ファイルには書かない）
- 現用スロット : service="Claude Code-credentials" / account=$USER の JSON 内 claudeAiOauth
- 弾倉スロット : service="claude-magazine"        / account=<slug>
  ※ 現用スロットには MCP の OAuth (mcpOAuth) が同居しているので claudeAiOauth だけを差し替える

検知は 3 系統の多重化:
  1. 実ヒット検知   … claude の端末出力に出る "session limit reached" 等（5h / 7d 両方）
  2. statusLine 経由 … Claude Code が statusLine に渡す rate_limits.*.used_percentage
  3. usage API      … https://api.anthropic.com/api/oauth/usage（起動時・切替時のみの低頻度）
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

VERSION = "0.1.0"

HOME = os.path.expanduser("~")


def _detect_lang() -> str:
    """MAGAZINE_LANG > LC_ALL/LC_MESSAGES/LANG の順に見る。既定は英語。"""
    explicit = os.environ.get("MAGAZINE_LANG")
    if explicit:
        return "ja" if explicit.lower().startswith("ja") else "en"
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(k)
        if v and v.lower().startswith("ja"):
            return "ja"
    return "en"


LANG = _detect_lang()


def T(en: str, ja: str) -> str:
    """表示文字列。CLI では銃の比喩を使わず、素直な語で書く。"""
    return ja if LANG == "ja" else en
ROOT = os.path.join(HOME, ".claude-magazine")
ACCOUNTS_PATH = os.path.join(ROOT, "accounts.json")
STATE_PATH = os.path.join(ROOT, "state.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
LIVE_DIR = os.path.join(ROOT, "live")
LOG_PATH = os.path.join(ROOT, "logs", "mag.log")

LIVE_SERVICE = "Claude Code-credentials"
LIVE_ACCOUNT = os.environ.get("USER") or os.path.basename(HOME)
MAG_SERVICE = "claude-magazine"

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
UA = "claude-cli/2.1.220 (external, cli)"

# ── Codex (ChatGPT) ─────────────────────────────────────────────────────
# Claude と違い認証は Keychain ではなく ~/.codex/auth.json（平文）に入る。
# 弾倉側の保管は Keychain（service=claude-magazine-codex）にして平文コピーを増やさない。
CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
CODEX_MAG_SERVICE = "claude-magazine-codex"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_UA = "codex-cli/0.145.0"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URLS = [
    "https://console.anthropic.com/v1/oauth/token",
    "https://api.anthropic.com/v1/oauth/token",
]

DEFAULT_CONFIG = {
    # statusLine / usage API の使用率がこの値以上なら次弾へ（順送り＝使い切り運用なので高め。
    # 主軸はあくまで実ヒット検知で、これは「取りこぼしの保険」）
    "five_hour_threshold": 99.5,
    "seven_day_threshold": 99.5,
    # 実ヒット検知時、resets_at が取れなかった場合のクールダウン既定値（秒）
    "fallback_cooldown_five_hour": 5 * 3600,
    "fallback_cooldown_seven_day": 7 * 24 * 3600,
    # 装填直後に再ヒットした場合の連続切替の最短間隔（秒）— 暴走防止
    "min_switch_interval": 20,
    # mag run が live ファイルを見に行く間隔（秒）
    "live_poll_interval": 2.0,
    # mag watch（無停止ホットスワップ）が次弾に移る使用率。上限に当たる前に入れ替えるので
    # 実ヒット用の閾値より少しだけ手前に置く。
    "hotswap_threshold": 98.0,
    # 現用の5h使用率がここに達したら、次弾に軽いクエリを1発投げて事前検証する
    # （認証切れ・凍結などを事前に検出する。CLAUDE_CODE_OAUTH_TOKEN 経由で現用 Keychain には触れない）
    "warm_threshold": 50.0,
    "warm_model": "claude-haiku-4-5-20251001",
    # 上限で落ちた後の再開時、自動で送信するプロンプト（空なら送らない）
    "auto_continue_prompt": "",
}

# ── 実ヒット検知パターン ────────────────────────────────────────────────
# Claude Code 内部のラベル: five_hour="session limit", seven_day="weekly limit",
#                           seven_day_opus="Opus limit", seven_day_sonnet="Sonnet limit"
HIT_PATTERNS = [
    (r"(?:session|5[- ]?hour|five[- ]?hour)\s+limit[^\n]{0,60}?(?:reached|exceeded|resets)", "five_hour"),
    (r"(?:weekly|7[- ]?day|seven[- ]?day|Opus|Sonnet)\s+limit[^\n]{0,60}?(?:reached|exceeded|resets)", "seven_day"),
    (r"usage limit reached", None),
    (r"rate[ _-]?limit(?:_error)?[^\n]{0,40}(?:reached|exceeded|error)", None),
    # "You have reached your session limit" のように限定語が後ろに来る形
    (r"(?:reached|hit|exceeded)\s+(?:your\s+)?[^\n]{0,20}?(?:session|5[- ]?hour)\s+limit", "five_hour"),
    (r"(?:reached|hit|exceeded)\s+(?:your\s+)?[^\n]{0,20}?(?:weekly|7[- ]?day)\s+limit", "seven_day"),
    (r"(?:reached|hit|exceeded)\s+(?:your\s+)?[^\n]{0,20}?usage\s+limit", None),
    (r"limit will reset at", None),
    (r"(?:利用|使用)(?:上限|制限)[^\n]{0,20}(?:に達し|を超|超過)", None),
]
# 上限とは無関係な "... limit reached" を弾く
HIT_EXCLUDE = re.compile(
    r"(context limit|subagent|budget limit|fast limit|spend limit|concurrent|"
    r"size limit|recursion|jit stack|token limit|nesting limit|export limit)",
    re.I,
)
HIT_RE = [(re.compile(p, re.I), kind) for p, kind in HIT_PATTERNS]

# Codex (ChatGPT) 側の上限メッセージ。バイナリ内の実文言に合わせてある。
#   "You've hit your usage limit." / "You've hit your usage limit for <window>"
CODEX_HIT_PATTERNS = [
    (r"you'?ve hit your usage limit", None),
    (r"usage limit reached", None),
    (r"rate[ _-]?limit(?:_error)?[^\n]{0,40}(?:reached|exceeded)", None),
    (r"(?:利用|使用)(?:上限|制限)[^\n]{0,20}(?:に達し|を超|超過)", None),
]
CODEX_HIT_RE = [(re.compile(p, re.I), kind) for p, kind in CODEX_HIT_PATTERNS]
# Codex の 5h / 週次どちらに当たったかは文言から拾う
CODEX_WEEKLY_RE = re.compile(r"(weekly|week|7[- ]?day)", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\r")


# ── 小物 ────────────────────────────────────────────────────────────────
def now() -> float:
    return time.time()


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as f:
        f.write(f"[{stamp}] {msg}\n")


def read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(CONFIG_PATH, {}))
    return cfg


def accounts() -> list:
    return read_json(ACCOUNTS_PATH, [])


def save_accounts(a: list) -> None:
    write_json(ACCOUNTS_PATH, a)


def state() -> dict:
    return read_json(STATE_PATH, {"current": None, "cooldowns": {}, "last_switch": 0})


def save_state(s: dict) -> None:
    write_json(STATE_PATH, s)


def parse_iso(ts) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fmt_when(epoch: float | None) -> str:
    if not epoch:
        return "?"
    dt = datetime.fromtimestamp(epoch)
    delta = epoch - now()
    if delta <= 0:
        return "now"
    h, m = int(delta // 3600), int((delta % 3600) // 60)
    rel = f"{h}h{m:02d}m" if h else f"{m}m"
    return (f"{dt.strftime('%m/%d %H:%M')} (in {rel})" if LANG == "en"
            else f"{dt.strftime('%m/%d %H:%M')} (残り{rel})")


# ── Keychain ────────────────────────────────────────────────────────────
def kc_read(service: str, account: str) -> dict | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    raw = r.stdout.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def kc_write(service: str, account: str, data: dict) -> None:
    payload = json.dumps(data, separators=(",", ":"))
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", account,
         "-D", "application password", "-w", payload],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Keychain 書き込み失敗 ({service}/{account}): {r.stderr.strip()}")


def kc_delete(service: str, account: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, text=True)


def live_creds() -> dict:
    return kc_read(LIVE_SERVICE, LIVE_ACCOUNT) or {}


def current_oauth() -> dict | None:
    return live_creds().get("claudeAiOauth")


def install_oauth(oauth: dict) -> None:
    """現用スロットの claudeAiOauth だけを差し替える（mcpOAuth は温存）。

    稼働中のセッションは Keychain を都度読み直すため、書き換えが中途半端だと
    走っているセッションが "Not logged in · Please run /login" を掴んでしまう。
    そのため (1) 事前に中身を検証し (2) 書き戻して読み直しで確認し
    (3) 壊れていたら即座に元へ戻す。
    """
    token = (oauth or {}).get("accessToken")
    if not token or not str(token).startswith("sk-ant-"):
        raise RuntimeError(T("The credential to activate is malformed (bad accessToken)", "有効化しようとした認証情報が壊れています（accessToken が不正）"))
    exp = oauth.get("expiresAt")
    if exp and (exp / 1000.0) <= now():
        raise RuntimeError(T("The credential to activate has an expired accessToken", "有効化しようとした認証情報の accessToken が期限切れです"))

    before = live_creds()
    creds = dict(before)
    creds["claudeAiOauth"] = oauth

    last_err = None
    for attempt in range(3):
        try:
            kc_write(LIVE_SERVICE, LIVE_ACCOUNT, creds)
        except RuntimeError as e:
            last_err = e
            time.sleep(0.2)
            continue
        back = live_creds().get("claudeAiOauth") or {}
        if back.get("accessToken") == token:
            return
        last_err = RuntimeError("書き戻しの確認に失敗しました")
        time.sleep(0.2)

    # ここまで来たら壊れている可能性がある。元の弾に戻す。
    if before.get("claudeAiOauth"):
        try:
            kc_write(LIVE_SERVICE, LIVE_ACCOUNT, before)
            log("install_oauth: 失敗したため元の弾へロールバックしました")
        except RuntimeError:
            log("install_oauth: ロールバックにも失敗（要 `claude auth login`）")
    raise last_err or RuntimeError(T("Failed to write the credential to the keychain", "Keychain への書き込みに失敗しました"))


def stored_oauth(slug: str) -> dict | None:
    d = kc_read(MAG_SERVICE, slug)
    if not d:
        return None
    return d.get("claudeAiOauth", d)


def store_oauth(slug: str, oauth: dict) -> None:
    kc_write(MAG_SERVICE, slug, {"claudeAiOauth": oauth})


# ── OAuth / usage API ───────────────────────────────────────────────────
class Limited(Exception):
    """API が 429 を返した = そのアカウントは上限に当たっている。"""

    def __init__(self, kind: str | None, resets_at: float | None):
        self.kind = kind
        self.resets_at = resets_at


def http_json(url: str, token: str = None, body: dict = None, timeout: int = 15):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            reset = e.headers.get("anthropic-ratelimit-unified-reset")
            status = (e.headers.get("anthropic-ratelimit-unified-status") or "").lower()
            kind = "seven_day" if "week" in status or "seven" in status else "five_hour"
            ra = None
            if reset:
                try:
                    ra = float(reset)
                except ValueError:
                    ra = parse_iso(reset)
            raise Limited(kind, ra)
        raise


def refresh_oauth(oauth: dict) -> dict | None:
    """refreshToken で accessToken を更新した新しい oauth を返す（失敗時 None）。"""
    rt = oauth.get("refreshToken")
    if not rt:
        return None
    body = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID}
    for url in TOKEN_URLS:
        try:
            d = http_json(url, body=body)
        except Limited:
            raise
        except Exception:
            continue
        if not d or "access_token" not in d:
            continue
        new = dict(oauth)
        new["accessToken"] = d["access_token"]
        if d.get("refresh_token"):
            new["refreshToken"] = d["refresh_token"]
        if d.get("expires_in"):
            new["expiresAt"] = int((now() + float(d["expires_in"])) * 1000)
        return new
    return None


def ensure_fresh(slug: str | None, oauth: dict) -> dict:
    """期限切れ間近なら refresh し、保管庫にも書き戻す。"""
    exp = oauth.get("expiresAt")
    if exp and (exp / 1000.0) - now() > 120:
        return oauth
    new = refresh_oauth(oauth)
    if not new:
        return oauth
    if slug:
        store_oauth(slug, new)
    if current_oauth() and current_oauth().get("refreshToken") == oauth.get("refreshToken"):
        install_oauth(new)
    return new


def fetch_usage(oauth: dict) -> dict:
    return http_json(USAGE_URL, token=oauth["accessToken"])


def usage_summary(u: dict) -> dict:
    """アカウント全体の逼迫度を返す。

    weekly_scoped（Fable/Opus など特定モデルだけの週次枠）は、その枠が 100% でも
    他モデルは動く。アカウント全体の可否に混ぜると使える弾を誤って捨てるので、
    全体判定には unscoped の枠だけを使い、モデル別は参考情報として別に返す。
    """
    out = {}
    for k in ("five_hour", "seven_day"):
        blk = (u or {}).get(k) or {}
        out[k] = {
            "pct": blk.get("utilization"),
            "resets_at": parse_iso(blk.get("resets_at")),
        }
    scoped = []
    for lim in (u or {}).get("limits") or []:
        if not isinstance(lim.get("percent"), (int, float)):
            continue
        sc = lim.get("scope") or {}
        model = (sc.get("model") or {}).get("display_name")
        if lim.get("group") == "weekly" and model:
            scoped.append({"model": model, "pct": lim["percent"],
                           "resets_at": parse_iso(lim.get("resets_at"))})
        elif lim.get("group") == "weekly" and not model:
            # unscoped の週次はアカウント全体の枠。より厳しい方を採用する。
            if out["seven_day"]["pct"] is None or lim["percent"] > out["seven_day"]["pct"]:
                out["seven_day"] = {"pct": lim["percent"],
                                    "resets_at": parse_iso(lim.get("resets_at")) or out["seven_day"]["resets_at"]}
    out["scoped"] = scoped
    return out


# ── Codex: 認証スロットの読み書き ───────────────────────────────────────
def codex_live_auth() -> dict | None:
    return read_json(CODEX_AUTH_PATH, None)


def codex_install_auth(auth: dict) -> None:
    """~/.codex/auth.json を差し替える。書き込みは原子的に行い、壊れたら元へ戻す。"""
    tok = (auth or {}).get("tokens") or {}
    if not tok.get("access_token") or not tok.get("refresh_token"):
        raise RuntimeError(T("The Codex credential to activate is malformed", "有効化しようとした Codex の認証情報が壊れています"))
    before = codex_live_auth()
    tmp = CODEX_AUTH_PATH + ".mag.tmp"
    with open(tmp, "w") as f:
        json.dump(auth, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CODEX_AUTH_PATH)
    back = codex_live_auth() or {}
    if (back.get("tokens") or {}).get("access_token") != tok["access_token"]:
        if before:
            with open(tmp, "w") as f:
                json.dump(before, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, CODEX_AUTH_PATH)
        raise RuntimeError(T("Codex credential write-back check failed (rolled back)", "Codex 認証の書き戻し確認に失敗（元に戻しました）"))


def codex_stored_auth(slug: str) -> dict | None:
    return kc_read(CODEX_MAG_SERVICE, slug)


def codex_store_auth(slug: str, auth: dict) -> None:
    kc_write(CODEX_MAG_SERVICE, slug, auth)


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        import base64
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def codex_identity(auth: dict) -> dict:
    """id_token からメールとプランを取り出す。"""
    c = jwt_claims(((auth or {}).get("tokens") or {}).get("id_token") or "")
    a = c.get("https://api.openai.com/auth") or {}
    return {
        "email": c.get("email") or "unknown",
        "plan": a.get("chatgpt_plan_type"),
        "account_id": a.get("chatgpt_account_id"),
        "exp": c.get("exp"),
    }


def codex_refresh(auth: dict) -> dict | None:
    tok = (auth or {}).get("tokens") or {}
    rt = tok.get("refresh_token")
    if not rt:
        return None
    body = {"client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": rt, "scope": "openid profile email"}
    try:
        req = urllib.request.Request(
            CODEX_TOKEN_URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": CODEX_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode())
    except Exception:
        return None
    if not j.get("access_token"):
        return None
    new = json.loads(json.dumps(auth))
    new.setdefault("tokens", {})
    new["tokens"]["access_token"] = j["access_token"]
    for k in ("id_token", "refresh_token"):
        if j.get(k):
            new["tokens"][k] = j[k]
    new["last_refresh"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return new


def codex_ensure_fresh(slug: str | None, auth: dict) -> dict:
    """access_token(JWT) の exp を見て、切れそうなら更新して保管庫へ書き戻す。"""
    exp = jwt_claims(((auth or {}).get("tokens") or {}).get("access_token") or "").get("exp")
    if exp and exp - now() > 300:
        return auth
    new = codex_refresh(auth)
    if not new:
        return auth
    if slug:
        codex_store_auth(slug, new)
    return new


CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")


def codex_live_limits(max_files: int = 20, only_after: float = None) -> dict | None:
    """Codex の利用状況をセッション JSONL から拾う。

    Codex は残量 API を公開していない（Cloudflare 403）が、各ターンの応答に付いてくる
    rate_limits を ~/.codex/sessions/**/*.jsonl に書き出している。ai-limits(1) と同じ源。

    注意点:
      - limit_id が "codex" 以外（期間限定モデルの Spark 枠など）は別枠なので除外する
      - 記録はその時ログインしていたアカウントのもの。装填を切り替えた後は
        切替時刻より新しい記録だけを信用しないと、前のアカウントの数値を読んでしまう
    """
    if not os.path.isdir(CODEX_SESSIONS_DIR):
        return None
    files = []
    for root, _dirs, names in os.walk(CODEX_SESSIONS_DIR):
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            p = os.path.join(root, n)
            try:
                files.append((os.stat(p).st_mtime, p))
            except OSError:
                pass
    files.sort(reverse=True)

    best = None
    for _mt, p in files[:max_files]:
        try:
            with open(p, errors="replace") as f:
                for line in f:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rl = ((d.get("payload") or {}).get("rate_limits")) or {}
                    if (rl.get("limit_id") or "codex") != "codex":
                        continue          # Spark 等の期間限定枠は別勘定
                    if not (rl.get("primary") or {}).get("used_percent") is not None:
                        continue
                    ts = parse_iso(d.get("timestamp")) or 0
                    if only_after and ts <= only_after:
                        continue          # 装填切替より前の記録＝別アカウントの数値
                    if now() - ts > 7 * 24 * 3600:
                        continue
                    if best is None or ts > best["ts"]:
                        best = {"ts": ts, "rl": rl}
        except OSError:
            continue

    if not best:
        return None
    rl = best["rl"]
    out = {"ts": best["ts"], "plan": rl.get("plan_type"),
           "reached": rl.get("rate_limit_reached_type"), "windows": []}
    for key in ("primary", "secondary"):
        b = rl.get(key)
        if not b or b.get("used_percent") is None:
            continue
        wm = b.get("window_minutes") or 0
        out["windows"].append({
            "key": key,
            "pct": b.get("used_percent"),
            "window_minutes": wm,
            "label": (f"{wm // 60}h window" if wm < 10080 else f"{wm // 1440}d window") if LANG == "en"
                     else (f"{wm // 60}時間枠" if wm < 10080 else f"{wm // 1440}日枠"),
            "resets_at": b.get("resets_at"),
        })
    return out


def codex_worst(only_after: float = None) -> dict | None:
    """Codex の最も逼迫している枠を返す。"""
    lim = codex_live_limits(only_after=only_after)
    if not lim or not lim["windows"]:
        return None
    w = max(lim["windows"], key=lambda x: x["pct"])
    return {**w, "plan": lim.get("plan"), "reached": lim.get("reached"), "ts": lim["ts"]}


def find_codex_bin() -> str:
    for c in (os.environ.get("CODEX_BIN"), shutil.which("codex"),
              os.path.expanduser("~/.local/bin/codex"), "/opt/homebrew/bin/codex"):
        if c and os.path.exists(c):
            return c
    return "codex"


def find_claude_bin() -> str:
    """launchd 常駐（mag watch）の PATH は狭いので claude 本体を明示的に探す。"""
    for c in (
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ):
        if c and os.path.exists(c):
            return c
    return "claude"


def auth_status() -> dict:
    r = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


# ── 弾倉ロジック ────────────────────────────────────────────────────────
def provider_of(a: dict) -> str:
    """provider 未設定の既存エントリは claude 扱い（後方互換）。"""
    return a.get("provider") or "claude"


def accounts_of(provider: str) -> list:
    return [a for a in accounts() if provider_of(a) == provider]


def get_current(provider: str) -> str | None:
    s = state()
    cur = s.get("current")
    if isinstance(cur, dict):
        return cur.get(provider)
    # 旧形式（文字列＝claude のスラグ）からの移行
    return cur if provider == "claude" else None


def set_current(provider: str, slug: str) -> None:
    s = state()
    cur = s.get("current")
    if not isinstance(cur, dict):
        cur = {"claude": cur} if cur else {}
    cur[provider] = slug
    s["current"] = cur
    s.setdefault("last_switch_by", {})[provider] = now()
    s["last_switch"] = now()
    save_state(s)


def find_account(slug: str) -> dict | None:
    return next((a for a in accounts() if a["slug"] == slug), None)


PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


def identify_claude_token(token: str) -> str | None:
    """アクセストークンから所有者のメールを引く（結果は state にキャッシュ）。"""
    key = token[-16:]
    cache = (state().get("profile_cache") or {})
    hit = cache.get(key)
    if hit and now() - hit.get("ts", 0) < 24 * 3600:
        return hit.get("email")
    try:
        d = http_json(PROFILE_URL, token=token)
    except Exception:
        return None
    email = ((d or {}).get("account") or {}).get("email")
    if email:
        s = state()
        s.setdefault("profile_cache", {})[key] = {"email": email, "ts": now()}
        save_state(s)
    return email


def sync_live_credentials() -> str | None:
    """現用スロットの最新トークンを、対応する弾の保管庫へ書き戻す。

    Anthropic は refresh token をローテーションする。登録時のスナップショットを
    抱えたままだと、本体がトークンを更新した瞬間に保管庫側が失効して
    「いざ切り替えたら 401」になる。現用の弾は使われるたびに新しくなるので、
    見かけたら保管庫を追従させる。
    """
    live = current_oauth() or {}
    lrt, lat = live.get("refreshToken"), live.get("accessToken")
    if not lrt or not lat:
        return None
    # すでに一致している弾があるなら何もしなくてよい
    for a in accounts_of("claude"):
        if (stored_oauth(a["slug"]) or {}).get("refreshToken") == lrt:
            return a["slug"]
    email = identify_claude_token(lat)
    if not email:
        return None
    for a in accounts_of("claude"):
        if (a.get("email") or "").lower() == email.lower():
            store_oauth(a["slug"], live)
            log(f"sync: {a['slug']} の保管トークンを現用の最新版に更新（rotation 追従）")
            return a["slug"]
    return None


def reconcile_current() -> dict:
    """実際に入っている弾を見て state を実態に合わせる。

    `claude auth login` / `codex login` を手で叩くと弾倉の外から現用スロットが
    変わる。state を信じたまま巡回すると誤ったアカウントを飛ばすので、
    保管してある refresh_token と突き合わせて現在弾を復元する。
    """
    fixed = {}
    sync_live_credentials()   # rotation で保管庫が古びるのを先に埋める
    live = (current_oauth() or {}).get("refreshToken")
    if live:
        for a in accounts_of("claude"):
            st = stored_oauth(a["slug"]) or {}
            if st.get("refreshToken") == live and get_current("claude") != a["slug"]:
                set_current("claude", a["slug"])
                fixed["claude"] = a["slug"]
                log(f"reconcile: claude の現在弾を {a['slug']} に修正")
                break

    cauth = codex_live_auth() or {}
    ctok = (cauth.get("tokens") or {}).get("refresh_token")
    if ctok:
        for a in accounts_of("codex"):
            st = (codex_stored_auth(a["slug"]) or {}).get("tokens") or {}
            if st.get("refresh_token") == ctok and get_current("codex") != a["slug"]:
                set_current("codex", a["slug"])
                fixed["codex"] = a["slug"]
                log(f"reconcile: codex の現在弾を {a['slug']} に修正")
                break
    return fixed


def cooldown_left(slug: str, s: dict = None) -> float:
    s = s or state()
    cd = (s.get("cooldowns") or {}).get(slug)
    if not cd:
        return 0.0
    return max(0.0, float(cd.get("until", 0)) - now())


def set_cooldown(slug: str, kind: str | None, resets_at: float | None) -> None:
    cfg = config()
    kind = kind or "five_hour"
    if not resets_at:
        span = cfg[f"fallback_cooldown_{kind}"]
        resets_at = now() + span
    s = state()
    s.setdefault("cooldowns", {})[slug] = {
        "until": resets_at, "kind": kind, "set_at": now(),
    }
    save_state(s)
    log(f"cooldown: {slug} kind={kind} until={datetime.fromtimestamp(resets_at):%m/%d %H:%M}")


def clear_expired_cooldowns() -> None:
    s = state()
    cds = s.get("cooldowns") or {}
    live = {k: v for k, v in cds.items() if float(v.get("until", 0)) > now()}
    if len(live) != len(cds):
        s["cooldowns"] = live
        save_state(s)


def probe(slug: str, quiet: bool = True) -> dict:
    """1 アカウントの残量を取得する（情報取得のみ。ここでは cooldown を張らない）。

    重要: /api/oauth/usage 自体に固有のレート制限があり、推論が普通に通る状態でも
    429 を返してくることがある。これを「アカウントが上限」と解釈すると、使える弾を
    誤って弾き飛ばす。実際に上限かどうかは推論側のシグナル
    （statusLine の rate_limits / 出力の実ヒット検知）で判断する。
    """
    res = {"slug": slug, "ok": False, "five_hour": None, "seven_day": None,
           "scoped": [], "error": None, "info_unavailable": False}
    oauth = stored_oauth(slug)
    if not oauth:
        res["error"] = T("no stored credential in keychain", "Keychain に認証情報がありません")
        return res
    try:
        oauth = ensure_fresh(slug, oauth)
        exp = oauth.get("expiresAt")
        if exp and (exp / 1000.0) <= now():
            # 期限切れなのに更新できなかった＝refresh token が失効している
            res["error"] = T("needs re-login (refresh token revoked)", "要再ログイン（refresh token 失効）")
            res["dead"] = True
            return res
        u = fetch_usage(oauth)
        s = usage_summary(u)
        res.update({"ok": True, "five_hour": s["five_hour"], "seven_day": s["seven_day"],
                    "scoped": s.get("scoped", [])})
    except Limited:
        # usage エンドポイント側の絞り。アカウントの可否は判定できない＝不明として扱う。
        res["error"] = T("usage API returned 429 (usage unknown; requests may still work)", "使用量APIが429（残量不明・リクエストは通る場合あり）")
        res["info_unavailable"] = True
    except (urllib.error.URLError, OSError) as e:
        # 圏外・スリープ復帰直後など。残量が読めないだけで、アカウントは無事。
        res["error"] = T("network unreachable", "ネットワーク到達不可")
        res["info_unavailable"] = True
        res["offline"] = True
        _ = e
    except Exception as e:
        res["error"] = str(e)[:80]
        res["info_unavailable"] = True
    return res


def is_usable(slug: str, p: dict = None) -> tuple[bool, str]:
    cfg = config()
    left = cooldown_left(slug)
    if left > 0:
        cd = state()["cooldowns"][slug]
        return False, f"cooldown ({cd.get('kind')}) → {fmt_when(cd['until'])}"
    if p is None:
        return True, "unknown"
    if p.get("dead"):
        # トークンが死んでいる弾は装填しても 401 になるだけなので飛ばす
        return False, p.get("error") or T("needs re-login", "要再ログイン")
    if not p["ok"]:
        # 残量が読めないだけでは使用不可にしない。使用量API側の 429 でも推論は通ることがある。
        return True, p.get("error") or T("usage unknown", "残量不明")
    # 閾値超えは「予防的にスキップ」であって上限確定ではないので cooldown は張らない。
    # cooldown は実ヒット（出力の上限メッセージ検知）でのみ張る。
    for kind, th in (("five_hour", cfg["five_hour_threshold"]), ("seven_day", cfg["seven_day_threshold"])):
        pct = (p[kind] or {}).get("pct")
        if pct is not None and pct >= th:
            return False, f"{kind} {pct:.1f}% ≥ 閾値{th:.1f}%"
    return True, "ok"


def do_load(slug: str, reason: str = "") -> bool:
    acct = find_account(slug) or {}
    prov = provider_of(acct) if acct else "claude"

    if prov == "codex":
        auth = codex_stored_auth(slug)
        if not auth:
            print(T(f"✗ {slug}: no stored credential in keychain", f"✗ {slug}: Keychain に認証情報がありません"), file=sys.stderr)
            return False
        auth = codex_ensure_fresh(slug, auth)
        try:
            codex_install_auth(auth)
        except (RuntimeError, OSError) as e:
            print(T(f"✗ failed to activate {slug}: {e}", f"✗ {slug} への切り替えに失敗: {e}"), file=sys.stderr)
            log(f"load failed: {slug}: {e}")
            return False
    else:
        oauth = stored_oauth(slug)
        if not oauth:
            print(T(f"✗ {slug}: no stored credential in keychain", f"✗ {slug}: Keychain に認証情報がありません"), file=sys.stderr)
            return False
        oauth = ensure_fresh(slug, oauth)
        try:
            install_oauth(oauth)
        except RuntimeError as e:
            print(T(f"✗ failed to activate {slug}: {e}", f"✗ {slug} への切り替えに失敗: {e}"), file=sys.stderr)
            log(f"load failed: {slug}: {e}")
            return False

    set_current(prov, slug)
    log(f"load[{prov}]: {slug} ({acct.get('email','?')}) {reason}")
    return True


def next_slug(after: str | None, probe_all: bool = True,
              provider: str = "claude") -> tuple[str | None, list]:
    """順送り（マガジン式）で次に使える弾を返す。巡回は同じ provider 内で閉じる。"""
    clear_expired_cooldowns()
    accs = accounts_of(provider)
    if not accs:
        return None, []
    slugs = [a["slug"] for a in accs]
    start = slugs.index(after) if after in slugs else -1
    report = []
    for i in range(1, len(slugs) + 1):
        cand = slugs[(start + i) % len(slugs)]
        if cooldown_left(cand) > 0:
            cd = state()["cooldowns"][cand]
            report.append((cand, f"cooldown → {fmt_when(cd['until'])}"))
            continue
        # Codex には残量 API が無い（Cloudflare で 403）ので実ヒット検知に委ねる
        p = probe(cand) if (probe_all and provider == "claude") else None
        ok, why = is_usable(cand, p)
        report.append((cand, why))
        if ok:
            return cand, report
    return None, report


def has_spare(except_slug: str | None, provider: str = "claude") -> bool:
    """API を叩かずに、同じ provider 内に切り替え先が残っているかだけ見る。"""
    return any(a["slug"] != except_slug and cooldown_left(a["slug"]) == 0
               for a in accounts_of(provider))


def soonest_reset() -> tuple[str | None, float | None]:
    best = (None, None)
    for a in accounts():
        cd = (state().get("cooldowns") or {}).get(a["slug"])
        if not cd:
            continue
        u = float(cd["until"])
        if best[1] is None or u < best[1]:
            best = (a["slug"], u)
    return best


# ── コマンド ────────────────────────────────────────────────────────────
def slugify(email: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (email or "acct").lower()).strip("-")
    return base[:40] or "acct"


def cmd_add_codex(args) -> int:
    """今 codex にログイン中の ChatGPT アカウントを弾倉に登録する。"""
    auth = codex_live_auth()
    if not auth or not (auth.get("tokens") or {}).get("refresh_token"):
        print(T("✗ Not signed in to codex. Run `codex login` first.",
                "✗ codex にログインしていません。先に `codex login` を実行してください。"), file=sys.stderr)
        return 1
    if auth.get("auth_mode") == "apikey" or auth.get("OPENAI_API_KEY"):
        print(T("✗ This is an API-key login. Only ChatGPT subscription auth is supported.",
                "✗ APIキー方式のログインです。ChatGPT サブスク認証のみ対象です。"), file=sys.stderr)
        return 1

    ident = codex_identity(auth)
    slug = args.slug or ("cx-" + slugify(ident["email"]))
    accs = accounts()
    existing = next((a for a in accs if a["slug"] == slug), None)
    codex_store_auth(slug, auth)
    if existing:
        existing.update({"email": ident["email"], "plan": ident.get("plan"),
                         "label": args.label or existing.get("label") or ident["email"]})
        print(T(f"↻ updated: {slug} ({ident['email']} / {ident.get('plan')})",
                f"↻ 更新: {slug} ({ident['email']} / {ident.get('plan')})"))
    else:
        accs.append({
            "slug": slug,
            "provider": "codex",
            "label": args.label or ident["email"],
            "email": ident["email"],
            "plan": ident.get("plan"),
            "account_id": ident.get("account_id"),
            "added_at": datetime.now().isoformat(timespec="seconds"),
        })
        n = len([a for a in accs if provider_of(a) == "codex"])
        print(T(f"✓ added: {slug} ({ident['email']} / {ident.get('plan')}) — Codex account #{n}",
                f"✓ 追加: {slug} ({ident['email']} / {ident.get('plan')}) — Codex {n} 個目"))
    save_accounts(accs)
    set_current("codex", slug)
    return 0


def cmd_add(args) -> int:
    """今ログイン中のアカウントを弾倉に登録する。"""
    if getattr(args, "provider", "claude") == "codex":
        return cmd_add_codex(args)
    oauth = current_oauth()
    if not oauth:
        print(T("✗ No account is currently signed in. Run `claude auth login` first.",
                "✗ 現在ログイン中のアカウントが見つかりません。まず `claude auth login` を実行してください。"),
              file=sys.stderr)
        return 1
    st = auth_status()
    email = st.get("email") or "unknown"
    slug = args.slug or slugify(email)
    accs = accounts()
    existing = next((a for a in accs if a["slug"] == slug), None)
    store_oauth(slug, oauth)
    if existing:
        existing["email"] = email
        existing["label"] = args.label or existing.get("label") or email
        print(T(f"↻ updated: {slug} ({email})", f"↻ 更新: {slug} ({email})"))
    else:
        accs.append({
            "slug": slug,
            "label": args.label or email,
            "email": email,
            "subscription": oauth.get("subscriptionType"),
            "tier": oauth.get("rateLimitTier"),
            "added_at": datetime.now().isoformat(timespec="seconds"),
        })
        print(T(f"✓ added: {slug} ({email}) — account #{len(accs)}",
                f"✓ 追加: {slug} ({email}) — {len(accs)} 個目"))
    save_accounts(accs)
    s = state()
    s["current"] = slug
    save_state(s)
    return 0


def cmd_remove(args) -> int:
    accs = [a for a in accounts() if a["slug"] != args.slug]
    if len(accs) == len(accounts()):
        print(T(f"✗ {args.slug} is not registered", f"✗ {args.slug} は登録されていません"), file=sys.stderr)
        return 1
    save_accounts(accs)
    kc_delete(MAG_SERVICE, args.slug)
    print(T(f"✓ removed: {args.slug}", f"✓ 削除: {args.slug}"))
    return 0


def bar(pct) -> str:
    if pct is None:
        return "－－－－－－－－－－   ?"
    n = int(round(min(100.0, max(0.0, pct)) / 10))
    return "█" * n + "░" * (10 - n) + f" {pct:5.1f}%"


def cmd_status(args) -> int:
    clear_expired_cooldowns()
    for prov, slug in (reconcile_current() or {}).items():
        print(f"（{prov} の現在弾を実態に合わせて {slug} に修正しました）")
    if not accounts():
        print(T("No accounts registered. Add one with `mag add`.",
              "アカウントが未登録です。`mag add` で登録してください。"))
        return 1
    rc = 0
    for prov, title in (("claude", "Claude Code"), ("codex", "Codex / ChatGPT")):
        accs = accounts_of(prov)
        if not accs:
            continue
        print(T(f"━━ {title} ━━  {len(accs)} account(s)   active: {get_current(prov) or '(none)'}",
                f"━━ {title} ━━  {len(accs)} 個   使用中: {get_current(prov) or '(なし)'}"))
        rc |= _print_magazine(accs, prov, args)
        print()
    return rc


def _print_magazine(accs: list, prov: str, args) -> int:
    cur = get_current(prov)
    for a in accs:
        slug = a["slug"]
        mark = "▶" if slug == cur else " "
        line = f"{mark} {a.get('label', slug)}  [{slug}]"
        if args.quick or prov == "codex":
            left = cooldown_left(slug)
            extra = a.get("plan")
            suffix = (f"   ⏳ {fmt_when(state()['cooldowns'][slug]['until'])}" if left
                      else "   ready")
            print(line + (f"  ({extra})" if extra else "") + suffix)
            if prov == "codex" and not args.quick:
                auth = codex_stored_auth(slug)
                if not auth:
                    print(T("    ✗ no stored credential in keychain", "    ✗ Keychain に認証情報がありません"))
                elif slug == cur:
                    since = (state().get("last_switch_by") or {}).get("codex")
                    lim = codex_live_limits(only_after=since)
                    if not lim:
                        print(T("    no usage recorded yet (run codex once and it appears)",
                              "    残量の記録なし（codex で1回やり取りすると出ます）"))
                    else:
                        for w in lim["windows"]:
                            print(f"    {w['label']:<8}{bar(w['pct'])}   reset {fmt_when(w['resets_at'])}")
                        if lim.get("reached"):
                            print(f"    ⛔ 上限到達: {lim['reached']}")
                        print(f"    （{datetime.fromtimestamp(lim['ts']):%m/%d %H:%M} 時点の記録）")
                else:
                    print(T("    usage readable only while active (codex records it at runtime)",
                          "    残量は使用中のみ取得可（codex は実行時に記録するため）"))
            continue
        p = probe(slug)
        print(line)
        if not p["ok"]:
            print(f"    ? {p['error']}")
        else:
            print(f"    5h  {bar((p['five_hour'] or {}).get('pct'))}   reset {fmt_when((p['five_hour'] or {}).get('resets_at'))}")
            print(f"    7d  {bar((p['seven_day'] or {}).get('pct'))}   reset {fmt_when((p['seven_day'] or {}).get('resets_at'))}")
            for sc in p.get("scoped") or []:
                if sc["pct"] >= 80:
                    print(f"    └ {sc['model']}枠 {sc['pct']:.0f}%（このモデルのみ・他は使えます）"
                          f"  reset {fmt_when(sc['resets_at'])}")
        left = cooldown_left(slug)
        if left > 0:
            cd = state()["cooldowns"][slug]
            print(T(f"    ⏳ limited ({cd.get('kind')}) → {fmt_when(cd['until'])}",
                  f"    ⏳ 上限到達 ({cd.get('kind')}) → {fmt_when(cd['until'])}"))
        w = (state().get("warm") or {}).get(slug)
        if w and w.get("for_since") == state().get("last_switch", 0):
            print(T(f"    🔥 pre-checked: {'OK' if w.get('ok') else 'FAILED: ' + w.get('msg','')[:40]}",
                  f"    🔥 事前検証 {'OK' if w.get('ok') else 'NG: ' + w.get('msg','')[:40]}"))
        print()
    return 0


def record_limits(slug: str, data: dict) -> None:
    """観測できた残量を state に控える。

    Codex は「今装填している弾」の分しか記録が残らないので、切り替える前に
    控えておかないと他の弾の残量を二度と表示できなくなる。
    """
    s = state()
    s.setdefault("limits", {})[slug] = {**data, "ts": now()}
    save_state(s)


def known_limits(slug: str) -> dict | None:
    return (state().get("limits") or {}).get(slug)


def collect_limits(parallel_fetch: bool = True) -> list:
    """全アカウントの残量を集めて、表示用の行に整える。"""
    rows = []
    claude_accs = accounts_of("claude")

    results = {}
    if claude_accs:
        if parallel_fetch:
            import threading
            threads = []
            for a in claude_accs:
                t = threading.Thread(target=lambda s=a["slug"]: results.__setitem__(s, probe(s)))
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=25)
        else:
            for a in claude_accs:
                results[a["slug"]] = probe(a["slug"])

    for a in claude_accs:
        slug = a["slug"]
        p = results.get(slug) or {}
        row = {"provider": "claude", "slug": slug, "label": a.get("label", slug),
               "current": get_current("claude") == slug, "windows": [], "note": None,
               "cooldown": cooldown_left(slug)}
        if p.get("ok"):
            for key, label in (("five_hour", T("5-hour", "5時間")), ("seven_day", T("weekly", "週次"))):
                blk = p.get(key) or {}
                if blk.get("pct") is not None:
                    row["windows"].append({"label": label, "pct": blk["pct"],
                                           "resets_at": blk.get("resets_at")})
            for sc in p.get("scoped") or []:
                row["windows"].append({"label": T(f"weekly/{sc['model']}", f"週次/{sc['model']}"), "pct": sc["pct"],
                                       "resets_at": sc.get("resets_at"), "scoped": True})
            record_limits(slug, {"windows": row["windows"]})
        else:
            row["note"] = p.get("error") or T("unavailable", "取得不可")
            old = known_limits(slug)
            if old:
                row["windows"] = old.get("windows") or []
                row["stale_ts"] = old.get("ts")
        rows.append(row)

    for a in accounts_of("codex"):
        slug = a["slug"]
        is_cur = get_current("codex") == slug
        row = {"provider": "codex", "slug": slug, "label": a.get("label", slug),
               "current": is_cur, "windows": [], "note": None,
               "cooldown": cooldown_left(slug)}
        if is_cur:
            since = (state().get("last_switch_by") or {}).get("codex")
            lim = codex_live_limits(only_after=since)
            if lim:
                row["windows"] = [{"label": w["label"], "pct": w["pct"],
                                   "resets_at": w["resets_at"]} for w in lim["windows"]]
                row["reached"] = lim.get("reached")
                record_limits(slug, {"windows": row["windows"]})
            else:
                row["note"] = T("no usage recorded yet (run codex once)", "残量の記録なし（codex で1回やり取りすると出ます）")
        else:
            old = known_limits(slug)
            if old:
                row["windows"] = old.get("windows") or []
                row["stale_ts"] = old.get("ts")
            else:
                row["note"] = T("never observed (activate it and use once)", "未観測（切り替えて1回使うと記録されます）")
        rows.append(row)
    return rows


def cmd_limits(args) -> int:
    """全マガジンの残量を一気に表示する。"""
    rows = collect_limits()
    if not rows:
        print(T("No accounts registered. Add one with `mag add`.",
              "アカウントが未登録です。`mag add` で登録してください。"))
        return 1
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=float))
        return 0

    titles = {"claude": "🔵 Claude Code", "codex": "🟢 Codex / ChatGPT"}
    worst_overall = 0.0
    for prov in ("claude", "codex"):
        group = [r for r in rows if r["provider"] == prov]
        if not group:
            continue
        print(f"\n\033[1m{titles[prov]}\033[0m")
        for r in group:
            mark = "\033[32m▶\033[0m" if r["current"] else " "
            name = f"{r['label']}"
            head = f" {mark} {name}"
            if r["cooldown"] > 0:
                cd = state()["cooldowns"][r["slug"]]
                head += T(f"  \033[31m⏳ limited → {fmt_when(cd['until'])}\033[0m",
                          f"  \033[31m⏳ 上限到達 → {fmt_when(cd['until'])}\033[0m")
            print(head)
            if r.get("note") and not r["windows"]:
                print(f"      \033[2m{r['note']}\033[0m")
            for w in r["windows"]:
                if r["current"]:
                    worst_overall = max(worst_overall, w["pct"] or 0)
                dim = "\033[2m" if w.get("scoped") else ""
                print(f"      {dim}{w['label']:<12}\033[0m {bar(w['pct'])}"
                      f"   reset {fmt_when(w.get('resets_at'))}")
            if r.get("stale_ts"):
                print(f"      \033[2m(前回観測: {datetime.fromtimestamp(r['stale_ts']):%m/%d %H:%M} 時点)\033[0m")
            if r.get("reached"):
                print(f"      \033[31m⛔ 上限到達: {r['reached']}\033[0m")
    print()
    return 0


def resolve_account(needle: str) -> tuple[dict | None, str]:
    """スラグ・ラベル・メールの部分一致でアカウントを引く。"""
    accs = accounts()
    exact = next((a for a in accs if a["slug"] == needle), None)
    if exact:
        return exact, ""
    n = needle.lower()
    hits = [a for a in accs
            if n in a["slug"].lower()
            or n in (a.get("label") or "").lower()
            or n in (a.get("email") or "").lower()]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, T(f"no account matches '{needle}'", f"'{needle}' に一致するアカウントがありません")
    names = ", ".join(a["slug"] for a in hits)
    return None, T(f"'{needle}' matches several: {names}", f"'{needle}' が複数に一致します: {names}")


def cmd_load(args) -> int:
    acct, err = resolve_account(args.slug)
    if not acct:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    return 0 if do_load(acct["slug"], "manual") else 1


def cmd_next(args) -> int:
    prov = getattr(args, "provider", "claude")
    cur = get_current(prov)
    slug, report = next_slug(cur, probe_all=not args.no_probe, provider=prov)
    if not slug:
        print(T(f"✗ No usable {prov} account left.", f"✗ 使える {prov} アカウントがありません。"), file=sys.stderr)
        for s_, why in report:
            print(f"   - {s_}: {why}", file=sys.stderr)
        bslug, when = soonest_reset()
        if bslug:
            print(T(f"   earliest recovery: {bslug} → {fmt_when(when)}",
                  f"   最短の復帰: {bslug} → {fmt_when(when)}"), file=sys.stderr)
        return 2
    if slug == cur:
        print(T(f"= keeping: {slug}", f"= そのまま使用: {slug}"))
        return 0
    ok = do_load(slug, "next")
    if ok:
        acct = find_account(slug) or {}
        print(T(f"🔁 switched to: {acct.get('label', slug)} [{slug}]",
                f"🔁 切り替え: {acct.get('label', slug)} [{slug}]"))
    return 0 if ok else 1


def cmd_auto(args) -> int:
    """現在の弾が使えるならそのまま、ダメなら順送りで次弾を装填する（起動前フック用）。"""
    clear_expired_cooldowns()
    accs = accounts()
    if not accs:
        return 0  # 未設定なら何もしない（通常の claude 起動を邪魔しない）
    cur = get_current("claude")
    if cur and cooldown_left(cur) == 0:
        p = probe(cur) if not args.no_probe else None
        ok, why = is_usable(cur, p)
        if ok:
            if args.verbose:
                pct = ((p or {}).get("five_hour") or {}).get("pct")
                print(T(f"[magazine] {cur}: 5h {pct if pct is not None else '?'}% — keeping",
                      f"[magazine] {cur}: 5h {pct if pct is not None else '?'}% — そのまま"))
            return 0
        print(T(f"[magazine] {cur} is out ({why})", f"[magazine] {cur} 上限到達 ({why})"))
    rc = cmd_next(argparse.Namespace(no_probe=args.no_probe))
    if rc == 2 and not getattr(args, "strict", False):
        # 起動前の判定はまだ「上限確定」ではない。残りを使い切らせるため現弾のまま起動する。
        print(T("[magazine] No spare account. Starting on the current one (a real limit will be detected).",
                    "[magazine] 使える予備がありません。現在のアカウントのまま起動します（上限に当たれば検知します）"))
        return 0
    return rc


def cmd_hit(args) -> int:
    """上限ヒットを手動で記録して次弾へ。"""
    cur = args.slug or get_current("claude")
    if not cur:
        print(T("✗ Cannot tell which account is active", "✗ 現在使用中のアカウントが不明です"), file=sys.stderr)
        return 1
    resets = None
    kind = args.kind
    p = probe(cur)
    if p["ok"]:
        cand = []
        for k in ("five_hour", "seven_day"):
            blk = p[k] or {}
            if blk.get("pct") is not None:
                cand.append((blk["pct"], k, blk.get("resets_at")))
        cand.sort(reverse=True)
        if cand and (kind is None or cand[0][0] >= 95):
            kind = kind or cand[0][1]
            resets = cand[0][2]
    set_cooldown(cur, kind, resets)
    print(T(f"⛔ marked as limited: {cur} ({kind or 'five_hour'})",
            f"⛔ 上限到達として記録: {cur} ({kind or 'five_hour'})"))
    return cmd_next(argparse.Namespace(no_probe=False))


# ── statusLine 連携 ─────────────────────────────────────────────────────
def cmd_statusline(args) -> int:
    """Claude Code の statusLine から呼ばれ、rate_limits を live/<session>.json に落とす。"""
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    sid = data.get("session_id") or "unknown"
    rl = data.get("rate_limits") or {}
    cur = get_current("claude")
    if rl:
        os.makedirs(LIVE_DIR, exist_ok=True)
        payload = {"ts": now(), "slug": cur, "session_id": sid}
        for k in ("five_hour", "seven_day"):
            blk = rl.get(k) or {}
            payload[k] = {
                "pct": blk.get("used_percentage"),
                "resets_at": parse_iso(blk.get("resets_at")) or blk.get("resets_at"),
            }
        try:
            write_json(os.path.join(LIVE_DIR, f"{sid}.json"), payload)
        except Exception:
            pass

    # 表示
    parts = []
    used = ((data.get("context_window") or {}).get("used_percentage"))
    if used is not None:
        parts.append(f"Context: {used:.0f}%")
    five = (rl.get("five_hour") or {}).get("used_percentage")
    seven = (rl.get("seven_day") or {}).get("used_percentage")
    if five is not None:
        parts.append(f"5h:{five:.0f}%")
    if seven is not None:
        parts.append(f"7d:{seven:.0f}%")
    accs = accounts()
    if accs and cur:
        slugs = [a["slug"] for a in accs]
        idx = slugs.index(cur) + 1 if cur in slugs else 0
        ready = sum(1 for s_ in slugs if cooldown_left(s_) == 0)
        label = (find_account(cur) or {}).get("label", cur)
        parts.append(f"acct:{label} [{idx}/{len(slugs)}]" if LANG == "en"
                     else f"アカウント:{label} [{idx}/{len(slugs)}]")
    print(" · ".join(parts) if parts else "")
    return 0


def read_live(sid: str) -> dict | None:
    return read_json(os.path.join(LIVE_DIR, f"{sid}.json"), None)


# ── mag run: PTY で claude を包み、上限で自動リロード＆再開 ──────────────
def parse_reset_time(text: str) -> float | None:
    """"resets 9:50pm (Asia/Tokyo)" / "resets at 21:50" から復帰時刻を読む。"""
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ap = (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    base = datetime.now()
    t = base.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t <= base:
        t += timedelta(days=1)
    # 5h 上限の復帰が 24 時間先ということはない
    if t.timestamp() - now() > 8 * 3600:
        return None
    return t.timestamp()


def detect_hit(text: str, provider: str = "claude") -> tuple[bool, str | None, float | None]:
    rules = CODEX_HIT_RE if provider == "codex" else HIT_RE
    for line in text.splitlines():
        if HIT_EXCLUDE.search(line):
            continue
        for rx, kind in rules:
            if rx.search(line):
                if provider == "codex" and kind is None:
                    kind = "seven_day" if CODEX_WEEKLY_RE.search(line) else "five_hour"
                return True, kind, parse_reset_time(line)
    return False, None, None


def set_winsize(fd: int) -> None:
    try:
        sz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, sz)
    except OSError:
        pass


def banner(msg: str) -> None:
    """自前の通知を出す。

    相手が TUI を描いている最中に書くと行が割り込まれて画面が壊れるので、
    行頭に戻し・行を消し・属性を戻してから出す。呼ぶのは子プロセスを
    終わらせた後に限ること。
    """
    sys.stdout.write(f"\r\x1b[2K\x1b[0m\n\x1b[1;33m{msg}\x1b[0m\n")
    sys.stdout.flush()


def spawn_claude(argv: list, sid: str, on_hit, provider: str = "claude") -> tuple[int, str | None, float | None]:
    """claude を PTY で起動。上限を検知したら子を畳んで (exit_code, kind, resets_at) を返す。"""
    cfg = config()

    # pty.fork() で作られる子側の端末は「既定値」で始まり、こちらの端末設定を
    # 引き継がない。そのままだと子から見た端末の性質が実際の端末と食い違い、
    #   - Ctrl-C が信号にならず ^C という文字として入ってしまう（ISIG 相当の差）
    #   - 改行の扱いがずれて2行目以降の表示が崩れる（ICRNL/ONLCR 相当の差）
    # といった不具合が出る。元の設定とウィンドウサイズを子側へ複製しておく。
    parent_is_tty = sys.stdin.isatty()
    mode = termios.tcgetattr(sys.stdin) if parent_is_tty else None
    winsz = None
    if parent_is_tty:
        try:
            winsz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
        except OSError:
            winsz = None

    pid, master = pty.fork()
    if pid == 0:
        # ここでの fd 0/1/2 は擬似端末の子側。実端末と同じ設定にしてから起動する。
        try:
            if mode is not None:
                termios.tcsetattr(0, termios.TCSANOW, mode)
            if winsz is not None:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsz)
        except (OSError, termios.error):
            pass
        os.execvp(argv[0], argv)
        os._exit(127)

    set_winsize(master)
    old = None
    if parent_is_tty:
        old = mode
        tty.setraw(sys.stdin.fileno())

    def on_winch(signum, frame):
        set_winsize(master)

    prev_winch = signal.signal(signal.SIGWINCH, on_winch)

    tail = ""
    hit_kind = None
    hit_reset = None
    hit = False
    last_live = 0.0
    started = now()
    # stdin が EOF（/dev/null・尽きたパイプ）になると select は即座に readable を
    # 返し続けて busy-loop になる。EOF を検知した時点で監視対象から外す
    # （パイプ経由でスクリプト的に入力を流し込むケースはそれまで機能させたい）。
    stdin_eof = False
    try:
        while True:
            stdin_fds = [] if stdin_eof else [sys.stdin]
            try:
                r, _, _ = select.select([master] + stdin_fds, [], [], 0.4)
            except (OSError, InterruptedError):
                break
            if master in r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                os.write(sys.stdout.fileno(), chunk)
                tail = (tail + ANSI_RE.sub("", chunk.decode("utf-8", "replace")))[-8192:]
                found, kind, reset_at = detect_hit(tail, provider)
                if found and not hit:
                    hit, hit_kind, hit_reset = True, kind, reset_at
                    tail = ""
                    break
            if sys.stdin in r:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                except OSError:
                    data = b""
                if data:
                    os.write(master, data)
                else:
                    stdin_eof = True

            # statusLine 経由のリアルタイム使用率（実ヒット前に切り替えるための保険）。
            # これは Claude Code 固有の仕組みなので Codex では使えない。
            if provider == "claude" and now() - last_live > cfg["live_poll_interval"]:
                last_live = now()
                lv = read_live(sid)
                if lv and now() - lv.get("ts", 0) < 60:
                    for k, th in (("five_hour", cfg["five_hour_threshold"]),
                                  ("seven_day", cfg["seven_day_threshold"])):
                        pct = (lv.get(k) or {}).get("pct")
                        if (pct is not None and pct >= th
                                and now() - started > cfg["min_switch_interval"]
                                and has_spare(lv.get("slug"))):
                            hit, hit_kind, hit_reset = True, k, lv.get(k, {}).get("resets_at")
                            break
                if hit:
                    break
    finally:
        signal.signal(signal.SIGWINCH, prev_winch)
        if old:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)

    if hit:
        # 子プロセスの TUI がまだ描画している最中にこちらが書き込むと、行が割り込まれて
        # 画面が壊れる。先に子を終わらせ、画面を掃除してから知らせる。
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = now() + 5
        while now() < deadline:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        # 子が終了時に吐く後片付け（代替画面からの復帰など）を読み切ってから知らせる。
        # 残したまま書くと、こちらの行に相手のエスケープが割り込む。
        try:
            deadline = now() + 1.0
            while now() < deadline:
                r, _, _ = select.select([master], [], [], 0.1)
                if not r:
                    break
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                os.write(sys.stdout.fileno(), chunk)
        except OSError:
            pass
        try:
            os.close(master)
        except OSError:
            pass
        # 端末を素の状態に戻す（代替画面を抜け、属性と折返しを既定へ）
        sys.stdout.write("\x1b[?1049l\x1b[?25h\x1b[?7h\x1b[0m\r\n")
        sys.stdout.flush()
        on_hit(hit_kind)
        return 0, hit_kind, hit_reset

    try:
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        code = 0
    try:
        os.close(master)
    except OSError:
        pass
    return code, None, None


def cmd_run(args) -> int:
    cfg = config()
    passthrough = list(args.claude_args or [])
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    # 起動前に弾を確認
    rc = cmd_auto(argparse.Namespace(no_probe=False, verbose=True))
    if rc == 2:
        return 2

    manages_session = not any(
        a in passthrough for a in ("-c", "--continue", "-r", "--resume", "--session-id")
    )
    sid = str(uuid.uuid4())
    if not manages_session:
        # --resume <id> で渡されたセッション ID を引き継いで再開に使う
        for flag in ("--resume", "-r", "--session-id"):
            if flag in passthrough:
                i = passthrough.index(flag)
                if i + 1 < len(passthrough) and re.fullmatch(r"[0-9a-f-]{36}", passthrough[i + 1]):
                    sid = passthrough[i + 1]
                break
    base = ["claude"] + passthrough
    argv = base + (["--session-id", sid] if manages_session else [])

    # 再開時に最初の一言を送りたい場合（workflow の続きなど）は位置引数で渡す
    initial = getattr(args, "initial_prompt", None)
    if initial:
        argv = argv + [initial]

    rounds = 0
    while True:
        rounds += 1
        cur = get_current("claude")

        def on_hit(kind, _cur=cur):
            label = ({"five_hour": "5-hour", "seven_day": "weekly"} if LANG == "en"
                     else {"five_hour": "5時間", "seven_day": "週次"}).get(kind or "five_hour",
                     "usage" if LANG == "en" else "使用")
            banner(T(f"[magazine] {_cur} hit its {label} limit — switching account…",
                     f"[magazine] {_cur} が{label}上限に到達 — アカウントを切り替えます…"))

        code, kind, resets = spawn_claude(argv, sid, on_hit)
        if kind is None:
            return code

        # 出力からの実測 resets_at を優先し、取れなければ usage API で裏取りする
        if resets is None and cur:
            p = probe(cur)
            if p.get("ok"):
                blk = p.get(kind) or {}
                if blk.get("pct") is not None:
                    resets = blk.get("resets_at")
        if cur:
            set_cooldown(cur, kind, resets)

        slug, report = next_slug(cur, probe_all=True)
        if not slug:
            banner(T("[magazine] All accounts are limited. Waiting for recovery.",
                     "[magazine] 全アカウントが上限です。復帰待ちです。"))
            for s_, why in report:
                sys.stdout.write(f"   - {s_}: {why}\r\n")
            bslug, when = soonest_reset()
            if bslug:
                sys.stdout.write(f"   最短復帰: {bslug} → {fmt_when(when)}\r\n")
            sys.stdout.flush()
            return 2

        do_load(slug, f"auto after {kind}")
        label = (find_account(slug) or {}).get("label", slug)
        banner(T(f"[magazine] Switched to {label} [{slug}] — resuming (round {rounds})",
                     f"[magazine] {label} [{slug}] に切り替えました — 会話を再開します (round {rounds})"))
        time.sleep(1.0)

        if manages_session:
            argv = base + ["--resume", sid]
        elif not any(a in base for a in ("-c", "--continue", "-r", "--resume")):
            argv = base + ["-c"]
        else:
            argv = base

        # workflow が走っていたなら、最初からやり直させず runId で続きから再開させる
        wf = find_workflow(session_dir_for(sid))
        if wf:
            sys.stdout.write(f"   ⚙ workflow {wf['name']} ({wf['run_id']}) を続きから再開します\r\n")
            sys.stdout.flush()
            argv = argv + [wf_resume_prompt(wf)]


# ── 止まったセッションの検出 / 再開 ─────────────────────────────────────
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
STALL_RE = re.compile(
    r"(hit your (?:session|weekly|usage) limit|(?:session|weekly|usage) limit reached|"
    r"Not logged in|Please run /login)", re.I)


def tail_text(path: str, nbytes: int = 60000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - nbytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return ""
    if start:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else data
    return data.decode("utf-8", "replace")


def session_dir_for(sid: str, cwd: str = None) -> str:
    """~/.claude/projects/<cwd をハイフン化したもの>/<session-id>/ を組み立てる。"""
    cwd = cwd or os.getcwd()
    return os.path.join(PROJECTS_DIR, cwd.replace("/", "-"), sid)


def find_workflow(session_dir: str) -> dict | None:
    """<session>/workflows/scripts/<name>-wf_<runId>.js から再開情報を作る。

    同じセッション ID でも script と transcript が別のプロジェクトディレクトリに
    落ちることがある（cwd 由来のディレクトリ名が実行時と食い違うケース）ため、
    セッション ID をキーに projects 配下を横断して探す。
    """
    sid = os.path.basename(session_dir.rstrip("/"))
    cand_dirs = [session_dir]
    if os.path.isdir(PROJECTS_DIR):
        for proj in os.listdir(PROJECTS_DIR):
            d = os.path.join(PROJECTS_DIR, proj, sid)
            if d not in cand_dirs and os.path.isdir(d):
                cand_dirs.append(d)

    best = None
    for base in cand_dirs:
        sdir = os.path.join(base, "workflows", "scripts")
        if not os.path.isdir(sdir):
            continue
        for fn in os.listdir(sdir):
            m = re.search(r"-(wf_[A-Za-z0-9-]+)\.js$", fn)
            if not m:
                continue
            p = os.path.join(sdir, fn)
            try:
                mt = os.stat(p).st_mtime
            except OSError:
                continue
            if best is None or mt > best["mtime"]:
                best = {"run_id": m.group(1), "script_path": p, "mtime": mt,
                        "name": fn.rsplit("-wf_", 1)[0]}
    if best:
        # journal.jsonl に残っている完了済み agent() は再開時にキャッシュから返る。
        # transcript 側も script と別ディレクトリのことがあるので同様に横断で探す。
        best["cached_agents"] = 0
        for base in cand_dirs:
            jr = os.path.join(base, "subagents", "workflows", best["run_id"], "journal.jsonl")
            if not os.path.exists(jr):
                continue
            try:
                with open(jr, errors="replace") as f:
                    n = sum(1 for l in f if l.strip() and '"type":"result"' in l.replace(" ", ""))
                best["cached_agents"] = max(best["cached_agents"], n)
                best["journal"] = jr
            except OSError:
                pass
    return best


def inspect_session(path: str) -> dict | None:
    txt = tail_text(path)
    if not txt:
        return None
    rows = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not rows:
        return None

    # 末尾の assistant テキストが上限/未ログインなら「止まっている」
    stalled_by = None
    for r in reversed(rows[-12:]):
        if r.get("type") != "assistant":
            continue
        c = (r.get("message") or {}).get("content")
        texts = []
        if isinstance(c, str):
            texts = [c]
        elif isinstance(c, list):
            texts = [x.get("text", "") for x in c if isinstance(x, dict)]
        joined = " ".join(t for t in texts if t)
        m = STALL_RE.search(joined)
        if m:
            stalled_by = m.group(0)
        break

    cwd = next((r["cwd"] for r in reversed(rows) if r.get("cwd")), None)
    ts = next((r["timestamp"] for r in reversed(rows) if r.get("timestamp")), None)
    last_user = None
    for r in reversed(rows):
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        t = c if isinstance(c, str) else " ".join(
            x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")
        if t and not t.lstrip().startswith("<"):
            last_user = t.strip()[:100]
            break

    sid = os.path.basename(path)[:-6]
    sess_dir = path[:-6]
    return {
        "session_id": sid,
        "path": path,
        "cwd": cwd,
        "mtime": os.stat(path).st_mtime,
        "timestamp": ts,
        "last_user": last_user,
        "stalled_by": stalled_by,
        "workflow": find_workflow(sess_dir),
    }


def scan_sessions(hours: float = 24.0, stalled_only: bool = True) -> list:
    cutoff = now() - hours * 3600
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for proj in os.listdir(PROJECTS_DIR):
        pdir = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, fn)
            try:
                if os.stat(path).st_mtime < cutoff:
                    continue
            except OSError:
                continue
            info = inspect_session(path)
            if info and (info["stalled_by"] or not stalled_only):
                out.append(info)
    out.sort(key=lambda x: -x["mtime"])
    return out


def wf_resume_prompt(wf: dict) -> str:
    return (
        f"直前の Workflow が使用上限で中断しました。同じ作業を最初からやり直さず、"
        f"Workflow ツールを scriptPath=\"{wf['script_path']}\"、"
        f"resumeFromRunId=\"{wf['run_id']}\" で呼び出して続きから再開してください。"
        f"完了済みの agent() 呼び出しはキャッシュから返るので再実行されません。"
    )


def cmd_stalled(args) -> int:
    sessions = scan_sessions(args.hours, stalled_only=not args.all)
    if not sessions:
        print(f"直近 {args.hours:.0f}h に上限で止まったセッションはありません。")
        return 0
    print(f"上限などで止まっているセッション: {len(sessions)} 件\n")
    for i, s in enumerate(sessions, 1):
        when = (s["timestamp"] or "")[:19].replace("T", " ")
        print(f"{i}. {s['session_id']}")
        print(f"   cwd    : {s['cwd'] or '?'}")
        print(f"   停止   : {s['stalled_by'] or '(不明)'}   最終 {when}")
        if s["last_user"]:
            print(f"   最後の指示: {s['last_user']}")
        if s["workflow"]:
            wf = s["workflow"]
            cached = wf.get("cached_agents") or 0
            saved = f"完了 {cached} エージェント分はキャッシュ再利用" if cached else "キャッシュなし"
            print(f"   ⚙ workflow: {wf['name']}  runId={wf['run_id']}  → {saved}")
        print(f"   再開   : mag resume {s['session_id']}")
        print()
    return 0


def cmd_resume(args) -> int:
    target = args.session
    sessions = scan_sessions(args.hours, stalled_only=False)
    hit = next((s for s in sessions if s["session_id"].startswith(target)), None)
    if not hit:
        print(f"✗ セッション {target} が見つかりません（直近 {args.hours:.0f}h）", file=sys.stderr)
        return 1

    wf = hit["workflow"]
    if wf and not args.no_workflow:
        print("⚙ 中断した workflow を検出しました:")
        print(f"   name  : {wf['name']}")
        print(f"   runId : {wf['run_id']}")
        print(f"   script: {wf['script_path']}")
        print("   再開時に以下を投入します:")
        print(f"   → {wf_resume_prompt(wf)[:100]}…\n")

    if hit["cwd"] and os.path.isdir(hit["cwd"]):
        os.chdir(hit["cwd"])
        print(f"cd {hit['cwd']}")

    if args.dry_run:
        print(f"[dry-run] claude --resume {hit['session_id']}")
        if wf and not args.no_workflow:
            print("[dry-run] 投入プロンプト:")
            print(wf_resume_prompt(wf))
        return 0

    prompt = wf_resume_prompt(wf) if (wf and not args.no_workflow) else (args.prompt or "")
    run_args = argparse.Namespace(
        claude_args=["--resume", hit["session_id"]],
        initial_prompt=prompt,
    )
    return cmd_run(run_args)


# ── 常駐ホットスワップ ──────────────────────────────────────────────────
def notify(title: str, msg: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}" sound name "Submarine"'],
            capture_output=True, timeout=5)
    except Exception:
        pass


def live_worst(max_age: float = 120.0) -> dict | None:
    """稼働中セッションの statusLine から届いた使用率のうち、最も逼迫したものを返す。"""
    if not os.path.isdir(LIVE_DIR):
        return None
    cur = get_current("claude")
    worst = None
    for fn in os.listdir(LIVE_DIR):
        if not fn.endswith(".json"):
            continue
        d = read_json(os.path.join(LIVE_DIR, fn), None)
        if not d or now() - d.get("ts", 0) > max_age:
            continue
        if cur and d.get("slug") and d["slug"] != cur:
            continue
        for k in ("five_hour", "seven_day"):
            pct = (d.get(k) or {}).get("pct")
            if pct is None:
                continue
            if worst is None or pct > worst["pct"]:
                worst = {"kind": k, "pct": pct, "resets_at": (d.get(k) or {}).get("resets_at")}
    return worst


def live_five_hour_pct(max_age: float = 120.0) -> float | None:
    """現用アカウントの直近 5h 使用率（稼働中セッションの statusLine 由来）。"""
    if not os.path.isdir(LIVE_DIR):
        return None
    cur = get_current("claude")
    best = None
    for fn in os.listdir(LIVE_DIR):
        if not fn.endswith(".json"):
            continue
        d = read_json(os.path.join(LIVE_DIR, fn), None)
        if not d or now() - d.get("ts", 0) > max_age:
            continue
        if cur and d.get("slug") and d["slug"] != cur:
            continue
        pct = (d.get("five_hour") or {}).get("pct")
        if pct is not None and (best is None or pct > best):
            best = pct
    return best


def already_warmed(target: str) -> bool:
    """今の現用アカウントの「在任期間」中に、この弾はもう検証済みか。"""
    s = state()
    w = (s.get("warm") or {}).get(target)
    if not w:
        return False
    return w.get("for_since") == s.get("last_switch", 0)


def mark_warmed(target: str, ok: bool, msg: str) -> None:
    s = state()
    s.setdefault("warm", {})[target] = {
        "at": now(), "ok": ok, "msg": msg, "for_since": s.get("last_switch", 0),
    }
    save_state(s)


def warm_account(slug: str) -> tuple[bool, str]:
    """次弾に軽いクエリを1発投げて事前検証する。

    CLAUDE_CODE_OAUTH_TOKEN を使い、現用 Keychain スロットには一切触れない
    （＝今動いている稼働中セッションに影響しない）。狙いは2つ:
      - 認証切れ・失効などを実際に使う前に検出する
      - 5h ウィンドウが未使用なら、軽く動かして起動しておく
    """
    oauth = stored_oauth(slug)
    if not oauth:
        return False, T("no stored credential in keychain", "Keychain に認証情報がありません")
    try:
        oauth = ensure_fresh(slug, oauth)
    except Limited:
        # トークン更新側の 429。推論の可否とは別物なので cooldown は張らない。
        return False, T("token refresh got 429 (retry later)", "トークン更新が429（時間をおいて再試行）")
    except Exception as e:
        return False, f"refresh 失敗: {e}"

    cfg = config()
    env = dict(os.environ)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth["accessToken"]
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        r = subprocess.run(
            [find_claude_bin(), "--model", cfg["warm_model"], "--no-session-persistence",
             "-p", "OK とだけ返してください"],
            env=env, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, T("timed out", "タイムアウト")
    except FileNotFoundError:
        return False, T("claude command not found", "claude コマンドが見つかりません")

    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0 or re.search(r"not logged in|invalid.?api|expired|revoked|/login", out, re.I):
        return False, out[:150] or f"exit={r.returncode}"
    return True, "ok"


def hotswap(kind: str, reason: str, resets_at=None) -> bool:
    """稼働中のプロセスには触れず、Keychain の弾だけを入れ替える。"""
    cur = get_current("claude")
    if cur:
        set_cooldown(cur, kind, resets_at if isinstance(resets_at, (int, float)) else None)
    slug, report = next_slug(cur, probe_all=True)
    if not slug:
        bslug, when = soonest_reset()
        log(f"hotswap: 全弾切れ ({reason})")
        notify("🔫 claude-magazine", f"全弾切れ。最短復帰 {fmt_when(when)}")
        return False
    do_load(slug, f"hotswap: {reason}")
    label = (find_account(slug) or {}).get("label", slug)
    log(f"hotswap: {cur} → {slug} ({reason})")
    notify("🔫 claude-magazine", f"{reason} → {label} に装填しました（セッションはそのまま継続）")
    return True


def cmd_watch(args) -> int:
    cfg = config()
    th = args.threshold if args.threshold is not None else cfg["hotswap_threshold"]
    interval = args.interval
    print(f"👁 watch 開始: 閾値 {th}% / {interval}s ごと / ログ {LOG_PATH}")
    log(f"watch start (threshold={th}, interval={interval})")
    last_api = 0.0
    while True:
        try:
            clear_expired_cooldowns()
            reconcile_current()   # 手動 login で外から差し替えられていても追従する
            cur = get_current("claude")
            swapped = False

            # 1) statusLine 経由のリアルタイム値（稼働中セッションがある間は最速）
            lv = live_worst()
            if lv and lv["pct"] >= th and has_spare(cur):
                label = {"five_hour": "5h", "seven_day": "7d"}[lv["kind"]]
                swapped = hotswap(lv["kind"], f"{label} {lv['pct']:.0f}%", lv.get("resets_at"))

            # 1.5) 次弾の事前ウォームアップ（現用の5h使用率が閾値を超えたら軽く1回検証）
            #      Keychain の現用スロットには触れないので稼働中セッションに影響しない
            if not swapped and cur:
                pct5 = live_five_hour_pct()
                if pct5 is not None and pct5 >= cfg["warm_threshold"]:
                    target, _ = next_slug(cur, probe_all=False)
                    if target and target != cur and not already_warmed(target):
                        ok, msg = warm_account(target)
                        mark_warmed(target, ok, msg)
                        label = (find_account(target) or {}).get("label", target)
                        if ok:
                            log(f"warm: {target} OK (5h={pct5:.0f}%)")
                        else:
                            log(f"warm: {target} NG: {msg}")
                            notify("🔫 claude-magazine", f"⚠ 次弾 {label} の事前検証に失敗: {msg[:60]}")

            # 1.7) Codex 側の監視。セッション JSONL に書かれた rate_limits を読む。
            #      Claude と違い auth.json 差し替えは走行中プロセスに即時反映されないため、
            #      ここでの入れ替えは「次に codex を起動したとき」から効く。
            cur_cx = get_current("codex")
            if cur_cx and accounts_of("codex"):
                since = (state().get("last_switch_by") or {}).get("codex")
                cw = codex_worst(only_after=since)
                if cw and (cw["pct"] >= th or cw.get("reached")):
                    if has_spare(cur_cx, "codex"):
                        set_cooldown(cur_cx,
                                     "seven_day" if cw["window_minutes"] >= 10080 else "five_hour",
                                     cw.get("resets_at"))
                        nxt, _r = next_slug(cur_cx, probe_all=False, provider="codex")
                        if nxt and do_load(nxt, f"codex {cw['label']} {cw['pct']:.0f}%"):
                            lbl = (find_account(nxt) or {}).get("label", nxt)
                            log(f"hotswap[codex]: {cur_cx} → {nxt} ({cw['label']} {cw['pct']:.0f}%)")
                            notify("🔫 claude-magazine",
                                   f"Codex {cw['label']} {cw['pct']:.0f}% → {lbl} に装填")

            # 2) usage API（低頻度の裏取り。429 ならその時点で上限確定）
            if not swapped and now() - last_api > args.api_interval:
                last_api = now()
                if cur:
                    p = probe(cur)
                    if not p["ok"]:
                        # 残量が読めないだけでは切り替えない（使用量API側の絞りと区別できないため）。
                        # 実際の上限は statusLine の rate_limits と出力の実ヒット検知で捕まえる。
                        log(f"watch: {cur} の残量取得に失敗 ({p.get('error')}) — 切替はしない")
                    else:
                        for k in ("five_hour", "seven_day"):
                            pct = (p.get(k) or {}).get("pct")
                            if pct is not None and pct >= th and has_spare(cur):
                                label = {"five_hour": "5h", "seven_day": "7d"}[k]
                                hotswap(k, f"{label} {pct:.0f}%", (p.get(k) or {}).get("resets_at"))
                                break
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n👁 watch 停止")
            return 0
        except Exception as e:
            log(f"watch error: {e}")
            time.sleep(interval)


def cmd_crun(args) -> int:
    """codex を監視付きで起動し、上限に当たったら次弾を装填して直前セッションを再開する。

    Codex には残量 API が無い（Cloudflare 403）ため、切替のトリガーは
    出力に出る "You've hit your usage limit" の実ヒット検知のみ。
    """
    passthrough = list(args.codex_args or [])
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    accs = accounts_of("codex")
    if not accs:
        print("Codex の弾倉が空です。`codex login` のあと `mag add --provider codex` で登録してください。",
              file=sys.stderr)
        return 1

    cur = get_current("codex")
    need_swap = cur and cooldown_left(cur) > 0
    if cur and not need_swap:
        since = (state().get("last_switch_by") or {}).get("codex")
        cw = codex_worst(only_after=since)
        if cw:
            print(f"[magazine/codex] {cur}: {cw['label']} {cw['pct']:.0f}%")
            if cw["pct"] >= config()["hotswap_threshold"] or cw.get("reached"):
                need_swap = True
    if need_swap:
        slug, report = next_slug(cur, probe_all=False, provider="codex")
        if slug and slug != cur:
            do_load(slug, "crun 起動前")
            print(f"[magazine/codex] 🔁 次弾装填: {(find_account(slug) or {}).get('label', slug)}")
        else:
            print("[magazine/codex] 使える次弾がありません。現在の弾のまま起動します")

    base = [find_codex_bin()] + passthrough
    argv = list(base)
    rounds = 0
    while True:
        rounds += 1
        cur = get_current("codex")

        def on_hit(kind, _cur=cur):
            label = ({"five_hour": "5-hour", "seven_day": "weekly"} if LANG == "en"
                     else {"five_hour": "5時間", "seven_day": "週次"}).get(kind or "five_hour",
                     "usage" if LANG == "en" else "使用")
            banner(T(f"[magazine/codex] {_cur} hit its {label} limit — switching account…",
                     f"[magazine/codex] {_cur} が{label}上限に到達 — アカウントを切り替えます…"))

        code, kind, resets = spawn_claude(argv, "codex", on_hit, provider="codex")
        if kind is None:
            return code

        if cur:
            set_cooldown(cur, kind, resets)
        slug, report = next_slug(cur, probe_all=False, provider="codex")
        if not slug:
            banner(T("[magazine/codex] All accounts are limited.", "[magazine/codex] 全アカウントが上限です。"))
            for s_, why in report:
                sys.stdout.write(f"   - {s_}: {why}\r\n")
            sys.stdout.flush()
            return 2

        do_load(slug, f"auto after {kind}")
        label = (find_account(slug) or {}).get("label", slug)
        banner(T(f"[magazine/codex] Switched to {label} — resuming (round {rounds})",
                     f"[magazine/codex] {label} に切り替えました — セッションを再開します (round {rounds})"))
        time.sleep(1.0)
        # codex は直近セッションを --last で継続できる
        if not any(a in base for a in ("resume", "exec")):
            argv = base + ["resume", "--last"]
        else:
            argv = list(base)


def cmd_doctor(args) -> int:
    ok = True
    print("── claude-magazine doctor ──")
    cur = current_oauth()
    print(f"現用 Keychain      : {'OK' if cur else '見つからない'} ({LIVE_SERVICE}/{LIVE_ACCOUNT})")
    ok &= bool(cur)
    for prov, title in (("claude", "Claude 弾倉"), ("codex", "Codex 弾倉")):
        accs = accounts_of(prov)
        if not accs:
            continue
        print(f"{title:<18} : {len(accs)} 発")
        for a in accs:
            has = (codex_stored_auth(a["slug"]) if prov == "codex"
                   else stored_oauth(a["slug"])) is not None
            print(f"  - {a['slug']:<32} {'OK' if has else 'Keychain に弾なし'}")
            ok &= has
    # `claude auth status` はプロフィールをキャッシュしており Keychain の差し替えに
    # 追従しない（別アカウントを表示し続ける）。実際に入っている弾は
    # refreshToken の一致で判定する。
    reconcile_current()
    live_rt = (current_oauth() or {}).get("refreshToken")
    real = next((a for a in accounts_of("claude")
                 if (stored_oauth(a["slug"]) or {}).get("refreshToken") == live_rt), None)
    print(f"実際の Claude 現用  : {real.get('label') if real else '弾倉外のアカウント'}"
          f" [{real['slug'] if real else '?'}]")
    st = auth_status()
    if real and st.get("email") and st["email"] != real.get("email"):
        print(f"  ※ `claude auth status` は {st['email']} と表示しますが、"
              f"これはキャッシュで実態は上記です")
    if accounts_of("codex"):
        cauth = codex_live_auth()
        ident = codex_identity(cauth) if cauth else {}
        print(f"codex login status : {ident.get('email', '未ログイン')} ({ident.get('plan', '?')})")
    sl_script = os.path.join(HOME, ".claude", "statusline-command.sh")
    hooked = False
    if os.path.exists(sl_script):
        with open(sl_script) as f:
            hooked = "mag.py" in f.read()
    print(f"statusLine 連携    : {'OK' if hooked else '未接続（mag install-statusline で接続）'}")
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.claude-magazine.watch"],
                        capture_output=True, text=True)
    watching = "state = running" in r.stdout
    print(f"watch 常駐         : {'稼働中' if watching else '停止中（launchctl bootstrap で起動）'}")
    warm = state().get("warm") or {}
    if warm:
        print("次弾ウォームアップ :")
        for slug, w in warm.items():
            mark = "OK" if w.get("ok") else f"NG ({w.get('msg', '')[:40]})"
            print(f"  - {slug:<32} {mark}")
    print(f"ログ               : {LOG_PATH}")
    return 0 if ok else 1


def cmd_install_statusline(args) -> int:
    """既存の statusline-command.sh を mag statusline 経由に差し替える（バックアップ付き）。"""
    path = os.path.join(HOME, ".claude", "statusline-command.sh")
    if os.path.exists(path):
        bak = path + f".bak.{int(now())}"
        with open(path) as f:
            old = f.read()
        with open(bak, "w") as f:
            f.write(old)
        print(f"バックアップ: {bak}")
    script = f"""#!/bin/bash
# claude-magazine 連携 statusLine
exec python3 {os.path.join(ROOT, 'mag.py')} statusline
"""
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    print(f"✓ statusLine を mag 経由に接続: {path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="mag",
        description=T("Pool your Claude Code / Codex subscriptions and switch when one runs out",
                      "Claude Code / Codex のサブスクをまとめ、上限に達したら次に切り替える"),
        epilog=T("""Common flow:
  mag limits                     usage for every account
  mag next                       switch to the next account
  mag use sub                    switch to a specific one (partial name ok)
  mag stalled                    find sessions stopped by a limit
  mag resume <session-id>        resume one (an interrupted workflow continues)

Registering:
  claude auth login  ->  mag add --label main
  codex login        ->  mag add --provider codex --label codex-main
""", """よく使う流れ:
  mag limits                     全アカウントの残量を一覧
  mag next                       次のアカウントに切り替え
  mag use sub                    指定アカウントに切り替え（部分一致可）
  mag stalled                    上限で止まったセッションを探す
  mag resume <session-id>        再開（中断した workflow は続きから）

登録:
  claude auth login  ->  mag add --label main
  codex login        ->  mag add --provider codex --label codex-main
"""),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"magazine {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    a = sub.add_parser("add", help=T("register the account you are signed in to now","現在ログイン中のアカウントを登録"))
    a.add_argument("--label"); a.add_argument("--slug")
    a.add_argument("--provider", choices=["claude", "codex"], default="claude",
                   help=T("use codex to register a ChatGPT (codex CLI) account","codex を指定すると ChatGPT (codex CLI) 側に登録"))
    a.set_defaults(func=cmd_add)

    a = sub.add_parser("crun", help=T("run codex with auto-switch on limits","codex を監視付きで起動（上限で自動切替）"))
    a.add_argument("codex_args", nargs=argparse.REMAINDER)
    a.set_defaults(func=cmd_crun)

    a = sub.add_parser("remove", help=T("remove an account","アカウントを削除"))
    a.add_argument("slug"); a.set_defaults(func=cmd_remove)

    a = sub.add_parser("status", aliases=["st"], help=T("per-account detail","アカウントごとの詳細"))
    a.add_argument("--quick", action="store_true", help="API を叩かず cooldown だけ表示")
    a.set_defaults(func=cmd_status)
    sub.add_parser("list", aliases=["ls"], help=T("list accounts (no usage lookup)","アカウント一覧（残量は見に行かない）")).set_defaults(
        func=cmd_status, quick=True)

    a = sub.add_parser("limits", aliases=["l"], help=T("show usage for every account at once","全アカウントの残量を一覧"))
    a.add_argument("--json", action="store_true", help=T("output JSON (for scripts)","JSON で出力（スクリプト用）"))
    a.set_defaults(func=cmd_limits)

    a = sub.add_parser("load", aliases=["use"], help=T("switch to an account (partial name ok)","指定アカウントに切り替え（部分一致可）"))
    a.add_argument("slug", metavar="NAME"); a.set_defaults(func=cmd_load)

    a = sub.add_parser("next", help=T("advance to the next account","次のアカウントに切り替え"))
    a.add_argument("--no-probe", action="store_true")
    a.add_argument("--provider", choices=["claude", "codex"], default="claude")
    a.set_defaults(func=cmd_next)

    a = sub.add_parser("auto", help=T("switch only if needed (pre-launch hook)","必要なら切り替え（起動前フック）"))
    a.add_argument("--no-probe", action="store_true"); a.add_argument("-v", "--verbose", action="store_true")
    a.set_defaults(func=cmd_auto)

    a = sub.add_parser("hit", help=T("record a limit hit and move on","上限到達を記録して次へ"))
    a.add_argument("--kind", choices=["five_hour", "seven_day"]); a.add_argument("--slug")
    a.set_defaults(func=cmd_hit)

    a = sub.add_parser("run", help=T("run claude with auto-switch and resume","claude を監視付きで起動（上限で自動切替・再開）"))
    a.add_argument("claude_args", nargs=argparse.REMAINDER)
    a.set_defaults(func=cmd_run, initial_prompt=None)

    a = sub.add_parser("watch", help=T("daemon: switch before limits, without stopping sessions","常駐監視：セッションを止めずに事前切替"))
    a.add_argument("--interval", type=float, default=20.0, help="live 監視の間隔（秒）")
    a.add_argument("--api-interval", type=float, default=300.0, help="usage API の裏取り間隔（秒）")
    a.add_argument("--threshold", type=float, default=None, help="切替する使用率（既定 98%%）")
    a.set_defaults(func=cmd_watch)

    a = sub.add_parser("stalled", help=T("list sessions stopped by a limit","上限で止まったセッションを一覧"))
    a.add_argument("--hours", type=float, default=24.0)
    a.add_argument("--all", action="store_true", help="止まっていないセッションも含める")
    a.set_defaults(func=cmd_stalled)

    a = sub.add_parser("resume", help=T("resume a stopped session (workflow continues)","止まったセッションを再開（workflow は続きから）"))
    a.add_argument("session", help="セッション ID（先頭一致で可）")
    a.add_argument("--hours", type=float, default=72.0)
    a.add_argument("--dry-run", action="store_true", help="何をするかだけ表示")
    a.add_argument("--no-workflow", action="store_true", help="workflow 再開プロンプトを投入しない")
    a.add_argument("--prompt", help="再開時に送る最初の一言")
    a.set_defaults(func=cmd_resume)

    sub.add_parser("statusline", help=T("(internal) called by the statusLine hook","(内部) statusLine から呼ばれる")).set_defaults(func=cmd_statusline)
    sub.add_parser("install-statusline", help=T("wire up the statusLine hook","statusLine 連携を設定")).set_defaults(func=cmd_install_statusline)
    sub.add_parser("doctor", help=T("check the installation","インストール状態を確認")).set_defaults(func=cmd_doctor)

    args = p.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
