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

`install-reascripts` installs the bundled `vgt_initialize.lua`,
`vgt_sync.lua`, `vgt_sync_tempo_map.lua`, and `vgt_working_copy.lua` into
`~/Library/Application Support/REAPER/Scripts/vgt`, so it works equally from a
wheel, Git URL, or local source. In REAPER, open the Action List and use
`ReaScript: Load` to register all four files once. Run `vgt_initialize.lua`
to initialize a project and apply vgt's generated objects, `vgt_sync.lua` to
save chord, section, and key corrections, `vgt_sync_tempo_map.lua` to adopt a
tempo-map correction after its confirmation prompt, and `vgt_working_copy.lua`
to create a protected, user-owned `[work]` copy of a generated reference MIDI track. Use
`vgt install-reascripts --dry-run` to preview paths, or `--destination DIR`
to install into a test or nonstandard Scripts directory. Existing files that
differ from vgt's bundled versions are never replaced unless you confirm the
prompt or pass `--force`.

## Development checks

Run the full offline suite:

```sh
uv run pytest -q
```

The local suite uses mocked LALAL API v1 fixtures; it does not receive
credentials or make live LALAL requests.

Run this offline regression suite locally as the source of truth. GitHub
Actions is intentionally not part of verification for this hobby project while
hosted runners are billing-blocked. Every test gets isolated
pytest temporary paths; the runner fixes UTC, the C.UTF-8 locale, Python hash
ordering, pytest plugin loading, BLAS/Numba worker counts, and the Lua
executable. The suite covers the
CLI, sidecar migrations and locking, analysis and separation state machines,
ReaScript fixture behavior, package contents, and transcription routing/output
validation. LALAL uses `httpx.MockTransport`; Basic Pitch and DrumScript
subprocesses are fixture writers. It never supplies credentials, sends network
requests, downloads model environments, starts REAPER, or requires human
verification. `tests/test_analysis.py` mocks the expensive real-MP3 detector
calls while testing orchestration and sidecar behavior; the focused
tempo/key/section/chord detector tests retain their small local audio fixtures.
The executable `tests/test_goal_contract.py` acceptance contract is part of
that same local suite.
