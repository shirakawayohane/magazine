# User guide

[Back to magazine](../README.md)

## Installation and updates

Python 3.10+ and Git are prerequisites. The installer does not install either provider CLI.
The release instructions in the README pin the source to a tag. Keep that clone in place:
the installed command points to it. Moving or deleting the clone breaks that command.

For the current development version instead of a tagged release:

```sh
git clone https://github.com/shirakawayohane/magazine.git
cd magazine
./install.sh
```

On Windows, run `powershell -ExecutionPolicy Bypass -File windows\install.ps1` in the clone.

The optional remote installers fetch the development branch, not a pinned release:

```sh
curl -fsSL https://raw.githubusercontent.com/shirakawayohane/magazine/main/install.sh | bash
```

```powershell
irm https://raw.githubusercontent.com/shirakawayohane/magazine/main/windows/install.ps1 | iex
```

The remote installation stores the source in the user's `.local/share/magazine`
directory. `MAGAZINE_SRC` overrides that destination for remote installation.
On macOS/Linux the command is a symlink at `.local/bin/mag`; Windows gets `.local/bin/mag.cmd`.

When piped into Bash, the installer skips optional shell integration and background
monitoring. Run the local installer interactively to enable those options. Windows
prints a separate command for installing its Scheduled Task; it does not enable it automatically.

To update a development clone, use `git pull --ff-only` and run its installer again.
For a release clone, fetch tags and check out the release you want before reinstalling.
Restart an existing monitor after an update so it loads the new code.

Once a day, the first `mag` command you run asks GitHub in the background whether a
newer release exists (nothing else is sent). The next command in a terminal then
prints the update command once a day. Set `"update_check": false` in `config.json`, or
`MAGAZINE_NO_UPDATE_CHECK=1`, to turn this off.

## Codex setup

