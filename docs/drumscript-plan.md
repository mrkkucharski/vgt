# Drum MIDI with DrumScript - deferred implementation plan

Status: deferred - do not implement until the current Basic Pitch transcription
capability is fully delivered and all T-A through T-F work is closed.

Date: 2026-07-21

Basis: the user-provided research report *Converting Drum Stems to MIDI - Tools,
Workflows, and Pipelines*, DrumScript's public v0.1.6 source and documentation,
and vgt's accepted [stem transcription plan](transcription-plan.md).

## Decision

When this work starts, **DrumScript should replace Basic Pitch for the `drums`
target only**. Basic Pitch remains the backend for guitar, bass, vocals, piano,
strings, instrumental, backing, and original-mix targets.

Do not combine the two MIDI outputs in production and do not interpret agreement
between them as a confidence score. A short-lived evaluation mode may run both
backends on the same drum stem, but only to compare results before DrumScript is
made the default drum backend.

This is deliberately a deferred plan rather than a change to the current design
of record. The current transcription work should be completed, reviewed, and
stabilized first so this work starts from a known-good per-target cache, CLI,
status, and REAPER import contract.

## Why this is the preferred option

Basic Pitch and DrumScript solve different transcription problems:

- Basic Pitch produces pitched musical notes and pitch bends. On drum audio its
  pitches do not represent drum-kit instruments, so its output is not a useful
  groove map.
- DrumScript detects percussive onsets, classifies one or more drum instruments
  at each onset, and maps them to General MIDI percussion notes. Its current map
  covers kick, snare, low/mid/high toms, closed/open hi-hat, crash, and ride.
- DrumScript events currently contain timestamps, instrument labels, and debug
  features, but no calibrated probability. Its MIDI exporter also writes a fixed
  velocity of 100. Neither value can honestly be presented as model confidence.
- Turning Basic Pitch notes into a second opinion would require an arbitrary
  pitch-to-drum mapping. Agreement after that mapping would measure the heuristic
  more than either transcriber's accuracy.

For those reasons, the production contract should be one specialized backend per
target, not an ensemble. If DrumScript fails, vgt should record a per-target error
and continue the overall analysis; it must not silently fall back to Basic Pitch
and label that output as a drum transcription.

## Prerequisite gate

Do not create or start implementation issues from this plan until all of the
following are true:

1. The T-A through T-F work in [the current transcription plan](transcription-plan.md)
   is complete, reviewed, pushed, and closed.
2. `vgt analyze`, per-target caching, `vgt status`, and REAPER MIDI import have a
   stable delivered contract documented in the user manual.
3. The default Basic Pitch guitar workflow passes the full regression suite.
4. Any human-owned verification required by the current transcription plan has
   been recorded. This gate does not require a live REAPER check by an autonomous
   agent.

## Desired user-visible behavior

The existing CLI surface should remain unchanged:

```sh
vgt analyze --transcribe drums Song.RPP
```

The command persists `drums` in `requested_targets`, consumes the existing LALAL
drum stem, and creates:

- `vgt/<namespace>/transcription/drums.mid`
- `vgt/<namespace>/transcription/drums.json`
- `[vgt] Drums Ref (MIDI)` immediately below `[vgt] Drums` in REAPER

The MIDI must use General MIDI percussion notes on channel 10 and remain aligned
to the source audio at `reference_start`. It must be created, cached, forgotten,
and reapplied with the same non-destructive and idempotent behavior as every other
transcription target.

No new top-level CLI flag is needed merely to select the backend. Backend routing
is an implementation detail:

| Target | Default backend |
|---|---|
| `drums` | DrumScript |
| all other supported targets | Basic Pitch |

An explicit backend override may be added later for diagnostics, but it should not
be part of the first user-facing release.

## Architecture

### 1. Make transcription specs backend-aware

The current `TranscriptionSpec` is shaped around Basic Pitch settings such as
frequency bounds, onset/frame thresholds, pitch bends, and the Melodia option.
Do not populate those fields with meaningless drum values.

Refactor the spec contract into either:

- a small common spec plus typed backend-specific options; or
- separate `BasicPitchSpec` and `DrumScriptSpec` values behind the existing
  `Transcriber` protocol.

The DrumScript settings hash must cover at least:

- backend name and pinned package version;
- execution/runtime version;
- standard polyphonic versus rudiment classifier mode;
- time-signature input, if used;
- every future DrumScript option that can change event or MIDI output.

Changing DrumScript settings must invalidate only the drums entry. It must not
refresh guitar, bass, or any other cached target.

### 2. Route backends by target

Add a single target-to-backend selection point. Avoid `if target == "drums"`
checks scattered through analysis, CLI, and status code.

Conceptually:

```python
def backend_for_target(target: str) -> Transcriber:
    if target == "drums":
        return DrumScriptTranscriber()
    return BasicPitchTranscriber()
```

The fake test backend should follow the same routing seam so normal tests never
install or run DrumScript.

### 3. Run DrumScript in an isolated subprocess

