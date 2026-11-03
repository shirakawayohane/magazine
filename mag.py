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

端末を横取りしたり、走っているプロセスを止めたりはしない。
Codex の共有サーバーには、常駐処理から認証情報の反映と更新を行う。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import queue
import re
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

VERSION = "0.1.3"

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
CODEX_AUTH_PATH = os.path.join(os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))), "auth.json")
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
    # 1 日 1 回、新しいリリースが出ていないか確認する（GitHub に問い合わせるだけ）
    "update_check": True,
    # 中継（mag relay）の待ち受けポート（127.0.0.1 のみ）
    "relay_port": 47821,
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


@contextlib.contextmanager
def file_lock(path: str):
    """プロセス間・スレッド間の排他（ファイルごとに別の fd を開くので両方に効く）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        if os.name == "nt":
            import msvcrt
            # 中身は書かない: 他のスレッドが鍵をかけた範囲へ書くと PermissionError になる。
            # Windows はファイル末尾より先のバイトにも鍵をかけられる。
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


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
    if not slug:
        return refresh_oauth(oauth) or oauth
    # refresh token は使い捨て。中継・常駐監視・手で打った mag が同時に更新すると、
    # 負けた側のトークンが失効する。プロセスをまたいで 1 人ずつにし、
    # 鍵を取れたら保管庫を読み直す（先に誰かが更新していればそれを使う）。
    with file_lock(os.path.join(ROOT, "locks", f"refresh-{slug}.lock")):
        latest = stored_oauth(slug) or oauth
        exp = latest.get("expiresAt")
        if exp and (exp / 1000.0) - now() > 120:
            return latest
        new = refresh_oauth(latest)
        if not new:
            return latest
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


WINDOW_LABEL = {"five_hour": "5h", "seven_day": "7d"}


def peak_window(p: dict | None) -> tuple[str | None, float | None]:
    """probe 結果のうち、報告されている枠で最も逼迫したもの (kind, pct)。"""
    best = (None, None)
    for k in ("five_hour", "seven_day"):
        pct = ((p or {}).get(k) or {}).get("pct")
        if pct is not None and (best[1] is None or pct > best[1]):
            best = (k, pct)
    return best


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


def codex_refresh(auth: dict, timeout: float = 20) -> tuple[dict | None, str | None]:
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
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


def codex_ensure_fresh(slug: str | None, auth: dict, verify: bool = False) -> tuple[dict, str | None, bool]:
    """(使える auth, 失効理由, サーバーで確かめられたか)。更新できたら保管庫へ書き戻す。

    verify=True では期限に関わらず必ず 1 回更新する。現用スロットを踏み潰す前に
    「この弾はまだ生きているか」を確かめるため。refresh token はローテーションする
    ので、保管したスナップショットは本体が 1 度更新しただけで死ぬ。死んだ弾を
    装填すると codex 側が auth.json を消してしまい、生きていたログインまで失う。

    3 つ目は、この呼び出しでサーバーの返事を得られたか。圏外で確かめられなかった
    ときは False で、手元の auth をそのまま返す。生死の分からない弾を装填しては
    いけない（直前に失効と判定した弾を圏外の隙に装填してしまい、以後に始めた
    codex セッションが全部止まった）。装填するかどうかは呼び出し側が決める。
    """
    exp = jwt_claims(((auth or {}).get("tokens") or {}).get("access_token") or "").get("exp")
    if not verify and exp and exp - now() > 300:
        return auth, None, False
    new, dead = codex_refresh(auth)
    if dead:
        return auth, dead, True
    if not new:
        return auth, None, False     # 通信できないだけ。生死は分からない
    if slug:
        codex_store_auth(slug, new)
    return new, None, True


def codex_sync_live() -> str | None:
    with codex_auth_lock():
        return _codex_sync_live()


def _codex_sync_live() -> str | None:
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
    live_at = parse_iso(live.get("last_refresh")) or 0
    for a in accs:
        if email and (a.get("email") or "").lower() == email:
            stored = codex_stored_auth(a["slug"]) or {}
            if (parse_iso(stored.get("last_refresh")) or 0) > live_at:
                # 保管庫の方が新しい＝`mag login` で入れ直した直後。現用（古く、
                # たいてい失効済み）で上書きすると、入れ直したログインをその場で失い、
                # 何度ログインし直しても戻れなくなる（実際に起きた）。新しい方を残す。
                return a["slug"]
            codex_store_auth(a["slug"], live)
            log(f"sync: {a['slug']} の保管トークンを現用の最新版に更新（rotation 追従）")
            return a["slug"]
    return None


CODEX_SESSIONS_DIR = os.path.join(os.path.dirname(CODEX_AUTH_PATH), "sessions")


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
    if os.path.lexists(codex_daemon_socket()) or not codex_session_recently_active():
        return None
    return T(f"  note: standalone Codex processes may still use the previous account instead of {name}. "
             "Only the default shared server supports switching without restarting.",
             f"  注: 独立して動く Codex は {name} に切り替わらない場合があります。"
             "再起動せず切り替えられるのは共有サーバーです。")


def codex_daemon_socket() -> str:
    return os.path.join(os.path.dirname(CODEX_AUTH_PATH),
                        "app-server-control", "app-server-control.sock")


class CodexLocalRPC:
    """Persistent local WebSocket; handles the auth host's refresh requests."""
    def __init__(self, on_request=None):
        self.on_request = on_request

    def __enter__(self):
        self.deadline = time.monotonic() + 8
        self.sock = socket.socket(socket.AF_UNIX)
        self.request_id = 0
        try:
            self.sock.settimeout(8)
            self.sock.connect(os.path.realpath(codex_daemon_socket()))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Key: " + key +
                               "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                if len(header) >= 8192:
                    raise ValueError("oversized handshake")
                header += self.read(1)
            lines = header.decode("ascii").split("\r\n")
            headers = {name.lower(): value.strip() for name, value in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if " 101 " not in lines[0] or headers.get("sec-websocket-accept") != expected:
                raise ValueError("invalid websocket handshake")
            self.rpc("initialize", {"clientInfo": {"name": "magazine", "version": VERSION},
                                    "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized", "params": {}})
            return self
        except Exception:
            self.sock.close()
            raise

    def __exit__(self, *args):
        self.sock.close()

    def read(self, n):
        data = b""
        while len(data) < n:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise OSError("server disconnected")
            data += chunk
        return data

    def frame(self, data, opcode=1):
        mask = os.urandom(4)
        n = len(data)
        if n > 65535:
            raise ValueError("oversized outgoing message")
        head = bytes([128 | opcode, 128 | n]) if n < 126 else bytes([128 | opcode, 254]) + struct.pack("!H", n)
        self.sock.settimeout(8)
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def send(self, message):
        self.frame(json.dumps(message).encode())

    def receive(self):
        while True:
            head = self.read(2)
            n = head[1] & 127
            if n == 126:
                n = struct.unpack("!H", self.read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self.read(8))[0]
            if n > 4 * 1024 * 1024 or head[1] & 128 or head[0] & 112 or not head[0] & 128:
                raise ValueError("unsupported websocket frame")
            data = self.read(n)
            opcode = head[0] & 15
            if opcode == 9:
                if n > 125:
                    raise ValueError("invalid ping")
                self.frame(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode != 1:
                raise ValueError("unexpected websocket frame")
            message = json.loads(data)
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            return message

    def dispatch(self, message):
        if "method" not in message or "id" not in message:
            return
        try:
            if self.on_request is None:
                raise ValueError("unsupported server request")
            result = self.on_request(message["method"], message.get("params") or {})
            reply = {"id": message["id"], "result": result}
        except Exception:
            # Provider error text and request parameters may contain credentials.
            reply = {"id": message["id"], "error": {"code": -32603,
                     "message": "magazine could not renew authentication; check mag status"}}
        self.send(reply)

    def rpc(self, method, params):
        self.request_id += 1
        request_id = self.request_id
        self.deadline = time.monotonic() + 8
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if "method" in message:
                self.dispatch(message)
            elif message.get("id") == request_id:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise ValueError("request failed")
                return message["result"]

    def poll(self, timeout=1):
        if select.select([self.sock], [], [], timeout)[0]:
            self.deadline = time.monotonic() + 8
            self.dispatch(self.receive())


def codex_sync_state_path() -> str:
    # Only coordination/health data. auth.json remains the credential source.
    return os.path.join(ROOT, "codex-auth-sync.json")


def codex_daemon_busy_count(client) -> int | None:
    """Only when a handover is needed: N thread reads + two lists, max 500 threads."""
    deadline = time.monotonic() + 8
    try:
        def loaded():
            ids, cursor = set(), None
            for _ in range(10):
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                result = client.rpc("thread/loaded/list", {"cursor": cursor, "limit": 100})
                data = result.get("data")
                if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
                    raise ValueError("invalid thread list")
                ids.update(data)
                if len(ids) > 500:
                    raise ValueError("too many threads")
                cursor = result.get("nextCursor")
                if cursor is None:
                    return ids
            raise ValueError("too many pages")
        ids = loaded()
        busy = 0
        for thread_id in ids:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            result = client.rpc("thread/read", {"threadId": thread_id, "includeTurns": False})
            status = result["thread"]["status"]["type"]
            if status == "active":
                busy += 1
            elif status not in ("idle", "systemError"):
                raise ValueError("unknown thread status")
        return busy if ids == loaded() else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


_CODEX_HELD_LOCKS = threading.local()


@contextlib.contextmanager
def codex_auth_lock(suffix=".mag.lock", blocking=True):
    # Reentrant for refresh requests received while an auth RPC is in flight.
    path = CODEX_AUTH_PATH + suffix
    held = _CODEX_HELD_LOCKS.__dict__
    if path in held:
        yield held[path]
        return
    os.makedirs(os.path.dirname(CODEX_AUTH_PATH), exist_ok=True)
    with open(path, "a+") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write("0"); lock.flush()
                lock.seek(0)
                while True:
                    try:
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if not blocking:
                            raise BlockingIOError() from None
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
        else:
            held[path] = lock
            try:
                yield lock
            finally:
                del held[path]
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def codex_sync_report(status, request_id=None):
    with codex_auth_lock(".mag-state.lock"):
        current = read_json(codex_sync_state_path(), {})
        if request_id is not None and current.get("id") != request_id:
            return
        current.update(status=status, checked_at=now())
        write_json(codex_sync_state_path(), current)


def codex_apply_selection() -> bool:
    """Start one persistent auth host. Never restart Codex or stop conversations."""
    if not os.path.lexists(codex_daemon_socket()):
        print(T("  No shared Codex server found. New sessions will use the saved login; "
                "standalone processes need their own restart.",
                "  Codex 共有サーバーは見つかりません。新しいセッションは保存した認証を使います。"
                "独立して動くプロセスは再起動が必要です。"))
        return True
    try:
        # Retire requests left by the previous deferred-restart implementation.
        # A running legacy worker checks this queue before restarting anything.
        legacy = os.path.join(ROOT, "codex-restart.json")
        if os.path.exists(legacy):
            import fcntl
            with open(legacy + ".lock", "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                old = read_json(legacy, {})
                if old.get("status") == "pending":
                    write_json(legacy, {**old, "status": "superseded"})
        with codex_auth_lock(".mag-state.lock"):
            write_json(codex_sync_state_path(), {"id": uuid.uuid4().hex, "status": "pending", "at": now()})
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "_codex-auth-worker"],
                             env={**os.environ, "MAGAZINE_HOME": ROOT,
                                  "CODEX_HOME": os.path.dirname(CODEX_AUTH_PATH)},
                             cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ImportError):
        with contextlib.suppress(OSError, ImportError):
            codex_sync_report("failed")
        print(T("  Login saved, but the authentication monitor could not start. Retry `mag use <alias>`.",
                "  認証は保存済みですが、認証の常駐監視を起動できません。`mag use <alias>` を再実行してください。"),
              file=sys.stderr)
        return False
    print(T("  Login saved. Once conversations finish, magazine will apply it without restarting Codex and keep its tokens fresh. "
            "Check `mag status` for confirmation.",
            "  認証を保存しました。会話の処理終了後、Codexを再起動せずに反映し、magazineが認証の更新を続けます。"
            "`mag status` で反映状況を確認できます。"))
    return True


class CodexAuthError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(status)


def codex_auth_tokens(auth):
    tokens = (auth or {}).get("tokens") or {}
    identity = codex_identity(auth)
    account_id = tokens.get("account_id") or identity.get("account_id")
    if not tokens.get("access_token") or not tokens.get("refresh_token") or not account_id:
        raise CodexAuthError("reauthentication_required")
    if auth.get("OPENAI_API_KEY") or auth.get("auth_mode") not in (None, "chatgpt"):
        raise CodexAuthError("reauthentication_required")
    return {"accessToken": tokens["access_token"], "chatgptAccountId": account_id,
            "chatgptPlanType": identity.get("plan")}


class CodexAuthSync:
    def __init__(self):
        self.applied = None
        self.applied_auth = None
        self.request_id = None
        self.last_report = 0
        self.failed_tokens = None
        self.needs_refresh_for = None
        self.next_idle_check = 0

    def live(self, previous_account_id=None):
        """Caller holds the shared selection/refresh lock."""
        auth = codex_live_auth() or {}
        tokens = codex_auth_tokens(auth)
        signature = (tokens["accessToken"], (auth.get("tokens") or {}).get("refresh_token"))
        if self.failed_tokens == signature:
            raise CodexAuthError("reauthentication_required")
        expires = jwt_claims(tokens["accessToken"]).get("exp")
        unauthorized = (previous_account_id == tokens["chatgptAccountId"] and tokens == self.applied)
        if unauthorized or (isinstance(expires, (int, float)) and expires <= now() + 300):
            new, dead = codex_refresh(auth, timeout=6)
            if dead:
                self.failed_tokens = signature
                raise CodexAuthError("reauthentication_required")
            if not new:
                raise CodexAuthError("refresh_failed")
            # Save rotation before contacting the daemon; never resurrect an old token.
            try:
                codex_install_auth(new)
                codex_sync_live()
            except (OSError, RuntimeError):
                raise CodexAuthError("storage_failed") from None
            tokens = codex_auth_tokens(new)
        return tokens

    def refresh_request(self, method, params):
        if method != "account/chatgptAuthTokens/refresh":
            raise ValueError("unsupported server request")
        try:
            with codex_auth_lock():
                tokens = self.refresh_runtime(params.get("previousAccountId"))
                if self.applied and tokens["chatgptAccountId"] == self.applied["chatgptAccountId"]:
                    # Codex installs a successful refresh response itself. Do
                    # not run login/start again during the in-flight turn.
                    self.applied = tokens
                    live = codex_live_auth() or {}
                    if codex_auth_tokens(live)["chatgptAccountId"] == tokens["chatgptAccountId"]:
                        self.applied_auth = live
                self.needs_refresh_for = None
                return tokens
        except CodexAuthError as exc:
            self.needs_refresh_for = params.get("previousAccountId")
            codex_sync_report(exc.status)
            raise
        except OSError:
            self.needs_refresh_for = params.get("previousAccountId")
            codex_sync_report("storage_failed")
            raise

    def refresh_runtime(self, previous_account_id):
        current = codex_auth_tokens(codex_live_auth() or {})
        if previous_account_id == current["chatgptAccountId"]:
            return self.live(previous_account_id)
        # A switch can be pending while an older account's turn finishes.
        # Renew that turn's account without overwriting the selected file.
        if not self.applied or previous_account_id != self.applied["chatgptAccountId"]:
            raise CodexAuthError("reauthentication_required")
        auth = self.applied_auth
        owner = None
        for account in accounts_of("codex"):
            candidate = codex_stored_auth(account["slug"]) or {}
            candidate_id = (candidate.get("tokens") or {}).get("account_id") or codex_identity(candidate).get("account_id")
            if candidate_id == previous_account_id:
                owner = account["slug"]
                if (parse_iso(candidate.get("last_refresh")) or 0) > (parse_iso(auth.get("last_refresh")) or 0):
                    auth = candidate
                break
        tokens = codex_auth_tokens(auth)
        signature = (tokens["accessToken"], (auth.get("tokens") or {}).get("refresh_token"))
        if self.failed_tokens == signature:
            raise CodexAuthError("reauthentication_required")
        if tokens["accessToken"] == self.applied["accessToken"]:
            # Without a vault slot we cannot safely preserve this rotation
            # while another account occupies the selected credential file.
            if owner is None:
                raise CodexAuthError("reauthentication_required")
            new, dead = codex_refresh(auth, timeout=6)
            if dead:
                self.failed_tokens = signature
                raise CodexAuthError("reauthentication_required")
            if not new:
                raise CodexAuthError("refresh_failed")
            auth = new
            tokens = codex_auth_tokens(new)
        self.applied_auth = auth
        if owner is not None:
            try:
                codex_store_auth(owner, auth)
            except (OSError, RuntimeError):
                raise CodexAuthError("storage_failed") from None
        return tokens

    def tick(self, client):
        with codex_auth_lock():
            request = read_json(codex_sync_state_path(), {})
            if self.needs_refresh_for is not None:
                self.refresh_runtime(self.needs_refresh_for)
            self.needs_refresh_for = None
            tokens = self.live()
            if tokens != self.applied or self.request_id != request.get("id"):
                if now() < self.next_idle_check:
                    return
                busy = codex_daemon_busy_count(client)
                if busy != 0:
                    codex_sync_report("pending", request.get("id"))
                    self.next_idle_check = now() + 5
                    return
                self.next_idle_check = 0
                # The lock covers both selection and RPC, so an older snapshot
                # cannot be installed over a newly selected account.
                try:
                    codex_sync_live()
                except (OSError, RuntimeError):
                    raise CodexAuthError("storage_failed") from None
                result = client.rpc("account/login/start", {"type": "chatgptAuthTokens", **tokens})
                if result.get("type") != "chatgptAuthTokens":
                    raise ValueError("external authentication not confirmed")
                observed = client.rpc("getAuthStatus", {"includeToken": True, "refreshToken": False})
                if observed.get("authToken") != tokens["accessToken"]:
                    raise ValueError("authentication application unconfirmed")
                self.applied = tokens
                self.applied_auth = codex_live_auth()
                self.request_id = request.get("id")
                codex_sync_report("active", self.request_id)
                self.last_report = now()
            elif now() - self.last_report >= 10:
                codex_sync_report("active", self.request_id)
                self.last_report = now()


def cmd_codex_auth_worker(args) -> int:
    # One owner per auth file, including launch races and different terminals.
    with codex_auth_lock(".mag-worker.lock", blocking=False) as worker:
        if worker is None:
            return 0
        while True:
            try:
                sync = CodexAuthSync()
                with CodexLocalRPC(on_request=sync.refresh_request) as client:
                    retry_at = 0
                    while True:
                        if time.monotonic() >= retry_at:
                            try:
                                sync.tick(client)
                            except CodexAuthError as exc:
                                codex_sync_report(exc.status)
                                retry_at = time.monotonic() + 5
                        # Service server refresh requests even during retry backoff.
                        client.poll(1)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                # Keep the owner alive during daemon restarts / sleep / transport
                # failure. No credentials or upstream error messages are logged.
                codex_sync_report("reconnecting")
                time.sleep(5)


def print_codex_sync_status():
    request = read_json(codex_sync_state_path(), {})
    status = request.get("status")
    if not status:
        return
    if now() - request.get("checked_at", request.get("at", 0)) > 30:
        text = T("authentication monitor is not responding; retry mag use <alias>",
                 "認証の常駐監視が応答していません。mag use <alias> を再実行してください")
    else:
        texts = {
            "pending": T("authentication application pending; waiting for conversations to finish", "認証の反映待ち・会話の処理終了を待っています"),
            "active": T("login applied without restart; authentication monitor active", "再起動せず認証を反映済み・認証の常駐監視が稼働中"),
            "reconnecting": T("authentication monitor reconnecting; retry mag use <alias> if this persists", "認証の常駐監視が再接続中。続く場合はmag use <alias>を再実行してください"),
            "refresh_failed": T("token renewal failed; checking network and retrying", "認証の更新に失敗。接続を確認しながら再試行中"),
            "storage_failed": T("could not save renewed credentials; check the credential store, then retry mag use <alias>", "更新した認証を保存できません。認証の保管先を確認し、mag use <alias>を再実行してください"),
            "reauthentication_required": T("sign-in needs renewal; run mag login codex <alias>, then mag use <alias>", "再ログインが必要です。mag login codex <alias>の後、mag use <alias>を実行してください"),
            "failed": T("authentication monitor could not start; retry mag use <alias>", "認証の常駐監視を起動できません。mag use <alias>を再実行してください"),
        }
        text = texts.get(status, T("authentication application unconfirmed", "認証の反映を確認できません"))
    print("  Codex: " + text)


def print_codex_switch_note(prov: str, name: str) -> None:
    if prov != "codex":
        return
    note = codex_switch_note(name)
    if note:
        print(note)


def _record_ts(line: str) -> float | None:
    """セッション JSONL の 1 行から時刻を取る。壊れた行は None。"""
    try:
        return parse_iso(json.loads(line).get("timestamp"))
    except (json.JSONDecodeError, AttributeError):
        return None


def codex_foreign_resets(slug: str) -> set:
    """slug 以外の Codex アカウントで観測済みの枠リセット時刻（秒）。

    枠のリセット時刻はアカウントごとに違うので、秒まで一致する記録は同じ
    アカウントのもの。切替前から動いている codex プロセス（Codex Desktop や
    開きっぱなしの TUI）は、切替後に新しいスレッドを作っても起動時の
    アカウントで書き続ける。それを現用アカウントの数値として読まないための鍵。
    自分の記録にもある時刻は除く（過去の誤帰属で他アカウントに残った自分の
    時刻で、自分の記録まで捨てないように）。
    """
    own = {int(w["resets_at"]) for w in (known_limits(slug) or {}).get("windows") or [] if w.get("resets_at")}
    out = set()
    for a in accounts_of("codex"):
        if a["slug"] == slug:
            continue
        for w in (known_limits(a["slug"]) or {}).get("windows") or []:
            if w.get("resets_at") and int(w["resets_at"]) not in own:
                out.add(int(w["resets_at"]))
    return out


def codex_live_limits(max_files: int = 20, only_after: float = None,
                      exclude_resets: set = None) -> dict | None:
    """Codex の利用状況をセッション JSONL から拾う。

    各ターンの rate_limits を ~/.codex/sessions/**/*.jsonl から読む。
    表示用の古い記録は別途 app-server で更新する。ここはローカル記録だけを扱う。

    注意点:
      - limit_id が "codex" 以外（期間限定モデルの Spark 枠など）は別枠なので除外する
      - 記録は「そのセッションを始めたとき」にログインしていたアカウントのもの。
        codex は起動時に読んだ認証を持ち続けるので、切替前に始まったセッションは
        切替後も前のアカウントの残量を書き続ける（切替後 8 分経っても書いていた）。
        切替時刻より後に始まったセッションの記録だけを信用する
      - それでも、切替前から動いているプロセスが切替後に作った新しいスレッドは
        見分けられない。exclude_resets（他アカウントで観測済みのリセット時刻）に
        一致する記録は、そのアカウントの数値なので読まない
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
                started = None
                for line in f:
                    if started is None:
                        # 先頭行の時刻＝セッション開始。切替前に始まっていれば、
                        # 以降の行がいつ書かれたものでも前のアカウントの数値
                        started = _record_ts(line) or 0
                        if only_after and started and started <= only_after:
                            break
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
                    if exclude_resets and any(
                            int((rl.get(k) or {}).get("resets_at") or 0) in exclude_resets
                            for k in ("primary", "secondary")):
                        continue          # 他アカウントの枠＝古いプロセスが書いた別アカウントの数値
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
    """Codex の現用アカウントの、最も逼迫している枠を返す。"""
    cur = get_current("codex")
    lim = codex_live_limits(only_after=only_after,
                            exclude_resets=codex_foreign_resets(cur) if cur else None)
    if not lim or not lim["windows"]:
        return None
    w = max(lim["windows"], key=lambda x: x["pct"])
    return {**w, "plan": lim.get("plan"), "reached": lim.get("reached"), "ts": lim["ts"]}


def codex_rate_limits(auth: dict, timeout: float = 15) -> dict:
    """Read account limits without inference or rotating shared credentials."""
    import tempfile
    tok = auth.get("tokens") or {}
    ident = codex_identity(auth)
    account_id = tok.get("account_id") or ident.get("account_id")
    if not tok.get("access_token") or not account_id:
        return {"error": T("no usable Codex sign-in; log in again",
                           "Codex の認証情報がありません。再ログインしてください")}

    # External-token login keeps the running Codex session's refresh token intact.
    # The temporary home has neither user config/MCP servers nor stored credentials.
    with tempfile.TemporaryDirectory(prefix="mag-codex-limits-") as home:
        proc = None
        reader = None
        try:
            proc = subprocess.Popen(
                [find_codex_bin(), "app-server"],
                env={**os.environ, "CODEX_HOME": home}, cwd=home,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            messages = queue.Queue()
            deadline = time.monotonic() + timeout

            def read_messages():
                try:
                    for line in proc.stdout:
                        try:
                            messages.put(json.loads(line))
                        except ValueError:
                            continue
                finally:
                    messages.put(None)

            reader = threading.Thread(target=read_messages, daemon=True)
            reader.start()

            def rpc(request_id, method, params):
                proc.stdin.write(json.dumps({"id": request_id, "method": method,
                                             "params": params}) + "\n")
                proc.stdin.flush()
                while True:
                    msg = messages.get(timeout=max(0.001, deadline - time.monotonic()))
                    if msg is None:
                        raise OSError("app-server exited")
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("method") == "account/chatgptAuthTokens/refresh":
                        # Never refresh a token behind the running client's back.
                        raise PermissionError()
                    if msg.get("id") == request_id and "method" not in msg:
                        if "error" in msg:
                            raise OSError("app-server request failed")
                        return msg.get("result") or {}
                    if time.monotonic() >= deadline:
                        raise queue.Empty()

            rpc(1, "initialize", {"clientInfo": {"name": "magazine", "version": VERSION},
                                  "capabilities": {"experimentalApi": True}})
            rpc(2, "account/login/start", {
                "type": "chatgptAuthTokens", "accessToken": tok["access_token"],
                "chatgptAccountId": account_id, "chatgptPlanType": ident.get("plan")})
            result = rpc(3, "account/rateLimits/read", {})
            buckets = result.get("rateLimitsByLimitId")
            rl = (buckets.get("codex") or {}) if buckets is not None else result.get("rateLimits") or {}
            if rl.get("limitId") not in (None, "codex"):
                rl = {}
            wins = []
            for key in ("primary", "secondary"):
                window = rl.get(key) or {}
                pct = window.get("usedPercent")
                if pct is None:
                    continue
                wm = window.get("windowDurationMins") or 0
                wins.append({"key": key, "pct": pct, "window_minutes": wm,
                             "resets_at": window.get("resetsAt"),
                             "label": (f"{wm // 60}h window" if wm < 10080 else f"{wm // 1440}d window")
                             if LANG == "en" else (f"{wm // 60}時間枠" if wm < 10080 else f"{wm // 1440}日枠")})
            if not wins:
                raise ValueError("no account-wide usage")
            return {"windows": wins, "ts": now(), "plan": rl.get("planType"),
                    "reached": rl.get("rateLimitReachedType")}
        except PermissionError:
            return {"error": T("Codex sign-in needs renewal; log in again",
                               "Codex の認証を更新できません。再ログインしてください")}
        except (OSError, ValueError, TypeError, AttributeError, queue.Empty):
            return {"error": T("could not read current usage; retry with mag limits --refresh",
                               "現在の残量を取得できませんでした。mag limits --refresh で再試行できます")}
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                if reader is not None:
                    reader.join(timeout=2)
                proc.stdin.close()
                proc.stdout.close()


def codex_probe(slug: str, timeout: int = 15) -> dict | None:
    """Read this account's limits without spending an inference request."""
    auth = codex_stored_auth(slug) or {}
    live = codex_live_auth() or {}
    stored_id = (auth.get("tokens") or {}).get("account_id") or codex_identity(auth).get("account_id")
    live_id = (live.get("tokens") or {}).get("account_id") or codex_identity(live).get("account_id")
    if stored_id and stored_id == live_id:
        if (parse_iso(live.get("last_refresh")) or 0) > (parse_iso(auth.get("last_refresh")) or 0):
            auth = live
    return codex_rate_limits(auth, timeout=timeout)


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


def set_current(provider: str, slug: str, *, manual: bool = False) -> None:
    s = state()
    cur = s.get("current")
    if not isinstance(cur, dict):
        cur = {"claude": cur} if cur else {}
    # Reaffirming the same selection must not discard its active session readings.
    if not manual or cur.get(provider) != slug:
        s.setdefault("last_switch_by", {})[provider] = now()
        s["last_switch"] = now()
    cur[provider] = slug
    s["current"] = cur
    selected = s.setdefault("manual_selection", {})
    if manual:
        selected[provider] = slug
        # A deliberate retry supersedes an earlier (possibly preventive) cooldown.
        s.setdefault("cooldowns", {}).pop(slug, None)
    else:
        selected.pop(provider, None)
    save_state(s)


def manually_selected(slug: str | None, provider: str = "claude") -> bool:
    return bool(slug and get_current(provider) == slug
                and (state().get("manual_selection") or {}).get(provider) == slug)


def usage_threshold(slug: str | None, default: float, provider: str = "claude") -> float:
    """An explicit `mag use` keeps the selected account until it is fully used."""
    return 100.0 if manually_selected(slug, provider) else default


def find_account(slug: str) -> dict | None:
    return next((a for a in accounts() if a["slug"] == slug), None)


# ── メインアカウント ─────────────────────────────────────────────────────
# Artifact・会話履歴・定期実行など、アカウントに紐づいて残るものは、使った
# アカウントごとに散らばる。provider ごとに 1 つ「メイン」を決めておき、
# 予備に移っても使えるようになり次第メインへ戻す。メインの印はアカウントの
# 登録情報に持たせる（改名・削除にそのまま追従させるため）。
def main_slug(provider: str = "claude") -> str | None:
    return next((a["slug"] for a in accounts_of(provider) if a.get("main")), None)


def set_main(slug: str | None, provider: str) -> None:
    accs = accounts()
    for a in accs:
        if provider_of(a) != provider:
            continue
        if a["slug"] == slug:
            a["main"] = True
        else:
            a.pop("main", None)
    save_accounts(accs)


def main_mark(slug: str) -> str:
    return T(" ★ main", " ★ メイン") if (find_account(slug) or {}).get("main") else ""


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


# コマンドの一部として読む語。alias にすると `mag main clear` などと区別できない
RESERVED_ALIASES = ("clear",)


def alias_reserved_error(label: str) -> str | None:
    if (label or "").strip().lower() in RESERVED_ALIASES:
        return T(f"'{label}' is reserved by magazine — pick another alias",
                 f"'{label}' は magazine が予約している語なので alias にできません — 別名にしてください")
    return None


def label_conflict_error(label: str, accs: list, except_slug: str | None = None) -> str | None:
    if err := alias_reserved_error(label):
        return err
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
    # 中継中は本体のログインが常にメインなので、そこから「使っている枠」は分からない
    live = None if relay_enabled() else (current_oauth() or {}).get("refreshToken")
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
    # Claude のサブスクには 5 時間枠がもう無い（usage API の five_hour は null）。
    # どの枠に当たったか分からないときは「不明」として扱い、5 時間後に見直す
    # （週次と決めつけて 7 日間外すより、早めに再確認する方が損が小さい）。
    kind = kind or "unknown"
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
    if (main_slug("claude") if relay_enabled() else get_current("claude")) == slug:
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
        th = usage_threshold(slug, th)
        pct = (p[kind] or {}).get("pct")
        if pct is not None and pct >= th:
            return False, T(f"{kind} {pct:.1f}% ≥ threshold {th:.1f}%", f"{kind} {pct:.1f}% ≥ 閾値{th:.1f}%")
    return True, "ok"


def do_load(slug: str, reason: str = "") -> bool:
    if provider_of(find_account(slug) or {}) == "codex":
        with codex_auth_lock():
            ok = _do_load(slug, reason)
        return codex_apply_selection() if ok else False
    return _do_load(slug, reason)


def _do_load(slug: str, reason: str = "") -> bool:
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
        auth, dead, checked = codex_ensure_fresh(slug, auth, verify=True)
        if dead:
            print(T(f"✗ {name}: {dead} — run `mag login codex {name}` to sign in again "
                    f"(the active account is left as it is)",
                    f"✗ {name}: {dead} — `mag login codex {name}` で入れ直してください"
                    f"（今使っているアカウントはそのままです）"), file=sys.stderr)
            log(f"load refused: {slug}: {dead}")
            return False
        if not checked:
            # 圏外で生死を確かめられなかった。死んでいた場合、装填した瞬間から
            # 新しく始める codex セッションが全部止まる。codex 自体も圏外では
            # 使えないので、待つ方が安い。
            print(T(f"✗ {name}: could not confirm the credential is still valid (network error) — "
                    f"not switching; try again once online (the active account is left as it is)",
                    f"✗ {name}: 認証情報が生きているか確認できません（ネットワークエラー）— "
                    f"切り替えません。接続を確認して再実行してください（今使っているアカウントはそのままです）"),
                  file=sys.stderr)
            log(f"load deferred: {slug}: could not verify (network)")
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
            if relay_enabled():
                # 中継中は本体のログイン（メイン）を動かさない。使う枠だけを切り替え、
                # 次の会話リクエストから中継がこのアカウントの認証で送る。
                exp = oauth.get("expiresAt")
                if exp and (exp / 1000.0) <= now():
                    raise RuntimeError(T("the stored credential has expired; sign in again",
                                         "保管している認証情報が期限切れです。入れ直してください"))
            else:
                install_oauth(oauth)
        except RuntimeError as e:
            print(T(f"✗ failed to activate {name}: {e}", f"✗ {name} への切り替えに失敗: {e}"), file=sys.stderr)
            log(f"load failed: {slug}: {e}")
            return False

    set_current(prov, slug, manual=(reason == "manual"))
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
    order = [slugs[(start + i) % len(slugs)] for i in range(1, len(slugs) + 1)]
    # メインが使えるなら、順番に関係なくメインへ戻す（散らばりを最小にする）
    main = main_slug(provider)
    if main and main != after:
        order.remove(main)
        order.insert(0, main)
    report = []
    for cand in order:
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


def main_ready(provider: str = "claude", probe_main: bool = True) -> tuple[str | None, str]:
    """予備を使っている間に、メインへ戻ってよい状態になったか。

    戻すのは「自動で予備に移った」ときだけ。`mag use` で選んだアカウントは
    本人の意思なので、メインが空いてもそのままにする。戻した直後に上限で
    また離れる往復を避けるため、メインの使用率は監視の切替閾値より下を求める。
    """
    main, cur = main_slug(provider), get_current(provider)
    if not main or not cur or cur == main:
        return None, "not applicable"
    if manually_selected(cur, provider):
        return None, "manual selection"
    if cooldown_left(main) > 0:
        return None, "main is limited"
    p = probe(main) if (probe_main and provider == "claude") else None
    if p is not None:
        if not p.get("ok"):
            return None, p.get("error") or "usage unknown"
        _kind, pct = peak_window(p)
        if pct is not None and pct >= config()["hotswap_threshold"]:
            return None, f"main at {pct:.0f}%"
    ok, why = is_usable(main, p)
    return (main, "ok") if ok else (None, why)


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
            src_cfg = os.path.join(os.path.dirname(CODEX_AUTH_PATH), "config.toml")
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
    if err := alias_reserved_error(alias):
        print(f"✗ {err}", file=sys.stderr)
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


def cmd_main(args) -> int:
    """メインアカウントを指定・表示・解除する（provider ごとに 1 つ）。"""
    clear = getattr(args, "clear", None)
    target = getattr(args, "provider", None)
    if (args.name or "").strip().lower() == "clear":
        clear = target or "all"
    elif target:
        print(T("✗ a provider is only taken after `clear`: `mag main clear [claude|codex]`",
                "✗ claude / codex を付けるのは解除のときだけです: `mag main clear [claude|codex]`"), file=sys.stderr)
        return 1
    if clear:
        provs = ("claude", "codex") if clear == "all" else (clear,)
        for prov in provs:
            if prov == "claude" and (relay_enabled() or relay_settings_installed()):
                if relay_turn_off() != 0:          # 中継はメインが前提。先に止めて設定を戻す
                    return 1
            had = main_slug(prov)
            set_main(None, prov)
            if had or clear != "all":
                print(T(f"✓ {prov}: no main account", f"✓ {prov}: メインアカウントを解除しました"))
        return 0
    if not args.name:
        shown = False
        for prov, title in (("claude", "Claude Code"), ("codex", "Codex / ChatGPT")):
            if not accounts_of(prov):
                continue
            shown = True
            main = main_slug(prov)
            print(f"{title}: " + (describe(find_account(main)) if main
                                  else T("(not set — `mag main ALIAS`)", "(未設定 — `mag main ALIAS` で指定)")))
        if not shown:
            print(T("No accounts registered. Add one with `mag add`.",
                    "アカウントが未登録です。`mag add` で登録してください。"))
            return 1
        return 0
    acct, err = resolve_account(args.name)
    if not acct:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    prov = provider_of(acct)
    set_main(acct["slug"], prov)
    print(T(f"★ main {prov} account: {describe(acct)}", f"★ {prov} のメインアカウント: {describe(acct)}"))
    if prov == "claude" and not IS_WINDOWS:
        # メインを決めたら中継も ON: ログインはメインに固定し、切り替えるのは利用枠だけにする
        if relay_turn_on() != 0:
            print(T("  The main account is set, but switching still replaces the Claude Code login."
                    " Fix the problem above and run `mag relay on`.",
                    "  メインは設定しましたが、切り替えは従来どおり Claude Code のログインを入れ替えます。"
                    "上の問題を直して `mag relay on` を実行してください。"), file=sys.stderr)
            return 1
        print(T("  When a spare's usage is in use, magazine moves back to this account's usage as soon as it is usable"
                " (`mag watch` checks every few minutes; a `mag use` choice is left alone).",
                "  予備の利用枠を使っていても、メインの枠が使えるようになり次第こちらへ戻します"
                "（`mag watch` が数分おきに確認。`mag use` で選んだアカウントはそのまま）。"))
        return 0
    print(T("  When a spare is in use, magazine switches back here as soon as this account is usable again "
            "(`mag watch` checks every few minutes; a `mag use` choice is left alone).",
            "  予備に移っても、ここが使えるようになり次第こちらへ戻します"
            "（`mag watch` が数分おきに確認。`mag use` で選んだアカウントはそのまま）。"))
    if get_current(prov) != acct["slug"]:
        print(T(f"  Active now: {label_of(get_current(prov))}. Switch now with `mag use {display(acct)}`.",
                f"  いま使用中: {label_of(get_current(prov))}。すぐ切り替えるなら `mag use {display(acct)}`。"))
    return 0


def cmd_relay(args) -> int:
    """利用枠だけを予備へ回す中継の on / off / status / serve。"""
    action = args.action or "status"
    if action == "serve":
        if not relay_enabled():
            print(T("relay is off — `mag relay on` first", "中継は無効です。先に `mag relay on`"), file=sys.stderr)
            return 1
        srv = RelayServer(relay_port())
        srv.start()
        print(T(f"relay listening on {relay_url()} (Ctrl-C to stop)", f"中継: {relay_url()} で待ち受け中（Ctrl-C で停止）"))
        try:
            while srv.alive():
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return 0

    if action == "on":
        return relay_turn_on()

    if action == "off":
        return relay_turn_off()

    # status
    on = relay_enabled()
    print(T(f"relay: {'on' if on else 'off'}", f"中継: {'ON' if on else 'OFF'}"))
    if on:
        print(T(f"  login (Artifacts etc.): {label_of(main_slug('claude'))}   usage: {label_of(get_current('claude'))}",
                f"  ログイン（Artifact など）: {label_of(main_slug('claude'))}   利用枠: {label_of(get_current('claude'))}"))
        print(T(f"  listening: {'yes' if relay_listening() else 'NO — Claude Code cannot connect'} ({relay_url()})",
                f"  待ち受け: {'OK' if relay_listening() else 'なし — Claude Code が接続できません'} ({relay_url()})"))
        print(T(f"  Claude Code settings: {'ok' if relay_settings_installed() else 'missing — run `mag relay on`'}",
                f"  Claude Code の設定: {'OK' if relay_settings_installed() else 'なし — `mag relay on` を再実行'}"))
    elif main_slug("claude"):
        print(T("  Switching replaces the Claude Code login. `mag relay on` keeps it on the main account.",
                "  切り替えのたびに Claude Code のログインを入れ替えます。`mag relay on` でメインに固定できます。"))
    return 0


def relay_turn_on() -> int:
    """中継を ON にする（何度呼んでもよい）。`mag main` からも呼ばれる。"""
    if IS_WINDOWS:
        print(T("✗ the relay is not supported on Windows yet", "✗ 中継は Windows ではまだ使えません"), file=sys.stderr)
        return 1
    main = main_slug("claude")
    if not main:
        print(T("✗ choose the main account first: `mag main ALIAS`",
                "✗ 先にメインアカウントを決めてください: `mag main ALIAS`"), file=sys.stderr)
        return 1
    if conflict := relay_settings_conflict():
        print(T(f"✗ another HTTPS proxy is already configured ({conflict}); the relay cannot be chained to it",
                f"✗ 別の HTTPS プロキシが設定済みです（{conflict}）。中継はこれと併用できません"), file=sys.stderr)
        return 1
    try:
        if not relay_certs_ready():
            relay_make_certs()
    except (RuntimeError, OSError) as e:
        print(T(f"✗ could not create the relay certificate: {e}", f"✗ 中継用の証明書を作れません: {e}"), file=sys.stderr)
        return 1
    set_relay_enabled(True)        # 先に立てる: ログインを入れ替えても「使う枠」は動かさない
    err = install_main_login()
    if err:
        set_relay_enabled(False)
        print(T(f"✗ could not sign Claude Code in to the main account: {err}",
                f"✗ Claude Code のログインをメインにできませんでした: {err}"), file=sys.stderr)
        return 1
    try:
        relay_settings_apply(True)
    except (RuntimeError, OSError) as e:
        set_relay_enabled(False)
        print(T(f"✗ could not update Claude Code settings: {e}", f"✗ Claude Code の設定を更新できません: {e}"),
              file=sys.stderr)
        return 1
    print(T(f"✓ relay on: Claude Code stays signed in to {describe(find_account(main))};"
            f" usage is charged to {label_of(get_current('claude'))}",
            f"✓ 中継 ON: Claude Code のログインは {describe(find_account(main))} のまま、"
            f"利用枠は {label_of(get_current('claude'))} から使います"))
    print(T("  Artifacts, Remote Control and everything else stay on the main account.",
            "  Artifact・Remote Control などはすべてメインに残ります。"))
    if watch_daemon_state() != T("running", "稼働中"):
        print(T("  ⚠ the relay runs inside `mag watch`. Start it (or enable the background service);"
                " until then Claude Code cannot connect.",
                "  ⚠ 中継は `mag watch` の中で動きます。起動してください（常駐サービス推奨）。"
                "起動するまで Claude Code は接続できません。"))
    else:
        print(T("  The background monitor starts the relay within about 20 seconds.",
                "  常駐監視が 20 秒ほどで中継を起動します。"))
    print(T("  Restart Claude Code sessions that were already open; they keep using the main account's usage.",
            "  開いていた Claude Code のセッションは再起動してください（再起動まではメインの枠を使います）。"))
    return 0


def relay_turn_off() -> int:
    """中継を OFF にし、Claude Code の設定を元に戻す。"""
    if not relay_enabled() and not relay_settings_installed():
        print(T("relay is already off", "中継はすでに OFF です"))
        return 0
    try:
        relay_settings_apply(False)
    except (RuntimeError, OSError) as e:
        print(T(f"✗ could not update Claude Code settings: {e}", f"✗ Claude Code の設定を更新できません: {e}"),
              file=sys.stderr)
        return 1
    set_relay_enabled(False)
    main = main_slug("claude")
    if main:
        set_current("claude", main)      # 本体のログイン＝メインが、そのまま使う枠になる
    print(T("✓ relay off. Restart open Claude Code sessions so they stop using the relay.",
            "✓ 中継 OFF。開いている Claude Code のセッションは再起動してください。"))
    print(T("  Switching goes back to replacing the Claude Code login. `mag relay on` turns it back on.",
            "  以後の切り替えは、これまで通り Claude Code のログインを入れ替えます。戻すときは `mag relay on`。"))
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
        if prov == "claude" and relay_enabled():
            ok = relay_listening()
            print(T(f"   relay on: signed in as {label_of(main_slug('claude'))} (Artifacts etc.),"
                    f" usage from ▶{'' if ok else '   ⚠ relay not listening'}",
                    f"   中継 ON: ログインは {label_of(main_slug('claude'))}（Artifact など）、利用枠は ▶"
                    f"{'' if ok else '   ⚠ 中継が待ち受けていません'}"))
        rc |= _print_magazine(accs, prov, args)
        if prov == "codex":
            print_codex_sync_status()
        print()
    return rc


def _print_magazine(accs: list, prov: str, args) -> int:
    cur = get_current(prov)
    for a in accs:
        slug = a["slug"]
        mark = "▶" if slug == cur else " "
        line = f"{mark} {display(a)}{main_mark(slug)}  \033[2m{a.get('email') or ''}\033[0m"
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
            # 報告された枠だけ並べる。Claude のサブスクは 5 時間枠を返さなくなった
            # （five_hour が null）ので、無い枠を「?」で並べると誤解を招く。
            rows = [(k, p[key] or {}) for k, key in (("5h", "five_hour"), ("7d", "seven_day"))
                    if (p[key] or {}).get("pct") is not None] or [("7d", {})]
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
    """Display-only snapshot; refresh active accounts when their observation is old."""
    row = {"windows": [], "note": None}
    old = known_limits(slug)
    lim = None
    is_current = get_current("codex") == slug
    if is_current and not refresh:
        since = (state().get("last_switch_by") or {}).get("codex")
        lim = codex_live_limits(only_after=since, exclude_resets=codex_foreign_resets(slug))
    candidates = [v for v in (old, lim) if v and v.get("windows")]
    latest = max(candidates, key=lambda v: v.get("ts") or 0) if candidates else None
    fresh = bool(latest and now() - (latest.get("ts") or 0) < 60
                 and not any(w.get("resets_at") and w["resets_at"] <= now()
                             for w in latest["windows"]))
    attempted = refresh or (is_current and not fresh)
    if attempted:
        lim = codex_probe(slug)
        if lim and lim.get("dead"):
            row["note"] = T("needs re-login (refresh token revoked)", "要再ログイン（refresh token 失効）")
        elif not lim or not lim.get("windows"):
            row["note"] = (lim or {}).get("error") or T("could not read usage", "残量を取得できませんでした")
    elif is_current and fresh:
        lim = latest

    if lim and lim.get("windows") and latest and (latest.get("ts") or 0) > (lim.get("ts") or 0):
        lim = latest
    if lim and not lim.get("dead") and lim.get("windows"):
        row["windows"] = [{"label": w["label"], "pct": w["pct"],
                           "resets_at": w.get("resets_at")} for w in lim["windows"]]
        row["reached"] = lim.get("reached")
        row["observed_ts"] = lim.get("ts") or now()
        record_limits(slug, {"windows": row["windows"], "ts": row["observed_ts"]})
    elif latest:
        row["windows"] = latest["windows"]
        row["stale_ts"] = latest.get("ts")
        if latest is not old:
            record_limits(slug, latest)
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


def is_five_hour_window(w: dict) -> bool:
    """記録済みの枠が Claude の 5 時間枠か（旧記録は kind を持たないので表示名で見る）。"""
    return w.get("kind") == "five_hour" or w.get("label") in ("5-hour", "5時間")


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
               "current": get_current("claude") == slug, "main": bool(a.get("main")),
               "windows": [], "note": None,
               "cooldown": cooldown_left(slug)}
        if p.get("ok"):
            for key, label in (("five_hour", T("5-hour", "5時間")), ("seven_day", T("weekly", "週次"))):
                blk = p.get(key) or {}
                if blk.get("pct") is not None:
                    row["windows"].append({"label": label, "pct": blk["pct"], "kind": key,
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
                # 5 時間枠が廃止される前の記録は、もう存在しない枠の数字なので出さない
                row["windows"] = [w for w in old.get("windows") or [] if not is_five_hour_window(w)]
                row["stale_ts"] = old.get("ts")
        rows.append(row)

    for a in accounts_of("codex"):
        slug = a["slug"]
        is_cur = get_current("codex") == slug
        row = {"provider": "codex", "slug": slug, "label": a.get("label", slug),
               "current": is_cur, "main": bool(a.get("main")), "windows": [], "note": None,
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
            name = f"{r['label']}" + (main_mark(r["slug"]) if r.get("main") else "")
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
    acct, _ = resolve_account(args.alias)
    lock = codex_auth_lock() if acct and provider_of(acct) == "codex" else contextlib.nullcontext()
    with lock:
        return _cmd_load(args)


def _cmd_load(args) -> int:
    acct, err = resolve_account(args.alias)
    if not acct:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    prov = provider_of(acct)
    restart_codex = getattr(args, "restart_codex", False)
    if restart_codex and prov != "codex":
        print(T("✗ --restart-codex requires a Codex account.",
                "✗ --restart-codex は Codex アカウントにのみ指定できます。"), file=sys.stderr)
        return 1
    already_active = get_current(prov) == acct["slug"]
    if prov == "codex" and already_active:
        # State can lag a manual login. Confirm the disk account before
        # treating a selection as already active.
        live = codex_live_auth() or {}
        stored = codex_stored_auth(acct["slug"]) or {}
        live_id = (live.get("tokens") or {}).get("account_id")
        stored_id = (stored.get("tokens") or {}).get("account_id")
        live_at = parse_iso(live.get("last_refresh")) or 0
        stored_at = parse_iso(stored.get("last_refresh")) or 0
        # A fresh `mag login` can renew this same account in the vault while
        # the file still contains the revoked login. Install that newer login.
        already_active = bool(live_id and stored_id and live_id == stored_id and live_at >= stored_at)
    if already_active:
        set_current(prov, acct["slug"], manual=True)
        print(T(f"= already active: {describe(acct)}", f"= すでに使用中: {describe(acct)}"))
        print(T("  Manual selection: automatic switching waits for 100% usage.",
                "  手動選択を優先: 使用率100%になるまで自動切替しません。"))
        if prov == "codex":
            return 0 if codex_apply_selection() else 1
        print_codex_switch_note(prov, display(acct))
        return 0
    if not do_load(acct["slug"], "manual"):
        return 1
    print(T(f"🔁 switched to: {describe(acct)}", f"🔁 切り替え: {describe(acct)}"))
    print(T("  Manual selection: automatic switching waits for 100% usage.",
            "  手動選択を優先: 使用率100%になるまで自動切替しません。"))
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
    # 予備で動いていてメインが空いたら戻す。--no-probe（起動前フックの既定）では
    # メインの残量を確かめられないので、戻すのは常駐監視に任せる。
    if not args.no_probe:
        main, _why = main_ready(prov)
        if main and do_load(main, "return to main"):
            print(T(f"[magazine] back to main: {label_of(main)}",
                    f"[magazine] メインに戻しました: {label_of(main)}"))
            return 0
    if cur and cooldown_left(cur) == 0:
        p = probe(cur) if (not args.no_probe and prov == "claude") else None
        ok, why = is_usable(cur, p)
        if ok:
            if args.verbose:
                kind, pct = peak_window(p)
                shown = f"{WINDOW_LABEL.get(kind, kind)} {pct:.0f}%" if pct is not None else "?"
                print(T(f"[magazine] {label_of(cur)}: {shown} — keeping",
                      f"[magazine] {label_of(cur)}: {shown} — そのまま"))
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
    kind = (state().get("cooldowns") or {}).get(cur, {}).get("kind") or kind
    print(T(f"⛔ marked as limited: {label_of(cur)} ({kind})",
            f"⛔ 上限到達として記録: {label_of(cur)} ({kind})"))
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
    if manually_selected(slug):
        pct = (p.get(kind) or {}).get("pct")
        # Old sessions may still report a different account's limits. For an
        # explicit choice, require this account's own measurement of 100%.
        if not p.get("ok") or pct is None or pct < 100.0:
            log(f"cooldown 見送り: {slug} は手動選択中、実測100%を未確認（{source}）")
            return False
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


def live_peak_pct(max_age: float = 120.0) -> float | None:
    """現用アカウントの直近の使用率（報告された枠のうち最も高いもの。statusLine 由来）。

    以前は 5 時間枠だけを見ていたが、Claude のサブスクは 5 時間枠を返さなくなった。
    5 時間枠だけを見続けると、週次が上限に近づいても監視間隔が縮まらず、
    次のアカウントの事前確認も走らない。
    """
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
        for k in ("five_hour", "seven_day"):
            pct = (d.get(k) or {}).get("pct")
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
      - 凍結・プラン解約など、残量 API には出ない理由で使えないことを先に知る
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


# ── 中継（relay）: 利用枠だけを予備アカウントへ ─────────────────────────
# Keychain は常にメインのまま置き、Claude Code の会話リクエスト
# （POST /v1/messages）の認証だけを、いま枠を使っている予備のものへ差し替える。
# Artifact・Remote Control・プラグインなど、それ以外の通信は本体のログイン
# （＝メイン）のまま通るので、アカウントに残るものはメインに集まる。
#
# 仕組み: Claude Code に HTTPS_PROXY でこの中継を使わせる。api.anthropic.com
# への接続だけは、名前制約付きのローカル CA（api.anthropic.com 以外には使えない）
# で TLS を受けて中身を見る。CA は NODE_EXTRA_CA_CERTS で Claude Code だけに信頼させる。
# 設定の環境変数は Claude Code が起動するコマンドにも引き継がれるので、
# 接続元が Claude Code 本体のときだけ開き、それ以外（curl・Python など）は
# 中身を見ずにトンネルする。ANTHROPIC_BASE_URL を使わないのは、使うと
# Claude Code が Remote Control などを無効にするため（実測）。
RELAY_DIR = os.path.join(ROOT, "relay")
RELAY_HOST = "api.anthropic.com"
RELAY_SWAP_PREFIX = "/v1/messages"
RELAY_NO_PROXY = ("localhost", "127.0.0.1", "::1")


def relay_port() -> int:
    return int(config().get("relay_port") or 47821)


def relay_url() -> str:
    return f"http://127.0.0.1:{relay_port()}"


def relay_state() -> dict:
    return state().get("relay") or {}


def relay_enabled() -> bool:
    return bool(relay_state().get("enabled"))


def relay_ca_path() -> str:
    return os.path.join(RELAY_DIR, "ca.pem")


def _openssl() -> str | None:
    return shutil.which("openssl")


def relay_certs_ready() -> bool:
    return all(os.path.exists(os.path.join(RELAY_DIR, f)) for f in ("ca.pem", "leaf.pem", "leaf.key"))


def relay_make_certs() -> None:
    """api.anthropic.com 専用のローカル CA とサーバー証明書を作る。

    CA には名前制約を付け、api.anthropic.com 以外の証明書には使えないようにする。
    署名が済んだら CA の秘密鍵は消す（この CA で新しい証明書を作れる者を残さない）。
    どちらも 10 年有効。作り直すと動いているセッションが新しい CA を知らないので、
    期限前に作り直すことはしない。
    """
    ssl_bin = _openssl()
    if not ssl_bin:
        raise RuntimeError(T("openssl command not found", "openssl コマンドが見つかりません"))
    os.makedirs(RELAY_DIR, exist_ok=True)
    harden_path(RELAY_DIR, 0o700)
    work = os.path.join(RELAY_DIR, f".new-{os.getpid()}")
    os.makedirs(work, exist_ok=True)
    try:
        def run(*cmd):
            r = subprocess.run([ssl_bin, *cmd], cwd=work, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"openssl {cmd[0]}: {(r.stderr or r.stdout).strip()[-200:]}")

        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
            "-days", "3650", "-subj", "/CN=magazine relay (api.anthropic.com only)",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", f"nameConstraints=critical,permitted;DNS:{RELAY_HOST}")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
            "-subj", f"/CN={RELAY_HOST}")
        with open(os.path.join(work, "leaf.ext"), "w") as f:
            f.write(f"subjectAltName=DNS:{RELAY_HOST}\nbasicConstraints=critical,CA:FALSE\n"
                    "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "leaf.pem", "-days", "3650", "-extfile", "leaf.ext")
        for name in ("leaf.key",):
            harden_path(os.path.join(work, name), 0o600)
        for name in ("ca.pem", "leaf.pem", "leaf.key"):
            os.replace(os.path.join(work, name), os.path.join(RELAY_DIR, name))
    finally:
        shutil.rmtree(work, ignore_errors=True)   # ca.key もここで消える
    log("relay: 証明書を作成しました")


# ── 接続元が Claude Code 本体か ──
def _peer_pid(client_port: int) -> int | None:
    """127.0.0.1:client_port から中継へ繋いできたプロセスの PID。"""
    me = os.getpid()
    if sys.platform.startswith("linux"):
        want = f":{client_port:04X}"
        inode = None
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table) as f:
                    next(f, None)
                    for line in f:
                        cols = line.split()
                        if len(cols) > 9 and cols[1].endswith(want) and cols[3] == "01":
                            inode = cols[9]
                            break
            except OSError:
                continue
            if inode:
                break
        if not inode:
            return None
        target = f"socket:[{inode}]"
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    if os.readlink(os.path.join(fd_dir, fd)) == target:
                        return int(pid)
            except OSError:
                continue
        return None
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP@127.0.0.1:{client_port}", "-sTCP:ESTABLISHED", "-Fp"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit() and int(line[1:]) != me:
            return int(line[1:])
    return None


def _pid_command(pid: int) -> str:
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def is_claude_command(cmd: str) -> bool:
    """コマンドラインが Claude Code 本体か（ネイティブ版・npm 版の両方）。"""
    if not cmd:
        return False
    exe = os.path.basename(cmd.split()[0])
    return (exe == "claude" or "/claude/versions/" in cmd
            or "@anthropic-ai/claude-code" in cmd or "/claude-code/cli.js" in cmd)


def relay_peer_is_claude(client_port: int) -> bool:
    pid = _peer_pid(client_port)
    return bool(pid) and is_claude_command(_pid_command(pid))


# ── 差し替えの判断 ──
class RelayAuth:
    """会話リクエストの認証を、どのトークンに差し替えるか決める。

    差し替えるのは、届いた認証が Keychain のログイン（メイン）そのものの時だけ。
    別のトークン（事前確認用の CLAUDE_CODE_OAUTH_TOKEN など）は、送り主が
    わざわざ選んだものなので触らない。
    """

    LIVE_RECHECK = 1.0

    def __init__(self):
        self._lock = threading.Lock()
        self._live = (None, 0.0)          # (accessToken, 読んだ時刻)
        self._tokens = {}                 # slug -> (accessToken, 期限)
        self._warned = {}

    def _is_live(self, token: str) -> bool:
        with self._lock:
            live, at = self._live
        if token == live:
            return True
        if now() - at < self.LIVE_RECHECK:
            return False
        live = (current_oauth() or {}).get("accessToken")
        with self._lock:
            self._live = (live, now())
        return token == live

    def token_for(self, slug: str) -> str | None:
        with self._lock:
            hit = self._tokens.get(slug)
        if hit and hit[1] - now() > 120:
            return hit[0]
        oauth = stored_oauth(slug)
        if not oauth:
            return None
        oauth = ensure_fresh(slug, oauth)
        exp = (oauth.get("expiresAt") or 0) / 1000.0
        if not oauth.get("accessToken") or (exp and exp <= now()):
            return None
        with self._lock:
            self._tokens[slug] = (oauth["accessToken"], exp or now() + 300)
        return oauth["accessToken"]

    def replacement(self, bearer: str | None) -> str | None:
        """差し替え先のトークン。差し替えないなら None。"""
        if not bearer or not relay_enabled():
            return None
        cur, main = get_current("claude"), main_slug("claude")
        if not cur or not main or cur == main:
            return None
        if not self._is_live(bearer):
            return None
        try:
            tok = self.token_for(cur)
        except Exception as e:   # 予備が使えないときは、止めずにメインのまま通す
            tok, err = None, e
        else:
            err = None
        if not tok:
            if now() - self._warned.get(cur, 0) > 3600:
                self._warned[cur] = now()
                log(f"relay: {cur} の認証を用意できず、メインのまま通しました ({err or 'no token'})")
                notify("🔫 claude-magazine",
                       f"{label_of(cur)} の認証が使えないため、メインの枠で送信しています")
            return None
        return tok


def rewrite_request_head(head: bytes, auth: RelayAuth) -> tuple[bytes, dict, bool]:
    """リクエストの頭（リクエスト行＋ヘッダー）を必要なら書き換える。

    返り値: (送る頭, 小文字ヘッダー辞書, 差し替えたか)
    """
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    method, path = (parts + ["", ""])[:2]
    hdr = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdr[k.strip().lower()] = v.strip()
    swapped = False
    if method == "POST" and path.startswith(RELAY_SWAP_PREFIX):
        bearer = hdr.get("authorization", "")
        bearer = bearer[7:] if bearer.lower().startswith("bearer ") else None
        new = auth.replacement(bearer)
        if new:
            for i, ln in enumerate(lines):
                if i and ln.lower().startswith("authorization:"):
                    lines[i] = f"Authorization: Bearer {new}"
                    swapped = True
            head = "\r\n".join(lines).encode("latin-1")
    return head, hdr, swapped


# ── 中継の配線 ──
def _recv_head(sock, buf: bytes, limit: int = 256 * 1024) -> tuple[bytes | None, bytes]:
    while b"\r\n\r\n" not in buf:
        if len(buf) > limit:
            raise ValueError("request head too large")
        chunk = sock.recv(65536)
        if not chunk:
            return None, buf
        buf += chunk
    i = buf.index(b"\r\n\r\n") + 4
    return buf[:i], buf[i:]


def _pipe(src, dst) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except (OSError, ValueError):
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _send_body_exact(src, dst, buf: bytes, n: int) -> bytes:
    head = buf[:n]
    dst.sendall(head)
    n -= len(head)
    buf = buf[len(head):]
    while n > 0:
        data = src.recv(min(65536, n))
        if not data:
            raise EOFError
        dst.sendall(data)
        n -= len(data)
    return buf


def _send_body_chunked(src, dst, buf: bytes) -> bytes:
    def line():
        nonlocal buf
        while b"\r\n" not in buf:
            data = src.recv(65536)
            if not data:
                raise EOFError
            buf += data
        ln, buf = buf.split(b"\r\n", 1)
        dst.sendall(ln + b"\r\n")
        return ln

    while True:
        size = int(line().split(b";")[0].strip() or b"0", 16)
        if size == 0:
            while line():       # トレーラーを空行まで
                pass
            return buf
        buf = _send_body_exact(src, dst, buf, size + 2)


def relay_forward_requests(client, upstream, auth: RelayAuth, buf: bytes = b"") -> None:
    """クライアント→上流の向きを、リクエスト単位で読みながら流す。

    上流→クライアントは中身を変えないので、別スレッドでそのまま流す。
    こちら向きだけリクエストの区切りを追い、頭だけ書き換える。
    """
    while True:
        head, buf = _recv_head(client, buf)
        if head is None:
            return
        head, hdr, _swapped = rewrite_request_head(head, auth)
        upstream.sendall(head)
        if "upgrade" in hdr.get("connection", "").lower():
            if buf:
                upstream.sendall(buf)
            _pipe(client, upstream)
            return
        if "chunked" in hdr.get("transfer-encoding", "").lower():
            buf = _send_body_chunked(client, upstream, buf)
        elif hdr.get("content-length"):
            buf = _send_body_exact(client, upstream, buf, int(hdr["content-length"]))


class RelayServer:
    def __init__(self, port: int, auth: RelayAuth | None = None):
        self.port = port
        self.auth = auth or RelayAuth()
        self.sock = None
        self.thread = None
        self._server_ctx = None

    def server_ctx(self):
        if self._server_ctx is None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(os.path.join(RELAY_DIR, "leaf.pem"), os.path.join(RELAY_DIR, "leaf.key"))
            ctx.set_alpn_protocols(["http/1.1"])
            self._server_ctx = ctx
        return self._server_ctx

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(128)
        self.sock = s
        self.server_ctx()        # 証明書の不備はここで分かるようにする
        self.thread = threading.Thread(target=self._accept_loop, name="mag-relay", daemon=True)
        self.thread.start()
        log(f"relay: 127.0.0.1:{self.port} で待ち受け開始")

    def stop(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def alive(self) -> bool:
        return bool(self.sock and self.thread and self.thread.is_alive())

    def _accept_loop(self) -> None:
        while self.sock:
            try:
                c, addr = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(c, addr), daemon=True).start()

    def _handle(self, client, addr) -> None:
        try:
            client.settimeout(30)
            head, rest = _recv_head(client, b"")
            if not head:
                client.close()
                return
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            if len(parts) < 2 or parts[0] != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                client.close()
                return
            host, _, port = parts[1].rpartition(":")
            host, port = host.strip("[]"), int(port or 443)
            try:
                upstream = socket.create_connection((host, port), timeout=30)
            except OSError:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                client.close()
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            upstream.settimeout(None)
            if host == RELAY_HOST and port == 443 and relay_enabled() and relay_peer_is_claude(addr[1]):
                self._intercept(client, upstream, rest)
            else:
                if rest:
                    upstream.sendall(rest)
                back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
                back.start()
                _pipe(client, upstream)
                back.join(30)
                for sock_ in (client, upstream):
                    try:
                        sock_.close()
                    except OSError:
                        pass
        except Exception as e:
            log(f"relay: 接続処理エラー: {type(e).__name__}: {e}")
            try:
                client.close()
            except OSError:
                pass

    def _intercept(self, client, upstream_raw, rest: bytes) -> None:
        tls_client = tls_up = None
        try:
            if rest:
                # TLS が始まる前にデータが来ることはない（CONNECT の応答待ちのはず）
                raise ValueError("unexpected bytes before TLS")
            tls_client = self.server_ctx().wrap_socket(client, server_side=True)
            up_ctx = ssl.create_default_context()
            up_ctx.set_alpn_protocols(["http/1.1"])
            tls_up = up_ctx.wrap_socket(upstream_raw, server_hostname=RELAY_HOST)
            threading.Thread(target=_pipe, args=(tls_up, tls_client), daemon=True).start()
            relay_forward_requests(tls_client, tls_up, self.auth)
        except (OSError, EOFError, ValueError, ssl.SSLError):
            pass
        finally:
            for s in (tls_client, tls_up, client, upstream_raw):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


_RELAY_SERVER: RelayServer | None = None
_RELAY_FAIL_AT = 0.0


def relay_supervise() -> None:
    """mag watch の周回ごとに呼ぶ。有効なら中継を動かし、無効なら止める。"""
    global _RELAY_SERVER, _RELAY_FAIL_AT
    want = relay_enabled()
    if want and not (_RELAY_SERVER and _RELAY_SERVER.alive()):
        if now() - _RELAY_FAIL_AT < 60:
            return
        try:
            srv = RelayServer(relay_port())
            srv.start()
            _RELAY_SERVER = srv
        except Exception as e:
            _RELAY_FAIL_AT = now()
            log(f"relay: 起動できません: {e}")
            notify("🔫 claude-magazine", f"中継を起動できません（Claude Code が繋がりません）: {e}")
    elif not want and _RELAY_SERVER:
        _RELAY_SERVER.stop()
        _RELAY_SERVER = None
        log("relay: 停止しました")


def relay_listening(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", relay_port()), timeout=timeout):
            return True
    except OSError:
        return False


# ── Claude Code の設定（settings.json の env） ──
def _claude_settings_path() -> str:
    return os.path.join(claude_config_dir(), "settings.json")


def relay_env_values(existing_env: dict) -> dict:
    """settings.json の env に入れる値。既存の NO_PROXY・追加 CA は残して足す。"""
    no_proxy = [p.strip() for p in str(existing_env.get("NO_PROXY") or os.environ.get("NO_PROXY") or "").split(",")
                if p.strip()]
    for h in RELAY_NO_PROXY:
        if h not in no_proxy:
            no_proxy.append(h)
    ca = relay_ca_path()
    prev_ca = existing_env.get("NODE_EXTRA_CA_CERTS") or os.environ.get("NODE_EXTRA_CA_CERTS")
    if prev_ca and os.path.abspath(prev_ca) not in (ca, os.path.join(RELAY_DIR, "ca-bundle.pem")):
        # 会社の CA などを既に足している場合は、それと合わせた束を作る
        bundle = os.path.join(RELAY_DIR, "ca-bundle.pem")
        with open(prev_ca, encoding="utf-8") as a, open(ca, encoding="utf-8") as b, \
                open(bundle, "w", encoding="utf-8") as out:
            out.write(a.read().rstrip() + "\n" + b.read())
        ca = bundle
    return {"HTTPS_PROXY": relay_url(), "NODE_EXTRA_CA_CERTS": ca, "NO_PROXY": ",".join(no_proxy)}


def relay_settings_conflict() -> str | None:
    """他の HTTPS プロキシが既に使われているなら、その説明を返す。"""
    settings = read_json(_claude_settings_path(), {}) if os.path.exists(_claude_settings_path()) else {}
    env = (settings or {}).get("env") or {}
    for where, src in (("settings.json", env), ("shell", os.environ)):
        for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            val = src.get(key)
            if val and val != relay_url():
                return f"{key}={val} ({where})"
    return None


def relay_settings_apply(enable: bool) -> None:
    path = _claude_settings_path()
    settings = read_json(path, None) if os.path.exists(path) else {}
    if not isinstance(settings, dict):
        raise RuntimeError(T(f"{path} must contain a JSON object", f"{path} は JSON オブジェクトである必要があります"))
    env = dict(settings.get("env") or {})
    rs = relay_state()
    if enable:
        new = relay_env_values(env)
        # 2 回目以降の on では、最初に控えた「元の値」を残す（中継の値で上書きしない）
        saved = (rs.get("saved_env") if relay_settings_installed() and rs.get("saved_env") is not None
                 else {k: env.get(k) for k in new})
        changed = any(env.get(k) != v for k, v in new.items())
        env.update(new)
    else:
        saved = rs.get("saved_env") or {}
        ours = {"HTTPS_PROXY": relay_url()}
        changed = False
        for k in ("HTTPS_PROXY", "NODE_EXTRA_CA_CERTS", "NO_PROXY"):
            if k not in env:
                continue
            if k == "HTTPS_PROXY" and env[k] != ours[k]:
                continue      # 自分が入れた値でなければ触らない
            if k == "NODE_EXTRA_CA_CERTS" and not str(env[k]).startswith(RELAY_DIR):
                continue
            if saved.get(k) is None:
                env.pop(k)
            else:
                env[k] = saved[k]
            changed = True
    if not changed:
        if enable:
            s = state()
            s.setdefault("relay", {}).setdefault("saved_env", saved)
            save_state(s)
        return
    if os.path.exists(path):
        backup = path + f".bak.{time.time_ns()}"
        shutil.copy2(path, backup)
        print(T(f"backup: {backup}", f"バックアップ: {backup}"))
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, settings)
    s = state()
    r = s.setdefault("relay", {})
    if enable:
        r["saved_env"] = saved
    else:
        r.pop("saved_env", None)
    save_state(s)


def relay_settings_installed() -> bool:
    settings = read_json(_claude_settings_path(), {}) if os.path.exists(_claude_settings_path()) else {}
    env = (settings or {}).get("env") or {}
    return env.get("HTTPS_PROXY") == relay_url() and str(env.get("NODE_EXTRA_CA_CERTS") or "").startswith(RELAY_DIR)


def set_relay_enabled(on: bool) -> None:
    s = state()
    s.setdefault("relay", {})["enabled"] = on
    save_state(s)


def install_main_login() -> str | None:
    """Keychain（本体のログイン）をメインにする。失敗理由を返す。"""
    main = main_slug("claude")
    if not main:
        return T("no main account", "メインアカウントが未設定です")
    sync_live_credentials()
    live_rt = (current_oauth() or {}).get("refreshToken")
    stored = stored_oauth(main)
    if not stored:
        return T("no stored credential in keychain", "Keychain に認証情報がありません")
    if live_rt and live_rt == stored.get("refreshToken"):
        return None
    try:
        install_oauth(ensure_fresh(main, stored))
    except RuntimeError as e:
        return str(e)
    log(f"relay: 本体のログインをメイン {main} にしました")
    return None


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
            relay_supervise()     # 中継が有効なら、このプロセスの中で動かし続ける
            clear_expired_cooldowns()
            reconcile_current()   # 手動 login で外から差し替えられていても追従する
            cur = get_current("claude")
            swapped = False

            # 1) statusLine 経由のリアルタイム値（稼働中セッションがある間は最速）
            lv = live_worst()
            if lv and lv["pct"] >= usage_threshold(cur, th) and has_spare(cur):
                label = {"five_hour": "5h", "seven_day": "7d"}[lv["kind"]]
                swapped = hotswap(lv["kind"], f"{label} {lv['pct']:.0f}%", lv.get("resets_at"))

            # 1.5) 次弾の事前ウォームアップ（現用の使用率が閾値を超えたら軽く1回検証）
            #      Keychain の現用スロットには触れないので稼働中セッションに影響しない
            if not swapped and cur:
                peak = live_peak_pct()
                if peak is not None and peak >= cfg["warm_threshold"]:
                    target, _ = next_slug(cur, probe_all=False)
                    if target and target != cur and not already_warmed(target):
                        ok, msg = warm_account(target)
                        mark_warmed(target, ok, msg)
                        label = (find_account(target) or {}).get("label", target)
                        if ok:
                            log(f"warm: {target} OK (peak={peak:.0f}%)")
                        else:
                            log(f"warm: {target} NG: {msg}")
                            notify("🔫 claude-magazine", f"⚠ 次弾 {label} の事前検証に失敗: {msg[:60]}")

            # 1.7) Codex 側の監視。セッション JSONL に書かれた rate_limits を読む。
            #      do_load は共有サーバーへ認証を同期する常駐処理も起動する。
            #      送信済みのリクエストはそのまま完了し、以降のリクエストで新しい認証を使う。
            cur_cx = get_current("codex")
            if cur_cx and accounts_of("codex"):
                since = (state().get("last_switch_by") or {}).get("codex")
                cw = codex_worst(only_after=since)
                if cw and (cw["pct"] >= usage_threshold(cur_cx, th, "codex")
                           or (cw.get("reached") and not manually_selected(cur_cx, "codex"))):
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
                                   + ("（共有サーバーへ認証を同期）"
                                      if codex_session_recently_active() else ""))

            # 2) usage API（低頻度の裏取り。429 ならその時点で上限確定）
            if not swapped and now() - last_api > args.api_interval:
                last_api = now()
                # 2.0) 予備で動いている間にメインが空いたら戻す（Artifact などを散らさない）
                main, _why = main_ready("claude")
                if main and do_load(main, "return to main"):
                    log(f"return to main: {cur} → {main}")
                    notify("🔫 claude-magazine", f"メインの {label_of(main)} に戻しました")
                    cur, swapped = main, True
                # Codex は残量 API を使わず、上限の記録だけで判断する。
                # 戻せなかった（認証切れなど）場合に毎周回やり直さないよう、この間隔で見る。
                cx_main, _why = main_ready("codex", probe_main=False)
                if cx_main and do_load(cx_main, "return to main"):
                    log(f"return to main[codex]: → {cx_main}")
                    notify("🔫 claude-magazine", f"Codex をメインの {label_of(cx_main)} に戻しました")
                if cur and not swapped:
                    p = probe(cur)
                    if not p["ok"]:
                        # 残量が読めないだけでは切り替えない（使用量API側の絞りと区別できないため）。
                        # 実際の上限は statusLine の rate_limits と出力の実ヒット検知で捕まえる。
                        log(f"watch: {cur} の残量取得に失敗 ({p.get('error')}) — 切替はしない")
                    else:
                        for k in ("five_hour", "seven_day"):
                            pct = (p.get(k) or {}).get("pct")
                            if pct is not None and pct >= usage_threshold(cur, th) and has_spare(cur):
                                label = {"five_hour": "5h", "seven_day": "7d"}[k]
                                hotswap(k, f"{label} {pct:.0f}%", (p.get(k) or {}).get("resets_at"))
                                break

            # 上限が近いほど短い間隔で見る（枠を超えて課金域に入らないため）
            pct_now = live_peak_pct() or 0.0
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
    if relay_enabled():
        main = main_slug("claude")
        listening, wired = relay_listening(), relay_settings_installed()
        print(T(f"relay              : on — login {label_of(main)}, usage {label_of(get_current('claude'))}",
                f"中継               : ON — ログイン {label_of(main)}、利用枠 {label_of(get_current('claude'))}"))
        print(T(f"  listening        : {'OK' if listening else 'NO — Claude Code cannot connect (is mag watch running?)'}",
                f"  待ち受け         : {'OK' if listening else 'なし — Claude Code が接続できません（mag watch は動いていますか）'}"))
        print(T(f"  settings env     : {'OK' if wired else 'missing — run `mag relay on`'}",
                f"  設定の env       : {'OK' if wired else 'なし — `mag relay on` を再実行'}"))
        if real and main and real["slug"] != main:
            print(T("  ⚠ Claude Code is not signed in to the main account — run `mag relay on` again",
                    "  ⚠ Claude Code のログインがメインではありません — `mag relay on` を再実行"))
        ok &= listening and wired and bool(real and real["slug"] == main)
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


# ── 更新の確認 ─────────────────────────────────────────────────────────
# どのコマンドでも、前回の確認から 24 時間以上たっていたら裏で最新リリースを見に行く。
# 結果は次の実行で知らせる（待たせない・ネットワークが無くても止まらない）。
UPDATE_REPO = "shirakawayohane/magazine"
UPDATE_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_PATH = os.path.join(ROOT, "update.json")      # state.json とは別（watch と書き込みが競合しない）
UPDATE_INTERVAL = 24 * 3600
# 自動で呼ばれるもの・出力を機械が読むものでは知らせない（確認自体はする）
UPDATE_QUIET_COMMANDS = ("statusline", "watch", "auto", "hit")


def version_tuple(v) -> tuple:
    nums = re.findall(r"\d+", str(v or "").split("-")[0])
    return tuple(int(n) for n in nums[:3]) if nums else ()


def update_check_enabled() -> bool:
    if os.environ.get("MAGAZINE_NO_UPDATE_CHECK"):
        return False
    return bool(config().get("update_check", True))


def update_info() -> dict:
    return read_json(UPDATE_PATH, {}) or {}


def update_check_due(now_ts: float | None = None) -> bool:
    last = update_info().get("checked_at") or 0
    return (now_ts or time.time()) - last >= UPDATE_INTERVAL


def fetch_latest_release() -> None:
    """GitHub の最新リリースを見て update.json に残す（裏プロセスで動く）。"""
    info = update_info()
    info["checked_at"] = time.time()
    try:
        rel = http_json(UPDATE_URL, timeout=10) or {}
        tag = rel.get("tag_name")
        if tag and version_tuple(tag):
            info["latest"] = tag
            info["url"] = rel.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases/tag/{tag}"
        info.pop("error", None)
    except Exception as e:                       # 失敗しても次の 24 時間後にまた見る
        info["error"] = type(e).__name__
    write_json(UPDATE_PATH, info)


def start_update_check() -> None:
    """確認時刻を先に記録してから、裏で取りに行く（同時に何本も走らせない）。"""
    info = update_info()
    info["checked_at"] = time.time()
    write_json(UPDATE_PATH, info)
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_update-check"], **kw)
    except OSError as e:
        log(f"update check: 起動できません: {e}")


def available_update() -> str | None:
    latest = update_info().get("latest")
    if latest and version_tuple(latest) > version_tuple(VERSION):
        return latest
    return None


def update_instructions(tag: str) -> str:
    src = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(src, ".git")):
        installer = "windows\\install.ps1" if IS_WINDOWS else "./install.sh"
        try:
            with open(os.path.join(src, ".git", "HEAD")) as f:
                on_branch = f.read().startswith("ref:")
        except OSError:
            on_branch = False
        if on_branch:                            # 開発版の clone はブランチを進める
            return f"cd {src} && git pull --ff-only && {installer}"
        return f"cd {src} && git fetch --tags && git checkout {tag} && {installer}"
    return f"https://github.com/{UPDATE_REPO}/releases/tag/{tag}"


