#!/usr/bin/env bash
# magazine installer
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$HOME/.claude-magazine"
BIN_DIR="$HOME/.local/bin"
PLIST="$HOME/Library/LaunchAgents/com.magazine.watch.plist"

say()  { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

[ "$(uname)" = "Darwin" ] || { echo "macOS only (uses the login keychain)." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }

say "magazine をインストールします"

mkdir -p "$DATA_DIR"/{logs,live} "$BIN_DIR"
ok "データディレクトリ: $DATA_DIR"

ln -sf "$REPO_DIR/mag.py" "$BIN_DIR/mag"
chmod +x "$REPO_DIR/mag.py"
ok "コマンド: $BIN_DIR/mag"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR が PATH にありません。シェル設定に追加してください" ;;
esac

# ── Claude Code の statusLine 連携 ──────────────────────────────────────
# 稼働中セッションの利用率を追加のAPIコール無しで拾うために使う
if [ -d "$HOME/.claude" ]; then
  if python3 "$REPO_DIR/mag.py" install-statusline >/dev/null 2>&1; then
    ok "Claude Code の statusLine に連携（既存設定はバックアップ済み）"
  else
    warn "statusLine の連携に失敗しました。あとで 'mag install-statusline' を実行してください"
  fi
fi

# ── シェルラッパー（任意）────────────────────────────────────────────────
FISH_DIR="$HOME/.config/fish/conf.d"
if [ -d "$HOME/.config/fish" ]; then
  read -r -p "claude / codex コマンドを自動切替ラッパー経由にしますか? [y/N] " a
  if [[ "${a:-N}" =~ ^[Yy]$ ]]; then
    mkdir -p "$FISH_DIR"
    cp "$REPO_DIR/shell/magazine.fish" "$FISH_DIR/magazine.fish"
    ok "fish: $FISH_DIR/magazine.fish（外したいときはこのファイルを消すだけ）"
  else
    warn "スキップしました。'mag run -- claude' / 'mag crun -- codex' で個別に使えます"
  fi
fi

# ── 常駐監視（任意）──────────────────────────────────────────────────────
read -r -p "上限の手前で自動的に弾を送る常駐監視を入れますか? [y/N] " b
if [[ "${b:-N}" =~ ^[Yy]$ ]]; then
  mkdir -p "$(dirname "$PLIST")"
  sed -e "s|__HOME__|$HOME|g" -e "s|__MAG__|$REPO_DIR/mag.py|g" \
      "$REPO_DIR/launchd/com.magazine.watch.plist.template" > "$PLIST"
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  ok "常駐監視を起動しました（停止: launchctl bootout gui/$(id -u) $PLIST）"
else
  warn "スキップしました。'mag watch' で手動起動できます"
fi

echo
say "次の手順"
cat <<'EOS'
  1. 使いたいアカウントにログインして登録する（アカウントの数だけ繰り返す）
       claude auth login          →  mag add --label main
       codex login                →  mag add --provider codex --label codex-main
  2. 残量を一覧する
       mag limits
  3. 動作確認
       mag doctor
EOS
