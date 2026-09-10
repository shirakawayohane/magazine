# Offline demo

[Back to magazine](../README.md)

The README animation is a 40-second walkthrough of five real magazine commands.
Accounts, tokens, usage responses, and the interrupted-session record are synthetic.
The final command is a dry run; no live AI response, rate-limit recovery, or Workflow
cache replay is being shown. English output is selected explicitly.

The capture script runs the actual command parser, account registration, file-backed
credential writes, usage formatting, account switching, session scanning, and resume
preview. Provider/network boundaries are replaced with fixtures; external subprocess
and network calls fail the capture. Temporary files are cleaned up on normal completion.

## Try it without an account

From a clone, with Python 3.10+:

```sh
python3 scripts/demo.py
```

This requires neither Claude Code nor Codex and does not read your real credentials.

## Text walkthrough

1. `mag limits` shows Claude accounts `main` (97% of its five-hour window) and `spare`
   (12%), plus a Codex account (42% of its weekly window).
2. `mag use spare` changes the synthetic active Claude credential and confirms the account name.
3. `mag limits` shows the active marker beside `spare`.
4. `mag stalled` finds a synthetic Claude transcript containing a limit message.
5. `mag resume demo-session --dry-run` prints `[dry-run] claude --resume demo-session`.

[Complete captured output](assets/demo-transcript.json) · [Static preview](assets/demo.png)

## Regenerate the animation

Install Pillow in a development environment, then run:

```sh
python3 scripts/render_demo.py --font /path/to/a/monospace-font.ttf
```

On macOS the default font is the system Menlo font. Rendering produces five frames
at 1200 × 760 pixels, with durations totaling 40 seconds. It checks for clipped text.
No font file is redistributed and no image-generation model is used. Re-run after
changing relevant command output so the illustration stays tied to the implementation.
