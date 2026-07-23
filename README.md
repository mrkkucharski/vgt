# vgt

vgt non-destructively prepares existing REAPER projects for guitar practice. It
analyzes a reference mix, optionally separates practice stems with LALAL.AI,
creates local reference MIDI from requested stems, and adds only its own
`[vgt]`-managed objects.

Read the [user manual](docs/USER-MANUAL.md) for the current workflow, commands,
track states, correction process, cost controls, and regression contract.

## Install

With Python 3.11 and `uv`:

```sh
uv tool install git+https://github.com/mrkkucharski/vgt.git
vgt install-reascripts
```

`install-reascripts` installs the bundled `vgt_initialize.lua` and
`vgt_sync.lua` into `~/Library/Application Support/REAPER/Scripts/vgt`, so it
works equally from a wheel, Git URL, or local source. In REAPER, open the
Action List and use `ReaScript: Load` to register both files once. Use
`vgt install-reascripts --dry-run` to preview paths, or `--destination DIR`
to install into a test or nonstandard Scripts directory. Existing files that
differ from vgt's bundled versions are never replaced unless you confirm the
prompt or pass `--force`.

## Development checks

Run the full offline suite:

```sh
uv run pytest -q
```

CI runs the same tests with mocked LALAL API v1 fixtures; it does not receive
credentials or make live LALAL requests.
