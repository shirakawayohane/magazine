# claude-magazine: claude を弾倉ラッパー経由で起動する
# 無効化したいときは `command claude ...` か、この関数ファイルを削除するだけ。
function claude --description 'claude (magazine auto-reload)'
    set -l mag $HOME/.claude-magazine/mag.py

    # 弾倉が未設定なら何もせず素通し
    if not test -f $HOME/.claude-magazine/accounts.json
        command claude $argv
        return $status
    end

    # 管理系サブコマンドは弾倉ロジックを一切挟まず素通しする。
    # 特に `claude auth login` はブラウザ認証を伴い、PTY 監視や自動装填が邪魔になる。
    if contains -- "$argv[1]" auth mcp plugin plugins install update upgrade doctor setup-token agents project gateway ultrareview
        command claude $argv
        return $status
    end

    # 非対話（-p / パイプ / リダイレクト）は PTY 監視をかけず、起動前の自動装填だけ行う
    if not isatty stdout; or contains -- -p $argv; or contains -- --print $argv
        python3 $mag auto >/dev/null 2>&1
        command claude $argv
        return $status
    end

    python3 $mag run -- $argv
    return $status
end

# codex (ChatGPT) 側も同じ弾倉で回す
function codex --description 'codex (magazine auto-reload)'
    set -l mag $HOME/.claude-magazine/mag.py

    if not test -f $HOME/.claude-magazine/accounts.json
        command codex $argv
        return $status
    end

    # 認証・管理系は素通し（codex login はブラウザ認証を伴う）
    if contains -- "$argv[1]" login logout mcp plugin update doctor completion app sandbox debug
        command codex $argv
        return $status
    end

    # 非対話（exec / パイプ）は監視をかけない
    if not isatty stdout; or contains -- exec $argv
        command codex $argv
        return $status
    end

    python3 $mag crun -- $argv
    return $status
end
