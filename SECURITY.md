# Security and credentials

magazine handles login credentials. Treat its data directory, provider credential
files, temporary login profiles, and their backups as sensitive.

## Where credentials are stored

- On macOS with Keychain support, saved Claude accounts use service `claude-magazine`
  and saved Codex accounts use `claude-magazine-codex`, one entry per internal account ID.
- Without Keychain support, saved credentials are JSON files in `secrets/` under the
  magazine data directory. The code sets the directory to mode 0700 and files to 0600.
- On Windows the code additionally attempts to restrict access with `icacls`.
  ACL-setting failures are not currently surfaced reliably; verify access restrictions
  in your environment. These files are not encrypted by magazine.
- The active Claude login uses Claude Code's Keychain entry on macOS, or the configured
  Claude credentials file on other platforms. Only the `claudeAiOauth` portion is
  replaced; unrelated OAuth entries are preserved.
- The active Codex login is written to the default user `.codex/auth.json` with mode
  0600 where POSIX permissions apply. This release does not manage Codex keyring-only
  storage or custom `CODEX_HOME` locations.
- Isolated login and explicit Codex usage refresh create temporary provider profiles.
  These can contain credentials on disk and are cleaned up during normal completion.
  An abrupt process or machine shutdown can leave temporary files behind.

Account names and email addresses are also stored locally in metadata and logs.
They are not appropriate for an unredacted public bug report.

## Communications

The Python implementation calls Anthropic usage/profile and OAuth endpoints and OpenAI's
OAuth endpoint. Provider CLIs perform login and optional small usage/pre-check requests.
See the [operation-by-operation explanation](docs/guide.md#usage-data-and-network-access).
There is no magazine-operated server or telemetry endpoint.

The implementation depends on provider credential and journal formats. It has not had
an independent security audit. Local credential switching is not session isolation:
processes sharing the provider's active credential store can be affected together.

## Reporting a vulnerability

Use [GitHub's private vulnerability report](https://github.com/shirakawayohane/magazine/security/advisories/new)
for credential exposure or another security issue. Do not open a public Issue containing
tokens, credential files, session transcripts, or personal data.

Include the release/commit, OS, provider CLI version, expected behavior, and a minimal
reproduction using synthetic data. If a real token was exposed, revoke or rotate it
through the relevant provider before sharing further details.

The project is maintained on a best-effort basis; there is no response-time guarantee.