def maybe_check_update(cmd: str | None) -> None:
    """コマンド実行のついでに更新を確認し、新しい版があれば 1 日 1 回知らせる。"""
    if not update_check_enabled():
        return
    try:
        if update_check_due():
            start_update_check()
        tag = available_update()
        if not tag or cmd in UPDATE_QUIET_COMMANDS or not sys.stderr.isatty():
            return
        info = update_info()
        if time.time() - (info.get("notified_at") or 0) < UPDATE_INTERVAL and info.get("notified_tag") == tag:
            return
        info["notified_at"], info["notified_tag"] = time.time(), tag
        write_json(UPDATE_PATH, info)
        print(T(f"\n⬆ magazine {tag} is available (installed: {VERSION}). Update:\n  {update_instructions(tag)}\n"
                f"  Then restart the background monitor. Turn these checks off with \"update_check\": false in config.json.",
                f"\n⬆ magazine {tag} が出ています（いまは {VERSION}）。更新:\n  {update_instructions(tag)}\n"
                f"  そのあと常駐監視を再起動してください。確認を止めるには config.json に \"update_check\": false。"),
              file=sys.stderr)
    except Exception as e:                       # 更新確認でコマンド本体を失敗させない
        log(f"update check: {type(e).__name__}: {e}")


def main() -> int:
    # Claude Code の中から実行されると、中継用の HTTPS_PROXY を引き継ぐ。
    # mag 自身の通信は中継に頼らない（中継が止まっていても診断できるように）。
    for key in ("HTTPS_PROXY", "https_proxy"):
        if os.environ.get(key) == relay_url():
            os.environ.pop(key)
    if sys.argv[1:] == ["_codex-restart-worker"]:
        return 0  # Retired worker invocation; never restart a provider process.
    if sys.argv[1:] == ["_codex-auth-worker"]:
        return cmd_codex_auth_worker(None)
    if sys.argv[1:] == ["_update-check"]:
        fetch_latest_release()
        return 0
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

    a = sub.add_parser("main", help=T("set the account that Artifacts and other saved work stay on"
                                      " (Claude Code stays signed in to it; only usage switches)",
                                      "Artifact などを集めるメインアカウントを指定"
                                      "（Claude Code のログインはメインのまま、利用枠だけ切り替え）"))
    a.add_argument("name", nargs="?", metavar="ALIAS|clear",
                   help=T("omit to show the current main account(s); `clear` unsets it (Claude: also turns the relay off)",
                          "省略すると現在のメインを表示。`clear` で解除（Claude は中継も OFF）"))
    a.add_argument("provider", nargs="?", choices=["claude", "codex"],
                   help=T("with `clear`: only this provider", "`clear` のとき: この種類だけ解除"))
    a.add_argument("--clear", choices=["claude", "codex"], help=argparse.SUPPRESS)   # 旧書式
    a.set_defaults(func=cmd_main)

    a = sub.add_parser("relay", help=T("status of the relay `mag main` turns on; `off` goes back to swapping logins",
                                       "`mag main` で ON になる中継の状態。`off` で従来のログイン入れ替えに戻す"))
    a.add_argument("action", nargs="?", choices=["on", "off", "status", "serve"])
    a.set_defaults(func=cmd_relay)

    a = sub.add_parser("status", aliases=["st"], help=T("per-account detail","アカウントごとの詳細"))
    a.add_argument("--quick", action="store_true", help="API を叩かず cooldown だけ表示")
    a.set_defaults(func=cmd_status)
    sub.add_parser("list", aliases=["ls"], help=T("list accounts (no usage lookup)","アカウント一覧（残量は見に行かない）")).set_defaults(
        func=cmd_status, quick=True)

    a = sub.add_parser("limits", aliases=["l"], help=T("show usage for every account at once","全アカウントの残量を一覧"))
    a.add_argument("--json", action="store_true", help=T("output JSON (for scripts)","JSON で出力（スクリプト用）"))
    a.add_argument("--refresh", action="store_true",
                   help=T("fetch current usage for all Codex accounts (no inference request)",
                          "全 Codex アカウントの現在の残量を取得（推論リクエストの消費なし）"))
    a.set_defaults(func=cmd_limits)

    a = sub.add_parser("load", aliases=["use"], help=T("switch to an account by alias (unique prefix ok)","alias でアカウントに切り替え（一意な先頭文字列で可）"))
    a.add_argument("--restart-codex", action="store_true",
                   help=argparse.SUPPRESS)  # Compatibility alias; authentication sync is the default.
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
        rc = args.func(args)
    except KeyboardInterrupt:
        return 130
    maybe_check_update(args.cmd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
