# magazine v0.1.0

The first tagged release of magazine: a small Python CLI for managing your own
Claude Code and Codex subscription accounts.

- View usage across accounts and switch by name.
- Optionally select another account as reported usage approaches a threshold.
- Find Claude Code sessions interrupted by limit/login messages and reopen them.
- Follow a shorter setup guide and a reproducible, offline 40-second demo.

Release preparation also fixed first-run Claude status-line setup and settings backups,
shell integration paths, macOS installer expansion, and Windows UTF-8 file handling.

The 83-test suite, offline walkthrough, and clone installation checks run on macOS,
Ubuntu, and Windows with Python 3.10 and 3.13. POSIX/Fish checks are skipped where unavailable.
Live provider login, switching during inference, and Workflow replay were not revalidated
end to end for this release.

Codex uses the default file credential store and needs a new launch after switching.
This is an early release; read the [setup guide](https://github.com/shirakawayohane/magazine/blob/v0.1.0/README.md)
and [verification notes](https://github.com/shirakawayohane/magazine/blob/v0.1.0/docs/verification.md)
before using it with your accounts.

MIT licensed. Independent of Anthropic and OpenAI.
