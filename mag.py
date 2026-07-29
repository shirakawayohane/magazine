#!/usr/bin/env python3
"""
claude-magazine — 複数の Claude Max アカウントを「弾倉」のように自動装填する。

- 認証情報は macOS Keychain に置いたまま扱う（平文ファイルには書かない）
- 現用スロット : service="Claude Code-credentials" / account=$USER の JSON 内 claudeAiOauth
- 弾倉スロット : service="claude-magazine"        / account=<slug>
  ※ 現用スロットには MCP の OAuth (mcpOAuth) が同居しているので claudeAiOauth だけを差し替える

残量の見方は 2 系統:
  1. statusLine 経由 … Claude Code が statusLine に渡す rate_limits（追加コストなし）
  2. usage API      … https://api.anthropic.com/api/oauth/usage

CLI 本体には干渉しない。端末を横取りしたり、走っているプロセスを止めたりはしない。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

VERSION = "0.1.0"

HOME = os.path.expanduser("~")


def _data_root() -> str:
    """設定と状態の置き場。

    既に ~/.claude-magazine があるならそれを使い続ける（作り直させない）。
    新規は ~/.config/magazine。MAGAZINE_HOME で明示指定もできる。
    """
    env = os.environ.get("MAGAZINE_HOME")
    if env:
        return os.path.expanduser(env)
    legacy = os.path.join(HOME, ".claude-magazine")
    if os.path.isdir(legacy):
        return legacy
    return os.path.join(HOME, ".config", "magazine")


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


def _make_output_safe() -> None:
    """出力先が扱えない文字で落ちないようにする。

    Windows の既定コンソールは cp932 などになることがあり、✓ や █ を
    そのまま書くと UnicodeEncodeError で異常終了する。表示が崩れるのは
    許容できても、表示のせいでコマンドが失敗するのは許容できない。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                enc = (getattr(stream, "encoding", "") or "").lower()
                if enc in ("utf-8", "utf8"):
                    continue
                stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


_make_output_safe()


def T(en: str, ja: str) -> str:
    """表示文字列。CLI では銃の比喩を使わず、素直な語で書く。"""
    return ja if LANG == "ja" else en
ROOT = _data_root()
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
    # mag watch が次のアカウントに移る使用率。
    # 枠を1%でも超えると、方針によっては従量課金へ流れて課金が発生する。
    # 超えてから気付いても遅いので、必ず手前で移る。
    "hotswap_threshold": 97.0,
    # 上限が近いほど短い間隔で見る。並列に走る workflow は数十秒で数%を消すので、
    # 一定間隔だと 97%→100% を丸ごと飛び越して課金域に入る。
    "poll_schedule": [[90.0, 3.0], [80.0, 8.0], [0.0, 20.0]],   # [使用率, 間隔秒]
    # 現用の5h使用率がここに達したら、次弾に軽いクエリを1発投げて事前検証する
    # （認証切れ・凍結などを事前に検出する。CLAUDE_CODE_OAUTH_TOKEN 経由で現用 Keychain には触れない）
    "warm_threshold": 50.0,
    "warm_model": "claude-haiku-4-5-20251001",
    # 上限で落ちた後の再開時、自動で送信するプロンプト（空なら送らない）
    "auto_continue_prompt": "",
}

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


# ── 認証情報の保管 ──────────────────────────────────────────────────────
# macOS はログインキーチェーンを使う。Linux / Windows にはそれに当たる共通の
# 置き場が無いので、権限を絞ったファイルに置く。Claude Code 自身も macOS 以外では
# ~/.claude/.credentials.json に平文で置いているので、保護の強さはそれと同じ。
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
SECRETS_DIR = os.path.join(ROOT, "secrets")


def use_keychain() -> bool:
    return IS_MAC and shutil.which("security") is not None