DrumScript must not become a dependency of vgt's interpreter. Version 0.1.6
requires Python `>=3.9,<3.14`, NumPy `<2`, Torch, Torchaudio, and currently lists
Demucs as a mandatory dependency. vgt may run on a newer Python, already constrains
NumPy for its own MIR dependencies, and does not need DrumScript's separator.

Use a pinned isolated invocation, initially:

```sh
uvx --python 3.12 --from drumscript==0.1.6 drumscript /absolute/path/to/drums.wav
```

Run it with a fresh per-target temporary working directory. Pass the already
separated LALAL drum stem and do not enable DrumScript's `--full-song` behavior.
That prevents redundant Demucs separation and preserves vgt's existing source and
cache identity.

Add `VGT_DRUMSCRIPT_CMD`, analogous to `VGT_BASIC_PITCH_CMD`, so a user can point
vgt at a prebuilt offline installation. Parse the override with `shlex.split`; do
not invoke it through a shell.

The package pin is part of the cache settings hash. Upgrading it is an explicit,
reviewed change with an evaluation run, not an automatic dependency update.

### 4. Normalize DrumScript artifacts

DrumScript's public transcription call currently builds PDF, JSON, and MIDI
together. vgt needs only the event JSON and MIDI. Run the backend in temporary
storage, locate exactly one expected MIDI and JSON file, validate both, and move
them to the stable target names:

```text
transcription/drums.mid
transcription/drums.json
```

Discard the generated PDF and any other temporary files after successful
normalization. Do not expose temporary filenames in the sidecar.

The existing Basic Pitch `notes_file` remains a CSV. Add an optional
backend-neutral `events_file` for DrumScript rather than putting a JSON path in a
field documented as CSV. Old target records without `events_file` must continue to
load.

### 5. Validate independently of process exit status

Do not trust subprocess success alone. DrumScript v0.1.6 catches some PDF/MIDI
export failures internally, so successful execution may still lack a usable
artifact.

Validation must require:

- a readable, non-empty Standard MIDI File beginning with `MThd`;
- a JSON array whose entries have finite, non-negative `time_sec` values;
- a non-empty instrument list on each retained event;
- instrument names belonging to the supported mapping;
- event times not extending implausibly beyond the source duration;
- output paths contained in the per-target temporary directory;
- no unexpected duplicate MIDI or event files.

An empty but structurally valid transcription may be accepted for silent input,
but the sidecar and status output must make `0 events` visible.

### 6. Preserve timing and polyphony

DrumScript may classify several instruments at one onset. Preserve these as
simultaneous MIDI notes rather than collapsing the event to a single instrument.

Record both DrumScript's detected tempo and vgt's detected tempo for diagnostics.
DrumScript's MIDI note timing is based on absolute onset seconds; the imported
REAPER item remains time-based, so vgt must not quantize or stretch it merely to
make the two BPM estimates agree.

If vgt later introduces an explicit quantization feature, keep the raw event data
and raw MIDI as the machine baseline and make quantization a separate derived
artifact or user action.

### 7. Extend the target record and status output

A successfully transcribed drums record should include the existing common cache
and artifact fields plus drum-specific diagnostics:

```json
{
  "backend": "drumscript",
  "package_pin": "drumscript==0.1.6",
  "status": "transcribed",
  "midi_file": "transcription/drums.mid",
  "events_file": "transcription/drums.json",
  "event_count": 428,
  "instrument_counts": {
    "kick": 91,
    "snare": 87,
    "hi_hat_closed": 184
  },
  "first_event_s": 0.421,
  "last_event_s": 178.903,
  "backend_tempo": 118.0,
  "midi_tempo": 118.02,
  "confidence": null
}
```

The exact schema should reuse existing common names where they remain accurate.
Do not report `pitch_range_midi` as a musical range for drums: the values are
instrument selectors in the GM percussion map, not pitches. Do not invent a
confidence percentage.

Human-readable status should summarize event and instrument counts, for example:

```text
drums    428 events (kick 91, snare 87, hats 201, other 49), drumscript 0.1.6
```

### 8. Reuse the existing REAPER import

The existing `transcription/drums.mid` contract and `[vgt] Drums Ref (MIDI)` track
placement should remain valid. Confirm rather than rewrite the ReaScript path:

- the reference track follows the `[vgt] Drums` audio stem;
- the MIDI item begins at `reference_start`;
- the item is time-based;
- the track and item are registered as vgt-managed;
- apply remains idempotent;
- forgetting drums removes only vgt-owned drum transcription artifacts/tracks;
- the MIDI preserves channel 10 and simultaneous notes.

As with all reference MIDI, edits to the managed track do not survive reapply. The
user manual should continue to direct users to copy it to a user-owned track before
editing.

## Evaluation strategy

### Automated correctness

The normal test suite uses a fake drum transcriber and covers:

- target routing (`drums` versus every Basic Pitch target);
- settings-hash and per-target cache independence;
- missing drum-stem behavior;
- backend failure isolation;
- malformed/missing MIDI and JSON output;
- path-containment checks;
- zero-event results;
- multi-instrument events becoming simultaneous channel-10 notes;
- instrument-count status and JSON output;
- forget/removal behavior;
- unchanged REAPER placement and idempotency.

