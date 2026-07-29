# magazine: claude / codex を起動する前に、使えるアカウントを選んでおくだけのラッパー。
# bash / zsh 用。fish は shell/magazine.fish を使う。
#
# 方針: CLI 本体には干渉しない。起動前に「必要なら切り替える」だけを行い、
# あとは素の claude / codex を exec する。稼働中の切り替えは常駐監視が行う。
#
# 外すときは、読み込んでいる行を .bashrc / .zshrc から消すだけ。

_magazine_py() {
  command python3 "${MAGAZINE_SRC:-$HOME/.local/share/magazine}/mag.py" "$@"
}

claude() {
  local accounts="${MAGAZINE_HOME:-$HOME/.config/magazine}/accounts.json"
  [ -f "$accounts" ] || [ -f "$HOME/.claude-magazine/accounts.json" ] || {
    command claude "$@"; return $?
  }
  case "${1:-}" in
    auth|mcp|plugin|plugins|install|update|upgrade|doctor|setup-token|agents|project|gateway|ultrareview)
      command claude "$@"; return $? ;;
  esac
  _magazine_py auto --no-probe >/dev/null 2>&1
  command claude "$@"
}

codex() {
  local accounts="${MAGAZINE_HOME:-$HOME/.config/magazine}/accounts.json"
  [ -f "$accounts" ] || [ -f "$HOME/.claude-magazine/accounts.json" ] || {
    command codex "$@"; return $?
  }
  case "${1:-}" in
    login|logout|mcp|plugin|update|doctor|completion|app|sandbox|debug)
      command codex "$@"; return $? ;;
  esac
  _magazine_py auto --no-probe --provider codex >/dev/null 2>&1
  command codex "$@"
}
