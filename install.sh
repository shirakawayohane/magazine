#!/usr/bin/env bash
# magazine installer
#
#   curl -fsSL https://raw.githubusercontent.com/shirakawayohane/magazine/main/install.sh | bash
#
# もしくはクローンしたディレクトリで ./install.sh
set -euo pipefail

REPO_URL="${MAGAZINE_REPO:-https://github.com/shirakawayohane/magazine}"
SRC_DIR="${MAGAZINE_SRC:-$HOME/.local/share/magazine}"
BIN_DIR="$HOME/.local/bin"
PLIST="$HOME/Library/LaunchAgents/com.magazine.watch.plist"

say()  { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM=mac ;;
  Linux)  PLATFORM=linux ;;
  *)      die "未対応の環境です: $OS（Windows は windows\\install.ps1 を使ってください）" ;;
esac
command -v python3 >/dev/null || die "python3 が必要です"

# パイプ実行かクローン実行かを見分ける。パイプならソースを取りに行く。
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/mag.py" ]; then
  SRC_DIR="$HERE"
  say "magazine をインストールします（$SRC_DIR）"
else
  say "magazine をインストールします"
  command -v git >/dev/null || die "git が必要です"
  if [ -d "$SRC_DIR/.git" ]; then
    git -C "$SRC_DIR" pull --ff-only --quiet || warn "更新に失敗しました。既存のものを使います"
    ok "ソースを更新: $SRC_DIR"
  else
    mkdir -p "$(dirname "$SRC_DIR")"
    git clone --depth 1 --quiet "$REPO_URL" "$SRC_DIR" || die "クローンに失敗しました: $REPO_URL"
    ok "ソースを取得: $SRC_DIR"
  fi
fi

[ -f "$SRC_DIR/mag.py" ] || die "mag.py が見つかりません: $SRC_DIR"

mkdir -p "$BIN_DIR"
chmod +x "$SRC_DIR/mag.py"
ln -sf "$SRC_DIR/mag.py" "$BIN_DIR/mag"
ok "コマンド: $BIN_DIR/mag"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR が PATH にありません。シェル設定に追加してください" ;;
esac

# データ置き場を先に作る（既存があればそちらが尊重される）
python3 "$SRC_DIR/mag.py" doctor >/dev/null 2>&1 || true

# ── Claude Code の statusLine 連携 ──────────────────────────────────────
# 走っているセッションの利用率を、追加のAPIコール無しで拾うために使う
if [ -d "$HOME/.claude" ]; then
  if python3 "$SRC_DIR/mag.py" install-statusline >/dev/null 2>&1; then
    ok "Claude Code の statusLine に連携（既存設定はバックアップ済み）"
  else
    warn "statusLine 連携に失敗。あとで 'mag install-statusline' を実行してください"
  fi
fi

# 対話できるか（パイプ実行だと stdin が塞がっている）
if [ -t 0 ]; then ASK=1; else ASK=0; warn "非対話実行なので、任意項目は既定でスキップします"; fi
confirm() { [ "$ASK" = 1 ] || return 1; read -r -p "$1 [y/N] " a; [[ "${a:-N}" =~ ^[Yy]$ ]]; }

# ── シェル連携（任意）────────────────────────────────────────────────────
if confirm "claude / codex の起動前に自動でアカウントを選ぶようにしますか?"; then
  if [ -d "$HOME/.config/fish" ]; then
    mkdir -p "$HOME/.config/fish/conf.d"
    cp "$SRC_DIR/shell/magazine.fish" "$HOME/.config/fish/conf.d/magazine.fish"
    ok "fish: ~/.config/fish/conf.d/magazine.fish"
  fi
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ -f "$rc" ] || continue
    if ! grep -q "magazine.sh" "$rc" 2>/dev/null; then
      printf '\n# magazine\n[ -f "%s/shell/magazine.sh" ] && . "%s/shell/magazine.sh"\n' \
        "$SRC_DIR" "$SRC_DIR" >> "$rc"
      ok "$(basename "$rc") に読み込み行を追加"
    fi
  done
  warn "外すときは追加した行、または conf.d のファイルを消してください"
fi

# ── 常駐監視（任意）──────────────────────────────────────────────────────
if confirm "上限の手前で自動的にアカウントを切り替える常駐監視を入れますか?"; then
  if [ "$PLATFORM" = mac ]; then
    mkdir -p "$(dirname "$PLIST")"
    sed -e "s|__HOME__|$HOME|g" -e "s|__MAG__|$SRC_DIR/mag.py|g" \
        "$SRC_DIR/launchd/com.magazine.watch.plist.template" > "$PLIST"
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    ok "常駐監視を起動（停止: launchctl bootout gui/$(id -u) $PLIST）"
  elif command -v systemctl >/dev/null; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    sed -e "s|__PYTHON__|$(command -v python3)|g" -e "s|__MAG__|$SRC_DIR/mag.py|g" \
        "$SRC_DIR/systemd/magazine-watch.service.template" > "$UNIT_DIR/magazine-watch.service"
    systemctl --user daemon-reload
    systemctl --user enable --now magazine-watch.service
    ok "常駐監視を起動（停止: systemctl --user disable --now magazine-watch）"
  else
    warn "systemd が見つかりません。'mag watch' を手動で起動してください"
  fi
else
  warn "常駐監視なし。'mag watch' で手動起動、または再度 install.sh を実行してください"
fi

echo
say "次の手順"
cat <<'EOS'
  1. 使いたいアカウントにログインして登録する（アカウントの数だけ繰り返す）
       claude auth login   →  mag add --label main
       codex login         →  mag add --provider codex --label codex-main
  2. 残量を一覧する
       mag limits
  3. 状態を確認する
       mag doctor
EOS