Magazine uses the file-based Codex credential store and session directory under
`CODEX_HOME` (default: `.codex` in the user's home directory). It does not implement
keyring-only authentication or API-key account rotation.

OpenAI supports several [credential storage modes](https://learn.chatgpt.com/docs/auth#credential-storage).
If your environment permits file storage, select this in your Codex configuration before
using `codex login` and `mag add codex-main --provider codex`:

```toml
cli_auth_credentials_store = "file"
```

Do not override an organization's authentication requirements. If you need keyring-only
storage or a managed login that does not expose this file, this magazine release is not
compatible with that setup.

Running Codex processes can retain the old login even after the credential file
changes. Magazine now applies account changes to the shared server without
restarting it or reopening conversations:

```sh
mag use codex-main
```

The command starts a background authentication monitor and returns without waiting
for ongoing work. `mag next --provider codex`, `mag auto`, and `mag watch` use the
same path. Selecting the current alias again repairs a stopped monitor. Further
selections replace the saved login; the monitor always applies the latest file.
The monitor waits for loaded conversations to finish, including turns awaiting
approval, before applying a different login. Account handover can change Codex's
application network permissions, so it is not applied in the middle of active work.
While waiting, renewal requests for the older running account use its existing
vault slot without overwriting the newly selected file. Failed turns can be repaired
without reopening the conversation.

The idle check and handover are not atomic: a new turn starting immediately between
them can still be affected by Codex's permission update. This does not guarantee
uninterrupted inference. Detached background commands are not represented by the
conversation-status check.

**Keep the authentication monitor running.** Codex's external-token mode makes
magazine responsible for renewal. The monitor renews tokens five minutes before
expiry, handles Codex's renewal requests, saves rotated credentials in the normal
file and account vault, and reconnects after connection loss. The monitor is
separate from `mag watch`; disabling usage-based rotation does not stop token renewal.

`mag status` reports pending application, active monitoring, reconnection, or a
renewal problem. If the monitor has stopped or you reboot, run `mag use codex-main`
again. Temporary renewal failures are retried. If credentials have been revoked,
run `mag login codex codex-main`, then `mag use codex-main`. Stopping the monitor
while the shared server remains in external-token mode can cause authentication
errors once its current access token expires.

This integration uses Codex's experimental `chatgptAuthTokens` protocol over its
Unix control socket and was tested with Codex **0.162.1** on macOS. Compatible Unix
shared servers are required. If no socket exists, magazine does not start one;
new sessions use the saved file normally. Separate standalone, IDE, and Desktop
servers are not updated and may still require their own restart. Older Codex
versions and Windows live handover have not been verified. Magazine does not
fall back to automatically restarting an unsupported server.

## Account management

`mag add <name>` saves the currently signed-in Claude account. Add `--provider codex`
for Codex. Names are unique across providers; use names such as `main`, `spare`, and
`codex-main`.

`mag login claude spare` or `mag login codex codex-spare` runs the provider's login in
a temporary profile and saves the result. An existing name can refresh that same
account; it cannot be reassigned to a different account. Registration does not switch
the active login. `mag use <name>` performs the switch.

`mag next` rotates Claude accounts in registration order. `mag next --provider codex`
does the same for Codex. It skips accounts in recorded cooldown; Claude usage checks
can also exclude unavailable or exhausted candidates. Unknown usage is not proof
that an account is exhausted.

If credentials expire, sign in again through `mag login <provider> <name>`, or sign in
with the provider CLI and run `mag update <name>`. magazine refuses to overwrite an
account when the verified identity does not match.

`mag remove <name>` removes the saved account and its stored credentials. It does not
log the provider CLI out of an already active session.

### Main account

Some work stays with the account that created it — Claude Code Artifacts, for example,
are owned by the account that was signed in when they were published. Rotating through
spares scatters that work. Mark the account where it should live:

```sh
mag main main            # one main account per provider
mag main codex-main
mag main                 # show the current main accounts
mag main clear           # unset (Claude: also turns the relay off)
mag main clear codex     # unset only one provider
```

For Claude, `mag main` also turns on the [relay](#relay-switch-only-the-usage): Claude Code
stays signed in to the main account and switching moves only the usage. Without the
relay (Windows, `mag relay off`), switching replaces the Claude Code login as before.

The main account is marked with ★ in `mag status` and `mag limits`. When magazine
picks the next account (`mag next`, the monitor, the pre-launch check), a usable main
account comes first. While a spare is in use, `mag watch` switches back to the main
account as soon as it is usable again, checked on its usage-API interval (default
5 minutes). For Claude the main account's own usage must be below `hotswap_threshold`;
for Codex its recorded cooldown must have ended. `mag auto` returns to the main account
only when it may check usage (not with `--no-probe`). A spare you picked with
`mag use` is left alone until it runs out.

Artifacts are published with the same login as the conversation, so one made while
a spare is active still belongs to that spare. Returning to the main account quickly
keeps that window short; it does not move Artifacts that already exist. This applies
only when the relay is off.

### Relay: switch only the usage

```sh
mag main main         # sets the main account and turns the relay on
mag main codex-main   # the same for Codex
mag relay status
mag relay off         # back to replacing the login on every switch (both, or `off codex`)
mag relay on          # turn it back on for the current main accounts
```

With the relay on, Claude Code is always signed in to the main account. Only its
conversation requests (`POST /v1/messages`) are sent with the credential of the account
whose usage is being spent (▶ in `mag status`). Artifacts, Remote Control, plugins,
and every other request keep the main account's login. Switching (`mag use`, `mag next`,
the monitor) changes only that paying account, and takes effect on the next request
without touching the Claude Code login.

How it works:

- Turning the relay on (`mag main` or `mag relay on`) adds `HTTPS_PROXY`, `NODE_EXTRA_CA_CERTS`, and `NO_PROXY` to the `env`
  of Claude Code's `settings.json` (backed up first), and signs Claude Code in to the
  main account. `mag relay off` restores the previous values. Restart open sessions
  after either.
- The relay listens on `127.0.0.1:47821` (`relay_port` in `config.json`) inside
  `mag watch`, so the monitor must be running. If it is not, Claude Code cannot connect
  and retries, and HTTPS commands Claude Code runs (`git push`, `gh`, `curl`) fail too,
  because they inherit the proxy setting. Restarting the monitor leaves a gap of about
  a second. `mag main clear` (or `mag relay off`) puts things back.
- To see and rewrite requests to `api.anthropic.com`, the relay uses a local certificate
  authority created with `openssl`. It is name-constrained to `api.anthropic.com`, its
  private key is deleted after signing, and only Claude Code is told to trust it.
- The environment is inherited by commands Claude Code runs. The relay opens only
  connections made by the Claude Code program itself; other programs (curl, Python,
  git…) are tunnelled unchanged, so their certificate checks are unaffected.
- A request is rewritten only when it carries the main account's own login. A
  credential the caller chose (for example the spare pre-check) is left alone. If the
  paying account's credential cannot be used, the request goes out on the main account
  and you are notified.
- It cannot be combined with another HTTPS proxy, and is not available on Windows yet.

Pointing Claude Code at the relay with `ANTHROPIC_BASE_URL` instead was tested and
rejected: Claude Code then turns off Remote Control and some tools.

#### Codex

Codex needs no certificate or proxy. `mag main CODEX_ALIAS` adds one line to the top of
Codex's `config.toml` (backed up first):

```toml
openai_base_url = "http://127.0.0.1:47822/backend-api/codex"
```

That moves only Codex's conversation traffic (the model list and responses) to the
relay, which forwards it to `chatgpt.com` over HTTPS. Remote control, Cloud tasks,
plugins and the account page keep going straight to OpenAI with the main login, so
`auth.json` always holds the main account. When a spare's usage is being spent, the
relay sends conversation requests with that spare's credential and account ID.

- Codex reads `config.toml` when it starts. Restart the Codex app, open sessions and the
  shared background server (`codex app-server`) after turning the relay on or off.
- Codex keeps one connection open for a conversation. After a switch, the relay closes
  connections made for the previous account once they have been quiet for 3 seconds;
  Codex reconnects and the next turn uses the new account.
- If the spare is rejected (401/403), the relay uses the main account for 10 minutes and
  notifies you.
- The relay listens on `127.0.0.1:47822` (`codex_relay_port`). If it is not running, new
  Codex conversations cannot connect. `mag main clear codex` removes the line again.
- Not available on Windows, or when `openai_base_url` / `OPENAI_BASE_URL` is already set.

## Usage data and network access

| Operation | Data source / side effects |
| --- | --- |
| `mag limits`, `mag status` | Claude usage endpoint; can refresh stored inactive credentials. Codex uses recent observations and fetches current limits for the active account when missing, at least 60 seconds old, or past reset |
| `mag limits --refresh` | Fetches current limits for all Codex accounts through Codex app-server, including the active account; no inference request |
| Claude status-line hook | Reads the JSON Claude Code provides; the hook itself makes no additional API request |
| `mag watch` | Reads status-line and Codex session records, periodically checks Claude usage, and can pre-check a spare Claude account with a small model request; Codex switches also start the separate authentication monitor |
| `mag use`, `mag next`, `mag auto` | Can refresh expired stored credentials; `next` and `auto` can check Claude usage unless `--no-probe` is used |
| `mag add`, `mag login`, `mag update` | Provider authentication/profile operations; `login` opens the provider's own login flow |
| Any command, once a day | Asks the GitHub releases API for the latest magazine release in the background (`update_check`) |

`--no-probe` skips usage probing. It does not promise zero network traffic if activating
an account requires refreshing its credentials. No magazine-operated server or analytics
collector is used. Requests go to the relevant provider, or through that provider's CLI.

Codex usage is read through the installed CLI's `account/rateLimits/read` API in an
isolated temporary profile. It needs a CLI supporting external ChatGPT tokens and a
valid access token. This lookup does not run a model, rotate refresh tokens, or change
the running Codex session. If authentication needs renewal, the display explains it;
other lookup failures remain usage errors, not proof that the account is unusable.
Inactive accounts show their saved observations until `mag limits --refresh` succeeds.

Both `mag status` and `mag limits` reuse the last saved Codex usage when no newer
reading is available, including for inactive accounts and just after switching back.
They label it “last seen” / “current usage unknown”; the percentage is not reset to
zero when its recorded reset time passes. Observation timestamps come from the Codex
record or successful API lookup, not the time you display it. Older snapshots saved by previous magazine versions
retain their original timestamps, which may represent when magazine read the record.
An explicit `mag limits --refresh` failure keeps the previous value alongside the error.
`mag status --quick` still skips usage reads.

Usage snapshots share the existing account-specific `state.json` storage. Switching
away also saves available usage before replacing the active credentials, provided the
active account matches magazine's tracked account. This uses the same lookup as the
display, including a current-limits request when needed. The local reader costs one directory traversal and metadata
lookup per session file, sorting that list, then reading at most the 20 most recently
modified JSONL files. Inactive accounts only read their saved snapshots. Historical
snapshots are for display, not evidence of current availability or an automatic-switch
decision. The existing session reader filters records by switch time; it does not prove
account ownership for concurrent Codex processes left running across a switch.

The Claude usage endpoint and credential storage details can change independently of
magazine. A usage endpoint returning HTTP 429 is treated as unavailable information,
not proof that inference is blocked. Model-specific weekly usage is displayed separately
from the account-wide window.

## Automatic switching

Run `mag watch` in a separate terminal, or enable the installer-provided background service.
The monitor switches accounts when reported usage reaches the threshold. It does not
prevent all rate-limit interruptions or enforce a billing cap; usage may change between checks.

An explicit `mag use <name>` keeps that account selected until an account-wide
usage window reaches **100%**, even if the configured switching threshold is lower.
This applies to both background monitoring and the pre-launch check, and survives
monitor restarts. Selecting an account also clears its previous cooldown; using
the same name again reaffirms the choice. For Claude, a status-line limit must be
confirmed against that account's usage before it can override a manual selection.
After switching to another account, the usual thresholds apply again.

Configuration is read from `config.json` in the magazine data directory:

| Key | Default | Meaning |
| --- | --- | --- |
| `hotswap_threshold` | `97.0` | Reported usage percentage at which the monitor considers switching |
| `five_hour_threshold` | `99.5` | Five-hour usage threshold when selecting candidates with probing; Claude subscriptions no longer report a five-hour window, so it applies only when one is reported |
| `seven_day_threshold` | `99.5` | Weekly usage threshold when selecting candidates with probing |
| `warm_threshold` | `50.0` | Claude usage (highest reported window) that triggers a spare-account pre-check |
| `warm_model` | `claude-haiku-4-5-20251001` | Model used for that pre-check; availability depends on the provider |
| `min_switch_interval` | `20` | Minimum interval in seconds for the Claude hot-swap guard |
| `poll_schedule` | `[[90, 3], [80, 8], [0, 20]]` | Usage threshold / seconds between local checks |
| `update_check` | `true` | Check GitHub for a newer release once a day |
| `relay_port` | `47821` | Claude relay port on `127.0.0.1` |
| `codex_relay_port` | `47822` | Codex relay port on `127.0.0.1` |

For example, `{"hotswap_threshold": 95.0, "warm_threshold": 101.0}` switches earlier
and prevents the usual 0–100% usage signal from triggering a model pre-check.
This does not disable other provider requests.

CLI options include `mag watch --threshold 95 --api-interval 300`.
Stop a foreground monitor with Ctrl-C.

## Session recovery

`mag stalled` scans the last 24 hours of local Claude Code transcripts for recognized
limit or login-error messages. `--hours 72` extends the window. It is a heuristic over
recorded text: it does not detect every possible failure, and a recorded error is not
proof the session is still unusable.

`mag resume <session-id>` searches the last 72 hours, selects an account, changes to
the recorded working directory if it still exists, and runs `claude --resume` with
the session ID. `--dry-run` previews the command; it can still change the current
directory inside its short-lived process, but does not activate an account or launch Claude.

For specialized Workflow recovery, magazine looks for a script under the session's
`workflows/scripts/` directory with a `-wf_<run-id>.js` suffix and a corresponding
`subagents/workflows/<run-id>/journal.jsonl`. It supplies a continuation instruction
using `scriptPath` and `resumeFromRunId`. **The original Workflow runtime must support
those parameters and replay semantics.** magazine does not supply that runtime or
implement agent-result caching. Use `--no-workflow` for plain Claude session resume.
There is no general token-savings guarantee.

## Data locations

The default data directory is the user's `.config/magazine`. An existing
`.claude-magazine` directory takes precedence for compatibility. `MAGAZINE_HOME`
overrides both. `accounts.json`, `state.json`, `config.json`, `live/`, `logs/`, and
file-backed `secrets/` live there. `mag doctor` prints the log path in use.

See [SECURITY.md](../SECURITY.md) for credentials and temporary login files.

## Uninstall

First remove the saved accounts with `mag remove <name>` while the command is installed.
This also removes their magazine credential entries; deleting the source alone does
not remove Keychain entries. It does not sign out the provider's active account.

Stop and remove any monitor you enabled:

- macOS: `launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/com.magazine.watch.plist"`, then remove that plist.
- Linux: `systemctl --user disable --now magazine-watch`, remove the user `magazine-watch.service` file, then run `systemctl --user daemon-reload`.
- Windows PowerShell: `Stop-ScheduledTask -TaskName magazine-watch`, then `Unregister-ScheduledTask -TaskName magazine-watch`.

Remove magazine's source line from your Bash/Zsh configuration, or remove
`.config/fish/conf.d/magazine.fish` if installed. Restore the previous `statusLine`
entry and hook script from their `.bak.*` files, preserving any newer unrelated settings;
if no previous entry existed, remove magazine's `statusLine` entry and generated hook.

You can then remove the installed command, the source clone, and the magazine data
directory you identified above. Review these exact locations before deleting anything.
Your provider CLI configuration and active login should remain in place.
