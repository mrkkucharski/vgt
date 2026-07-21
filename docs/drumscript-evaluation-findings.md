# DrumScript evaluation findings

Status: evaluation-only evidence for the deferred D-F routing decision.  It
does not change `vgt analyze`, REAPER tracks, production artifact names, or
sidecar fields.

## Reproducible harness

The corpus selected for the full evaluation is **IDMT-SMT-Drums V2** (Zenodo
record 7544164, file `IDMT-SMT-DRUMS-V2.zip`, MD5
`d2664b4c2aaa34b90ba2f57b389c5663`).  It supplies manual kick, snare, and
hi-hat onset annotations.  Fraunhofer makes it available for evaluation under
CC BY-NC-ND 4.0; therefore neither its audio nor derived clips are committed
to vgt.  This makes the smoke check a deliberately user-supplied-fixture
command, outside the normal test suite.

The benchmark command deliberately consumes a small manifest plus normalized
DrumScript JSON, rather than downloading data or invoking a model:

```sh
# Generate a manifest from the selected IDMT annotations, then retain it with
# the run report outside this repository (audio remains external).
uv run python scripts/idmt_drum_manifest.py /path/to/IDMT-SMT-DRUMS-V2 idmt-manifest.json
uv run python scripts/drumscript_benchmark.py idmt-manifest.json \
  --events-dir idmt-drumscript-events --output idmt-report.json

# Explicit, potentially downloading integration check; output must be a
# disposable directory, not vgt/<namespace>/transcription/.
uv run python scripts/drumscript_smoke.py /path/to/redistributable-drums.wav /tmp/vgt-drumscript-smoke
```

`drumscript_benchmark.py` reports precision, recall, F1, TP, FP, and FN for
every observed/annotated class at the fixed 50 ms tolerance.  Its macro and
global figures provide context only; the individual cymbal rows remain visible.
The deterministic checked-in fixture verifies the command shape and scoring
semantics without claiming to be a model-quality result:

```sh
uv run python scripts/drumscript_benchmark.py tests/fixtures/drum_evaluation/annotated-manifest.json \
  --events-dir tests/fixtures/drum_evaluation/events
```

| Class | Precision | Recall | F1 | TP | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| crash | 0.000 | 0.000 | 0.000 | 0 | 0 | 1 |
| kick | 1.000 | 0.500 | 0.667 | 1 | 0 | 1 |
| ride | 0.000 | 0.000 | 0.000 | 0 | 1 | 0 |
| snare | 1.000 | 1.000 | 1.000 | 1 | 0 | 0 |
| macro | 0.500 | 0.375 | 0.417 | — | — | — |
| global | 0.667 | 0.500 | 0.571 | 2 | 1 | 2 |

This is an intentionally adverse, tiny regression fixture, not an IDMT claim.
It demonstrates that a weak cymbal class cannot disappear behind the global
number.  The full-IDMT results must be appended below before D-F considers a
route switch.

## IDMT-SMT-Drums V2 automated run

Run on 2026-07-21 against the first sorted official mix clip,
`RealDrum01_00#MIX.wav`, using the verified archive checksum above.  This is a
reproducible **one-clip smoke benchmark**, not a representative corpus result:
the deliberately small selection establishes the end-to-end evidence path
without silently converting an expensive corpus/model run into normal CI.

```sh
uv run python scripts/idmt_drum_manifest.py /tmp/IDMT-SMT-DRUMS-V2 idmt-manifest.json
uv run python scripts/drumscript_smoke.py /tmp/IDMT-SMT-DRUMS-V2/audio/RealDrum01_00#MIX.wav /tmp/vgt-drumscript-smoke
# retain just RealDrum01_00#MIX and rename the normalized event JSON to
# <clip-id>.json, then run drumscript_benchmark.py as above
```

| Dataset identity | DrumScript/runtime | vgt Python / uv | Clips | Tolerance |
| --- | --- | --- | ---: | ---: |
| IDMT-SMT-Drums V2 / Zenodo 7544164 / `d2664b4c2aaa34b90ba2f57b389c5663` | `drumscript==0.1.6`, isolated Python 3.12.13 | Python 3.11.15 / uv 0.11.7 | 1 | 50 ms |

| Class | Precision | Recall | F1 | TP | FP | FN |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| kick | 0.000 | 0.000 | 0.000 | 0 | 2 | 23 |
| snare | 0.130 | 0.143 | 0.136 | 3 | 20 | 18 |
| hi_hat_closed | 0.312 | 0.357 | 0.333 | 15 | 33 | 27 |
| macro | 0.148 | 0.167 | 0.157 | — | — | — |
| global | 0.247 | 0.209 | 0.226 | 18 | 55 | 68 |

The output contained 49 DrumScript events and passed the normalized-MIDI/JSON
smoke validation.  This result is strongly negative evidence for a route
switch, especially the zero kick F1.  It is also too narrow to estimate
general performance: a future D-F decision needs a broader explicitly chosen
IDMT subset/full-corpus report and a cymbal-capable corpus, because IDMT's
annotations only cover kick, snare, and hi-hat.  No user-song listening or
REAPER work was involved.

## Temporary Basic Pitch shadow diagnostic

```sh
uv run python scripts/drumscript_shadow_compare.py drumscript-events.json basic-pitch-notes.csv \
  --output /tmp/vgt-drum-shadow.json
```

The report is named `temporary-evaluation-only-shadow-comparison`.  It groups
Basic Pitch note starts within 20 ms into unlabeled transient clusters, then
one-to-one matches them with DrumScript event onsets within 50 ms.  It only
contains `matched`, `drumscript_only`, and `basic_pitch_only` counts.  It does
not map Basic Pitch pitches to drum instruments, filter events, create MIDI or
REAPER tracks, write a production artifact name, or represent confidence.

## Limits and human-owned work

The integration smoke input must be redistributable or user-owned.  Real-song
listening, usefulness judgments, and any REAPER verification remain optional,
human-owned work and are neither test requirements nor blockers for this
evaluation tooling.
