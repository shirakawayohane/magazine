#!/usr/bin/env python3
"""
claude-magazine — 複数の Claude Max アカウントを「弾倉」のように自動装填する。

- 認証情報は macOS Keychain に置いたまま扱う（平文ファイルには書かない）
- 現用スロット : service="Claude Code-credentials" / account=$USER の JSON 内 claudeAiOauth
- 弾倉スロット : service="claude-magazine"        / account=<slug>
  ※ 現用スロットには MCP の OAuth (mcpOAuth) が同居しているので claudeAiOauth だけを差し替える

現用スロットの扱いには 2 つの約束がある（守らないと走っているセッションが落ちる）:
  1. トークンの更新（refresh）は Claude Code 本体だけの仕事。refresh token は
     使い捨てなので、弾倉が横から更新すると本体が握っている分が失効する。
     弾倉は読むだけで、書くのは弾を入れ替えるときだけ。
  2. 書くときは本体と同じ書き込みロック
     （<設定ディレクトリ>/.storage-write.lock）を取る。取らずに書くと、
     本体が更新した直後の内容を古い弾で踏み潰す。

残量の見方は 2 系統:
  1. statusLine 経由 … Claude Code が statusLine に渡す rate_limits（追加コストなし）
  2. usage API      … https://api.anthropic.com/api/oauth/usage

CLI 本体には干渉しない。端末を横取りしたり、走っているプロセスを止めたりはしない。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
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


def _os_username() -> str:
    """Node の os.userInfo().username 相当（本体はこれを使う）。"""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USERNAME") or os.path.basename(HOME)


def _live_account() -> str:
    """現用スロットの account 名。本体（2.1.260 で確認）と同じ決め方にする。

    ここがずれると別のエントリを読み書きしてしまい、本体からは「ログアウト
    した」ように見える。本体は $USER → os.userInfo().username の順に見て、
    英数と ._- 以外が混じっていたら claude-code-user に落とす。
    """
    try:
        name = os.environ.get("USER") or _os_username()
    except Exception:
        name = "claude-code-user"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return "claude-code-user"
    return name


LIVE_ACCOUNT = _live_account()
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
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {msg}\n")


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
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


def isolated_claude_service(config_dir: str) -> str:
    """CLAUDE_CONFIG_DIR を変えたときに Claude Code が使う Keychain のサービス名。

    本体は `Claude Code-credentials-<sha256(設定ディレクトリ)の先頭8桁>` にする
    （2.1.260 で確認）。この規則が変わると回収に失敗して明示的にエラーになる。
    """
    d = unicodedata.normalize("NFC", config_dir)
    return f"{LIVE_SERVICE}-{hashlib.sha256(d.encode('utf-8')).hexdigest()[:8]}"


def live_service() -> str:
    """いま本体が使っている現用スロットのサービス名。

    設定ディレクトリを環境変数でずらしていると本体は別のエントリを使う。
    固定名を読み書きすると、本体から見て「ログアウトした」状態になる。
    """
    d = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if d is None:
        d = os.environ.get("CLAUDE_CONFIG_DIR")
    return isolated_claude_service(d) if d else LIVE_SERVICE


def live_creds() -> dict:
    if use_keychain():
        return kc_read(live_service(), LIVE_ACCOUNT) or {}
    return read_json(claude_creds_file(), None) or {}


def write_live_creds(creds: dict) -> None:
    if use_keychain():
        kc_write(live_service(), LIVE_ACCOUNT, creds)
        return
    path = claude_creds_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, creds)
    harden_path(path, 0o600)


# 本体（2.1.259 以降）は現用スロットを書き換える前にこのロックを取る。
# proper-lockfile 互換で、印は <設定ディレクトリ>/.storage-write.lock というディレクトリ。
# 同じロックを取らずに書くと、本体がトークンを更新した直後に古い内容で上書きしてしまい、
# 走っているセッションが "Not logged in · Please run /login" を掴む。
STORAGE_LOCK_NAME = ".storage-write.lock"
STORAGE_LOCK_STALE = 15.0     # 本体が「放置された鍵」と見なす秒数


def storage_lock_path() -> str:
    return os.path.join(claude_config_dir(), STORAGE_LOCK_NAME)


@contextlib.contextmanager
def live_write_lock():
    """本体と同じ書き込みロックを握る。取れなかった場合も止めずに続ける
    （切り替えが永久に止まる方が困る）。握れたかどうかを yield する。"""
    path = storage_lock_path()
    held = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as e:
        log(f"live_write_lock: 置き場を作れません ({e}) — ロック無しで続行")
        yield False
        return

    deadline = now() + STORAGE_LOCK_STALE + 5.0
    wait = 0.05
    while now() < deadline:
        try:
            os.mkdir(path)
            held = os.stat(path).st_mtime_ns
            break
        except FileExistsError:
            pass
        except OSError as e:
            log(f"live_write_lock: 使えません ({e}) — ロック無しで続行")
            break
        try:
            age = now() - os.stat(path).st_mtime
        except OSError:
            age = 0.0
        if age > STORAGE_LOCK_STALE:
            try:
                os.rmdir(path)      # 本体と同じ基準で放置された鍵を外す
            except OSError:
                pass
            continue
        time.sleep(wait)
        wait = min(wait * 2, 0.5)
    if held is None:
        log("live_write_lock: 取得できないまま続行します（本体と競合の可能性）")

    try:
        yield held is not None
    finally:
        if held is not None:
            try:
                if os.stat(path).st_mtime_ns == held:
                    os.rmdir(path)
            except OSError:
                pass


def current_oauth() -> dict | None:
    return live_creds().get("claudeAiOauth")


def install_oauth(oauth: dict) -> None:
    """現用スロットの claudeAiOauth だけを差し替える（mcpOAuth は温存）。

    稼働中のセッションは Keychain を都度読み直すため、書き換えが中途半端だと
    走っているセッションが "Not logged in · Please run /login" を掴んでしまう。
    そのため (1) 事前に中身を検証し (2) 本体と同じ書き込みロックを取り
    (3) 書き戻して読み直しで確認し (4) 壊れていたら即座に元へ戻す。
    """
    token = (oauth or {}).get("accessToken")
    if not token or not str(token).startswith("sk-ant-"):
        raise RuntimeError(T("The credential to activate is malformed (bad accessToken)", "有効化しようとした認証情報が壊れています（accessToken が不正）"))
    exp = oauth.get("expiresAt")
    if exp and (exp / 1000.0) <= now():
        raise RuntimeError(T("The credential to activate has an expired accessToken", "有効化しようとした認証情報の accessToken が期限切れです"))

    # 読み → 差し替え → 書き の一連を丸ごとロックの中でやる。
    # 途中で本体が更新すると、その新しい弾を古い内容で踏み潰してしまう。
    with live_write_lock() as locked:
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
                log(f"install: 現用スロットを差し替えました "
                    f"(expires={fmt_when(exp / 1000.0 if exp else None)}, lock={'取得' if locked else '無し'})")
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


def is_live_oauth(oauth: dict) -> bool:
    """この認証情報が、いま現用スロットに入っているものか。"""
    rt = (oauth or {}).get("refreshToken")
    return bool(rt) and (current_oauth() or {}).get("refreshToken") == rt


def ensure_fresh(slug: str | None, oauth: dict) -> dict:
    """期限切れ間近なら refresh し、保管庫にも書き戻す。

    ただし現用スロットに入っている弾には絶対に触らない。Anthropic の
    refresh token は使い捨て（使うと次のものに切り替わる）なので、本体と
    弾倉の両方が更新しにいくと、負けた方が握っているトークンが失効する。
    本体が負けると、走っているセッションが
    "Not logged in · Please run /login" で止まる。
    現用の更新は本体だけの仕事にして、弾倉は sync_live_credentials で追従する。
    """
    exp = oauth.get("expiresAt")
    if exp and (exp / 1000.0) - now() > 120:
        return oauth
    if is_live_oauth(oauth):
        return oauth
    new = refresh_oauth(oauth)
    if not new:
        return oauth
    if slug:
        store_oauth(slug, new)
        log(f"refresh: {slug} のトークンを更新しました（現用ではない弾）")
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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CODEX_AUTH_PATH)
    back = codex_live_auth() or {}
    if (back.get("tokens") or {}).get("access_token") != tok["access_token"]:
        if before:
            with open(tmp, "w", encoding="utf-8") as f:
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


def codex_refresh(auth: dict) -> tuple[dict | None, str | None]:
    """(更新後の auth, 失効理由) を返す。

    OpenAI の refresh token は 1 回使うたびに新しいものへ回り、古い方は
    その場で無効になる。なので 400/401 は「通信できなかった」ではなく
    「この認証情報はもう蘇らない（再ログインが要る）」を意味する。
    圏外などの一時的な失敗と混ぜないよう、理由を分けて返す。
    """
    tok = (auth or {}).get("tokens") or {}
    rt = tok.get("refresh_token")
    if not rt:
        return None, T("no refresh token is stored", "refresh token が保管されていません")
    body = {"client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": rt, "scope": "openid profile email"}
    try:
        req = urllib.request.Request(
            CODEX_TOKEN_URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": CODEX_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code not in (400, 401):
            return None, None
        try:
            msg = ((json.loads(e.read().decode()) or {}).get("error") or {}).get("message")
        except Exception:
            msg = None
        return None, msg or T("this sign-in has expired", "このログインは失効しています")
    except Exception:
        return None, None
    if not j.get("access_token"):
        return None, None
    new = json.loads(json.dumps(auth))
    new.setdefault("tokens", {})
    new["tokens"]["access_token"] = j["access_token"]
    for k in ("id_token", "refresh_token"):
        if j.get(k):
            new["tokens"][k] = j[k]
    new["last_refresh"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return new, None


def codex_ensure_fresh(slug: str | None, auth: dict, verify: bool = False) -> tuple[dict, str | None]:
    """(使える auth, 失効理由)。更新できたら保管庫へ書き戻す。

    verify=True では期限に関わらず必ず 1 回更新する。現用スロットを踏み潰す前に
    「この弾はまだ生きているか」を確かめるため。refresh token はローテーションする
    ので、保管したスナップショットは本体が 1 度更新しただけで死ぬ。死んだ弾を
    装填すると codex 側が auth.json を消してしまい、生きていたログインまで失う。
    """
    exp = jwt_claims(((auth or {}).get("tokens") or {}).get("access_token") or "").get("exp")
    if not verify and exp and exp - now() > 300:
        return auth, None
    new, dead = codex_refresh(auth)
    if dead:
        return auth, dead
    if not new:
        return auth, None     # 通信できないだけ。手元のもので進む
    if slug:
        codex_store_auth(slug, new)
    return new, None


def codex_sync_live() -> str | None:
    """現用の auth.json を、持ち主の保管スロットへ書き戻す。

    codex 本体もトークンを更新するので、その瞬間に保管庫のスナップショットは
    失効する。現用を上書きする前にここを通しておかないと、切り替えて戻った
    ときに死んだ弾を掴む。戻り値は持ち主の slug。
    """
    live = codex_live_auth() or {}
    rt = (live.get("tokens") or {}).get("refresh_token")
    if not rt:
        return None
    accs = accounts_of("codex")
    for a in accs:
        if ((codex_stored_auth(a["slug"]) or {}).get("tokens") or {}).get("refresh_token") == rt:
            return a["slug"]          # 保管庫は既に最新
    email = (codex_identity(live).get("email") or "").lower()
    for a in accs:
        if email and (a.get("email") or "").lower() == email:
            codex_store_auth(a["slug"], live)
            log(f"sync: {a['slug']} の保管トークンを現用の最新版に更新（rotation 追従）")
            return a["slug"]
    return None


CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")


def codex_session_recently_active(within: float = 1800) -> bool:
    """直近 within 秒に書き込まれた codex セッションがあるか。

    走っているセッションがあるかの目安。セッションは 1 ターンごとに
    自分の JSONL へ追記するので、最終更新が新しければ誰かが使っている。
    """
    if not os.path.isdir(CODEX_SESSIONS_DIR):
        return False
    cutoff = now() - within
    for root, _dirs, names in os.walk(CODEX_SESSIONS_DIR):
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            try:
                if os.path.getmtime(os.path.join(root, n)) >= cutoff:
                    return True
            except OSError:
                continue
    return False


def codex_switch_note(name: str) -> str | None:
    """切り替え直後に出す一言。開いているセッションが無さそうなら黙る。

    codex は認証をターンの区切り（と認証エラーからの復帰）でしか読み直さない。
    そのため開いているセッションは次のターンまで前のアカウントのまま走り、
    その間に上限メッセージが出ることがある。実測では切り替えから反映まで
    十数分かかり、「切り替えたのに上限と言われる」と見える。
    """
    if not codex_session_recently_active():
        return None
    return T(f"  note: an open codex session keeps using the previous account until its next turn "
             f"(a limit message may show up once). Start a new session — or `codex resume` — to use {name} right away.",
             f"  注: 開いている codex セッションは、次のターンまで前のアカウントのまま動きます"
             f"（上限メッセージが1回出ることがあります）。すぐ {name} で使うなら、"
             f"新しいセッションを開くか `codex resume` で開き直してください。")


def print_codex_switch_note(prov: str, name: str) -> None:
    if prov != "codex":
        return
    note = codex_switch_note(name)
    if note:
        print(note)


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
            with open(p, errors="replace", encoding="utf-8") as f:
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
    auth, dead = codex_ensure_fresh(slug, auth)
    if dead:
        return {"dead": True, "error": dead}
    import tempfile, shutil as _sh
    home = tempfile.mkdtemp(prefix="mag-codex-")
    try:
        with open(os.path.join(home, "auth.json"), "w", encoding="utf-8") as f:
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
                    with open(os.path.join(root, n), errors="replace", encoding="utf-8") as f:
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


# ── 表示名（alias）────────────────────────────────────────────────────────
# slug は Keychain / state のキーとして内部でだけ使う。ユーザーが打つのも
# 画面に出るのも label（alias）と、どのアカウントかを示すメールだけ。
def display(a: dict | None) -> str:
    if not a:
        return "?"
    return a.get("label") or a["slug"]


def label_of(slug: str | None) -> str:
    return display(find_account(slug)) if slug else "?"


def describe(a: dict | None) -> str:
    """一覧・確認メッセージ用: `alias (email)`。"""
    if not a:
        return "?"
    email = a.get("email")
    return f"{display(a)} ({email})" if email and email != display(a) else display(a)


def label_taken_by(label: str, accs: list, except_slug: str | None = None) -> dict | None:
    """同じ alias（大文字小文字は区別しない）を持つ別アカウントを返す。"""
    n = (label or "").strip().lower()
    return next((a for a in accs
                 if a["slug"] != except_slug and (a.get("label") or "").strip().lower() == n), None)


def label_conflict_error(label: str, accs: list, except_slug: str | None = None) -> str | None:
    other = label_taken_by(label, accs, except_slug)
    if not other:
        return None
    return T(f"alias '{label}' is already used by {describe(other)} — pick another, or `mag rename` that one first",
             f"alias '{label}' は {describe(other)} が使用中です — 別名にするか、先に `mag rename` で変えてください")


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
        # 保管庫が現用より古いのに、持ち主を確認できない（圏外など）。
        # このまま切り替えると古いトークンを掴むので、気づけるように残す。
        s = state()
        if now() - float(s.get("sync_warned_at") or 0) > 600:
            s["sync_warned_at"] = now()
            save_state(s)
            log("sync: 現用トークンの持ち主を確認できず、保管庫が古いままです（圏外?）")
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

    matched = codex_sync_live()   # codex も rotation で保管庫が古びる
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
    # 現用の弾は「現用スロットの実物」で見る。本体がトークンを更新した直後は
    # 保管庫のコピーが古いままになるが、それは弾が死んだという意味ではない。
    if get_current("claude") == slug:
        live = current_oauth()
        if live and live.get("accessToken"):
            oauth = live
    if not oauth:
        res["error"] = T("no stored credential in keychain", "Keychain に認証情報がありません")
        return res
    try:
        oauth = ensure_fresh(slug, oauth)
        exp = oauth.get("expiresAt")
        if exp and (exp / 1000.0) <= now():
            if is_live_oauth(oauth):
                # 現用の更新は本体の仕事。更新されるまで残量が読めないだけで、
                # 弾が死んだわけではないので使用不可にはしない。
                res["error"] = T("waiting for Claude Code to refresh the live token (usage unknown)",
                                 "本体のトークン更新待ち（残量不明）")
                res["info_unavailable"] = True
                return res
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
            return False, T(f"{kind} {pct:.1f}% ≥ threshold {th:.1f}%", f"{kind} {pct:.1f}% ≥ 閾値{th:.1f}%")
    return True, "ok"


def do_load(slug: str, reason: str = "") -> bool:
    acct = find_account(slug) or {}
    prov = provider_of(acct) if acct else "claude"
    name = display(acct) if acct else slug

    if prov == "codex":
        auth = codex_stored_auth(slug)
        if not auth:
            print(T(f"✗ {name}: no stored credential in keychain", f"✗ {name}: Keychain に認証情報がありません"), file=sys.stderr)
            return False
        outgoing = codex_sync_live()
        # 装填してから死んでいると分かっても遅い（codex 本体が auth.json を
        # 消してしまい、直前まで使えていたログインごと失う）。先に確かめる。
        auth, dead = codex_ensure_fresh(slug, auth, verify=True)
        if dead:
            print(T(f"✗ {name}: {dead} — run `mag login codex {name}` to sign in again "
                    f"(the active account is left as it is)",
                    f"✗ {name}: {dead} — `mag login codex {name}` で入れ直してください"
                    f"（今使っているアカウントはそのままです）"), file=sys.stderr)
            log(f"load refused: {slug}: {dead}")
            return False
        try:
            # 認証情報と state の持ち主が一致する場合だけ、切替前の記録を保存する。
            # 手動ログインなどで食い違っている場合は他アカウントへの誤帰属を避ける。
            if outgoing and outgoing == get_current("codex"):
                codex_usage_snapshot(outgoing)
            codex_install_auth(auth)
        except (RuntimeError, OSError) as e:
            print(T(f"✗ failed to activate {name}: {e}", f"✗ {name} への切り替えに失敗: {e}"), file=sys.stderr)
            log(f"load failed: {slug}: {e}")
            return False
    else:
        oauth = stored_oauth(slug)
        if not oauth:
            print(T(f"✗ {name}: no stored credential in keychain", f"✗ {name}: Keychain に認証情報がありません"), file=sys.stderr)
            return False
        # codex と同じ理由で、現用スロットの最新トークンを先に退避する。
        sync_live_credentials()
        oauth = ensure_fresh(slug, oauth)
        try:
            install_oauth(oauth)
        except RuntimeError as e:
            print(T(f"✗ failed to activate {name}: {e}", f"✗ {name} への切り替えに失敗: {e}"), file=sys.stderr)
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


def slug_for(prov: str, email: str) -> str:
    return ("cx-" if prov == "codex" else "") + slugify(email)


def store_credential(prov: str, slug: str, cred: dict) -> None:
    (codex_store_auth if prov == "codex" else store_oauth)(slug, cred)


def refresh_entry(entry: dict, prov: str, cred: dict, ident: dict) -> None:
    """登録エントリの付随情報（プラン等）を最新のログイン結果で更新する。alias は触らない。"""
    entry["email"] = ident["email"]
    if prov == "codex":
        entry.update({"plan": ident.get("plan"), "account_id": ident.get("account_id")})
    else:
        entry.update({"subscription": cred.get("subscriptionType"), "tier": cred.get("rateLimitTier")})


def register_new(prov: str, slug: str, alias: str, cred: dict, ident: dict, accs: list) -> dict:
    """認証情報を保管庫に入れ、alias 付きで台帳に載せる。呼ぶ前に重複検査を済ませておくこと。"""
    store_credential(prov, slug, cred)
    entry = {"slug": slug, "provider": prov, "label": alias, "email": ident["email"],
             "added_at": datetime.now().isoformat(timespec="seconds")}
    refresh_entry(entry, prov, cred, ident)
    accs.append(entry)
    save_accounts(accs)
    n = len([a for a in accs if provider_of(a) == prov])
    plan = f" / {ident.get('plan')}" if ident.get("plan") else ""
    print(T(f"✓ added: {alias} ({ident['email']}{plan}) — {prov} account #{n}",
            f"✓ 追加: {alias} ({ident['email']}{plan}) — {prov} {n} 個目"))
    return entry


def live_identity(prov: str, strict: bool = False) -> tuple[dict | None, dict | None, str | None]:
    """今 CLI にログイン中のアカウントの (認証情報, 素性, エラー)。

    strict のときは、トークンから本人確認できない限り成功にしない
    （`claude auth status` のキャッシュは差し替え直後に前の人を指すことがある）。
    """
    if prov == "codex":
        auth = codex_live_auth()
        if not auth or not (auth.get("tokens") or {}).get("refresh_token"):
            return None, None, T("Not signed in to codex. Run `codex login` first.",
                                 "codex にログインしていません。先に `codex login` を実行してください。")
        if auth.get("auth_mode") == "apikey" or auth.get("OPENAI_API_KEY"):
            return None, None, T("This is an API-key login. Only ChatGPT subscription auth is supported.",
                                 "APIキー方式のログインです。ChatGPT サブスク認証のみ対象です。")
        return auth, codex_identity(auth), None
    oauth = current_oauth()
    if not oauth:
        return None, None, T("No account is currently signed in. Run `claude auth login` first.",
                             "現在ログイン中のアカウントが見つかりません。まず `claude auth login` を実行してください。")
    email = identify_claude_token(oauth.get("accessToken") or "")
    if not email and strict:
        return None, None, T("could not verify who is signed in (network?)",
                             "ログイン中のアカウントを確認できません（ネットワーク?）")
    email = email or auth_status().get("email") or "unknown"
    return oauth, {"email": email}, None


def already_registered_error(slug: str, accs: list) -> str | None:
    hit = next((a for a in accs if a["slug"] == slug), None)
    if not hit:
        return None
    return T(f"this account is already registered as {describe(hit)} — to re-store its credentials run `mag update {display(hit)}`",
             f"このアカウントは {describe(hit)} として登録済みです — 認証情報を入れ直すなら `mag update {display(hit)}`")


def cmd_add(args) -> int:
    """今ログイン中のアカウントを弾倉に登録する。"""
    prov, alias = getattr(args, "provider", "claude"), args.alias.strip()
    cred, ident, err = live_identity(prov)
    if err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    accs = accounts()
    slug = slug_for(prov, ident["email"])
    if err := already_registered_error(slug, accs) or label_conflict_error(alias, accs):
        print(f"✗ {err}", file=sys.stderr)
        return 1
    register_new(prov, slug, alias, cred, ident, accs)
    set_current(prov, slug)
    return 0


# ── 現用に触れないログイン ──────────────────────────────────────────────
# claude / codex は保存先を環境変数で切り替えられる。使い捨ての保存先で
# ログインだけ走らせ、出来た認証情報を保管庫へ移す。走っている
# セッションが掴んでいる現用の認証には一切書き込まない。
def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def claude_keychain_services() -> set:
    """Keychain にある Claude Code の認証エントリのサービス名一覧（現用は除く）。"""
    if not use_keychain():
        return set()
    try:
        r = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {m for m in re.findall(r'"svce"<blob>="(' + re.escape(LIVE_SERVICE) + r'-[^"]+)"', r.stdout or "")}


def collect_isolated_claude(home: str, env: dict, services_before: set = frozenset()) -> tuple[dict | None, dict | None, str | None]:
    """使い捨ての設定ディレクトリでのログイン結果を回収し、その保存先は消す。

    まず命名規則どおりのサービス名を読む。外れていたら、ログイン前に無かった
    エントリを「今のログインが作ったもの」とみなして拾う（規則変更への保険）。
    """
    creds = None
    if use_keychain():
        svc = isolated_claude_service(home)
        creds = kc_read(svc, LIVE_ACCOUNT)
        if creds:
            kc_delete(svc, LIVE_ACCOUNT)
        else:
            new = claude_keychain_services() - set(services_before)
            if len(new) == 1:
                svc = new.pop()
                creds = kc_read(svc, LIVE_ACCOUNT)
                kc_delete(svc, LIVE_ACCOUNT)
                log(f"login: Keychain のサービス名が想定と違ったので差分で回収 ({svc})")
    creds = creds or read_json(os.path.join(home, ".credentials.json"), None) or {}
    oauth = creds.get("claudeAiOauth")
    if not oauth or not oauth.get("refreshToken"):
        return None, None, T("no credential was produced (login cancelled, or Claude Code changed where it stores it)",
                             "認証情報が得られませんでした（ログイン中止、または Claude Code の保存先が変わった）")
    email = identify_claude_token(oauth.get("accessToken") or "")
    if not email:
        # このキャッシュは今のログインの結果そのものなので信じてよい
        try:
            r = subprocess.run([find_claude_bin(), "auth", "status", "--json"],
                               env=env, capture_output=True, text=True, timeout=20)
            email = (json.loads(r.stdout) or {}).get("email")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            email = None
    if not email:
        return None, None, T("could not determine the email of the account you signed in to",
                             "ログインしたアカウントのメールを特定できませんでした")
    return oauth, {"email": email}, None


def collect_isolated_codex(home: str) -> tuple[dict | None, dict | None, str | None]:
    auth = read_json(os.path.join(home, "auth.json"), None)
    if not auth or not (auth.get("tokens") or {}).get("refresh_token"):
        return None, None, T("no credential was produced (login cancelled?)", "認証情報が得られませんでした（ログイン中止?）")
    if auth.get("auth_mode") == "apikey" or auth.get("OPENAI_API_KEY"):
        return None, None, T("This is an API-key login. Only ChatGPT subscription auth is supported.",
                             "APIキー方式のログインです。ChatGPT サブスク認証のみ対象です。")
    return auth, codex_identity(auth), None


def isolated_login(prov: str) -> tuple[dict | None, dict | None, str | None]:
    import tempfile, shutil as _sh
    home = os.path.realpath(tempfile.mkdtemp(prefix=f"mag-login-{prov}-"))
    env = dict(os.environ)
    try:
        if prov == "codex":
            env["CODEX_HOME"] = home
            src_cfg = os.path.expanduser("~/.codex/config.toml")
            if os.path.exists(src_cfg):
                try:
                    _sh.copy(src_cfg, os.path.join(home, "config.toml"))
                except OSError:
                    pass
            argv = [find_codex_bin(), "login"]
        else:
            env["CLAUDE_CONFIG_DIR"] = home
            argv = [find_claude_bin(), "auth", "login"]
            services_before = claude_keychain_services()
        print(T(f"→ running `{os.path.basename(argv[0])} {' '.join(argv[1:])}` in a throwaway profile; the active account is not touched",
                f"→ 使い捨てのプロファイルで `{os.path.basename(argv[0])} {' '.join(argv[1:])}` を実行します（使用中の認証には触れません）"))
        try:
            r = subprocess.run(argv, env=env)
        except OSError as e:
            return None, None, str(e)
        if r.returncode != 0:
            return None, None, T(f"login exited with {r.returncode}", f"ログインが終了コード {r.returncode} で終わりました")
        return (collect_isolated_codex(home) if prov == "codex"
                else collect_isolated_claude(home, env, services_before))
    finally:
        _sh.rmtree(home, ignore_errors=True)


def cmd_login(args) -> int:
    """使用中の認証に触れずに別アカウントでログインし、alias を付けて登録する。"""
    prov, alias = args.provider, args.alias.strip()
    if not alias:
        print(T("✗ alias is empty", "✗ alias が空です"), file=sys.stderr)
        return 1
    taken = label_taken_by(alias, accounts())
    # provider が違えば同じメールでも別エントリになるため、ログイン後に
    # 衝突が解消する可能性はない。不要な認証を始める前に弾く。
    if taken and provider_of(taken) != prov:
        err = label_conflict_error(alias, [taken])
        print(f"✗ {err}", file=sys.stderr)
        return 1
    if taken:
        print(T(f"note: alias '{alias}' is {describe(taken)}. Signing in as that account re-stores its credentials; any other account is refused.",
                f"注: alias '{alias}' は {describe(taken)} です。同じアカウントでログインすれば認証の入れ直し、別アカウントなら拒否します。"))
    cred, ident, err = isolated_login(prov)
    if err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    accs = accounts()   # ログインの間に変わっているかもしれないので読み直す
    slug = slug_for(prov, ident["email"])
    hit = next((a for a in accs if a["slug"] == slug), None)
    if not hit:
        if err := label_conflict_error(alias, accs):
            print(f"✗ {err}", file=sys.stderr)
            return 1
        register_new(prov, slug, alias, cred, ident, accs)
        print(T(f"  the active {prov} account is unchanged — switch with `mag use {alias}`",
                f"  使用中の {prov} アカウントはそのままです — 切り替えるなら `mag use {alias}`"))
        return 0
    # 登録済みのアカウントに再ログインした
    store_credential(prov, slug, cred)
    refresh_entry(hit, prov, cred, ident)
    save_accounts(accs)
    if display(hit).lower() == alias.lower():
        print(T(f"↻ updated credentials: {describe(hit)}", f"↻ 認証情報を入れ直しました: {describe(hit)}"))
        return 0
    other = label_taken_by(alias, accs, except_slug=slug)
    if other:
        print(T(f"↻ updated credentials: {describe(hit)}. alias '{alias}' is already used by {describe(other)}, so the name is unchanged",
                f"↻ 認証情報を入れ直しました: {describe(hit)}。alias '{alias}' は {describe(other)} が使用中なので名前は変えていません"))
        return 0
    if confirm(T(f"this account is already registered as {describe(hit)}. Rename it to '{alias}'?",
                 f"このアカウントは {describe(hit)} として登録済みです。alias を '{alias}' に変えますか?")):
        old = display(hit)
        hit["label"] = alias
        save_accounts(accs)
        print(T(f"✓ renamed: {old} → {alias}, credentials updated", f"✓ 改名: {old} → {alias}、認証情報も入れ直しました"))
    else:
        print(T(f"↻ updated credentials: {describe(hit)} (alias unchanged; `mag rename` to change it later)",
                f"↻ 認証情報を入れ直しました: {describe(hit)}（alias はそのまま。変えるなら `mag rename`）"))
    return 0


def cmd_update(args) -> int:
    """登録済みアカウントの認証情報を、今ログイン中のものに入れ直す。

    別アカウントでログインしたまま呼ぶと、その alias の保管庫を上書きして
    しまうので、本人確認が取れてメールが一致するときだけ通す。
    """
    acct, err = resolve_account(args.alias)
    if not acct:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    prov, slug = provider_of(acct), acct["slug"]
    cred, ident, err = live_identity(prov, strict=True)
    if err:
        print(T(f"✗ {err} — leaving {display(acct)} untouched", f"✗ {err} — {display(acct)} には触りません"), file=sys.stderr)
        return 1
    if (acct.get("email") or "").lower() != (ident["email"] or "").lower():
        print(T(f"✗ signed in as {ident['email']}, but {display(acct)} is {acct.get('email')} — sign in as that account first",
                f"✗ 今のログインは {ident['email']} ですが、{display(acct)} は {acct.get('email')} です — そのアカウントでログインし直してください"),
              file=sys.stderr)
        return 1
    accs = accounts()
    me = next(a for a in accs if a["slug"] == slug)
    store_credential(prov, slug, cred)
    refresh_entry(me, prov, cred, ident)
    save_accounts(accs)
    set_current(prov, slug)
    print(T(f"↻ updated credentials: {describe(acct)}", f"↻ 認証情報を入れ直しました: {describe(acct)}"))
    return 0


def cmd_remove(args) -> int:
    acct, err = resolve_account(args.name)
    if not acct:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    slug = acct["slug"]
    save_accounts([a for a in accounts() if a["slug"] != slug])
    kc_delete(CODEX_MAG_SERVICE if provider_of(acct) == "codex" else MAG_SERVICE, slug)
    print(T(f"✓ removed: {describe(acct)}", f"✓ 削除: {describe(acct)}"))
    return 0


def cmd_rename(args) -> int:
    """alias を付け替える。slug（内部キー）は動かさないので認証情報はそのまま。"""
    acct, err = resolve_account(args.name)
    if not acct:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    new = args.new_name.strip()
    if not new:
        print(T("✗ new alias is empty", "✗ 新しい alias が空です"), file=sys.stderr)
        return 1
    accs = accounts()
    if err := label_conflict_error(new, accs, acct["slug"]):
        print(f"✗ {err}", file=sys.stderr)
        return 1
    old = display(acct)
    for a in accs:
        if a["slug"] == acct["slug"]:
            a["label"] = new
    save_accounts(accs)
    print(T(f"✓ renamed: {old} → {new} ({acct.get('email', '?')})",
            f"✓ 改名: {old} → {new} ({acct.get('email', '?')})"))
    return 0


def bar(pct) -> str:
    if pct is None:
        return "－－－－－－－－－－   ?"
    n = int(round(min(100.0, max(0.0, pct)) / 10))
    return "█" * n + "░" * (10 - n) + f" {pct:5.1f}%"


def cmd_status(args) -> int:
    clear_expired_cooldowns()
    for prov, slug in (reconcile_current() or {}).items():
        print(T(f"(corrected the active {prov} account to {label_of(slug)} to match reality)",
                f"（{prov} の現在弾を実態に合わせて {label_of(slug)} に修正しました）"))
    if not accounts():
        print(T("No accounts registered. Add one with `mag add`.",
              "アカウントが未登録です。`mag add` で登録してください。"))
        return 1
    rc = 0
    for prov, title in (("claude", "Claude Code"), ("codex", "Codex / ChatGPT")):
        accs = accounts_of(prov)
        if not accs:
            continue
        cur = get_current(prov)
        print(T(f"━━ {title} ━━  {len(accs)} account(s)   active: {label_of(cur) if cur else '(none)'}",
                f"━━ {title} ━━  {len(accs)} 個   使用中: {label_of(cur) if cur else '(なし)'}"))
        rc |= _print_magazine(accs, prov, args)
        print()
    return rc


def _print_magazine(accs: list, prov: str, args) -> int:
    cur = get_current(prov)
    for a in accs:
        slug = a["slug"]
        mark = "▶" if slug == cur else " "
        line = f"{mark} {display(a)}  \033[2m{a.get('email') or ''}\033[0m"
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
                usage = codex_usage_snapshot(slug)
                if usage["note"]:
                    print(f"    {usage['note']}")
                for w in usage["windows"]:
                    print(f"    {w['label']:<10}{bar(w['pct'])}   reset {fmt_usage_reset(w, usage)}")
                if usage.get("reached"):
                    print(T(f"    ⛔ limit reached: {usage['reached']}", f"    ⛔ 上限到達: {usage['reached']}"))
                print_usage_timestamp(usage, indent="    ")
            continue
        p = probe(slug)
        print(line)
        if not p["ok"]:
            print(f"    ? {p['error']}")
        else:
            rows = [("5h", p["five_hour"] or {}), ("7d", p["seven_day"] or {})]
            # モデル別の週次枠（Fable など）。全体枠とは別勘定なので同じゲージで並べる。
            rows += [(f"7d/{sc['model']}", sc) for sc in p.get("scoped") or []]
            width = max(4, max(len(k) for k, _ in rows) + 2)
            for key, blk in rows:
                dim = "\033[2m" if "/" in key else ""
                note = ""
                if "/" in key and (blk.get("pct") or 0) >= 80:
                    note = T("  (this model only; others still work)", "  （このモデルのみ・他は使えます）")
                print(f"    {dim}{key:<{width}}\033[0m{bar(blk.get('pct'))}   reset {fmt_when(blk.get('resets_at'))}{note}")
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
            print(T(f"    🔥 pre-check as next account: {'OK (a small request went through)' if w.get('ok') else 'FAILED: ' + w.get('msg','')[:40]}",
                  f"    🔥 切替候補としての事前確認: {'OK（軽いリクエストが通った）' if w.get('ok') else 'NG: ' + w.get('msg','')[:40]}"))
        print()
    return 0


def record_limits(slug: str, data: dict) -> None:
    """観測できた残量を state に控える。

    Codex は「今装填している弾」の分しか記録が残らないので、切り替える前に
    控えておかないと他の弾の残量を二度と表示できなくなる。
    """
    s = state()
    s.setdefault("limits", {})[slug] = {**data, "ts": data.get("ts") or now()}
    save_state(s)


def known_limits(slug: str) -> dict | None:
    return (state().get("limits") or {}).get(slug)


def codex_usage_snapshot(slug: str, refresh: bool = False) -> dict:
    """表示用の観測値。取得できない場合も、同じアカウントの前回値を残す。

    自動切替の可否判定には使わない。通常表示では追加のリクエストを送らない。
    """
    row = {"windows": [], "note": None}
    old = known_limits(slug)
    lim = None
    if get_current("codex") == slug:
        since = (state().get("last_switch_by") or {}).get("codex")
        lim = codex_live_limits(only_after=since)
        if not lim:
            row["note"] = T("no usage recorded yet (run codex once)",
                            "残量の記録なし（codex で1回やり取りすると出ます）")
    elif refresh:
        lim = codex_probe(slug)
        if lim and lim.get("dead"):
            row["note"] = T("needs re-login (refresh token revoked)", "要再ログイン（refresh token 失効）")
        elif not lim:
            row["note"] = T("could not read usage", "残量を取得できませんでした")

    # 明示更新で保存した値より古いセッション記録に、前回値を巻き戻さない。
    if (lim and lim.get("ts") and old and old.get("windows")
            and (old.get("ts") or 0) > lim["ts"]):
        lim = None
    if lim and not lim.get("dead") and lim.get("windows"):
        row["windows"] = [{"label": w["label"], "pct": w["pct"],
                           "resets_at": w.get("resets_at")} for w in lim["windows"]]
        row["reached"] = lim.get("reached")
        row["observed_ts"] = lim.get("ts") or now()
        record_limits(slug, {"windows": row["windows"], "ts": row["observed_ts"]})
    else:
        if old and old.get("windows"):
            row["windows"] = old["windows"]
            row["stale_ts"] = old.get("ts")
            # 未観測という案内は前回値がある場合には不要。更新失敗の理由は残す。
            if not refresh or get_current("codex") == slug:
                row["note"] = None
        elif not row["note"]:
            row["note"] = T("never observed (use mag limits --refresh to measure)",
                            "未観測（mag limits --refresh で実測できます）")
    return row


def fmt_usage_reset(window: dict, row: dict) -> str:
    reset = window.get("resets_at")
    if row.get("stale_ts") and reset and reset <= now():
        return T("time passed; unverified", "予定時刻経過・未確認")
    return fmt_when(reset)


def print_usage_timestamp(row: dict, indent: str = "      ") -> None:
    ts = row.get("stale_ts") or row.get("observed_ts")
    if not ts:
        return
    at = f"{datetime.fromtimestamp(ts):%m/%d %H:%M}"
    if row.get("stale_ts"):
        text = T(f"(last seen {at}; current usage unknown)", f"(前回観測: {at} 時点・現在の使用量は不明)")
    else:
        text = T(f"(recorded at {at})", f"({at} 時点の記録)")
    print(f"{indent}\033[2m{text}\033[0m")


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
        row.update(codex_usage_snapshot(slug, refresh=getattr(args_ns, "refresh", False)))
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
            if r.get("note"):
                print(f"      \033[2m{r['note']}\033[0m")
            for w in r["windows"]:
                if r["current"]:
                    worst_overall = max(worst_overall, w["pct"] or 0)
                dim = "\033[2m" if w.get("scoped") else ""
                print(f"      {dim}{w['label']:<12}\033[0m {bar(w['pct'])}"
                      f"   reset {fmt_usage_reset(w, r)}")
            mt = r.get("metered") or {}
            if mt.get("limit_reached"):
                print(T("      \033[31m⛔ metered credits exhausted (org-capped) — this account just stops\033[0m",
                        "      \033[31m⛔ 従量課金枠も使い切り（組織で停止中）— このアカウントは止まります\033[0m"))
            elif mt.get("enabled"):
                print(T("      \033[33m💸 metered billing ON — going over the plan window will cost money\033[0m",
                        "      \033[33m💸 従量課金が有効 — 枠を超えると課金されます\033[0m"))
            print_usage_timestamp(r)
            if r.get("reached"):
                print(T(f"      \033[31m⛔ limit reached: {r['reached']}\033[0m",
                        f"      \033[31m⛔ 上限到達: {r['reached']}\033[0m"))
    print()
    return 0


def resolve_account(needle: str) -> tuple[dict | None, str]:
    """alias（label）でアカウントを引く。

    優先順: alias 完全一致 → slug 完全一致（内部キー、通常は打たない）
            → alias の前方一致が1件 → alias/メールの部分一致が1件。
    `main` と `codex-main` が両方あっても `main` は前方一致で `main` に決まる。
    """
    accs = accounts()
    n = (needle or "").strip().lower()
    if not n:
        return None, T("account name is empty", "アカウント名が空です")

    def lab(a):
        return (a.get("label") or "").strip().lower()

    exact = [a for a in accs if lab(a) == n]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        # 旧データで alias が重複している。slug でしか区別できないので、ここだけ出す。
        names = ", ".join(f"{display(a)} [{a['slug']}]" for a in exact)
        return None, T(f"alias '{needle}' is duplicated: {names} — fix with `mag rename <slug> NEW`",
                       f"alias '{needle}' が重複しています: {names} — `mag rename <slug> 新名` で直してください")
    by_slug = next((a for a in accs if a["slug"] == needle), None)
    if by_slug:
        return by_slug, ""
    prefix = [a for a in accs if lab(a).startswith(n)]
    if len(prefix) == 1:
        return prefix[0], ""
    hits = prefix or [a for a in accs
                      if n in lab(a) or n in (a.get("email") or "").lower()]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, T(f"no account matches '{needle}'", f"'{needle}' に一致するアカウントがありません")
    names = ", ".join(describe(a) for a in hits)
    return None, T(f"'{needle}' matches several: {names}", f"'{needle}' が複数に一致します: {names}")


def cmd_load(args) -> int:
    acct, err = resolve_account(args.alias)
    if not acct:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    prov = provider_of(acct)
    if get_current(prov) == acct["slug"]:
        print(T(f"= already active: {describe(acct)}", f"= すでに使用中: {describe(acct)}"))
        return 0
    if not do_load(acct["slug"], "manual"):
        return 1
    print(T(f"🔁 switched to: {describe(acct)}", f"🔁 切り替え: {describe(acct)}"))
    print_codex_switch_note(prov, display(acct))
    return 0


def cmd_next(args) -> int:
    prov = getattr(args, "provider", "claude")
    cur = get_current(prov)
    slug, report = next_slug(cur, probe_all=not args.no_probe, provider=prov)
    if not slug:
        print(T(f"✗ No usable {prov} account left.", f"✗ 使える {prov} アカウントがありません。"), file=sys.stderr)
        for s_, why in report:
            print(f"   - {label_of(s_)}: {why}", file=sys.stderr)
        bslug, when = soonest_reset()
        if bslug:
            print(T(f"   earliest recovery: {label_of(bslug)} → {fmt_when(when)}",
                  f"   最短の復帰: {label_of(bslug)} → {fmt_when(when)}"), file=sys.stderr)
        return 2
    if slug == cur:
        print(T(f"= keeping: {label_of(slug)}", f"= そのまま使用: {label_of(slug)}"))
        return 0
    ok = do_load(slug, "next")
    if ok:
        acct = find_account(slug)
        print(T(f"🔁 switched to: {describe(acct)}", f"🔁 切り替え: {describe(acct)}"))
        print_codex_switch_note(prov, display(acct))
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
                print(T(f"[magazine] {label_of(cur)}: 5h {pct if pct is not None else '?'}% — keeping",
                      f"[magazine] {label_of(cur)}: 5h {pct if pct is not None else '?'}% — そのまま"))
            return 0
        print(T(f"[magazine] {label_of(cur)} is out ({why})", f"[magazine] {label_of(cur)} 上限到達 ({why})"))
    rc = cmd_next(argparse.Namespace(no_probe=args.no_probe, provider=prov))
    if rc == 2 and not getattr(args, "strict", False):
        # 起動前の判定はまだ「上限確定」ではない。残りを使い切らせるため現弾のまま起動する。
        print(T("[magazine] No spare account. Starting on the current one (a real limit will be detected).",
                    "[magazine] 使える予備がありません。現在のアカウントのまま起動します（上限に当たれば検知します）"))
        return 0
    return rc


def cmd_hit(args) -> int:
    """上限ヒットを手動で記録して次弾へ。"""
    if args.alias:
        acct, err = resolve_account(args.alias)
        if not acct:
            print(f"✗ {err}", file=sys.stderr)
            return 1
        cur = acct["slug"]
    else:
        cur = get_current("claude")
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
    print(T(f"⛔ marked as limited: {label_of(cur)} ({kind or 'five_hour'})",
            f"⛔ 上限到達として記録: {label_of(cur)} ({kind or 'five_hour'})"))
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
    effort = ((data.get("effort") or {}).get("level"))
    if effort:
        parts.append(f"effort:{effort}")
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
                with open(jr, errors="replace", encoding="utf-8") as f:
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
        print(T(f"No sessions stopped by a limit in the last {args.hours:.0f}h.",
                f"直近 {args.hours:.0f}h に上限で止まったセッションはありません。"))
        return 0
    print(T(f"Sessions stopped by a limit or login error: {len(sessions)}\n",
            f"上限などで止まっているセッション: {len(sessions)} 件\n"))
    for i, s in enumerate(sessions, 1):
        when = (s["timestamp"] or "")[:19].replace("T", " ")
        print(f"{i}. {s['session_id']}")
        print(f"   cwd    : {s['cwd'] or '?'}")
        print(T(f"   reason : {s['stalled_by'] or 'unknown'}   last {when}",
                f"   停止   : {s['stalled_by'] or '(不明)'}   最終 {when}"))
        if s["last_user"]:
            print(T(f"   task   : {s['last_user']}", f"   最後の指示: {s['last_user']}"))
        if s["workflow"]:
            wf = s["workflow"]
            cached = wf.get("cached_agents") or 0
            saved = (T(f"{cached} completed agent results in journal", f"完了 {cached} エージェント分はキャッシュ再利用")
                     if cached else T("no cached results", "キャッシュなし"))
            print(f"   ⚙ workflow: {wf['name']}  runId={wf['run_id']}  → {saved}")
        print(T(f"   resume : mag resume {s['session_id']}", f"   再開   : mag resume {s['session_id']}"))
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
    warm = s.setdefault("warm", {})
    warm[target] = {
        "at": now(), "ok": ok, "msg": msg, "for_since": s.get("last_switch", 0),
    }
    # 外した弾の検証結果が残ると、doctor に出ない名前の NG が並んで紛らわしい
    known = {a["slug"] for a in accounts()}
    for slug in [k for k in warm if k not in known]:
        del warm[slug]
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
    mark_watch_alive(started=True)
    last_api = 0.0
    while True:
        try:
            mark_watch_alive()
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
            #      Claude と違い auth.json の差し替えは即時には効かない。codex は認証を
            #      ターンの区切り（と認証エラーからの復帰）でしか読み直さないので、開いて
            #      いるセッションは次のターンまで前のアカウントのまま走る（実測: 反映まで
            #      十数分、その間に上限メッセージが1回出る）。
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
                                   f"Codex {cw['label']} {cw['pct']:.0f}% → {lbl} に装填"
                                   + ("（開いているセッションは次のターンから）"
                                      if codex_session_recently_active() else ""))

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


WATCH_HEARTBEAT = 60.0    # state に生存を書き込む間隔（秒）


def mark_watch_alive(started: bool = False) -> None:
    """常駐監視が「どの版で」「いつまで」動いていたかを残す。

    mag.py を更新しても常駐を再起動するまで古い版が動き続ける。
    後から気づけるよう、動いている版のタイムスタンプを置いておく。
    """
    s = state()
    w = s.get("watch") or {}
    if not started and now() - float(w.get("at") or 0) < WATCH_HEARTBEAT:
        return
    path = os.path.abspath(__file__)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = None
    s["watch"] = {
        "pid": os.getpid(), "at": now(), "script": path, "script_mtime": mtime,
        "version": VERSION,
        "started_at": now() if started else (w.get("started_at") or now()),
    }
    save_state(s)


def watch_restart_hint() -> str:
    if IS_MAC:
        return f"launchctl kickstart -k gui/{os.getuid()}/com.claude-magazine.watch"
    if IS_WINDOWS:
        return "schtasks /end /tn magazine-watch && schtasks /run /tn magazine-watch"
    return "systemctl --user restart magazine-watch"


def watch_version_state() -> tuple[bool, str]:
    """常駐が今のファイルと同じ版で動いているか (揃っている?, 説明)。"""
    w = state().get("watch") or {}
    if not w.get("at"):
        return True, T("version unknown (restart once to start reporting)",
                       "版は不明（一度再起動すると分かるようになります）")
    age = now() - float(w["at"])
    if age > 10 * 60:
        return False, T(f"last heartbeat {int(age // 60)} min ago — it may be dead",
                        f"最後の生存確認が {int(age // 60)} 分前 — 落ちている可能性")
    path = w.get("script") or os.path.abspath(__file__)
    try:
        disk = os.stat(path).st_mtime
    except OSError:
        return True, T("running", "稼働中")
    if w.get("script_mtime") and abs(disk - float(w["script_mtime"])) > 1:
        return False, T(f"running an older copy (loaded {fmt_when(float(w['script_mtime']))})"
                        f" — restart it: {watch_restart_hint()}",
                        f"読み込んだのは {datetime.fromtimestamp(float(w['script_mtime'])):%m/%d %H:%M} 版 —"
                        f" 再起動してください: {watch_restart_hint()}")
    return True, T("running the current version", "現在の版で稼働中")


def watch_daemon_state() -> str:
    """常駐監視が動いているか。見え方は OS ごとに違う。"""
    try:
        if IS_MAC:
            r = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.magazine.watch"],
                capture_output=True, text=True, timeout=15)
            if "state = running" in r.stdout:
                return T("running", "稼働中")
            r = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.claude-magazine.watch"],
                capture_output=True, text=True, timeout=15)
            if "state = running" in r.stdout:
                return T("running", "稼働中")
            return T("not running (re-run install.sh to enable)",
                     "停止中（install.sh を再実行すると入ります）")
        if IS_WINDOWS:
            r = subprocess.run(["schtasks", "/query", "/tn", "magazine-watch"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return (T("registered and running", "登録済み・実行中")
                        if "Running" in r.stdout or "実行中" in r.stdout
                        else T("registered", "登録済み"))
            return T("not registered (windows\\register-task.ps1)",
                     "未登録（windows\\register-task.ps1 で登録）")
        r = subprocess.run(["systemctl", "--user", "is-active", "magazine-watch"],
                           capture_output=True, text=True, timeout=15)
        if r.stdout.strip() == "active":
            return T("running", "稼働中")
        return T("not running (systemctl --user enable --now magazine-watch)",
                 "停止中（systemctl --user enable --now magazine-watch）")
    except (OSError, subprocess.SubprocessError):
        return T("unknown", "不明")


def cmd_doctor(args) -> int:
    ok = True
    print(T("-- magazine doctor --", "-- magazine doctor --"))
    cur = current_oauth()
    where = (f"keychain {live_service()}/{LIVE_ACCOUNT}" if use_keychain()
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
            mark = "OK" if has else T("credential missing", "認証情報なし")
            print(f"  - {describe(a):<40} {mark}")
            ok &= has
    dups = {}
    for a in accounts():
        dups.setdefault((a.get("label") or "").strip().lower(), []).append(a)
    for group in (g for g in dups.values() if len(g) > 1):
        ok = False
        print(T(f"⚠ duplicate alias '{display(group[0])}' — give each a unique name:",
                f"⚠ alias '{display(group[0])}' が重複しています — それぞれ別名にしてください:"))
        for a in group:
            print(f"    mag rename {a['slug']} <new-alias>    # {a.get('email', '?')} / {provider_of(a)}")
    # `claude auth status` はプロフィールをキャッシュしており Keychain の差し替えに
    # 追従しない（別アカウントを表示し続ける）。実際に入っている弾は
    # refreshToken の一致で判定する。
    reconcile_current()
    live_rt = (current_oauth() or {}).get("refreshToken")
    real = next((a for a in accounts_of("claude")
                 if (stored_oauth(a["slug"]) or {}).get("refreshToken") == live_rt), None)
    name = real.get("label") if real else T("an unregistered account", "未登録のアカウント")
    print(T(f"active Claude acct : {describe(real) if real else name}",
            f"実際の Claude 現用 : {describe(real) if real else name}"))
    st = auth_status()
    if real and st.get("email") and st["email"] != real.get("email"):
        print(f"  ※ `claude auth status` は {st['email']} と表示しますが、"
              f"これはキャッシュで実態は上記です")
    if accounts_of("codex"):
        cauth = codex_live_auth()
        ident = codex_identity(cauth) if cauth else {}
        print(T(f"codex account      : {ident.get('email') or 'not signed in'} ({ident.get('plan') or '?'})",
                f"codex アカウント   : {ident.get('email') or '未ログイン'} ({ident.get('plan') or '?'})"))
    hooked = False
    for fn in ("statusline-command.sh", "statusline-command.cmd"):
        p_ = os.path.join(claude_config_dir(), fn)
        if os.path.exists(p_):
            with open(p_, errors="replace", encoding="utf-8") as f:
                body = f.read()
            # 実体を直接指すことも、PATH 上の mag（symlink）を指すこともある
            hooked = hooked or ("statusline" in body and re.search(r"\bmag(\.py)?\b", body) is not None)
    print(T(f"statusLine hook    : {'OK' if hooked else 'not wired (mag install-statusline)'}",
            f"statusLine 連携    : {'OK' if hooked else '未接続（mag install-statusline で接続）'}"))
    print(T(f"watch daemon       : {watch_daemon_state()}",
            f"watch 常駐         : {watch_daemon_state()}"))
    fresh, why = watch_version_state()
    print(T(f"watch version      : {why}", f"watch の版         : {why}"))
    ok &= fresh
    # 現用スロットは本体も同時に書き換える。同じロックを取れているかを見せる。
    lock = storage_lock_path()
    holder = ""
    if os.path.isdir(lock):
        try:
            holder = T(f" (held, {int(now() - os.stat(lock).st_mtime)}s)",
                       f"（保持中・{int(now() - os.stat(lock).st_mtime)}秒）")
        except OSError:
            holder = ""
    print(T(f"write lock         : {lock}{holder}",
            f"書き込みロック     : {lock}{holder}"))
    warm = state().get("warm") or {}
    if warm:
        print(T("pre-checked next   :", "次の候補の事前検証 :"))
        known = {a["slug"] for a in accounts()}
        for slug, w in warm.items():
            if slug not in known:
                continue
            mark = "OK" if w.get("ok") else f"NG ({w.get('msg', '')[:40]})"
            print(f"  - {label_of(slug):<32} {mark}")
    print(T(f"log                : {LOG_PATH}", f"ログ               : {LOG_PATH}"))
    return 0 if ok else 1


def cmd_install_statusline(args) -> int:
    """statusLine から mag を呼ぶようにする（既存設定はバックアップする）。

    走っているセッションの残量は、Claude Code が statusLine に渡す JSON に
    含まれている。ここを経由すれば追加の API コール無しで読める。
    """
    conf_dir = claude_config_dir()
    os.makedirs(conf_dir, exist_ok=True)
    settings_path = os.path.join(conf_dir, "settings.json")
    settings = read_json(settings_path, None) if os.path.exists(settings_path) else {}
    if not isinstance(settings, dict):
        print(T(f"✗ {settings_path} must contain a JSON object; left unchanged",
                f"✗ {settings_path} は JSON オブジェクトである必要があります。変更しません"), file=sys.stderr)
        return 1
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
        bak = path + f".bak.{time.time_ns()}"
        with open(path, encoding="utf-8") as f:
            old = f.read()
        with open(bak, "w", encoding="utf-8") as f:
            f.write(old)
        print(T(f"backup: {bak}", f"バックアップ: {bak}"))

    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(script)
    if not IS_WINDOWS:
        os.chmod(path, 0o755)

    # settings.json の statusLine もこのファイルを指すようにしておく
    want = f'"{path}"' if IS_WINDOWS else f'bash "{path}"'
    previous = settings.get("statusLine")
    previous = previous if isinstance(previous, dict) else {}
    if previous.get("command") != want or previous.get("type") != "command":
        if os.path.exists(settings_path):
            backup = settings_path + f".bak.{time.time_ns()}"
            shutil.copy2(settings_path, backup)
            print(T(f"backup: {backup}", f"バックアップ: {backup}"))
        settings["statusLine"] = {**previous, "type": "command", "command": want}
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
  mag use sub                    switch to one by alias (a unique prefix is enough)
  mag rename sub work            change an alias
  mag stalled                    find sessions stopped by a limit
  mag resume <session-id>        resume one (an interrupted workflow continues)

Registering (the alias is what you type from then on):
  mag login claude sub           sign in to another account; the active one is untouched
  mag login codex codex-sub
  claude auth login  ->  mag add main          register the account you are already signed in to
  codex login        ->  mag add codex-main --provider codex
  signed in again?   ->  mag update main
""", """よく使う流れ:
  mag limits                     全アカウントの残量を一覧
  mag next                       次のアカウントに切り替え
  mag use sub                    alias で切り替え（一意に決まる先頭数文字で可）
  mag rename sub work            alias を変更
  mag stalled                    上限で止まったセッションを探す
  mag resume <session-id>        再開（中断した workflow は続きから）

登録（alias が以後打つ名前になる）:
  mag login claude sub           別アカウントでログインして登録（使用中の認証はそのまま）
  mag login codex codex-sub
  claude auth login  ->  mag add main          今ログイン中のアカウントを登録
  codex login        ->  mag add codex-main --provider codex
  ログインし直した   ->  mag update main
"""),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"magazine {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    a = sub.add_parser("add", help=T("register the account you are signed in to now, under an alias","現在ログイン中のアカウントを alias を付けて登録"))
    a.add_argument("alias", metavar="ALIAS",
                   help=T("short unique name you will type to switch", "切り替え時に打つ短い一意な名前"))
    a.add_argument("--provider", choices=["claude", "codex"], default="claude",
                   help=T("use codex to register a ChatGPT (codex CLI) account","codex を指定すると ChatGPT (codex CLI) 側に登録"))
    a.set_defaults(func=cmd_add)

    a = sub.add_parser("login", help=T("sign in to another account without touching the active one, and register it",
                                       "使用中の認証に触れずに別アカウントでログインし、登録する"))
    a.add_argument("provider", choices=["claude", "codex"])
    a.add_argument("alias", metavar="ALIAS", help=T("alias for the account (an existing alias for the same provider re-stores that account's credentials)",
                                                    "このアカウントの alias（同じ provider の登録済み alias なら認証の入れ直し）"))
    a.set_defaults(func=cmd_login)

    a = sub.add_parser("update", help=T("re-store a registered account's credentials from the current sign-in",
                                        "登録済みアカウントの認証情報を今のログインで入れ直す"))
    a.add_argument("alias", metavar="ALIAS"); a.set_defaults(func=cmd_update)


    a = sub.add_parser("remove", aliases=["rm"], help=T("remove an account","アカウントを削除"))
    a.add_argument("name", metavar="ALIAS"); a.set_defaults(func=cmd_remove)

    a = sub.add_parser("rename", aliases=["mv"], help=T("change an account's alias","アカウントの alias を変更"))
    a.add_argument("name", metavar="ALIAS"); a.add_argument("new_name", metavar="NEW_ALIAS")
    a.set_defaults(func=cmd_rename)

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

    a = sub.add_parser("load", aliases=["use"], help=T("switch to an account by alias (unique prefix ok)","alias でアカウントに切り替え（一意な先頭文字列で可）"))
    a.add_argument("alias", metavar="ALIAS"); a.set_defaults(func=cmd_load)

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
    a.add_argument("--kind", choices=["five_hour", "seven_day"])
    a.add_argument("--account", "--slug", dest="alias", metavar="ALIAS",
                   help=T("which account (default: the active one)", "対象アカウント（省略時は使用中のもの）"))
    a.set_defaults(func=cmd_hit)


    a = sub.add_parser("watch", help=T("daemon: switch before limits, without stopping sessions","常駐監視：セッションを止めずに事前切替"))
    a.add_argument("--interval", type=float, default=20.0, help="live 監視の間隔（秒）")
    a.add_argument("--api-interval", type=float, default=300.0, help="usage API の裏取り間隔（秒）")
    a.add_argument("--threshold", type=float, default=None,
                   help=T("usage percentage to switch at (default: 97%%)", "切替する使用率（既定 97%%）"))
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
