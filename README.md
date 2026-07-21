# vgt

vgt non-destructively prepares existing REAPER projects for guitar practice. It
analyzes a reference mix, optionally separates practice stems with LALAL.AI,
and adds only its own `[vgt]`-managed objects.

Read the [user manual](docs/USER-MANUAL.md) for the current workflow, commands,
track states, correction process, cost controls, and regression contract.

## Install

With Python 3.11 and `uv`:

```sh
uv tool install .
vgt inspect "test/Reaper Project/Reaper Project.RPP"
```

## Development checks

Run the full offline suite:

```sh
uv run pytest -q
```

CI runs the same tests with mocked LALAL API v1 fixtures; it does not receive
credentials or make live LALAL requests.