No default test may download or invoke real DrumScript.

Add an opt-in integration script that runs the pinned subprocess against a small,
redistributable drum fixture and verifies artifact structure. If no suitable audio
can legally be committed, document the command and keep this as a human-supplied
fixture check rather than placing it in the autonomous acceptance criteria.

### Quality evaluation

Before switching the production drums route, evaluate DrumScript on:

1. An annotated ADT dataset such as IDMT-SMT-Drums V2, using per-instrument
   precision, recall, and F1 with a 50 ms onset tolerance.
2. A small set of real LALAL drum stems representing simple, dense, cymbal-heavy,
   and fast material.
3. At least one stem with separation artifacts or melodic bleed.

Dataset evaluation can be automated. Listening and judging usefulness on the
user's songs is human-owned under [project agent rules](AGENTS.md).

Record per-class results rather than only a global score. A backend that finds
kicks and snares reliably but confuses open hats, rides, and crashes should be
described that way in the user manual.

### Temporary shadow comparison

For evaluation only, optionally retain the legacy Basic Pitch drums output outside
the production artifact name. Normalize it solely to an onset diagnostic:

1. Collapse Basic Pitch notes beginning within a small window into one transient
   cluster.
2. Match those clusters to DrumScript onsets within 50 ms.
3. Report matched, DrumScript-only, and Basic-Pitch-only onset counts.

This diagnostic may reveal missed transients, but it must not:

- map Basic Pitch pitches to drum instruments;
- filter DrumScript events;
- generate a user-visible second drums track;
- be stored as a confidence score;
- remain enabled after the evaluation decision.

## Rollout and fallback policy

1. Implement the backend and fake-backed tests without changing the default route.
2. Run the opt-in DrumScript smoke test and annotated evaluation.
3. Run the temporary shadow comparison on user-selected stems.
4. Have the user perform the listening/visual REAPER check.
5. If acceptable, change the single target-routing table so `drums` selects
   DrumScript and update documentation.
6. Remove or disable shadow artifacts and comparison code before declaring the
   work delivered.

After rollout, a DrumScript execution or validation failure yields `status: error`
for drums while other targets continue. No automatic Basic Pitch fallback is
allowed. A future explicit diagnostic override may remain available, but its
output must be clearly labeled as non-drum-aware.

## Suggested future issue breakdown

Create these issues only after the prerequisite gate is satisfied. Every created
issue must also receive the normal `status:queued` and priority labels required by
the repository's orchestration rules.

| Issue | Scope | Priority | Depends on |
|---|---|---|---|
| D-A | Backend-aware specs and target router; fake drum backend | high | current T-A through T-F complete |
| D-B | Pinned isolated `DrumScriptTranscriber`, command override, artifact normalization and validation | high | D-A |
| D-C | Drum event metadata, cache/status integration, forget behavior | medium | D-A, D-B |
| D-D | REAPER channel-10/polyphony regression coverage and opt-in smoke verifier | medium | D-B, D-C |
| D-E | Annotated benchmark plus temporary shadow comparison and written findings | medium | D-D |
| D-F | Switch default drums route, user documentation, remove shadow output | medium | D-E and human quality approval |

Do not label a parent issue blocked merely because these sub-issues are open; use
sub-issue ordering as described in the repository root `AGENTS.md`.

## Risks

- **Alpha upstream:** DrumScript is explicitly in public alpha and is still
  stabilizing its API and classifier. Pinning and output validation are mandatory.
- **Large cold environment:** mandatory Torch/Torchaudio/Demucs dependencies make
  the first isolated run heavier than the actual drum-only task requires.
- **Rule-based generalization:** physics thresholds may behave poorly on unusual
  kits, electronic drums, heavy processing, bleed, ghost notes, and dense cymbals.
- **No calibrated confidence:** debug features are useful for diagnosis but are not
  probabilities. UI and docs must not imply otherwise.
- **Fixed velocity:** v0.1.6 exports velocity 100, so dynamics are not preserved.
- **Exporter error handling:** current upstream code can catch export failures;
  artifact validation must remain vgt's responsibility.
- **Upstream output changes:** filenames and JSON shape may change before 1.0. The
  adapter must isolate that churn from the sidecar and REAPER contracts.

## Upstream references checked for this plan

- [DrumScript repository and public API](https://github.com/DrumScript/DrumScript)
- [DrumScript v0.1.6 package metadata and dependencies](https://github.com/DrumScript/DrumScript/blob/main/pyproject.toml)
- [DrumScript high-level transcription wrapper](https://github.com/DrumScript/DrumScript/blob/main/drumscript/__init__.py)
- [DrumScript score builder](https://github.com/DrumScript/DrumScript/blob/main/drumscript/notation_generator/score_builder.py)
- [DrumScript MIDI exporter](https://github.com/DrumScript/DrumScript/blob/main/drumscript/notation_generator/midi_exporter.py)
- [DrumScript GM drum mapping](https://github.com/DrumScript/DrumScript/blob/main/drumscript/notation_generator/constants.py)
