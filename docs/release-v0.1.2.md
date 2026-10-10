# magazine v0.1.2

`mag use <name>` now keeps your selected account until usage reaches **100%**.
Previously, the background monitor could immediately switch it away at the default
97% threshold, even when both accounts had the same usage.

- Manual selection applies to both background monitoring and pre-launch checks,
  persists across monitor restarts, and clears the selected account's old cooldown.
  Selecting the already-active account reaffirms the choice.
- Stale Claude status-line reports cannot override a manual choice without a 100%
  measurement from that account. Unavailable usage does not cancel the selection.
- Normal automatic rotation still uses the configured thresholds after switching
  to another account.
- Codex usage refresh now reads the installed CLI's account limits API without
  spending an inference request or rotating refresh tokens. Fresh observations are
  reused; failed lookups retain the last observed usage and explain the failure.

Validated with 134 regression tests and the offline CLI walkthrough. The manual
selection fix was also checked on a live macOS installation, including pre-launch
checks and a monitor restart. The 100% boundary was tested with synthetic usage.
See the [verification notes](https://github.com/shirakawayohane/magazine/blob/v0.1.2/docs/verification.md).

Install:

```sh
git clone --branch v0.1.2 --depth 1 https://github.com/shirakawayohane/magazine.git
cd magazine
./install.sh
```

After updating an existing installation, restart its background monitor so it
loads the new code, then run `mag use <name>` to record your preferred account.
