# magazine: claude / codex を起動する前に、使えるアカウントを選んでおくだけのラッパー。
#
# 方針: CLI 本体には干渉しない。
#   起動前に「必要なら切り替える」だけを行い、あとは素の claude / codex を exec する。
#   稼働中の切り替えは常駐監視（mag watch）が Keychain を書き換えて行うので、
#   端末を横取りする必要はない。TUI を擬似端末で包むと、キーボードプロトコルの
#   ネゴシエーションや改行の扱いが壊れる。
#
# 無効化したいときは `command claude ...` か、このファイルを削除するだけ。

function claude --description 'claude (magazine: pick an account first)'
    set -l mag $HOME/.claude-magazine/mag.py

    if not test -f $HOME/.claude-magazine/accounts.json
        command claude $argv
        return $status
    end

    # 認証・管理系は素通し（claude auth login はブラウザ認証を伴う）
    if contains -- "$argv[1]" auth mcp plugin plugins install update upgrade doctor setup-token agents project gateway ultrareview
        command claude $argv
        return $status
    end

    # ネットワークを叩かずローカルの状態だけで判断する（起動を遅らせない）
    python3 $mag auto --no-probe >/dev/null 2>&1
    command claude $argv
    return $status
end

function codex --description 'codex (magazine: pick an account first)'
    set -l mag $HOME/.claude-magazine/mag.py

    if not test -f $HOME/.claude-magazine/accounts.json
        command codex $argv
        return $status
    end

    if contains -- "$argv[1]" login logout mcp plugin update doctor completion app sandbox debug
        command codex $argv
        return $status
    end

    python3 $mag auto --no-probe --provider codex >/dev/null 2>&1
    command codex $argv
    return $status
end
