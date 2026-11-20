# magazine v0.1.5

This release improves relay recovery and makes unexpected stops easier to diagnose.

- On macOS and Linux, Magazine runs the Claude and Codex relays under launchd or systemd. The service restarts after a crash and checks that the relay is listening before redirecting either client.
- The log shown by `mag doctor` records which operation stopped a relay, where setup or repair failed, and whether the service was unregistered.
- Turning off the Claude relay carries the selected account over to the direct Claude Code login. The shell wrapper clears the inherited Magazine proxy address when starting Claude Code so an old setting cannot point a new session at a stopped relay.
- Concurrent state updates preserve account selections and usage records. A failed automatic switch no longer reports success.

When upgrading a relay from v0.1.4, stop the old `mag watch` first to free the relay ports. Run `mag relay on` after updating to register the service, then start the usage monitor and reopen the Claude Code and Codex sessions that use the relay.

See the [user guide](https://github.com/shirakawayohane/magazine/blob/v0.1.5/docs/guide.md#relay-switch-only-the-usage) and [verification notes](https://github.com/shirakawayohane/magazine/blob/v0.1.5/docs/verification.md).
