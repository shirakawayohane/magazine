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
- The active Codex login is written to `CODEX_HOME/auth.json` (default: user `.codex`) with mode
  0600 where POSIX permissions apply. This release does not manage Codex keyring-only
  storage.
- Live Codex handover uses external-token authentication over the local Unix socket.
  A persistent magazine process holds access tokens in memory and handles renewal;
  rotated credentials are saved to the same active file and account vault. Its health
  file contains coordination data, not tokens. Keep this monitor running, or renewals
  can fail after the access token expires. No token is passed through command arguments.
- Isolated login and explicit Codex usage refresh create temporary provider profiles.
  These can contain credentials on disk and are cleaned up during normal completion.
  An abrupt process or machine shutdown can leave temporary files behind.

Account names and email addresses are also stored locally in metadata and logs.
They are not appropriate for an unredacted public bug report.

## Relay

Setting a Claude main account (`mag main`, or `mag relay on`) makes magazine an HTTPS proxy for Claude Code on `127.0.0.1`. The relay
creates a local certificate authority under `relay/` in the data directory. It is
name-constrained to `api.anthropic.com`, the CA private key is deleted right after the
server certificate is signed, and the certificate is trusted only through Claude Code's
`NODE_EXTRA_CA_CERTS`. The server key (`relay/leaf.key`) is mode 0600; anyone who can
read it can impersonate `api.anthropic.com` to programs that trust this CA. Only
connections from the Claude Code program are decrypted; the relay replaces the
`Authorization` header of conversation requests and logs no request content.

Setting a Codex main account adds `openai_base_url` to Codex's `config.toml`, pointing
Codex's conversation traffic at a plain-HTTP listener on `127.0.0.1`, which forwards it
to `chatgpt.com` over verified HTTPS. No certificate is involved. Conversation requests
carrying the main account's own access token (the one in `auth.json`) may be re-sent with
a spare account's access token and account ID; a request without that token is forwarded
unchanged, so a local process cannot borrow a spare without the main login. Nothing else
is changed and no request content is logged.

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
