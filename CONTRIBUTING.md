# Contributing

Bug reports, documentation improvements, and focused fixes are welcome.

For a bug, use the [issue form](https://github.com/shirakawayohane/magazine/issues/new/choose).
Include the OS, Python and provider CLI versions, and steps that reproduce the problem.
Replace account names/emails with examples. Never attach credential files or raw session logs.
For security issues, use the private route in [SECURITY.md](SECURITY.md).

Before opening a pull request:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/demo.py
```

On macOS/Linux, also run `bash -n install.sh shell/magazine.sh`.
The application and tests use Python's standard library. Pillow is needed only if
you regenerate the demo animation; see [the demo instructions](docs/demo.md).

Keep changes small and describe the user-visible problem and the result. For a bug fix,
include a regression test that fails before the fix. Use temporary directories and
synthetic credentials; tests must not touch the user's real accounts or call provider APIs.

For a substantial behavior change, open an Issue describing the use case first.
Provider formats evolve: record the versions actually tested and avoid claims of
compatibility that a mocked test cannot establish.
