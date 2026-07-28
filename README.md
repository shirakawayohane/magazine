# magazine

Keep coding when your AI subscription hits its limit.

`magazine` holds several **Claude Code** and **Codex (ChatGPT)** subscriptions in one clip and
chambers the next one when the current account runs dry — without killing the session you are in.

```
$ mag limits

🔵 Claude Code
 ▶ main
      5時間         ████████░░  85.0%   reset 07/27 02:39 (残り30m)
      週次          ███░░░░░░░  32.0%   reset 08/02 11:59 (残り153h50m)
   sub
      5時間         ░░░░░░░░░░   0.0%   reset 07/27 05:00 (残り2h50m)
      週次          █████████░  91.0%   reset 07/27 05:00 (残り2h50m)
      週次/Fable    ██████████ 100.0%   reset 07/27 05:00 (残り2h50m)

🟢 Codex / ChatGPT
 ▶ codex-main
      7日枠         ███████░░░  74.0%   reset 08/02 16:41 (残り158h32m)
```

## Why another one of these?

There are already good account switchers. Before adding one more, here is where this one
actually differs — and where it does not.

|                                   | magazine | [cux] | [clauth] | [claude-swap] | [teamclaude] | [claude-account-switcher] | [codex-rotator] |
| --------------------------------- | :------: | :---: | :------: | :-----------: | :----------: | :-----------------------: | :-------------: |
| Claude Code                       |    ✅    |  ✅   |    ✅    |      ✅       |      ✅      |            ✅             |       —         |
| Codex (ChatGPT)                   |    ✅    |   —   |    —     |       —       |      —       |            ✅             |      ✅         |
| **Both, from one CLI**            |  **✅**  |   —   |    —     |       —       |      —       |       GUI menu bar        |       —         |
| **Resumes interrupted workflows** |  **✅**  |   —   |    —     |       —       |      —       |             —             |       —         |
| **Finds sessions already dead**   |  **✅**  |   —   |    —     |       —       |      —       |             —             |       —         |
| **Reads usage of inactive accts** |  **✅**  |   —   |    —     |       —       |      —       |            ✅             |       —         |
| No proxy, no wrapper, no restart  |    ✅    | wraps |    ✅    |      ✅       |   proxy      |            ✅             |      ✅         |

If you only use Claude Code, [cux] and [clauth] are mature and you should look at them first.
If you want a menu-bar GUI covering both, use [claude-account-switcher].

**Use `magazine` if you live in the terminal, run *both* Claude Code and Codex, and lose real
work when a limit lands mid-task.**

[cux]: https://github.com/inulute/cux
[clauth]: https://github.com/uwuclxdy/clauth
[claude-swap]: https://github.com/realiti4/claude-swap
[teamclaude]: https://github.com/KarpelesLab/teamclaude
[claude-account-switcher]: https://github.com/Symbioose/claude-account-switcher
[codex-rotator]: https://github.com/PhanTrongGiap/codex-rotator

## The two things it does that others don't

### 1. It gives you back the work a limit interrupted

When a limit lands in the middle of a multi-agent `Workflow`, the completed agents are not lost —
they are on disk in `journal.jsonl`, keyed by a hash of each agent's prompt. `magazine` finds the
interrupted run and resumes it by `runId`, so finished agents replay from cache instead of running again.

Measured on a real interrupted run:

| | agents | output tokens |
| --- | ---: | ---: |
| first run | 3 | 51,847 |
| resumed (+1 new agent) | 4 | **17,282** |

Only the new agent actually ran. The three completed ones replayed from cache.

```console
$ mag stalled
上限などで止まっているセッション: 2 件

1. 58f72cbd-3be9-45f5-bd7d-349ea5f11ef3
   cwd    : ~/src/my-project
   停止   : hit your session limit   最終 2026-07-26 19:13
   ⚙ workflow: design-pass  runId=wf_af56b4bf-9a1  → 完了 11 エージェント分はキャッシュ再利用
   再開   : mag resume 58f72cbd

$ mag resume 58f72cbd     # swaps in a live account, resumes, continues the workflow
```

### 2. Swapping does not restart your session, and does not touch the CLI

Claude Code re-reads the keychain on each request. `magazine` rewrites only the
`claudeAiOauth` slot, and every session already running — including background workflows —
continues on the new account. Nothing is killed, nothing is resumed, nothing is lost.

That is the whole mechanism, and it is deliberately **outside** the CLI:

- `mag watch` is a daemon. It never attaches to your terminal or to any `claude` process.
- Usage comes from the **statusLine hook** — a documented extension point — so reading it
  costs no extra API calls and requires no scraping.
- The shell wrapper only runs `mag auto --no-probe` (~0.09s, no network) before handing off
  to the real binary with `exec`. Your terminal talks to `claude` directly, as it always did.

