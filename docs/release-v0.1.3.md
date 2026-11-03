# magazine v0.1.3

**Keep your work on one account.** `mag main ALIAS` picks the account that Artifacts,
Remote Control, and other saved work belong to. For Claude Code, magazine then keeps
Claude Code signed in to that account and moves only *whose usage is spent* when it
switches. `mag main clear` puts things back the way they were.

- `mag main` / `mag main ALIAS` / `mag main clear [claude|codex]`. The main account is
  marked ★ in `mag status` and `mag limits`, comes first in rotation, and the monitor
  returns to it once it is usable again. `clear` is now reserved and cannot be an alias.
- The relay runs inside `mag watch` on `127.0.0.1`. It adds `HTTPS_PROXY` and
  `NODE_EXTRA_CA_CERTS` to Claude Code's settings (backed up first) and uses a local
  certificate limited to `api.anthropic.com`. Only conversation requests from the
  Claude Code program are re-signed; everything else passes through untouched.
  See [the guide](https://github.com/shirakawayohane/magazine/blob/v0.1.3/docs/guide.md#relay-switch-only-the-usage)
  and [SECURITY.md](https://github.com/shirakawayohane/magazine/blob/v0.1.3/SECURITY.md#relay).
- Claude subscriptions no longer report a five-hour window. Status, limits, and
  automatic switching now use the windows that are actually reported (weekly).
- magazine now checks for a newer release once a day, in the background, and the
  next command in a terminal shows how to update. `"update_check": false` in
  `config.json` turns it off.
- Codex switches hand the new account to a running shared Codex server without
  restarting it (Unix). See the verification notes for what was and was not tested.

Nothing changes for existing installations until you run `mag main`.
macOS and Linux only for the relay; Windows keeps replacing the login on each switch.

Validated with 215 regression tests and the offline CLI walkthrough, plus a live macOS
check through Claude Code 2.1.317. See the
[verification notes](https://github.com/shirakawayohane/magazine/blob/v0.1.3/docs/verification.md).

Install:

```sh
git clone --branch v0.1.3 --depth 1 https://github.com/shirakawayohane/magazine.git
cd magazine
./install.sh
```

After updating an existing installation, restart its background monitor so it
loads the new code. Then, optionally, `mag main <alias>`.