def _secret_path(service: str, account: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{service}__{account}")
    return os.path.join(SECRETS_DIR, safe + ".json")


def kc_read(service: str, account: str) -> dict | None:
    if use_keychain():
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
    return read_json(_secret_path(service, account), None)


def kc_write(service: str, account: str, data: dict) -> None:
    if use_keychain():
        payload = json.dumps(data, separators=(",", ":"))
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account,
             "-D", "application password", "-w", payload],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Keychain 書き込み失敗 ({service}/{account}): {r.stderr.strip()}")
        return
    path = _secret_path(service, account)
    try:
        os.makedirs(SECRETS_DIR, exist_ok=True)
        harden_path(SECRETS_DIR, 0o700)
        write_json(path, data)
        harden_path(path, 0o600)
    except OSError as e:
        raise RuntimeError(f"認証情報の書き込みに失敗 ({service}/{account}): {e}")


def kc_delete(service: str, account: str) -> None:
    if use_keychain():
        subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                       capture_output=True, text=True)
        return
    try:
        os.remove(_secret_path(service, account))
    except OSError:
        pass


def harden_path(path: str, mode: int) -> None:
    """本人だけが読めるようにする。Windows は POSIX 権限が効かないので ACL で絞る。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    if not IS_WINDOWS:
        return
    try:
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(F)"],
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


# ── 現用スロット ────────────────────────────────────────────────────────
# macOS は Keychain。それ以外では Claude Code 自身が使う
# <CLAUDE_CONFIG_DIR or ~/.claude>/.credentials.json を読み書きする。
def claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def claude_creds_file() -> str:
    return os.path.join(claude_config_dir(), ".credentials.json")


def live_creds() -> dict:
    if use_keychain():
        return kc_read(LIVE_SERVICE, LIVE_ACCOUNT) or {}
    return read_json(claude_creds_file(), None) or {}


def write_live_creds(creds: dict) -> None:
    if use_keychain():
        kc_write(LIVE_SERVICE, LIVE_ACCOUNT, creds)
        return
    path = claude_creds_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, creds)
    harden_path(path, 0o600)


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
            write_live_creds(creds)
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
            write_live_creds(before)
            log("install_oauth: 失敗したため元の弾へロールバックしました")
        except RuntimeError:
            log("install_oauth: ロールバックにも失敗（要 `claude auth login`）")
    raise last_err or RuntimeError(T("Failed to write the credential", "認証情報の書き込みに失敗しました"))


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
    # 従量課金（プラン枠を超えた分の課金）の状態。枠を使い切ったあと、ここが
    # 有効だと黙って課金が始まる。無効なら単に止まる。どちらなのかは
    # 当たってからでは遅いので、残量と一緒に見えるようにしておく。
    eu = (u or {}).get("extra_usage") or {}
    out["metered"] = {
        "enabled": eu.get("is_enabled"),
        "limit_reached": eu.get("spend_limit_reached"),
        "reason": eu.get("disabled_reason"),
        "used": eu.get("used_credits"),
        "monthly_limit": eu.get("monthly_limit"),
    }
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


def codex_probe(slug: str, timeout: int = 90) -> dict | None:
    """装填していない Codex アカウントの残量を取る。

    Codex は残量 API を公開していないが、CODEX_HOME を向ければ設定ごと別の
    アカウントとして動く。使い捨ての一時ディレクトリにその弾の auth.json を置き、
    最小のリクエストを1回投げて、そこに記録された rate_limits を読む。
    現用の ~/.codex/auth.json には一切触らないので、走っているセッションに影響しない。

    ただしリクエストを1回消費するので、常時ポーリングには使わないこと。
    """
    auth = codex_stored_auth(slug)
    if not auth:
        return None
    auth = codex_ensure_fresh(slug, auth)
    import tempfile, shutil as _sh
    home = tempfile.mkdtemp(prefix="mag-codex-")
    try:
        with open(os.path.join(home, "auth.json"), "w") as f:
            json.dump(auth, f)
        os.chmod(os.path.join(home, "auth.json"), 0o600)
        src_cfg = os.path.expanduser("~/.codex/config.toml")
        if os.path.exists(src_cfg):
            try:
                _sh.copy(src_cfg, os.path.join(home, "config.toml"))
            except OSError:
                pass
        env = dict(os.environ)
        env["CODEX_HOME"] = home
        r = subprocess.run(
            [find_codex_bin(), "exec", "--skip-git-repo-check", "OK"],
            env=env, capture_output=True, text=True, timeout=timeout, cwd="/tmp")
        out = (r.stdout or "") + (r.stderr or "")
        if re.search(r"refresh token was revoked|could not be refreshed", out, re.I):
            return {"dead": True}

        best = None
        for root, _d, names in os.walk(home):
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                try:
                    with open(os.path.join(root, n), errors="replace") as f:
                        for line in f:
                            if '"rate_limits"' not in line:
                                continue
                            d = json.loads(line)
                            rl = ((d.get("payload") or {}).get("rate_limits")) or {}
                            if (rl.get("limit_id") or "codex") != "codex":
                                continue
                            if (rl.get("primary") or {}).get("used_percent") is None:
                                continue
                            ts = parse_iso(d.get("timestamp")) or 0
                            if best is None or ts > best[0]:
                                best = (ts, rl)
                except (OSError, json.JSONDecodeError):
                    continue
        if not best:
            return None
        rl = best[1]
        wins = []
        for k in ("primary", "secondary"):
            b = rl.get(k)
            if not b or b.get("used_percent") is None:
                continue
            wm = b.get("window_minutes") or 0
            wins.append({"label": (f"{wm // 60}h window" if wm < 10080 else f"{wm // 1440}d window")
                         if LANG == "en" else (f"{wm // 60}時間枠" if wm < 10080 else f"{wm // 1440}日枠"),
                         "pct": b["used_percent"], "resets_at": b.get("resets_at")})
        return {"windows": wins, "plan": rl.get("plan_type"),
                "reached": rl.get("rate_limit_reached_type"), "ts": best[0]}
    except subprocess.TimeoutExpired:
        return None
    finally:
        _sh.rmtree(home, ignore_errors=True)


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
    """`claude auth status` の内容。claude が無い環境では空を返す。

    （この値はプロフィールのキャッシュなので、現用アカウントの判定には使わない）
    """
    try:
        r = subprocess.run([find_claude_bin(), "auth", "status", "--json"],
                           capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
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
        matched = None
        for a in accounts_of("codex"):
            st = (codex_stored_auth(a["slug"]) or {}).get("tokens") or {}
            if st.get("refresh_token") == ctok:
                matched = a["slug"]
                break
        if matched is None:
            # OpenAI も refresh token を使い捨てにするため、保管したスナップショットは
            # 本体が更新した時点で失効する。現用の中身で保管庫を上書きして追従する。
            live_id = codex_identity(cauth)
            for a in accounts_of("codex"):
                if (a.get("email") or "").lower() == (live_id.get("email") or "").lower():
                    codex_store_auth(a["slug"], cauth)
                    matched = a["slug"]
                    log(f"sync: {a['slug']} の保管トークンを現用の最新版に更新（rotation 追従）")
                    break
        if matched and get_current("codex") != matched:
            set_current("codex", matched)
            fixed["codex"] = matched
            log(f"reconcile: codex の現在弾を {matched} に修正")
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
        span = cfg.get(f"fallback_cooldown_{kind}") or cfg["fallback_cooldown_five_hour"]
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
                    "scoped": s.get("scoped", []), "metered": s.get("metered") or {}})
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
            report.append((cand, T(f"spent, metered also capped → {fmt_when(cd['until'])}",
                                   f"枠+従量課金とも上限 → {fmt_when(cd['until'])}")
                                  if cd.get("kind") == "spend"
                                  else f"cooldown → {fmt_when(cd['until'])}"))
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
            if cd.get("kind") == "spend":
                print(T(f"    ⛔ plan window spent, metered credits also capped → {fmt_when(cd['until'])}",
                        f"    ⛔ 枠を使い切り従量課金も上限 → {fmt_when(cd['until'])}"))
            else:
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


def collect_limits(parallel_fetch: bool = True, args_ns=None) -> list:
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
            row["metered"] = p.get("metered") or {}
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
        elif getattr(args_ns, "refresh", False):
            # 現用でない弾も、使い捨ての CODEX_HOME で1回だけ問い合わせて実測する
            got = codex_probe(slug)
            if got and got.get("dead"):
                row["note"] = T("needs re-login (refresh token revoked)", "要再ログイン（refresh token 失効）")
            elif got:
                row["windows"] = got["windows"]
                row["reached"] = got.get("reached")
                record_limits(slug, {"windows": row["windows"]})
            else:
                row["note"] = T("could not read usage", "残量を取得できませんでした")
        else:
            old = known_limits(slug)
            if old:
                row["windows"] = old.get("windows") or []
                row["stale_ts"] = old.get("ts")
            else:
                row["note"] = T("never observed (use --refresh to measure)", "未観測（--refresh で実測できます）")
        rows.append(row)
    return rows


def cmd_limits(args) -> int:
    """全マガジンの残量を一気に表示する。"""
    rows = collect_limits(args_ns=args)
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
                if cd.get("kind") == "spend":
                    head += T(f"  \033[31m⛔ plan window spent, metered credits also capped → {fmt_when(cd['until'])}\033[0m",
                              f"  \033[31m⛔ 枠を使い切り従量課金も上限 → {fmt_when(cd['until'])}\033[0m")
                else:
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
            mt = r.get("metered") or {}
            if mt.get("limit_reached"):
                print(T("      \033[31m⛔ metered credits exhausted (org-capped) — this account just stops\033[0m",
                        "      \033[31m⛔ 従量課金枠も使い切り（組織で停止中）— このアカウントは止まります\033[0m"))
            elif mt.get("enabled"):
                print(T("      \033[33m💸 metered billing ON — going over the plan window will cost money\033[0m",
                        "      \033[33m💸 従量課金が有効 — 枠を超えると課金されます\033[0m"))
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
    """起動前に、必要なときだけアカウントを切り替える。

    シェルのラッパーから毎回呼ばれるので、既定ではネットワークを叩かない
    （--no-probe）。残量の追跡は常駐監視に任せ、ここはローカルの状態だけを見る。
    起動を遅らせないことと、CLI 本体に干渉しないことを優先する。
    """
    prov = getattr(args, "provider", "claude")
    clear_expired_cooldowns()
    if not accounts_of(prov):
        return 0  # 未設定なら何もしない（通常の起動を邪魔しない）
    cur = get_current(prov)
    if cur and cooldown_left(cur) == 0:
        p = probe(cur) if (not args.no_probe and prov == "claude") else None
        ok, why = is_usable(cur, p)
        if ok:
            if args.verbose:
                pct = ((p or {}).get("five_hour") or {}).get("pct")
                print(T(f"[magazine] {cur}: 5h {pct if pct is not None else '?'}% — keeping",
                      f"[magazine] {cur}: 5h {pct if pct is not None else '?'}% — そのまま"))
            return 0
        print(T(f"[magazine] {cur} is out ({why})", f"[magazine] {cur} 上限到達 ({why})"))
    rc = cmd_next(argparse.Namespace(no_probe=args.no_probe, provider=prov))
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
    """上限で止まったセッションを開き直す。

    ここでは端末を横取りしない。使えるアカウントを選び、作業ディレクトリへ移り、
    素の claude を exec するだけ。中断した workflow があれば、続きから再開させる
    一言を最初のプロンプトとして渡す。
    """
    target = args.session
    sessions = scan_sessions(args.hours, stalled_only=False)
    hit = next((s for s in sessions if s["session_id"].startswith(target)), None)
    if not hit:
        print(T(f"✗ session {target} not found (last {args.hours:.0f}h)",
                f"✗ セッション {target} が見つかりません（直近 {args.hours:.0f}h）"), file=sys.stderr)
        return 1

    wf = hit["workflow"]
    if wf and not args.no_workflow:
        cached = wf.get("cached_agents") or 0
        print(T("⚙ Found an interrupted workflow:", "⚙ 中断した workflow を検出しました:"))
        print(f"   name  : {wf['name']}")
        print(f"   runId : {wf['run_id']}")
        print(T(f"   cached: {cached} finished agent(s) will replay instead of re-running",
                f"   再利用: 完了済み {cached} エージェントは再実行されません"))

    if hit["cwd"] and os.path.isdir(hit["cwd"]):
        os.chdir(hit["cwd"])
        print(f"cd {hit['cwd']}")

    prompt = wf_resume_prompt(wf) if (wf and not args.no_workflow) else (args.prompt or "")
    argv = [find_claude_bin(), "--resume", hit["session_id"]] + ([prompt] if prompt else [])

    if args.dry_run:
        print("[dry-run] " + " ".join(argv[:3]) + (" <workflow 再開の一言>" if prompt else ""))
        if prompt:
            print("[dry-run] 投入プロンプト:")
            print(prompt)
        return 0

    # 起動前に使えるアカウントへ寄せる（ネットワークは叩かない）
    cmd_auto(argparse.Namespace(no_probe=True, provider="claude", verbose=False))
    # 擬似端末を挟まず、このプロセスを claude に置き換える
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        print(T(f"✗ could not start claude: {e}", f"✗ claude を起動できません: {e}"), file=sys.stderr)
        return 1


# ── 常駐ホットスワップ ──────────────────────────────────────────────────
def notify(title: str, msg: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}" sound name "Submarine"'],
            capture_output=True, timeout=5)
    except Exception:
        pass


def switch_grace_ok(ts: float, provider: str = "claude", grace: float = 45.0) -> bool:
    """切り替え直後の測定値を信用してよいか。

    Claude Code は直近のAPI応答に付いてきた rate_limits を保持しているので、
    アカウントを入れ替えた直後のしばらくは「前のアカウントの数値」を報告し続ける。
    それを新しいアカウントの数値として扱うと、切り替えた先を即座に上限扱いして
    しまう（実際にそれで無限に切り替わる不具合を出した）。
    """
    last = (state().get("last_switch_by") or {}).get(provider) or 0
    return ts >= last + grace


def set_cooldown_verified(slug: str, kind: str, resets_at=None, source: str = "") -> bool:
    """間接的なシグナルで上限扱いにする前に、本人の実測で裏を取る。

    statusLine 由来の数値は「誰の数値か」を取り違えうる。裏取りせずに
    cooldown を張ると、まだ余裕のあるアカウントを外してしまう。
    """
    p = probe(slug)
    if p.get("ok"):
        blk = p.get(kind) or {}
        pct = blk.get("pct")
        th = config().get(f"{kind}_threshold", 99.5)
        if pct is not None and pct < th - 5:
            log(f"cooldown 見送り: {slug} は実測 {pct:.0f}%（{source} の申告と不一致）")
            return False
        if blk.get("resets_at"):
            resets_at = blk["resets_at"]
    set_cooldown(slug, kind, resets_at)
    return True


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
        if not switch_grace_ok(d.get("ts", 0)):
            continue      # 切替直後は前のアカウントの数値が残っている
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
        # 申告されただけでは外さない。本人の実測と突き合わせてから上限扱いにする。
        if not set_cooldown_verified(cur, kind,
                                     resets_at if isinstance(resets_at, (int, float)) else None,
                                     source=reason):
            return False
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

            # 上限が近いほど短い間隔で見る（枠を超えて課金域に入らないため）
            pct_now = live_five_hour_pct() or 0.0
            nap = interval
            for edge, sec in cfg.get("poll_schedule") or []:
                if pct_now >= edge:
                    nap = min(interval, sec) if interval < 20 else sec
                    break
            time.sleep(nap)
        except KeyboardInterrupt:
            print("\n👁 watch 停止")
            return 0
        except Exception as e:
            log(f"watch error: {e}")
            time.sleep(interval)


def cmd_doctor(args) -> int:
    ok = True
    print(T("-- magazine doctor --", "-- magazine doctor --"))
    cur = current_oauth()
    where = (f"keychain {LIVE_SERVICE}/{LIVE_ACCOUNT}" if use_keychain()
             else claude_creds_file())
    print(T(f"live credential    : {'OK' if cur else 'not found'}  ({where})",
            f"現用の認証情報     : {'OK' if cur else '見つからない'}  ({where})"))
    ok &= bool(cur)
    for prov, title in ((("claude", T("Claude accounts", "Claude アカウント")),
                     ("codex", T("Codex accounts", "Codex アカウント")))):
        accs = accounts_of(prov)
        if not accs:
            continue
        print(T(f"{title:<18} : {len(accs)}", f"{title:<18} : {len(accs)} 個"))
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
    """statusLine から mag を呼ぶようにする（既存設定はバックアップする）。

    走っているセッションの残量は、Claude Code が statusLine に渡す JSON に
    含まれている。ここを経由すれば追加の API コール無しで読める。
    """
    conf_dir = claude_config_dir()
    os.makedirs(conf_dir, exist_ok=True)
    # このファイル自身の位置を使う。データ置き場（ROOT）とは別物。
    me = os.path.abspath(__file__)
    py = sys.executable or ("python" if IS_WINDOWS else "python3")

    if IS_WINDOWS:
        path = os.path.join(conf_dir, "statusline-command.cmd")
        script = f'@echo off\r\n"{py}" "{me}" statusline\r\n'
    else:
        path = os.path.join(conf_dir, "statusline-command.sh")
        script = f'#!/bin/bash\n# magazine 連携 statusLine\nexec "{py}" "{me}" statusline\n'

    if os.path.exists(path):
        bak = path + f".bak.{int(now())}"
        with open(path) as f:
            old = f.read()
        with open(bak, "w") as f:
            f.write(old)
        print(T(f"backup: {bak}", f"バックアップ: {bak}"))

    with open(path, "w", newline="") as f:
        f.write(script)
    if not IS_WINDOWS:
        os.chmod(path, 0o755)

    # settings.json の statusLine もこのファイルを指すようにしておく
    settings_path = os.path.join(conf_dir, "settings.json")
    settings = read_json(settings_path, None)
    if isinstance(settings, dict):
        want = f'"{path}"' if IS_WINDOWS else f'bash "{path}"'
        cur = (settings.get("statusLine") or {}).get("command")
        if cur != want:
            settings["statusLine"] = {"type": "command", "command": want}
            write_json(settings_path, settings)
            print(T("settings.json: statusLine updated", "settings.json の statusLine を更新"))
    print(T(f"✓ statusLine now goes through mag: {path}",
            f"✓ statusLine を mag 経由に接続: {path}"))
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


    a = sub.add_parser("remove", help=T("remove an account","アカウントを削除"))
    a.add_argument("slug"); a.set_defaults(func=cmd_remove)

    a = sub.add_parser("status", aliases=["st"], help=T("per-account detail","アカウントごとの詳細"))
    a.add_argument("--quick", action="store_true", help="API を叩かず cooldown だけ表示")
    a.set_defaults(func=cmd_status)
    sub.add_parser("list", aliases=["ls"], help=T("list accounts (no usage lookup)","アカウント一覧（残量は見に行かない）")).set_defaults(
        func=cmd_status, quick=True)

    a = sub.add_parser("limits", aliases=["l"], help=T("show usage for every account at once","全アカウントの残量を一覧"))
    a.add_argument("--json", action="store_true", help=T("output JSON (for scripts)","JSON で出力（スクリプト用）"))
    a.add_argument("--refresh", action="store_true",
                   help=T("measure inactive Codex accounts too (costs one request each)",
                          "装填していない Codex も実測する（1アカウントにつき1リクエスト消費）"))
    a.set_defaults(func=cmd_limits)

    a = sub.add_parser("load", aliases=["use"], help=T("switch to an account (partial name ok)","指定アカウントに切り替え（部分一致可）"))
    a.add_argument("slug", metavar="NAME"); a.set_defaults(func=cmd_load)

    a = sub.add_parser("next", help=T("advance to the next account","次のアカウントに切り替え"))
    a.add_argument("--no-probe", action="store_true")
    a.add_argument("--provider", choices=["claude", "codex"], default="claude")
    a.set_defaults(func=cmd_next)

    a = sub.add_parser("auto", help=T("switch only if needed (pre-launch hook)","必要なら切り替え（起動前フック）"))
    a.add_argument("--no-probe", action="store_true",
                   help=T("decide from local state only, no network",
                          "ネットワークを使わずローカル状態だけで判断"))
    a.add_argument("--provider", choices=["claude", "codex"], default="claude")
    a.add_argument("-v", "--verbose", action="store_true")
    a.set_defaults(func=cmd_auto)

    a = sub.add_parser("hit", help=T("record a limit hit and move on","上限到達を記録して次へ"))
    a.add_argument("--kind", choices=["five_hour", "seven_day"]); a.add_argument("--slug")
    a.set_defaults(func=cmd_hit)


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