Nothing sits between your keyboard and the CLI. That matters more than it sounds: wrapping a
TUI in a pty means brokering terminal capability negotiation and keyboard protocols, and
getting any of it subtly wrong breaks arrow keys and multi-line input. `magazine` does not
take that risk on the default path.

> Codex swaps take effect from the next `codex` start, since `~/.codex/auth.json` is read at
> startup. The daemon still tracks Codex usage (from its session journals) and swaps ahead of
> the limit, so the next start already has a fresh account.

This tool does one thing: switch accounts and show you what is left. It does not wrap, watch,
restart, or otherwise manage your CLI sessions — an earlier version tried to, and brokering
terminal capability negotiation on the CLI's behalf broke arrow keys and multi-line input in
ways that kept resurfacing. That approach is gone.

## Correctness notes

Rate-limit data is easy to read wrong. These are mistakes `magazine` makes a point of not making —
each one was found by testing against live accounts, and each one costs you a usable account if you get it wrong.

- **A 429 from the usage endpoint does not mean the account is out.**
  `/api/oauth/usage` is itself rate-limited and returns 429 while inference still works fine.
  Treating that as "account exhausted" benches a perfectly good account. `magazine` only trusts
  inference-side signals (statusLine `rate_limits`, real limit messages) to declare an account dry.
- **A model-scoped weekly limit is not an account-wide limit.**
  `weekly_scoped` at 100% for one model (e.g. Fable) leaves every other model usable.
  Folding it into the account total retires an account that still has ~9% of its real weekly quota left.
- **`claude auth status` caches its profile.** After a keychain-level swap it keeps reporting the
  previous account. `magazine` identifies the live account by matching the refresh token, not by asking the CLI.
- **Refresh tokens rotate.** A credential snapshot taken at registration dies as soon as the CLI
  refreshes that account. `magazine` re-syncs the stored copy whenever it sees the account live,
  and flags a dead one as `要再ログイン` instead of silently chambering a round that 401s.

## Install

Requires macOS (keychain), Python 3.9+, and `claude` and/or `codex` on your `PATH`.

```sh
git clone https://github.com/shirakawayohane/magazine
cd magazine
./install.sh
```

This puts `mag` on your `PATH`, wires the Claude Code statusLine hook (so live usage is
readable without extra API calls), and optionally installs the `watch` daemon and shell wrappers.

## Usage

```sh
# register the account you are logged into right now
claude auth login          # then:
mag add --label main

codex login                # then:
mag add --provider codex --label codex-main

# look at everything at once
mag limits
mag limits --json          # for scripts / status bars

# move around
mag next                   # advance the clip
mag use sub                # chamber a specific one (partial names work)
mag status                 # per-account detail

# nothing to do — with the daemon running, just use claude / codex normally.
# the shell wrapper only picks an account before handing off to the real binary.

# recover what a limit interrupted
mag stalled
mag resume <session-id>

# background daemon: swap before you hit the wall
mag watch
mag doctor                 # check the install
```

### How rotation picks the next account

Accounts advance in registration order (a clip, not a load balancer), so usage concentrates on
one account at a time and your weekly consumption stays legible. Rotation is per-provider:
Claude accounts cycle among Claude accounts, Codex among Codex.

An account is skipped when it is in cooldown (a real limit was observed), or when its credential
is dead. Reaching a usage threshold advances the clip but does **not** bench the account — its
remaining quota is still there next time around.

## Configuration

`~/.claude-magazine/config.json` — every field is optional.

| key | default | meaning |
| --- | --- | --- |
| `hotswap_threshold` | `98.0` | usage % at which `watch` advances the clip |
| `five_hour_threshold` | `99.5` | usage % at which a run wrapper advances |
| `seven_day_threshold` | `99.5` | same, for the weekly window |
| `warm_threshold` | `50.0` | usage % at which the next account is validated ahead of time |
| `warm_model` | `claude-haiku-4-5-20251001` | model used for that validation ping |
| `min_switch_interval` | `20` | seconds; guards against swap loops |

## Where things live

| path | what |
| --- | --- |
| keychain `Claude Code-credentials` | the live Claude slot (owned by Claude Code) |
| keychain `claude-magazine` | stored Claude accounts |
| keychain `claude-magazine-codex` | stored Codex accounts |
| `~/.codex/auth.json` | the live Codex slot (owned by Codex) |
| `~/.claude-magazine/accounts.json` | the clip: order, labels, providers |
| `~/.claude-magazine/state.json` | current round, cooldowns, last-seen usage |
| `~/.claude-magazine/logs/mag.log` | every swap, with its reason |

Credentials are only ever held in the macOS keychain. `magazine` writes tokens to disk in exactly
one place — `~/.codex/auth.json`, because that is the file Codex itself reads — with mode `0600`.

## A word on fairness

This rotates between subscriptions **you pay for**, on one machine, for one person. That is the
only thing it is built for. Sharing one subscription across people, or running accounts you do not
pay for, is against the providers' terms — and this tool will not help you do it.

## License

MIT
