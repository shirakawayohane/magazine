# Compatibility and verification

Verification date: **2026-11-03**. Release: **v0.1.3** (early release).

## v0.1.3

### Main account and relay

`mag main ALIAS` marks the account that Artifacts and other saved work belong to.
For Claude it also turns on a local relay: Claude Code stays signed in to the main
account, and switching changes only which account's usage conversation requests spend.
`mag main clear` turns it off again. Claude subscriptions no longer report a five-hour
window (`five_hour: null`); magazine now switches on the windows that are reported.

- All 216 regression tests passed locally on macOS with Python 3.14 and with the
  monitor's Python 3.9.6, together with the offline CLI walkthrough, Python syntax
  check, shell syntax check, and diff whitespace check.
- Relay tests cover which requests are re-signed (`POST /v1/messages*` only, and only
  with the main login's token), keep-alive, chunked and upgraded connections, plain
  tunnels for other hosts, recognition of the Claude Code process, settings backup and
  restore, an existing `NODE_EXTRA_CA_CERTS`, proxy conflicts, and a single refresh when
  several requests need a renewed spare token at once.
- On a real macOS installation with **Claude Code 2.1.317**, a new session through the
  relay listed the main account's Artifacts, kept Remote Control available, and reported
  the spare account's weekly usage in the status line. A Python request to
  `api.anthropic.com` started from that session was tunneled without a certificate error.
- Linux uses `/proc` to recognise the Claude Code process; this was covered by tests but
  not on a live Linux installation. The relay is not available on Windows.

### Update check

Any command starts a background request to the GitHub releases API when the last
check is at least 24 hours old; the next terminal command prints the update command
at most once a day. Automatic commands (status line, monitor, pre-launch, hooks) and
non-terminal output stay quiet. Tests cover the daily limit, version ordering, quiet
commands, opting out, and failed requests. On macOS the real API returned the latest
tag, and a terminal run printed the notice once and then stayed quiet.

### Codex account handover without restarting

Codex switches now start a persistent authentication host that applies external
ChatGPT tokens to the shared server after active conversations finish. It does not
restart the server or reopen threads. Manual selection, next-account selection,
and automatic rotation use this path. The host handles renewal, keeps the selected
credential file and vault current, and reconnects after transport loss.

- All **171 regression tests** passed locally, together with the offline CLI
  walkthrough, Python syntax check, shell syntax check, and diff whitespace check.
- Auth tests cover application and verification, active-turn deferral, failed-turn
  recovery, approval waits, unknown/changing/paginated thread inventories, local
  WebSocket framing, interleaved server refresh requests, rotation persistence,
  transient and revoked credentials, vault failures, same-alias login renewal,
  concurrent selection locks, logout, API-key refusal, and credential-safe errors.
- An isolated **Codex 0.162.1** Unix server with synthetic accounts A/B/C retained
  A after the file changed to B. The real magazine host then applied B and C while
  preserving the same server process, observer connection, and loaded thread ID.
  A synthetic local OAuth endpoint verified proactive renewal and refresh-token
  persistence. The host also recovered after the isolated server was restarted.
  All isolated processes were stopped afterward. No real account was switched.

Busy transitions and server-requested renewal were tested with synthetic protocol
responses. Continuation of real-account inference across handover was not tested.
An attempted local Responses fixture with fake credentials failed with Codex's
application-network permission error; it does not establish uninterrupted inference.
The idle check and handover are not atomic, so newly arriving turns or detached
background work can still be affected by permission changes. Separate IDE/Desktop
servers, Linux live handover, and Windows live handover were not verified. The
authentication host must remain running for renewal; `mag status` exposes stale
monitoring and renewal failures. No restart fallback is used.

## v0.1.2

Manual account selections now wait for 100% usage before automatic switching.
Selecting an account clears its earlier cooldown, including when it is already active.
Claude status-line reports must be confirmed by that account's own usage measurement
before they can override the selection. Codex usage lookup now uses the installed
CLI's account limits API without an inference request or refresh-token rotation.

- All 134 regression tests passed locally on macOS with Python 3.14.5. The 11 manual
  selection tests also passed under the existing monitor's Python 3.9.6; the documented
  installation requirement remains Python 3.10+.
- Regression cases cover both accounts at 98%, the 99.9%/100% boundary in both usage
  windows, stale or unavailable usage, reselecting the current account, monitor restarts,
  independent Claude/Codex selections, and returning to normal automatic rotation.
- Codex limits tests substitute the app-server process and cover account-wide versus
  model-specific limits, zero usage, timeout/exit failures, authentication renewal,
  matching credentials, and reuse of fresh or previously observed usage.
- The offline CLI walkthrough, Python syntax check, and Bash syntax checks passed.
- On a real macOS installation, selecting a Claude account cleared its old cooldown.
  Both pre-launch checks retained it; the usage check reported 13% five-hour usage.
  After restarting the monitor, a subsequent heartbeat retained the manual selection
  with a 100% threshold and no cooldown. No inference request was needed for this check.

The 100% boundary is verified with synthetic usage, not by consuming a real account's
remaining quota. Live Codex login/inference and adoption by already-running provider
sessions were not revalidated for this release. See the exact release commit in
[GitHub Actions](https://github.com/shirakawayohane/magazine/actions/workflows/test.yml)
for the macOS, Ubuntu, and Windows checks with Python 3.10 and 3.13.

Earlier release evidence follows.

## v0.1.1

This release fixes three Codex credential and usage-attribution defects found on a real
two-account setup on 2026-09-16 (macOS, Codex CLI **0.154.0**, default file credential store):

- A credential that could not be verified because of a network error was installed anyway,
  which put a revoked login into the live slot. Now the switch is refused instead.
- Syncing the live credential back to its owner could overwrite a newer `mag login` result
  with the revoked live copy, so signing in again never recovered. The newer copy now wins.
- Usage written by a Codex process that started before a switch (including new threads in
  an already-running Codex Desktop) was shown as the active account's usage, so two accounts
  displayed identical numbers. Those records are now excluded.

Observed on the real accounts after the fix: manual switch to the other account, a new
`codex exec` request succeeding on the selected account, `mag login` for the revoked
account, and `mag limits --refresh` reporting distinct usage for each account. The Python
suite has 112 tests. Everything below was recorded for v0.1.0 and is otherwise unchanged.

## What was checked

| Check | Evidence / scope |
| --- | --- |
| Python regression suite | 83 tests discovered; passed locally on macOS with Python 3.14.5. Includes usage interpretation, credential preservation, account selection, and first-run status-line setup |
| Real OS runners | macOS, Ubuntu, and Windows, each with Python 3.10 and 3.13, run the suite and offline demo in [GitHub Actions](https://github.com/shirakawayohane/magazine/actions/workflows/test.yml) |
| Installer from a clone | CI installs the actual command and runs its help on each OS. No provider account is needed; optional persistent monitoring is not enabled in this check |
| Bash and Fish integration | Local isolated command stubs verify normal launch, provider selection, and login passthrough. Fish is skipped where it is not installed; POSIX shell checks are skipped on Windows |
| New and existing Claude settings | Regression tests check new settings creation, preservation of unrelated fields, exact settings backup, repeated setup, and rejection of malformed settings without overwriting them |
| Offline walkthrough | Real command parser and file-backed registration/switching, supplied provider usage responses, local transcript discovery, and dry-run resume. No live AI session or OS Keychain/ACL validation |
| Demo artifact | Five scenes, 40 seconds, 1200 × 760. Captured from the implementation; rendering rejects clipped text. The static preview was visually inspected |

The initial CI run exposed a macOS Bash variable-expansion failure in the installer
and Windows non-UTF-8 log-writing and PowerShell 5.1 script-parsing failures. These were
fixed and rechecked in CI. The suite deliberately leaves Windows' default text locale
in place so that adding `PYTHONUTF8=1` cannot hide application encoding problems.

The GitHub test badge and linked Actions runs are the current source of truth for
the tested commit. Tests run without real provider accounts and do not establish
compatibility with every CLI version.

## Provider versions and live behavior

The release-preparation machine has Claude Code **2.1.267** and Codex CLI **0.153.4**.
Only their version commands were checked for this release; those numbers are not a
claim of fresh end-to-end login, hot-switch, or inference verification.

Existing code comments record Claude credential naming/locking observations against
**2.1.260**. No saved, reproducible live-run evidence is included for the old README's
token-savings claim, so that claim has been removed.

Live provider login, switching during active inference, background-service installation,
and specialized Workflow result replay have **not been revalidated end to end for this
release**. A real-account check should record the provider version, OS, credential mode,
switch trigger, observed account, and whether the next request/resume succeeds, without
publishing credentials or transcripts.

## Known limitations

- Codex requires ChatGPT subscription credentials in its file store (default or
  `CODEX_HOME`); keyring-only storage is unsupported. Shared servers can retain
  the old account across CLI restarts; the unreleased authentication host above
  addresses live handover on compatible Unix shared servers.
- Claude switching depends on its credential cache and storage format. magazine does
  not guarantee that a running process will adopt a change instantly.
- Usage is observed data, not a spending cap. It can be stale, unavailable, or change
  between checks. Optional spare-account pre-checks consume a small amount of usage.
- The Claude write-lock helper can continue after a lock failure. Concurrent credential
  writes are therefore not guaranteed to be serialized in every failure mode.
- Specialized Workflow continuation depends on an external runtime and its exact
  script/journal layout. Plain Claude session resume does not require that runtime.
- `mag doctor` still performs Claude checks on Codex-only installs, so a nonzero result
  there is not necessarily a Codex installation failure.
- Some installer and diagnostic text remains Japanese. Windows ACL failures are not
  always surfaced; inspect credential access in your environment.
- Windows installation has been checked in the ASCII-path GitHub runner environment;
  non-ASCII installation paths have not been validated.

## Reproduce

```sh
python3 -m unittest discover -s tests -v
python3 scripts/demo.py
```

For shell changes, also run `bash -n install.sh shell/magazine.sh` and, if available,
`fish -n shell/magazine.fish`. Full platform checks are defined in the repository's
GitHub Actions workflow.
