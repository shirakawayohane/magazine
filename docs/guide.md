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

## Codex setup

magazine v0.1.0 uses the default user `.codex/auth.json` and `.codex/sessions` locations.
It does not implement custom `CODEX_HOME`, keyring-only authentication, or API-key account rotation.

OpenAI supports several [credential storage modes](https://learn.chatgpt.com/docs/auth#credential-storage).
If your environment permits file storage, select this in your Codex configuration before
using `codex login` and `mag add codex-main --provider codex`:

```toml
cli_auth_credentials_store = "file"
```

Do not override an organization's authentication requirements. If you need keyring-only
storage or a managed login that does not expose this file, this magazine release is not
compatible with that setup.

After switching, start Codex again. magazine writes the selected login; it does not
restart or inject credentials into an already running Codex process.

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

## Usage data and network access

| Operation | Data source / side effects |
| --- | --- |
| `mag limits`, `mag status` | Claude usage endpoint; can refresh stored inactive credentials. Codex normally reads local session records or previously observed usage |
| `mag limits --refresh` | Also probes inactive Codex accounts by making a small Codex request in a temporary profile; consumes some usage |
| Claude status-line hook | Reads the JSON Claude Code provides; the hook itself makes no additional API request |
| `mag watch` | Reads status-line and Codex session records, periodically checks Claude usage, and can pre-check a spare Claude account with a small model request |
| `mag use`, `mag next`, `mag auto` | Can refresh expired stored credentials; `next` and `auto` can check Claude usage unless `--no-probe` is used |
| `mag add`, `mag login`, `mag update` | Provider authentication/profile operations; `login` opens the provider's own login flow |

`--no-probe` skips usage probing. It does not promise zero network traffic if activating
an account requires refreshing its credentials. No magazine-operated server or analytics
collector is used. Requests go to the relevant provider, or through that provider's CLI.

Codex usage depends on recorded events. An account can show “never observed” until it
has been used, or until an explicit refresh succeeds. Previously observed values are
marked with a timestamp and can be stale.

The Claude usage endpoint and credential storage details can change independently of
magazine. A usage endpoint returning HTTP 429 is treated as unavailable information,
not proof that inference is blocked. Model-specific weekly usage is displayed separately
from the account-wide window.

## Automatic switching

Run `mag watch` in a separate terminal, or enable the installer-provided background service.
The monitor switches accounts when reported usage reaches the threshold. It does not
prevent all rate-limit interruptions or enforce a billing cap; usage may change between checks.

Configuration is read from `config.json` in the magazine data directory:

| Key | Default | Meaning |
| --- | --- | --- |
| `hotswap_threshold` | `97.0` | Reported usage percentage at which the monitor considers switching |
| `five_hour_threshold` | `99.5` | Five-hour usage threshold when selecting candidates with probing |
| `seven_day_threshold` | `99.5` | Weekly usage threshold when selecting candidates with probing |
| `warm_threshold` | `50.0` | Claude five-hour usage that triggers a spare-account pre-check |
| `warm_model` | `claude-haiku-4-5-20251001` | Model used for that pre-check; availability depends on the provider |
| `min_switch_interval` | `20` | Minimum interval in seconds for the Claude hot-swap guard |
| `poll_schedule` | `[[90, 3], [80, 8], [0, 20]]` | Usage threshold / seconds between local checks |

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
