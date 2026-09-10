# Compatibility and verification

Verification date: **2026-09-10**. Release: **v0.1.0** (early release).

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
and a Windows non-UTF-8 log-writing failure during credential switching. Both were
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

- Codex requires ChatGPT subscription credentials in the default file store. Custom
  `CODEX_HOME` and keyring-only storage are unsupported. Start Codex again after switching.
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
