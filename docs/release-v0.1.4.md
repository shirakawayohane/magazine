# magazine v0.1.4

**Codex now keeps your work on one account too.** `mag main CODEX_ALIAS` keeps Codex
signed in to that account — Remote Control, Cloud tasks and plugins stay there — and
switching moves only whose usage your conversations spend. `mag main clear codex`
puts things back.

- No certificate or proxy is involved. magazine adds one `openai_base_url` line to
  Codex's `config.toml` (backed up first), which sends only the model list and
  conversations to a relay on `127.0.0.1:47822` inside `mag watch`. Everything else
  goes straight to OpenAI. See
  [the guide](https://github.com/shirakawayohane/magazine/blob/v0.1.4/docs/guide.md#codex).
- Only requests carrying the main login's own token are re-signed. A rejected spare
  falls back to the main account for 10 minutes and you are notified.
- After a switch, quiet connections from the previous account are closed so the next
  turn of an open session uses the new account without a restart.
- `mag relay on|off|status` now take an optional `claude` or `codex`.
- Restart the Codex app, open sessions and the shared background server after
  `mag main` so they pick up the setting. macOS and Linux only.

Validated with 237 regression tests and a live macOS check with Codex CLI 0.166.0.
See the [verification notes](https://github.com/shirakawayohane/magazine/blob/v0.1.4/docs/verification.md).

Install:

```sh
git clone --branch v0.1.4 --depth 1 https://github.com/shirakawayohane/magazine.git
cd magazine
./install.sh
```

After updating, restart the background monitor. Then, optionally, `mag main <codex-alias>`.
